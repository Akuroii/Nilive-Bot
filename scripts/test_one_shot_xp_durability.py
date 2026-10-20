"""A one-shot XP reward must never be lost to a correction lease.

This is the regression for the defect Akuroi exercised in production: while a
correction lease was held, `mission_engine.record_activities()` marked the
mission completed and committed, `give_reward()` timed out after its wait and
returned a *deferred failure*, and the XP was never written. Re-recording the
activity produced no second completion, so the reward was never retried - a
benefit the member had already earned simply vanished.

Recurring XP (message/voice) is deliberately NOT covered by the queue: the next
tick earns it again, so dropping it costs nothing and queueing it would only
build a backlog. A one-shot award - a mission, tag mission, minigame, event or
tag-partner grant - has already committed the work that earned it, so it is
recorded under an idempotency key and retried until it lands. That is the only
design that survives a lease outliving any fixed wait.

Deterministic: a disposable database, a lease held explicitly and released
explicitly, and a mocked Discord member. No live Discord and no production data.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase1_support import GUILD, USER, execute, rows, reset_database  # noqa: E402

FAILS = []
REWARD = 500


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def xp_of():
    found = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, USER))
    return int(found[0][0]) if found else None


def queued_rows():
    return rows("SELECT idem_key, amount, status FROM level_xp_pending_grants "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))


async def fenced(guild_id, user_id, operation_id):
    """The state a racing award really meets in production.

    A lease on its own does not stop an ordinary writer: `xp_write_barrier` only
    refuses once `fence_live_writers()` has moved the epoch, and `_apply_admin_xp`
    fences before the sweep reads. So a reproduction must fence, not just lease.
    """
    from utils import level_ops as ops
    lease = await ops.acquire_lock(guild_id, user_id, holder="setlevel",
                                   operation_id=operation_id)
    fence = await ops.fence_live_writers(guild_id, user_id, operation_id)
    return lease, fence


async def main():
    print("== 1. a mission reward outlives a lease longer than any wait ==")
    await reset_database()
    from utils import level_ops as ops
    from utils import mission_engine as me
    from utils.level_claims import ensure_tables as claims_ensure
    from utils.mission_engine import create_definition, ensure_tables

    await ensure_tables()
    await claims_ensure()
    await ops.ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,0)", (GUILD, USER, 1000, 1))

    await create_definition(GUILD, name="Audit one-shot", mtype="messages",
                            target=1, reward_type="xp",
                            reward_value=str(REWARD), period="daily")

    # 90 s TTL, i.e. far longer than give_reward()'s 3 s default wait. This is
    # the whole point: no fixed timeout can cover this, so the award must be
    # durable rather than merely patient.
    lease, fence = await fenced(GUILD, USER, "op-one-shot")
    check("setup: the correction lease is live and fenced",
          lease.get("ok") and fence.get("ok"), (lease, fence))
    held = await ops.lock_held(GUILD, USER)
    check("setup: the lease outlives the caller's wait", held is not None, held)

    completed = await me.record_activities(None, GUILD, USER, {"messages": 1})
    check("the mission really completed - the work was committed",
          len(completed) == 1, completed)
    check("the XP is NOT written while the fence is live",
          xp_of() == 1000, xp_of())
    queued = queued_rows()
    check("the award is durably QUEUED, not dropped",
          bool(queued) and queued[0][2] == "pending", queued)
    check("the queue row carries the reward amount",
          bool(queued) and queued[0][1] == REWARD, queued)

    # Reprocessing must neither re-complete nor double-pay.
    again = await me.record_activities(None, GUILD, USER, {"messages": 1})
    check("re-recording does not complete the mission a second time",
          again == [], again)
    check("re-recording while still fenced pays nothing",
          xp_of() == 1000, xp_of())

    # The correction finishes. The member's very next activity settles it.
    await ops.release_lock(GUILD, USER, "op-one-shot")
    await me.record_activities(None, GUILD, USER, {"messages": 1})
    settled = xp_of()
    check("the queued award is applied once the correction ends",
          settled == 1000 + REWARD, settled)
    check("the queue row is marked applied, not left pending",
          queued_rows() and queued_rows()[0][2] == "applied", queued_rows())

    # Idempotency: further activity must never pay it again.
    await me.record_activities(None, GUILD, USER, {"messages": 1})
    await me.record_activities(None, GUILD, USER, {"messages": 1})
    check("further activity never duplicates the award",
          xp_of() == settled, (xp_of(), settled))

    print("\n== 2. the award survives a process restart, not just activity ==")
    # A member who goes quiet must still be paid: the periodic sweep settles
    # everything left pending, including rows from before the last restart.
    await reset_database()
    await ensure_tables()
    await claims_ensure()
    await ops.ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,0)", (GUILD, USER, 1000, 1))
    await create_definition(GUILD, name="Audit quiet member", mtype="messages",
                            target=1, reward_type="xp",
                            reward_value=str(REWARD), period="daily")
    await fenced(GUILD, USER, "op-quiet")
    await me.record_activities(None, GUILD, USER, {"messages": 1})
    check("setup: the award is queued and the member goes quiet",
          xp_of() == 1000 and queued_rows(), (xp_of(), queued_rows()))
    await ops.release_lock(GUILD, USER, "op-quiet")
    # No further activity at all - only the sweep runs.
    swept = await ops.drain_xp_grants_sweep()
    check("the periodic sweep settles the queued award with no activity",
          swept.get("applied") == 1, swept)
    check("and the member has the XP", xp_of() == 1000 + REWARD, xp_of())
    idle = await ops.drain_xp_grants_sweep()
    check("a second sweep pays nothing again", idle.get("applied") == 0, idle)

    print("\n== 3. an event winner's award is keyed, so it cannot vanish ==")
    await reset_database()
    from utils.level_claims import ensure_tables as claims_ensure2
    from cogs.events import ButtonRaceView
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    await claims_ensure2()
    await ops.ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,0)", (GUILD, USER, 1000, 1))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,0,0)", (GUILD, USER))

    member = SimpleNamespace(id=USER, guild=SimpleNamespace(id=GUILD))
    view = ButtonRaceView(501, 1, "xp", "250")
    await fenced(GUILD, USER, "op-event")
    interaction = SimpleNamespace(
        user=member, guild=SimpleNamespace(id=GUILD), client=None,
        response=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
        channel=SimpleNamespace(send=AsyncMock()))
    await view.children[0].callback(interaction)
    winners = rows("SELECT user_id FROM event_winners WHERE event_id=?", (501,))
    check("the winner row is committed - the member cannot claim twice",
          winners == [(USER,)], winners)
    check("the XP is not written while fenced", xp_of() == 1000, xp_of())
    event_queued = [r for r in queued_rows() if r[0].startswith("event:")]
    check("the event award is queued under its idempotency key",
          bool(event_queued) and event_queued[0][2] == "pending", queued_rows())

    await ops.release_lock(GUILD, USER, "op-event")
    swept = await ops.drain_xp_grants_sweep()
    check("the event award is applied once the correction ends",
          swept.get("applied") == 1, swept)
    check("the member has the event XP exactly once",
          xp_of() == 1000 + 250, xp_of())
    swept2 = await ops.drain_xp_grants_sweep()
    check("the event award is never paid twice", swept2.get("applied") == 0,
          swept2)

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        return 1
    print("ALL ONE-SHOT XP DURABILITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
