import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import random
from datetime import datetime, timedelta
from database import DB_PATH


_daily_cooldowns:  dict[int, datetime] = {}
_work_cooldowns:   dict[int, datetime] = {}


async def get_balance(guild_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT balance FROM economy
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else 0


async def add_balance(guild_id: int, user_id: int,
                      amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, balance)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET balance = balance + ?
        """, (guild_id, user_id, amount, amount))
        await db.commit()
        cursor = await db.execute("""
            SELECT balance FROM economy
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else 0


async def get_currency_name(guild_id: int) -> str:
    """
    Reads currency name from guild_settings.
    Falls back to 'Coins' if not configured.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT currency_name FROM guild_settings
            WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else "Coins"


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ─── BALANCE ────────────────────────────────────────
    @app_commands.command(name="balance",
                          description="Check your coin balance")
    async def balance(self, interaction: discord.Interaction,
                      member: discord.Member = None):
        member   = member or interaction.user
        bal      = await get_balance(
            interaction.guild.id, member.id)
        currency = await get_currency_name(interaction.guild.id)
        embed    = discord.Embed(
            title=f"💰 {member.display_name}'s Balance",
            description=f"**{bal:,}** {currency}",
            color=0xFFD700)
        await interaction.response.send_message(embed=embed)

    # ─── DAILY ──────────────────────────────────────────
    @app_commands.command(name="daily",
                          description="Claim your daily coins")
    async def daily(self, interaction: discord.Interaction):
        now      = datetime.utcnow()
        user_id  = interaction.user.id
        cooldown = _daily_cooldowns.get(user_id)
        if cooldown and now < cooldown:
            remaining = cooldown - now
            hours     = int(remaining.total_seconds() // 3600)
            minutes   = int((remaining.total_seconds() % 3600) // 60)
            await interaction.response.send_message(
                f"Daily already claimed! Try again in "
                f"**{hours}h {minutes}m**.",
                ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT value FROM bot_settings WHERE key = 'daily_min'
            """)
            row      = await cursor.fetchone()
            daily_min = int(row[0]) if row else 100
            cursor    = await db.execute("""
                SELECT value FROM bot_settings WHERE key = 'daily_max'
            """)
            row       = await cursor.fetchone()
            daily_max = int(row[0]) if row else 300

        amount   = random.randint(daily_min, daily_max)
        new_bal  = await add_balance(interaction.guild.id, user_id, amount)
        currency = await get_currency_name(interaction.guild.id)
        _daily_cooldowns[user_id] = now + timedelta(hours=24)

        embed = discord.Embed(
            title="🎁 Daily Reward!",
            description=(f"You received **{amount:,}** {currency}!\n"
                         f"Balance: **{new_bal:,}** {currency}"),
            color=0x57F287)
        await interaction.response.send_message(embed=embed)

    # ─── WORK ───────────────────────────────────────────
    @app_commands.command(name="work",
                          description="Work to earn coins")
    async def work(self, interaction: discord.Interaction):
        now      = datetime.utcnow()
        user_id  = interaction.user.id
        cooldown = _work_cooldowns.get(user_id)
        if cooldown and now < cooldown:
            remaining = cooldown - now
            minutes   = int(remaining.total_seconds() // 60)
            await interaction.response.send_message(
                f"You're tired! Rest for **{minutes}m** more.",
                ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor   = await db.execute(
                "SELECT value FROM bot_settings WHERE key = 'work_min'")
            row      = await cursor.fetchone()
            work_min = int(row[0]) if row else 20
            cursor   = await db.execute(
                "SELECT value FROM bot_settings WHERE key = 'work_max'")
            row      = await cursor.fetchone()
            work_max = int(row[0]) if row else 80

        jobs = [
            "wrote some code", "delivered packages",
            "fixed a server", "designed a logo",
            "taught a class", "drove a taxi",
            "cooked meals", "streamed games",
            "built furniture", "walked dogs",
        ]
        amount   = random.randint(work_min, work_max)
        new_bal  = await add_balance(
            interaction.guild.id, user_id, amount)
        currency = await get_currency_name(interaction.guild.id)
        _work_cooldowns[user_id] = now + timedelta(hours=1)

        embed = discord.Embed(
            title="💼 Work Complete!",
            description=(f"You {random.choice(jobs)} and earned "
                         f"**{amount:,}** {currency}!\n"
                         f"Balance: **{new_bal:,}** {currency}"),
            color=0x57F287)
        await interaction.response.send_message(embed=embed)

    # ─── GIVE ───────────────────────────────────────────
    @app_commands.command(name="give",
                          description="Give coins to another member")
    async def give(self, interaction: discord.Interaction,
                   member: discord.Member, amount: int):
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't give coins to yourself!",
                ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id  = interaction.user.id
        bal      = await get_balance(guild_id, user_id)
        if bal < amount:
            await interaction.response.send_message(
                f"You only have {bal:,} coins!", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE economy SET balance = balance - ?
                WHERE guild_id = ? AND user_id = ?
            """, (amount, guild_id, user_id))
            await db.execute("""
                INSERT INTO economy (guild_id, user_id, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET balance = balance + ?
            """, (guild_id, member.id, amount, amount))
            await db.commit()

        currency = await get_currency_name(guild_id)
        await interaction.response.send_message(
            f"Gave **{amount:,}** {currency} to {member.mention}!")

    # ─── RICHEST ────────────────────────────────────────
    @app_commands.command(name="richest",
                          description="View the richest members")
    async def richest(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, balance FROM economy
                WHERE guild_id = ?
                ORDER BY balance DESC LIMIT 10
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No economy data yet.", ephemeral=True)
            return

        currency = await get_currency_name(interaction.guild.id)
        embed    = discord.Embed(
            title=f"💰 Richest Members",
            color=0xFFD700)
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, bal) in enumerate(rows, 1):
            medal  = medals[i-1] if i <= 3 else f"#{i}"
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            embed.add_field(
                name=f"{medal} {name}",
                value=f"{bal:,} {currency}",
                inline=False)
        await interaction.response.send_message(embed=embed)

    # ─── ADD COINS (admin) ──────────────────────────────
    @app_commands.command(name="addcoins",
                          description="Add coins to a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def addcoins(self, interaction: discord.Interaction,
                       member: discord.Member, amount: int):
        new_bal  = await add_balance(
            interaction.guild.id, member.id, amount)
        currency = await get_currency_name(interaction.guild.id)
        await interaction.response.send_message(
            f"Added **{amount:,}** {currency} to {member.mention}. "
            f"New balance: **{new_bal:,}**.",
            ephemeral=True)

    # ─── REMOVE COINS (admin) ───────────────────────────
    @app_commands.command(name="removecoins",
                          description="Remove coins from a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def removecoins(self, interaction: discord.Interaction,
                          member: discord.Member, amount: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE economy
                SET balance = MAX(0, balance - ?)
                WHERE guild_id = ? AND user_id = ?
            """, (amount, interaction.guild.id, member.id))
            await db.commit()
            cursor = await db.execute("""
                SELECT balance FROM economy
                WHERE guild_id = ? AND user_id = ?
            """, (interaction.guild.id, member.id))
            row     = await cursor.fetchone()
            new_bal = row[0] if row else 0
        currency = await get_currency_name(interaction.guild.id)
        await interaction.response.send_message(
            f"Removed **{amount:,}** {currency} from {member.mention}. "
            f"New balance: **{new_bal:,}**.",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Economy(bot))
