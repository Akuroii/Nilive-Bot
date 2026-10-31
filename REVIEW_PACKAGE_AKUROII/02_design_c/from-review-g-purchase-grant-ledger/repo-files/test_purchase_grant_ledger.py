"""Purchase grant ledger (G fix) regressions. Real SQLite; scratch databases only.

Run with: OWNER_ID=1 PYTHONPATH=.:scripts python -m pytest -q test_purchase_grant_ledger.py

What is under test:
  * `utils.reward_engine.give_reward(..., op_id=)` writes the stack increment and
    the `reward_grants` row (`grant_source='shop_purchase'`) in ONE transaction.
  * `utils.level_ops.consume_grants` consumes level grants first (by id), then
    purchase grants (by id); never a pure time-FIFO across the two groups.
  * `utils.level_ops.reverse_purchase_grant` takes back only the purchase's own
    grant, once, after checking guild/user/item and the returnable quantity.
  * `utils.timed_ownership.compensate_late_delivery` (stackable branch) uses that
    and nothing else: no snapshot comparison, no name-addressed deduction.
  * Legacy policy: a purchase with no grant row stays pending
    (`legacy_no_provenance`); only an operator can close it, with a note, and
    closing never removes a unit.
"""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from phase1_support import GUILD, USER, DB_PATH, execute, rows, reset_database
import aiosqlite
from utils import inventory
from utils import level_ops as LO
from utils import reward_engine as RE
from utils import timed_ownership as TO
from utils.reward_engine import give_reward

OTHER_USER = USER + 1
ITEM = "LedgerPotion"


async def open_op(name=ITEM, user=USER):
    return await TO.open_purchase(GUILD, user, item_id=1, item_name=name,
                                  currency="balance", amount=100)


async def deliver(op_id, name=ITEM, *, user=USER, item_type="potion"):
    """One unit, the way the Shop delivers since the ledger: with its grant."""
    res = await give_reward(None, GUILD, user, "item", amount=1, item_name=name,
                            item_type=item_type, source="shop", op_id=str(op_id))
    assert res.get("success") and res.get("grant_id"), res
    return res


def level_grant(name=ITEM, qty=1, *, claim_id, user=USER):
    """A level-reward grant exactly as the claim engine records it."""
    gid = f"lvl-{claim_id}"
    execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
            "reward_level,track,kind,item_name,quantity,remaining_quantity,"
            "operation_id,created_at,grant_source) VALUES "
            "(?,?,?,?,5,'level','inventory',?,?,?,?,'scratch','level_reward')",
            (gid, GUILD, user, claim_id, name, qty, qty, f"lvl-op-{claim_id}"))
    return gid


def stack(name=ITEM, user=USER):
    return rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND"
                " user_id=? AND item_name=?", (GUILD, user, name))


def grants(name=ITEM, user=USER):
    return rows("SELECT grant_id, grant_source, remaining_quantity,"
                " reversed_at IS NOT NULL FROM reward_grants WHERE guild_id=?"
                " AND user_id=? AND item_name=? ORDER BY id", (GUILD, user, name))


def spec_for(op_id, name=ITEM, *, user=USER, internal_type="potion",
             delivered=None, **extra):
    spec = {"guild_id": GUILD, "user_id": user, "item_id": 1, "item_name": name,
            "internal_type": internal_type, "inventory_rolled_back": False,
            "equip_reconciled": False, "role_reconciled": False}
    if delivered is not None:
        spec["delivered_inventory"] = list(delivered)
    spec.update(extra)
    return spec


async def refund_and_compensate(op_id, spec):
    await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)
    assert await TO.record_pending_delivery_rollback(op_id, spec, bot=None)
    TO._PENDING_DELIVERY_ROLLBACKS.clear()
    return await TO.compensate_late_delivery(op_id, dict(spec), bot=None)


def descriptor(op_id):
    return rows("SELECT delivery_rollback FROM shop_purchase_operations"
                " WHERE op_id=?", (op_id,))[0][0]


class LedgerBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        TO._PENDING_DELIVERY_ROLLBACKS.clear()


# ── 1. Delivery: increment + grant in one transaction ─────────────────────

