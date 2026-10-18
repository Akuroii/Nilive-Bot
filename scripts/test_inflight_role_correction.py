"""Regression: a reward role delivered across a reset, then a level correction.

The defect this locks down (found in review of bd0f2d2):

1. an administrative grant creates a role claim;
2. a Claim All reserves it and its Discord add is still in flight;
3. `/level_test_reset` runs while the role is NOT on the member yet, kept the
   `processing` claim, but recorded the role as removed (`applied`) anyway;
4. the delivery finishes: the role is added and the claim becomes `fulfilled`;
5. a later `/setxp` correction saw the stale `applied` journal row, trusted it,
   deleted the claim - and left the role on the member with no record naming it.

The reset must not record a removal it did not perform, and the correction must
re-check the member instead of trusting the journal alone. The contract asserted
here is the one that matters: a worn reward role is never left without a claim
record that names it, and a reset never calls the result a clean New Game while
that is the state.

Cases:
  A  Akuroi's sequence: role absent during the reset, then delivered.
  B  the role was already added before the reset inspected it.
  C  a stale `applied` row with the role still worn and the removal failing.
  D  the role is shared by another claim that is NOT being reversed.
  E  two stale rows for the same role, both above the corrected level.
  F  a stale `applied` row and the role already gone.
  G  a stale `applied` row with no member object (old behaviour, no guessing).
  H  the reset's own role removal fails while the role is worn.

Run with:
    python scripts/test_inflight_role_correction.py
"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database

ROLE_ID = 7001
FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


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
    """A member whose Discord calls can be paused or made to fail."""

    def __init__(self, *, held=False, fail_remove=False):
        self.role = Role()
        self.held = [self.role] if held else []
        self.before_add = asyncio.Event()
        self.after_add = asyncio.Event()
        self.fail_remove = fail_remove
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
        await self.before_add.wait()
        for role in roles:
            if role not in self.held:
                self.held.append(role)
        await self.after_add.wait()

    async def _remove(self, *roles, **kwargs):
        if self.fail_remove:
            raise RuntimeError("Discord refused the role removal (simulated)")
        for role in roles:
            if role in self.held:
                self.held.remove(role)
                self.removed.append(role.id)


async def reset_db():
    await reset_database()
    from utils.level_claims import ensure_tables
    from utils.xp_calculator import xp_for_level
    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,10,0)",
            (GUILD, USER, sum(xp_for_level(n) for n in range(1, 11))))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,0,0)", (GUILD, USER))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,10,?,'everyone')", (GUILD, ROLE_ID))


def seed_claim(status="pending", *, level=10, ref=str(ROLE_ID),
               operation="setlevel-regression", source="setlevel",
               payload=None):
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "track,reward_ref,payload_json,status,source,operation_id,"
            "created_at) VALUES (?,?,?,'role',?,?,?,?,?,CURRENT_TIMESTAMP)",
            (GUILD, USER, level, ref, payload or f'{{"role_id": {ROLE_ID}}}',
             status, source, operation))
    return rows("SELECT id FROM level_reward_claims ORDER BY id DESC")[0][0]


def claim_status(claim_id):
    found = rows("SELECT status FROM level_reward_claims WHERE id=?", (claim_id,))
    return found[0][0] if found else None


def claim_count():
    return rows("SELECT COUNT(*) FROM level_reward_claims")[0][0]


def journal(claim_id):
    return rows("SELECT status, reason FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? AND claim_id=? AND kind='role'",
                (GUILD, USER, claim_id))


async def mark_applied(claim_id):
    from utils import level_ops as ops
    await ops.note_reversal(
        guild_id=GUILD, user_id=USER, claim_id=claim_id, kind=ops.KIND_ROLE,
        quantity=1, operation_id="old-reset", status=ops.STATUS_APPLIED,
        role_id=ROLE_ID, reason="reward role removed from the member")


async def wait_processing(claim_id, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if claim_status(claim_id) == "processing":
            return True
        await asyncio.sleep(0.05)
    return False


async def wait_held(world, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if world.role in world.held:
            return True
        await asyncio.sleep(0.02)
    return False


def start_delivery(world, claim_id):
    from utils.level_claims import claim_available
    return asyncio.create_task(
        claim_available(GUILD, USER, member=world.member, bot=None,
                        claim_ids=[claim_id]))


async def reset_member_apply(world):
    from utils.level_test_reset import reset_member
    return await reset_member(GUILD, USER, apply=True, allow_production=True,
                              reversed_by=999, reason="regression",
                              member=world.member)


async def reconcile(level, world, operation="correction-regression"):
    from utils.level_claims import reconcile_level_drop
    execute("UPDATE levels SET xp=0, level=? WHERE guild_id=? AND user_id=?",
            (level, GUILD, USER))
    return await reconcile_level_drop(GUILD, USER, level,
                                      operation_id=operation,
                                      actor_id=999, reason="regression",
                                      member=world.member)


async def case_a_role_absent_during_reset():
    """Akuroi's exact sequence: the reset runs before the add reaches Discord."""
    print("\n== A. the role is absent while the reset runs, then it is delivered ==")
    await reset_db()
    claim_id = seed_claim("pending")
    world = World()
    task = start_delivery(world, claim_id)
    check("A: the claim really reached the in-flight state",
          await wait_processing(claim_id), claim_status(claim_id))
    check("A: the role is not on the member yet", world.role not in world.held,
          world.held)

    report = await reset_member_apply(world)
    check("A: the reset does NOT report the role as removed",
          (report.get("roles_removed") or []) == [],
          report.get("roles_removed"))
    check("A: the reset does NOT say it removed a Discord role",
          not any("removed Discord role" in line for line in report["ran"]),
          report["ran"])
    check("A: the reset does NOT journal the role as applied",
          journal(claim_id) == [], journal(claim_id))
    deferred = report.get("roles_deferred") or []
    check("A: the role is reported as deferred, not removed",
          [d["role_id"] for d in deferred] == [ROLE_ID]
          and "still being delivered" in deferred[0]["reason"],
          deferred)
    check("A: the report tells the admin the role was NOT removed",
          any("was NOT removed" in line and "still being delivered" in line
              for line in report.get("could_not") or []),
          report.get("could_not"))
    check("A: the claim is kept and this is not a clean New Game",
          claim_status(claim_id) == "processing"
          and report["new_game_complete"] is False, report["traces"])

    world.before_add.set()
    world.after_add.set()
    delivered = await task
    check("A: the delivery finishes and the role is worn",
          delivered["fulfilled"] == [claim_id] and world.role in world.held,
          (delivered, world.held))
    check("A: the claim is fulfilled and still names the worn role",
          claim_status(claim_id) == "fulfilled" and claim_count() == 1)

    correction = await reconcile(5, world)
    check("A: the correction removes the role",
          world.role not in world.held, world.held)
    check("A: the correction deletes the claim it just reversed",
          [c["claim_id"] for c in correction["deleted_claims"]] == [claim_id]
          and claim_count() == 0, correction["deleted_claims"])
    check("A: the reversal is reported, not silently claimed",
          len(correction["reversed"]["role"]) == 1,
          correction["reversed"]["role"])
    check("A: no worn reward role is left without a claim record",
          not (world.role in world.held and claim_count() == 0))
    check("A: the journal row finally written is truthful (applied, role gone)",
          journal(claim_id)[:1] == [("applied",
                                     "reward role removed from the member / "
                                     "already absent")],
          journal(claim_id))

    from utils.level_test_reset import inspect_member
    seen = await inspect_member(GUILD, USER)
    check("A: a later inspection sees neither the role nor a hidden record",
          seen["claims"] == [] and seen["role_ids"] == []
          and seen["plan"]["remove_discord_roles"] == [])


