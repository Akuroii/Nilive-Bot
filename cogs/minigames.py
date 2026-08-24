import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import random
from datetime import datetime, timezone
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso

# ═══════════════════════════════════════════════════════════════════════
# EVENT STACK BUILDER ("Minigames")
#
# Distinct, standalone automated system — NOT the same thing as
# cogs/events.py (manual admin-triggered button-race events, which
# stays untouched). This is a quota-driven weekly scheduler:
#
#   - Each guild configures a min/max number of events per week
#     (default 5-10).
#   - Every day, an adaptive spawn probability is computed from how
#     many events have already fired this week vs. how many days are
#     left — behind pace pushes probability up, ahead of pace pushes
#     it down, and it's capped at 0 once the weekly max is hit.
#   - On the final day of the week (Sunday), if the weekly minimum
#     still hasn't been met, the event is force-fired regardless of
#     the rolled probability.
#   - The week resets every Monday (weekday index 0).
#
# Deliberately does NOT import from cogs/events.py — per project rule,
# engines/systems must not import each other in load-order-sensitive
# ways. Reward granting goes through utils/reward_engine.py (the one
# shared engine every reward path in this project already uses), but
# the event-spawn/claim mechanics here are self-contained.
# ═══════════════════════════════════════════════════════════════════════

VALID_TIERS = ("bronze", "silver", "gold", "platinum")
TIER_COLOR = {
    "bronze":   0xCD7F32,
    "silver":   0xC0C0C0,
    "gold":     0xFFD700,
    "platinum": 0xB9F2FF,
}
TIER_EMOJI = {
    "bronze":   "🥉",
    "silver":   "🥈",
    "gold":     "🥇",
    "platinum": "💎",
}

# Daily-probability tuning. Kept as module constants rather than
# per-guild config for now — these shape the pacing curve itself, not
# a guild-facing setting like min/max events are.
MIN_DAILY_PROB   = 0.10   # floor while still behind pace
MAX_DAILY_PROB   = 0.60   # ceiling while still behind pace
BONUS_DAILY_PROB = 0.15   # flat chance once weekly minimum is already met

CHECK_LOOP_MINUTES = 30