class DeliveryTransactionTests(LedgerBase):
    async def test_purchase_writes_unit_and_grant_together(self):
        op = await open_op()
        res = await deliver(op)
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(grants(), [(res["grant_id"], "shop_purchase", 1, 0)])
        g = rows("SELECT claim_id, reward_level, track, kind, operation_id,"
                 " quantity FROM reward_grants WHERE grant_id=?",
                 (res["grant_id"],))[0]
        self.assertEqual(g, (None, None, "shop", "inventory", op, 1))

    async def test_failure_between_increment_and_grant_leaves_no_unit(self):
        """Crash window: the increment ran, the grant did not. The transaction
        is one, so the stack must not keep a unit without its grant."""
        op = await open_op()
        async def boom(*a, **k):
            raise RuntimeError("simulated failure after give_item, before grant")
        with patch.object(LO, "record_purchase_grant", boom):
            res = await give_reward(None, GUILD, USER, "item", amount=1,
                                    item_name=ITEM, item_type="potion",
                                    source="shop", op_id=str(op))
        self.assertFalse(res["success"], res)
        self.assertEqual(stack(), [])
        self.assertEqual(grants(), [])

    async def test_cancellation_between_increment_and_grant_leaves_no_unit(self):
        op = await open_op()
        async def cancel(*a, **k):
            raise asyncio.CancelledError()
        with patch.object(LO, "record_purchase_grant", cancel):
            with self.assertRaises(asyncio.CancelledError):
                await give_reward(None, GUILD, USER, "item", amount=1,
                                  item_name=ITEM, item_type="potion",
                                  source="shop", op_id=str(op))
        self.assertEqual(stack(), [])
        self.assertEqual(grants(), [])

    async def test_process_exit_before_commit_leaves_no_unit_and_no_grant(self):
        """A real process death between the two writes: nothing committed."""
        op = await open_op()
        # The child inherits DATABASE_PATH (set by phase1_support), so it
        # writes the same scratch file; it dies after the increment ran and
        # before the grant/commit.
        code = f'''
import asyncio, os
from utils import level_ops as LO
from utils.reward_engine import give_reward
async def main():
    async def die(*a, **k):
        os._exit(17)
    LO.record_purchase_grant = die
    await give_reward(None, {GUILD}, {USER}, "item", amount=1, item_name="{ITEM}",
                      item_type="potion", source="shop", op_id="{op}")
asyncio.run(main())
'''
        proc = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", code], env=dict(os.environ),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)
        self.assertEqual(stack(), [])
        self.assertEqual(grants(), [])

    async def test_one_operation_cannot_deliver_twice(self):
        op = await open_op()
        await deliver(op)
        res = await give_reward(None, GUILD, USER, "item", amount=1,
                                item_name=ITEM, item_type="potion",
                                source="shop", op_id=str(op))
        self.assertFalse(res["success"], res)
        self.assertIn("UNIQUE", str(res["error"]))
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(len(grants()), 1)

    async def test_concurrent_purchases_each_get_their_own_grant(self):
        ops = [await open_op() for _ in range(2)]
        results = await asyncio.gather(*(deliver(op) for op in ops))
        self.assertEqual(stack(), [(2,)])
        self.assertEqual(sorted(r["grant_id"] for r in results),
                         sorted(g[0] for g in grants()))
        self.assertEqual([g[2] for g in grants()], [1, 1])

    async def test_non_shop_item_grant_records_no_purchase_grant(self):
        res = await give_reward(None, GUILD, USER, "item", amount=1,
                                item_name=ITEM, item_type="potion",
                                source="mission")
        self.assertTrue(res["success"], res)
        self.assertIsNone(res["grant_id"])
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(grants(), [])


# ── 2. Consume order: level first (by id), then purchase (by id) ──────────

