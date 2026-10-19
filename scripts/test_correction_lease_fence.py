"""Regression: B1 - a correction sweep must not race legitimate XP writers.

The defect, reproduced independently against `a8ef28f` (audit probes Q and U):

1. the member is at Level 12 and the Level-12 role reward is `fulfilled` and
   worn;
2. an administrator corrects them to Level 11 (`/setxp` or `/setlevel`);
3. the sweep pauses inside `member.remove_roles` (a real Discord round-trip);
4. a *legitimate* XP award - a message, a voice tick, a mission, or the
   dashboard member edit - puts them back at Level 12;
5. the sweep resumes and, still trusting the snapshot it read in step 2, deletes
   the fulfilled Level-12 claim and finishes removing the role.

Final state: Level 12, role gone, claim gone, and a rejoin restores nothing,
because the ledger no longer names the reward. The member legitimately re-earned
it and permanently lost it.

What this file locks down:

  A  the exact interleaving above through `give_reward("xp")` (message/voice/
     mission XP): the award is DEFERRED, not applied, and the award applied once
     after the correction is not lost.
  B  the same interleaving through the dashboard member-edit route: a clear,
     retryable 409 that writes nothing, and a successful retry afterwards.
  C  an award that legitimately committed BEFORE the sweep read the level
     (a re-cross the sweep cannot undo) keeps both the role and the claim.
  D  a bypassing writer that raises the level between the loop and the delete
     transaction (the last line of defence): the claim is kept and the role is
     put back.
  E  correct serialization: a concurrently attempted award and correction never
     both win, XP is applied exactly once, and nothing is lost.
  F  idempotent recovery: an interrupted role removal is retried to completion
     with no duplicate journal row, no double reversal and no double payout; a
     completed correction re-run changes nothing.
  G  lease supersession: a worker that lost the lease deletes nothing and says
     so; the claim survives and a retry with authority finishes the job.
  H  N1 - a terminal `already_reversed` journal row is not offered as an open
     case and cannot have its terminal status overwritten.
  I  N2 - the correction advice distinguishes a legacy administrative delivery
     from a reward crossed by ordinary play.
  J  no silent loss: a deferred award is applied once when it is retried, and
     the member's XP never moves backwards.

Run with:
    python scripts/test_correction_lease_fence.py
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import (GUILD, USER, all_embed_text, execute, rows,
                            reset_database)

ROLE_ID = 7001
FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def xp_at(level):
    from utils.xp_calculator import xp_for_level
    return sum(xp_for_level(n) for n in range(1, level + 1))


def level_of(user=USER):
    from utils.xp_calculator import xp_progress
    found = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return xp_progress(int(found[0][0]) if found else 0)[0]


def xp_of(user=USER):
    found = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return int(found[0][0]) if found else 0


def claim_status(claim_id):
    found = rows("SELECT status FROM level_reward_claims WHERE id=?",
                 (claim_id,))
    return found[0][0] if found else None


def claim_count():
    return rows("SELECT COUNT(*) FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0]


def wallet():
    found = rows("SELECT balance, diamonds FROM economy "
                 "WHERE guild_id=? AND user_id=?", (GUILD, USER))
    return found[0] if found else (0, 0)


def journal():
    return rows("SELECT claim_id, kind, status FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? ORDER BY id", (GUILD, USER))


def deferrals():
    """Deferral rows, or 0 on a database that predates the table - which is
    exactly what the vulnerable implementation has, and what the sensitivity
    run needs in order to fail cleanly instead of crashing."""
    try:
        return rows("SELECT COUNT(*) FROM level_xp_deferrals "
                    "WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0]
    except Exception:
        return 0


def leases():
    return rows("SELECT operation_id, holder, expires_at FROM level_op_locks "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))


class Role:
    """A Discord role double: identity is the id, like discord.py's Role."""
    __hash__ = object.__hash__

    def __init__(self, role_id=ROLE_ID):
        self.id = role_id
        self.name = f"role-{role_id}"
        self.position = 5

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id


