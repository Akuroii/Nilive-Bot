"""Reward provenance, the reversal journal and the cross-process operation lease.

Locks down `utils/level_ops.py` and the hooks that feed it - every inventory
mutation path, `utils/ledger.py`, the claim engine and both XP-writing front
ends - against a real SQLite file. These are the mechanisms the scoped reset,
`/xp_rollback` and `/level_reconcile` all rely on, so they are tested on their own
terms and against the failure modes that produced the original defects:

  A  a retry removed inventory a second time;
  B  value was removed from a stack that provably belonged to another source;
  C  two claims were each judged against the whole stack;
  D  a currency reversal could debit twice when it died before marking itself;
  E  one movement produced two ledger rows, so summing double-counted it;
  F  a claim could be paid while a correction was mid-flight (two processes over
     one file, which no in-memory lock can coordinate);
  G  an XP change left no record of what it caused, including from the dashboard.

Run with:
    python scripts/test_level_ops_provenance.py
"""
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database, member

OTHER = USER + 500
COINS = 250
ITEM = "Potion"

FAILS = []


def check(label, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(label)
        if detail:
            print("        " + str(detail)[:900])


def wallet(user=USER, guild=GUILD):
    found = rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
                 (guild, user))
    return found[0] if found else (0, 0)


def stack(user=USER, guild=GUILD, item=ITEM):
    found = rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
                 "AND item_name=?", (guild, user, item))
    return found[0][0] if found else 0


def ledger_total(user=USER, guild=GUILD):
    return rows("SELECT COALESCE(SUM(amount), 0) FROM transaction_ledger "
                "WHERE guild_id=? AND user_id=?", (guild, user))[0][0]


def ledger_rows_for(user=USER, guild=GUILD):
    return rows("SELECT id, currency, amount, type, reason, source, reversed "
                "FROM transaction_ledger WHERE guild_id=? AND user_id=? ORDER BY id",
                (guild, user))


def grants(user=USER, guild=GUILD):
    return rows("SELECT claim_id, kind, item_name, quantity, remaining_quantity, "
                "unattributed, unattributed_reason, ledger_id FROM reward_grants "
                "WHERE guild_id=? AND user_id=? ORDER BY id", (guild, user))


def journal(user=USER, guild=GUILD):
    return rows("SELECT claim_id, kind, status, quantity, reason FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? ORDER BY id", (guild, user))


def leases(user=USER, guild=GUILD):
    return rows("SELECT operation_id, holder, expires_at FROM level_op_locks "
                "WHERE guild_id=? AND user_id=?", (guild, user))