async def case_b_role_present_during_reset():
    """The add already landed, but the row is still `processing`."""
    print("\n== B. the role is already worn when the reset runs ==")
    await reset_db()
    claim_id = seed_claim("pending")
    world = World()
    task = start_delivery(world, claim_id)
    check("B: the claim reached the in-flight state",
          await wait_processing(claim_id), claim_status(claim_id))
    world.before_add.set()
    check("B: the Discord add landed", await wait_held(world), world.held)

    report = await reset_member_apply(world)
    check("B: the reset really removed the role",
          report["roles_removed"] == [ROLE_ID] and world.role not in world.held,
          (report["roles_removed"], world.held))
    check("B: this time the applied journal row is truthful",
          journal(claim_id)[:1] == [("applied",
                                     "reward role removed from the member")],
          journal(claim_id))
    check("B: nothing was deferred and the claim is kept",
          (report.get("roles_deferred") or []) == []
          and claim_status(claim_id) == "processing"
          and report["new_game_complete"] is False)

    world.after_add.set()
    await task
    check("B: the delivery still marks the claim fulfilled", 
          claim_status(claim_id) == "fulfilled")
    correction = await reconcile(5, world)
    check("B: the correction deletes the claim and no role is worn",
          world.role not in world.held and claim_count() == 0,
          (world.held, claim_count()))
    check("B: no orphan state is reachable",
          not (world.role in world.held and claim_count() == 0))
    # The role was already off the member (the reset removed it truthfully), so
    # this correction deletes the claim without claiming a second removal.
    check("B: the correction does not invent a second role reversal",
          correction["reversed"]["role"] == []
          and [c["claim_id"] for c in correction["deleted_claims"]] == [claim_id],
          (correction["reversed"]["role"], correction["deleted_claims"]))
    check("B: the journal still holds exactly the truthful reset row",
          journal(claim_id)[:1] == [("applied",
                                     "reward role removed from the member")],
          journal(claim_id))


