import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH
from utils.economy_safe import (
    safe_transfer, safe_credit, safe_admin_deduct, safe_convert,
    get_guild_exchange_rate, InsufficientBalance,
)
from utils.currency import get_currency_config


# Daily/Streak state lives entirely in utils/daily_engine.py. The
# /daily command was removed — /streak is now the single public entry
# point (cogs/wallet.py), and the Wallet 🔥 Streak button opens the
# same panel. There is no alias or duplicate claim path here.


async def get_balance(guild_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT balance FROM economy
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else 0


async def get_diamonds(guild_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT diamonds FROM economy
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else 0


async def add_balance(guild_id: int, user_id: int,
                      amount: int, reason: str = "Balance credit",
                      source: str = "system") -> int:
    # Routed through safe_credit (atomic upsert) instead of a raw
    # INSERT..ON CONFLICT here. Functionally the same for a single
    # credit, but keeps every write path going through one audited
    # helper instead of two separate implementations that could
    # drift apart. safe_credit writes a ledger entry itself, so
    # every path that calls add_balance() (daily, work, addcoins) is
    # automatically ledgered with no extra code here.
    await safe_credit(guild_id, user_id, amount, reason=reason, source=source)
    return await get_balance(guild_id, user_id)


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ─── BALANCE ────────────────────────────────────────
    @app_commands.command(name="balance",
                          description="Check your coin and diamond balance (opens your wallet)")
    async def balance(self, interaction: discord.Interaction,
                      member: discord.Member = None):
        # Wallet is the primary hub. For muscle memory, `/balance`
        # without arguments opens the caller's own wallet; viewing
        # another member's balance still shows a lightweight public
        # embed (only the numbers, no inventory/receipts controls,
        # since those are private to the owner).
        if member is None or member.id == interaction.user.id:
            from cogs.wallet import render_hub
            await render_hub(
                interaction, interaction.guild.id, interaction.user.id,
                first=True)
            return

        bal = await get_balance(interaction.guild.id, member.id)
        gems = await get_diamonds(interaction.guild.id, member.id)
        cur = await get_currency_config(interaction.guild.id)
        cc, cd = cur["coins"], cur["diamonds"]
        embed = discord.Embed(
            title=f"Balance — {member.display_name}",
            color=WALLET_ECHO_COLOR)
        embed.add_field(name=f"{cc['emoji']} {cc['name']}",
                        value=f"**{bal:,}**")
        embed.add_field(name=f"{cd['emoji']} {cd['name']}",
                        value=f"**{gems:,}**")
        embed.set_footer(text="Use /wallet to manage your own items and receipts")
        await interaction.response.send_message(embed=embed)

    # ─── GIVE ───────────────────────────────────────────
    @app_commands.command(name="give",
                          description="Give coins to another member")
    async def give(self, interaction: discord.Interaction,
                   member: discord.Member, amount: int):
        if member.id == interaction.user.id:
            cur = await get_currency_config(interaction.guild.id)
            await interaction.response.send_message(
                f"You cannot give {cur['coins']['name']} to yourself.",
                ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        # safe_transfer wraps check-and-deduct in a single
        # BEGIN IMMEDIATE transaction so two /give calls fired close
        # together cannot both pass the balance check and let a user
        # spend the same coins twice. safe_transfer logs both legs to
        # the ledger automatically, cross-referenced via
        # related_user_id.
        try:
            await safe_transfer(
                guild_id, interaction.user.id, member.id, amount,
                reason="Player-to-player transfer", source="give")
        except InsufficientBalance:
            bal = await get_balance(guild_id, interaction.user.id)
            cur = await get_currency_config(guild_id)
            await interaction.response.send_message(
                f"You only have {bal:,} {cur['coins']['name']}.",
                ephemeral=True)
            return

        cur = await get_currency_config(guild_id)
        cc = cur["coins"]
        await interaction.response.send_message(
            f"Gave **{amount:,}** {cc['name']} to {member.mention}.")

    # ─── CONVERT (Phase 5 / Economy v2) ─────────────────
    # Converts coins into diamonds at this guild's configured rate
    # (default 500:1, set via the Economy dashboard page or left at
    # default). Rounds down to the nearest full diamond — leftover
    # coins that do not divide evenly stay in the user's balance.
    @app_commands.command(name="convert",
                          description="Convert coins into diamonds")
    async def convert(self, interaction: discord.Interaction, coins: int):
        if coins <= 0:
            await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        rate = await get_guild_exchange_rate(guild_id)
        cur = await get_currency_config(guild_id)
        cc, cd = cur["coins"], cur["diamonds"]

        try:
            result = await safe_convert(
                guild_id, interaction.user.id, coins, rate,
                reason="User conversion", source="convert")
        except InsufficientBalance:
            bal = await get_balance(guild_id, interaction.user.id)
            await interaction.response.send_message(
                f"You only have {bal:,} {cc['name']}.", ephemeral=True)
            return
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        embed = discord.Embed(
            title="💱 Converted",
            description=(
                f"Spent **{result['coins_spent']:,}** {cc['name']} → "
                f"got **{result['diamonds_gained']:,}** {cd['emoji']} {cd['name']}\n"
                f"New balance: **{result['new_balance']:,}** {cc['name']} · "
                f"**{result['new_diamonds']:,}** {cd['emoji']}"),
            color=0x57F287)
        embed.set_footer(text=f"Rate: {rate:,} {cc['name']} = 1 {cd['emoji']}")
        await interaction.response.send_message(embed=embed)

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

        cur = await get_currency_config(interaction.guild.id)
        cc = cur["coins"]
        embed = discord.Embed(
            title=f"Richest Members",
            color=0xFFD700)
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, bal) in enumerate(rows, 1):
            medal = medals[i-1] if i <= 3 else f"#{i}"
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            embed.add_field(
                name=f"{medal} {name}",
                value=f"{bal:,} {cc['name']}",
                inline=False)
        await interaction.response.send_message(embed=embed)

    # ─── ADD COINS (admin) ──────────────────────────────
    @app_commands.command(name="addcoins",
                          description="Add coins to a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def addcoins(self, interaction: discord.Interaction,
                       member: discord.Member, amount: int):
        new_bal = await add_balance(
            interaction.guild.id, member.id, amount,
            reason=f"Admin grant by {interaction.user.display_name}",
            source="admin")
        cur = await get_currency_config(interaction.guild.id)
        cc = cur["coins"]
        await interaction.response.send_message(
            f"Added **{amount:,}** {cc['name']} to {member.mention}. "
            f"New balance: **{new_bal:,}**.",
            ephemeral=True)

    # ─── REMOVE COINS (admin) ───────────────────────────
    @app_commands.command(name="removecoins",
                          description="Remove coins from a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def removecoins(self, interaction: discord.Interaction,
                          member: discord.Member, amount: int):
        # Routed through safe_admin_deduct(), which is atomic
        # (BEGIN IMMEDIATE), clamps to zero like the old behavior, and
        # logs the actual amount removed to the ledger.
        new_bal = await safe_admin_deduct(
            interaction.guild.id, member.id, amount,
            reason=f"Admin removal by {interaction.user.display_name}",
            source="admin")
        cur = await get_currency_config(interaction.guild.id)
        cc = cur["coins"]
        await interaction.response.send_message(
            f"Removed **{amount:,}** {cc['name']} from {member.mention}. "
            f"New balance: **{new_bal:,}**.",
            ephemeral=True)

    # ─── ADD DIAMONDS (admin, Phase 5 / Economy v2) ─────
    @app_commands.command(name="adddiamonds",
                          description="Add diamonds to a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def adddiamonds(self, interaction: discord.Interaction,
                          member: discord.Member, amount: int):
        if amount <= 0:
            await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True)
            return
        await safe_credit(
            interaction.guild.id, member.id, amount, currency="diamonds",
            reason=f"Admin grant by {interaction.user.display_name}",
            source="admin")
        new_gems = await get_diamonds(interaction.guild.id, member.id)
        cur = await get_currency_config(interaction.guild.id)
        cd = cur["diamonds"]
        await interaction.response.send_message(
            f"Added **{amount:,}** {cd['emoji']} to {member.mention}. "
            f"New {cd['name'].lower()} balance: **{new_gems:,}**.",
            ephemeral=True)

    # ─── REMOVE DIAMONDS (admin, Phase 5 / Economy v2) ──
    @app_commands.command(name="removediamonds",
                          description="Remove diamonds from a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def removediamonds(self, interaction: discord.Interaction,
                             member: discord.Member, amount: int):
        if amount <= 0:
            await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True)
            return
        new_gems = await safe_admin_deduct(
            interaction.guild.id, member.id, amount, currency="diamonds",
            reason=f"Admin removal by {interaction.user.display_name}",
            source="admin")
        cur = await get_currency_config(interaction.guild.id)
        cd = cur["diamonds"]
        await interaction.response.send_message(
            f"Removed **{amount:,}** {cd['emoji']} from {member.mention}. "
            f"New {cd['name'].lower()} balance: **{new_gems:,}**.",
            ephemeral=True)


# Color used for the public `/balance @user` echo embed (private view
# is always the wallet hub). Kept local here so we don't import from
# cogs/wallet (which would create a circular import at load time if
# wallet later imports economy).
WALLET_ECHO_COLOR = 0x7c5cbf


async def setup(bot):
    await bot.add_cog(Economy(bot))
