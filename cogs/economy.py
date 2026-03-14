import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import random
from database import DB_PATH

async def get_balance(guild_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def update_balance(guild_id: int, user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, balance)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                balance = balance + ?
        """, (guild_id, user_id, amount, amount))
        await db.commit()

class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cooldowns = {}

    @app_commands.command(name="balance", description="Check your coin balance")
    async def balance(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        bal = await get_balance(interaction.guild.id, member.id)
        embed = discord.Embed(title=f"{member.display_name}'s Balance", color=discord.Color.gold())
        embed.add_field(name="Coins", value=f"🪙 {bal:,}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="daily", description="Claim your daily coins")
    async def daily(self, interaction: discord.Interaction):
        import time
        key = (interaction.guild.id, interaction.user.id, "daily")
        now = time.time()
        cooldown = 86400
        if key in self.cooldowns and now - self.cooldowns[key] < cooldown:
            remaining = int(cooldown - (now - self.cooldowns[key]))
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.response.send_message(
