import asyncio

import discord
import aiosqlite
from datetime import datetime, timedelta, timezone
from database import DB_PATH
from utils.economy_safe import safe_credit, safe_deduct, InsufficientBalance
from utils.permissions import check_bot_role_position
from utils.xp_calculator import xp_progress


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
                       item_metadata: dict = None,
                       defer_retry_seconds: float | None = None,
                       idempotency_key: str | None = None) -> dict:
    """...

    ``idempotency_key`` marks a ONE-SHOT xp award - a mission, tag mission,
    minigame, event or tag-partner grant whose triggering work the caller has
    already committed. The award is queued under that key before it is
    attempted and retried until it lands, so a correction lease that outlives
    the caller's wait can defer it but can never delete it. Repeating the same
    key never pays twice. Recurring awards (message/voice XP) must NOT pass it:
    they are dropped on a fence and simply re-earned, which is correct for them.
    """
    if reward_type in ("coins", "diamonds"):
        if amount is None:
            raise RewardError(f"{reward_type} reward requires 'amount'")
        amount = int(amount)
        currency = "balance" if reward_type == "coins" else "diamonds"
        try:
            if amount >= 0:
                # Finalized Prestige: apply the earn-time multiplier based on
                # the user's EFFECTIVE Prestige tier. Only positive earnings
                # are scaled; deductions are never multiplied. Prestige is
                # disabled by default until configured (always 1.0 then).
                # bot is passed so an active Booster resolves to effective VI.
                # Defensive: if prestige lookup ever fails, grant the raw
                # amount — a reward path must never crash on a config error.
                try:
                    from utils.prestige import get_prestige_earn_multiplier, is_booster
                    booster = None
                    if bot is not None:
                        g = bot.get_guild(guild_id)
                        m = g.get_member(user_id) if g else None
                        if m is not None:
                            booster = is_booster(m)
                    mult = await get_prestige_earn_multiplier(
                        guild_id, user_id, currency, is_booster=booster, bot=bot)
                except Exception as e:
                    print(f"[PRESTIGE] multiplier lookup failed; granting "
                          f"raw (guild={guild_id} user={user_id}): {e}")
                    mult = 1.0
                final_amount = int(round(amount * mult))
                if final_amount == 0 and amount > 0:
                    final_amount = amount
                await safe_credit(guild_id, user_id, final_amount,
                                   currency=currency, reason=reason, source=source)
            else:
                final_amount = amount
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
                "amount": final_amount, "new_balance": row[0] if row else 0}

    elif reward_type == "xp":
        if amount is None:
            raise RewardError("xp reward requires 'amount'")
        amount = int(amount)
        # XP sources are independent. Message XP and Voice XP each apply their
        # own switches in their runtime listeners; shared rewards (missions,
        # events, minigames, tags, claims) are never suppressed by Message XP.
        from utils.level_claims import (
            member_is_booster, record_crossing, snapshot_multipliers,
        )
        member = None
        guild = None
        if bot is not None and hasattr(bot, "get_guild"):
            guild = bot.get_guild(guild_id)
        if guild is not None and hasattr(guild, "get_member"):
            member = guild.get_member(user_id)
        multipliers = await snapshot_multipliers(
            guild_id, user_id, member=member, bot=bot)

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
        # How long an award may wait for a correction to finish before it gives
        # up. Message and voice XP pass 0: the very next message/minute earns the
        # XP again, so blocking the listener is strictly worse than deferring.
        # Every OTHER source (missions, events, minigames, tags) is one-shot - it
        # is paid once and never retried by its caller - so it waits a few
        # seconds rather than losing the reward.
        wait_for = (3.0 if defer_retry_seconds is None else defer_retry_seconds)

        # Pay anything this member earned earlier while a correction had them
        # fenced. One indexed SELECT when there is nothing queued. This is what
        # lets a deferred one-shot award land without waiting for a timer: the
        # member's very next XP event settles it.
        drained = None
        try:
            from utils import level_ops as ops
            drained = await ops.drain_xp_grants(guild_id, user_id)
        except Exception as exc:
            # A drain failure must never cost the award being granted now.
            print(f"[XP] pending-grant drain failed (guild={guild_id} "
                  f"user={user_id}): {exc}")

        # One-shot awards: record the award FIRST, then try to apply it.
        #
        # A recurring award (message/voice XP) that the fence refuses simply
        # earns the XP again on the next tick, so dropping it costs nothing. A
        # one-shot award - a mission, tag mission, minigame, event or tag-partner
        # grant - has already committed the work that earned it, so refusing it
        # would delete a benefit the member earned. It is queued under
        # `idempotency_key` and retried until it lands, which is the only design
        # that survives a correction lasting longer than any fixed wait.
        if idempotency_key:
            from utils import level_ops as ops
            loop = asyncio.get_running_loop()
            fresh = await ops.enqueue_xp_grant(
                guild_id, user_id, amount, idem_key=idempotency_key,
                source=source, reason=reason)
            if not fresh:
                prior = await ops.grant_row(idempotency_key) or {}
                if prior.get("status") == "applied":
                    # `INSERT OR IGNORE` on the UNIQUE key guarantees there has
                    # only ever been one row for this completed mission / round
                    # / event, so an already-applied row is that same award,
                    # not a second one.
                    #
                    # Unless THIS call is what applied it: the opening drain
                    # above settles every queued row for this member, including
                    # a grant the caller pre-enqueued in the same transaction
                    # that committed the work which earned it. That is the
                    # normal path now, and it is this award being delivered -
                    # reporting it back as a duplicate would tell the caller
                    # the reward failed when the XP has just landed.
                    own = next(
                        (a for a in ((drained or {}).get("applied") or [])
                         if str(a.get("idem_key")) == str(idempotency_key)),
                        None)
                    if own is not None:
                        paid = int(own["amount"])
                        new_xp = int(own["new_xp"])
                        old_level = xp_progress(max(0, new_xp - paid))[0]
                        new_level, _, _ = xp_progress(new_xp)
                        return {"success": True, "reward_type": "xp",
                                "amount": paid, "new_xp": new_xp,
                                "old_level": old_level,
                                "new_level": new_level,
                                "leveled_up": new_level > old_level,
                                "queued": False}
                    return {"success": False, "reward_type": "xp", "amount": 0,
                            "duplicate": True, "deferred": False,
                            "queued": False,
                            "error": ("this reward was already recorded under "
                                      f"'{idempotency_key}' and is not paid "
                                      "twice"),
                            "old_level": None, "new_level": None,
                            "leveled_up": False}
                # Still `pending`. Either this is a retry after an interrupted
                # run, or the caller pre-enqueued the grant in the same
                # transaction that committed the work which earned it - in both
                # cases there is exactly one row and it is still owed, so fall
                # through and deliver it. Treating this as a duplicate would
                # return "already recorded" while the XP sits unapplied.
            deadline = loop.time() + max(0.0, float(wait_for))
            blocked = None
            while True:
                out = await ops.drain_xp_grants(guild_id, user_id,
                                                idem_key=idempotency_key)
                if out["applied"]:
                    granted = out["applied"][0]
                    new_xp = int(granted["new_xp"])
                    new_level, _, _ = xp_progress(new_xp)
                    return {"success": True, "reward_type": "xp",
                            "amount": amount, "new_xp": new_xp,
                            "old_level": xp_progress(max(0, new_xp - amount))[0],
                            "new_level": new_level, "leveled_up": True,
                            "queued": False}
                blocked = (out["deferred"][0]["reason"] if out["deferred"]
                           else (out["failed"][0]["error"]
                                 if out["failed"] else blocked))
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(0.5)
            # Still fenced. NOT lost: the row is durable and every later drain
            # (the next award for this member, the next activity, or the
            # periodic sweep) applies it once the correction finishes.
            return {"success": False, "reward_type": "xp", "amount": 0,
                    "deferred": True, "queued": True,
                    "error": (f"{blocked or 'a level correction is in progress'} "
                              "- this reward has been recorded and will be "
                              "applied automatically once the correction "
                              "finishes; it is not earned again by ordinary "
                              "play, so it is not dropped"),
                    "old_level": None, "new_level": None, "leveled_up": False}

        deadline = None
        while True:
            from utils import level_ops as ops
            deferred_reason = None
            # One BEGIN IMMEDIATE transaction per attempt, spanning the fence
            # check and the write. The barrier runs FIRST, inside the
            # transaction that already holds SQLite's write lock, so the lease
            # it reads cannot change before this award commits:
            # `ops.fence_live_writers()` needs the same lock, so either the
            # fence committed first (this attempt defers, and nothing was
            # written) or this award commits first (the fence counts it and the
            # correction re-runs against state that includes it).
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    deferred_reason = await ops.xp_write_barrier(
                        guild_id, user_id, db)
                    if deferred_reason is None:
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
                        # Entitlements use xp_progress, not the stored level
                        # column. Auto-grant stays off; the member claims the
                        # snapshot later.
                        await record_crossing(
                            db, guild_id, user_id, old_xp, new_xp, multipliers,
                            source=source, is_booster=member_is_booster(member))
                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise
            if deferred_reason is None:
                break
            loop = asyncio.get_running_loop()
            if deadline is None:
                deadline = loop.time() + max(0.0, float(wait_for))
            if loop.time() >= deadline:
                # Be honest about what a deferral costs. Recurring XP
                # (message/voice, which pass defer_retry_seconds=0) is simply
                # earned again by the next tick, so dropping it costs nothing.
                # Anything else is one-shot: without `idempotency_key` this
                # award really may never be retried, and the message must not
                # claim otherwise.
                recurring = (defer_retry_seconds is not None
                             and float(defer_retry_seconds) == 0)
                tail = ("nothing was written, so the next message or voice "
                        "tick earns this XP again"
                        if recurring else
                        "nothing was written and this award is one-shot, so "
                        "it should be retried with idempotency_key to be safe")
                return {"success": False, "reward_type": "xp", "amount": 0,
                        "deferred": True, "queued": False,
                        "error": f"{deferred_reason} - {tail}",
                        "old_level": None, "new_level": None,
                        "leveled_up": False}
            await asyncio.sleep(0.5)

        await _log_xp_ledger(guild_id, user_id, amount, new_xp, reason, source)

        leveled_up = new_level > old_level
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
