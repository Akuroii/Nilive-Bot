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
                f"You already claimed your daily! Come back in {hours}h {minutes}m.", ephemeral=True)
            return
        amount = random.randint(100, 300)
        await update_balance(interaction.guild.id, interaction.user.id, amount)
        self.cooldowns[key] = now
        embed = discord.Embed(title="Daily Reward!", color=discord.Color.gold())
        embed.add_field(name="You received", value=f"🪙 {amount:,} coins")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="work", description="Work to earn some coins")
    async def work(self, interaction: discord.Interaction):
        import time
        key = (interaction.guild.id, interaction.user.id, "work")
        now = time.time()
        cooldown = 3600
        if key in self.cooldowns and now - self.cooldowns[key] < cooldown:
            remaining = int(cooldown - (now - self.cooldowns[key]))
            minutes = remaining // 60
            await interaction.response.send_message(
                f"You're tired! Rest for {minutes}m before working again.", ephemeral=True)
            return
        amount = random.randint(20, 80)
        await update_balance(interaction.guild.id, interaction.user.id, amount)
        self.cooldowns[key] = now
        messages = [
            f"You delivered packages and earned 🪙 {amount:,}!",
            f"You coded for hours and got paid 🪙 {amount:,}!",
            f"You walked dogs and made 🪙 {amount:,}!",
            f"You fixed computers and earned 🪙 {amount:,}!",
        ]
        await interaction.response.send_message(random.choice(messages))

    @app_commands.command(name="coinflip", description="Flip a coin and bet your coins")
    async def coinflip(self, interaction: discord.Interaction, amount: int, side: str):
        if side.lower() not in ["heads", "tails"]:
            await interaction.response.send_message("Choose heads or tails!", ephemeral=True)
            return
        bal = await get_balance(interaction.guild.id, interaction.user.id)
        if amount <= 0:
            await interaction.response.send_message("Bet must be more than 0!", ephemeral=True)
            return
        if bal < amount:
            await interaction.response.send_message(f"You only have 🪙 {bal:,}!", ephemeral=True)
            return
        result = random.choice(["heads", "tails"])
        won = result == side.lower()
        change = amount if won else -amount
        await update_balance(interaction.guild.id, interaction.user.id, change)
        new_bal = bal + change
        embed = discord.Embed(
            title="Coin Flip!",
            color=discord.Color.green() if won else discord.Color.red())
        embed.add_field(name="Result", value=result.capitalize())
        embed.add_field(name="Outcome", value=f"{'Won' if won else 'Lost'} 🪙 {amount:,}")
        embed.add_field(name="New Balance", value=f"🪙 {new_bal:,}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="slots", description="Play the slot machine")
    async def slots(self, interaction: discord.Interaction, amount: int):
        bal = await get_balance(interaction.guild.id, interaction.user.id)
        if amount <= 0:
            await interaction.response.send_message("Bet must be more than 0!", ephemeral=True)
            return
        if bal < amount:
            await interaction.response.send_message(f"You only have 🪙 {bal:,}!", ephemeral=True)
            return
        symbols = ["🍒", "🍋", "🍊", "⭐", "💎"]
        weights = [35, 30, 20, 10, 5]
        reels = random.choices(symbols, weights=weights, k=3)
        if reels[0] == reels[1] == reels[2]:
            if reels[0] == "💎":
                multiplier = 10
            elif reels[0] == "⭐":
                multiplier = 5
            else:
                multiplier = 3
            winnings = amount * multiplier
            await update_balance(interaction.guild.id, interaction.user.id, winnings - amount)
            result_text = f"JACKPOT! Won 🪙 {winnings:,}! (x{multiplier})"
            color = discord.Color.gold()
        elif reels[0] == reels[1] or reels[1] == reels[2]:
            winnings = int(amount * 0.5)
            await update_balance(interaction.guild.id, interaction.user.id, winnings - amount)
            result_text = f"Almost! Won back 🪙 {winnings:,}"
            color = discord.Color.blue()
        else:
            await update_balance(interaction.guild.id, interaction.user.id, -amount)
            result_text = f"Lost 🪙 {amount:,}"
            color = discord.Color.red()
        embed = discord.Embed(title="Slot Machine", color=color)
        embed.add_field(name="Reels", value=" ".join(reels), inline=False)
        embed.add_field(name="Result", value=result_text)
        embed.add_field(name="New Balance", value=f"🪙 {bal + (winnings - amount if 'winnings' in dir() else -amount):,}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="give", description="Give coins to another member")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if member.id == interaction.user.id:
            await interaction.response.send_message("You can't give coins to yourself!", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("Amount must be more than 0!", ephemeral=True)
            return
        bal = await get_balance(interaction.guild.id, interaction.user.id)
        if bal < amount:
            await interaction.response.send_message(f"You only have 🪙 {bal:,}!", ephemeral=True)
            return
        await update_balance(interaction.guild.id, interaction.user.id, -amount)
        await update_balance(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(
            f"{interaction.user.mention} gave 🪙 {amount:,} to {member.mention}!")

    @app_commands.command(name="richest", description="See the richest members")
    async def richest(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, balance FROM economy
                WHERE guild_id=?
                ORDER BY balance DESC LIMIT 10
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No one has any coins yet!", ephemeral=True)
            return
        embed = discord.Embed(title="Richest Members", color=discord.Color.gold())
        for i, (uid, bal) in enumerate(rows, 1):
            embed.add_field(name=f"#{i}", value=f"<@{uid}> — 🪙 {bal:,}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="addcoins", description="Add coins to a member (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def addcoins(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await update_balance(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(
            f"Added 🪙 {amount:,} to {member.mention}", ephemeral=True)

    @app_commands.command(name="removecoins", description="Remove coins from a member (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def removecoins(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await update_balance(interaction.guild.id, member.id, -amount)
        await interaction.response.send_message(
            f"Removed 🪙 {amount:,} from {member.mention}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Economy(bot))
