import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import math
from database import DB_PATH

def xp_for_level(level: int) -> int:
    return math.floor(100 * (level ** 1.5))

def calculate_level(xp: int) -> int:
    level = 0
    while xp >= xp_for_level(level + 1):
        xp -= xp_for_level(level + 1)
        level += 1
    return level

class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        word_count = len(message.content.split())
        if word_count < 1:
            return
        xp_gain = min(word_count * 2, 50)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                (message.guild.id, message.author.id))
            row = await cursor.fetchone()
            if row:
                current_xp, current_level = row
            else:
                current_xp, current_level = 0, 0
            new_xp = current_xp + xp_gain
            new_level = calculate_level(new_xp)
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    xp = ?, level = ?
            """, (message.guild.id, message.author.id, new_xp, new_level, new_xp, new_level))
            await db.commit()
        if new_level > current_level:
            embed = discord.Embed(
                title="Level Up!",
                description=f"{message.author.mention} reached level **{new_level}**!",
                color=discord.Color.purple()
            )
            await message.channel.send(embed=embed)

    @app_commands.command(name="rank", description="Check your rank and XP")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id))
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(f"{member.mention} has no XP yet!", ephemeral=True)
                return
            xp, level = row
            rank_cursor = await db.execute("""
                SELECT COUNT(*) FROM levels
                WHERE guild_id=? AND xp > ?
            """, (interaction.guild.id, xp))
            rank = (await rank_cursor.fetchone())[0] + 1
        next_level_xp = xp_for_level(level + 1)
        current_level_xp = xp_for_level(level)
        progress = xp - current_level_xp
        embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.purple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Rank", value=f"#{rank}")
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="XP", value=f"{progress} / {next_level_xp - current_level_xp}")
        bar_filled = int((progress / max(next_level_xp - current_level_xp, 1)) * 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        embed.add_field(name="Progress", value=f"`{bar}`", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="See the top members by XP")
    async def leaderboard(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                WHERE guild_id=?
                ORDER BY xp DESC LIMIT 10
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No one has any XP yet!", ephemeral=True)
            return
        embed = discord.Embed(title="XP Leaderboard", color=discord.Color.purple())
        for i, (uid, xp, level) in enumerate(rows, 1):
            embed.add_field(
                name=f"#{i}",
                value=f"<@{uid}> — Level {level} ({xp} XP)",
                inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="setxp", description="Set XP for a member (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setxp(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        new_level = calculate_level(amount)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?
            """, (interaction.guild.id, member.id, amount, new_level, amount, new_level))
            await db.commit()
        await interaction.response.send_message(
            f"Set {member.mention}'s XP to {amount} (Level {new_level})", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Leveling(bot))
