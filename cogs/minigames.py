import json
import random
import traceback
from datetime import datetime, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks
from discord import app_commands

from database import DB_PATH
from utils import minigame_store as store
from utils import minigame_engine as engine

# ═══════════════════════════════════════════════════════════════════════
# MINIGAMES v2 — the Discord surface (Phase 3 of the approved
# MINIGAMES_V2_PLAN.md).
#
# What this cog owns (plan §1, §21-Phase 3):
#   * the weekly pacing loop — `daily_check_loop` + compute_daily_probability
#     KEPT VERBATIM (D5: it decides only WHETHER an automatic spawn happens);
#   * the recursive weighted selection + shuffle-bag pop (plan §4/§5);
#   * the shared spawn path `spawn_game()` — auto / manual / test all go
#     through it (plan §8/§9/§14): snapshot → run row → real engine →
#     post → (auto only) weekly counter bump AFTER a successful post;
#   * `spawn_request_loop` (10s) — executes dashboard/slash spawn requests
#     (plan §9/§19: atomic claim, real engine, no fake implementation);
#   * the startup sweep — open run rows finalized as aborted_restart with a
#     best-effort embed note (plan §14, no-forfeit: no rewards);
#   * the v2 slash commands (/minigames_setup kept, /minigames_spawn new,
#     /minigames_stats kept) — the tier commands are RETIRED (plan §8).
#
# Deliberately does NOT import from other cogs (project rule). Reward
# granting goes through utils/reward_engine.py via the engine (the one
# shared engine every reward path in this project already uses). All
# minigame SQL lives in utils/minigame_store.py; all game logic lives in
# utils/minigame_engine.py (Phase 2).
# ═══════════════════════════════════════════════════════════════════════

# ── Weekly pacing tuning (kept verbatim from v1 — D5) ──────────────────
MIN_DAILY_PROB   = 0.10   # floor while still behind pace
MAX_DAILY_PROB   = 0.60   # ceiling while still behind pace
BONUS_DAILY_PROB = 0.15   # flat chance once weekly minimum is already met

CHECK_LOOP_MINUTES = 30
REQUEST_POLL_SECONDS = 10
# A process that just started has NO live in-memory games — its first
# ready may sweep every open row. A RECONNECT (same process) gets the
# plan's 5-minute grace window so a still-healthy in-memory run is never
# touched.
STARTUP_SWEEP_GRACE_MINUTES = 5


def _monday_of(d) -> str:
    monday = d.date() if hasattr(d, "date") else d
    from datetime import timedelta as _td
    monday = monday - _td(days=monday.weekday())
    return monday.isoformat()


def compute_daily_probability(events_so_far: int, weekday: int,
                               min_target: int, max_target: int) -> tuple[float, bool]:
    """
    weekday: 0=Monday ... 6=Sunday (datetime.weekday()).
    Returns (probability, force_fire).

    - Already at/above weekly max -> 0% (no bonus firing past the cap).
    - Last day of the week (Sunday) and still short of the minimum ->
      force fire regardless of probability.
    - Otherwise still behind the minimum -> probability scales with
      how many events are still needed vs. how many days are left,
      clamped to [MIN_DAILY_PROB, MAX_DAILY_PROB].
    - Minimum already met but max not reached -> flat small bonus
      chance (BONUS_DAILY_PROB) to occasionally exceed the minimum.
    """
    remaining_days = 7 - weekday  # today counts as 1

    if events_so_far >= max_target:
        return 0.0, False

    needed_min = max(0, min_target - events_so_far)

    if remaining_days <= 1 and needed_min > 0:
        return 1.0, True

    if needed_min > 0:
        prob = needed_min / remaining_days
        prob = max(MIN_DAILY_PROB, min(MAX_DAILY_PROB, prob))
        return prob, False

    return BONUS_DAILY_PROB, False


# ── Recursive weighted selection (plan §4) + shuffle bags (plan §5) ────

def _effective_playable(node: dict) -> int:
    """
    Count of playable (enabled + auto_spawn) templates reachable at or
    below this node UNDER THE ANCESTOR-ENABLED RULE: a disabled node
    contributes 0 for its whole branch (D10 — pure rotation exclusion,
    nothing is modified). `direct_playable` / `children` come from
    store.get_categories_tree().
    """
    if not node.get("enabled"):
        return 0
    total = node.get("direct_playable", 0)
    for child in node.get("children", []):
        total += _effective_playable(child)
    return total


