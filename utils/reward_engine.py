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
                       metadata_extra: dict = None,
                       defer_retry_seconds: float | None = None,
                       idempotency_key: str | None = None,
                       op_id: str | None = None) -> dict:
    """...

    ``idempotency_key`` marks a ONE-SHOT xp award - a mission, tag mission,
    minigame, event or tag-partner grant whose triggering work the caller has
    already committed. The award is queued under that key before it is
    attempted and retried until it lands, so a correction lease that outlives
    the caller's wait can defer it but can never delete it. Repeating the same
    key never pays twice. Recurring awards (message/voice XP) must NOT pass it:
    they are dropped on a fence and simply re-earned, which is correct for them.

    ``op_id`` (``reward_type == "item"`` only) is the Shop purchase operation
    that is delivering. When given, the stack increment and a purchase grant
    (`utils.level_ops.record_purchase_grant`) commit in ONE transaction, and
    that grant is the only thing a later compensation may take the unit back
    by. Non-Shop item grants (missions, minigames) do not pass it and are
    unchanged: their units stay unattributed, which no reversal may touch.
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

        # Color identity travels with the ownership record, so a Colour stays
        # identifiable after the listing is renamed or deleted, and after it is
        # granted by an Event/Mission/Minigame instead of a purchase.
        #
        # Resolution is deliberately conservative: `role_id` -> exactly one
        # enabled Colors listing in this guild. A role no listing claims (or one
        # claimed by several with no product id passed in) must NOT become a
        # Colour, so an ordinary role reward is unaffected. The expiry meaning
        # is never taken from the listing here — this branch applies exactly the
        # duration the caller asked for.
        from utils.timed_ownership import color_metadata
        role_metadata = {"role_id": role_id, "expires_at": expires_at}
        if metadata_extra:
            role_metadata.update(metadata_extra)
        role_metadata = await color_metadata(guild_id, role_id, role_metadata)

        role_was_present = role in member.roles
        prior_inv = None
        delivered_inv = None
        inv_id = None
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, quantity, item_type, source, metadata, "
                "created_at, updated_at FROM inventory_items "
                "WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, display_name))
            prior_inv = await cursor.fetchone()

        async def _restore_prior_inventory() -> bool:
            return await rollback_role_inventory_grant(
                guild_id, user_id, display_name,
                prior_inv=prior_inv, delivered_inv=delivered_inv)

        from utils.inventory import set_quantity
        try:
            await set_quantity(
                guild_id, user_id, display_name, 1,
                item_type=reward_type, source=source,
                metadata=role_metadata)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT id, quantity, item_type, source, metadata, "
                    "created_at, updated_at FROM inventory_items "
                    "WHERE guild_id=? AND user_id=? AND item_name=?",
                    (guild_id, user_id, display_name))
                delivered_inv = await cursor.fetchone()
                if delivered_inv:
                    inv_id = int(delivered_inv[0])
        except Exception as exc:
            return {"success": False, "reward_type": reward_type,
                    "error": f"Inventory error: {exc}", "uncertain": False}

        entry_key = f"i:{inv_id}" if inv_id is not None else None
        from utils.equip_engine import equip_role
        try:
            equip_result = await equip_role(
                bot, guild_id, user_id, display_name,
                reason=reason, entry_key=entry_key)
        except Exception as exc:
            from utils.timed_ownership import is_ambiguous_discord_role_error
            role_added = (not role_was_present) and (role in member.roles)
            ambiguous = is_ambiguous_discord_role_error(
                exc, member=member, role=role)
            inv_restored = False
            if not role_added and not ambiguous:
                inv_restored = await _restore_prior_inventory()
            return {"success": False, "reward_type": reward_type,
                    "error": f"Failed to equip role: {exc}",
                    "uncertain": bool(role_added or ambiguous or not inv_restored)}

        if not equip_result.get("success"):
            role_added = (bool(equip_result.get("role_added"))
                          or ((not role_was_present) and (role in member.roles)))
            equip_uncertain = bool(equip_result.get("uncertain"))
            inv_restored = False
            if not role_added and not equip_uncertain:
                inv_restored = await _restore_prior_inventory()
            return {"success": False, "reward_type": reward_type,
                    "error": equip_result.get("error", "Failed to equip role"),
                    "uncertain": bool(
                        role_added or equip_uncertain or not inv_restored
                    )}

        role_added = (bool(equip_result.get("role_added"))
                      or ((not role_was_present) and (role in member.roles)))
        result = {"success": True, "reward_type": reward_type,
                  "role_id": role_id, "item_name": display_name,
                  "entry_key": equip_result.get("entry_key") or entry_key,
                  "role_added": role_added,
                  "prior_inventory": prior_inv,
                  "delivered_inventory": delivered_inv,
                  "uncertain": False}

        if reward_type == "temp_role":
            if not duration_hours:
                result["warning"] = ("No duration_hours given; role "
                                      "was granted but NOT scheduled "
                                      "to expire.")
            else:
                try:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            INSERT INTO temp_roles
                                (guild_id, user_id, role_id, expires_at, source)
                            VALUES (?, ?, ?, ?, ?)
                        """, (guild_id, user_id, role_id, expires_at, source))
                        await db.commit()
                except Exception as exc:
                    return {"success": False, "reward_type": reward_type,
                            "error": f"Could not record temporary role expiry: {exc}",
                            "uncertain": True}
                result["expires_at"] = expires_at

        return result

    elif reward_type == "item":
        if not item_name:
            raise RewardError("item reward requires 'item_name'")
        quantity = int(amount) if amount else 1
        if quantity <= 0:
            raise RewardError("item reward quantity must be positive")

        prior_inv = None
        delivered_inv = None
        grant_id = None
        from utils.inventory import give_item
        _snapshot_sql = (
            "SELECT id, quantity, item_type, source, metadata, "
            "created_at, updated_at FROM inventory_items "
            "WHERE guild_id=? AND user_id=? AND item_name=?")
        # One write transaction for: the pre-grant read, the increment, the
        # purchase grant (Shop only) and the post-grant read. Before this the
        # two reads and the write used three connections, so the "delivered"
        # snapshot could include a concurrent writer's units - and for a Shop
        # purchase there was no grant at all. The grant and the increment now
        # commit together or not at all: no delivered unit without its grant,
        # no grant without its unit.
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        _snapshot_sql, (guild_id, user_id, item_name))
                    prior_inv = await cursor.fetchone()
                    new_qty = await give_item(
                        guild_id, user_id, item_name,
                        quantity=quantity, item_type=item_type,
                        metadata=item_metadata, source=source, db=db)
                    if op_id is not None and str(op_id).strip():
                        from utils.level_ops import record_purchase_grant
                        grant_id = await record_purchase_grant(
                            db, guild_id=guild_id, user_id=user_id,
                            operation_id=str(op_id), item_name=item_name,
                            quantity=quantity)
                    cursor = await db.execute(
                        _snapshot_sql, (guild_id, user_id, item_name))
                    delivered_inv = await cursor.fetchone()
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
        except Exception as e:
            return {"success": False, "reward_type": "item",
                    "error": f"Inventory error: {e}", "uncertain": False}

        return {"success": True, "reward_type": "item",
                "item_name": item_name, "quantity": quantity,
                "new_quantity": new_qty, "prior_inventory": prior_inv,
                "delivered_inventory": delivered_inv,
                "grant_id": grant_id, "uncertain": False}

    else:
        raise RewardError(f"Unknown reward_type: {reward_type!r}")