async def case_c_stale_applied_and_removal_fails():
    """A terminal `applied` row contradicts a worn role; removal fails."""
    print("\n== C. stale applied + worn role + Discord refuses the removal ==")
    await reset_db()
    claim_id = seed_claim("fulfilled")
    world = World(held=True, fail_remove=True)
    await mark_applied(claim_id)

    correction = await reconcile(5, world)
    check("C: the role is still worn", world.role in world.held)
    check("C: the claim is NOT deleted while the role is live",
          claim_status(claim_id) == "fulfilled" and claim_count() == 1,
          claim_status(claim_id))
    check("C: the failure is reported as an unresolved case",
          [e["claim_id"] for e in correction["kept_unresolved"]] == [claim_id]
          and "Discord refused" in correction["kept_unresolved"][0]["reason"],
          correction["kept_unresolved"])
    check("C: the failure is reported per role too",
          [f["role_id"] for f in correction["role_failures"]] == [ROLE_ID],
          correction["role_failures"])
    check("C: the terminal journal row is left untouched (documented limit)",
          journal(claim_id)[0][0] == "applied", journal(claim_id))

    from utils import level_ops as ops
    open_cases = await ops.outstanding_reversals(GUILD, USER)
    check("C: because the row is terminal it is not listed as an open case "
          "(retry is the correction itself / a reset, see the retry below)",
          [c for c in open_cases if c["claim_id"] == claim_id] == [], open_cases)

    world.fail_remove = False
    retry = await reconcile(5, world, operation="correction-retry")
    check("C: re-running the correction retries the removal and succeeds",
          world.role not in world.held, world.held)
    check("C: and only then is the claim deleted",
          [c["claim_id"] for c in retry["deleted_claims"]] == [claim_id]
          and claim_count() == 0, retry["deleted_claims"])
    check("C: the retry reports the reversal",
          len(retry["reversed"]["role"]) == 1, retry["reversed"]["role"])

    from utils.level_test_reset import inspect_member
    seen = await inspect_member(GUILD, USER)
    check("C: the documented limit is that /level_reconcile cannot see it, but "
          "the claim stays inspectable until a retry/reset collects it",
          seen["claims"] == [] and seen["role_ids"] == [])


async def case_d_shared_role_kept():
    """Another claim (below the corrected level) still justifies the role."""
    print("\n== D. the role is shared by a claim that is not being reversed ==")
    await reset_db()
    stale = seed_claim("fulfilled", level=10, operation="admin-10")
    await mark_applied(stale)
    keeper = seed_claim("fulfilled", level=5, operation="admin-5")
    world = World(held=True)

    correction = await reconcile(5, world)
    check("D: the stale row is closed without touching the shared role",
          [c["claim_id"] for c in correction["deleted_claims"]] == [stale],
          correction["deleted_claims"])
    check("D: the role stays on the member, still justified by the other claim",
          world.role in world.held, world.held)
    check("D: the claim that names the worn role survives",
          claim_status(keeper) == "fulfilled" and claim_count() == 1,
          claim_count())
    check("D: therefore no worn role is left undocumented",
          world.role in world.held and claim_count() == 1)