class ConsumeOrderTests(LedgerBase):
    async def test_level_grant_consumed_first_even_when_purchase_is_older(self):
        op = await open_op()
        purchase = (await deliver(op))["grant_id"]          # older (lower id)
        lvl = level_grant(claim_id=1)                       # newer
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="level_claim")
        self.assertEqual(stack(), [(2,)])
        await inventory.remove_item(GUILD, USER, ITEM, 1)
        self.assertEqual(grants(), [(purchase, "shop_purchase", 1, 0),
                                    (lvl, "level_reward", 0, 0)])

    async def test_multiple_level_grants_consumed_by_id(self):
        a = level_grant(claim_id=1, qty=2)
        b = level_grant(claim_id=2, qty=2)
        await inventory.give_item(GUILD, USER, ITEM, 4, item_type="potion",
                                  source="level_claim")
        await inventory.remove_item(GUILD, USER, ITEM, 3)
        self.assertEqual(grants(), [(a, "level_reward", 0, 0),
                                    (b, "level_reward", 1, 0)])

    async def test_multiple_purchase_grants_consumed_by_id_after_level(self):
        lvl = level_grant(claim_id=1, qty=1)
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="level_claim")
        p1 = (await deliver(await open_op()))["grant_id"]
        p2 = (await deliver(await open_op()))["grant_id"]
        self.assertEqual(stack(), [(3,)])
        await inventory.remove_item(GUILD, USER, ITEM, 2)
        self.assertEqual(grants(), [(lvl, "level_reward", 0, 0),
                                    (p1, "shop_purchase", 0, 0),
                                    (p2, "shop_purchase", 1, 0)])

    async def test_partial_consumption_crosses_from_level_to_purchase(self):
        p1 = (await deliver(await open_op()))["grant_id"]
        lvl = level_grant(claim_id=1, qty=2)
        await inventory.give_item(GUILD, USER, ITEM, 2, item_type="potion",
                                  source="level_claim")
        p2 = (await deliver(await open_op()))["grant_id"]
        # stack 4: level 2 + purchase 2. Take 3: both level units, then p1.
        await inventory.remove_item(GUILD, USER, ITEM, 3)
        self.assertEqual(grants(), [(p1, "shop_purchase", 0, 0),
                                    (lvl, "level_reward", 0, 0),
                                    (p2, "shop_purchase", 1, 0)])
        self.assertEqual(stack(), [(1,)])

    async def test_null_grant_source_rows_sort_with_level_grants(self):
        """A row that still has a NULL `grant_source` counts as a level grant
        (that is what it is); only the migration that adds the column
        backfills, see MigrationTests."""
        p1 = (await deliver(await open_op()))["grant_id"]
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
                "track,kind,item_name,quantity,remaining_quantity,created_at,"
                "grant_source) VALUES ('old-row',?,?,9,'level','inventory',?,1,1,"
                "'scratch',NULL)", (GUILD, USER, ITEM))
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="level_claim")
        await inventory.remove_item(GUILD, USER, ITEM, 1)
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"
                              " WHERE grant_id IN ('old-row',?) ORDER BY id",
                              (p1,)), [(1,), (0,)])
        # Re-running the schema check on an already-migrated table is a
        # read-only no-op: it does not rewrite rows (previews reach it too).
        LO.invalidate_tables_cache()
        await LO.ensure_tables()
        self.assertEqual(rows("SELECT grant_source FROM reward_grants"
                              " WHERE grant_id='old-row'"), [(None,)])


# ── 3. Group isolation: neither reversal reaches the other group ──────────

class GroupIsolationTests(LedgerBase):
    async def test_level_reversal_cannot_take_a_purchase_unit(self):
        lvl = level_grant(claim_id=1)
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="level_claim")
        p = (await deliver(await open_op()))["grant_id"]
        await inventory.remove_item(GUILD, USER, ITEM, 1)   # consumes lvl
        self.assertEqual(grants(), [(lvl, "level_reward", 0, 0),
                                    (p, "shop_purchase", 1, 0)])
        res = await LO.reverse_inventory(
            guild_id=GUILD, user_id=USER, claim_id=1, item_name=ITEM,
            quantity=1, operation_id="lvl-reverse-1", reason="test")
        self.assertNotEqual(res["status"], LO.STATUS_APPLIED, res)
        self.assertEqual(stack(), [(1,)], "the purchase unit was taken by a level reversal")
        self.assertEqual(grants()[1], (p, "shop_purchase", 1, 0))

    async def test_purchase_compensation_cannot_take_a_level_unit(self):
        lvl = level_grant(claim_id=1)
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="level_claim")
        op = await open_op()
        res = await deliver(op)
        # Hand-consume the PURCHASE grant only (simulating a trade of that unit
        # recorded against it), leaving the level unit in the stack.
        execute("UPDATE reward_grants SET remaining_quantity=0 WHERE grant_id=?",
                (res["grant_id"],))
        execute("UPDATE inventory_items SET quantity=1 WHERE guild_id=? AND"
                " user_id=? AND item_name=?", (GUILD, USER, ITEM))
        out = await refund_and_compensate(
            op, spec_for(op, delivered=res["delivered_inventory"]))
        self.assertFalse(out["complete"], out)
        self.assertIn(LO.REFUSAL_CONSUMED, out["error"])
        self.assertEqual(stack(), [(1,)], "the level unit was taken by a purchase rollback")
        self.assertEqual(grants()[0], (lvl, "level_reward", 1, 0))