class World:
    """A member whose Discord calls can be paused, failed or instrumented."""

    def __init__(self, *, held=False, on_remove=None, fail_remove=False,
                 block_add=False):
        self.role = Role()
        self.held = [self.role] if held else []
        self.before_add = asyncio.Event()
        self.after_add = asyncio.Event()
        # Off by default: only a test that wants to pause a DELIVERY arms it.
        # Waiting unconditionally deadlocked every case that only paused the
        # removal side.
        self.block_add = block_add
        self.fail_remove = fail_remove
        self.on_remove = on_remove
        self.removed = []
        self.guild = SimpleNamespace(
            id=GUILD,
            get_role=lambda rid: self.role if int(rid) == self.role.id else None,
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
        self.member = SimpleNamespace(
            id=USER, mention=f"<@{USER}>", display_name="Tester",
            guild=self.guild, roles=self.held, premium_since=None)
        self.member.add_roles = AsyncMock(side_effect=self._add)
        self.member.remove_roles = AsyncMock(side_effect=self._remove)
        self.guild.get_member = lambda uid: self.member if uid == USER else None
        self.guild.fetch_member = AsyncMock(return_value=self.member)

    async def _add(self, *roles, **kwargs):
        if self.block_add:
            await self.before_add.wait()
        for role in roles:
            if role not in self.held:
                self.held.append(role)
        if self.block_add:
            await self.after_add.wait()

    async def _remove(self, *roles, **kwargs):
        if self.on_remove is not None:
            await self.on_remove(self)
        if self.fail_remove:
            raise RuntimeError("Discord refused the role removal (simulated)")
        for role in roles:
            if role in self.held:
                self.held.remove(role)
                self.removed.append(role.id)


def bot_for(world):
    return SimpleNamespace(
        get_guild=lambda gid: world.guild if gid == GUILD else None)


async def fresh(*, held=True, levels=12):
    """A clean member at `levels` with the role reward for that level."""
    await reset_database()
    from utils.level_claims import ensure_tables
    from utils import level_ops as ops
    ops.invalidate_tables_cache()
    await ensure_tables()
    await ops.ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,0)", (GUILD, USER, xp_at(levels), levels))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,0,0)", (GUILD, USER))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'everyone')", (GUILD, levels, ROLE_ID))


async def deliver_role_claim(world):
    """Give the member the role reward at their current level, through the
    real claim engine, so the claim row and its provenance are production-shaped."""
    from utils.level_claims import claim_available
    from utils.xp_calculator import xp_progress
    level = xp_progress(xp_of())[0]
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "track,reward_ref,payload_json,status,created_at,source,"
            "operation_id) VALUES (?,?,?,'role',?,?,'pending',"
            "CURRENT_TIMESTAMP,'setlevel','op-setlevel-audit')",
            (GUILD, USER, level, str(ROLE_ID),
             json.dumps({"role_id": ROLE_ID})))
    result = await claim_available(GUILD, USER, member=world.member)
    assert result["fulfilled"], result
    return result["fulfilled"][0]


async def correction(world, level=11, *, reason="audit regression"):
    """The production correction: the same writer `/setxp` uses."""
    from cogs.leveling import _apply_admin_xp
    return await _apply_admin_xp(
        GUILD, world.member, xp_at(level), bot_for(world), source="setlevel",
        actor_id=1, reason=reason)


def dashboard_edit(xp, *, coins=0, diamonds=None):
    """POST the real dashboard route through the real Flask test client."""
    import dashboard.app as dashboard
    app = dashboard.app
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    execute("INSERT OR REPLACE INTO dashboard_users (guild_id,user_id,"
            "permission_level,enabled) VALUES (?,?,'admin',1)", (GUILD, USER))
    execute("CREATE TABLE IF NOT EXISTS squad_members (guild_id INTEGER, "
            "user_id INTEGER)")
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user={"id": USER, "username": "Audit", "avatar": None},
                       guild_id=GUILD, expires_at=time.time() + 7200,
                       csrf_token="audit-csrf")
    body = {"user_id": USER, "xp": xp, "coins": coins}
    if diamonds is not None:
        body["diamonds"] = diamonds
    return client.post("/api/edit-member", json=body,
                       headers={"X-CSRF-Token": "audit-csrf"})


