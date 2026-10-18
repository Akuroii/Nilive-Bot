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
  I  /xp_rollback: two delivered claims for one role created by the SAME
     operation must not justify each other; an INDEPENDENT claim still must.
  J  /xp_rollback: a caused claim still being delivered is reported and the
     operation is `partial`, never `completed`.
  K  an earlier OPEN reversal case keeps the reset from calling itself clean,
     and the applied embed never says "nothing was preserved" while it kept
     rows.
  L  /setxp correction: two delivered claims for one role, both above the
     corrected level, role worn. Neither may cite the other as an independent
     grant (no journal row is pre-seeded, as in production).
  M  the same fresh pair with the role already absent: both rows close, no
     removal is attempted, and nothing is left outstanding.
  N  a genuinely independent claim still protects the role from the correction.

Run with:
    python scripts/test_inflight_role_correction.py
"""
import asyncio
import sqlite3
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


async def rollback_db(*levels):
    """A clean member at L0 with one role reward per configured level."""
    await reset_db()
    execute("DELETE FROM leveling_rewards")
    execute("DELETE FROM level_reward_claims")
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    for level in levels:
        execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
                "VALUES (?,?,?,'everyone')", (GUILD, level, ROLE_ID))


def rollback_bot(world):
    return SimpleNamespace(
        get_guild=lambda gid: world.guild if gid == GUILD else None)


async def grant_and_deliver(world, top_level):
    """One administrative change up to ``top_level``, then Claim All."""
    from cogs.leveling import _apply_admin_xp
    from utils.level_claims import SOURCE_SETLEVEL, claim_available
    from utils.xp_calculator import xp_for_level
    target = sum(xp_for_level(n) for n in range(1, top_level + 1))
    granted = await _apply_admin_xp(
        GUILD, world.member, target, rollback_bot(world),
        source=SOURCE_SETLEVEL, actor_id=999, reason="regression")
    delivered = await claim_available(GUILD, USER, member=world.member)
    return granted, delivered


async def rollback(operation_id, world):
    from utils.level_test_reset import rollback_xp_operation
    return await rollback_xp_operation(
        GUILD, USER, operation_id=operation_id, apply=True,
        reversed_by=999, reason="regression rollback", member=world.member)


def op_status(kind="xp_rollback"):
    return [r[0] for r in rows(
        "SELECT status FROM level_ops WHERE user_id=? AND kind=?", (USER, kind))]


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

    from utils.level_test_reset import inspect_member
    preview = await inspect_member(GUILD, USER)
    check("A: the preview counts the in-flight claim as KEPT, not deleted",
          preview["plan"]["claims_kept"] == [claim_id]
          and preview["plan"]["delete_claims"] == 0,
          (preview["plan"]["claims_kept"], preview["plan"]["delete_claims"]))

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

    # What the administrator actually reads: the applied embed must not end on
    # "nothing was preserved" while a claim row was kept.
    from cogs.leveling import _level_reset_embed
    embed = _level_reset_embed(world.member, report, applied=True)
    footer = ((getattr(embed, "_footer", None) or {}).get("text") or "")
    body = all_embed_text(embed)
    check("A: the applied embed says this is NOT a clean New Game",
          "NOT a clean New Game" in footer
          and "nothing was preserved" not in footer
          and "Clean New Game" not in body, (footer, body[-400:]))

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


async def case_i_rollback_shared_role():
    """Two delivered claims for one role from ONE operation are not independent."""
    print("\n== I. /xp_rollback and a role configured at two levels ==")
    await rollback_db(4, 5)
    world = World()
    world.before_add.set()          # deliver immediately, not paused
    world.after_add.set()
    granted, delivered = await grant_and_deliver(world, 5)
    created = [r[0] for r in rows("SELECT id FROM level_reward_claims ORDER BY id")]
    check("I: the change delivered the same role at both levels",
          sorted(delivered["fulfilled"]) == created and len(created) == 2
          and world.role in world.held, (delivered, world.held, created))
    check("I: both rows carry the operation id being rolled back",
          {r[0] for r in rows("SELECT operation_id FROM level_reward_claims")}
          == {granted["operation_id"]})

    report = await rollback(granted["operation_id"], world)
    check("I: the role is removed, not kept by the row being reversed",
          report["roles_removed"] == [ROLE_ID] and world.role not in world.held,
          (report["roles_removed"], report.get("roles_left_on_member"),
           world.held))
    check("I: no claim from the operation survives above the target level",
          claim_count() == 0, rows("SELECT id,reward_level,status FROM "
                                   "level_reward_claims"))
    check("I: the report does not cite a claim this run deletes as the "
          "reason to keep the role",
          report.get("roles_left_on_member") == []
          and report["claims_preserved"] == [],
          (report.get("roles_left_on_member"), report.get("claims_preserved")))
    check("I: the operation is complete because every effect is gone",
          op_status() == ["completed"], op_status())

    print("\n== I2. an INDEPENDENT claim for the same role still justifies it ==")
    await rollback_db(4, 5)
    world = World()
    world.before_add.set()
    world.after_add.set()
    # The member earned the L5 role by playing, before the administrative
    # change. It is not part of the operation and must survive the rollback.
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "track,reward_ref,payload_json,status,source,created_at) "
            "VALUES (?,?,5,'role',?,?,'fulfilled','level_up',"
            "CURRENT_TIMESTAMP)",
            (GUILD, USER, str(ROLE_ID), f'{{"role_id": {ROLE_ID}}}'))
    independent = rows("SELECT id FROM level_reward_claims")[0][0]
    granted, delivered = await grant_and_deliver(world, 5)
    caused = delivered["fulfilled"][0]
    check("I2: the administrative change created only its own claim",
          len(delivered["fulfilled"]) == 1 and caused != independent
          and claim_count() == 2, (delivered, independent, claim_count()))
    check("I2: the role is worn and documented by both rows",
          world.role in world.held and claim_count() == 2)

    report = await rollback(granted["operation_id"], world)
    check("I2: the role stays, because a claim that is NOT being reversed "
          "grants it",
          world.role in world.held
          and [r["role_id"] for r in report.get("roles_left_on_member") or []]
          == [ROLE_ID],
          (world.held, report.get("roles_left_on_member")))
    check("I2: the independent claim survives untouched",
          claim_status(independent) == "fulfilled", claim_status(independent))
    check("I2: the caused claim is preserved with that reason, not deleted",
          [c["claim_id"] for c in report["claims_preserved"]] == [caused]
          and "also granted" in report["claims_preserved"][0]["reason"],
          report.get("claims_preserved"))
    check("I2: the role is never left undocumented",
          not (world.role in world.held and claim_count() == 0))


async def case_j_rollback_in_flight_claim():
    """A delivery in flight is an open case: the operation is partial."""
    print("\n== J. /xp_rollback while a caused claim is still being delivered ==")
    await rollback_db(4)
    world = World()
    from cogs.leveling import _apply_admin_xp
    from utils.level_claims import SOURCE_SETLEVEL
    from utils.xp_calculator import xp_for_level
    target = sum(xp_for_level(n) for n in range(1, 5))
    granted = await _apply_admin_xp(
        GUILD, world.member, target, rollback_bot(world),
        source=SOURCE_SETLEVEL, actor_id=999, reason="regression")
    claim_id = rows("SELECT id FROM level_reward_claims")[0][0]
    task = start_delivery(world, claim_id)
    check("J: the claim reached the in-flight state",
          await wait_processing(claim_id), claim_status(claim_id))
    check("J: the role is not on the member yet", world.role not in world.held)

    report = await rollback(granted["operation_id"], world)
    check("J: the in-flight claim is reported, not dropped",
          [c["claim_id"] for c in report.get("in_flight_claims") or []]
          == [claim_id], report.get("in_flight_claims"))
    check("J: the claim is kept and the reason says the rollback is not "
          "complete",
          claim_status(claim_id) == "processing"
          and "NOT complete" in report["claims_preserved"][0]["reason"],
          (claim_status(claim_id), report.get("claims_preserved")))
    check("J: the operation is PARTIAL, never completed",
          op_status() == ["partial"], op_status())
    check("J: the XP was still rolled back", rows(
        "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
        (GUILD, USER))[0][0] == 0)

    # Documented residual: the delivery finishes afterwards and pays a reward
    # above the target level. The claim row stays as its record, and the
    # operation already says `partial`, so this is visible, not silent.
    world.before_add.set()
    world.after_add.set()
    await task
    check("J: the late delivery is still documented by its claim row",
          claim_status(claim_id) == "fulfilled" and world.role in world.held,
          (claim_status(claim_id), world.held))


async def case_k_open_case_blocks_clean_verdict():
    """An earlier open reversal case is a trace, even with no claim row left."""
    print("\n== K. an earlier open reversal case blocks a clean New Game ==")
    await reset_db()
    claim_id = seed_claim("fulfilled", level=10, operation="old-admin")
    from utils import level_ops as ops
    await ops.note_reversal(
        guild_id=GUILD, user_id=USER, claim_id=claim_id, kind=ops.KIND_ROLE,
        quantity=1, operation_id="old-correction", status=ops.STATUS_OUTSTANDING,
        role_id=ROLE_ID,
        reason="Discord refused the role removal; the case is still open")
    execute("DELETE FROM level_reward_claims WHERE id=?", (claim_id,))
    world = World()             # the role is NOT held: the case is the only trace
    report = await reset_member_apply(world)
    check("K: the reset still reports the open case",
          [c["claim_id"] for c in report.get("outstanding") or []] == [claim_id],
          report.get("outstanding"))
    check("K: and refuses to call the result a clean New Game",
          report["new_game_complete"] is False
          and any("unresolved" in t for t in report["traces"]),
          report.get("traces"))
    from cogs.leveling import _level_reset_embed
    embed = _level_reset_embed(world.member, report, applied=True)
    footer = ((getattr(embed, "_footer", None) or {}).get("text") or "")
    check("K: the embed footer agrees", "NOT a clean New Game" in footer, footer)


async def case_l_correction_two_claims_same_role_worn():
    """Fresh correction: two delivered rows for one role must not block each other."""
    print("\n== L. correction, two delivered claims for one role, role worn ==")
    await reset_db()
    first = seed_claim("fulfilled", level=10, operation="admin-a")
    second = seed_claim("fulfilled", level=12, operation="admin-b",
                        payload=f'{{"role_id": {ROLE_ID}}}')
    world = World(held=True)

    report = await reconcile(5, world)
    check("L: the role is removed, not kept because the rows cite each other",
          world.role not in world.held
          and report["reversed"]["role"] != [],
          (world.held, report["reversed"]["role"]))
    check("L: both claims are deleted, so neither blocks the other",
          claim_count() == 0
          and {c["claim_id"] for c in report["deleted_claims"]} == {first, second},
          (claim_count(), report["deleted_claims"]))
    check("L: no claim is left outstanding and no case cites a sibling",
          report["kept_unresolved"] == [], report["kept_unresolved"])
    check("L: both role outcomes are recorded as applied, not outstanding",
          sorted(status for status, _ in journal(first)) == ["applied"]
          and sorted(status for status, _ in journal(second)) == ["applied"],
          (journal(first), journal(second)))

    retry = await reconcile(5, world)
    check("L: a retry has nothing left to do",
          retry["deleted_claims"] == [] and retry["kept_unresolved"] == []
          and claim_count() == 0,
          (retry["deleted_claims"], retry["kept_unresolved"], claim_count()))


async def case_m_correction_two_claims_same_role_absent():
    """No live role effect: both fresh rows must still close, nothing to remove."""
    print("\n== M. correction, two delivered claims, role already absent ==")
    await reset_db()
    first = seed_claim("fulfilled", level=10, operation="admin-a")
    second = seed_claim("fulfilled", level=12, operation="admin-b",
                        payload=f'{{"role_id": {ROLE_ID}}}')
    world = World(held=False)

    report = await reconcile(5, world)
    check("M: neither claim is left permanently outstanding",
          report["kept_unresolved"] == [] and claim_count() == 0,
          (report["kept_unresolved"], claim_count()))
    check("M: both rows are deleted and recorded as gone",
          {c["claim_id"] for c in report["deleted_claims"]} == {first, second}
          and sorted(status for status, _ in journal(first)) == ["applied"]
          and sorted(status for status, _ in journal(second)) == ["applied"],
          (report["deleted_claims"], journal(first), journal(second)))
    check("M: no Discord removal is attempted for an absent role",
          world.member.remove_roles.await_count == 0,
          world.member.remove_roles.await_count)
    check("M: it is reported as already gone, not as a removal failure",
          report["role_failures"] == []
          and len(report["reversed"]["role"]) == 2
          and all(e["status"] == "applied"
                  for e in report["reversed"]["role"]),
          (report["role_failures"], report["reversed"]["role"]))


async def case_n_correction_independent_grant_protects_role():
    """A claim at/below the corrected level is a real entitlement, not a sibling."""
    print("\n== N. correction: an independent claim still protects the role ==")
    await reset_db()
    stale = seed_claim("fulfilled", level=10, operation="admin-10")
    keeper = seed_claim("fulfilled", level=5, operation="admin-5")
    world = World(held=True)

    report = await reconcile(5, world)
    check("N: the role stays, because another claim still grants it",
          world.role in world.held and report["reversed"]["role"] == [],
          (world.held, report["reversed"]["role"]))
    check("N: the shared-role claim is kept, not deleted",
          claim_status(stale) == "fulfilled"
          and stale not in {c["claim_id"] for c in report["deleted_claims"]},
          (claim_status(stale), report["deleted_claims"]))
    check("N: the genuinely independent claim survives untouched",
          claim_status(keeper) == "fulfilled" and claim_count() == 2,
          (claim_status(keeper), claim_count()))
    check("N: the report names the independent grant as the reason",
          any("also granted" in c["reason"] for c in report["kept_unresolved"]),
          report["kept_unresolved"])
    check("N: no removal is claimed and no orphan is created",
          not (world.role in world.held and claim_count() == 0))


async def main():
    for case in (case_a_role_absent_during_reset,
                 case_b_role_present_during_reset,
                 case_c_stale_applied_and_removal_fails,
                 case_d_shared_role_kept,
                 case_e_two_stale_rows_same_role,
                 case_f_stale_applied_role_gone,
                 case_g_stale_applied_no_member,
                 case_h_reset_removal_fails,
                 case_i_rollback_shared_role,
                 case_j_rollback_in_flight_claim,
                 case_k_open_case_blocks_clean_verdict,
                 case_l_correction_two_claims_same_role_worn,
                 case_m_correction_two_claims_same_role_absent,
                 case_n_correction_independent_grant_protects_role):
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