# ── 4. Compensation: only the purchase's own grant, once ──────────────────

class CompensationTests(LedgerBase):
    async def test_overlapping_purchases_compensating_the_first_keeps_the_second(self):
        op1 = await open_op(); r1 = await deliver(op1)
        op2 = await open_op(); r2 = await deliver(op2)
        self.assertEqual(stack(), [(2,)])
        out = await refund_and_compensate(
            op1, spec_for(op1, delivered=r1["delivered_inventory"]))
        self.assertTrue(out["complete"], out)
        self.assertTrue(out["inventory_rolled_back"])
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(grants(), [(r1["grant_id"], "shop_purchase", 0, 1),
                                    (r2["grant_id"], "shop_purchase", 1, 0)])
        self.assertEqual(descriptor(op1), "")

    async def test_aba_same_second_identical_row_is_not_evidence(self):
        """The reproducer shape: old unit consumed, newer unit delivered, live
        row byte-identical to the old snapshot. The old grant is spent, so the
        rollback refuses and the newer unit stays."""
        op_old = await open_op(); r_old = await deliver(op_old)
        snapshot = tuple(r_old["delivered_inventory"])
        await inventory.remove_item(GUILD, USER, ITEM, 1)
        op_new = await open_op(); r_new = await deliver(op_new)
        execute("UPDATE inventory_items SET updated_at=? WHERE item_name=?",
                (snapshot[6], ITEM))
        live = rows("SELECT id,quantity,item_type,source,metadata,created_at,"
                    "updated_at FROM inventory_items WHERE item_name=?", (ITEM,))[0]
        self.assertEqual(live, snapshot, "fixture must reproduce the ABA shape")
        out = await refund_and_compensate(op_old, spec_for(op_old, delivered=snapshot))
        self.assertFalse(out["complete"], out)
        self.assertFalse(out["inventory_rolled_back"])
        self.assertIn(LO.REFUSAL_CONSUMED, out["error"])
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(grants(), [(r_old["grant_id"], "shop_purchase", 0, 0),
                                    (r_new["grant_id"], "shop_purchase", 1, 0)])
        self.assertNotEqual(descriptor(op_old), "")

    async def test_partial_consumption_before_compensation_refuses(self):
        op = await open_op(); r = await deliver(op)
        await inventory.remove_item(GUILD, USER, ITEM, 1)
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="mission")
        out = await refund_and_compensate(op, spec_for(op, delivered=r["delivered_inventory"]))
        self.assertFalse(out["complete"], out)
        self.assertIn(LO.REFUSAL_CONSUMED, out["error"])
        self.assertEqual(stack(), [(1,)], "a mission unit was taken for a consumed purchase")
        self.assertEqual(json.loads(descriptor(op)).get("refusal_code"),
                         LO.REFUSAL_CONSUMED)

    async def test_retry_after_successful_compensation_does_not_deduct_again(self):
        op = await open_op(); r = await deliver(op)
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="mission")
        spec = spec_for(op, delivered=r["delivered_inventory"])
        await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
        assert await TO.record_pending_delivery_rollback(op, spec, bot=None)
        real_update = TO.update_purchase
        async def clear_fails(oid, **kwargs):
            if kwargs.get("delivery_rollback") == "":
                raise RuntimeError("simulated crash before the completion sentinel")
            return await real_update(oid, **kwargs)
        with patch.object(TO, "update_purchase", clear_fails):
            first = await TO.compensate_late_delivery(op, dict(spec), bot=None)
        self.assertFalse(first["complete"], first)
        self.assertTrue(first["inventory_rolled_back"])
        self.assertEqual(stack(), [(1,)])
        # Restart-equivalent retries from the durable descriptor, and from the
        # stale in-memory spec: neither may move a second unit.
        TO._PENDING_DELIVERY_ROLLBACKS.clear()
        second = await TO.compensate_late_delivery(op, dict(spec), bot=None)
        self.assertTrue(second["complete"], second)
        self.assertEqual(stack(), [(1,)], "a second unit was deducted on retry")
        self.assertEqual(grants(), [(r["grant_id"], "shop_purchase", 0, 1)])
        self.assertEqual(descriptor(op), "")
        # And straight at the ledger: already applied, no movement.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            res = await LO.reverse_purchase_grant(
                db, operation_id=op, guild_id=GUILD, user_id=USER,
                item_name=ITEM, reversed_by="test")
            await db.commit()
        self.assertEqual(res["status"], LO.PURCHASE_REVERSAL_ALREADY, res)
        self.assertEqual(stack(), [(1,)])

    async def test_concurrent_consumption_and_compensation_move_exactly_one_unit(self):
        op = await open_op(); r = await deliver(op)
        spec = spec_for(op, delivered=r["delivered_inventory"])
        await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
        assert await TO.record_pending_delivery_rollback(op, spec, bot=None)
        TO._PENDING_DELIVERY_ROLLBACKS.clear()

        async def consume():
            try:
                await inventory.remove_item(GUILD, USER, ITEM, 1)
                return "consumed"
            except Exception as exc:  # insufficient quantity
                return f"refused:{type(exc).__name__}"

        consumed, out = await asyncio.gather(
            consume(), TO.compensate_late_delivery(op, dict(spec), bot=None))
        self.assertEqual(stack(), [(0,)], "stack must end at exactly zero")
        g = grants()[0]
        self.assertEqual(g[2], 0, "the grant must be spent exactly once")
        if out["complete"]:
            self.assertEqual(consumed[:7], "refused", (consumed, out))
            self.assertEqual(g[3], 1)
        else:
            self.assertEqual(consumed, "consumed", (consumed, out))
            self.assertIn(LO.REFUSAL_CONSUMED, out["error"])
            self.assertEqual(g[3], 0)

    async def test_identity_mismatch_user_refuses_without_writes(self):
        op = await open_op(); r = await deliver(op)
        await inventory.give_item(GUILD, OTHER_USER, ITEM, 1, item_type="potion",
                                  source="shop")
        # Runtime: the descriptor-vs-operation binding refuses first.
        out = await refund_and_compensate(
            op, spec_for(op, user=OTHER_USER, delivered=r["delivered_inventory"]))
        self.assertFalse(out["complete"], out)
        self.assertFalse(out["inventory_rolled_back"])
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(stack(user=OTHER_USER), [(1,)])
        # Ledger: the grant's own identity check refuses too, and writes nothing.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            res = await LO.reverse_purchase_grant(
                db, operation_id=op, guild_id=GUILD, user_id=OTHER_USER,
                item_name=ITEM, reversed_by="test")
            await db.commit()
        self.assertEqual((res["status"], res["code"]),
                         (LO.PURCHASE_REVERSAL_REFUSED, LO.REFUSAL_IDENTITY), res)
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(stack(user=OTHER_USER), [(1,)])
        self.assertEqual(grants(), [(r["grant_id"], "shop_purchase", 1, 0)])

    async def test_invalidated_provenance_refuses(self):
        op = await open_op(); r = await deliver(op)
        await inventory.set_quantity(GUILD, USER, ITEM, 3, item_type="potion",
                                     source="admin")
        self.assertEqual(rows("SELECT unattributed FROM reward_grants"
                              " WHERE grant_id=?", (r["grant_id"],)), [(1,)])
        out = await refund_and_compensate(op, spec_for(op, delivered=r["delivered_inventory"]))
        self.assertFalse(out["complete"], out)
        self.assertIn(LO.REFUSAL_INVALIDATED, out["error"])
        self.assertEqual(stack(), [(3,)])

    async def test_descriptor_and_ledger_commit_together(self):
        """The progress write inside the shared transaction fails: the unit
        must come back and the grant must not be marked reversed."""
        op = await open_op(); r = await deliver(op)
        spec = spec_for(op, delivered=r["delivered_inventory"])
        await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
        assert await TO.record_pending_delivery_rollback(op, spec, bot=None)
        TO._PENDING_DELIVERY_ROLLBACKS.clear()
        real_update = TO.update_purchase
        async def tx_progress_fails(oid, **kwargs):
            raw = kwargs.get("delivery_rollback")
            if kwargs.get("db") is not None and raw and \
                    json.loads(raw).get("inventory_rolled_back"):
                raise RuntimeError("simulated progress write failure inside tx")
            return await real_update(oid, **kwargs)
        with patch.object(TO, "update_purchase", tx_progress_fails):
            out = await TO.compensate_late_delivery(op, dict(spec), bot=None)
        self.assertFalse(out["complete"], out)
        self.assertEqual(stack(), [(1,)])
        self.assertEqual(grants(), [(r["grant_id"], "shop_purchase", 1, 0)])
        self.assertFalse(json.loads(descriptor(op)).get("inventory_rolled_back"))


