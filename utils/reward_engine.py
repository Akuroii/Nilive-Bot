import discord
import aiosqlite
from datetime import datetime, timedelta, timezone
from database import DB_PATH
from utils.economy_safe import safe_credit, safe_deduct, InsufficientBalance
from utils.permissions import check_bot_role_position
from utils.xp_calculator import (
    xp_progress, check_and_award_level_rewards,
    check_and_award_level_currency_rewards,
)


class RewardError(Exception):
    pass


async def _log_xp_ledger(guild_id: int, user_id: int, amount: int,
                          new_xp: int, reason: str, source: str):
    try:
        from utils.ledger import log_transaction
        await log_transaction(
            guild_id, user_id, "xp", amount, new_xp,
            type="credit" if amount >= 0 else "deduct",
            reason=reason, source=source)
    except Exception as e:
        print(f"[LEDGER] Failed to log xp transaction "
              f"(guild={guild_id} user={user_id}): {e}")


async def give_reward(bot: discord.Client,
                       guild_id: int,
                       user_id: int,
                       reward_type: str,
                       amount: int | str = None,
                       role_id: int | str = None,
                       duration_hours: int = None,
                       reason: str = "Reward",
                       source: str = "system",
                       item_name: str = None,
                       item_type: str = "custom",
                       item_metadata: dict = None) -> dict:
    if reward_type in ("coins", "diamonds"):
        if amount is None:
            raise RewardError(f"{reward_type} reward requires 'amount'")
        amount = int(amount)
        currency = "balance" if reward_type == "coins" else "diamonds"
        try:
            if amount >= 0:
                await safe_credit(guild_id, user_id, amount,
                                   currency=currency, reason=reason, source=source)
            else:
                await safe_deduct(guild_id, user_id, -amount,
                                   currency=currency, reason=reason, source=source)
        except InsufficientBalance:
            return {"success": False, "reward_type": reward_type,
                    "error": "Insufficient balance for this deduction"}

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
        return {"success": True, "reward_type": reward_type,
                "amount": amount, "new_balance": row[0] if row else 0}

    elif reward_type == "xp":
        if amount is None:
            raise RewardError("xp reward requires 'amount'")
        amount = int(amount)

        # CONCURRENCY FIX (dark-fixes pass): this used to be a plain
        # read (SELECT xp, level) followed by a separate write
        # (INSERT ... ON CONFLICT DO UPDATE) with no transaction
        # guard around the pair. Two XP grants landing close together
        # for the same member — e.g. a message-XP award racing a
        # voice-XP tick, or a shop XP-boost purchase racing either —
        # could both read the same old_xp, both compute new_xp
        # independently, and the second write would silently clobber
        # the first (a classic lost-update race), undercounting XP
        # and potentially skipping a level-up's role/currency reward
        # entirely. This now runs as one BEGIN IMMEDIATE transaction
        # spanning the read and the write, mirroring the pattern
        # utils/economy_safe.py already uses for coins/diamonds, so a
        # second concurrent grant is forced to wait and read the
        # already-updated row instead of overwriting it.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    SELECT xp, level FROM levels
                    WHERE guild_id = ? AND user_id = ?
                """, (guild_id, user_id))
                row = await cursor.fetchone()
                old_xp    = row[0] if row else 0
                old_level = row[1] if row else 0
                new_xp    = max(0, old_xp + amount)
                new_level, _, _ = xp_progress(new_xp)
                await db.execute("""
                    INSERT INTO levels (guild_id, user_id, xp, level)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id)
                    DO UPDATE SET xp = ?, level = ?
                """, (guild_id, user_id, new_xp, new_level,
                      new_xp, new_level))
                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

        await _log_xp_ledger(guild_id, user_id, amount, new_xp, reason, source)

        leveled_up = new_level > old_level
        if leveled_up:
            guild = bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            if guild and member:
                await check_and_award_level_rewards(
                    bot, member, guild_id, old_level, new_level)
                # Phase 5 / Leveling expansion: coin/diamond grants at
                # configured levels, alongside the role grants above.
                # Same trigger point (a real level-up), same resolved
                # guild/member — see utils/xp_calculator.py for why
                # this is a separate table/function from the role
                # rewards call just above.
                await check_and_award_level_currency_rewards(
                    bot, member, guild_id, old_level, new_level)

        return {"success": True, "reward_type": "xp", "amount": amount,
                "new_xp": new_xp, "old_level": old_level,
                "new_level": new_level, "leveled_up": leveled_up}

    elif reward_type in ("role", "temp_role"):
        if role_id is None:
            raise RewardError(f"{reward_type} reward requires 'role_id'")
        role_id = int(role_id)

        guild = bot.get_guild(guild_id)
        if not guild:
            return {"success": False, "reward_type": reward_type,
                    "error": "Bot is not in that guild"}
        member = guild.get_member(user_id)
        if not member:
            return {"success": False, "reward_type": reward_type,
                    "error": "Member not found in guild"}
        role = guild.get_role(role_id)
        if not role:
            return {"success": False, "reward_type": reward_type,
                    "error": "Role not found"}

        can_assign, warning = check_bot_role_position(guild, role)
        if not can_assign:
            return {"success": False, "reward_type": reward_type,
                    "error": warning}

        expires_at = None
        if reward_type == "temp_role" and duration_hours:
            expires_at = (
                datetime.now(timezone.utc) +
                timedelta(hours=duration_hours)
            ).isoformat()

        # Rank Card foundation / Equip system (locked with Dark): a
        # role/temp_role reward is a cosmetic collectible now, not
        # just a Discord permission grant — it lands in inventory_items
        # (quantity fixed at 1, roles don't stack) and, since the
        # Discord role has always been granted immediately on this
        # reward path, it also auto-equips (swapping out whatever was
        # previously equipped) via the same equip_role() swap the
        # manual /inventory picker uses. The inventory row is written
        # FIRST so equip_role() — which reads role_id back out of
        # inventory metadata rather than taking it as a raw param —
        # has something to resolve. display_name falls back to the
        # Discord role's own name only when the caller doesn't supply
        # a nicer one (cogs/shop.py passes the shop item's configured
        # name; other callers currently don't have one to offer).
        display_name = item_name or role.name

        from utils.inventory import set_quantity
        await set_quantity(
            guild_id, user_id, display_name, 1,
            item_type=reward_type, source=source,
            metadata={"role_id": role_id, "expires_at": expires_at})

        from utils.equip_engine import equip_role
        equip_result = await equip_role(
            bot, guild_id, user_id, display_name, reason=reason)
        if not equip_result.get("success"):
            return {"success": False, "reward_type": reward_type,
                    "error": equip_result.get("error", "Failed to equip role")}

        result = {"success": True, "reward_type": reward_type,
                  "role_id": role_id, "item_name": display_name}

        if reward_type == "temp_role":
            if not duration_hours:
                result["warning"] = ("No duration_hours given; role "
                                      "was granted but NOT scheduled "
                                      "to expire.")
            else:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT INTO temp_roles
                            (guild_id, user_id, role_id, expires_at, source)
                        VALUES (?, ?, ?, ?, ?)
                    """, (guild_id, user_id, role_id, expires_at, source))
                    await db.commit()
                result["expires_at"] = expires_at

        return result

    elif reward_type == "item":
        if not item_name:
            raise RewardError("item reward requires 'item_name'")
        quantity = int(amount) if amount else 1
        if quantity <= 0:
            raise RewardError("item reward quantity must be positive")

        from utils.inventory import give_item
        try:
            new_qty = await give_item(
                guild_id, user_id, item_name,
                quantity=quantity, item_type=item_type,
                metadata=item_metadata, source=source)
        except Exception as e:
            return {"success": False, "reward_type": "item",
                    "error": f"Inventory error: {e}"}

        return {"success": True, "reward_type": "item",
                "item_name": item_name, "quantity": quantity,
                "new_quantity": new_qty}

    else:
        raise RewardError(f"Unknown reward_type: {reward_type!r}")
