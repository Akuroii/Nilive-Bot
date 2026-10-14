"""Level reward roles: accumulate by default, exclusive when the guild opts in.

The behaviour locked down here (`remove_old_reward_role` on the Leveling page):

* OFF (default) — Level reward roles accumulate exactly as they always did;
* ON — a guild's leveling_rewards rows are a progression (Level 5 → Role 5,
  Level 10 → Role 10): delivering a higher role REMOVES the already delivered
  lower ones, so reaching Level 10 does not leave the member wearing Level 5's
  badge forever;
* it must never touch roles that are not part of that progression (manually
  granted roles, other systems' roles, other claim tracks);
* it must ride the existing claim-based delivery path — same entitlement
  rows, same reserve/fulfil transaction, same retryable failed-claim state —
  with no schema change and no second reward engine.

Cases: first reward / replace older / skip several levels / unrelated roles
survive / failed delivery retries without corrupting claim state.

Run from the scripts directory:
    python test_level_reward_role_progression.py
"""
import asyncio
from types import SimpleNamespace

import aiosqlite
from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows

ROLE_IDS = {5: 505, 10: 1010, 15: 1515, 20: 2020}
SECOND_LEVEL10_ROLE = 1011
UNRELATED_ROLE = 4242
MANUAL_ROLE = 7777


def xp_to(level: int) -> int:
    """Cumulative XP needed to be exactly at `level` (xp_for_level = 100*L^1.5)."""
    import math
    return sum(math.floor(100 * l ** 1.5) for l in range(1, level + 1))


class Role:
    def __init__(self, rid, name="Level Reward", position=1):
        self.id = rid
        self.name = name
        self.position = position


class FakeMember:
    """Minimal Discord member: role list + the two API calls level_claims uses."""

    def __init__(self, guild, roles=()):
        self.guild = guild
        self.roles = list(roles)
        self.added, self.removed = [], []
        self.fail_add = None
        self.fail_remove = None

    async def add_roles(self, role, reason=None):
        if self.fail_add:
            self.fail_add(role)
        if role not in self.roles:
            self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        if self.fail_remove:
            self.fail_remove(role)
        self.roles = [r for r in self.roles if r.id != role.id]
        self.removed.append(role.id)

    def held(self):
        return sorted(r.id for r in self.roles)


def guild_for(role_ids):
    catalog = {rid: Role(rid, f"Level Role {rid}") for rid in role_ids}
    catalog[8888] = Role(8888, "Shared Level Role")   # the two-levels-one-role case
    catalog[SECOND_LEVEL10_ROLE] = Role(SECOND_LEVEL10_ROLE, "Second Level 10 Role")
    catalog[UNRELATED_ROLE] = Role(UNRELATED_ROLE, "Member")
    catalog[MANUAL_ROLE] = Role(MANUAL_ROLE, "Event Winner")
    return SimpleNamespace(
        id=GUILD,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_role=lambda rid: catalog.get(int(rid)),
    )


def set_exclusive(on: bool):
    """The dashboard's `remove_old_reward_role` setting (0 = OFF, 1 = ON)."""
    execute("INSERT INTO leveling_config (guild_id, remove_old_reward_role) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET remove_old_reward_role=excluded.remove_old_reward_role",
            (GUILD, 1 if on else 0))


def seed_role_reward(level, role_id):
    execute("INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?,?,?)",
            (GUILD, level, role_id))


def clear_progression():
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_rewards")


async def cross(old_xp, new_xp):
    """Create the entitlement rows a level-up would create (production path)."""
    from utils.level_claims import record_crossing
    assert new_xp > old_xp
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        created = await record_crossing(db, GUILD, USER, old_xp, new_xp,
                                        {"balance": 1.0, "diamonds": 1.0},
                                        source="test")
        await db.commit()
    return created


def claim_rows(track="role"):
    return rows("SELECT reward_level, status FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=? AND track=? ORDER BY reward_level, id",
                (GUILD, USER, track))