# ── 5. Legacy policy: no grant -> pending; operator closes with a note ─────

class LegacyPolicyTests(LedgerBase):
    async def _legacy_op(self, extra_units=1):
        op = await open_op()
        await inventory.give_item(GUILD, USER, ITEM, 1, item_type="potion",
                                  source="shop")               # no grant
        snapshot = rows("SELECT id,quantity,item_type,source,metadata,created_at,"
                        "updated_at FROM inventory_items WHERE item_name=?",
                        (ITEM,))[0]
        if extra_units:
            await inventory.give_item(GUILD, USER, ITEM, extra_units,
                                      item_type="potion", source="mission")
        return op, snapshot

    async def test_legacy_purchase_stays_pending_and_removes_nothing(self):
        op, snap = await self._legacy_op()
        out = await refund_and_compensate(op, spec_for(op, delivered=snap))
        self.assertFalse(out["complete"], out)
        self.assertFalse(out["inventory_rolled_back"])
        self.assertIn(LO.REFUSAL_NO_PROVENANCE, out["error"])
        self.assertEqual(stack(), [(2,)])
        saved = json.loads(descriptor(op))
        self.assertEqual(saved.get("refusal_code"), LO.REFUSAL_NO_PROVENANCE)
        # Replays never change their mind.
        for _ in range(2):
            TO._PENDING_DELIVERY_ROLLBACKS.clear()
            rec = await TO.reconcile_purchases(bot=None)
            self.assertEqual(rec.get("completed", 0), 0, rec)
        self.assertEqual(stack(), [(2,)])
        self.assertNotEqual(descriptor(op), "")

    async def test_legacy_pending_refuses_retire_without_explicit_acceptance(self):
        op, snap = await self._legacy_op()
        await refund_and_compensate(op, spec_for(op, delivered=snap))
        ok, msg = await TO.retire_pending_rollback(
            op, confirmed=True, note="checked on Discord, nothing to do here")
        self.assertFalse(ok, msg)
        self.assertNotEqual(descriptor(op), "")
        self.assertEqual(stack(), [(2,)])

    async def test_legacy_pending_closed_by_operator_note_removes_nothing(self):
        op, snap = await self._legacy_op()
        await refund_and_compensate(op, spec_for(op, delivered=snap))
        ok, msg = await TO.retire_pending_rollback(
            op, confirmed=True, accept_unprovable=True, note="short")
        self.assertFalse(ok, msg)
        ok, msg = await TO.retire_pending_rollback(
            op, confirmed=True, accept_unprovable=True,
            note="member keeps the unit; purchase predates provenance (ticket 42)")
        self.assertTrue(ok, msg)
        self.assertEqual(descriptor(op), "")
        self.assertEqual(stack(), [(2,)], "closing a legacy case removed a unit")
        detail = rows("SELECT detail FROM shop_purchase_operations WHERE op_id=?",
                      (op,))[0][0]
        self.assertIn("WITHOUT automatic removal", detail)
        self.assertIn("ticket 42", detail)
        self.assertEqual(grants(), [], "closing fabricated provenance")

    async def test_live_grant_cannot_be_closed_by_hand(self):
        """`effects_live` is the runtime's to finish; the override must refuse."""
        op = await open_op(); r = await deliver(op)
        await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
        assert await TO.record_pending_delivery_rollback(
            op, spec_for(op, delivered=r["delivered_inventory"]), bot=None)
        verdict = await TO.rollback_retirement_evidence(op)
        self.assertFalse(verdict["safe"], verdict)
        self.assertEqual(verdict["code"], "effects_live")
        ok, msg = await TO.retire_pending_rollback(
            op, confirmed=True, accept_unprovable=True,
            note="trying to force-close a rollback the runtime can finish")
        self.assertFalse(ok, msg)
        self.assertEqual(stack(), [(1,)])

    async def test_retirement_evidence_reads_the_grant(self):
        op = await open_op(); r = await deliver(op)
        spec = spec_for(op, delivered=r["delivered_inventory"])
        out = await refund_and_compensate(op, spec)
        self.assertTrue(out["complete"], out)
        # Evidence for a descriptor that (hypothetically) survived a correct
        # rollback: the ledger says the unit came back, so it is retirable.
        verdict = await TO.rollback_retirement_evidence(
            op, op={"op_id": op, "status": TO.PURCHASE_REFUNDED,
                    "guild_id": GUILD, "user_id": USER, "item_id": 1,
                    "item_name": ITEM, "_raw_delivery_rollback": json.dumps(spec)})
        self.assertTrue(verdict["safe"], verdict)
        self.assertIn("taken back by grant", verdict["reason"])
        # And a legacy op whose item has no units left is trivially retirable.
        op2 = await open_op()
        await inventory.give_item(GUILD, USER, "Gone", 1, item_type="potion", source="shop")
        snap = rows("SELECT id,quantity,item_type,source,metadata,created_at,"
                    "updated_at FROM inventory_items WHERE item_name='Gone'")[0]
        await inventory.remove_item(GUILD, USER, "Gone", 1)
        out2 = await refund_and_compensate(op2, spec_for(op2, "Gone", delivered=snap))
        self.assertFalse(out2["complete"], out2)
        verdict2 = await TO.rollback_retirement_evidence(op2)
        self.assertTrue(verdict2["safe"], verdict2)
        ok, msg = await TO.retire_pending_rollback(
            op2, confirmed=True, note="no unit left; nothing to compensate")
        self.assertTrue(ok, msg)
        self.assertEqual(descriptor(op2), "")


