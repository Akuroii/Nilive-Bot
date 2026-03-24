import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
from datetime import datetime, timezone
from discord.ext import tasks
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso


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
    }


class MVP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mvp_cycle_task.start()

    def cog_unload(self):
        self.mvp_cycle_task.cancel()

    # ─── SCORE MESSAGES ─────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        config = await get_mvp_config(message.guild.id)
        if not config.get("enabled", 1):
            return

        word_count = len(message.content.split())
        weight     = float(config.get("chat_word_weight", 1.0))
        score      = word_count * weight
        today      = datetime.now(timezone.utc).date().isoformat()

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

    # ─── SCORE VOICE ────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after: discord.VoiceState):
        if member.bot:
            return
        config = await get_mvp_config(member.guild.id)
        if not config.get("enabled", 1):
            return

        # Joined voice
        if not before.channel and after.channel:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO voice_sessions (guild_id, user_id, join_time)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, user_id)
                    DO UPDATE SET join_time = ?
                """, (member.guild.id, member.id,
                      datetime.now(timezone.utc).timestamp(),
                      datetime.now(timezone.utc).timestamp()))
                await db.commit()

        # Left voice
        elif before.channel and not after.channel:
            await self._credit_voice_score(member, config)

    async def _credit_voice_score(self, member: discord.Member,
                                   config: dict):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT join_time FROM voice_sessions
                WHERE guild_id = ? AND user_id = ?
            """, (member.guild.id, member.id))
            row = await cursor.fetchone()
            if not row:
                return
            join_time = row[0]
            await db.execute("""
                DELETE FROM voice_sessions
                WHERE guild_id = ? AND user_id = ?
            """, (member.guild.id, member.id))
            await db.commit()

        now     = datetime.now(timezone.utc).timestamp()
        minutes = (now - join_time) / 60
        if minutes < 0.5:
            return

        weight = float(config.get("voice_minute_weight", 2.0))
        score  = minutes * weight
        today  = datetime.now(timezone.utc).date().isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_scores
                    (guild_id, user_id, date,
                     voice_minutes, total_score)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    voice_minutes = voice_minutes + ?,
                    total_score   = total_score   + ?
            """, (member.guild.id, member.id, today,
                  minutes, score, minutes, score))
            await db.commit()

    # ─── MVP CYCLE TASK ──────────────────────────────────
    @tasks.loop(minutes=30)
    async def mvp_cycle_task(self):
        """
        Checks every 30 min if a cycle has elapsed.
        Cycle length is configurable per guild (default 6hrs).
        """
        for guild in self.bot.guilds:
            try:
                config = await get_mvp_config(guild.id)
                if not config.get("enabled", 1):
                    continue

                cycle_hours = int(config.get("cycle_hours", 6))
                now         = datetime.now(timezone.utc)
                today       = now.date().isoformat()

                # Find top scorer today
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute("""
                        SELECT user_id, total_score FROM mvp_scores
                        WHERE guild_id = ? AND date = ?
                        ORDER BY total_score DESC LIMIT 1
                    """, (guild.id, today))
                    top = await cursor.fetchone()

                    # Check last MVP history to see if cycle elapsed
                    hist_cursor = await db.execute("""
                        SELECT cycle_end FROM mvp_history
                        WHERE guild_id = ?
                        ORDER BY created_at DESC LIMIT 1
                    """, (guild.id,))
                    last = await hist_cursor.fetchone()

                if last:
                    last_end = datetime.fromisoformat(last[0])
                    if last_end.tzinfo is None:
                        last_end = last_end.replace(tzinfo=timezone.utc)
                    elapsed = (now - last_end).total_seconds() / 3600
                    if elapsed < cycle_hours:
                        continue

                if not top:
                    continue

                mvp_user_id, mvp_score = top
                mvp_member = guild.get_member(mvp_user_id)
                if not mvp_member:
                    continue

                snap = snapshot_user(mvp_member)
                cycle_start = now.replace(
                    hour=0, minute=0, second=0, microsecond=0)

                # Save to history
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT INTO mvp_history
                            (guild_id, user_id, user_display_name,
                             cycle_start, cycle_end, score)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (guild.id, mvp_user_id,
                          snap["display_name"],
                          cycle_start.isoformat(),
                          now.isoformat(),
                          int(mvp_score)))
                    await db.commit()

                # Assign MVP role
                mvp_role_id = config.get("mvp_role_id")
                if mvp_role_id:
                    mvp_role = guild.get_role(int(mvp_role_id))
                    if mvp_role:
                        # Remove from all current holders
                        for m in guild.members:
                            if mvp_role in m.roles and m.id != mvp_user_id:
                                try:
                                    await m.remove_roles(
                                        mvp_role,
                                        reason="MVP cycle reset")
                                except Exception:
                                    pass
                        # Give to new MVP
                        try:
                            await mvp_member.add_roles(
                                mvp_role, reason="MVP of the cycle")
                        except Exception:
                            pass

                # Announce
                channel_id = config.get("announce_channel_id")
                if channel_id:
                    channel = guild.get_channel(int(channel_id))
                    if channel:
                        embed = discord.Embed(
                            title="🏆 New MVP!",
                            description=(
                                f"{mvp_member.mention} is the MVP "
                                f"of this cycle with "
                                f"**{int(mvp_score):,}** points!"),
                            color=0xFFD700)
                        if mvp_member.display_avatar:
                            embed.set_thumbnail(
                                url=mvp_member.display_avatar.url)
                        embed.set_footer(
                            text=f"Next cycle in {cycle_hours} hours")
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            pass

            except Exception as e:
                print(f"[MVP CYCLE] Error for guild {guild.id}: {e}")

    @mvp_cycle_task.before_loop
    async def before_mvp_task(self):
        await self.bot.wait_until_ready()

    # ─── SLASH COMMANDS ──────────────────────────────────
    @app_commands.command(name="mvp_scores",
                          description="View today's MVP scores")
    async def mvp_scores(self, interaction: discord.Interaction):
        today = datetime.now(timezone.utc).date().isoformat()
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
        config = await get_mvp_config(interaction.guild.id)
        today  = datetime.now(timezone.utc).date().isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, total_score FROM mvp_scores
                WHERE guild_id = ? AND date = ?
                ORDER BY total_score DESC LIMIT 1
            """, (interaction.guild.id, today))
            top = await cursor.fetchone()

        if not top:
            await interaction.followup.send("No scores yet today.")
            return

        uid, score  = top
        member      = interaction.guild.get_member(uid)
        if not member:
            await interaction.followup.send("MVP user not found.")
            return

        snap = snapshot_user(member)
        now  = datetime.now(timezone.utc)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_history
                    (guild_id, user_id, user_display_name,
                     cycle_start, cycle_end, score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (interaction.guild.id, uid,
                  snap["display_name"],
                  now.replace(hour=0, minute=0,
                              second=0, microsecond=0).isoformat(),
                  now.isoformat(), int(score)))
            await db.commit()

        await interaction.followup.send(
            f"✅ Forced MVP: {member.mention} with {int(score):,} pts")


async def setup(bot):
    await bot.add_cog(MVP(bot))
