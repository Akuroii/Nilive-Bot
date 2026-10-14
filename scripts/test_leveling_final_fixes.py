"""Rejoin restores the highest FULFILLED Level role; config validation guards.

* reconcile_role_progression (rejoin / Claim All) targets the highest Level
  whose role was actually delivered. A higher pending/failed Level is never
  treated as fulfilled, never granted, and never costs the member the role they
  already earned. Rejoin mutates no claim / XP / crossing / ledger row.
* Dashboard validation: xp_min_per_message <= xp_max_per_message, and the spam
  threshold floor is 2 (sanity guard; default stays 10, spam math untouched).

Run with:
    python scripts/test_leveling_final_fixes.py
"""
import asyncio
import re
import sqlite3
import sys
from pathlib import Path

import test_level_reward_role_progression as R
from test_level_reward_role_progression import (
    DB_PATH, GUILD, ROLE_IDS, SECOND_LEVEL10_ROLE, USER, FakeMember, clear_progression,
    cross, execute, guild_for, seed_role_reward, set_exclusive, xp_to)

FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def dump():
    conn = sqlite3.connect(DB_PATH)
    out = {}
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        out[table] = sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
    conn.close()
    return out


def set_status(level, status):
    execute("UPDATE level_reward_claims SET status=?, fulfilled_at=? WHERE guild_id=? AND reward_level=?",
            (status, "2026-01-01T00:00:00+00:00" if status == "fulfilled" else None, GUILD, level))


async def seed(levels, statuses, extra=None):
    clear_progression()
    for level in levels:
        seed_role_reward(level, ROLE_IDS[level])
    for level, role_id in (extra or []):
        seed_role_reward(level, role_id)
    xp = xp_to(max(levels) + 1)
    execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)", (GUILD, USER, xp, max(levels)))
    await cross(0, xp)
    for level, status in statuses.items():
        set_status(level, status)