async def ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigames_config (
                guild_id            INTEGER PRIMARY KEY,
                enabled             INTEGER DEFAULT 1,
                channel_id          INTEGER,
                min_events_per_week INTEGER DEFAULT 5,
                max_events_per_week INTEGER DEFAULT 10,
                events_this_week    INTEGER DEFAULT 0,
                week_start_date     TEXT,
                last_check_date     TEXT,
                claim_seconds       INTEGER DEFAULT 300,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigames_tiers (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id               INTEGER NOT NULL,
                tier                   TEXT NOT NULL,
                weight                 INTEGER DEFAULT 1,
                reward_type            TEXT NOT NULL,
                reward_value           TEXT NOT NULL,
                reward_duration_hours  INTEGER,
                enabled                INTEGER DEFAULT 1,
                created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgt_guild
            ON minigames_tiers(guild_id)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigames_log (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id           INTEGER NOT NULL,
                event_date         TEXT NOT NULL,
                tier               TEXT NOT NULL,
                channel_id         INTEGER,
                message_id         INTEGER,
                winner_id          INTEGER,
                winner_display_name TEXT,
                forced             INTEGER DEFAULT 0,
                fired_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgl_guild
            ON minigames_log(guild_id, event_date)
        """)
        await db.commit()


async def get_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM minigames_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return {
        "guild_id": guild_id, "enabled": 1, "channel_id": None,
        "min_events_per_week": 5, "max_events_per_week": 10,
        "events_this_week": 0, "week_start_date": None,
        "last_check_date": None, "claim_seconds": 300,
    }


async def get_tiers(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, tier, weight, reward_type, reward_value,
                   reward_duration_hours, enabled
            FROM minigames_tiers
            WHERE guild_id = ? AND enabled = 1
        """, (guild_id,))
        rows = await cursor.fetchall()
    return [{
        "id": r[0], "tier": r[1], "weight": r[2],
        "reward_type": r[3], "reward_value": r[4],
        "reward_duration_hours": r[5], "enabled": r[6],
    } for r in rows]


# Rank Card foundation: minigames_log already records winner_id per
# fired event — no new counter/table needed. Used by the future
# /rank card's "mini games" stat (utils/rank_card_data.py).
async def get_user_win_count(guild_id: int, user_id: int) -> int:
    # Resilient to minigames_log not existing yet: this is called by
    # utils/rank_card_data.py, which can run in the DASHBOARD process
    # (that never loads cogs, so ensure_tables() may not have run) or
    # before the minigames cog has fired for the first time. A missing
    # table means "this guild has simply never had a minigame" — which
    # is 0 wins, not a crash that takes down the whole rank card
    # backend. Matches the safe-default style used by get_catalog_entry
    # / get_equipped elsewhere in this feature.
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM minigames_log
                WHERE guild_id = ? AND winner_id = ?
            """, (guild_id, user_id))
            row = await cursor.fetchone()
        return row[0] if row else 0
    except aiosqlite.OperationalError:
        return 0


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


def pick_weighted_tier(tiers: list[dict]) -> dict | None:
    if not tiers:
        return None
    weights = [max(1, t["weight"]) for t in tiers]
    return random.choices(tiers, weights=weights, k=1)[0]


class MinigameClaimView(discord.ui.View):
    """Single-winner, first-click-wins claim button for a spawned event."""

    def __init__(self, guild_id: int, tier: dict, log_id: int, claim_seconds: int):
        super().__init__(timeout=claim_seconds)
        self.guild_id  = guild_id
        self.tier      = tier
        self.log_id    = log_id
        self.claimed   = False
        self.message: discord.Message | None = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Claim!", emoji="🎉", style=discord.ButtonStyle.success,
                       custom_id="minigame_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            await interaction.response.send_message(
                "Someone already claimed this one — better luck next time!",
                ephemeral=True)
            return
        self.claimed = True

        from utils.reward_engine import give_reward
        result = await give_reward(
            interaction.client, self.guild_id, interaction.user.id,
            self.tier["reward_type"],
            amount=self.tier["reward_value"] if self.tier["reward_type"] in ("coins", "diamonds", "xp") else None,
            role_id=self.tier["reward_value"] if self.tier["reward_type"] in ("role", "temp_role") else None,
            item_name=self.tier["reward_value"] if self.tier["reward_type"] == "item" else None,
            duration_hours=self.tier.get("reward_duration_hours"),
            reason=f"Minigame reward ({self.tier['tier']})",
            source="minigame",
        )

        snap = snapshot_user(interaction.user)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE minigames_log
                SET winner_id = ?, winner_display_name = ?
                WHERE id = ?
            """, (interaction.user.id, snap["display_name"], self.log_id))
            await db.commit()

        for item in self.children:
            item.disabled = True

        emoji = TIER_EMOJI.get(self.tier["tier"], "🎁")
        if not result.get("success"):
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                f"⚠️ Claim recorded but reward grant failed: {result.get('error')}",
                ephemeral=True)
            return

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"{emoji} You claimed the **{self.tier['tier'].title()}** reward!",
            ephemeral=True)
        try:
            await interaction.channel.send(
                f"{emoji} {interaction.user.mention} claimed the "
                f"**{self.tier['tier'].title()}** minigame reward!")
        except Exception:
            pass