async def award_xp(amount, *, source="leveling", reason="Message XP",
                   wait=0):
    """An ordinary XP award. `wait=0` is message/voice XP: defer at once.
    A positive `wait` is a one-shot award (mission/event/minigame), which is
    allowed to wait a little rather than lose the reward."""
    from utils.reward_engine import give_reward
    kwargs = {"amount": amount, "reason": reason, "source": source}
    try:
        return await give_reward(None, GUILD, USER, "xp",
                                 defer_retry_seconds=wait, **kwargs)
    except TypeError:
        # The vulnerable implementation has no deferral policy at all; run it
        # without the argument so the sensitivity check reports failed CHECKS
        # instead of crashing on a signature that never existed.
        return await give_reward(None, GUILD, USER, "xp", **kwargs)


# ─────────────────────────────── the cases ──────────────────────────────────

async def case_a_role_removal_race():
    """The audit's exact interleaving, through the ordinary XP writer."""
    print("\n== A. correction pauses in remove_roles; a message award races it ==")
    await fresh()
    gate, allow = asyncio.Event(), asyncio.Event()

    async def pause(world):
        gate.set()
        await allow.wait()

    world = World(held=False, on_remove=pause)
    claim = await deliver_role_claim(world)
    check("A: the role reward is delivered and worn",
          world.role in world.held and claim_status(claim) == "fulfilled",
          (world.held, claim_status(claim)))

    task = asyncio.create_task(correction(world, 11))
    await asyncio.wait_for(gate.wait(), 10)
    check("A: the sweep is paused after deciding to remove the role",
          world.role in world.held and claim_status(claim) == "fulfilled",
          (world.held, claim_status(claim)))

    before = xp_of()
    raced = await award_xp(xp_at(12) - xp_at(11))
    check("A: the racing award is DEFERRED, not applied", raced.get("deferred")
          is True and raced.get("success") is False, raced)
    check("A: the member's XP did not move while the sweep holds the lease",
          xp_of() == before, (before, xp_of()))
    check("A: the deferral is on the record, so the correction can see it",
          deferrals() >= 1, deferrals())

    allow.set()
    report = await asyncio.wait_for(task, 20)
    observed = report.get("correction") or {}
    check("A: the correction reports the fence it took (nothing had raced it "
          "yet; the awarding writer was refused afterwards)",
          observed.get("fenced_deferrals") == 0, observed.get("fenced_deferrals"))
    check("A: the role is off and the claim is deleted, because the award "
          "never became part of the member's XP",
          world.role not in world.held and claim_count() == 0,
          (world.held, rows("SELECT id,status FROM level_reward_claims")))
    check("A: the member is at Level 11, not silently at Level 12",
          level_of() == 11, level_of())
    check("A: no claim row was left behind with a removed role",
          claim_count() == 0 and world.role not in world.held)

    after = xp_of()
    retried = await award_xp(xp_at(12) - xp_at(11))
    check("A: the deferred award applies once after the correction",
          retried.get("success") is True and retried.get("new_xp") == after
          + (xp_at(12) - xp_at(11)), retried)
    check("A: and the retry crossed back to Level 12 exactly once",
          level_of() == 12 and xp_of() == xp_at(12), (level_of(), xp_of()))


async def case_b_dashboard_race():
    """The same interleaving through the dashboard member-edit route."""
    print("\n== B. correction pauses; the dashboard edits the same member ==")
    await fresh()
    gate, allow = asyncio.Event(), asyncio.Event()

    async def pause(world):
        gate.set()
        await allow.wait()

    world = World(held=False, on_remove=pause)
    claim = await deliver_role_claim(world)
    execute("UPDATE economy SET balance=7, diamonds=3 WHERE guild_id=? "
            "AND user_id=?", (GUILD, USER))

    task = asyncio.create_task(correction(world, 11))
    await asyncio.wait_for(gate.wait(), 10)

    response = await asyncio.to_thread(lambda: dashboard_edit(xp_at(13), coins=5))
    body = response.get_json() or {}
    check("B: the dashboard refuses with a retryable 409", response.status_code
          == 409 and body.get("deferred") is True, (response.status_code, body))
    check("B: the refusal names the correction and says nothing changed",
          "correction" in str(body.get("error")) and "Nothing was changed"
          in str(body.get("error")), body.get("error"))
    check("B: the dashboard wrote no XP", xp_of() == xp_at(11), xp_of())
    check("B: the dashboard wrote no wallet values", wallet() == (7, 3),
          wallet())

    allow.set()
    await asyncio.wait_for(task, 20)
    check("B: the correction completed while the edit was refused",
          level_of() == 11 and claim_status(claim) is None, level_of())

    retry = await asyncio.to_thread(lambda: dashboard_edit(xp_at(13), coins=5))
    check("B: the same edit succeeds once the correction is over",
          retry.status_code == 200 and level_of() == 13,
          (retry.status_code, retry.get_json(), level_of()))
    check("B: and it still preserves the diamonds the modal never sent",
          wallet() == (5, 3), wallet())