def _pick_option(options: list[tuple]) -> tuple:
    """Weighted pick over (kind, target, weight) options — weights are
    clamped to >=1, relative only (D8: no template-level weights in v1)."""
    weights = [max(1, int(o[2] or 1)) for o in options]
    i = random.choices(range(len(options)), weights=weights, k=1)[0]
    return options[i]


async def select_template(guild_id: int) -> dict | None:
    """
    The recursive weighted traversal of plan §4:

      * A node is a candidate iff enabled AND it has >=1 playable
        template somewhere below (counted with the ancestor-enabled
        rule) — empty/disabled branches NEVER consume a selection.
      * At any node, its DIRECT playable templates (as one bag-option,
        weight = the node's weight) compete with its eligible
        subcategories (weight = each subcategory's own weight).
      * A bag hit pops the node's shuffle bag (without replacement,
        persisted, staleness-guarded — plan §5); a sub hit recurses.

    Returns a FRESH full template read (the §14 snapshot source) or
    None when nothing is eligible.
    """
    tree = await store.get_categories_tree(guild_id)
    if not tree:
        return None

    async def walk(node: dict):
        options: list[tuple] = []
        if node.get("direct_playable", 0) > 0:
            options.append(("bag", node, node.get("weight", 1)))
        for child in node.get("children", []):
            if _effective_playable(child) > 0:
                options.append(("sub", child, child.get("weight", 1)))
        if not options:
            return None
        kind, target, _weight = _pick_option(options)
        if kind == "sub":
            return await walk(target)
        ids = await store.get_direct_playable_ids(guild_id, node["id"])
        tid, _remaining = await store.pop_bag(guild_id, node["id"], ids)
        if tid is None:
            return None
        return await store.get_template(guild_id, tid)

    roots = [("root", r, r.get("weight", 1)) for r in tree
             if _effective_playable(r) > 0]
    if not roots:
        return None
    _kind, root, _weight = _pick_option(roots)
    return await walk(root)


# ── THE COG ─────────────────────────────────────────────────────────────

