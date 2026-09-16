import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso
from utils.economy_safe import safe_deduct, safe_decrement_stock, InsufficientBalance
from utils.currency import get_currency_config, for_currency

SHOP_COLOR = 0x7c5cbf


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


# Rank Card foundation / Equip system: lets a member pick which owned
# role/temp_role item to wear from /inventory. Shares equip_role()
# (utils/equip_engine.py) with the auto-equip-on-grant path in
# utils/reward_engine.py — this is purely the manual-swap entry point,
# same underlying logic either way.
class InventoryEquipSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int,
                 role_items: list[dict], equipped_name: str | None):
        options = []
        for it in role_items[:25]:
            label = it["item_name"]
            if it["item_name"] == equipped_name:
                label = f"✅ {label} (equipped)"
            options.append(discord.SelectOption(
                label=label[:100], value=it["item_name"][:100]))
        super().__init__(
            placeholder="Equip a role...", options=options,
            custom_id="inventory_equip_select")
        self.guild_id = guild_id
        self.user_id  = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This isn't your inventory.", ephemeral=True)
            return
        from utils.equip_engine import equip_role
        result = await equip_role(
            interaction.client, self.guild_id, self.user_id, self.values[0])
        if result.get("success"):
            await interaction.response.send_message(
                f"Equipped role: **{result['role_name']}**",
                ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Could not equip that role — {result.get('error', 'something went wrong.')}",
                ephemeral=True)