class Minigames(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_check_loop.start()

    def cog_unload(self):
        self.daily_check_loop.cancel()

    async def cog_load(self):
        await ensure_tables()

    async def _spawn_event(self, guild: discord.Guild, config: dict,
                            forced: bool) -> bool:
        channel_id = config.get("channel_id")
        if not channel_id:
            print(f"[MINIGAMES] guild={guild.id} has no channel configured — skipping spawn")
            return False
        channel = guild.get_channel(int(channel_id))
        if not channel:
            print(f"[MINIGAMES] guild={guild.id} configured channel {channel_id} not found")
            return False

        tiers = await get_tiers(guild.id)
        tier = pick_weighted_tier(tiers)
        if not tier:
            print(f"[MINIGAMES] guild={guild.id} has no reward tiers configured — skipping spawn")
            return False

        today = datetime.now(timezone.utc).date().isoformat()
        claim_seconds = int(config.get("claim_seconds") or 300)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO minigames_log
                    (guild_id, event_date, tier, channel_id, forced)
                VALUES (?, ?, ?, ?, ?)
            """, (guild.id, today, tier["tier"], channel.id, int(forced)))
            await db.commit()
            log_id = cursor.lastrowid

        emoji = TIER_EMOJI.get(tier["tier"], "🎁")
        embed = discord.Embed(
            title=f"{emoji} A Wild Minigame Appeared!",
            description=(
                f"First person to click **Claim!** wins a "
                f"**{tier['tier'].title()}** reward!\n"
                f"Expires in {claim_seconds // 60} minute(s)."),
            color=TIER_COLOR.get(tier["tier"], 0x7c5cbf))
        embed.set_footer(text=f"Tier: {tier['tier'].title()}")

        view = MinigameClaimView(guild.id, tier, log_id, claim_seconds)
        try:
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
        except Exception as e:
            print(f"[MINIGAMES] guild={guild.id} failed to send spawn message: {e}")
            return False

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE minigames_log SET message_id = ? WHERE id = ?",
                (msg.id, log_id))
            await db.execute("""
                UPDATE minigames_config
                SET events_this_week = events_this_week + 1
                WHERE guild_id = ?
            """, (guild.id,))
            await db.commit()

        return True

    @tasks.loop(minutes=CHECK_LOOP_MINUTES)
    async def daily_check_loop(self):
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

                config = await get_config(guild_id)

                # Weekly reset — new week started since last recorded
                # week_start_date.
                if config.get("week_start_date") != monday:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            UPDATE minigames_config
                            SET events_this_week = 0, week_start_date = ?
                            WHERE guild_id = ?
                        """, (monday, guild_id))
                        await db.commit()
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

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        UPDATE minigames_config
                        SET last_check_date = ?
                        WHERE guild_id = ?
                    """, (today, guild_id))
                    await db.commit()

                if roll_success:
                    await self._spawn_event(guild, config, forced=force)

            except Exception as e:
                print(f"[MINIGAMES] daily_check_loop error for guild {guild_id}: {e}")

    @daily_check_loop.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    # ─── SLASH COMMANDS ──────────────────────────────────

    @app_commands.command(name="minigames_setup",
                          description="Configure the Event Stack Builder (minigames)")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_setup(self, interaction: discord.Interaction,
                              channel: discord.TextChannel,
                              min_events: int = 5,
                              max_events: int = 10,
                              claim_seconds: int = 300,
                              enabled: bool = True):
        if min_events < 1 or max_events < min_events:
            await interaction.response.send_message(
                "min_events must be >= 1 and max_events must be >= min_events.",
                ephemeral=True)
            return
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO minigames_config
                    (guild_id, enabled, channel_id, min_events_per_week,
                     max_events_per_week, claim_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled              = excluded.enabled,
                    channel_id           = excluded.channel_id,
                    min_events_per_week  = excluded.min_events_per_week,
                    max_events_per_week  = excluded.max_events_per_week,
                    claim_seconds        = excluded.claim_seconds,
                    updated_at           = CURRENT_TIMESTAMP
            """, (interaction.guild.id, int(enabled), channel.id,
                  min_events, max_events, claim_seconds))
            await db.commit()

        embed = discord.Embed(title="Event Stack Builder Configured",
                              color=0x57F287 if enabled else 0xED4245)
        embed.add_field(name="Status", value="Enabled" if enabled else "Disabled")
        embed.add_field(name="Channel", value=channel.mention)
        embed.add_field(name="Weekly range", value=f"{min_events}–{max_events} events")
        embed.add_field(name="Claim window", value=f"{claim_seconds}s")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="minigames_tier_add",
                          description="Add a reward tier for minigame spawns")
    @app_commands.describe(
        tier="bronze / silver / gold / platinum",
        weight="Relative weight for random selection (higher = more common)",
        reward_type="coins / diamonds / xp / role / temp_role / item",
        reward_value="Amount (coins/diamonds/xp) or Role ID or Item name",
        duration_hours="Only used for temp_role")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_tier_add(self, interaction: discord.Interaction,
                                 tier: str, reward_type: str, reward_value: str,
                                 weight: int = 1, duration_hours: int = None):
        tier = tier.lower().strip()
        if tier not in VALID_TIERS:
            await interaction.response.send_message(
                f"tier must be one of: {', '.join(VALID_TIERS)}", ephemeral=True)
            return
        valid_reward_types = ("coins", "diamonds", "xp", "role", "temp_role", "item")
        if reward_type not in valid_reward_types:
            await interaction.response.send_message(
                f"reward_type must be one of: {', '.join(valid_reward_types)}",
                ephemeral=True)
            return
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO minigames_tiers
                    (guild_id, tier, weight, reward_type, reward_value,
                     reward_duration_hours)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (interaction.guild.id, tier, max(1, weight),
                  reward_type, reward_value, duration_hours))
            await db.commit()
        await interaction.response.send_message(
            f"{TIER_EMOJI.get(tier, '🎁')} Added **{tier}** tier "
            f"({reward_type}: {reward_value}, weight {max(1, weight)}).",
            ephemeral=True)

    @app_commands.command(name="minigames_tier_list",
                          description="List configured minigame reward tiers")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_tier_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, tier, weight, reward_type, reward_value,
                       reward_duration_hours, enabled
                FROM minigames_tiers WHERE guild_id = ?
                ORDER BY tier ASC, id ASC
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No reward tiers configured yet. Use /minigames_tier_add.",
                ephemeral=True)
            return
        embed = discord.Embed(title="🎁 Minigame Reward Tiers", color=0x7c5cbf)
        for (tid, t, w, rtype, rval, dur, enabled) in rows:
            status = "✅" if enabled else "❌"
            dur_str = f" ({dur}h)" if dur else ""
            embed.add_field(
                name=f"#{tid} {TIER_EMOJI.get(t, '🎁')} {t.title()} {status}",
                value=f"{rtype}: {rval}{dur_str} · weight {w}",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="minigames_tier_remove",
                          description="Remove a minigame reward tier by ID")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_tier_remove(self, interaction: discord.Interaction, tier_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM minigames_tiers WHERE id = ? AND guild_id = ?",
                (tier_id, interaction.guild.id))
            await db.commit()
            found = cursor.rowcount > 0
        if found:
            await interaction.response.send_message(f"Removed tier #{tier_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No tier #{tier_id} found.", ephemeral=True)

    @app_commands.command(name="minigames_force",
                          description="Force-spawn a minigame right now (admin, ignores probability)")
    @app_commands.checks.has_permissions(administrator=True)
    async def minigames_force(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = await get_config(interaction.guild.id)
        if not config.get("channel_id"):
            await interaction.followup.send(
                "No channel configured — run /minigames_setup first.")
            return
        ok = await self._spawn_event(interaction.guild, config, forced=True)
        if ok:
            await interaction.followup.send("✅ Minigame spawned.")
        else:
            await interaction.followup.send(
                "❌ Couldn't spawn — check that a channel and at least one "
                "reward tier are configured.")

    @app_commands.command(name="minigames_stats",
                          description="View this week's Event Stack Builder progress")
    async def minigames_stats(self, interaction: discord.Interaction):
        config = await get_config(interaction.guild.id)
        now = datetime.now(timezone.utc)
        weekday = now.weekday()
        remaining_days = 7 - weekday
        min_t = int(config.get("min_events_per_week") or 5)
        max_t = int(config.get("max_events_per_week") or 10)
        so_far = int(config.get("events_this_week") or 0)
        prob, force = compute_daily_probability(so_far, weekday, min_t, max_t)

        embed = discord.Embed(title="🎲 Event Stack Builder — This Week", color=0x7c5cbf)
        embed.add_field(name="Status", value="Enabled" if config.get("enabled") else "Disabled")
        embed.add_field(name="Events so far", value=f"{so_far} / {min_t}–{max_t}")
        embed.add_field(name="Days left in week", value=str(remaining_days))
        embed.add_field(
            name="Today's spawn chance",
            value="Forced (minimum not met)" if force else f"{prob*100:.0f}%")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Minigames(bot))