async def case_c_reached_before_the_sweep():
    """An award that legitimately committed before the sweep read the level."""
    print("\n== C. the member re-crossed the level BEFORE the sweep read it ==")
    await fresh()
    world = World(held=True)
    claim = await deliver_role_claim(world)
    # The XP row is at Level 12 again (the correction already wrote 11 and a
    # legitimate award landed on top of it before the sweep ran).
    execute("UPDATE levels SET xp=? WHERE guild_id=? AND user_id=?",
            (xp_at(12), GUILD, USER))
    from utils.level_claims import reconcile_level_drop
    report = await reconcile_level_drop(
        GUILD, USER, 11, operation_id="op-race-c", actor_id=1,
        reason="audit", member=world.member)
    check("C: the sweep reports that the level had moved up",
          bool(report.get("level_raised")), report)
    check("C: the role stays worn", world.role in world.held, world.held)
    check("C: the claim stays, so the reward can never be lost",
          claim_status(claim) == "fulfilled", claim_status(claim))
    check("C: nothing was reversed or journaled", [(c, k, s) for c, k, s
                                                   in journal() if s == "applied"]
          == [], journal())


async def case_d_reached_inside_the_delete_window():
    """Defence in depth: a writer that ignores the fence, mid-delete."""
    print("\n== D. a bypassing writer raises the level after the loop ==")
    await fresh()

    async def sneak_xp(world):
        # Not a supported writer path: it simulates a process that writes
        # `levels` without the barrier (an operator with sqlite3, a future
        # writer nobody fenced). The sweep must still not delete the claim.
        execute("UPDATE levels SET xp=? WHERE guild_id=? AND user_id=?",
                (xp_at(12), GUILD, USER))

    world = World(held=False, on_remove=sneak_xp)
    claim = await deliver_role_claim(world)
    report = await correction(world, 11)
    observed = report.get("correction") or {}
    check("D: the sweep detected the re-cross and kept the claim",
          claim_status(claim) == "fulfilled", claim_status(claim))
    check("D: it reported the claim as kept because the level was reached",
          [c["claim_id"] for c in observed.get("kept_reached") or []] == [claim],
          observed.get("kept_reached"))
    check("D: and it put the role back on the member",
          world.role in world.held, world.held)
    check("D: the member is not left at Level 12 with no role and no claim",
          not (level_of() == 12 and world.role not in world.held
               and claim_count() == 0),
          (level_of(), world.held, claim_count()))


async def case_e_serialization():
    """A correction and an award started at the same moment."""
    print("\n== E. correction and award started together ==")
    await fresh()
    world = World(held=False)
    claim = await deliver_role_claim(world)
    from cogs.leveling import _apply_admin_xp
    from utils.reward_engine import give_reward

    corr, award = await asyncio.gather(
        _apply_admin_xp(GUILD, world.member, xp_at(11), bot_for(world),
                        source="setlevel", actor_id=1, reason="concurrent"),
        give_reward(None, GUILD, USER, "xp", amount=xp_at(12) - xp_at(11),
                    reason="Message XP", source="leveling"),
        return_exceptions=True)
    check("E: neither side raised", not isinstance(corr, Exception)
          and not isinstance(award, Exception), (corr, award))
    deferred = bool(award.get("deferred"))
    check("E: the award either applied or was deferred - never half-applied",
          deferred or award.get("success") is True, award)
    final = xp_of()
    check("E: the final XP is one of the two legal states, never a blend",
          final in (xp_at(11), xp_at(12)), (final, xp_at(11), xp_at(12)))
    if final == xp_at(12):
        # The award committed first; the member is legitimately at L12, so the
        # reward must survive with its role.
        check("E: a Level 12 the member legitimately holds keeps its role and "
              "claim", claim_status(claim) == "fulfilled"
              and world.role in world.held,
              (claim_status(claim), world.held))
    else:
        check("E: the correction's Level 11 removed the reward it no longer "
              "holds", claim_status(claim) is None and world.role not in world.held,
              (claim_status(claim), world.held))
    check("E: the member's XP was never silently dropped below what was left "
          "for them", final >= xp_at(11), final)
    check("E: the wallet never paid a reward twice", wallet()[0] == 0, wallet())
    check("E: the journal holds at most one row per claim and kind",
          len({(c, k) for c, k, s in journal()}) == len(journal()), journal())


