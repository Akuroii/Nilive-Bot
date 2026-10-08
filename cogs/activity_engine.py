import discord
from discord.ext import commands, tasks
import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH
from utils.timezone import get_cairo_daily_key


async def get_today_activity(guild_id: int, user_id: int) -> dict:
    today = get_cairo_daily_key()
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        word_count = len(message.content.split())
        today = get_cairo_daily_key()

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

        self.bot.dispatch("activity_message", message, word_count)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if not isinstance(thread.parent, discord.ForumChannel):
            return
        owner_id = thread.owner_id
        if not owner_id:
            return
        try:
            member = thread.guild.get_member(owner_id)
            if member and member.bot:
                return
        except Exception:
            pass

        today = get_cairo_daily_key()
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

    @tasks.loop(seconds=60)
    async def voice_tick_task(self):
        today = get_cairo_daily_key()
        for guild in self.bot.guilds:
            try:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None

                for channel in guild.voice_channels:
                    # Sensor/policy split: AFK channel / alone / deafened are
                    # still computed here, with the definitions this engine
                    # has always used, but they no longer end the member's
                    # tick for Leveling. They keep gating everything else
                    # (activity_stats.voice_minutes and the
                    # `activity_voice_tick` consumers: missions, mvp).
                    in_afk_channel = channel.id == afk_channel_id
                    real_members = [m for m in channel.members if not m.bot]
                    alone = len(real_members) < 2

                    for member in real_members:
                        try:
                            if not member.voice:
                                continue
                            flags = {
                                "self_mute": bool(member.voice.self_mute),
                                "mute":      bool(member.voice.mute),
                                "self_deaf": bool(member.voice.self_deaf),
                                "deaf":      bool(member.voice.deaf),
                                "channel_id": channel.id,
                            }
                            # Leveling's own event: it carries the three
                            # anti-farming facts so the guild's
                            # `voice_farming_guard` setting decides what they
                            # mean. Dispatched for EVERY real member in a
                            # voice channel; no other consumer listens to it.
                            xp_flags = dict(flags, alone=alone,
                                            afk=in_afk_channel)
                            if (in_afk_channel or alone
                                    or member.voice.self_deaf
                                    or member.voice.deaf):
                                self.bot.dispatch(
                                    "activity_voice_xp_tick",
                                    guild, member, xp_flags)
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

                            # channel_id rides along so channel-restricted
                            # missions can check WHERE the minute was spent,
                            # without changing the listener signature the
                            # other on_activity_voice_tick consumers
                            # (missions, mvp) already bind to.
                            self.bot.dispatch(
                                "activity_voice_tick", guild, member, flags)
                            self.bot.dispatch(
                                "activity_voice_xp_tick",
                                guild, member, xp_flags)
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
