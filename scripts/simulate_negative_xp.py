"""Simulation for the anti-spam penalty (item 3, decision D1 = B).

With D1 = B implemented, the penalty is a real deduction that stays floored at
zero, and the stored level is recomputed from the resulting XP. This simulation
is the evidence trail for that decision, and it re-derives the two rejected
shapes every run:

* the **legacy** write (floor at zero, `level` left stale) — kept here as the
  before-picture, showing the silent level regression it produced;
* the **no-floor** proposal — XP goes negative, progress renders negative, and
  the member sorts below every 0-XP peer;
* and the duplicate-reward question, answered against the real claim ledger.

The shipped path is exercised through the real `apply_spam_penalty()`, and the
rejected shapes are reproduced as raw SQL behind a drift guard that fails
loudly if the production text changes.

Run from the scripts directory:
    python simulate_negative_xp.py
"""
import asyncio
import contextlib
import io
import sqlite3
import sys
from contextlib import closing

from phase1_support import GUILD, ROOT, execute, reset_database, rows

import aiosqlite
from database import DB_PATH

PENALTY = 10          # leveling_config.spam_xp_penalty default
GRANT = 5             # one small message's worth of XP at xp_per_word=1


def production_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def guard(rel: str, needle: str) -> None:
    """The simulation is only honest while these production snippets exist."""
    if needle not in production_text(rel):
        raise SystemExit(
            f"DRIFT: {rel} no longer contains {needle!r}. The simulation models "
            "behaviour this file no longer has — re-derive the audit before "
            "trusting any number below.")


def sql_exec(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(sql, args)
        db.commit()


def penalty_legacy(guild_id, user_id, amount):
    """The PRE-FIX write, kept only to document what was wrong with it.

    Floors XP at zero but leaves `levels.level` untouched, so the stored level
    and `xp_progress(xp)` disagree and the next XP grant silently demotes the
    member. No longer in the codebase."""
    sql_exec("""
        INSERT INTO levels (guild_id, user_id, xp, level)
        VALUES (?, ?, 0, 0)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET xp = MAX(0, xp - ?)
    """, (guild_id, user_id, amount))


def penalty_unfloored(guild_id, user_id, amount):
    """The proposed shape: subtract from current XP, no floor. (Not shipped.)"""
    sql_exec("""
        INSERT INTO levels (guild_id, user_id, xp, level)
        VALUES (?, ?, 0, 0)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET xp = xp - ?
    """, (guild_id, user_id, amount))


def state(guild_id, user_id):
    got = rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
               (guild_id, user_id))
    return got[0] if got else (0, 0)


