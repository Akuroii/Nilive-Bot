import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from datetime import datetime, date
import time
from database import DB_PATH

class MVP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mvp_reset_loop.start()

    def cog_unload(self):
        self.mvp_reset_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        word_count = len(message.content.split())
        score = word_count * 1.5
        today = date.today().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_scores (guild_id, user_id, date, message_score, total_score)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    message_score = message_score + ?,
                    total_score = message_score + voice_minutes + ?
            """, (message.guild.id, message.author.id, today, score, score, score, score))
            await db.commit()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not member.guild:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            if before.channel is None and after.channel is not None:
                await db.execute("""
                    INSERT OR REPLACE INTO voice_sessions (guild_id, user_id, join_time)
                    VALUES (?, ?, ?)
                """, (member.guild.id, member.id, time.time()))
                await db.commit()
            elif before.channel is not None and after.channel is None:
                cursor = await db.execute("""
                    SELECT join_time FROM voice_sessions WHERE guild_id=? AND user_id=?
                """, (member.guild.id, member.id))
                row = await cursor.fetchone()
                if row:
                    minutes = (time.time() - row[0]) / 60
                    today = date.today().isoformat()
                    voice_score = minutes * 2
                    await db.execute("""
                        INSERT INTO mvp_scores (guild_id, user_id, date, voice_minutes, total_score)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                            voice_minutes = voice_minutes + ?,
                            total_score = message_score + voice_minutes + ?
                    """, (member.guild.id, member.id, today, voice_score, voice_score, voice_score, voice_score))
                    await db.execute("DELETE FROM voice_sessions WHERE guild_id=? AND user_id=?",
                                     (member.guild.id, member.id))
                    await db.commit()

    @tasks.loop(hours=24)
    async def mvp_reset_loop(self):
        await self.announce_and_reset()

    async def announce_and_reset(self):
        yesterday = date.today().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            guilds_cursor = await db.execute("SELECT DISTINCT guild_id FROM mvp_config")
            guilds = await guilds_cursor.fetchall()
            for (guild_id,) in guilds:
                cfg_cursor = await db.execute(
                    "SELECT mvp_role_id, announce_channel_id FROM mvp_config WHERE guild_id=?",
                    (guild_id,))
                cfg = await cfg_cursor.fetchone()
                if not cfg:
                    continue
                role_id, channel_id = cfg
                scores_cursor = await db.execute("""
                    SELECT user_id, total_score FROM mvp_scores
                    WHERE guild_id=? AND date=?
                    ORDER BY total_score DESC
                """, (guild_id, yesterday))
                scores = await scores_cursor.fetchall()
                if not scores:
                    continue
                top_score = scores[0][1]
                winners = [row[0] for row in scores if row[1] == top_score]
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue
                role = guild.get_role(role_id)
                if role:
                    for m in guild.members:
                        if role in m.roles:
                            await m.remove_roles(role)
                    for uid in winners:
                        member = guild.get_member(uid)
                        if member:
                            await member.add_roles(role)
                if channel_id:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        mentions = " ".join(f"<@{uid}>" for uid in winners)
                        embed = discord.Embed(
                            title="MVP of the Day",
                            description=f"{mentions}\nScore: {top_score:.1f} pts",
                            color=discord.Color.gold()
                        )
                        await channel.send(embed=embed)

    @app_commands.command(name="mvp_setup", description="Set the MVP role and announcement channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def mvp_setup(self, interaction: discord.Interaction,
                        role: discord.Role, channel: discord.TextChannel):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO mvp_config (guild_id, mvp_role_id, announce_channel_id)
                VALUES (?, ?, ?)
            """, (interaction.guild.id, role.id, channel.id))
            await db.commit()
        await interaction.response.send_message(
            f"MVP system set up! Role: {role.mention} | Channel: {channel.mention}", ephemeral=True)

    @app_commands.command(name="mvp_scores", description="See today's top active members")
    async def mvp_scores(self, interaction: discord.Interaction):
        today = date.today().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, total_score FROM mvp_scores
                WHERE guild_id=? AND date=?
                ORDER BY total_score DESC LIMIT 10
            """, (interaction.guild.id, today))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No activity recorded today yet!", ephemeral=True)
            return
        embed = discord.Embed(title="Today's MVP Leaderboard", color=discord.Color.gold())
        for i, (uid, score) in enumerate(rows, 1):
            embed.add_field(name=f"#{i}", value=f"<@{uid}> — {score:.1f} pts", inline=False)
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(MVP(bot))
