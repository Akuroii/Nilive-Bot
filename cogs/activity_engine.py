import discord
from discord.ext import commands, tasks
import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH

# ══════════════════════════════════════════════════════════════
# ACTIVITY TRACKING ENGINE (Phase 3, E1)
#
# WHY THIS EXISTS
# Before this engine, cogs/leveling.py and cogs/mvp.py each had
# their own on_message listener independently computing the exact
# same word_count from message.content.split(), and — worse — two
# COMPLETELY DIFFERENT voice-tracking mechanisms running in parallel:
#   - leveling.py: a 60s poll loop over every guild/channel/member,
#     with real anti-farming guards (2+ real members in channel,
#     deafened always excluded, muted excluded if configured).
#   - mvp.py: a join/leave session timer (on_voice_state_update +
#     the voice_sessions table) with NO guards at all — a member
#     could sit alone, deafened, or in the AFK channel and still
#     accumulate MVP voice score.
#
# This engine is now the ONE place that listens to raw Discord
# message/voice/thread events. It does two things with each signal:
#   1. Persists it to activity_stats (a day-bucketed table any
#      future feature — missions, tag quest, anti-spam — can query
#      without adding yet another listener).
#   2. Re-broadcasts it as a custom bot event (activity_message,
#      activity_voice_tick, activity_forum_post) that feature cogs
#      subscribe to instead of listening to Discord's raw events
#      directly. Each feature keeps its OWN policy on top — its own
#      cooldowns, spam handling, weights, and (for voice) whether it
#      requires unmuted — the engine only decides what counts as a
#      genuine raw signal in the first place.
#
# cogs/leveling.py and cogs/mvp.py have both been migrated onto
# these events; see the "Phase 3 / E1" comments in each.
# ══════════════════════════════════════════════════════════════


async def get_today_activity(guild_id: int, user_id: int) -> dict:
    """Convenience read used by other cogs/dashboard pages that just
    want today's raw activity numbers for a member."""
    today = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT messages_count, words_count, voice_minutes, forum_posts_count
            FROM activity_stats
            WHERE guild_id = ? AND user_id = ? AND date = ?
        """, (guild_id, user_id, today))
        row = await cursor.fetchone()
    if not row:
        return {"messages_count": 0, "words_count": 0,
                "voice_minutes": 0.0, "forum_posts_count": 0}
    return {
        "messages_count": row[0], "words_count": row[1],
        "voice_minutes": row[2], "forum_posts_count": row[3],
    }


class ActivityEngine(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_tick_task.start()

    def cog_unload(self):
        self.voice_tick_task.cancel()

    # ─── MESSAGE ACTIVITY ────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        word_count = len(message.content.split())
        today = datetime.now(timezone.utc).date().isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO activity_stats
                    (guild_id, user_id, date, messages_count, words_count)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    messages_count = messages_count + 1,
                    words_count    = words_count + ?
            """, (message.guild.id, message.author.id, today,
                  word_count, word_count))
            await db.commit()

        # Raw signal only — no cooldowns, no spam policy, no weights.
        # Consumers (leveling, mvp, future missions/anti-spam) decide
        # what to do with it via their own on_activity_message listener.
        self.bot.dispatch("activity_message", message, word_count)

    # ─── FORUM POST ACTIVITY ─────────────────────────────
    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if not isinstance(thread.parent, discord.ForumChannel):
            return
        owner_id = thread.owner_id
        if not owner_id:
            return
        # Best-effort bot check — owner may not be cached as a Member;
        # skip silently if we can't resolve them rather than raising.
        try:
            member = thread.guild.get_member(owner_id)
            if member and member.bot:
                return
        except Exception:
            pass

        today = datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO activity_stats
                    (guild_id, user_id, date, forum_posts_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    forum_posts_count = forum_posts_count + 1
            """, (thread.guild.id, owner_id, today))
            await db.commit()

        self.bot.dispatch("activity_forum_post", thread, owner_id)

    # ─── VOICE ACTIVITY (ported from leveling.py's voice_xp_task) ──
    @tasks.loop(seconds=60)
    async def voice_tick_task(self):
        """
        Single shared voice-presence poll, replacing the two separate
        mechanisms that used to exist (leveling's poll loop, mvp's
        join/leave session timer). Runs every 60s across every guild;
        anything counted here is worth 1 minute of voice activity.

        Raw disqualifiers applied here (apply to EVERY consumer,
        not just leveling): fewer than 2 real (non-bot) members in
        the channel, in the AFK channel, or the member is deafened
        (self_deaf/deaf) — a deafened member isn't meaningfully
        "present" for any feature's purposes.

        Muted (self_mute/mute) is deliberately NOT filtered here —
        that's a policy choice ("does this feature require unmuted
        to count?") that leveling already made configurable per-guild
        and MVP might want to decide differently, so both mute flags
        are passed through in the dispatched event and each consumer
        applies its own rule, exactly like leveling did before.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        for guild in self.bot.guilds:
            try:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None

                for channel in guild.voice_channels:
                    if channel.id == afk_channel_id:
                        continue

                    real_members = [m for m in channel.members if not m.bot]
                    if len(real_members) < 2:
                        continue

                    for member in real_members:
                        try:
                            if not member.voice:
                                continue
                            if member.voice.self_deaf or member.voice.deaf:
                                continue

                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute("""
                                    INSERT INTO activity_stats
                                        (guild_id, user_id, date, voice_minutes)
                                    VALUES (?, ?, ?, 1)
                                    ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                                        voice_minutes = voice_minutes + 1
                                """, (guild.id, member.id, today))
                                await db.commit()

                            flags = {
                                "self_mute": bool(member.voice.self_mute),
                                "mute":      bool(member.voice.mute),
                                "self_deaf": bool(member.voice.self_deaf),
                                "deaf":      bool(member.voice.deaf),
                            }
                            self.bot.dispatch(
                                "activity_voice_tick", guild, member, flags)
                        except Exception as e:
                            print(f"[ACTIVITY] voice tick error for "
                                  f"member {member.id} in guild "
                                  f"{guild.id}: {e}")
            except Exception as e:
                print(f"[ACTIVITY] voice tick error for guild {guild.id}: {e}")

    @voice_tick_task.before_loop
    async def before_voice_tick(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ActivityEngine(bot))