class Minigames(commands.Cog):
    """Minigames v2 — the bot side of the category/template system."""

    def __init__(self, bot):
        self.bot = bot
        # log_id → live engine. The engines keep themselves alive via
        # their timer tasks; this registry is for observability and
        # bounded memory (finished entries are pruned on every poll tick
        # — a finished engine is referenced by its closed log row).
        self.live_games: dict[int, engine.MinigameEngine] = {}
        # False until the first on_ready of THIS process has swept.
        self._sweep_done = False
        self.daily_check_loop.start()
        self.spawn_request_loop.start()

    def cog_unload(self):
        self.daily_check_loop.cancel()
        self.spawn_request_loop.cancel()

    async def cog_load(self):
        await store.ensure_tables()

    # ── Startup sweep (plan §14) ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        await self._startup_sweep()

    async def _startup_sweep(self):
        """
        Finalize run rows that are 'running' but have no live in-memory
        engine behind them (plan §14): a fresh process sweeps EVERY open
        row (a new process has no live games — applying the 5-minute
        grace here would leave runs started <5min before a crash stuck
        forever); a reconnect (same process, live games may still be
        healthy) only touches rows older than the grace window.
        Idempotent: the sweep only sees rows with ended_at IS NULL, and
        finish_run sets ended_at.
        """
        grace = 0 if not self._sweep_done else STARTUP_SWEEP_GRACE_MINUTES
        self._sweep_done = True
        for row in await store.get_open_runs(max_age_minutes=grace):
            try:
                await store.finish_run(row["id"], "aborted_restart")
            except Exception as e:
                print(f"[MINIGAMES] sweep: closing run {row['id']} failed: {e}")
                continue
            print(f"[MINIGAMES] sweep: run {row['id']} (guild {row['guild_id']}) "
                  f"finalized as aborted_restart — no rewards")
            await self._notify_aborted(row)

    async def _notify_aborted(self, row: dict):
        """Best-effort '⚠️ bot restarted' note on the original message.
        Never blocks the sweep, never touches rewards (there are none —
        the row was closed before this runs)."""
        try:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None or not row.get("channel_id") or not row.get("message_id"):
                return
            channel = guild.get_channel(int(row["channel_id"]))
            if channel is None:
                return
            msg = await channel.fetch_message(int(row["message_id"]))
            embed = msg.embeds[0] if msg.embeds else discord.Embed(description="")
            base = embed.description or ""
            embed.description = (base + "\n\n⚠️ **Game ended — bot "
                                   "restarted**")[:4096]
            await msg.edit(embed=embed)
        except Exception as e:
            print(f"[MINIGAMES] sweep: abort notice for run {row['id']} "
                  f"failed (row still closed): {e}")

    # ── Channel resolution (plan §8 step 4 / §17 edge matrix) ─────────

    @staticmethod
    def _resolve_channel(guild: discord.Guild, template: dict,
                         config: dict) -> tuple[discord.TextChannel | None, str | None]:
        """
        Template override first, then the guild default (a deleted
        override falls back to the default — plan §17). Returns
        (channel, error).
        """
        candidates = [template.get("channel_id"), config.get("channel_id")]
        tried: set[int] = set()
        for cid in candidates:
            try:
                cid = int(cid) if cid else None
            except (TypeError, ValueError):
                cid = None
            if not cid or cid in tried:
                continue
            tried.add(cid)
            ch = guild.get_channel(cid)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                return ch, None
        if tried:
            return None, ("no valid spawn channel — the template's channel "
                          "is missing and the configured guild default is "
                          "missing too")
        return None, "no spawn channel configured (run /minigames_setup)"

    # ── Shared spawn path (plan §8/§9/§14) ─────────────────────────────

    async def spawn_game(self, guild: discord.Guild, template: dict,
                         mode: str) -> tuple[bool, str | None]:
        """
        THE shared spawn path for auto / manual / test:
          1. resolve + validate the channel (BEFORE the run row — a
             channel failure leaves no phantom row, plan §17);
          2. SNAPSHOT the template (deep copies — plan §14: after start,
             nothing reads the template again);
          3. open the run row (auditable even if the post fails);
          4. real engine → post message + arm timers;
          5. store the message id, register the live engine.
        Returns (ok, error). The weekly counter is NOT touched here —
        the auto caller bumps it only on success (D5).
        """
        config = await store.get_config(guild.id)
        channel, ch_err = self._resolve_channel(guild, template, config)
        if channel is None:
            return False, ch_err

        category = await store.get_category(guild.id, template["category_id"])
        snapshot = {
            "guild_id": guild.id,
            "template_id": template["id"],
            "name": template["name"],
            "game_type": template["game_type"],
            "category_id": template["category_id"],
            "category_name": (category or {}).get("name")
                              or template["name"],
            # §14 deep copies — fresh JSON parses on purpose:
            "embed": json.loads(json.dumps(template.get("embed") or {})),
            "config": json.loads(json.dumps(template.get("config") or {})),
            "rewards": json.loads(json.dumps(template.get("rewards") or [])),
            "channel_id": channel.id,
        }

        log_id = await store.start_run(
            guild.id, template["id"], template["name"],
            template["category_id"], snapshot["category_name"],
            template["game_type"], mode, channel.id)

        try:
            game = engine.make_engine(snapshot, mode, log_id, bot=self.bot)
            message = await game.start(channel)
        except ValueError as e:
            # Unplayable template (e.g. no answers) — pref light refusal.
            await store.finish_run(log_id, "failed")
            return False, f"template cannot be spawned: {e}"
        except Exception as e:
            print(f"[MINIGAMES] guild={guild.id} run {log_id} post failed: {e}")
            await store.finish_run(log_id, "failed")
            return False, "failed to post the game message"

        await store.set_run_message(log_id, message.id)
        self.live_games[log_id] = game
        return True, None

    # ── Automatic spawn (plan §8) ──────────────────────────────────────

    async def _auto_spawn(self, guild: discord.Guild, config: dict,
                          forced: bool) -> bool:
        template = await select_template(guild.id)
        if not template:
            print(f"[MINIGAMES] guild={guild.id} no eligible template — "
                  f"nothing spawns, counter NOT incremented, retried at "
                  f"the next daily check")
            return False
        ok, err = await self.spawn_game(guild, template, "auto")
        if not ok:
            print(f"[MINIGAMES] guild={guild.id} auto spawn of "
                  f"'{template['name']}' failed: {err} — counter NOT "
                  f"incremented, retried at the next daily check")
            return False
        # D5: the weekly counter increments ONLY after the message posts.
        await store.bump_events_this_week(guild.id)
        return True

    @tasks.loop(minutes=CHECK_LOOP_MINUTES)
    async def daily_check_loop(self):
        await self._daily_check_iteration()

    async def _daily_check_iteration(self):
        """The pacing iteration (extracted so tests can drive one pass
        without waiting 30 minutes — the loop body is unchanged)."""
        now   = datetime.now(timezone.utc)
        today = now.date().isoformat()
        monday = _monday_of(now)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT guild_id FROM minigames_config WHERE enabled = 1")
            guild_ids = [r[0] for r in await cursor.fetchall()]

        for guild_id in guild_ids:
            try:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                config = await store.get_config(guild_id)

                # Weekly reset — new week started since last recorded
                # week_start_date.
                if config.get("week_start_date") != monday:
                    await store.mark_week(
                        guild_id, monday, config.get("last_check_date"),
                        events_this_week=0)
                    config["events_this_week"] = 0
                    config["week_start_date"] = monday

                # Already rolled today for this guild.
                if config.get("last_check_date") == today:
                    continue

                min_target = int(config.get("min_events_per_week") or 5)
                max_target = int(config.get("max_events_per_week") or 10)
                events_so_far = int(config.get("events_this_week") or 0)
                weekday = now.weekday()

                prob, force = compute_daily_probability(
                    events_so_far, weekday, min_target, max_target)

                roll_success = force or (random.random() < prob)

                await store.mark_week(guild_id, monday, today)

                if roll_success:
                    await self._auto_spawn(guild, config, forced=force)

            except Exception as e:
                print(f"[MINIGAMES] daily_check_loop error for guild {guild_id}: {e}")

    @daily_check_loop.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    # ── Spawn request queue (plan §9/§19) ──────────────────────────────

    @tasks.loop(seconds=REQUEST_POLL_SECONDS)
    async def spawn_request_loop(self):
        if not self.bot.is_ready():
            return
        try:
            await self._process_spawn_requests()
        except Exception as e:
            print(f"[MINIGAMES] spawn_request_loop error: {e}")
            traceback.print_exc()

    async def _process_spawn_requests(self):
        # Bounded memory: drop finished engines from the registry (their
        # log rows are already closed — the row is the record).
        if self.live_games:
            self.live_games = {k: v for k, v in self.live_games.items()
                               if not v.finished}

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT DISTINCT guild_id FROM minigame_spawn_requests "
                "WHERE status = 'pending'")
            guild_ids = [r[0] for r in await cursor.fetchall()]

        for gid in guild_ids:
            guild = self.bot.get_guild(gid)
            if guild is None:
                # The bot is not in this guild — the request can never
                # run. Close its pending rows instead of letting them
                # rot (the dashboard surfaces the error, plan §9).
                for req in await store.get_in_flight_requests(gid):
                    if req["status"] == "pending":
                        await store.finish_request(
                            req["id"], False, "bot is not in this guild")
                continue

            req = await store.claim_next_request(gid)
            if not req:
                continue
            try:
                template = await store.get_template(gid, req["template_id"])
                if template is None:
                    await store.finish_request(
                        req["id"], False, "template no longer exists")
                    continue
                # D12: test → always; manual (specific template) → the
                # template's own enabled toggle (category state is
                # irrelevant).
                if req["mode"] == "manual" and not template.get("enabled"):
                    await store.finish_request(
                        req["id"], False, "template is disabled")
                    continue
                ok, err = await self.spawn_game(guild, template, req["mode"])
                await store.finish_request(req["id"], ok, err)
            except Exception as e:
                print(f"[MINIGAMES] request {req['id']} (guild {gid}) "
                      f"failed: {e}")
                traceback.print_exc()
                try:
                    await store.finish_request(req["id"], False, str(e)[:400])
                except Exception:
                    pass

    # ─── SLASH COMMANDS ───────────────────────────────────────────────

    @app_commands.command(name="minigames_setup",
                          description="Configure the minigames spawn system (channel + weekly range)")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_setup(self, interaction: discord.Interaction,
                              channel: discord.TextChannel,
                              min_events: int = 5,
                              max_events: int = 10,
                              enabled: bool = True):
        if min_events < 1 or max_events < min_events:
            await interaction.response.send_message(
                "min_events must be >= 1 and max_events must be >= min_events.",
                ephemeral=True)
            return
        await store.save_config(
            interaction.guild.id,
            enabled=int(bool(enabled)),
            channel_id=channel.id,
            min_events_per_week=min_events,
            max_events_per_week=max_events)

        embed = discord.Embed(title="Minigames Configured",
                              color=0x57F287 if enabled else 0xED4245)
        embed.add_field(name="Status", value="Enabled" if enabled else "Disabled")
        embed.add_field(name="Spawn channel", value=channel.mention)
        embed.add_field(name="Weekly range", value=f"{min_events}–{max_events} events")
        embed.add_field(name="Note",
                        value="Categories, templates and rewards are managed "
                              "in the dashboard.",
                        inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="minigames_spawn",
                          description="Spawn a minigame right now (manual — admin)")
    @app_commands.describe(
        template_id="Optional: force a specific template by ID. "
                    "Omit to let the rotation pick a game.")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_spawn(self, interaction: discord.Interaction,
                              template_id: int | None = None):
        await interaction.response.defer(ephemeral=True)
        requester = f"slash:{interaction.user.id}"
        ok, message = await self._command_spawn(
            interaction.guild, template_id, requester)
        await interaction.followup.send(message, ephemeral=True)

    async def _command_spawn(self, guild: discord.Guild,
                             template_id: int | None,
                             requester: str) -> tuple[bool, str]:
        """
        Manual spawn from a slash command (plan §8/§9): ONE shared path
        — the request queue. spawn_request_loop executes it with the real
        engine (~10s latency, the accepted D4 queue delay). D12: a
        specific template must be enabled (category state irrelevant);
        no template → the §4 recursive selection. Never touches the
        weekly counter.
        """
        if template_id is not None:
            tpl = await store.get_template(guild.id, template_id)
            if tpl is None:
                return False, "❌ No such template in this server."
            if not tpl.get("enabled"):
                return False, "❌ That template is disabled."
        else:
            tpl = await select_template(guild.id)
            if tpl is None:
                return False, ("❌ Nothing eligible right now — no enabled "
                               "template with automatic rotation on.")
        _rid, err = await store.create_spawn_request(
            guild.id, tpl["id"], "manual", requested_by=requester)
        if err:
            return False, f"❌ {err}."
        return True, f"✅ Queued **{tpl['name']}** — it will appear in a " \
                     "moment."

    @app_commands.command(name="minigames_stats",
                          description="View this week's minigames progress")
    async def minigames_stats(self, interaction: discord.Interaction):
        config = await store.get_config(interaction.guild.id)
        now = datetime.now(timezone.utc)
        weekday = now.weekday()
        remaining_days = 7 - weekday
        min_t = int(config.get("min_events_per_week") or 5)
        max_t = int(config.get("max_events_per_week") or 10)
        so_far = int(config.get("events_this_week") or 0)
        prob, force = compute_daily_probability(so_far, weekday, min_t, max_t)

        embed = discord.Embed(title="🎲 Minigames — This Week", color=0x7c5cbf)
        embed.add_field(name="Status", value="Enabled" if config.get("enabled") else "Disabled")
        embed.add_field(name="Automatic spawns so far", value=f"{so_far} / {min_t}–{max_t}")
        embed.add_field(name="Days left in week", value=str(remaining_days))
        embed.add_field(
            name="Today's spawn chance",
            value="Forced (minimum not met)" if force else f"{prob*100:.0f}%")
        embed.add_field(
            name="Manual spawns",
            value="via /minigames_spawn or the dashboard (never counted "
                  "toward the weekly range)",
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── RETIRED (Phase 6 — migration verified, Phase 5 retirement pass) ──
# The Phase 4/6 compatibility window is CLOSED:
#   - VALID_TIERS and get_tiers() — the old dashboard page/API that
#     read them was replaced in Phase 4 (dashboard/api/minigames.py is
#     now store-based); nothing in production imported them anymore.
#   - the ensure_tables()/get_config() aliases — every remaining
#     caller uses utils.minigame_store directly.
# The tier SYSTEM (slash commands, MinigameClaimView, legacy
# claim/spawn flow) was removed in Phase 3 (plan §8).
#
# KEPT on purpose:
#   - the legacy TABLES (minigames_tiers / minigames_config /
#     minigames_log with their v1 columns) — never dropped, so
#     redeploying the old code finds its data intact (plan §3.4);
#   - get_user_win_count() — a LIVE consumer: utils/rank_card_data.py
#     reads the legacy winner_id column, which v2 rows also fill via
#     the first-winner mapping.
# Do not rebuild removed shims.
# ═══════════════════════════════════════════════════════════════════════

async def get_user_win_count(guild_id: int, user_id: int) -> int:
    """
    Rank Card foundation: minigames_log records winner_id per run —
    used by utils/rank_card_data.py. (Kept: it reads the LEGACY column,
    which v2 rows also fill via the first-winner mapping.)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM minigames_log WHERE guild_id = ? AND winner_id = ?",
            (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else 0


async def setup(bot):
    await bot.add_cog(Minigames(bot))
