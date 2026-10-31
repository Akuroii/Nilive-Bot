"""Diagnostics: an old rollback must never remove units it cannot prove it gave.

Run with OWNER_ID=1 PYTHONPATH=.:scripts python docs/execution-20261030/reproduce_inventory_ambiguity.py
Real SQLite and production inventory/compensation functions; scratch data only.
Purchases are journal fixtures, not end-to-end Discord purchase interactions.
Exit 1 is a delivery blocker, NOT an expected passing test or a skip.

History: on f9e413f both the `rollback_item_inventory_grant` helper and the
durable compensation path removed NEWER units (and in the same-second ABA case
the snapshot comparison could not even tell anything had changed). Since the
purchase grant ledger (G fix) the helper no longer exists and compensation
reverses only the purchase's own `reward_grants` row, so this script covers:

  * legacy     - the old purchase has no grant row (delivered before the ledger):
                 compensation must refuse (`legacy_no_provenance`), stay
                 pending, and leave every unit in place.
  * provenance - both purchases delivered through the ledger; the OLD unit was
                 consumed before the rollback, the NEWER one(s) remain. The
                 old purchase's grant is spent, so compensation must refuse
                 (`consumed_before_rollback`) and leave the newer units alone,
                 even in the same-second ABA shape where the live row is
                 byte-identical to the old snapshot.
"""
import asyncio
from phase1_support import GUILD, USER, rows, reset_database, execute
from utils import inventory, timed_ownership as TO
from utils.reward_engine import give_reward
from utils.level_ops import REFUSAL_NO_PROVENANCE, REFUSAL_CONSUMED

try:  # the snapshot-comparing helper must be gone, not merely unused
    from utils.reward_engine import rollback_item_inventory_grant  # noqa: F401
except ImportError:
    HELPER_REMOVED = True
else:
    HELPER_REMOVED = False


async def _deliver(op_id, qty, *, with_grant):
    if with_grant:
        for _ in range(qty):
            res = await give_reward(None, GUILD, USER, "item", amount=1,
                                    item_name="Same", item_type="custom",
                                    source="shop", op_id=str(op_id))
            assert res.get("success") and res.get("grant_id"), res
    else:
        await inventory.give_item(GUILD, USER, "Same", qty,
                                  item_type="custom", source="shop")


async def main():
    failures = []
    if not HELPER_REMOVED:
        failures.append(("helper_still_exists", None))
    for path in ("legacy", "provenance"):
        for newer_qty in (2, 1):
            await reset_database()
            with_grant = path == "provenance"
            old_op = await TO.open_purchase(GUILD, USER, item_id=1, item_name="Same",
                                            currency="balance", amount=100)
            await _deliver(old_op, 1, with_grant=with_grant)
            snapshot = rows("SELECT id,quantity,item_type,source,metadata,created_at,updated_at "
                            "FROM inventory_items WHERE item_name='Same'")[0]
            await inventory.remove_item(GUILD, USER, "Same")
            # One purchase operation delivers one unit (the ledger enforces one
            # grant per operation), so `newer_qty` units are `newer_qty` ops.
            newer_ops = []
            for _ in range(newer_qty):
                newer_op = await TO.open_purchase(GUILD, USER, item_id=1, item_name="Same",
                                                  currency="balance", amount=100)
                await _deliver(newer_op, 1, with_grant=with_grant)
                newer_ops.append(newer_op)
            if newer_qty == 1:
                # Same-second ABA: timestamps are not monotonic revisions.
                execute("UPDATE inventory_items SET updated_at=? WHERE item_name='Same'",
                        (snapshot[6],))
                assert rows("SELECT id,quantity,item_type,source,metadata,created_at,updated_at "
                            "FROM inventory_items WHERE item_name='Same'")[0] == snapshot
            spec = {"guild_id": GUILD, "user_id": USER, "item_id": 1, "item_name": "Same",
                    "internal_type": "custom", "delivered_inventory": snapshot}
            await TO.update_purchase(old_op, status="refunded")
            assert await TO.record_pending_delivery_rollback(old_op, spec)
            result = await TO.compensate_late_delivery(old_op, spec)
            left = rows("SELECT quantity FROM inventory_items WHERE item_name='Same'")
            pending = rows("SELECT delivery_rollback FROM shop_purchase_operations"
                           " WHERE op_id=?", (old_op,))[0][0]
            expected_code = REFUSAL_CONSUMED if with_grant else REFUSAL_NO_PROVENANCE
            safe = (left == [(newer_qty,)]
                    and not result.get("complete")
                    and not result.get("inventory_rolled_back")
                    and pending not in ("", None)
                    and expected_code in str(result.get("error")))
            print({"path": path, "old_operation": old_op, "newer_operations": newer_ops,
                   "newer_units": newer_qty, "rollback_result": result,
                   "remaining": left, "still_pending": pending not in ("", None),
                   "safe": safe}, flush=True)
            if not safe:
                failures.append((path, newer_qty))
    assert not failures, f"BLOCKER: old rollback removed newer units or misreported: {failures}"
    print("OK: no unprovable deduction on any path", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