async def rollback_role_inventory_grant(
    guild_id: int,
    user_id: int,
    item_name: str,
    *,
    prior_inv: tuple | None = None,
    delivered_inv: tuple | None = None,
    db: aiosqlite.Connection | None = None,
    tx_guard=None,
) -> bool:
    """Conditionally roll back a permanent-role inventory write (`set_quantity(..., 1)`).

    Never deletes or overwrites an `inventory_items` row unconditionally:
    - If the row still matches the state written by this delivery (`delivered_inv`),
      restores `prior_inv` (when one existed) or deletes the row (when none existed).
    - If a concurrent operation incremented `quantity` above the delivered quantity
      when no active prior stack existed, decrements only the 1 unit attributable
      to this purchase while preserving any units protected by `reward_grants`.
    - If a concurrent independent grant replaced the row (`cur_row != delivered_inv`
      with `quantity <= delivered_qty`), leaves the row untouched so the concurrent
      grant is never lost.
    - When `db` is provided, joins the caller's open transaction without committing;
      when `tx_guard` is provided, runs pre/post journal checks inside the same
      `BEGIN IMMEDIATE` transaction so the inventory rollback and purchase-journal
      progress update commit or roll back atomically.
    """
    async def _apply(conn: aiosqlite.Connection) -> bool:
        cursor = await conn.execute(
            "SELECT id, quantity, item_type, source, metadata, "
            "created_at, updated_at FROM inventory_items "
            "WHERE guild_id=? AND user_id=? AND item_name=?",
            (guild_id, user_id, item_name),
        )
        cur_row = await cursor.fetchone()
        if cur_row is None:
            return True

        cur_id, cur_qty, cur_type, cur_src, cur_meta, cur_cat, cur_uat = cur_row
        cur_qty_int = int(cur_qty or 0)
        protected_qty = 0
        try:
            prov_cur = await conn.execute(
                "SELECT COALESCE(SUM(remaining_quantity), 0) "
                "FROM reward_grants "
                "WHERE guild_id=? AND user_id=? AND item_name=? "
                "AND remaining_quantity > 0 AND COALESCE(unattributed, 0) = 0",
                (guild_id, user_id, item_name),
            )
            prov_row = await prov_cur.fetchone()
            if prov_row and prov_row[0] is not None:
                protected_qty = int(prov_row[0] or 0)
        except Exception as exc:
            if "no such table" not in str(exc).lower():
                raise
            protected_qty = 0

        if delivered_inv is not None:
            matches_delivered = tuple(cur_row) == tuple(delivered_inv)
        else:
            matches_delivered = cur_qty_int == 1

        if matches_delivered:
            if prior_inv is None:
                if protected_qty <= 0:
                    await conn.execute(
                        "DELETE FROM inventory_items "
                        "WHERE id=? AND quantity IS ? AND item_type IS ? "
                        "AND source IS ? AND metadata IS ? AND updated_at IS ?",
                        (cur_id, cur_qty, cur_type, cur_src, cur_meta, cur_uat),
                    )
            else:
                p_id, p_qty, p_type, p_src, p_meta, p_cat, p_uat = prior_inv
                target_qty = max(int(p_qty or 0), protected_qty)
                await conn.execute(
                    "UPDATE inventory_items "
                    "SET quantity=?, item_type=?, source=?, metadata=?, "
                    "created_at=?, updated_at=? "
                    "WHERE id=? AND quantity IS ? AND item_type IS ? "
                    "AND source IS ? AND metadata IS ? AND updated_at IS ?",
                    (
                        target_qty, p_type, p_src, p_meta, p_cat, p_uat,
                        cur_id, cur_qty, cur_type, cur_src, cur_meta, cur_uat,
                    ),
                )
        else:
            delivered_qty = int(
                (delivered_inv[1] if delivered_inv is not None else 1) or 1
            )
            prior_qty = int((prior_inv[1] if prior_inv is not None else 0) or 0)
            if cur_qty_int > delivered_qty and prior_qty <= 0 and (cur_qty_int - protected_qty) > 0:
                await conn.execute(
                    "UPDATE inventory_items "
                    "SET quantity = quantity - 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id=? AND quantity = ?",
                    (cur_id, cur_qty_int),
                )
        return True

    if db is not None:
        return await _apply(db)

    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            in_tx = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                in_tx = True
                if tx_guard is not None:
                    pre_state = await tx_guard("pre", conn)
                    if pre_state == "already_done":
                        await conn.execute("ROLLBACK")
                        in_tx = False
                        return True
                    if pre_state != "proceed":
                        await conn.execute("ROLLBACK")
                        in_tx = False
                        return False
                ok = await _apply(conn)
                if not ok:
                    await conn.execute("ROLLBACK")
                    in_tx = False
                    return False
                if tx_guard is not None:
                    post_ok = await tx_guard("post", conn)
                    if not post_ok:
                        await conn.execute("ROLLBACK")
                        in_tx = False
                        return False
                await conn.commit()
                in_tx = False
                return ok
            except Exception:
                if in_tx:
                    try:
                        await conn.execute("ROLLBACK")
                    except Exception:
                        pass
                raise
    except Exception as exc:
        print(f"[REWARD] Failed to roll back role inventory for {item_name!r}: {exc}")
        return False


# NOTE (G fix, 2026-10-30): `rollback_item_inventory_grant` used to live here. It
# decided how many units to take back by comparing the live stack with the
# `delivered_inventory` snapshot, which cannot distinguish the purchase's own
# unit from units granted later (incl. same-second ABA). Stackable
# compensation now reverses the purchase's own `reward_grants` row via
# `utils.level_ops.reverse_purchase_grant`; purchases with no such row are
# refused and stay pending for an operator (`legacy_no_provenance`).