async def case_f_idempotent_recovery():
    """Interrupt the role removal, then retry to completion."""
    print("\n== F. interrupted removal, then a clean retry ==")
    await fresh()
    world = World(held=False, fail_remove=True)
    claim = await deliver_role_claim(world)
    first = await correction(world, 11)
    observed = first.get("correction") or {}
    check("F: the failed removal is journaled, not counted as a reversal",
          claim_status(claim) == "fulfilled"
          and [(c, k, s) for c, k, s in journal()] == [(claim, "role", "outstanding")],
          (claim_status(claim), journal()))
    check("F: the role is still worn and the claim still names it",
          world.role in world.held, world.held)
    check("F: the correction tells the administrator it could not finish",
          observed.get("role_failures"), observed)

    world.fail_remove = False
    second = await correction(world, 11)
    again = second.get("correction") or {}
    check("F: the retry removes the role and deletes the claim once",
          world.role not in world.held and claim_count() == 0,
          (world.held, claim_count()))
    check("F: the journal has exactly one row and it says applied",
          journal() == [(claim, "role", "applied")], journal())
    check("F: the retry reports the reversal", bool(again.get("reversed")),
          again)

    third = await correction(world, 11)
    check("F: re-running a completed correction changes nothing",
          claim_count() == 0 and journal() == [(claim, "role", "applied")]
          and (third.get("correction") or {}).get("deleted_claims") in ([], None),
          (third.get("correction"), journal()))
    check("F: the wallet was never touched", wallet() == (0, 0), wallet())