def errors_for(level):
    found = rows("SELECT last_error FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                 "AND track='role' AND reward_level=?", (GUILD, USER, level))
    return found[0][0] if found else None


async def main():
    await reset_database()
    from utils.level_claims import claim_available, ensure_tables
    await ensure_tables()
    set_exclusive(True)            # the rollout cases below run with the toggle ON

    checks = []

    def check(name, ok, extra=""):
        checks.append((name, ok, extra))
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not ok else ""))

    guild = guild_for(list(ROLE_IDS.values()))

    # ── 1. first Level reward role: added, nothing removed ──────────────────
    print("== 1. [ON] first Level reward role ==")
    seed_role_reward(5, ROLE_IDS[5])
    await cross(0, xp_to(6))                      # crosses L5
    who = FakeMember(guild, [guild.get_role(UNRELATED_ROLE)])
    result = await claim_available(GUILD, USER, member=who)
    check("the L5 role was delivered",
          who.held() == sorted([ROLE_IDS[5], UNRELATED_ROLE])
          and result["delivered_roles"] == 1, f"held={who.held()} result={result}")
    check("nothing was removed on a first reward", who.removed == [], str(who.removed))
    check("the unrelated role survived", UNRELATED_ROLE in who.held(), str(who.held()))
    check("the claim is fulfilled", claim_rows() == [(5, "fulfilled")], str(claim_rows()))

    # ── 2. replacing an older Level reward role ─────────────────────────────
    print("== 2. [ON] replacing an older Level reward role ==")
    seed_role_reward(10, ROLE_IDS[10])
    who.removed, who.added = [], []
    await cross(xp_to(6), xp_to(11))              # crosses L10
    check("only the newly crossed level is pending",
          [r for r in claim_rows() if r[1] == "pending"] == [(10, "pending")], str(claim_rows()))
    delivered = await claim_available(GUILD, USER, member=who)
    check("the L10 role was added", ROLE_IDS[10] in who.held(), str(who.held()))
    check("the L5 role was removed", ROLE_IDS[5] not in who.held(), str(who.held()))
    check("exactly the superseded role was removed", who.removed == [ROLE_IDS[5]], str(who.removed))
    check("the unrelated role still survived", UNRELATED_ROLE in who.held(), str(who.held()))
    check("both claims are fulfilled and neither duplicated",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled")], str(claim_rows()))
    check("the delivery counted the new role", delivered["delivered_roles"] == 1, str(delivered))

    # ── 3. skipping multiple reward levels ──────────────────────────────────
    print("== 3. [ON] skipping multiple reward levels ==")
    seed_role_reward(15, ROLE_IDS[15])
    seed_role_reward(20, ROLE_IDS[20])
    who.removed, who.added = [], []
    await cross(xp_to(11), xp_to(21))             # crosses L15 and L20 in one jump
    check("both skipped levels got their own entitlement",
          [r for r in claim_rows() if r[1] == "pending"] == [(15, "pending"), (20, "pending")],
          str(claim_rows()))
    await claim_available(GUILD, USER, member=who)
    check("only the highest level's role is held",
          who.held() == sorted([ROLE_IDS[20], UNRELATED_ROLE]), str(who.held()))
    check("the intermediate role was not left behind",
          ROLE_IDS[15] not in who.held() and ROLE_IDS[15] in who.removed, str(who.removed))
    check("each skipped level keeps exactly one fulfilled row",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled"),
                           (15, "fulfilled"), (20, "fulfilled")], str(claim_rows()))

    # ── 4. unrelated roles are never touched ───────────────────────────────
    print("== 4. [ON] unrelated roles ==")
    who.roles = [guild.get_role(ROLE_IDS[20]), guild.get_role(UNRELATED_ROLE),
                 guild.get_role(MANUAL_ROLE)]
    removed_before = list(who.removed)
    await claim_available(GUILD, USER, member=who)     # nothing left to deliver
    check("a manual/other-system role is untouched",
          MANUAL_ROLE in who.held() and UNRELATED_ROLE in who.held(), str(who.held()))
    check("the current progression role is untouched", ROLE_IDS[20] in who.held(), str(who.held()))
    check("a no-op claim pass removes nothing", who.removed == removed_before, str(who.removed))

    print("== 4b. [ON] a role id shared by two levels is never removed ==")
    clear_progression()
    who.roles, who.removed, who.added = [], [], []
    seed_role_reward(5, 8888)
    seed_role_reward(10, 8888)                         # same role at two levels
    await cross(0, xp_to(11))
    await claim_available(GUILD, USER, member=who)
    check("the shared role stays after the highest level is delivered",
          who.held() == [8888] and who.removed == [], f"held={who.held()} removed={who.removed}")
    check("both levels are fulfilled", claim_rows() == [(5, "fulfilled"), (10, "fulfilled")],
          str(claim_rows()))

    # ── 5. lower-role removal failures never corrupt claims ───────────────
    print("== 5. [ON] a failed removal leaves the successful higher claim fulfilled ==")
    clear_progression()
    who.roles, who.removed, who.added = [guild.get_role(ROLE_IDS[5])], [], []
    seed_role_reward(5, ROLE_IDS[5])
    seed_role_reward(10, ROLE_IDS[10])
    await cross(0, xp_to(6))
    await claim_available(GUILD, USER, member=who)      # L5 already delivered + held
    check("historical state: L5 fulfilled and held",
          claim_rows() == [(5, "fulfilled")] and who.held() == [ROLE_IDS[5]],
          f"{claim_rows()} {who.held()}")

    await cross(xp_to(6), xp_to(11))                    # crosses L10
    who.fail_remove = lambda role: (_ for _ in ()).throw(RuntimeError("discord rate limited"))
    failed = await claim_available(GUILD, USER, member=who)
    check("the higher role claim stays fulfilled when old-role removal fails",
          failed["delivered_roles"] == 1
          and claim_rows() == [(5, "fulfilled"), (10, "fulfilled")]
          and failed["reconciled"]["failed"]
          and "rate limited" in failed["reconciled"]["failed"][0][1]
          and errors_for(10) is None,
          f"{claim_rows()} reconciliation={failed['reconciled']}")
    check("the failure creates no extra entitlement and preserves the old role",
          len(claim_rows()) == 2 and ROLE_IDS[5] in who.held()
          and ROLE_IDS[10] in who.held(), f"{claim_rows()} {who.held()}")

    who.fail_remove = None
    retried = await claim_available(GUILD, USER, member=who)
    check("the next explicit pass retries only Discord reconciliation",
          retried["delivered_roles"] == 0
          and claim_rows() == [(5, "fulfilled"), (10, "fulfilled")]
          and errors_for(10) is None, f"{retried} {claim_rows()}")
    check("successful reconciliation then removes only the superseded role",
          who.held() == [ROLE_IDS[10]] and who.removed == [ROLE_IDS[5]],
          f"held={who.held()} removed={who.removed}")
    check("a further claim pass is a no-op",
          (await claim_available(GUILD, USER, member=who))["owned"] == 0, "")

    print("== 5b. [ON] a failed add retries cleanly and removes nothing ==")
    clear_progression()
    who.roles, who.removed, who.added = [guild.get_role(ROLE_IDS[5])], [], []
    seed_role_reward(5, ROLE_IDS[5])
    seed_role_reward(10, ROLE_IDS[10])
    await cross(0, xp_to(6))
    await claim_available(GUILD, USER, member=who)      # L5 fulfilled + held
    await cross(xp_to(6), xp_to(11))                    # L10 pending
    who.fail_add = lambda role: (_ for _ in ()).throw(RuntimeError("role above the bot"))
    failed = await claim_available(GUILD, USER, member=who)
    check("a failed add removes nothing and keeps the old role",
          failed["delivered_roles"] == 0 and who.held() == [ROLE_IDS[5]]
          and who.removed == [] and "above the bot" in (errors_for(10) or ""),
          f"held={who.held()} removed={who.removed} err={errors_for(10)}")
    who.fail_add = None
    ok = await claim_available(GUILD, USER, member=who)
    check("the retry delivers the new role and supersedes the old one",
          ok["delivered_roles"] == 1 and who.held() == [ROLE_IDS[10]]
          and claim_rows() == [(5, "fulfilled"), (10, "fulfilled")],
          f"{ok} held={who.held()} claims={claim_rows()}")

    print("== 5c. [ON] multiple same-Level roles are independent and atomic for replacement ==")
    clear_progression()
    who.roles, who.removed, who.added = [guild.get_role(ROLE_IDS[5])], [], []
    who.fail_add = None
    seed_role_reward(5, ROLE_IDS[5])
    seed_role_reward(10, ROLE_IDS[10])
    seed_role_reward(10, SECOND_LEVEL10_ROLE)
    await cross(0, xp_to(6))
    await claim_available(GUILD, USER, member=who)
    await cross(xp_to(6), xp_to(11))
    definitions_before = rows(
        "SELECT id,level,role_id FROM leveling_rewards WHERE guild_id=? ORDER BY id",
        (GUILD,))
    who.fail_add = lambda role: (
        (_ for _ in ()).throw(RuntimeError("second same-Level role unavailable"))
        if role.id == SECOND_LEVEL10_ROLE else None)
    partial = await claim_available(GUILD, USER, member=who)
    check("one successful L10 role does not remove L5 while its sibling fails",
          ROLE_IDS[5] in who.held() and ROLE_IDS[10] in who.held()
          and SECOND_LEVEL10_ROLE not in who.held() and who.removed == []
          and partial["reconciled"]["blocked"] is True,
          f"held={who.held()} removed={who.removed} reconciled={partial['reconciled']}")
    check("same-Level role claims stay independent; successful and failed states persist",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled"), (10, "failed")]
          and len(claim_rows()) == 3, str(claim_rows()))
    check("role definitions were neither collapsed nor rewritten",
          rows("SELECT id,level,role_id FROM leveling_rewards WHERE guild_id=? ORDER BY id",
               (GUILD,)) == definitions_before and len(definitions_before) == 3,
          str(definitions_before))

    who.fail_add = None
    completed_group = await claim_available(GUILD, USER, member=who)
    check("after all highest-Level deliveries succeed, both roles stay and L5 is removed",
          who.held() == sorted([ROLE_IDS[10], SECOND_LEVEL10_ROLE])
          and who.removed == [ROLE_IDS[5]]
          and completed_group["delivered_roles"] == 1,
          f"held={who.held()} removed={who.removed} result={completed_group}")
    check("the retried same-Level claim is fulfilled without deleting definitions",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled"), (10, "fulfilled")]
          and rows("SELECT id,level,role_id FROM leveling_rewards WHERE guild_id=? ORDER BY id",
                   (GUILD,)) == definitions_before,
          f"claims={claim_rows()}")

    # ── 6. toggle OFF: roles accumulate, nothing is removed ────────────────
    print("== 6. [OFF] the toggle is off: old roles accumulate ==")
    clear_progression()
    set_exclusive(False)
    who.roles = [guild.get_role(UNRELATED_ROLE), guild.get_role(MANUAL_ROLE)]
    who.removed, who.added = [], []
    seed_role_reward(5, ROLE_IDS[5])
    seed_role_reward(10, ROLE_IDS[10])
    await cross(0, xp_to(6))
    await claim_available(GUILD, USER, member=who)          # L5 delivered
    check("[OFF] the L5 role is held",
          who.held() == sorted([ROLE_IDS[5], UNRELATED_ROLE, MANUAL_ROLE]), str(who.held()))
    await cross(xp_to(6), xp_to(11))
    off_result = await claim_available(GUILD, USER, member=who)
    check("[OFF] the L10 role is added",
          ROLE_IDS[10] in who.held() and off_result["delivered_roles"] == 1,
          f"held={who.held()} result={off_result}")
    check("[OFF] the old L5 role is NOT removed",
          ROLE_IDS[5] in who.held() and who.removed == [], f"held={who.held()} removed={who.removed}")
    check("[OFF] unrelated roles are still untouched",
          UNRELATED_ROLE in who.held(), str(who.held()))
    check("[OFF] both claims are fulfilled as usual",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled")], str(claim_rows()))

    # ── 7. switching OFF → ON does not corrupt claims and reconciles ───────
    print("== 7. switching OFF → ON ==")
    claims_before = claim_rows()
    set_exclusive(True)
    check("flipping the setting leaves the claim ledger untouched",
          claim_rows() == claims_before and errors_for(5) is None and errors_for(10) is None,
          f"{claim_rows()} err5={errors_for(5)} err10={errors_for(10)}")
    check("no extra entitlement row was created by the flip", len(claim_rows()) == 2,
          str(claim_rows()))

    # a claimed-but-not-yet-delivered level reconciles the set on its next pass
    seed_role_reward(15, ROLE_IDS[15])
    await cross(xp_to(11), xp_to(16))
    who.removed, who.added = [], []
    await claim_available(GUILD, USER, member=who)
    progression_roles = {ROLE_IDS[5], ROLE_IDS[10], ROLE_IDS[15]}
    check("[ON] the next delivery supersedes the accumulated lower roles",
          progression_roles & set(who.held()) == {ROLE_IDS[15]}
          and sorted(who.removed) == sorted([ROLE_IDS[5], ROLE_IDS[10]]),
          f"held={who.held()} removed={who.removed}")
    check("[ON] unrelated roles still survive the supersede",
          UNRELATED_ROLE in who.held() and MANUAL_ROLE in who.held(), str(who.held()))
    check("[ON] every claim stays fulfilled exactly once",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled"), (15, "fulfilled")],
          str(claim_rows()))

    # ── 8. switching ON → OFF stops removing, and is not retroactive ───────
    print("== 8. switching ON → OFF ==")
    set_exclusive(False)
    seed_role_reward(20, ROLE_IDS[20])
    who.roles = [guild.get_role(ROLE_IDS[15])]
    who.removed, who.added = [], []
    await cross(xp_to(16), xp_to(21))
    await claim_available(GUILD, USER, member=who)
    check("[OFF] a new role is added and the older one stays",
          sorted(who.held()) == sorted([ROLE_IDS[15], ROLE_IDS[20]]) and who.removed == [],
          f"held={who.held()} removed={who.removed}")
    check("[OFF] claims remain fulfilled and unduplicated",
          claim_rows() == [(5, "fulfilled"), (10, "fulfilled"),
                           (15, "fulfilled"), (20, "fulfilled")], str(claim_rows()))
    check("[OFF] a second pass is still a no-op",
          (await claim_available(GUILD, USER, member=who))["owned"] == 0, "")

    failed_checks = [c for c in checks if not c[1]]
    print(f"\nLEVEL ROLE PROGRESSION: {len(checks) - len(failed_checks)} passed, "
          f"{len(failed_checks)} failed")
    for name, _, extra in failed_checks:
        print("  FAILED:", name, extra)
    return 1 if failed_checks else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
