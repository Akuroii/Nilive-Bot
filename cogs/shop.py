import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import json
from datetime import datetime, timezone
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso
from utils.economy_safe import safe_deduct, safe_decrement_stock, InsufficientBalance
from utils.currency import get_currency_config, for_currency
from utils.emoji import CHECK_EMOJI
from utils.timed_ownership import (
    open_purchase, update_purchase, claim_purchase_transition,
    refund_purchase, format_remaining, parse_time, CATEGORY_COLOR,
    PURCHASE_PENDING, PURCHASE_DELIVERED, PURCHASE_UNCERTAIN,
    PURCHASE_FAILED,
)

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
            # The "equipped" marker goes in the option's emoji field, not in
            # its label: select-option labels are plain text, so a custom
            # emoji token there would render as the literal text
            # <a:check:id> instead of the glyph.
            equipped_emoji = None
            if it["item_name"] == equipped_name:
                label = f"{label} (equipped)"
                equipped_emoji = CHECK_EMOJI
            options.append(discord.SelectOption(
                label=label[:100], value=it["item_name"][:100],
                emoji=equipped_emoji))
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
                   price_diamonds,
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
     req_level, req_role_id, enabled,
     price_diamonds, xp_boost_multiplier, prestige_tier) = item

    # ── Prestige VI: Booster-only shop tier ────────────────────
    # Never written to levels.prestige; activation lives in
    # prestige_vi_activations and is only effective while boosting.
    # Server-side gate: the disabled button for non-boosters is UX,
    # this check is the actual enforcement.
    if itype == "prestige" and prestige_tier == 6:
        from utils.prestige import (
            activate_booster_prestige, PrestigeError, tier_label,
            sync_prestige_roles, is_booster,
        )
        if not is_booster(interaction.user):
            await interaction.response.send_message(
                "Prestige VI is a Booster-only tier.", ephemeral=True)
            return
        try:
            result = await activate_booster_prestige(
                guild_id, user_id, price,
                item_name=name, item_id=iid,
                display_name=interaction.user.display_name,
                bot=interaction.client,
            )
        except PrestigeError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        try:
            await sync_prestige_roles(
                interaction.client, interaction.guild, interaction.user)
        except Exception as e:
            print(f"[SHOP] Prestige VI role sync failed: {e}")
        embed = discord.Embed(
            title="⭐ Prestige VI Active",
            description=(
                f"{interaction.user.mention} activated **Prestige "
                f"{tier_label(6)}** for **Free** (Booster). No Coins were changed."),
            color=0xFFD700)
        embed.add_field(
            name="While boosting",
            value="Prestige VI stays active as long as your Server "
                  "Boost lasts; when it ends you return to your "
                  "permanent Prestige and VI activation is removed. "
                  "After re-boosting, activate again for Free.",
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return


    if itype == "prestige":
        from utils.shop_validation import integer, ShopValidationError
        try:
            req_level = integer(req_level if req_level is not None else 0, "Required level")
            if req_role_id not in (None, "", 0):
                req_role_id = integer(req_role_id, "Required role", minimum=1)
        except ShopValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

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
        from utils.shop_validation import prestige_terms, ShopValidationError
        try:
            price, prestige_tier = prestige_terms(price, prestige_tier, price_diamonds)
        except ShopValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
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

    async with aiosqlite.connect(DB_PATH) as db:
        stock_row = await (await db.execute(
            "SELECT max_stock,current_stock FROM shop_items WHERE id=? AND guild_id=?",
            (iid, guild_id))).fetchone()
    if stock_row is None:
        await interaction.response.send_message("Item not found.", ephemeral=True)
        return
    max_stock, curr_stock = stock_row
    # Price validation, unchanged in what it refuses: a NULL/negative/
    # non-integer charge is still a misconfigured listing, never a free one.
    # Free is detected explicitly as pay_amount == 0 (never `<= 0`, never
    # truthiness) and is the ONLY difference in what follows: no balance is
    # read or written for it.
    if pay_amount is None or not isinstance(pay_amount, int) or pay_amount < 0:
        await interaction.response.send_message("Paid Shop items require a positive price.", ephemeral=True)
        return
    free = pay_amount == 0

    # ── Purchase saga ───────────────────────────────────────────────────
    # What used to happen: stock was claimed, money was deducted and a
    # purchase_history row was written BEFORE delivery, every delivery failure
    # was swallowed with print(), and the success embed was sent regardless. A
    # member could be charged for a product they never received and be told the
    # purchase succeeded.
    #
    # Now every purchase is an explicit operation with a journal row written
    # BEFORE anything moves, and the success message is gated on delivery:
    #
    #   pending ──► delivered            (delivery succeeded, history written)
    #      │
    #      ├──────► failed ──► refunded  (known failure: money + stock once)
    #      └──────► uncertain            (outcome unknown: keep state, review)
    #
    # The ordering constraint that cannot be swapped: delivery must not happen
    # before the charge, or a failed payment would hand out free goods. So the
    # fix is compensation, not reordering.
    op_id = await open_purchase(
        guild_id, user_id, item_id=iid, item_name=name,
        currency=pay_currency, amount=0 if free else pay_amount)

    # P1 #11 FIX (kept): stock is claimed atomically FIRST; if the balance
    # deduction that follows fails, the stock claim is released. A Free item
    # claims stock exactly like a paid one.
    # Stock decrement + `stock_reserved=1` share a single BEGIN IMMEDIATE
    # transaction so a crash between them never leaks stock without a
    # journal flag.
    stock_ok = False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            try:
                stock_ok = await safe_decrement_stock(iid, db=db)
            except TypeError as type_exc:
                if "db" not in str(type_exc):
                    raise
                stock_ok = await safe_decrement_stock(iid)
            if stock_ok:
                try:
                    await update_purchase(op_id, stock_reserved=1, db=db)
                except TypeError as type_exc:
                    if "db" not in str(type_exc):
                        raise
                    await update_purchase(op_id, stock_reserved=1)
                await db.commit()
            else:
                await db.execute("ROLLBACK")
        except Exception as exc:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            try:
                await claim_purchase_transition(
                    op_id, from_statuses=(PURCHASE_PENDING,),
                    to_status=PURCHASE_FAILED,
                    detail=f"stock reservation error: {exc}")
            except Exception:
                pass
            raise

    if not stock_ok:
        await claim_purchase_transition(
            op_id, from_statuses=(PURCHASE_PENDING,),
            to_status=PURCHASE_FAILED, detail="out of stock")
        await interaction.response.send_message(
            "This item is out of stock.", ephemeral=True)
        return

    # Only the deduction is skipped for a Free item — safe_deduct() keeps its
    # own "amount must be positive" contract untouched; it is simply not called
    # when there is nothing to charge.
    # Balance deduction + `charged=1` share a single BEGIN IMMEDIATE
    # transaction so a crash after the debit rolls back the debit unless
    # `charged=1` commits with it.
    if not free:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    try:
                        await safe_deduct(
                            guild_id, user_id, pay_amount,
                            currency=pay_currency,
                            reason=f"Shop purchase: {name}", source="shop",
                            db=db)
                    except TypeError as type_exc:
                        if "db" not in str(type_exc):
                            raise
                        await safe_deduct(
                            guild_id, user_id, pay_amount,
                            currency=pay_currency,
                            reason=f"Shop purchase: {name}", source="shop")
                    try:
                        await update_purchase(op_id, charged=1, db=db)
                    except TypeError as type_exc:
                        if "db" not in str(type_exc):
                            raise
                        await update_purchase(op_id, charged=1)
                    await db.commit()
                except Exception:
                    try:
                        await db.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
        except InsufficientBalance:
            # Nothing was charged, so only the stock claim has to be undone.
            if await claim_purchase_transition(
                    op_id, from_statuses=(PURCHASE_PENDING,),
                    to_status=PURCHASE_FAILED, detail="insufficient balance"):
                await refund_purchase(op_id)
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
        except Exception as exc:
            # The charge transaction rolled back (charged=0), so only any
            # already-reserved stock needs to be released.
            try:
                if await claim_purchase_transition(
                        op_id, from_statuses=(PURCHASE_PENDING,),
                        to_status=PURCHASE_FAILED,
                        detail=f"charge error: {exc}"):
                    await refund_purchase(op_id)
            except Exception:
                pass
            raise

    # `color` is a Shop CATEGORY, not a reward type: internally a permanent
    # Colour is the existing `role` grant and a timed Colour is a timed role
    # holding. Nothing new is introduced on the delivery side, so every role
    # path (equip, entitlement checks, boost/prestige role guards) keeps
    # working unchanged.
    if itype == "color":
        internal_type = "temp_role" if duration_hours else "role"
        category = CATEGORY_COLOR
    else:
        internal_type = itype
        category = None

    delivery = await _deliver_purchase(
        interaction, guild_id=guild_id, user_id=user_id,
        itype=internal_type, listing_type=itype, name=name,
        role_id=role_id, duration_hours=duration_hours,
        multiplier=xp_boost_multiplier,
        category=category, product_id=iid, source="shop")

    if not delivery["success"]:
        await _compensate_failed_purchase(
            interaction, op_id=op_id, delivery=delivery, name=name,
            pay_amount=pay_amount, pay_currency=pay_currency, free=free)
        return

    # Receipt LAST: purchase_history now records finalized purchases only, and
    # the receipt insert + `pending -> delivered` transition execute atomically
    # inside one `BEGIN IMMEDIATE` transaction guarded by `refunded=0 AND
    # stock_returned=0`. If the operation left `pending` while delivery was in
    # flight (for example, parked as `uncertain` and refunded by an operator),
    # the transaction rolls back so no receipt row is persisted and `status` is
    # never overwritten into `delivered`.
    snap = snapshot_user(interaction.user)
    finalized = False
    history_id = None
    committed_outside_tx = False
    was_compensated = False
    persisted_descriptor = False
    late_rollback_spec = {
        "op_id": str(op_id),
        "guild_id": int(guild_id),
        "user_id": int(user_id),
        "item_id": int(item_id) if item_id is not None else None,
        "item_name": str(delivery.get("item_name") or name),
        "internal_type": str(internal_type),
        "category": str(category) if category is not None else None,
        "role_id": int(role_id) if role_id is not None else None,
        "entry_key": str(delivery["entry_key"]) if delivery.get("entry_key") else None,
        "role_added": bool(delivery.get("role_added", True)),
        "prior_inventory": (
            list(delivery["prior_inventory"])
            if delivery.get("prior_inventory") is not None else None
        ),
        "delivered_inventory": (
            list(delivery["delivered_inventory"])
            if delivery.get("delivered_inventory") is not None else None
        ),
        "boost_id": delivery.get("boost_id"),
        "boost_expires_at": delivery.get("boost_expires_at"),
        "inventory_rolled_back": False,
        "equip_reconciled": False,
        "role_reconciled": False,
        "retry_entry_key": None,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            try:
                history_id = await _write_purchase_history(
                    guild_id, user_id, snap["display_name"], iid, name,
                    pay_amount, delivery.get("expires_at"), pay_currency,
                    db=db)
            except TypeError as type_exc:
                if "db" not in str(type_exc):
                    raise
                history_id = await _write_purchase_history(
                    guild_id, user_id, snap["display_name"], iid, name,
                    pay_amount, delivery.get("expires_at"), pay_currency)
                committed_outside_tx = True

            finalized = await claim_purchase_transition(
                op_id,
                from_statuses=(PURCHASE_PENDING,),
                to_status=PURCHASE_DELIVERED,
                detail=delivery.get("detail") or None,
                history_id=history_id,
                require_uncompensated=True,
                db=db,
            )
            if finalized:
                await db.commit()
            else:
                await db.execute("ROLLBACK")
                try:
                    from utils.timed_ownership import (
                        PURCHASE_REFUNDED,
                        record_pending_delivery_rollback,
                    )
                    cur = await db.execute(
                        "SELECT status, refunded, stock_returned "
                        "FROM shop_purchase_operations WHERE op_id=?",
                        (op_id,),
                    )
                    op_state = await cur.fetchone()
                    was_compensated = bool(
                        op_state and (
                            op_state[0] in (PURCHASE_REFUNDED, PURCHASE_FAILED)
                            or op_state[1]
                            or op_state[2]
                        )
                    )
                    if was_compensated:
                        staged_ok = await record_pending_delivery_rollback(
                            op_id,
                            late_rollback_spec,
                            bot=interaction.client,
                            db=db,
                        )
                        if staged_ok:
                            await db.commit()
                            persisted_descriptor = True
                        else:
                            await db.execute("ROLLBACK")
                except Exception as stage_exc:
                    persisted_descriptor = False
                    print(
                        f"[SHOP] Pre-compensation descriptor write failed "
                        f"for op_id={op_id}: {stage_exc}"
                    )
                    try:
                        await db.execute("ROLLBACK")
                    except Exception:
                        pass
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise

    if not finalized:
        if committed_outside_tx and history_id is not None:
            try:
                async with aiosqlite.connect(DB_PATH) as cleanup_db:
                    await cleanup_db.execute(
                        "DELETE FROM purchase_history WHERE id=?",
                        (int(history_id),))
                    await cleanup_db.commit()
            except Exception:
                pass
        comp_res = {
            "complete": True,
            "durable_persisted": persisted_descriptor,
            "error": None,
        }
        try:
            from utils.timed_ownership import (
                PURCHASE_REFUNDED,
                compensate_late_delivery,
                get_purchase,
                record_pending_delivery_rollback,
            )
            if not was_compensated:
                cur_op = await get_purchase(op_id)
                was_compensated = bool(
                    cur_op and (
                        cur_op.get("status") in (PURCHASE_REFUNDED, PURCHASE_FAILED)
                        or cur_op.get("refunded")
                        or cur_op.get("stock_returned")
                    )
                )
            if was_compensated:
                if not persisted_descriptor:
                    persisted_descriptor = bool(
                        await record_pending_delivery_rollback(
                            op_id, late_rollback_spec, bot=interaction.client
                        )
                    )
                comp_res = await compensate_late_delivery(
                    op_id,
                    late_rollback_spec,
                    bot=interaction.client,
                    interaction=interaction,
                )
        except Exception as cleanup_exc:
            comp_res = {
                "complete": False,
                "durable_persisted": persisted_descriptor,
                "error": str(cleanup_exc),
            }
            print(f"[SHOP] Late delivery cleanup failed for {op_id}: {cleanup_exc}")
        embed = discord.Embed(
            title="⚠️ Purchase needs a check",
            description=(
                f"The purchase for **{name}** could not be finalized because "
                f"operation `{op_id}` was already updated or refunded while "
                f"delivery was in progress."
            ),
            color=0xFAA61A,
        )
        if was_compensated and not comp_res.get("complete", False):
            if comp_res.get("durable_persisted", False):
                embed.add_field(
                    name="Rollback pending reconciliation",
                    value=(
                        f"Late-delivery rollback for `{op_id}` did not complete "
                        f"immediately and has been queued for automatic reconciliation."
                    ),
                    inline=False,
                )
            else:
                print(
                    f"[SHOP] ERROR: Late-delivery rollback incomplete and durable "
                    f"recovery state could not be saved for op_id={op_id}: "
                    f"{comp_res.get('error')}"
                )
                embed.add_field(
                    name="Manual recovery required",
                    value=(
                        f"Late-delivery rollback for operation `{op_id}` did not "
                        f"complete and durable recovery state could not be saved "
                        f"to the database. Automatic recovery after a restart is "
                        f"not guaranteed; please contact staff with operation ID "
                        f"`{op_id}` to inspect and reconcile the remaining delivery state."
                    ),
                    inline=False,
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    cur = await get_currency_config(guild_id)
    cinfo = for_currency(cur, pay_currency)
    # Zero reads as Free, never as "0 Coins". Paid wording is unchanged.
    price_line = ("**Free**" if free
                  else f"**{pay_amount:,}** {cinfo['emoji']} {cinfo['name']}")
    embed = discord.Embed(
        title=f"{CHECK_EMOJI} Purchase successful",
        description=f"You bought **{name}** for {price_line}.",
        color=0x57F287)
    if itype == "xp_boost" and delivery.get("boost_expires_at"):
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
    elif delivery.get("expires_at"):
        # The countdown is read back from the ownership record, so what the
        # buyer sees is exactly what the cleanup loop will act on.
        embed.add_field(
            name="⏳ Temporary ownership",
            value=(f"Remaining: **{format_remaining(delivery['expires_at'])}** "
                   f"(<t:{_expiry_epoch(delivery['expires_at'])}:R>). "
                   f"The duration starts at this delivery."))
    elif duration_hours:
        embed.set_footer(text=f"This role expires in {duration_hours} hours")
    if delivery.get("warning"):
        embed.add_field(name="Note", value=delivery["warning"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _expiry_epoch(expires_at) -> int:
    """Discord relative-timestamp integer for a stored ISO expiry."""
    parsed = parse_time(expires_at)
    return int(parsed.timestamp()) if parsed else 0


async def _write_purchase_history(guild_id: int, user_id: int,
                                 display_name: str, item_id: int,
                                 item_name: str, price_paid: int,
                                 expires_at: str | None,
                                 currency: str | None, *,
                                 db: aiosqlite.Connection | None = None) -> int:
    """Insert the receipt row and return its id.

    A Free item records the normal receipt with a zero value, exactly as the
    Prestige VI activation does: price_paid = 0 with the same currency key a
    paid purchase of this listing would have used. No ledger row is written
    because no ledger movement occurred.

    When `db` is supplied, joins the caller's open transaction without
    committing so the receipt and the journal transition commit atomically.
    """
    sql = """
        INSERT INTO purchase_history
            (guild_id, user_id, user_display_name,
             item_id, item_name, price_paid, expires_at,
             currency_paid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (guild_id, user_id, display_name,
              item_id, item_name, price_paid, expires_at, currency)
    if db is not None:
        cursor = await db.execute(sql, params)
        return cursor.lastrowid
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.lastrowid


async def _deliver_purchase(interaction: discord.Interaction, *,
                            guild_id: int, user_id: int,
                            itype: str, listing_type: str, name: str,
                            role_id=None, duration_hours=None,
                            multiplier=None, category: str | None = None,
                            product_id: int | None = None,
                            source: str = "shop") -> dict:
    """Deliver one purchased product; returns a result dict, never raises.

    Every branch reports success explicitly. There is no "unknown type"
    fall-through: a listing whose type cannot be delivered fails, is
    compensated, and never produces a success message.

    `duration_hours` selects timed ownership for the branches that support it
    (Colors, Items). Potion/Title/xp_boost keep their dedicated semantics — a
    potion's duration is its effect once activated, a title has no expiry and
    an XP boost is applied immediately.
    """
    result = {"success": False, "expires_at": None, "warning": None,
              "detail": None, "boost_expires_at": None, "error": None,
              "uncertain": False, "entry_key": None}
    timed = bool(duration_hours)
    bot = interaction.client

    if itype in ("role", "temp_role"):
        if not role_id:
            result["error"] = "This listing has no role configured."
            return result
        if timed:
            from utils.timed_ownership import grant_timed_product
            granted = await grant_timed_product(
                bot, guild_id, user_id, item_name=name,
                duration_hours=duration_hours, item_type="temp_role",
                category=category, product_id=product_id,
                role_id=role_id, source=source,
                reason=f"Shop purchase: {name}")
            if not granted["success"]:
                result.update(error=granted.get("error"),
                              uncertain=granted.get("uncertain", False))
                if granted.get("uncertain"):
                    result["warning"] = (
                        "The role was applied but the ownership record could "
                        "not be saved; an admin should check this purchase.")
                return result
            result.update(success=True, expires_at=granted.get("expires_at"),
                          entry_key=granted.get("entry_key"),
                          role_added=bool(granted.get("role_added")),
                          detail=f"holding {granted.get('entry_key')}")
            return result

        from utils.reward_engine import give_reward
        extra: dict = {}
        if category:
            extra["category"] = category
        if product_id is not None:
            extra["product_id"] = int(product_id)
        granted = await give_reward(
            bot, guild_id, user_id, "role", role_id=role_id,
            reason=f"Shop purchase: {name}", source=source,
            item_name=name, metadata_extra=extra or None)
        if not granted.get("success"):
            result.update(
                error=granted.get("error") or "Role delivery failed.",
                uncertain=bool(granted.get("uncertain", False)),
            )
            if granted.get("uncertain"):
                result["warning"] = (
                    "The role or ownership state could not be cleanly "
                    "rolled back; an admin should check this purchase.")
            return result
        result.update(
            success=True,
            entry_key=granted.get("entry_key"),
            item_name=granted.get("item_name") or name,
            role_added=bool(granted.get("role_added")),
            prior_inventory=granted.get("prior_inventory"),
            delivered_inventory=granted.get("delivered_inventory"),
            detail=(f"permanent role ({granted['entry_key']})"
                    if granted.get("entry_key") else "permanent role"),
        )
        return result

    if itype == "xp_boost":
        # Legacy path kept intact: existing listings still deliver the
        # immediate, non-inventory boost they always did. New listings cannot
        # be created with this type (see utils/shop_validation.py).
        from utils.xp_calculator import grant_xp_boost
        try:
            boost_expires_at = await grant_xp_boost(
                guild_id, user_id, multiplier, duration_hours, source=source)
        except Exception as exc:
            result["error"] = f"XP boost grant failed: {exc}"
            return result
        if not boost_expires_at:
            result["error"] = "XP boost grant failed."
            return result
        boost_id = None
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT id FROM leveling_active_boosts "
                    "WHERE guild_id=? AND user_id=? AND expires_at=? AND source=? "
                    "ORDER BY id DESC LIMIT 1",
                    (guild_id, user_id, boost_expires_at, source))
                b_row = await cursor.fetchone()
                if b_row:
                    boost_id = int(b_row[0])
        except Exception:
            boost_id = None
        result.update(
            success=True,
            boost_expires_at=boost_expires_at,
            boost_id=boost_id,
        )
        return result

    if itype == "potion":
        # Delivered into inventory as a consumable rather than applied now.
        # The effect parameters ride along in the row's `metadata` JSON column,
        # so the potion stays usable even if the listing is edited or deleted.
        from utils.reward_engine import give_reward
        from utils.potion_engine import (
            POTION_ITEM_TYPE, EFFECT_XP_BOOST, build_metadata,
        )
        granted = await give_reward(
            bot, guild_id, user_id, "item",
            amount=1, item_name=name, item_type=POTION_ITEM_TYPE,
            item_metadata=build_metadata(
                EFFECT_XP_BOOST, multiplier, duration_hours),
            reason=f"Shop purchase: {name}", source=source)
        if not granted.get("success"):
            result.update(
                error=granted.get("error") or "Potion delivery failed.",
                uncertain=bool(granted.get("uncertain", False)),
            )
            return result
        result.update(
            success=True,
            item_name=granted.get("item_name") or name,
            prior_inventory=granted.get("prior_inventory"),
            delivered_inventory=granted.get("delivered_inventory"),
        )
        return result

    if itype == "title":
        # A title is a cosmetic, DB-only label — no Discord role is created or
        # assigned (that is the whole point of the Title slot being independent
        # from the Role slot).
        from utils.reward_engine import give_reward
        from utils.title_engine import TITLE_ITEM_TYPE
        granted = await give_reward(
            bot, guild_id, user_id, "item",
            amount=1, item_name=name, item_type=TITLE_ITEM_TYPE,
            reason=f"Shop purchase: {name}", source=source)
        if not granted.get("success"):
            result.update(
                error=granted.get("error") or "Title delivery failed.",
                uncertain=bool(granted.get("uncertain", False)),
            )
            return result
        result.update(
            success=True,
            item_name=granted.get("item_name") or name,
            prior_inventory=granted.get("prior_inventory"),
            delivered_inventory=granted.get("delivered_inventory"),
        )
        return result

    if itype in ("custom", "shop_custom", "item"):
        if timed:
            from utils.timed_ownership import grant_timed_product
            granted = await grant_timed_product(
                bot, guild_id, user_id, item_name=name,
                duration_hours=duration_hours, item_type="shop_custom",
                category=category, product_id=product_id, source=source,
                reason=f"Shop purchase: {name}")
            if not granted["success"]:
                result.update(error=granted.get("error"),
                              uncertain=granted.get("uncertain", False))
                return result
            result.update(success=True, expires_at=granted.get("expires_at"),
                          entry_key=granted.get("entry_key"),
                          role_added=bool(granted.get("role_added")),
                          detail=f"holding {granted.get('entry_key')}")
            return result

        from utils.reward_engine import give_reward
        granted = await give_reward(
            bot, guild_id, user_id, "item",
            amount=1, item_name=name, item_type="shop_custom",
            reason=f"Shop purchase: {name}", source=source)
        if not granted.get("success"):
            result.update(
                error=granted.get("error") or "Item delivery failed.",
                uncertain=bool(granted.get("uncertain", False)),
            )
            return result
        result.update(
            success=True,
            item_name=granted.get("item_name") or name,
            prior_inventory=granted.get("prior_inventory"),
            delivered_inventory=granted.get("delivered_inventory"),
        )
        return result

    result["error"] = f"Unsupported listing type: {listing_type}"
    return result


async def _compensate_failed_purchase(interaction: discord.Interaction, *,
                                      op_id: str, delivery: dict, name: str,
                                      pay_amount: int, pay_currency: str,
                                      free: bool) -> None:
    """React to a failed delivery without ever lying to the buyer.

    Three outcomes, deliberately distinct:

    * certain failure        → journal `failed`, money and stock returned once,
                               buyer told the purchase did not go through.
    * uncertain outcome      → journal `uncertain`, nothing returned, buyer told
                               to contact an admin. Refunding here could hand
                               out a product for free.
    * compensation half-done → the journal keeps the outstanding flag and the
                               reconciliation loop finishes it (only for
                               `failed`/`refunded` operations; `uncertain`
                               operations require manual admin review).
    """
    reason = delivery.get("error") or "Delivery failed."
    if delivery.get("uncertain"):
        await claim_purchase_transition(
            op_id,
            from_statuses=(PURCHASE_PENDING, PURCHASE_UNCERTAIN),
            to_status=PURCHASE_UNCERTAIN,
            detail=reason,
            require_uncompensated=True,
        )
        embed = discord.Embed(
            title="⚠️ Purchase needs a check",
            description=(f"The payment for **{name}** went through, but the "
                         f"delivery could not be confirmed. Nothing else was "
                         f"charged. Please contact an admin with this "
                         f"reference: `{op_id}`."),
            color=0xFAA61A)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    from utils.timed_ownership import get_purchase, PURCHASE_REFUNDED
    outcome = {"refunded": False, "stock_returned": False, "complete": False,
               "error": None}
    op_status = PURCHASE_FAILED
    stock_ok = not free
    if await claim_purchase_transition(
            op_id, from_statuses=(PURCHASE_PENDING,),
            to_status=PURCHASE_FAILED, detail=reason):
        outcome = await refund_purchase(op_id)
        try:
            cur_op = await get_purchase(op_id)
        except Exception:
            cur_op = None
        if cur_op is not None:
            op_status = cur_op.get("status") or PURCHASE_FAILED
            stock_ok = bool(
                cur_op.get("stock_returned") or not cur_op.get("stock_reserved")
            )
        else:
            stock_ok = bool(outcome.get("complete") or outcome.get("stock_returned"))
    else:
        try:
            cur_op = await get_purchase(op_id)
        except Exception as exc:
            cur_op = None
            outcome["error"] = str(exc)
        if cur_op is not None:
            op_status = cur_op.get("status")
            refunded_ok = bool(cur_op.get("refunded") or not cur_op.get("charged"))
            stock_ok = bool(
                cur_op.get("stock_returned") or not cur_op.get("stock_reserved")
            )
            outcome = {
                "refunded": bool(cur_op.get("refunded")),
                "stock_returned": bool(cur_op.get("stock_returned")),
                "complete": (
                    refunded_ok and stock_ok
                    and op_status in (PURCHASE_FAILED, PURCHASE_REFUNDED)
                ),
                "error": None,
            }
        else:
            op_status = None
            stock_ok = False

    auto_retried = op_status in (PURCHASE_FAILED, PURCHASE_REFUNDED)

    if free:
        if outcome.get("complete") and stock_ok:
            # Nothing was charged and no stock reservation remains held.
            description = (f"**{name}** could not be delivered, so the purchase "
                           f"was cancelled. You were not charged.")
        else:
            description = (
                f"**{name}** could not be delivered. You were not charged, "
                f"but its stock reservation is still held (reference: `{op_id}`).")
    elif outcome.get("refunded"):
        description = (
            f"**{name}** could not be delivered, so the purchase was "
            f"cancelled and your {pay_amount:,} payment was returned.")
    else:
        description = (
            f"**{name}** could not be delivered, so the purchase was "
            f"cancelled. Your {pay_amount:,} payment has not been returned yet "
            f"(reference: `{op_id}`).")
    embed = discord.Embed(
        title="❌ Purchase not completed",
        description=description,
        color=0xED4245)
    embed.add_field(name="Reason", value=reason, inline=False)
    if free:
        if not (outcome.get("complete") and stock_ok):
            if auto_retried:
                embed.add_field(
                    name="Stock release pending",
                    value=("The reserved stock could not be released yet. It is "
                           "retried automatically; keep this reference if you "
                           f"need support: `{op_id}`"),
                    inline=False)
            else:
                embed.add_field(
                    name="Manual review required",
                    value=(f"This purchase is marked `{op_status or PURCHASE_UNCERTAIN}` "
                           "and requires manual admin review (it will not be "
                           "released automatically). Please contact an admin with "
                           f"reference: `{op_id}`"),
                    inline=False)
    elif not outcome.get("complete") or not outcome.get("refunded"):
        if auto_retried:
            embed.add_field(
                name="Refund pending",
                value=("Part of the refund could not be applied yet. It is retried "
                       "automatically; keep this reference if you need support: "
                       f"`{op_id}`"),
                inline=False)
        else:
            embed.add_field(
                name="Manual review required",
                value=(f"This purchase is marked `{op_status or PURCHASE_UNCERTAIN}` "
                       "and requires manual admin review (it will not be refunded "
                       "automatically). Please contact an admin with reference: "
                       f"`{op_id}`"),
                inline=False)
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
                    if member is None and hasattr(guild, "fetch_member"):
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            member = None
                        except Exception as e:
                            print(f"[SHOP] Failed to fetch member {user_id} "
                                  f"for expired role {role_id} in {guild_id}: {e}")
                            removal_ok = False
                    role = guild.get_role(role_id)
                    if removal_ok and member and role and role in member.roles:
                        try:
                            from utils.timed_ownership import role_still_entitled
                            still_entitled = await role_still_entitled(
                                guild_id, user_id, role_id,
                                bot=self.bot, member=member)
                        except Exception as e:
                            print(f"[SHOP] Failed to verify role entitlement "
                                  f"for expired role {role_id} on {user_id} in "
                                  f"{guild_id}: {e}")
                            removal_ok = False
                        else:
                            if not still_entitled:
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

        # ── Timed ownership reconciliation ──────────────────────────
        # Same 10-minute tick as the legacy temp_roles sweep above, so a
        # timed Color/Item is retired by one loop instead of a second timer
        # drifting against it. The legacy sweep stays: it owns the OLD
        # temp_roles rows, which are still the retry marker for roles granted
        # before timed_holdings existed. Both are safe to run in either order
        # — the holding is only deleted after its own role removal succeeded,
        # and `role_still_entitled()` keeps a role the member is independently
        # entitled to.
        #
        # Real-world delay, stated plainly: Discord only removes the role up to
        # ~10 minutes after the stored expiry, because this loop runs every
        # 10 minutes. Reads (/inventory, /rank) already treat the entry as
        # expired at the exact deadline; only the Discord role lags.
        try:
            from utils.timed_ownership import cleanup_expired_holdings
            stats = await cleanup_expired_holdings(self.bot)
            if stats.get("removed") or stats.get("retained") or stats.get("errors"):
                print(f"[SHOP] timed ownership cleanup: {stats}")
        except Exception as e:
            print(f"[SHOP] timed ownership cleanup error: {e}")

        # ── Purchase journal reconciliation ─────────────────────────
        # Finishes any compensation that was interrupted (money back but stock
        # not yet restored) and parks purchases that died before a delivery
        # result as `uncertain` for a human. It never auto-refunds an uncertain
        # purchase: that is the one decision this loop must not guess.
        try:
            from utils.timed_ownership import reconcile_purchases
            try:
                stats = await reconcile_purchases(bot=self.bot)
            except TypeError:
                stats = await reconcile_purchases()
            if any(stats.values()):
                print(f"[SHOP] purchase reconciliation: {stats}")
        except Exception as e:
            print(f"[SHOP] purchase reconciliation error: {e}")

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
                       CASE WHEN type='prestige' AND prestige_tier=6 THEN 0 ELSE required_level END,
                       CASE WHEN type='prestige' AND prestige_tier=6 THEN NULL ELSE max_stock END,
                       CASE WHEN type='prestige' AND prestige_tier=6 THEN NULL ELSE current_stock END,
                       price_diamonds, xp_boost_multiplier, prestige_tier
                FROM shop_items
                WHERE guild_id = ? AND enabled = 1
                  AND option_of_id IS NULL
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
            vi = itype == "prestige" and prestige_tier == 6
            stock_info = ""
            if not vi and max_s:
                stock_info = (f" • {curr_s or 0}/{max_s} left"
                              if curr_s else " • **Out of stock**")
            if itype == "xp_boost" and boost_mult:
                dur_info = f" • {boost_mult:g}× XP for {dur}h" if dur else f" • {boost_mult:g}× XP"
            elif itype == "prestige" and prestige_tier:
                from utils.prestige import tier_label
                dur_info = f" • Prestige {tier_label(prestige_tier)}"
                if int(prestige_tier) == 6:
                    dur_info += " • Booster only"
            else:
                dur_info = f" • {dur}h temp" if dur else ""
            lvl_info  = f" • Req. Level {req_lvl}" if req_lvl else ""
            # Free = zero charge: no diamond price and an explicit coin price
            # of 0, plus Prestige VI by its own rule. Explicit comparison, not
            # truthiness, so nothing else can drift into looking free.
            free = vi or (not price_diamonds and price == 0)
            price_str = "Free" if free else (f"{price_diamonds:,} {cd['emoji']}" if price_diamonds
                         else f"{price:,} {cc['emoji']} {cc['name']}")
            embed.add_field(
                name=f"{'⭐ ' if featured else ''}{name} — {price_str}",
                value=(f"{desc or ''}{dur_info}{lvl_info}{stock_info}"),
                inline=False)

        view = discord.ui.View()
        for (iid, name, desc, price, itype,
             dur, featured, req_lvl, max_s, curr_s,
             price_diamonds, boost_mult, prestige_tier) in items[:5]:
            vi = itype == "prestige" and prestige_tier == 6
            if not vi and max_s and not curr_s:
                continue
            # Label only — the custom_id (the purchase handle) is untouched.
            free = vi or (not price_diamonds and price == 0)
            if vi:
                btn_label = f"Activate {name} — Free"
            elif free:
                btn_label = f"Buy {name} — Free"
            else:
                btn_label = f"Buy {name}"
            btn = discord.ui.Button(
                label=btn_label,
                style=discord.ButtonStyle.green,
                custom_id=f"shop_buy_{iid}")
            if (itype == "prestige" and prestige_tier
                    and int(prestige_tier) == 6):
                from utils.prestige import is_booster
                if not is_booster(interaction.user):
                    btn.disabled = True
                    btn.style = discord.ButtonStyle.gray
                    btn.label = f"{name} — Booster only"
            view.add_item(btn)

        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = data.get("custom_id", "")
        if not isinstance(custom_id, str):
            return

        select_prefix = "shop_buy_sel_"
        if custom_id.startswith(select_prefix):
            # context_id identifies the Select component; the selected value
            # remains the existing shop_buy_<item_id> purchase handle.
            context_id = custom_id[len(select_prefix):]
            if not context_id.isascii() or not context_id.isdigit():
                return

            values = data.get("values")
            if not isinstance(values, list) or len(values) != 1:
                return
            value = values[0]
            value_prefix = "shop_buy_"
            if not isinstance(value, str) or not value.startswith(value_prefix):
                return
            item_id_text = value[len(value_prefix):]
            if not item_id_text.isascii() or not item_id_text.isdigit():
                return

            await process_purchase(interaction, int(item_id_text))
            return

        if custom_id.startswith("shop_buy_"):
            item_id = int(custom_id.replace("shop_buy_", ""))
            await process_purchase(interaction, item_id)

    # ─── INVENTORY ──────────────────────────────────────
    @app_commands.command(name="inventory",
                          description="Open your inventory")
    async def inventory(self, interaction: discord.Interaction):
        # /inventory opens the Inventory panel DIRECTLY (Items tab),
        # not the Wallet hub. It shares one renderer/view with Wallet's
        # Inventory button — the only difference is `first=True`, which
        # sends a fresh ephemeral message instead of editing the one
        # the button was clicked on. One template, one interaction
        # logic, one ownership guard (WalletBaseView).
        from cogs.wallet import TAB_ITEMS, open_inventory_panel
        await open_inventory_panel(
            interaction, interaction.guild.id, interaction.user.id,
            TAB_ITEMS, first=True)


async def setup(bot):
    await bot.add_cog(Shop(bot))
