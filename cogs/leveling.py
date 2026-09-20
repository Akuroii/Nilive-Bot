import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import time
import io
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.xp_calculator import (
    calculate_message_xp, calculate_voice_xp,
    xp_progress, get_leveling_config,
    is_role_blacklisted,
)
from utils.emoji import CHECK_EMOJI


# ─── Phase 5 / Leveling expansion — reset config helpers ────────────────
async def get_reset_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT enabled, period, last_reset FROM leveling_reset_config "
            "WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    if not row:
        return {"enabled": 0, "period": "weekly", "last_reset": None}
    return {"enabled": row[0], "period": row[1], "last_reset": row[2]}


def _period_hours(period: str) -> int:
    return 24 * 30 if period == "monthly" else 24 * 7


async def perform_leaderboard_reset(guild_id: int, period: str):
    """
    Snapshots the full current leaderboard into
    leveling_leaderboard_history (so a reset preserves the completed
    cycle instead of destroying it), then zeroes xp/level for every
    member of THIS guild only. Per-guild isolated throughout — every
    query is scoped to guild_id, matching every other reset/cleanup
    task in the project (cogs/mvp.py's cycle task, cogs/shop.py's
    temp_role_cleanup).

    CRITICAL FIX (dark-fixes pass): this used to run as four
    separate, unguarded statements — a SELECT, up to N history
    INSERTs, an UPDATE zeroing every member, and an upsert into
    leveling_reset_config — with no transaction wrapping the group.
    This function permanently destroys XP data (that's the entire
    point of a reset), so a crash or connection drop partway through
    the loop used to be able to leave the guild in a half-archived,
    half-zeroed state with no way to detect or recover from it: some
    members' history rows written, others not, and the zeroing UPDATE
    possibly applied to some but not all rows depending on exactly
    where the failure landed.

    The whole operation — snapshot, zero, and the reset_config
    bookkeeping row — now runs inside a single BEGIN IMMEDIATE
    transaction and either commits completely or rolls back
    completely, the same all-or-nothing guarantee
    utils/economy_safe.py already gives every coin/diamond mutation.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                WHERE guild_id = ? ORDER BY xp DESC
            """, (guild_id,))
            rows = await cursor.fetchall()

            for rank, (user_id, xp, level) in enumerate(rows, 1):
                await db.execute("""
                    INSERT INTO leveling_leaderboard_history
                        (guild_id, user_id, xp, level, rank, period, period_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (guild_id, user_id, xp, level, rank, period, now))

            await db.execute(
                "UPDATE levels SET xp = 0, level = 0 WHERE guild_id = ?",
                (guild_id,))

            await db.execute("""
                INSERT INTO leveling_reset_config (guild_id, enabled, period, last_reset)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    period = excluded.period,
                    last_reset = excluded.last_reset
            """, (guild_id, period, now))

            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return len(rows)


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._xp_cooldowns: dict[tuple, float] = {}
        self._spam_tracker: dict[tuple, list[float]] = {}
        # Phase 3 / E1: voice XP is now driven by
        # cogs/activity_engine.py's activity_voice_tick event (see
        # on_activity_voice_tick below) instead of this cog running
        # its own poll loop, so there's no task to start/cancel here
        # anymore.
        self.leaderboard_reset_task.start()

    def cog_unload(self):
        self.leaderboard_reset_task.cancel()

    # ─── SPAM DETECTION (P1 #12) ─────────────────────────
    # Frequency-based: N messages within X seconds = spam.
    # Penalizes XP instead of blocking messages (moderation.py
    # already handles actual mute/timeout enforcement).
    def _is_spamming(self, guild_id: int, user_id: int,
                      threshold: int, window_seconds: int) -> bool:
        key = (guild_id, user_id)
        now = time.time()
        times = self._spam_tracker.get(key, [])
        times = [t for t in times if now - t < window_seconds]
        times.append(now)
        self._spam_tracker[key] = times
        return len(times) >= threshold

    # ─── MESSAGE XP (Phase 3 / E1: now driven by the Activity
    # Engine's activity_message event instead of its own on_message
    # listener — word_count used to be computed here independently
    # of cogs/activity_engine.py's identical computation; now it's
    # computed once, centrally, and passed in. Everything else below
    # — the cooldown gate, spam-penalty ordering, multiplier lookup,
    # XP award, level-up announce — is unchanged from before.) ─────
    @commands.Cog.listener()
    async def on_activity_message(self, message: discord.Message,
                                   word_count: int):
        if message.author.bot or not message.guild:
            return

        config = await get_leveling_config(message.guild.id)
        if not config.get("enabled", 1):
            return

        guild_id = message.guild.id
        user_id  = message.author.id
        key      = (guild_id, user_id)
        now      = time.time()

        # P1 #12 FIX: spam detection must run on EVERY message,
        # BEFORE the XP-cooldown gate below — not after it.
        #
        # The previous ordering updated self._xp_cooldowns and
        # returned early whenever a message arrived inside the
        # cooldown window, which meant that message never reached
        # _is_spamming() at all. With the default settings
        # (xp_cooldown_seconds=30, spam_window_seconds=10,
        # spam_threshold=3), every message that could have counted
        # toward the spam threshold was filtered out by the cooldown
        # gate first — it was mathematically impossible to
        # accumulate 3 tracked messages inside a 10s window when
        # tracked messages were always >=30s apart. The anti-spam
        # feature existed in code but could never actually fire.
        #
        # Spam tracking now runs independently of the XP cooldown,
        # so rapid-fire messages get caught regardless of whether
        # they'd have earned XP anyway.
        if config.get("spam_detection_enabled", 1):
            threshold = int(config.get("spam_threshold", 3))
            window    = int(config.get("spam_window_seconds", 10))
            if self._is_spamming(guild_id, user_id, threshold, window):
                penalty = int(config.get("spam_xp_penalty", 10))
                if penalty > 0:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            INSERT INTO levels (guild_id, user_id, xp, level)
                            VALUES (?, ?, 0, 0)
                            ON CONFLICT(guild_id, user_id)
                            DO UPDATE SET xp = MAX(0, xp - ?)
                        """, (guild_id, user_id, penalty))
                        await db.commit()
                return

        cooldown = config.get("xp_cooldown_seconds", 30)
        last     = self._xp_cooldowns.get(key, 0)
        if now - last < cooldown:
            return
        self._xp_cooldowns[key] = now

        role_ids  = [r.id for r in message.author.roles]
        xp_to_add = await calculate_message_xp(
            guild_id, role_ids, word_count, user_id=user_id)

        if xp_to_add <= 0:
            return

        # Phase 3 / E2: XP granting + level-up detection + level
        # reward roles now go through the shared Reward Engine
        # instead of this cog doing its own raw INSERT/UPDATE and its
        # own inline level comparison — the exact same logic used to
        # be duplicated again below for voice XP. Announcing the
        # level-up in-channel stays here since that's leveling's own
        # UI concern, not something the engine should know about.
        from utils.reward_engine import give_reward
        result = await give_reward(
            self.bot, guild_id, user_id, "xp", amount=xp_to_add,
            reason="Message XP", source="leveling")

        if result.get("leveled_up"):
            await self._announce_levelup(
                message, result["new_level"], config)

    async def _announce_levelup(self, message: discord.Message,
                                 new_level: int, config: dict):
        if not config.get("levelup_announce", 1):
            return
        channel_id = config.get("levelup_channel_id")
        channel    = (message.guild.get_channel(int(channel_id))
                      if channel_id else message.channel)
        if not channel:
            return
        custom_msg = config.get("levelup_message")
        if custom_msg:
            text = (custom_msg
                    .replace("{user}", message.author.mention)
                    .replace("{level}", str(new_level))
                    .replace("{name}", message.author.display_name))
            await channel.send(text)
        else:
            embed = discord.Embed(
                description=(f"🎉 {message.author.mention} reached "
                             f"**Level {new_level}**."),
                color=0x7c5cbf)
            await channel.send(embed=embed)

    # ─── VOICE XP (Phase 3 / E1: now driven by the Activity
    # Engine's activity_voice_tick event instead of running its own
    # 60s poll loop over every guild/channel/member. The engine
    # already applies the raw disqualifiers (2+ real members present,
    # not AFK channel, not deafened) that used to live in this loop;
    # what's left here is leveling's own POLICY on top of a valid
    # tick — voice_xp_enabled, the require_unmuted choice, the XP
    # blacklist, and the actual XP math — exactly as before, just
    # invoked once per tick instead of leveling running its own
    # duplicate poll. ──────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_activity_voice_tick(self, guild: discord.Guild,
                                      member: discord.Member,
                                      flags: dict):
        try:
            config = await get_leveling_config(guild.id)
            if not config.get("voice_xp_enabled", 1):
                return

            require_unmuted = config.get("voice_require_unmuted", 1)
            if require_unmuted and (flags.get("self_mute") or flags.get("mute")):
                return

            # BUGFIX (dark-fixes pass #7): XP blacklist roles were
            # never checked here — only calculate_message_xp() (via
            # get_xp_multiplier) consulted leveling_blacklist_roles.
            # A member given a blacklist role to opt them out of the
            # leveling system entirely still silently earned XP from
            # every voice tick. Message XP and voice XP now share the
            # same blacklist gate; voice XP still does NOT apply
            # bonus-role multipliers, matching its existing (separate)
            # design.
            role_ids = [r.id for r in member.roles]
            if await is_role_blacklisted(guild.id, role_ids):
                return

            xp_per_min = config.get("voice_xp_per_minute", 3)
            xp_gain = calculate_voice_xp(1, xp_per_min)
            if xp_gain <= 0:
                return

            # Phase 3 / E2: same reward-engine handoff as message XP
            # above — no announcement on voice level-ups, matching the
            # pre-existing behavior (only chat XP announces).
            from utils.reward_engine import give_reward
            await give_reward(
                self.bot, guild.id, member.id, "xp", amount=xp_gain,
                reason="Voice XP", source="leveling")
        except Exception as e:
            print(f"[VOICE XP] Error for member {member.id} in "
                  f"guild {guild.id}: {e}")

    # ─── RANK COMMAND (Pillow Image Card) ───────────────
    @app_commands.command(name="rank",
                          description="View your rank card")
    async def rank(self, interaction: discord.Interaction,
                   member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.defer()

        # Cheap existence guard: distinguish "never earned XP" from "0 XP"
        # without pulling the full rank-card payload first.
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM levels WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id))
            if not await cursor.fetchone():
                await interaction.followup.send(
                    f"{member.mention} has no XP yet.")
                return

        # The real rank-card pipeline (same code path the fidelity
        # previews use): aggregated payload -> 1280x853 Pillow render.
        from utils.rank_card_data import get_rank_card_data
        from utils.rank_card_renderer import render_rank_card

        try:
            data = await get_rank_card_data(
                interaction.guild.id, member.id, member=member)
            buf = await render_rank_card(data)
            file = discord.File(buf, filename="rank.png")
            await interaction.followup.send(file=file)
        except Exception as e:
            print(f"[RANK CARD] render failed for {member.id}: {e}")
            # Fallback: a plain embed so the command still answers if the
            # renderer (fonts/assets/network) fails for any reason.
            from utils.prestige import tier_label
            data = await get_rank_card_data(
                interaction.guild.id, member.id, member=member)
            embed = discord.Embed(
                title=f"Rank — {member.display_name}", color=0x7c5cbf)
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Rank", value=f"#{data['rank']}")
            embed.add_field(name="Level", value=str(data["level"]))
            embed.add_field(name="Total XP", value=f"{data['xp_total']:,}")
            if data["effective_prestige"] > 0:
                embed.add_field(name="Prestige",
                                value=f"★{tier_label(data['effective_prestige'])}")
            embed.add_field(
                name=f"Progress ({data['xp_current']:,}/{data['xp_needed']:,} XP)",
                value="", inline=False)
            await interaction.followup.send(embed=embed)

    # ─── LEADERBOARD RESET TASK (Phase 5 / Leveling expansion) ─────
    # Mirrors cogs/mvp.py's mvp_cycle_task pattern: poll every 30
    # minutes, compare elapsed time against a stored last_reset per
    # guild, only act once the configured period has actually passed.
    # Each guild is isolated in its own try/except so one bad row
    # can't stop the loop from checking the rest — same defensive
    # pattern used throughout (shop temp_role_cleanup,
    # reactionroles expiry_check, moderation scheduled_unban_check).
    @tasks.loop(minutes=30)
    async def leaderboard_reset_task(self):
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT guild_id, period, last_reset
                FROM leveling_reset_config WHERE enabled = 1
            """)
            configs = await cursor.fetchall()

        for guild_id, period, last_reset in configs:
            try:
                if last_reset:
                    last_dt = datetime.fromisoformat(last_reset)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    elapsed_hours = (now - last_dt).total_seconds() / 3600
                    if elapsed_hours < _period_hours(period):
                        continue

                count = await perform_leaderboard_reset(guild_id, period)
                print(f"[LEVELING RESET] guild={guild_id} period={period} "
                      f"reset {count} members")

                guild = self.bot.get_guild(guild_id)
                config = await get_leveling_config(guild_id)
                channel_id = config.get("levelup_channel_id")
                if guild and channel_id:
                    channel = guild.get_channel(int(channel_id))
                    if channel:
                        embed = discord.Embed(
                            title="🔄 Leaderboard Reset",
                            description=(f"The {period} leaderboard has reset! "
                                         f"Last cycle's standings are archived — "
                                         f"everyone starts fresh."),
                            color=0x7c5cbf)
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[LEVELING RESET] Error for guild {guild_id}: {e}")

    @leaderboard_reset_task.before_loop
    async def before_reset_task(self):
        await self.bot.wait_until_ready()

    # ─── LEADERBOARD ────────────────────────────────────
    # Phase 5 / Prestige system: sort order is now
    # prestige DESC, xp DESC (STATUS.md locked decision) instead of
    # xp DESC alone, so a member who has prestiged always ranks above
    # a same-or-higher-XP member who hasn't, matching the intent of
    # prestige as a status tier above the raw XP race.
    @app_commands.command(name="leaderboard",
                          description="View the XP leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        # Finalized Prestige: sort on the clamped permanent tier so a legacy
        # levels.prestige above the max (old unbounded mechanic) ranks as V.
        from utils.prestige import MAX_PERMANENT_TIER
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level, prestige FROM levels
                WHERE guild_id = ?
                ORDER BY MIN(prestige, ?) DESC, xp DESC LIMIT 10
            """, (interaction.guild.id, MAX_PERMANENT_TIER))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No XP data yet.", ephemeral=True)
            return

        embed = discord.Embed(title="⭐ XP Leaderboard",
                              color=0x7c5cbf)
        medals = ["🥇", "🥈", "🥉"]
        # Finalized Prestige: display each member's EFFECTIVE tier (VI for
        # an active Booster). Sort order remains permanent prestige DESC,
        # xp DESC. Display only — never the source of multipliers.
        from utils.prestige import get_effective_prestige, is_booster, tier_label
        for i, (uid, xp, level, prestige) in enumerate(rows, 1):
            medal  = medals[i-1] if i <= 3 else f"#{i}"
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            eff    = await get_effective_prestige(
                interaction.guild.id, uid,
                member=member, bot=self.bot)
            if eff > 0:
                name = f"★{tier_label(eff)} {name}"
            embed.add_field(
                name=f"{medal} {name}",
                value=f"Level {level} • {xp:,} XP",
                inline=False)
        await interaction.response.send_message(embed=embed)

    # ─── SET XP (admin) ─────────────────────────────────
    @app_commands.command(name="setxp",
                          description="Set XP for a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setxp(self, interaction: discord.Interaction,
                    member: discord.Member, xp: int):
        # HARDENING (dark-fixes pass): clamp admin-supplied XP to
        # non-negative — nothing previously stopped a negative value
        # from being passed through to xp_progress()/the rank card,
        # which assume xp >= 0.
        xp = max(0, xp)
        new_level, _, _ = xp_progress(xp)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = ?, level = ?
            """, (interaction.guild.id, member.id, xp, new_level,
                  xp, new_level))
            await db.commit()
        await interaction.response.send_message(
            f"Set {member.mention}'s XP to {xp:,} (Level {new_level}).",
            ephemeral=True)

    # ─── RESET XP (admin, Phase 5 / Leveling expansion) ─
    # Fixes the flagged gap: dashboard/app.py's COMMAND_CATEGORIES has
    # listed a Leveling "resetxp" entry (Commands dashboard page) with
    # no matching command anywhere in the codebase. This is that
    # command — resets one member's xp/level back to 0 in this guild
    # only. Deliberately does NOT touch leveling_leaderboard_history
    # (that's only ever written by the scheduled/forced full-guild
    # reset below) and does NOT archive the member's XP anywhere —
    # a single-member reset is a moderation correction, not a
    # leaderboard cycle event.
    @app_commands.command(name="resetxp",
                          description="Reset a member's XP and level back to 0 (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetxp(self, interaction: discord.Interaction,
                      member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, 0, 0)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = 0, level = 0
            """, (interaction.guild.id, member.id))
            await db.commit()
        await interaction.response.send_message(
            f"Reset {member.mention}'s XP and level to 0.",
            ephemeral=True)

    # ─── RESET LEADERBOARD (admin, Phase 5 / Leveling expansion) ─
    # Manual/forced equivalent of leaderboard_reset_task — same
    # perform_leaderboard_reset() call, same archive-then-zero
    # behavior, just triggered on demand instead of waiting for the
    # configured weekly/monthly period to elapse. Mirrors
    # cogs/mvp.py's /mvp_force pattern.
    @app_commands.command(name="resetleaderboard",
                          description="Force an immediate leaderboard reset for this server (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetleaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reset_config = await get_reset_config(interaction.guild.id)
        period = reset_config.get("period") or "weekly"
        count = await perform_leaderboard_reset(interaction.guild.id, period)
        await interaction.followup.send(
            f"{CHECK_EMOJI} Leaderboard reset — {count} member(s) archived "
            f"and zeroed.")

    # ─── PRESTIGE STATE / READ-ONLY VIEW ─────────────────
    # The old XP/level-gated "/prestige reset" command has been retired
    # (the finalized Prestige system is Shop-purchased and never resets
    # XP/Level). This status command shows the member's current
    # Prestige: their permanent tier and their effective tier (which is
    # VI only with a valid activation and an active Discord boost).
    # It grants nothing; verified expiry may remove a stale VI activation.
    @app_commands.command(name="prestige",
                          description="View your Prestige state")
    async def prestige(self, interaction: discord.Interaction):
        from utils.prestige import (
            get_permanent_prestige, get_effective_prestige,
            is_booster, tier_label,
        )
        await interaction.response.defer()
        permanent = await get_permanent_prestige(
            interaction.guild.id, interaction.user.id)
        booster = is_booster(interaction.user)
        effective = await get_effective_prestige(
            interaction.guild.id, interaction.user.id, member=interaction.user)

        embed = discord.Embed(
            title="⭐ Prestige",
            color=0xFFD700)
        embed.add_field(
            name="Permanent Prestige",
            value=(f"**{tier_label(permanent)}**"
                   if permanent else "None"),
            inline=False)
        embed.add_field(
            name="Effective Prestige",
            value=f"**{tier_label(effective)}**",
            inline=False)
        if booster and effective == 6:
            explanation = (
                "Your free Prestige VI activation is active while you boost. "
                "When boosting ends, VI activation is removed and you return to "
                f"permanent Prestige {tier_label(permanent)}. After re-boosting, "
                "activate again for Free; VI does not return automatically.")
        elif booster:
            explanation = (
                "You're eligible for free Prestige VI activation in the Shop. "
                "Booster status alone does not activate VI. After re-boosting, "
                "activate again for Free; old VI activations do not return automatically.")
        else:
            explanation = (
                "Only active Discord Boosters can activate Prestige VI, for Free. "
                "When a boost ends, VI has ended and its activation is removed. "
                f"Your effective Prestige is your permanent Prestige {tier_label(permanent)}. "
                "If you boost again, activate VI again for Free; it does not return automatically.")
        embed.add_field(name="Booster Prestige VI", value=explanation, inline=False)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
