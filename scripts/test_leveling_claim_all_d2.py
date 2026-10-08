"""D2: Claim All restores a missing, already-FULFILLED Level reward role.

Locks down, against the real production paths and a scratch DB:

* `_claim_button_available` (cogs.leveling) — Claim All stays reachable with
  nothing pending only when a role claim is fulfilled AND
  `remove_old_reward_role` is ON (it is then the explicit reconcile action);
* the full member-initiated Claim All flow (`claim_available`) when every Level
  claim is already fulfilled but the member lacks the fulfilled role:
  - the missing role is restored; with `remove_old_reward_role` ON the old
    lower role is removed ONLY after the target role(s) are delivered;
  - no claim, XP, level, ledger, economy or inventory row changes (whole-DB diff);
  - a second pass is a no-op;
  - OFF: roles accumulate, Claim All never restores/removes anything.

Run with:
    python scripts/test_leveling_claim_all_d2.py
"""
import asyncio
import sqlite3
import sys

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
    """Every table, every row: the strongest 'nothing else changed' oracle."""
    conn = sqlite3.connect(DB_PATH)
    out = {}
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY 1"):
        out[table] = sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
    conn.close()
    return out


def changed_tables(before, after):
    return [t for t in before if before[t] != after[t]]


async def seed_fulfilled(levels=(5, 10)):
    """Level 5 + Level 10 role rewards, member at Level 10, every claim fulfilled."""
    clear_progression()
    for level in levels:
        seed_role_reward(level, ROLE_IDS[level])
    xp = xp_to(max(levels) + 1)
    execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, USER, xp, max(levels)))
    await cross(0, xp)
    execute("UPDATE level_reward_claims SET status='fulfilled', "
            "fulfilled_at='2026-01-01T00:00:00+00:00' WHERE guild_id=?", (GUILD,))