# ── 6. Migration ──────────────────────────────────────────────────────────

class MigrationTests(LedgerBase):
    async def test_pre_ledger_table_is_migrated_without_touching_quantities(self):
        execute("DROP INDEX IF EXISTS idx_rg_purchase_operation")
        for col in ("grant_source", "reversed_at", "reversed_by"):
            execute(f"ALTER TABLE reward_grants DROP COLUMN {col}")
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
                "track,kind,item_name,quantity,remaining_quantity,operation_id,"
                "created_at) VALUES ('legacy-1',?,?,3,'level','inventory',?,3,2,"
                "'lvl-op-3','2026-01-01T00:00:00')", (GUILD, USER, ITEM))
        before = rows("SELECT grant_id,quantity,remaining_quantity,created_at,"
                      "operation_id FROM reward_grants")
        for _ in range(2):                      # idempotent
            LO.invalidate_tables_cache()
            await LO.ensure_tables()
        self.assertEqual(rows("SELECT grant_id,quantity,remaining_quantity,"
                              "created_at,operation_id FROM reward_grants"), before)
        self.assertEqual(rows("SELECT grant_source, reversed_at, reversed_by"
                              " FROM reward_grants"), [("level_reward", None, None)])
        self.assertEqual(rows("SELECT name FROM sqlite_master WHERE name="
                              "'idx_rg_purchase_operation'"),
                         [("idx_rg_purchase_operation",)])
        # Level operation ids may repeat; only purchase operation ids are unique.
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
                "track,kind,item_name,quantity,remaining_quantity,operation_id,"
                "created_at,grant_source) VALUES ('legacy-2',?,?,4,'level',"
                "'inventory',?,1,1,'lvl-op-3','2026-01-01T00:00:00','level_reward')",
                (GUILD, USER, ITEM))
        self.assertEqual(rows("SELECT count(*) FROM reward_grants"), [(2,)])


# ── 7. The reproducer stays green ────────────────────────────────────────

class ReproducerTests(unittest.TestCase):
    def test_reproduce_inventory_ambiguity_exits_zero(self):
        script = Path.cwd() / "docs/execution-20261030/reproduce_inventory_ambiguity.py"
        env = dict(os.environ, OWNER_ID="1",
                   PYTHONPATH=f"{Path.cwd()}:{Path.cwd() / 'scripts'}")
        proc = subprocess.run([sys.executable, str(script)], env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.assertIn("OK: no unprovable deduction", proc.stdout)


if __name__ == "__main__":
    unittest.main()
