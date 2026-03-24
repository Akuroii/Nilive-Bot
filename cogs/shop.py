import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.permissions import check_bot_role_position
from utils.formatters import snapshot_user, now_iso


async def get_currency_name(guild_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT currency_name FROM guild_settings
            WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else "Coins"


class BuyView(discord.ui.View):
    def __init__(self, item_id: int, item_name: str, price: int):
        super().__init__(timeout=60)
        self.item_id   = item_id
        self.item_name = item_name
        self.price     = price

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.green,
                       emoji="🛒")
    async def buy(self, interaction: discord.Interaction,
                  button: discord.ui.Button):
        await process_purchase(interaction, self.item_id)


async def process_purchase(interaction: discord.Interaction,
                            item_id: int):
    guild_id = interaction.guild.id
    user_id  = interaction.user.id

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, name, price, type, role_id,
                   duration_hours, required_level,
                   required_role_id, enabled,
                   max_stock, current_stock
            FROM shop_items
            WHERE id = ? AND guild_id = ? AND enabled = 1
        """, (item_id, guild_id))
        item = await cursor.fetchone()

    if not item:
        await interaction.response.send_message(
            "Item not found or disabled.", ephemeral=True)
        return

    (iid, name, price, itype, role_id, duration_hours,
     req_level, req_role_id, enabled, max_stock, curr_stock) = item

    # Stock check
    if max_stock and curr_stock is not None and curr_stock <= 0:
        await interaction.response.send_message(
            "This item is out of stock!", ephemeral=True)
        return

    # Balance check
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT balance FROM economy
            WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    balance = row[0] if row else 0

    if balance < price:
        currency = await get_currency_name(guild_id)
        await interaction.response.send_message(
            f"You need {price:,} {currency} but only have {balance:,}.",
            ephemeral=True)
        return

    # Level check
    if req_level and req_level > 0:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT level FROM levels
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            row = await cursor.fetchone()
        user_level = row[0] if row else 0
        if user_level < req_level:
            await interaction.response.send_message(
                f"You need Level {req_level} to buy this.",
                ephemeral=True)
            return

    # Required role check
    if req_role_id:
        req_role = interaction.guild.get_role(int(req_role_id))
        if req_role and req_role not in interaction.user.roles:
            await interaction.response.send_message(
                f"You need {req_role.mention} to buy this.",
                ephemeral=True)
            return

    # Deduct coins
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE economy SET balance = balance - ?
            WHERE guild_id = ? AND user_id = ?
        """, (price, guild_id, user_id))

        # Reduce stock
        if max_stock:
            await db.execute("""
                UPDATE shop_items
                SET current_stock = current_stock - 1
                WHERE id = ?
            """, (iid,))

        # Record purchase
        snap       = snapshot_user(interaction.user)
        expires_at = None
        if duration_hours:
            expires_at = (
                datetime.now(timezone.utc) +
                timedelta(hours=duration_hours)).isoformat()

        await db.execute("""
            INSERT INTO purchase_history
                (guild_id, user_id, user_display_name,
                 item_id, item_name, price_paid, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, snap["display_name"],
              iid, name, price, expires_at))

        await db.commit()

    # Give role if applicable
    if itype in ("role", "temp_role") and role_id:
        role = interaction.guild.get_role(int(role_id))
        if role:
            can, warn = check_bot_role_position(
                interaction.guild, role)
            if can:
                try:
                    await interaction.user.add_roles(
                        role, reason=f"Shop purchase: {name}")
                except Exception as e:
                    print(f"[SHOP] Role give error: {e}")

                # Track temp role
                if duration_hours and expires_at:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            INSERT INTO temp_roles
                                (guild_id, user_id, role_id,
                                 expires_at, source)
                            VALUES (?, ?, ?, ?, 'shop')
                        """, (guild_id, user_id,
                              role_id, expires_at))
                        await db.commit()
            else:
                print(f"[SHOP ROLE WARNING] {warn}")

    currency = await get_currency_name(guild_id)
    embed    = discord.Embed(
        title="✅ Purchase Successful!",
        description=(f"You bought **{name}** for "
                     f"**{price:,}** {currency}!"),
        color=0x57F287)
    if duration_hours:
        embed.set_footer(
            text=f"This role expires in {duration_hours} hours")
    await interaction.response.send_message(embed=embed, ephemeral=True)


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.temp_role_cleanup.start()

    def cog_unload(self):
        self.temp_role_cleanup.cancel()

    # ─── TEMP ROLE CLEANUP ──────────────────────────────
    @tasks.loop(minutes=10)
    async def temp_role_cleanup(self):
        """Removes expired temp roles every 10 minutes."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, user_id, role_id
                FROM temp_roles
                WHERE expires_at <= ?
            """, (now,))
            expired = await cursor.fetchall()

        for (entry_id, guild_id, user_id, role_id) in expired:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            member = guild.get_member(user_id)
            role   = guild.get_role(role_id)
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(
                        role, reason="Temp role expired")
                except Exception:
                    pass
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM temp_roles WHERE id = ?",
                    (entry_id,))
                await db.commit()

    @temp_role_cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # ─── SHOP COMMAND ───────────────────────────────────
    @app_commands.command(name="shop",
                          description="View the server shop")
    async def shop(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, name, description, price,
                       type, duration_hours, featured,
                       required_level, max_stock, current_stock
                FROM shop_items
                WHERE guild_id = ? AND enabled = 1
                ORDER BY featured DESC, price ASC
            """, (interaction.guild.id,))
            items = await cursor.fetchall()

        if not items:
            await interaction.response.send_message(
                "The shop is empty right now.", ephemeral=True)
            return

        currency = await get_currency_name(interaction.guild.id)
        embed    = discord.Embed(
            title=f"🛒 {interaction.guild.name} Shop",
            color=0x7c5cbf)

        for (iid, name, desc, price, itype,
             dur, featured, req_lvl, max_s, curr_s) in items:
            stock_info = ""
            if max_s:
                stock_info = (f" • {curr_s or 0}/{max_s} left"
                              if curr_s else " • **Out of stock**")
            dur_info = f" • {dur}h temp" if dur else ""
            lvl_info = f" • Req. Level {req_lvl}" if req_lvl else ""
            embed.add_field(
                name=f"{'⭐ ' if featured else ''}{name} — {price:,} {currency}",
                value=(f"{desc or ''}{dur_info}{lvl_info}{stock_info}"),
                inline=False)

        view = discord.ui.View()
        for (iid, name, desc, price, itype,
             dur, featured, req_lvl, max_s, curr_s) in items[:5]:
            if max_s and not curr_s:
                continue
            btn = discord.ui.Button(
                label=f"Buy {name}",
                style=discord.ButtonStyle.green,
                custom_id=f"shop_buy_{iid}")
            view.add_item(btn)

        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if (interaction.type == discord.InteractionType.component
                and interaction.data.get("custom_id", "").startswith(
                    "shop_buy_")):
            item_id = int(
                interaction.data["custom_id"].replace("shop_buy_", ""))
            await process_purchase(interaction, item_id)

    @app_commands.command(name="inventory",
                          description="View your purchased items")
    async def inventory(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT item_name, price_paid, purchased_at, expires_at
                FROM purchase_history
                WHERE guild_id = ? AND user_id = ?
                ORDER BY purchased_at DESC LIMIT 15
            """, (interaction.guild.id, interaction.user.id))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "Your inventory is empty.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎒 {interaction.user.display_name}'s Inventory",
            color=0x7c5cbf)
        for name, price, bought_at, expires_at in rows:
            exp_str = ""
            if expires_at:
                exp_str = f"\nExpires: {expires_at[:10]}"
            embed.add_field(
                name=name,
                value=(f"Paid: {price:,} coins\n"
                       f"Bought: {bought_at[:10] if bought_at else '?'}"
                       f"{exp_str}"),
                inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Shop(bot))