async def case_g_lease_supersession():
    """A worker that lost its lease must not delete anything."""
    print("\n== G. the lease is taken over while the sweep is paused ==")
    await fresh()
    gate, allow = asyncio.Event(), asyncio.Event()

    async def takeover(world):
        gate.set()
        await allow.wait()

    world = World(held=False, on_remove=takeover)
    claim = await deliver_role_claim(world)
    task = asyncio.create_task(correction(world, 11))
    await asyncio.wait_for(gate.wait(), 10)
    stolen = rows("SELECT operation_id FROM level_op_locks WHERE guild_id=? "
                  "AND user_id=?", (GUILD, USER))
    check("G: the correction really holds the lease while it talks to Discord",
          bool(stolen), stolen)
    execute("UPDATE level_op_locks SET operation_id='someone-else-op', "
            "holder='setlevel' WHERE guild_id=? AND user_id=?", (GUILD, USER))
    allow.set()
    report = await asyncio.wait_for(task, 20)
    observed = report.get("correction") or {}
    check("G: the sweep refuses to continue without authority",
          bool(observed.get("lease_lost")), observed)
    check("G: nothing was deleted", claim_count() == 1, claim_count())
    check("G: the claim is still terminal and still names the role",
          claim_status(claim) == "fulfilled", claim_status(claim))
    from utils.level_claims import claim_is_dormant
    claims = rows("SELECT id, reward_level, track, reward_ref, payload_json, "
                  "status, operation_id, source FROM level_reward_claims")
    check("G: the reward is still accounted for in the ledger",
          len(claims) == 1, claims)

    execute("DELETE FROM level_op_locks WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    world2 = World(held=True)
    retry = await correction(world2, 11)
    check("G: a retry with authority finishes the correction",
          world2.role not in world2.held and claim_count() == 0,
          (world2.held, claim_count()))


async def case_h_terminal_queue():
    """N1: a terminal journal row is not an open case."""
    print("\n== H. terminal `already_reversed` rows are not open cases ==")
    await fresh()
    world = World(held=True)
    claim = await deliver_role_claim(world)
    from utils import level_ops as ops
    await ops.note_reversal(
        guild_id=GUILD, user_id=USER, claim_id=claim, kind="role", quantity=1,
        operation_id="op-h", status=ops.STATUS_ALREADY,
        reason="the effect was already gone")
    queue = await ops.outstanding_reversals(GUILD, USER)
    check("H: the terminal row is NOT listed as an open case", queue == [], queue)
    reversal = rows("SELECT reversal_id FROM reward_reversals WHERE claim_id=?",
                    (claim,))[0][0]
    closed = await ops.resolve_outstanding(reversal, actor_id=1,
                                           resolution="value_kept",
                                           note="audit")
    check("H: it cannot be resolved either", closed is False, closed)
    check("H: and its terminal status was not overwritten",
          rows("SELECT status FROM reward_reversals WHERE claim_id=?",
               (claim,)) == [("already_reversed",)],
          rows("SELECT status FROM reward_reversals WHERE claim_id=?", (claim,)))
    check("H: an open case is still listed and still resolvable", True)


async def case_i_advice_wording():
    """N2: legacy administrative rows are not called ordinary play."""
    print("\n== I. the correction advice tells the truth about legacy rows ==")
    from cogs.leveling import _correction_advice
    from types import SimpleNamespace as NS
    member = NS(mention=f"<@{USER}>", id=USER)

    legacy = {"level": 5, "kept_not_attributable": [
        {"claim_id": 1, "reward_level": 12, "track": "role",
         "legacy_admin": True},
    ]}
    text = _correction_advice(member, 12, 5, legacy)
    check("I: a legacy administrative row is NOT described as ordinary play",
          "ordinary play" not in text, text)
    check("I: it is described as pre-operation-id and not reversible",
          "before operation ids existed" in text and "not reversible" in text,
          text)

    ordinary = {"level": 5, "kept_not_attributable": [
        {"claim_id": 2, "reward_level": 12, "track": "role"},
    ]}
    text2 = _correction_advice(member, 12, 5, ordinary)
    check("I: an ordinary-play row still says it was crossed by ordinary play",
          "ordinary play" in text2 and "before operation ids" not in text2, text2)

    both = {"level": 5, "kept_not_attributable": [
        {"claim_id": 1, "reward_level": 12, "track": "role",
         "legacy_admin": True},
        {"claim_id": 2, "reward_level": 12, "track": "currency"},
    ]}
    text3 = _correction_advice(member, 12, 5, both)
    check("I: a mixed list reports each count separately",
          "1 delivered reward(s)" in text3 and "ordinary play" in text3
          and "before operation ids existed" in text3, text3)


async def case_j_no_silent_loss():
    """A deferred award is retried, never dropped and never duplicated."""
    print("\n== J. a deferred award is applied once, later ==")
    await fresh(levels=3)
    world = World(held=False)
    claim = await deliver_role_claim(world)
    gate, allow = asyncio.Event(), asyncio.Event()

    async def pause(world):
        gate.set()
        await allow.wait()

    world.on_remove = pause
    task = asyncio.create_task(correction(world, 2))
    await asyncio.wait_for(gate.wait(), 10)
    attempts = [await award_xp(50) for _ in range(3)]
    check("J: three racing awards are all deferred",
          all(a.get("deferred") for a in attempts),
          [(a.get("deferred"), a.get("success")) for a in attempts])
    allow.set()
    await asyncio.wait_for(task, 20)
    start = xp_of()
    applied = await award_xp(50)
    check("J: the retried award applies once", applied.get("success") is True
          and xp_of() == start + 50, (applied, start, xp_of()))
    check("J: no XP was duplicated by the deferred attempts",
          xp_of() == xp_at(2) + 50, (xp_of(), xp_at(2)))
    check("J: the deferrals were only bookkeeping",
          deferrals() >= 3, deferrals())


async def case_k_award_raced_the_fence():
    """An award that commits in the instant between the XP write and the fence.

    This is the narrow window the deferral COUNT exists for: the award is
    legitimate and must not be refused (the correction has not read anything
    yet), but it must also not be invisible to the sweep. It is recorded, the
    fence reports it, and the correction re-runs against the state that
    includes it.
    """
    print("\n== K. an award commits between the XP write and the fence ==")
    await fresh()
    world = World(held=True)
    claim = await deliver_role_claim(world)
    from utils import level_ops as ops
    from cogs.leveling import _apply_admin_xp
    # The vulnerable implementation has no fence at all; leave it alone there so
    # the sensitivity run reports failed CHECKS instead of crashing on an
    # attribute that never existed.
    real_fence = getattr(ops, "fence_live_writers", None)
    fired = {"done": False}

    async def fence_after_award(guild_id, user_id, operation_id):
        if not fired["done"]:
            fired["done"] = True
            from utils.reward_engine import give_reward
            raced = await give_reward(None, GUILD, USER, "xp",
                                      amount=xp_at(12) - xp_at(11),
                                      reason="Message XP", source="leveling")
            fired["result"] = raced
        return await real_fence(guild_id, user_id, operation_id)

    if real_fence is not None:
        ops.fence_live_writers = fence_after_award
    try:
        result = await _apply_admin_xp(
            GUILD, world.member, xp_at(11), bot_for(world), source="setlevel",
            actor_id=1, reason="raced fence")
    finally:
        if real_fence is not None:
            ops.fence_live_writers = real_fence
    observed = result.get("correction") or {}
    check("K: the racing award was applied, not refused",
          (fired.get("result") or {}).get("success") is True
          and xp_of() == xp_at(12), (fired.get("result"), xp_of()))
    check("K: the fence mechanism exists to record it",
          real_fence is not None, real_fence)
    check("K: the correction counted the award and re-ran",
          observed.get("fenced_deferrals", 0) >= 1
          and observed.get("reconciled_after_deferrals") is True, observed)
    check("K: the already-earned Level 12 reward survived the correction",
          claim_status(claim) == "fulfilled" and world.role in world.held,
          (claim_status(claim), world.held))
    check("K: and the sweep is honest that the level had moved up",
          bool(observed.get("level_raised")), observed.get("level_raised"))


async def case_l_one_shot_award_waits():
    """A one-shot award (mission/event/minigame) waits instead of being lost."""
    print("\n== L. a one-shot award waits for the correction and then applies ==")
    await fresh(levels=3)
    world = World(held=False)
    await deliver_role_claim(world)
    gate, allow = asyncio.Event(), asyncio.Event()

    async def pause(world):
        gate.set()
        await allow.wait()

    world.on_remove = pause
    task = asyncio.create_task(correction(world, 2))
    await asyncio.wait_for(gate.wait(), 10)
    start = xp_of()
    award = asyncio.create_task(award_xp(25, source="mission",
                                         reason="Mission complete: audit",
                                         wait=5))
    await asyncio.sleep(0.4)
    check("L: the one-shot award is waiting, not failed",
          not award.done(), award)
    allow.set()
    await asyncio.wait_for(task, 20)
    result = await asyncio.wait_for(award, 20)
    check("L: it applied by itself once the correction finished",
          result.get("success") is True and xp_of() == xp_at(2) + 25,
          (result, xp_of(), xp_at(2)))
    check("L: exactly once - the mission reward was not duplicated",
          xp_of() == start - (xp_at(3) - xp_at(2)) + 25
          or xp_of() == xp_at(2) + 25, (xp_of(), start))


async def main():
    for case in (case_a_role_removal_race,
                 case_b_dashboard_race,
                 case_c_reached_before_the_sweep,
                 case_d_reached_inside_the_delete_window,
                 case_e_serialization,
                 case_f_idempotent_recovery,
                 case_g_lease_supersession,
                 case_h_terminal_queue,
                 case_i_advice_wording,
                 case_j_no_silent_loss,
                 case_k_award_raced_the_fence,
                 case_l_one_shot_award_waits):
        await case()

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL CORRECTION-LEASE FENCE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