def main():
    from utils.xp_calculator import (
        calculate_level_from_xp, xp_for_level, xp_progress,
    )
    from utils.level_claims import SOURCE_SETXP, ensure_tables, record_crossing

    # ── 0. drift guards: the shapes this audit describes must still exist ──
    guard("utils/xp_calculator.py", "async def apply_spam_penalty(")
    guard("utils/xp_calculator.py", "new_xp = max(0, old_xp - penalty)")
    guard("utils/xp_calculator.py", "new_level = xp_progress(new_xp)[0]")
    guard("cogs/leveling.py", "await apply_spam_penalty(guild_id, user_id, penalty)")
    guard("cogs/leveling.py", "xp = max(0, xp)")
    guard("utils/reward_engine.py", "new_xp    = max(0, old_xp + amount)")
    guard("utils/reward_engine.py", "new_level, _, _ = xp_progress(new_xp)")
    guard("utils/level_claims.py", "AND status != 'fulfilled'")

    print("# Anti-spam penalty / negative-XP simulation\n")
    print(f"Scratch DB: {DB_PATH}  (penalty {PENALTY} XP, one small grant "
          f"{GRANT} XP, curve `floor(100 * level ** 1.5)`)\n")

    # ── 1. every XP write path, and whether it can go negative ─────────────
    print("## 1. XP write paths in the current model\n")
    print("| # | Path | Code | Floor | Level recomputed |")
    print("|---|------|------|-------|------------------|")
    print("| 1 | XP grant (message/voice/direct) | `utils/reward_engine.py:155` "
          "`new_xp = max(0, old_xp + amount)` | yes | yes (`xp_progress`) |")
    print("| 2 | Anti-spam penalty | `utils/xp_calculator.py` "
          "`apply_spam_penalty()` — `new_xp = max(0, old_xp - penalty)`, "
          "`new_level = xp_progress(new_xp)[0]` | yes | **yes (D1 = B)** |")
    print("| 3 | Admin `/setxp` | `cogs/leveling.py:693` `xp = max(0, xp)` "
          "| yes | yes |")
    print("| 4 | Admin `/resetxp` | `cogs/leveling.py:743` "
          "`SET xp = 0, level = 0` | n/a | yes |")
    print("| 5 | `/resetleaderboard` + scheduled reset | `cogs/leveling.py:84` "
          "`SET xp = 0, level = 0` | n/a | yes |")
    print("| 6 | Dashboard member edit | `dashboard/app.py:2656` "
          "`max(0, int(...))` | yes | yes (`calculate_level_from_xp`) |")
    print("\nXP cannot go negative on any shipped path: paths 1, 2, 3 and 6 "
          "clamp, and 4/5 write zero.\n")

    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(reset_database())
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?,?,?)",
            (GUILD, 1, 1111))

    # ── 2. the shipped penalty, from a cold start ──────────────────────────
    print("## 2. Shipped penalty (real `apply_spam_penalty`): repeated spam\n")
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, 9001, 0, 0))
    floor_trace = []
    for _ in range(12):
        from utils.xp_calculator import apply_spam_penalty
        asyncio.run(apply_spam_penalty(GUILD, 9001, PENALTY))
        floor_trace.append(state(GUILD, 9001)[0])
    print(f"12 penalties of {PENALTY} XP against a member with 0 XP → "
          f"xp sequence {floor_trace}")
    print(f"final state: xp={state(GUILD, 9001)[0]} "
          f"level={state(GUILD, 9001)[1]} → floor holds, never negative, "
          "no debt.\n")

    # ── 3. the stale-level window the shipped penalty opens ────────────────
    print("## 3. The stale-level defect this replaced\n")
    level2_floor = xp_for_level(1) + xp_for_level(2)
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, 9002, level2_floor + 3, 2))
    before = state(GUILD, 9002)
    penalty_legacy(GUILD, 9002, PENALTY)
    after = state(GUILD, 9002)
    derived_level, derived_remaining, needed = xp_progress(after[0])
    print(f"member 3 XP into Level 2 (xp={before[0]}; Level 2 starts at "
          f"{level2_floor}, Level 3 at {level2_floor + xp_for_level(3)}):")
    print(f"  penalty {PENALTY} → xp={after[0]}, stored level={after[1]}, "
          f"`xp_progress({after[0]})` says level {derived_level} "
          f"(remaining {derived_remaining}, needed {needed})")
    print(f"  stale = {after[1] != derived_level}  ← the defect, fixed by D1 = B\n")
    print("## 3b. The shipped write keeps them consistent\n")
    execute("UPDATE levels SET xp=?, level=? WHERE guild_id=? AND user_id=?",
            (level2_floor + 3, 2, GUILD, 9002))
    fixed = asyncio.run(apply_spam_penalty(GUILD, 9002, PENALTY))
    shipped_xp, shipped_level = state(GUILD, 9002)
    derived_level, derived_remaining, needed = xp_progress(shipped_xp)
    print(f"same member, shipped penalty: xp {level2_floor + 3} → {shipped_xp}, "
          f"stored level {shipped_level}, curve says {derived_level} "
          f"(remaining {derived_remaining}, needed {needed})")
    print(f"  demoted by the penalty itself = {shipped_level < 2}, "
          f"stale = {shipped_level != derived_level}\n")

    # ── 4. what the stale level does to the NEXT legitimate grant ──────────
    legacy_next = max(0, after[0] + GRANT)        # utils/reward_engine.py:155
    legacy_recomputed, _, _ = xp_progress(legacy_next)
    print(f"LEGACY: the next message ({GRANT} XP) recomputes the stale level "
          f"{after[1]} → {legacy_recomputed} and visibly demotes the member on a "
          f"message that granted XP.")
    shipped_next = max(0, shipped_xp + GRANT)
    shipped_recomputed, _, _ = xp_progress(shipped_next)
    print(f"SHIPPED: the penalty already put the member at level {shipped_level}; "
          f"the next message recomputes {shipped_level} → {shipped_recomputed}, so "
          f"no surprise demotion is left. Silent regression = "
          f"{shipped_recomputed < shipped_level}.")
    print("  Note: entitlements are not revoked by a level decrease — "
          "`record_crossing` only counts increases, and delivered claims keep "
          "their roles (with `remove_old_reward_role` ON nothing is removed on "
          "the way down either).\n")

    # ── 5. the proposed shape: no floor ────────────────────────────────────
    print("## 5. Proposed shape (`SET xp = xp - penalty`, no floor)\n")
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, 9003, 0, 0))
    neg_trace = []
    for _ in range(3):
        penalty_unfloored(GUILD, 9003, PENALTY)
        neg_trace.append(state(GUILD, 9003)[0])
    neg = state(GUILD, 9003)[0]
    level, remaining, needed = xp_progress(neg)
    print(f"3 penalties of {PENALTY} from 0 XP → xp sequence {neg_trace}")
    print(f"  xp={neg}: `xp_progress({neg})` → level {level}, remaining "
          f"{remaining}, needed {needed} → the rank card's progress fraction "
          f"becomes {remaining}/{needed} = {remaining / needed:.0%}")
    print(f"  `calculate_level_from_xp({neg})` → "
          f"{calculate_level_from_xp(neg)} (safe)")
    print(f"  XP owed before Level 1: {xp_for_level(1)} - ({neg}) = "
          f"{xp_for_level(1) - neg} XP (the cumulative curve is unchanged)")
    print("  ledger: a decrease creates no crossing, so entitlements are "
          "untouched (see §6)\n")

    # ── 6. duplicate-reward exploit window, both shapes ───────────────────
    print("## 6. Can a penalty + re-earn cycle pay a level reward twice?\n")
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, 9004, 0, 0))

    async def crossing_cycle():
        await ensure_tables()
        steps = (("reach Level 1", 0, 150),
                 ("penalty below the threshold", 150, 90),
                 ("re-earn past it", 90, 260),
                 ("second penalty", 260, 80),
                 ("third crossing", 80, 300))
        created_rows = []
        for label, old, new in steps:
            before = len(rows("SELECT 1 FROM level_reward_claims "
                              "WHERE guild_id=? AND user_id=?", (GUILD, 9004)))
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                reported = await record_crossing(
                    db, GUILD, 9004, old, new, {}, source=SOURCE_SETXP)
                await db.commit()
            after = len(rows("SELECT 1 FROM level_reward_claims "
                             "WHERE guild_id=? AND user_id=?", (GUILD, 9004)))
            created_rows.append(after - before)
            print(f"  {label:<28} xp {old:>3} → {new:<3}  ledger rows added: "
                  f"{after - before}  (record_crossing reported {reported})")
        return created_rows

    created_rows = asyncio.run(crossing_cycle())
    claim_rows = rows("SELECT reward_level, track, status FROM "
                      "level_reward_claims WHERE guild_id=? AND user_id=?",
                      (GUILD, 9004))
    print(f"\n  claim ledger rows: {claim_rows}")
    print(f"  real rows added per step {created_rows}; the ledger holds "
          f"{len(claim_rows)} row(s) → re-crossing cannot pay twice "
          "(UNIQUE(guild_id,user_id,reward_level,track,reward_ref) + "
          "INSERT OR IGNORE).")
    print("  (`record_crossing` reports the identities it attempted, not the "
          "rows it inserted — it printed 1 for each re-crossing while the "
          "ledger stayed at one row. No production caller reads that value.)")
    print("  A reset-and-relevel is the same shape and is covered by the same "
          "constraint.\n")

    # ── 7. rank order with a negative XP member ────────────────────────────
    print("## 7. Leaderboard order with negative XP\n")
    print("| Position | Member | XP |")
    print("|---|---|---|")
    for index, (name, xp) in enumerate(
            sorted([("member A", 150), ("member B", 0), ("spammer", neg)],
                   key=lambda pair: pair[1], reverse=True), start=1):
        print(f"| {index} | {name} | {xp} |")
    print("\n`ORDER BY xp DESC` keeps working, but the spammer sorts below "
          "every member at 0 XP — a rank the UI never explains.\n")

    # ── 8. verdict ─────────────────────────────────────────────────────────
    print("## 8. Verdict\n")
    print("1. XP cannot go negative in the shipped model: every write path "
          "clamps or writes zero.")
    print("2. The shipped penalty already has a latent defect: it decrements "
          "`xp` without recomputing `levels.level`, so the stored level can "
          "disagree with `xp_progress(xp)` and the next legitimate grant "
          "visibly demotes the member without an announcement.")
    print("3. Removing the floor is rejected (D1 = B does not allow debt): it "
          "makes XP representable-negative, which breaks the `xp >= 0` "
          "invariant `xp_progress` and the rank card were written against (the "
          "`/setxp` hardening comment records that assumption) and renders a "
          "negative progress fraction.")
    print("4. Reward integrity is not at risk from either shape: crossings only "
          "count increases, and the claim ledger refuses a second row for the "
          "same (guild, member, level, track, ref).")
    print("5. Implemented (D1 = B): the penalty deducts real XP, floors at "
          "zero, recomputes `level` in the same transaction and touches no "
          "entitlement. A penalty can legitimately demote the member; that is "
          "not a crossing, so nothing is paid twice.")

    # ── 9. machine-checked assertions, so a silent drift fails the run ─────
    assert floor_trace == [0] * 12, floor_trace
    assert neg_trace == [-10, -20, -30], neg_trace
    assert xp_progress(-30)[:2] == (0, -30), xp_progress(-30)
    assert calculate_level_from_xp(-30) == 0
    assert len(claim_rows) == 1, claim_rows
    assert after[1] != derived_level, "expected the legacy write to be stale"
    assert shipped_level == derived_level == 1, (shipped_level, derived_level)
    assert not shipped_recomputed < shipped_level, "shipped write must not regress silently"
    print("\n[simulate_negative_xp] all assertions held — every number above is "
          "reproducible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
