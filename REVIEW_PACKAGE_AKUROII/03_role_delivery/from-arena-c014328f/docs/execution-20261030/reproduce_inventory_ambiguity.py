"""Known-red diagnostics: an old rollback must not remove later units.

Run with PYTHONPATH=. python docs/execution-20261030/reproduce_inventory_ambiguity.py
Real SQLite and production inventory/compensation functions; scratch data only.
Purchases are journal fixtures, not end-to-end Discord purchase interactions.
Exit 1 is a delivery blocker, NOT an expected passing test or a skip.
"""
import asyncio
from phase1_support import GUILD, USER, rows, reset_database, execute
from utils import inventory, timed_ownership as TO
from utils.reward_engine import rollback_item_inventory_grant


async def main():
    failures = []
    for path in ("inventory_helper", "durable_compensation"):
        for newer_qty in (2, 1):
            await reset_database()
            old_op = await TO.open_purchase(GUILD, USER, item_id=1, item_name="Same",
                                            currency="balance", amount=100)
            await inventory.give_item(GUILD, USER, "Same", item_type="custom", source="shop")
            snapshot = rows("SELECT id,quantity,item_type,source,metadata,created_at,updated_at "
                            "FROM inventory_items WHERE item_name='Same'")[0]
            await inventory.remove_item(GUILD, USER, "Same")
            newer_op = await TO.open_purchase(GUILD, USER, item_id=1, item_name="Same",
                                              currency="balance", amount=100)
            await inventory.give_item(GUILD, USER, "Same", newer_qty,
                                      item_type="custom", source="shop")
            if newer_qty == 1:
                # Same-second ABA: timestamps are not monotonic revisions.
                execute("UPDATE inventory_items SET updated_at=? WHERE item_name='Same'",
                        (snapshot[6],))
                assert rows("SELECT id,quantity,item_type,source,metadata,created_at,updated_at "
                            "FROM inventory_items WHERE item_name='Same'")[0] == snapshot
            if path == "inventory_helper":
                result = await rollback_item_inventory_grant(
                    GUILD, USER, "Same", delivered_inv=snapshot)
            else:
                spec = {"guild_id":GUILD,"user_id":USER,"item_id":1,"item_name":"Same",
                        "internal_type":"custom","delivered_inventory":snapshot}
                await TO.update_purchase(old_op, status="refunded")
                assert await TO.record_pending_delivery_rollback(old_op, spec)
                result = await TO.compensate_late_delivery(old_op, spec)
            left = rows("SELECT quantity FROM inventory_items WHERE item_name='Same'")
            print({"path":path,"old_operation":old_op,"newer_operation":newer_op,
                   "newer_units":newer_qty,"rollback_result":result,
                   "remaining":left,"safe":left==[(newer_qty,)]}, flush=True)
            if left != [(newer_qty,)]:
                failures.append((path,newer_qty))
    assert not failures, f"BLOCKER: old rollback removed newer units: {failures}"


if __name__ == "__main__":
    asyncio.run(main())