async def main():
    await reset_database()
    from utils import level_ops as ops
    from utils import inventory
    from utils import ledger as ledger_mod
    from utils.level_claims import claim_available, ensure_tables
    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    os.environ["NERO_ENVIRONMENT"] = "production"
    os.environ["ALLOW_LEVEL_TEST_RESET"] = "1"

    # ── 1. the lease coordinates processes, not threads (defect F) ───────────
    print("== 1. the operation lease ==")
    operation = ops.new_operation_id("level_test_reset")
    first = await ops.acquire_lock(GUILD, USER, holder="level_test_reset",
                                   operation_id=operation)
    check("the first acquire takes the lease", first["ok"] is True, str(first))
    check("and it is a row another process can read",
          leases() and leases()[0][0] == operation
          and leases()[0][1] == "level_test_reset", str(leases()))
    second = await ops.acquire_lock(GUILD, USER, holder="xp_rollback",
                                    operation_id=ops.new_operation_id("xp_rollback"))
    check("a second acquire is refused while the lease is live",
          second["ok"] is False and operation in str(second["reason"])
          and "level_test_reset" in str(second["reason"]), str(second))
    check("the refused acquire did not steal or shorten the lease",
          leases()[0][0] == operation, str(leases()))
    held = await ops.lock_held(GUILD, USER)
    check("lock_held names the live holder",
          held and held["operation_id"] == operation
          and held["holder"] == "level_test_reset", str(held))
    check("another member is not blocked by this member's lease",
          (await ops.acquire_lock(GUILD, OTHER, holder="level_test_reset",
                                  operation_id=ops.new_operation_id("x")))["ok"]
          is True, str(leases(OTHER)))
    await ops.release_lock(GUILD, OTHER, operation)      # wrong operation id
    check("a lease can only be released by the operation that holds it",
          leases(OTHER) and leases(OTHER)[0][1] == "level_test_reset",
          str(leases(OTHER)))

    # The claim engine must refuse to pay while the lease is held: that window is
    # how a reward used to be delivered and then deleted by the correction.
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,"
            "audience) VALUES (?,?, 'balance', ?, 'everyone')", (GUILD, 3, COINS))
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,1200,3)",
            (GUILD, USER))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, USER))
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
            "reward_ref,payload_json,status,created_at,source) VALUES "
            "(?,?,3,'currency','balance',?,'pending','2026-01-01T00:00:00+00:00',"
            "'level_up')",
            (GUILD, USER, json.dumps({"currency": "balance", "amount": COINS})))
    blocked = await claim_available(GUILD, USER, member=member())
    check("Claim All during a correction is blocked and pays nothing (defect F)",
          bool(blocked.get("blocked")) and blocked["fulfilled"] == []
          and blocked["blocked_by_operation"] == operation
          and wallet() == (0, 0), str(blocked))
    check("and the claim row is still pending, so nothing was reserved or lost",
          rows("SELECT status FROM level_reward_claims WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [("pending",)],
          str(rows("SELECT * FROM level_reward_claims WHERE user_id=?", (USER,))))
    check("the block message tells the member what is happening",
          "running for this member" in str(blocked.get("blocked")),
          str(blocked.get("blocked")))

    renewed = await ops.renew_lock(GUILD, USER, operation)
    check("the holder can renew its own lease", renewed is True, str(renewed))
    check("a stranger cannot renew it",
          await ops.renew_lock(GUILD, USER, "someone-else") is False)
    released = await ops.release_lock(GUILD, USER, operation)
    check("the holder releases its lease", released is True, str(released))
    check("releasing twice is a no-op, not an error",
          await ops.release_lock(GUILD, USER, operation) is False)
    check("and the row is gone", leases() == [], str(leases()))

    paid = await claim_available(GUILD, USER, member=member())
    check("Claim All after the correction pays the reward once",
          len(paid["fulfilled"]) == 1 and wallet() == (COINS, 0), str(paid))
    claim_id = rows("SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                    (GUILD, USER))[0][0]
    check("the delivery recorded what it granted, in the claim's own transaction "
          "(defect G)",
          grants() and grants()[0][0] == claim_id and grants()[0][1] == "currency"
          and grants()[0][3] == COINS and grants()[0][7] is not None,
          str(grants()))

    # An abandoned lease must not wedge the member forever.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    execute("INSERT INTO level_op_locks (guild_id,user_id,operation_id,holder,"
            "acquired_at,expires_at) VALUES (?,?, 'level_test_reset-dead','dead',"
            "?,?)", (GUILD, USER, stale, stale))
    check("an expired lease is not reported as held",
          await ops.lock_held(GUILD, USER) is None, str(leases()))
    stolen = await ops.acquire_lock(GUILD, USER, holder="level_test_reset",
                                    operation_id=ops.new_operation_id("y"))
    check("an expired lease is replaced, so a crashed run cannot wedge the member",
          stolen["ok"] is True and leases()[0][1] == "level_test_reset"
          and len(leases()) == 1, str(leases()))
    await ops.release_lock(GUILD, USER, leases()[0][0])

    # Two claim passes racing for the same rows: the loser delivers nothing.
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
            "reward_ref,payload_json,status,created_at,source) VALUES "
            "(?,?,5,'currency','balance',?,'pending','2026-01-01T00:00:00+00:00',"
            "'level_up')",
            (GUILD, USER, json.dumps({"currency": "balance", "amount": 40})))
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,"
            "audience) VALUES (?,?, 'balance', 40, 'everyone')", (GUILD, 5))
    before_race = wallet()
    raced = await asyncio.gather(claim_available(GUILD, USER, member=member()),
                                 claim_available(GUILD, USER, member=member()))
    delivered = sum(len(r["fulfilled"]) for r in raced)
    check("two concurrent Claim All passes pay the reward once between them",
          delivered == 1 and wallet()[0] == before_race[0] + 40,
          str([(r["fulfilled"], r.get("failed")) for r in raced]))
    check("and wrote exactly one grant row for it",
          len([g for g in grants() if g[3] == 40]) == 1, str(grants()))

    # ── 2. provenance follows the units through every mutation path ──────────
    print("== 2. provenance through inventory mutations ==")
    import aiosqlite
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        await inventory.give_item(GUILD, OTHER, ITEM, 5, item_type="potion",
                                  source="level_claim", db=db)
        await ops.record_grant(db, guild_id=GUILD, user_id=OTHER, claim_id=9001,
                               reward_level=4, track="shop", kind=ops.KIND_INVENTORY,
                               quantity=5, item_name=ITEM,
                               operation_id="op-earlier")
        await db.commit()
    check("a grant starts fully attributable",
          grants(OTHER)[0][4] == 5 and grants(OTHER)[0][5] == 0, str(grants(OTHER)))
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        await ops.record_grant(db, guild_id=GUILD, user_id=OTHER, claim_id=9002,
                               reward_level=6, track="shop", kind=ops.KIND_INVENTORY,
                               quantity=3, item_name=ITEM, operation_id="op-later")
        await db.commit()
    await inventory.give_item(GUILD, OTHER, ITEM, 3, item_type="potion",
                              source="level_claim")
    check("two rewards of the same item keep separate grants (defect C)",
          len(grants(OTHER)) == 2 and [g[4] for g in grants(OTHER)] == [5, 3]
          and stack(OTHER) == 8, str(grants(OTHER)))

    await inventory.remove_item(GUILD, OTHER, ITEM, 2)
    check("using the item consumes the OLDEST grant first, FIFO",
          [g[4] for g in grants(OTHER)] == [3, 3] and stack(OTHER) == 6,
          str(grants(OTHER)))
    dropped = await inventory.drop_item(GUILD, OTHER, ITEM)
    check("dropping the stack consumes every remaining attributable unit",
          dropped.get("success") is not False
          and [g[4] for g in grants(OTHER)] == [0, 0], str([dropped, grants(OTHER)]))

    # An absolute quantity write cannot say which units it replaced, so it must
    # give up on provenance rather than leave a number nobody can trust.
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        await ops.record_grant(db, guild_id=GUILD, user_id=OTHER, claim_id=9003,
                               reward_level=7, track="shop", kind=ops.KIND_INVENTORY,
                               quantity=4, item_name=ITEM, operation_id="op-third")
        await db.commit()
    await inventory.give_item(GUILD, OTHER, ITEM, 4, item_type="potion",
                              source="level_claim")
    await inventory.set_quantity(GUILD, OTHER, ITEM, 9, item_type="potion",
                                 source="admin")
    invalidated = grants(OTHER)[-1]
    check("an absolute set_quantity write invalidates provenance instead of "
          "guessing",
          invalidated[5] == 1 and invalidated[6] and stack(OTHER) == 9,
          str(invalidated))
    outcome = await ops.reverse_inventory(guild_id=GUILD, user_id=OTHER,
                                          claim_id=9003, item_name=ITEM, quantity=4,
                                          operation_id="op-rev", reason="test")
    check("and a later reversal of that grant is REFUSED, moving nothing",
          outcome["status"] == ops.STATUS_REFUSED and stack(OTHER) == 9
          and "provenance was invalidated" in str(outcome.get("reason")),
          str([outcome, stack(OTHER)]))

    # A trade moves units out of the sender's stack, so it must move them out of
    # the sender's grants too - otherwise a later reset would take the receiver's
    # or a quest's items to "reverse" a reward that is already gone.
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        await inventory.give_item(GUILD, OTHER, ITEM, 6, item_type="potion",
                                  source="level_claim", db=db)
        await ops.record_grant(db, guild_id=GUILD, user_id=OTHER, claim_id=9004,
                               reward_level=8, track="shop", kind=ops.KIND_INVENTORY,
                               quantity=6, item_name=ITEM, operation_id="op-trade")
        await db.commit()
    from utils.trade_engine import _move_items
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _move_items(db, GUILD, OTHER, USER, {ITEM: 4})
        await db.commit()
    check("a trade consumes the sender's attributable units (defect B)",
          [g[4] for g in grants(OTHER) if g[0] == 9004] == [2]
          and stack(OTHER) == 11, str(grants(OTHER)))
    check("and gives the receiver no provenance it did not earn",
          [g for g in grants(USER) if g[2] == ITEM] == [], str(grants(USER)))

    # ── 3. the reversal primitives are idempotent and refuse honestly ────────
    print("== 3. reversal primitives ==")
    ledger_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,"
        "balance_after,type,reason,source) VALUES (?,?, 'balance', ?, ?, 'credit',"
        "'Level 3 reward','level_claim')", (GUILD, OTHER, COINS, COINS))
    execute("INSERT OR REPLACE INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,?,0)", (GUILD, OTHER, COINS + 10))
    before = ledger_rows_for(OTHER)
    first_try = await ops.reverse_currency(guild_id=GUILD, user_id=OTHER,
                                           ledger_id=ledger_id, amount=COINS,
                                           currency="balance", claim_id=9101,
                                           operation_id="op-1", reversed_by=7,
                                           reason="test reversal")
    check("a proven currency reversal moves the wallet once",
          first_try["status"] == ops.STATUS_APPLIED
          and wallet(OTHER)[0] == 10 and first_try["new_balance"] == 10,
          str([first_try, wallet(OTHER)]))
    added = ledger_rows_for(OTHER)[len(before):]
    check("and appends exactly ONE compensating row (defect E)",
          len(added) == 1 and added[0][3] == "reversal" and added[0][2] == -COINS
          and added[0][5] == "level_reward_reversal", str(added))
    check("the original row is marked reversed and kept (append-only)",
          ledger_rows_for(OTHER)[0][6] == 1 and ledger_rows_for(OTHER)[0][2] == COINS,
          str(ledger_rows_for(OTHER)[0]))
    check("the journal row committed with the movement it describes",
          [j for j in journal(OTHER) if j[0] == 9101]
          == [(9101, "currency", "applied", COINS, None)], str(journal(OTHER)))
    retry = await ops.reverse_currency(guild_id=GUILD, user_id=OTHER,
                                       ledger_id=ledger_id, amount=COINS,
                                       currency="balance", claim_id=9101,
                                       operation_id="op-2", reversed_by=7,
                                       reason="retry")
    check("a second attempt is already_reversed and moves nothing (defect D)",
          retry["status"] == ops.STATUS_ALREADY and wallet(OTHER)[0] == 10
          and len(ledger_rows_for(OTHER)) == len(before) + 1
          and len([j for j in journal(OTHER) if j[0] == 9101]) == 1,
          str([retry, wallet(OTHER), len(ledger_rows_for(OTHER))]))
    check("and it names the operation that got there first",
          "op-1" in str(retry.get("reason")), str(retry))

    # A crash between the wallet write and the journal write is impossible inside
    # one transaction; prove it by killing the process AFTER the commit and
    # before the caller could record anything else.
    spent_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,"
        "balance_after,type,reason,source) VALUES (?,?, 'balance', 500, 500, "
        "'credit','Level 9 reward','level_claim')", (GUILD, OTHER))
    spent = await ops.reverse_currency(guild_id=GUILD, user_id=OTHER,
                                       ledger_id=spent_id, amount=500,
                                       currency="balance", claim_id=9102,
                                       operation_id="op-3", reversed_by=7,
                                       reason="spent money")
    check("currency that has been spent is outstanding, and the wallet is "
          "never forced negative",
          spent["status"] == ops.STATUS_OUTSTANDING and wallet(OTHER)[0] == 10
          and spent["held"] == 10, str([spent, wallet(OTHER)]))
    check("the original row stays UNreversed, so the case is still actionable",
          [r[6] for r in ledger_rows_for(OTHER) if r[0] == spent_id] == [0],
          str([r for r in ledger_rows_for(OTHER) if r[0] == spent_id]))
    check("and the case is on the queue with its reason",
          [j[2] for j in journal(OTHER) if j[0] == 9102] == ["outstanding"]
          and "spent" in [j[4] for j in journal(OTHER) if j[0] == 9102][0],
          str(journal(OTHER)))
    missing = await ops.reverse_currency(guild_id=GUILD, user_id=OTHER,
                                         ledger_id=424242, amount=5,
                                         currency="balance", claim_id=9103,
                                         operation_id="op-4", reversed_by=7,
                                         reason="missing row")
    check("a ledger row that does not exist is refused, not invented",
          missing["status"] == ops.STATUS_REFUSED and wallet(OTHER)[0] == 10,
          str(missing))
    queue = await ops.outstanding_reversals(GUILD, OTHER)
    # 9003 is the invalidated-provenance refusal from section 2: the queue is
    # every open case for this member, not just the ones this section made.
    check("outstanding_reversals lists the open cases only",
          sorted(q["claim_id"] for q in queue) == [9003, 9102, 9103]
          and all(q["status"] != ops.STATUS_APPLIED for q in queue)
          and [q["claim_id"] for q in queue
               if q["status"] == ops.STATUS_APPLIED] == [], str(queue))
    closed = await ops.resolve_outstanding(
        [q for q in queue if q["claim_id"] == 9102][0]["reversal_id"],
        actor_id=7, resolution="value_kept", note="admin decided to leave it")
    check("an administrator can close a case, on the record", closed is True)
    check("closing it never rewrites the row to 'applied' and never deletes it",
          [j[2] for j in journal(OTHER) if j[0] == 9102] == ["resolved:value_kept"]
          and "admin decided to leave it" in [j[4] for j in journal(OTHER)
                                              if j[0] == 9102][0]
          and len(journal(OTHER)) == 4, str(journal(OTHER)))
    check("and an applied reversal cannot be 'resolved' away",
          await ops.resolve_outstanding(
              [q for q in await ops.outstanding_reversals(GUILD, OTHER)][0]
              ["reversal_id"], actor_id=7, resolution="effect_gone", note="x")
          is True)

    # Inventory reversal needs provenance covering the WHOLE granted quantity.
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        await ops.record_grant(db, guild_id=GUILD, user_id=OTHER, claim_id=9104,
                               reward_level=4, track="shop", kind=ops.KIND_INVENTORY,
                               quantity=2, item_name=ITEM, operation_id="op-5")
        await db.commit()
    no_provenance = await ops.reverse_inventory(
        guild_id=GUILD, user_id=OTHER, claim_id=9105, item_name=ITEM, quantity=2,
        operation_id="op-6", reason="no grant row")
    check("an item reversal with no grant row is refused and removes nothing "
          "(defect B)",
          no_provenance["status"] == ops.STATUS_REFUSED
          and "no provenance record" in str(no_provenance.get("reason"))
          and stack(OTHER) == 11, str([no_provenance, stack(OTHER)]))
    part = await ops.reverse_inventory(guild_id=GUILD, user_id=OTHER, claim_id=9104,
                                       item_name=ITEM, quantity=5,
                                       operation_id="op-7", reason="over-claim")
    check("asking for more than the attributable remainder is outstanding, and "
          "removes nothing (defect C)",
          part["status"] == ops.STATUS_OUTSTANDING and stack(OTHER) == 11
          and "granted 2 unit(s) and 2 are still attributable" in str(part.get("reason"))
          and "5 asked for" in str(part.get("reason")), str(part))
    check("and the message names what was granted, what is left and what was asked",
          all(str(n) in str(part.get("reason")) for n in (2, 5)), str(part.get("reason")))
    ok_inv = await ops.reverse_inventory(guild_id=GUILD, user_id=OTHER,
                                         claim_id=9104, item_name=ITEM, quantity=2,
                                         operation_id="op-8", reason="exact")
    check("a refusal does not permanently block a later provable reversal",
          ok_inv["status"] == ops.STATUS_APPLIED and stack(OTHER) == 9
          and ok_inv["remaining"] == 9          # the stack left after the removal
          and [g[4] for g in grants(OTHER) if g[0] == 9104] == [0],
          str([ok_inv, stack(OTHER), grants(OTHER)]))
    check("the later attempt UPDATED the one journal row, keeping the earlier "
          "refusal as history",
          len([j for j in journal(OTHER) if j[0] == 9104]) == 1
          and [j[2] for j in journal(OTHER) if j[0] == 9104] == ["applied"]
          and "fewer than the 5 asked for" in [j[4] for j in journal(OTHER)
                                               if j[0] == 9104][0]
          and "op-8" in [j[4] for j in journal(OTHER) if j[0] == 9104][0],
          str([j for j in journal(OTHER) if j[0] == 9104]))
    again_inv = await ops.reverse_inventory(guild_id=GUILD, user_id=OTHER,
                                            claim_id=9104, item_name=ITEM,
                                            quantity=2, operation_id="op-9",
                                            reason="retry")
    check("and a retry after value moved removes nothing a second time (defect A)",
          again_inv["status"] == ops.STATUS_ALREADY and stack(OTHER) == 9
          and len([j for j in journal(OTHER) if j[0] == 9104]) == 1
          and "op-8" in str(again_inv.get("reason")),
          str([again_inv, stack(OTHER), journal(OTHER)]))
    noted = await ops.note_reversal(guild_id=GUILD, user_id=OTHER, claim_id=9106,
                                    kind=ops.KIND_ROLE, quantity=1,
                                    operation_id="op-10", status=ops.STATUS_APPLIED,
                                    role_id=601, reason="role removed")
    check("a role removal is journaled without moving any value",
          noted is True and [j[1:3] for j in journal(OTHER) if j[0] == 9106]
          == [("role", "applied")], str(journal(OTHER)))
    check("and journaling it twice adds no second row",
          await ops.note_reversal(guild_id=GUILD, user_id=OTHER, claim_id=9106,
                                  kind=ops.KIND_ROLE, quantity=1,
                                  operation_id="op-11", status=ops.STATUS_APPLIED,
                                  role_id=601, reason="again") is False
          and len([j for j in journal(OTHER) if j[0] == 9106]) == 1)

    # ── 4. utils.ledger.reverse_transaction is atomic and single-row ─────────
    print("== 4. the shared ledger reversal ==")
    credit_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,"
        "balance_after,type,reason,source) VALUES (?,?, 'balance', 300, 300, "
        "'credit','Level 7 reward','level_claim')", (GUILD, USER))
    execute("UPDATE economy SET balance=300 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    before = ledger_rows_for(USER)
    result = await ledger_mod.reverse_transaction(credit_id, GUILD, reversed_by=7,
                                                  reason="dashboard undo")
    check("the shared reversal debits the wallet and reports the new balance",
          result["success"] is True and result["new_balance"] == 0
          and wallet(USER)[0] == 0, str([result, wallet(USER)]))
    check("it appends exactly ONE compensating row (defect E)",
          len(ledger_rows_for(USER)) == len(before) + 1
          and ledger_rows_for(USER)[-1][3] == "reversal"
          and ledger_rows_for(USER)[-1][2] == -300,
          str(ledger_rows_for(USER)[len(before):]))
    second = await ledger_mod.reverse_transaction(credit_id, GUILD, reversed_by=7)
    check("a second reversal of the same row is refused and moves nothing "
          "(defect D)",
          second["success"] is False and second["error"] == "Already reversed"
          and wallet(USER)[0] == 0
          and len(ledger_rows_for(USER)) == len(before) + 1, str(second))
    race_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,"
        "balance_after,type,reason,source) VALUES (?,?, 'balance', 60, 60, "
        "'credit','Level 4 reward','level_claim')", (GUILD, USER))
    execute("UPDATE economy SET balance=300 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    raced = await asyncio.gather(
        ledger_mod.reverse_transaction(race_id, GUILD, reversed_by=7),
        ledger_mod.reverse_transaction(race_id, GUILD, reversed_by=8))
    check("two processes reversing one row: exactly one wins (defect F)",
          len([r for r in raced if r.get("success")]) == 1
          and len([r for r in raced if r.get("error") == "Already reversed"]) == 1
          and wallet(USER)[0] == 240, str([raced, wallet(USER)]))
    check("and the winner wrote exactly one compensating row",
          len([r for r in ledger_rows_for(USER) if r[3] == "reversal"
               and r[2] == -60]) == 1,
          str([r for r in ledger_rows_for(USER) if r[3] == "reversal"]))
    execute("UPDATE economy SET balance=300, diamonds=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    debit_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,"
        "balance_after,type,reason,source) VALUES (?,?, 'balance', -120, 180, "
        "'deduct','shop purchase','shop')", (GUILD, USER))
    before = ledger_rows_for(USER)
    undone = await ledger_mod.reverse_transaction(debit_id, GUILD, reversed_by=7)
    check("reversing a deduct credits the money back, in one row",
          undone["success"] is True and wallet(USER)[0] == 420
          and len(ledger_rows_for(USER)) == len(before) + 1
          and ledger_rows_for(USER)[-1][2] == 120, str([undone, wallet(USER)]))
    execute("UPDATE economy SET balance=5 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    broke_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,"
        "balance_after,type,reason,source) VALUES (?,?, 'balance', 900, 905, "
        "'credit','Level 12 reward','level_claim')", (GUILD, USER))
    broke = await ledger_mod.reverse_transaction(broke_id, GUILD, reversed_by=7)
    check("a spent credit cannot be reversed, and the row stays actionable",
          broke["success"] is False and "enough balance" in broke["error"]
          and wallet(USER)[0] == 5
          and [r[6] for r in ledger_rows_for(USER) if r[0] == broke_id] == [0],
          str([broke, wallet(USER)]))
    check("the ledger total moved by exactly the reversals, never twice",
          ledger_total(USER) == sum(r[2] for r in ledger_rows_for(USER)),
          str(ledger_total(USER)))

    # ── 5. every XP mutation path records its operation (defect G) ───────────
    print("== 5. XP mutation provenance ==")
    from cogs.leveling import Leveling, _apply_admin_xp, level_start_xp
    from utils.level_claims import SOURCE_SETLEVEL, SOURCE_SETXP
    execute("INSERT OR IGNORE INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
            (GUILD, OTHER))
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,"
            "audience) VALUES (?,?, 'balance', 77, 'everyone')", (GUILD, 5))
    # A reward level nothing has crossed yet, so the dashboard edit below creates
    # a NEW entitlement that has to carry ITS operation id.
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,"
            "audience) VALUES (?,?, 'balance', 99, 'everyone')", (GUILD, 9))
    result = await _apply_admin_xp(GUILD, member(user=OTHER), level_start_xp(5),
                                   SimpleNamespace(get_guild=lambda gid: None),
                                   source=SOURCE_SETLEVEL, actor_id=42,
                                   reason="support ticket #123")
    mutation = await ops.get_xp_mutation(result["operation_id"])
    check("/setlevel's XP write records an immutable operation row",
          mutation is not None and mutation["source"] == SOURCE_SETLEVEL
          and mutation["actor_id"] == 42 and mutation["reason"] == "support ticket #123"
          and mutation["xp_after"] == level_start_xp(5)
          and mutation["delta"] == level_start_xp(5)
          and mutation["rolled_back"] is False, str(mutation))
    check("and records the reward levels it newly entitled",
          mutation["crossings"] == [3, 5] or mutation["crossings"] == [5]
          or set(mutation["crossings"]) <= {3, 5}, str(mutation["crossings"]))
    check("the entitlements it created carry its operation id",
          rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=? "
               "AND operation_id=?", (GUILD, OTHER, result["operation_id"]))[0][0] >= 1,
          str(rows("SELECT reward_level, track, operation_id FROM level_reward_claims "
                   "WHERE user_id=?", (OTHER,))))
    listed = await ops.list_xp_mutations(GUILD, OTHER)
    check("the member's changes are listed newest first",
          listed and listed[0]["operation_id"] == result["operation_id"], str(listed))
    await ops.mark_rolled_back(result["operation_id"])
    check("marking it rolled back is visible to the next reader",
          (await ops.get_xp_mutation(result["operation_id"]))["rolled_back"] is True)

    # The dashboard is a separate process over the same file, with its own XP
    # write. It must record provenance too, or an over-grant made there could
    # never be rolled back exactly.
    import dashboard.app as dash
    from dashboard import permissions
    import dashboard.auth as dash_auth
    import flask
    real_valid = dash_auth.is_session_valid
    real_refresh = dash_auth.refresh_session_if_needed
    real_level = permissions._get_permission_level
    dash_auth.is_session_valid = lambda: True
    dash_auth.refresh_session_if_needed = lambda: None

    async def owner(*args, **kwargs):
        return "owner"
    permissions._get_permission_level = owner
    try:
        with dash.app.test_request_context(
                "/api/edit-member", method="POST",
                json={"user_id": OTHER, "xp": level_start_xp(9), "coins": 15,
                      "diamonds": 2}):
            flask.session["guild_id"] = GUILD
            flask.session["user"] = {"id": 777, "username": "owner"}
            flask.session["user_level"] = "owner"
            response = dash.api_edit_member()
        payload = json.loads(response.get_data())
    finally:
        dash_auth.is_session_valid = real_valid
        dash_auth.refresh_session_if_needed = real_refresh
        permissions._get_permission_level = real_level
    check("the dashboard member edit succeeds", payload.get("success") is True,
          str(payload))
    dash_mutations = [m for m in await ops.list_xp_mutations(GUILD, OTHER)
                      if m["source"] == "dashboard"]
    check("and writes an operation record naming the logged-in administrator "
          "(defect G)",
          len(dash_mutations) == 1 and dash_mutations[0]["actor_id"] == 777
          and dash_mutations[0]["xp_after"] == level_start_xp(9)
          and "dashboard member edit" in (dash_mutations[0]["reason"] or ""),
          str(dash_mutations))
    check("the dashboard's entitlements carry its operation id too",
          rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=? "
               "AND operation_id=?",
               (GUILD, OTHER, dash_mutations[0]["operation_id"]))[0][0] >= 1
          and 9 in [r[0] for r in rows(
              "SELECT reward_level FROM level_reward_claims WHERE guild_id=? "
              "AND user_id=? AND operation_id=?",
              (GUILD, OTHER, dash_mutations[0]["operation_id"]))],
          str(rows("SELECT reward_level, track, operation_id FROM level_reward_claims "
                   "WHERE user_id=?", (OTHER,))))
    check("the wallet overwrite is captured in the record, not left to inference",
          "15" in (dash_mutations[0]["reason"] or "")
          and "coins" in (dash_mutations[0]["reason"] or ""),
          str(dash_mutations[0]["reason"]))
    check("no XP change anywhere is left unrecorded",
          rows("SELECT COUNT(*) FROM xp_mutations WHERE guild_id=?", (GUILD,))[0][0]
          == len(await ops.list_xp_mutations(GUILD, OTHER, limit=50))
          + len(await ops.list_xp_mutations(GUILD, USER, limit=50)),
          str(rows("SELECT source, COUNT(*) FROM xp_mutations GROUP BY source")))

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL PROVENANCE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
