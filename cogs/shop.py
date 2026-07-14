import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso
from utils.economy_safe import safe_deduct, safe_decrement_stock, InsufficientBalance


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
                   max_stock, current_stock, price_diamonds,
                   xp_boost_multiplier
            FROM shop_items
            WHERE id = ? AND guild_id = ? AND enabled = 1
        """, (item_id, guild_id))
        item = await cursor.fetchone()

    if not item:
        await interaction.response.send_message(
            "Item not found or disabled.", ephemeral=True)
        return

    (iid, name, price, itype, role_id, duration_hours,
     req_level, req_role_id, enabled, max_stock, curr_stock,
     price_diamonds, xp_boost_multiplier) = item

    # Phase 5 / Economy v2: an item is diamond-priced when
    # price_diamonds is set (nullable column — see database.py
    # migration). Coins-priced items are untouched, same as before.
    pay_currency = "diamonds" if price_diamonds else "balance"
    pay_amount   = price_diamonds if price_diamonds else price

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

    if req_role_id:
        req_role = interaction.guild.get_role(int(req_role_id))
        if req_role and req_role not in interaction.user.roles:
            await interaction.response.send_message(
                f"You need {req_role.mention} to buy this.",
                ephemeral=True)
            return

    # Phase 5 / Leveling expansion: an xp_boost item must have both a
    # multiplier and a duration configured — without both there's
    # nothing meaningful to grant. Checked before any balance/stock
    # is touched, same as the level/role gates above.
    if itype == "xp_boost":
        if not xp_boost_multiplier or xp_boost_multiplier <= 1.0:
            await interaction.response.send_message(
                "This XP boost item isn't configured correctly "
                "(missing or invalid multiplier). Ask an admin to fix it.",
                ephemeral=True)
            return
        if not duration_hours or duration_hours <= 0:
            await interaction.response.send_message(
                "This XP boost item isn't configured correctly "
                "(missing duration). Ask an admin to fix it.",
                ephemeral=True)
            return

    # P1 #11 FIX: previously stock and balance were checked with
    # plain SELECTs, then both decremented in separate UPDATEs
    # outside any shared transaction — two people buying the last
    # unit of a limited item at the same moment could both pass the
    # check and both succeed, overselling stock and/or letting a
    # buyer without enough balance still get charged into a negative
    # number. Now stock is claimed atomically first; if the balance
    # deduction that follows fails, the stock claim is released.
    stock_ok = await safe_decrement_stock(iid)
    if not stock_ok:
        await interaction.response.send_message(
            "This item is out of stock!", ephemeral=True)
        return

    try:
        await safe_deduct(guild_id, user_id, pay_amount,
                           currency=pay_currency,
                           reason=f"Shop purchase: {name}", source="shop")
    except InsufficientBalance:
        if max_stock:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE shop_items SET current_stock = current_stock + 1 WHERE id=?",
                    (iid,))
                await db.commit()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT {pay_currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
        bal = row[0] if row else 0
        if pay_currency == "diamonds":
            await interaction.response.send_message(
                f"You need {pay_amount:,} 💎 but only have {bal:,}.",
                ephemeral=True)
        else:
            currency = await get_currency_name(guild_id)
            await interaction.response.send_message(
                f"You need {pay_amount:,} {currency} but only have {bal:,}.",
                ephemeral=True)
        return

    snap       = snapshot_user(interaction.user)
    expires_at = None
    if duration_hours:
        expires_at = (
            datetime.now(timezone.utc) +
            timedelta(hours=duration_hours)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO purchase_history
                (guild_id, user_id, user_display_name,
                 item_id, item_name, price_paid, expires_at,
                 currency_paid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, snap["display_name"],
              iid, name, pay_amount, expires_at, pay_currency))
        await db.commit()

    boost_expires_at = None
    if itype in ("role", "temp_role") and role_id:
        # Phase 3 / E2: role/temp_role granting now goes through the
        # shared Reward Engine instead of this cog's own copy of the
        # bot-role-position check + add_roles + temp_roles insert
        # (the same logic that used to be duplicated in cogs/events.py
        # too). Also fixes a small pre-existing inconsistency: this
        # used to write a temp_roles row whenever the item's
        # duration_hours happened to be set, regardless of whether the
        # item's declared type was "role" (permanent) or "temp_role" —
        # now it's keyed off itype itself, matching what the admin
        # actually configured in the shop.
        from utils.reward_engine import give_reward
        result = await give_reward(
            interaction.client, guild_id, user_id, itype,
            role_id=role_id,
            duration_hours=duration_hours if itype == "temp_role" else None,
            reason=f"Shop purchase: {name}",
            source="shop",
        )
        if not result.get("success"):
            print(f"[SHOP] Role give error: {result.get('error')}")
    elif itype == "xp_boost":
        # Phase 5 / Leveling expansion: grants a temporary XP
        # multiplier instead of a role or inventory item. Read by
        # utils.xp_calculator.calculate_message_xp() on every message,
        # stacking multiplicatively on top of any role-based bonus.
        from utils.xp_calculator import grant_xp_boost
        try:
            boost_expires_at = await grant_xp_boost(
                guild_id, user_id, xp_boost_multiplier,
                duration_hours, source="shop")
        except Exception as e:
            print(f"[SHOP] XP boost grant error: {e}")
    elif itype not in ("role", "temp_role"):
        # Phase 3 / E4: anything that isn't a role/temp_role/xp_boost
        # (i.e. the shop's "Custom" item type) is delivered into the
        # buyer's Inventory instead of silently doing nothing beyond
        # the purchase_history row above — previously a "custom"
        # item's only trace after purchase was the receipt, with
        # nothing a member could actually check or a future feature
        # (missions, trade) could query against. Routed through the
        # Reward Engine's 'item' type so it's logged/sourced
        # consistently with every other grant.
        from utils.reward_engine import give_reward
        result = await give_reward(
            interaction.client, guild_id, user_id, "item",
            amount=1, item_name=name, item_type="shop_custom",
            reason=f"Shop purchase: {name}", source="shop",
        )
        if not result.get("success"):
            print(f"[SHOP] Item give error: {result.get('error')}")

    embed = discord.Embed(
        title="✅ Purchase Successful!",
        description=(
            f"You bought **{name}** for **{pay_amount:,}** "
            f"{'💎' if pay_currency == 'diamonds' else await get_currency_name(guild_id)}!"),
        color=0x57F287)
    if itype == "xp_boost" and boost_expires_at:
        embed.add_field(
            name="⚡ XP Boost Active",
            value=f"{xp_boost_multiplier}x XP for the next {duration_hours} hours")
    elif duration_hours:
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
        """
        Removes expired temp roles every 10 minutes.

        PHASE 2 FIX: previously the DELETE FROM temp_roles fired
        unconditionally right after the remove_roles try/except-pass,
        regardless of whether the removal actually succeeded. A
        transient failure (rate limit, missing permission, network
        blip) meant the row was deleted anyway and the temp role just
        stayed on the member forever with no record left to retry
        against. The delete is now conditional on the removal
        actually succeeding (or there being nothing to remove), and
        each entry is isolated in its own try/except so one bad row
        can't take out the rest of the batch or the whole loop.

        Phase 5 / Leveling expansion: also sweeps expired
        leveling_active_boosts rows in the same tick — same
        "expires_at has passed" shape as temp_roles, and boosts have
        no permission/Discord-API side effect to retry on failure
        (it's a pure DB row), so this half is a plain unconditional
        cleanup rather than needing its own try/except-per-row.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, user_id, role_id
                FROM temp_roles
                WHERE expires_at <= ?
            """, (now,))
            expired = await cursor.fetchall()

        for (entry_id, guild_id, user_id, role_id) in expired:
            try:
                guild = self.bot.get_guild(guild_id)
                removal_ok = True
                if guild:
                    member = guild.get_member(user_id)
                    role   = guild.get_role(role_id)
                    if member and role and role in member.roles:
                        try:
                            await member.remove_roles(
                                role, reason="Temp role expired")
                        except Exception as e:
                            print(f"[SHOP] Failed to remove expired "
                                  f"role {role_id} from {user_id} in "
                                  f"{guild_id}: {e}")
                            removal_ok = False
                else:
                    # Bot isn't in this guild right now — keep the
                    # row so it's retried once it rejoins.
                    removal_ok = False

                if removal_ok:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "DELETE FROM temp_roles WHERE id = ?",
                            (entry_id,))
                        await db.commit()
            except Exception as e:
                print(f"[SHOP] temp_role_cleanup error for entry "
                      f"{entry_id}: {e}")

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM leveling_active_boosts WHERE expires_at <= ?",
                    (now,))
                await db.commit()
        except Exception as e:
            print(f"[SHOP] xp_boost cleanup error: {e}")

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
                       required_level, max_stock, current_stock,
                       price_diamonds, xp_boost_multiplier
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
             dur, featured, req_lvl, max_s, curr_s,
             price_diamonds, boost_mult) in items:
            stock_info = ""
            if max_s:
                stock_info = (f" • {curr_s or 0}/{max_s} left"
                              if curr_s else " • **Out of stock**")
            if itype == "xp_boost" and boost_mult:
                dur_info = f" • {boost_mult}x XP for {dur}h" if dur else f" • {boost_mult}x XP"
            else:
                dur_info = f" • {dur}h temp" if dur else ""
            lvl_info  = f" • Req. Level {req_lvl}" if req_lvl else ""
            price_str = (f"{price_diamonds:,} 💎" if price_diamonds
                         else f"{price:,} {currency}")
            embed.add_field(
                name=f"{'⭐ ' if featured else ''}{name} — {price_str}",
                value=(f"{desc or ''}{dur_info}{lvl_info}{stock_info}"),
                inline=False)

        view = discord.ui.View()
        for (iid, name, desc, price, itype,
             dur, featured, req_lvl, max_s, curr_s,
             price_diamonds, boost_mult) in items[:5]:
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

    # ─── INVENTORY ──────────────────────────────────────
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

        # Phase 3 / E4: purchase_history is a receipt log (what was
        # bought, when, for how much) — it was never a live "what do
        # they currently hold" view, and had no concept of quantity
        # or of items granted outside a purchase (events, missions).
        # This pulls that live view from the Inventory module and
        # shows it alongside the existing purchase history, rather
        # than replacing it.
        from utils.inventory import get_inventory
        held_items = await get_inventory(
            interaction.guild.id, interaction.user.id)

        # Phase 5 / Leveling expansion: show any currently active XP
        # boosts alongside held items — same "what do I actually have
        # right now" purpose, just a different table.
        from utils.xp_calculator import get_active_boost_multiplier
        active_boost = await get_active_boost_multiplier(
            interaction.guild.id, interaction.user.id)

        if not rows and not held_items and active_boost == 1.0:
            await interaction.response.send_message(
                "Your inventory is empty.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎒 {interaction.user.display_name}'s Inventory",
            color=0x7c5cbf)

        if active_boost > 1.0:
            embed.add_field(
                name="⚡ Active XP Boost",
                value=f"{active_boost}x XP", inline=False)

        if held_items:
            items_text = "\n".join(
                f"**{it['item_name']}** ×{it['quantity']}"
                for it in held_items[:15])
            embed.add_field(name="Held Items", value=items_text, inline=False)

        for name, price, bought_at, expires_at in rows[:10]:
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