async def main():
    await R.reset_database()
    from utils.level_claims import claim_available, ensure_tables, reconcile_role_progression
    await ensure_tables()
    guild = guild_for(ROLE_IDS.values())
    role = guild.get_role

    print("== rejoin, remove_old_reward_role ON: target = highest FULFILLED level")
    set_exclusive(True)
    await seed([5, 10], {5: "fulfilled", 10: "pending"})
    member = FakeMember(guild, [])
    before = dump()
    r = await reconcile_role_progression(member, GUILD, USER)
    check("L5 fulfilled + L10 pending, empty roles -> L5 restored",
          member.held() == [505] and r["restored"] == [505], f"{member.held()} {r}")
    check("the pending L10 role is NOT granted", 1010 not in member.held())
    check("rejoin created no claim / XP / crossing / ledger / economy change", dump() == before)

    await seed([5, 10, 15], {5: "fulfilled", 10: "fulfilled", 15: "pending"})
    member = FakeMember(guild, [role(505), role(1010)])
    r = await reconcile_role_progression(member, GUILD, USER)
    check("L5+L10 fulfilled, L15 pending -> L5 superseded by L10, L15 not granted",
          member.held() == [1010] and r["removed"] == [505], f"{member.held()} {r}")

    await seed([5, 10], {5: "fulfilled", 10: "failed"})
    member = FakeMember(guild, [role(505)])

    def deny(r_):
        if r_.id == 1010:
            raise RuntimeError("Missing Permissions")
    member.fail_add = deny
    res = await claim_available(GUILD, USER, member=member)
    still = [x[0] for x in sqlite3.connect(DB_PATH).execute(
        "SELECT status FROM level_reward_claims WHERE guild_id=? AND reward_level=10", (GUILD,))]
    check("L10 delivery fails again: holder keeps L5, nothing removed, claim stays retryable",
          member.held() == [505] and res["reconciled"]["removed"] == [] and still == ["failed"],
          f"{member.held()} {res['reconciled']} {still}")
    member.fail_add = None
    res = await claim_available(GUILD, USER, member=member)
    check("once the failure clears, retry delivers L10 and THEN supersedes L5",
          member.held() == [1010] and res["reconciled"]["removed"] == [505], f"{member.held()} {res['reconciled']}")

    await seed([5, 10], {5: "fulfilled"}, extra=[(10, SECOND_LEVEL10_ROLE)])
    execute("UPDATE level_reward_claims SET status='fulfilled', fulfilled_at='2026-01-01T00:00:00+00:00' "
            "WHERE guild_id=? AND reward_level=10 AND payload_json LIKE '%1010%'", (GUILD,))
    member = FakeMember(guild, [role(505)])
    r = await reconcile_role_progression(member, GUILD, USER)
    check("L10 partial (one role fulfilled, sibling pending): fulfilled sibling restored, L5 KEPT",
          sorted(member.held()) == [505, 1010] and r["blocked"] is True and r["removed"] == [],
          f"{member.held()} {r}")

    await seed([5], {5: "pending"})
    member = FakeMember(guild, [])
    r = await reconcile_role_progression(member, GUILD, USER)
    check("nothing fulfilled at all: no restore, no crash, reported blocked",
          member.held() == [] and r["restored"] == [] and r["blocked"] is True, str(r))

    print("== rejoin, remove_old_reward_role OFF: accumulate semantics unchanged")
    set_exclusive(False)
    await seed([5, 10], {5: "fulfilled", 10: "pending"})
    member = FakeMember(guild, [])
    before = dump()
    r = await reconcile_role_progression(member, GUILD, USER)
    check("OFF: L5 restored, pending L10 not granted, nothing removed",
          member.held() == [505] and r["removed"] == [], f"{member.held()} {r}")
    check("OFF: rejoin changed no table", dump() == before)
    await seed([5], {5: "pending"})
    member = FakeMember(guild, [])
    r = await reconcile_role_progression(member, GUILD, USER)
    check("OFF: nothing fulfilled -> no restore and blocked stays False (OFF semantics unchanged)",
          member.held() == [] and r["restored"] == [] and r["blocked"] is False, str(r))
    await seed([5, 10], {5: "fulfilled", 10: "fulfilled"})
    member = FakeMember(guild, [])
    before = dump()
    await reconcile_role_progression(member, GUILD, USER)
    check("OFF: both fulfilled roles restored", member.held() == [505, 1010], str(member.held()))
    check("OFF: rejoin changed no table (both fulfilled)", dump() == before)

    print("== dashboard validation (real API validator)")
    from dashboard.api.leveling import _leveling_config_ints as validate
    _, err = validate({"xp_min_per_message": 100, "xp_max_per_message": 10})
    check("min 100 > max 10 REJECTED with a clear message", bool(err) and "Min XP" in err, str(err))
    values, err = validate({"xp_min_per_message": 20, "xp_max_per_message": 20})
    check("min == max accepted", err is None and values["xp_min_per_message"] == 20, str(err))
    _, err = validate({})
    check("defaults (min/max, threshold 10) accepted", err is None, str(err))
    values, err = validate({})
    check("default spam threshold stays 10", err is None and values["spam_threshold"] == 10, str(values))
    _, err = validate({"spam_threshold": 1})
    check("spam_threshold=1 REJECTED", bool(err), str(err))
    values, err = validate({"spam_threshold": 2})
    check("spam_threshold=2 accepted", err is None and values["spam_threshold"] == 2, str(err))
    values, err = validate({"spam_threshold": 37})
    check("custom threshold >= 2 preserved", err is None and values["spam_threshold"] == 37, str(err))

    html = (Path(__file__).resolve().parents[1] / "dashboard/templates/systems/leveling.html").read_text()
    check("dashboard input mirrors the server floor (min=2)",
          re.search(r'name="spam_threshold"[^>]*min="2"', html) is not None)

    if FAILS:
        print(f"\n*** {len(FAILS)} CHECK(S) FAILED: {FAILS}")
        sys.exit(1)
    print("\nALL REJOIN / VALIDATION CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