async def case_e_two_stale_rows_same_role():
    """Two stale rows must not justify each other into an orphan."""
    print("\n== E. two stale rows for the same role, both above the level ==")
    await reset_db()
    first = seed_claim("fulfilled", level=10, operation="admin-a")
    second = seed_claim("fulfilled", level=12, operation="admin-b",
                        payload=f'{{"role_id": {ROLE_ID}}}')
    await mark_applied(first)
    await mark_applied(second)
    world = World(held=True)

    correction = await reconcile(5, world)
    check("E: the role is removed, not left wearing two failed records",
          world.role not in world.held, world.held)
    check("E: both claims are deleted because the effect really is gone",
          claim_count() == 0
          and {c["claim_id"] for c in correction["deleted_claims"]} == {first, second},
          (claim_count(), correction["deleted_claims"]))
    check("E: the end state has no worn role without a record",
          not (world.role in world.held and claim_count() == 0))


async def case_f_stale_applied_role_gone():
    """The recorded outcome matches the member: no removal is attempted."""
    print("\n== F. stale applied row and the role is already gone ==")
    await reset_db()
    claim_id = seed_claim("fulfilled", operation="admin-f")
    world = World(held=False)
    await mark_applied(claim_id)

    correction = await reconcile(5, world)
    check("F: the claim is deleted", claim_count() == 0)
    check("F: no Discord removal was attempted",
          world.member.remove_roles.await_count == 0,
          world.member.remove_roles.await_count)
    check("F: the claim is closed with no unresolved case",
          correction["kept_unresolved"] == []
          and [c["claim_id"] for c in correction["deleted_claims"]] == [claim_id],
          (correction["reversed"]["role"], correction["kept_unresolved"]))
    check("F: no second removal is reported for an effect already gone",
          correction["reversed"]["role"] == [],
          correction["reversed"]["role"])


async def case_g_stale_applied_no_member():
    """No member to check: the settled row is left exactly as it was."""
    print("\n== G. stale applied row with no member object ==")
    await reset_db()
    claim_id = seed_claim("fulfilled", operation="admin-g")
    await mark_applied(claim_id)
    from utils.level_claims import reconcile_level_drop
    execute("UPDATE levels SET xp=0, level=5 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    correction = await reconcile_level_drop(GUILD, USER, 5,
                                            operation_id="corr-g",
                                            actor_id=999, member=None)
    check("G: the claim is deleted as before (unchanged contract)",
          claim_count() == 0, correction["deleted_claims"])


async def case_h_reset_removal_fails():
    """The reset's own removal fails while the role is worn."""
    print("\n== H. the reset's role removal fails ==")
    await reset_db()
    claim_id = seed_claim("pending")
    world = World(held=True, fail_remove=True)
    report = await reset_member_apply(world)
    check("H: the failure is reported, not called a removal",
          report["roles_removed"] == []
          and [f["role_id"] for f in report["role_failures"]] == [ROLE_ID],
          (report["roles_removed"], report["role_failures"]))
    check("H: no applied journal row is written on a failed removal",
          journal(claim_id) == [], journal(claim_id))
    check("H: the claim is kept and the reset is not a clean New Game",
          claim_status(claim_id) == "pending"
          and report["new_game_complete"] is False, report["traces"])
    check("H: the admin is told the role was not removed",
          any("was NOT removed" in line for line in report["could_not"]),
          report["could_not"])

    # The claim is still `pending` (nothing was delivered), so a correction
    # leaves it dormant by design. The recovery path is a second reset now that
    # the Discord call works: the plan still names the role and can remove it.
    world.fail_remove = False
    retry = await reset_member_apply(world)
    check("H: a second reset removes the role and closes the claim",
          world.role not in world.held and claim_count() == 0,
          (world.held, claim_count()))
    check("H: and only then is the reset a clean New Game",
          retry["new_game_complete"] is True and retry["traces"] == [],
          retry["traces"])


async def main():
    for case in (case_a_role_absent_during_reset,
                 case_b_role_present_during_reset,
                 case_c_stale_applied_and_removal_fails,
                 case_d_shared_role_kept,
                 case_e_two_stale_rows_same_role,
                 case_f_stale_applied_role_gone,
                 case_g_stale_applied_no_member,
                 case_h_reset_removal_fails):
        await case()
        # A worn role must never coexist with zero claim rows anywhere.
        if claim_count() == 0:
            from utils.level_test_reset import inspect_member
            seen = await inspect_member(GUILD, USER)
            check(f"{case.__name__}: end state has no hidden worn reward role",
                  seen["role_ids"] == [] or seen["claims"] != [])

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL IN-FLIGHT ROLE CORRECTION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