async def main():
    await R.reset_database()
    from cogs.leveling import _claim_button_available
    from utils.level_claims import claim_available, ensure_tables, list_claims
    await ensure_tables()
    guild = guild_for(ROLE_IDS.values())
    role = guild.get_role

    # ------------------------------------------------------------------ B
    print("\n== _claim_button_available (real production semantics)")
    set_exclusive(True)
    await seed_fulfilled()
    claims = await list_claims(GUILD, USER)
    check("precondition: every claim is fulfilled", all(c["status"] == "fulfilled" for c in claims))
    check("ON + fulfilled role claim -> enabled (Claim All is the reconcile action)",
          await _claim_button_available(GUILD, claims) is True)
    check("ON + only non-role claims fulfilled -> disabled",
          await _claim_button_available(GUILD, [c for c in claims if c["track"] != "role"]) is False)
    check("ON + no claims at all -> disabled",
          await _claim_button_available(GUILD, []) is False
          and await _claim_button_available(GUILD, None) is False)
    pending = [dict(claims[0], status="pending")]
    check("a pending claim enables the button regardless of the toggle",
          await _claim_button_available(GUILD, pending) is True)
    set_exclusive(False)
    check("OFF + fulfilled role claim -> disabled (nothing to claim or reconcile)",
          await _claim_button_available(GUILD, claims) is False)
    check("OFF + pending claim -> still enabled",
          await _claim_button_available(GUILD, pending) is True)

    # ------------------------------------------------------------------ C
    print("\n== D2: ON, all claims fulfilled, L10 role MISSING, member-initiated Claim All")
    set_exclusive(True)
    await seed_fulfilled()
    member = FakeMember(guild, [role(505)])          # holds old L5, lacks fulfilled L10
    before = dump()
    claim_ids_before = [r[0] for r in before["level_reward_claims"]]
    res = await claim_available(GUILD, USER, member=member)
    after = dump()
    check("pass owns / pays / delivers nothing new",
          res["owned"] == 0 and res["fulfilled"] == [] and res["delivered_roles"] == 0, str(res))
    check("missing fulfilled L10 role restored", res["reconciled"]["restored"] == [1010], str(res["reconciled"]))
    check("old L5 role removed after L10 was delivered",
          res["reconciled"]["removed"] == [505] and member.held() == [1010], f"{member.held()} {res['reconciled']}")
    check("removal came AFTER delivery (add L10 precedes remove L5)",
          member.added == [1010] and member.removed == [505])
    check("NO table changed (claims/levels/xp/ledger/economy/inventory identical)",
          changed_tables(before, after) == [], str(changed_tables(before, after)))
    check("no fake or duplicate claim created",
          [r[0] for r in after["level_reward_claims"]] == claim_ids_before)
    check("XP and level untouched",
          sqlite3.connect(DB_PATH).execute(
              "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER)
          ).fetchone() == (xp_to(11), 10))
    again = await claim_available(GUILD, USER, member=member)
    check("second pass is an idempotent no-op",
          again["reconciled"]["restored"] == [] and again["reconciled"]["removed"] == []
          and member.held() == [1010] and changed_tables(before, dump()) == [])

    print("\n== D2: ON, member has NO Level roles")
    await seed_fulfilled()
    member = FakeMember(guild, [])
    before = dump()
    await claim_available(GUILD, USER, member=member)
    check("only the HIGHEST fulfilled role is restored (L5 not re-granted)", member.held() == [1010], str(member.held()))
    check("DB unchanged", changed_tables(before, dump()) == [])

    print("\n== D2: ON, removal is withheld until EVERY target role is delivered")
    clear_progression()
    seed_role_reward(5, 505)
    seed_role_reward(10, 1010)
    seed_role_reward(10, SECOND_LEVEL10_ROLE)
    execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,10)", (GUILD, USER, xp_to(11)))
    await cross(0, xp_to(11))
    execute("UPDATE level_reward_claims SET status='fulfilled', "
            "fulfilled_at='2026-01-01T00:00:00+00:00' WHERE guild_id=?", (GUILD,))
    member = FakeMember(guild, [role(505)])

    def deny_second(r):
        if r.id == SECOND_LEVEL10_ROLE:
            raise RuntimeError("Missing Permissions")
    member.fail_add = deny_second
    before = dump()
    res = await claim_available(GUILD, USER, member=member)
    check("one L10 role delivered, second add failed -> old L5 KEPT, reported blocked",
          1010 in member.held() and 505 in member.held()
          and res["reconciled"]["blocked"] is True and res["reconciled"]["removed"] == [],
          f"{member.held()} {res['reconciled']}")
    check("the failure touched no claim/XP/ledger row", changed_tables(before, dump()) == [])
    member.fail_add = None
    res = await claim_available(GUILD, USER, member=member)
    check("retry delivers the missing role THEN removes L5",
          sorted(member.held()) == [1010, SECOND_LEVEL10_ROLE] and res["reconciled"]["removed"] == [505],
          f"{member.held()} {res['reconciled']}")

    print("\n== D2: OFF semantics (fulfilled Level roles accumulate)")
    set_exclusive(False)
    await seed_fulfilled()
    member = FakeMember(guild, [role(505)])
    before = dump()
    res = await claim_available(GUILD, USER, member=member)
    check("OFF: Claim All never restores or removes a Level role",
          member.held() == [505]
          and res["reconciled"] == {"restored": [], "removed": [], "failed": [], "blocked": False},
          f"{member.held()} {res['reconciled']}")
    check("OFF: DB unchanged", changed_tables(before, dump()) == [])
    # rejoin reconciliation under OFF: restore every fulfilled role, remove none
    from utils.level_claims import reconcile_role_progression
    r = await reconcile_role_progression(member, GUILD, USER)
    check("OFF rejoin path: L10 restored, L5 KEPT (accumulate)",
          member.held() == [505, 1010] and r["removed"] == [], f"{member.held()} {r}")
    fresh = FakeMember(guild, [])
    await reconcile_role_progression(fresh, GUILD, USER)
    check("OFF rejoin from empty: BOTH fulfilled roles restored", fresh.held() == [505, 1010], str(fresh.held()))
    check("OFF flows changed no table", changed_tables(before, dump()) == [])

    if FAILS:
        print(f"\n*** {len(FAILS)} D2 CHECK(S) FAILED: {FAILS}")
        sys.exit(1)
    print("\nALL D2 / CLAIM ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
