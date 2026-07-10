import discord
import aiosqlite
from datetime import datetime, timedelta, timezone
from database import DB_PATH
from utils.economy_safe import safe_credit, safe_deduct, InsufficientBalance
from utils.permissions import check_bot_role_position
from utils.xp_calculator import xp_progress, check_and_award_level_rewards

# ══════════════════════════════════════════════════════════════
# REWARD ENGINE (Phase 3, E2)
#
# WHY THIS EXISTS
# Before this engine, "give this member a reward" was implemented
# separately in at least three places with three slightly different
# bugs/gaps waiting to diverge further:
#   - cogs/events.py had its own give_reward() for coins/xp/role/temp_role
#   - cogs/shop.py's process_purchase() did its own role + temp_role
#     granting inline, with its own copy of the bot-role-position check
#   - cogs/leveling.py granted XP with its own raw INSERT/UPDATE and
#     its own level-up detection, duplicated across its message-XP and
#     voice-XP paths
#   - cogs/economy.py's admin addcoins/removecoins did their own raw
#     balance UPDATEs
#
# give_reward() below is the one place that knows how to grant any of
# coins, diamonds, xp, role, temp_role, or item — atomic economy ops,
# the bot-role-position permission guard, level-up detection, and
# (Phase 3, E4) inventory item delivery all live here exactly once.
# Every consumer (events, shop, leveling, future missions, admin
# grants) calls this instead of reimplementing it.
#
# Ledger hook (Phase 3, E3): coins/diamonds grants are logged
# automatically because safe_credit/safe_deduct now write to
# utils/ledger.py themselves. XP grants don't go through
# economy_safe (xp lives on the levels table, not economy), so this
# module logs xp transactions to the ledger directly, right after the
# level write commits, mirroring the same "log what already happened"
# pattern.
# ══════════════════════════════════════════════════════════════


class RewardError(Exception):
    """Raised when a reward can't be granted (bad input, insufficient
    balance for a negative coin/diamond grant, role misconfigured)."""
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
    """
    Single central reward-granting path.

    reward_type: 'coins' | 'diamonds' | 'xp' | 'role' | 'temp_role' | 'item'
    amount: required for coins/diamonds/xp (int or numeric string).
        Negative amounts are allowed for coins/diamonds (e.g. an admin
        deduction) and go through safe_deduct so they can't overdraw.
        For 'item', amount is used as the quantity (default 1).
    role_id: required for role/temp_role (int or numeric string).
    duration_hours: required for temp_role, ignored otherwise.
    item_name: required for 'item' — the inventory item to grant.
    item_type / item_metadata: optional extra detail stored alongside
        the item (Phase 3, E4 — see utils/inventory.py).
    reason: audit-trail text, passed through to Discord's own
        add_roles(reason=...) and to the ledger/inventory source log.
    source: free-text tag for where the grant came from ('event',
        'shop', 'leveling', 'admin', ...) — recorded in the ledger
        (E3) and inventory (E4) rows.

    Returns a result dict, always including "success" and "reward_type":
        coins/diamonds -> {"success", "reward_type", "amount", "new_balance"}
        xp             -> {"success", "reward_type", "amount", "new_xp",
                            "old_level", "new_level", "leveled_up"}
        role/temp_role -> {"success", "reward_type", "role_id",
                            "expires_at" (temp_role only)}
        item           -> {"success", "reward_type", "item_name",
                            "quantity", "new_quantity"}

    Raises RewardError for programmer-error-shaped problems (unknown
    reward_type, missing required args) so callers fail loudly during
    development rather than silently no-op'ing like the old per-feature
    copies sometimes did. Discord-side/inventory failures (role not
    found, bot can't assign it, insufficient balance for a deduction)
    are returned as {"success": False, "error": "..."} instead of
    raised, since those are expected, recoverable conditions callers
    already handle with a user-facing message.
    """
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

        async with aiosqlite.connect(DB_PATH) as db:
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

        # Phase 3 / E3: xp doesn't run through economy_safe (it's not
        # an economy currency), so it's logged to the ledger here,
        # right after the write commits.
        await _log_xp_ledger(guild_id, user_id, amount, new_xp, reason, source)

        leveled_up = new_level > old_level
        if leveled_up:
            guild = bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            if guild and member:
                await check_and_award_level_rewards(
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

        try:
            await member.add_roles(role, reason=reason)
        except Exception as e:
            return {"success": False, "reward_type": reward_type,
                    "error": f"Discord error: {e}"}

        result = {"success": True, "reward_type": reward_type,
                  "role_id": role_id}

        if reward_type == "temp_role":
            if not duration_hours:
                # Not fatal — the role was already granted — but flag
                # it clearly rather than silently making it permanent.
                result["warning"] = ("No duration_hours given; role "
                                      "was granted but NOT scheduled "
                                      "to expire.")
            else:
                expires_at = (
                    datetime.now(timezone.utc) +
                    timedelta(hours=duration_hours)
                ).isoformat()
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
        # Phase 3 / E4: generic stackable-item delivery, backed by
        # utils/inventory.py. Distinct from role/temp_role — this is
        # for genuinely inventory-style items (shop 'custom' type,
        # future mission/event drops, Trade System stock).
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