class InventoryEquipView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int,
                 role_items: list[dict], equipped_name: str | None):
        super().__init__(timeout=120)
        self.add_item(InventoryEquipSelect(
            guild_id, user_id, role_items, equipped_name))


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
                   xp_boost_multiplier, prestige_tier
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
     price_diamonds, xp_boost_multiplier, prestige_tier) = item

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

    # Wallet pass: a potion carries the SAME effect parameters an
    # xp_boost item does (multiplier + duration) — the difference is
    # only that the effect is stored in inventory and fired on use
    # instead of at purchase. Validated up front with the same
    # strictness, and for the same reason: an unusable potion sitting
    # in someone's bag is worse than a refused purchase, because the
    # member only discovers it's broken after they've paid.
    if itype == "potion":
        if not xp_boost_multiplier or xp_boost_multiplier <= 1.0:
            await interaction.response.send_message(
                "This potion isn't configured correctly "
                "(missing or invalid effect multiplier). Ask an admin to fix it.",
                ephemeral=True)
            return
        if not duration_hours or duration_hours <= 0:
            await interaction.response.send_message(
                "This potion isn't configured correctly "
                "(missing effect duration). Ask an admin to fix it.",
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

    # ── Finalized Prestige purchase (I–V) ──────────────────────────
    # Prestige is not an inventory item and is never delivered through the
    # reward engine. The shop item's coin `price` is the MINIMUM balance
    # required; purchasing sets the Coins balance to 0 and never touches
    # Level/XP/Diamonds. Sequential + no-rebuy enforcement lives on the
    # backend in utils/prestige.purchase_prestige().
    if itype == "prestige":
        if price_diamonds:
            # Only reached on a misconfigured item, so the config read
            # stays on this error path instead of costing every purchase.
            cur_cfg = await get_currency_config(guild_id)
            await interaction.response.send_message(
                f"Prestige is purchased with {cur_cfg['coins']['name']}; "
                f"this item can't have a {cur_cfg['diamonds']['name']} "
                f"price. Ask an admin to fix it.",
                ephemeral=True)
            return
        if not prestige_tier or int(prestige_tier) not in (1, 2, 3, 4, 5):
            await interaction.response.send_message(
                "This Prestige item isn't configured correctly (missing or "
                "invalid tier). Ask an admin to fix it.", ephemeral=True)
            return

        from utils.prestige import (
            purchase_prestige, PrestigeError, tier_label,
            sync_prestige_roles,
        )
        try:
            result = await purchase_prestige(
                guild_id, user_id, int(prestige_tier), price,
                item_name=name, item_id=iid,
                display_name=interaction.user.display_name,
            )
        except PrestigeError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        # Sync the cosmetic roles to the member's TRUE effective tier
        # (sync_prestige_roles computes it from permanent + booster status),
        # so an active Booster keeps wearing their tier-VI role even after
        # buying a permanent tier. Roles are representation only.
        try:
            await sync_prestige_roles(
                interaction.client, interaction.guild, interaction.user)
        except Exception as e:
            print(f"[SHOP] Prestige role sync failed: {e}")

        cur = await get_currency_config(guild_id)
        cc = cur["coins"]
        embed = discord.Embed(
            title="⭐ Prestige Unlocked",
            description=(
                f"{interaction.user.mention} is now **Prestige "
                f"{tier_label(result['new_tier'])}**."),
            color=0xFFD700)
        embed.add_field(
            name=f"{cc['name']} reset",
            value=f"Your {cc['emoji']} **{cc['name']}** were reset to **0**.",
            inline=False)
        embed.add_field(
            name=f"Level / XP / {cur['diamonds']['name']}",
            value=(f"**Untouched** — your level, XP and "
                   f"{cur['diamonds']['emoji']} {cur['diamonds']['name']} are safe."),
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
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
            "This item is out of stock.", ephemeral=True)
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
        cur = await get_currency_config(guild_id)
        cinfo = for_currency(cur, pay_currency)
        await interaction.response.send_message(
            f"You need {pay_amount:,} {cinfo['emoji']} but only have {bal:,}.",
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
        # Rank Card foundation / Equip system: item_name=name is now
        # passed through so the inventory row give_reward() writes
        # (and the single-equipped-slot swap it performs) shows the
        # shop's own configured item name ("Flame") rather than
        # falling back to the raw Discord role name.
        from utils.reward_engine import give_reward
        result = await give_reward(
            interaction.client, guild_id, user_id, itype,
            role_id=role_id,
            duration_hours=duration_hours if itype == "temp_role" else None,
            reason=f"Shop purchase: {name}",
            source="shop",
            item_name=name,
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
    elif itype == "potion":
        # Wallet pass: delivered into inventory as a consumable rather
        # than applied now. The effect parameters ride along in the
        # inventory row's existing `metadata` JSON column (the same
        # column role items already use for {"role_id":...}), so the
        # potion stays usable even if the shop listing is later edited
        # or deleted — matching how inventory_items already references
        # items purely by name. Routed through the Reward Engine's
        # 'item' type so it's sourced/logged like every other grant.
        from utils.reward_engine import give_reward
        from utils.potion_engine import (
            POTION_ITEM_TYPE, EFFECT_XP_BOOST, build_metadata,
        )
        result = await give_reward(
            interaction.client, guild_id, user_id, "item",
            amount=1, item_name=name, item_type=POTION_ITEM_TYPE,
            item_metadata=build_metadata(
                EFFECT_XP_BOOST, xp_boost_multiplier, duration_hours),
            reason=f"Shop purchase: {name}", source="shop",
        )
        if not result.get("success"):
            print(f"[SHOP] Potion give error: {result.get('error')}")
    elif itype == "title":
        # Wallet pass: a title is a cosmetic, DB-only label — no
        # Discord role is created or assigned (that's the whole point
        # of the Title slot being independent from the Role slot).
        # quantity is left to accumulate naturally like any other item;
        # owning two copies is harmless since only one can be equipped.
        from utils.reward_engine import give_reward
        from utils.title_engine import TITLE_ITEM_TYPE
        result = await give_reward(
            interaction.client, guild_id, user_id, "item",
            amount=1, item_name=name, item_type=TITLE_ITEM_TYPE,
            reason=f"Shop purchase: {name}", source="shop",
        )
        if not result.get("success"):
            print(f"[SHOP] Title give error: {result.get('error')}")
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

    cur = await get_currency_config(guild_id)
    cinfo = for_currency(cur, pay_currency)
    embed = discord.Embed(
        title="✅ Purchase successful",
        description=(
            f"You bought **{name}** for **{pay_amount:,}** "
            f"{cinfo['emoji']} {cinfo['name']}."),
        color=0x57F287)
    if itype == "xp_boost" and boost_expires_at:
        embed.add_field(
            name="⚡ XP boost active",
            value=f"{xp_boost_multiplier:g}× XP for the next {duration_hours} hours")
    elif itype == "potion":
        # A potion's duration_hours describes its EFFECT once used,
        # not an expiry on the item itself.
        embed.add_field(
            name="🧪 Added to your bag",
            value=(f"Activate it from `/wallet` → Inventory → Potions "
                   f"to get **{xp_boost_multiplier:g}× XP for "
                   f"{duration_hours}h**."))
    elif itype == "title":
        embed.add_field(
            name="🏷️ Title unlocked",
            value="Equip it from `/wallet` → Inventory → Titles.")
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

                    # Rank Card foundation / Equip system: the role's
                    # inventory row (and equipped_roles entry, if this
                    # was the equipped one) need to catch up now that
                    # the Discord role is actually gone — otherwise
                    # /inventory and the rank card would keep showing
                    # an item the member no longer has.
                    try:
                        from utils.equip_engine import cleanup_expired_role_item
                        await cleanup_expired_role_item(
                            guild_id, user_id, role_id)
                    except Exception as e:
                        print(f"[SHOP] temp_role inventory cleanup "
                              f"failed for entry {entry_id} "
                              f"(user={user_id} role={role_id}): {e}")
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
                       price_diamonds, xp_boost_multiplier, prestige_tier
                FROM shop_items
                WHERE guild_id = ? AND enabled = 1
                ORDER BY featured DESC, price ASC
            """, (interaction.guild.id,))
            items = await cursor.fetchall()

        if not items:
            await interaction.response.send_message(
                "The shop is empty right now.", ephemeral=True)
            return

        cur = await get_currency_config(interaction.guild.id)
        cc, cd = cur["coins"], cur["diamonds"]
        embed    = discord.Embed(
            title=f"🛒 {interaction.guild.name} Shop",
            color=SHOP_COLOR)

        for (iid, name, desc, price, itype,
             dur, featured, req_lvl, max_s, curr_s,
             price_diamonds, boost_mult, prestige_tier) in items:
            stock_info = ""
            if max_s:
                stock_info = (f" • {curr_s or 0}/{max_s} left"
                              if curr_s else " • **Out of stock**")
            if itype == "xp_boost" and boost_mult:
                dur_info = f" • {boost_mult:g}× XP for {dur}h" if dur else f" • {boost_mult:g}× XP"
            elif itype == "prestige" and prestige_tier:
                from utils.prestige import tier_label
                dur_info = f" • Prestige {tier_label(prestige_tier)}"
            else:
                dur_info = f" • {dur}h temp" if dur else ""
            lvl_info  = f" • Req. Level {req_lvl}" if req_lvl else ""
            price_str = (f"{price_diamonds:,} {cd['emoji']}" if price_diamonds
                         else f"{price:,} {cc['emoji']} {cc['name']}")
            embed.add_field(
                name=f"{'⭐ ' if featured else ''}{name} — {price_str}",
                value=(f"{desc or ''}{dur_info}{lvl_info}{stock_info}"),
                inline=False)

        view = discord.ui.View()
        for (iid, name, desc, price, itype,
             dur, featured, req_lvl, max_s, curr_s,
             price_diamonds, boost_mult, prestige_tier) in items[:5]:
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
                          description="Open your inventory (inside your wallet)")
    async def inventory(self, interaction: discord.Interaction):
        # /inventory now opens the wallet hub with the inventory
        # ready. The old verbose purchase-history dump is gone —
        # receipts live under Wallet → Receipts, and inventory is the
        # clean tabs view from cogs/wallet.py. This keeps one source
        # of truth for inventory rendering and removes the
        # "Paid/Bought/Expires" debug-style output entirely.
        from cogs.wallet import render_hub
        await render_hub(
            interaction, interaction.guild.id, interaction.user.id,
            first=True)


async def setup(bot):
    await bot.add_cog(Shop(bot))
