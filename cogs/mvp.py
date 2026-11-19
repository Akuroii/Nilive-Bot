import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timezone
from discord.ext import tasks
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso
from utils.timezone import get_cairo_daily_key
from utils.emoji import CHECK_EMOJI


async def get_mvp_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM mvp_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return {
        "enabled": 1, "cycle_hours": 6,
        "mvp_role_id": None, "announce_channel_id": None,
        "chat_word_weight": 1.0, "voice_minute_weight": 2.0,
        "winner_ranks": 1, "rank_rewards": "[]", "min_activity_words": 0,
    }


class MVP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mvp_cycle_task.start()

    def cog_unload(self):
        self.mvp_cycle_task.cancel()

    # ─── SCORE MESSAGES (Phase 3 / E1: now driven by the Activity
    # Engine's activity_message event instead of its own on_message
    # listener — word_count used to be computed here independently
    # of cogs/leveling.py's identical computation on the same
    # message; now it's computed once, centrally, in
    # cogs/activity_engine.py and passed to both.) ─────────────────
    @commands.Cog.listener()
    async def on_activity_message(self, message: discord.Message,
                                   word_count: int):
        if message.author.bot or not message.guild:
            return
        config = await get_mvp_config(message.guild.id)
        if not config.get("enabled", 1):
            return

        weight = float(config.get("chat_word_weight", 1.0))
        score  = word_count * weight
        today  = get_cairo_daily_key()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_scores
                    (guild_id, user_id, date, message_score, total_score)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    message_score = message_score + ?,
                    total_score   = total_score + ?
            """, (message.guild.id, message.author.id, today,
                  score, score, score, score))
            await db.commit()
        # Per-cycle score (the daily table above is left exactly as it was).
        from utils.mvp_engine import record_activity
        await record_activity(
            message.guild.id, message.author.id, message_score=score,
            cycle_hours=int(config.get("cycle_hours") or 6))

    # ─── SCORE VOICE (Phase 3 / E1: now driven by the Activity
    # Engine's activity_voice_tick event instead of MVP's own
    # join/leave session timer.
    #
    # Worth flagging: this also fixes a real gap MVP had on its own —
    # the old on_voice_state_update/_credit_voice_score session timer
    # had NO anti-farming guards at all (unlike leveling's poll loop),
    # so a member could sit alone, deafened, or in the AFK channel
    # and still rack up MVP voice score. The engine's tick already
    # excludes all of that before MVP ever sees the tick, so this is
    # a side-effect security/fairness improvement, not just a
    # refactor. The old voice_sessions-based join/leave bookkeeping
    # is gone entirely — a tick-based model doesn't need it. ────────
    @commands.Cog.listener()
    async def on_activity_voice_tick(self, guild: discord.Guild,
                                      member: discord.Member,
                                      flags: dict):
        try:
            config = await get_mvp_config(guild.id)
            if not config.get("enabled", 1):
                return

            weight = float(config.get("voice_minute_weight", 2.0))
            score  = 1 * weight  # one tick == one minute
            today  = get_cairo_daily_key()

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO mvp_scores
                        (guild_id, user_id, date,
                         voice_minutes, total_score)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                        voice_minutes = voice_minutes + 1,
                        total_score   = total_score   + ?
                """, (guild.id, member.id, today, score, score))
                await db.commit()
            from utils.mvp_engine import record_activity
            await record_activity(
                guild.id, member.id, voice_minutes=1, voice_score=score,
                cycle_hours=int(config.get("cycle_hours") or 6))
        except Exception as e:
            print(f"[MVP] voice tick error for member {member.id} "
                  f"in guild {guild.id}: {e}")

    # ─── MVP CYCLE TASK ──────────────────────────────────
    @tasks.loop(minutes=5)
    async def mvp_cycle_task(self):
        """Settles each guild's cycle once its configured duration has elapsed.

        Idempotent: utils.mvp_engine.settle_cycle commits payout + history +
        the move to the next cycle in one transaction, so a repeated or
        overlapping pass cannot pay twice, and downtime is not replayed.
        """
        from utils.mvp_engine import settle_cycle
        for guild in self.bot.guilds:
            try:
                await settle_cycle(self.bot, guild)
            except Exception as e:
                print(f"[MVP CYCLE] Error for guild {guild.id}: {e}")

    @mvp_cycle_task.before_loop
    async def before_mvp_task(self):
        await self.bot.wait_until_ready()

    # ─── SLASH COMMANDS ──────────────────────────────────
    @app_commands.command(name="mvp_scores",
                          description="View today's MVP scores")
    async def mvp_scores(self, interaction: discord.Interaction):
        today = get_cairo_daily_key()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, message_score, voice_minutes, total_score
                FROM mvp_scores
                WHERE guild_id = ? AND date = ?
                ORDER BY total_score DESC LIMIT 10
            """, (interaction.guild.id, today))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No scores today yet.", ephemeral=True)
            return

        embed = discord.Embed(title="🏆 Today's MVP Scores",
                              color=0xFFD700)
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, msg, voice, total) in enumerate(rows, 1):
            medal  = medals[i-1] if i <= 3 else f"#{i}"
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            embed.add_field(
                name=f"{medal} {name}",
                value=(f"💬 {msg:.1f} pts | "
                       f"🎙️ {voice:.1f} min | "
                       f"**{total:.1f} total**"),
                inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="mvp_setup",
                          description="Configure MVP system")
    @app_commands.checks.has_permissions(administrator=True)
    async def mvp_setup(self, interaction: discord.Interaction,
                        mvp_role: discord.Role = None,
                        announce_channel: discord.TextChannel = None,
                        cycle_hours: int = 6,
                        chat_weight: float = 1.0,
                        voice_weight: float = 2.0):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_config
                    (guild_id, mvp_role_id, announce_channel_id,
                     cycle_hours, chat_word_weight,
                     voice_minute_weight, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    mvp_role_id         = excluded.mvp_role_id,
                    announce_channel_id = excluded.announce_channel_id,
                    cycle_hours         = excluded.cycle_hours,
                    chat_word_weight    = excluded.chat_word_weight,
                    voice_minute_weight = excluded.voice_minute_weight
            """, (
                interaction.guild.id,
                mvp_role.id if mvp_role else None,
                announce_channel.id if announce_channel else None,
                cycle_hours,
                chat_weight,
                voice_weight,
            ))
            await db.commit()

        embed = discord.Embed(title="MVP System Configured",
                              color=0x57F287)
        embed.add_field(name="Cycle", value=f"Every {cycle_hours}h")
        embed.add_field(name="Chat Weight", value=f"{chat_weight}x")
        embed.add_field(name="Voice Weight", value=f"{voice_weight}x")
        if mvp_role:
            embed.add_field(name="MVP Role", value=mvp_role.mention)
        if announce_channel:
            embed.add_field(name="Announce", value=announce_channel.mention)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="mvp_force",
                          description="Force a new MVP cycle now (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mvp_force(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        from utils.mvp_engine import settle_cycle
        settled = await settle_cycle(self.bot, interaction.guild, force=True)
        if not settled or not settled["winners"]:
            await interaction.followup.send("No eligible winners this cycle.")
            return
        names = ", ".join(f"{w['rank']}- <@{w['user_id']}>" for w in settled["winners"])
        await interaction.followup.send(f"{CHECK_EMOJI} Forced MVP cycle: {names}")


async def setup(bot):
    await bot.add_cog(MVP(bot))
