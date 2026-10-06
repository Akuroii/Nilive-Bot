"""Rejoin persistence + explicit reconciliation of a missing Level role (D2 = B).

Locks down the two decisions:

1. **Leave/rejoin is not an event.** XP/level live on one `(guild_id, user_id)`
   row, no production path deletes it, and no member-lifecycle listener touches
   level state — so rejoining cannot reset, recreate or re-derive progress. A
   normal chat message must never fake a level crossing to hand a role back:
   the message path reaches the ledger only through `give_reward()` →
   `record_crossing()`, which refunds nothing and delivers nothing.

2. **Restoring a missing role is an explicit reconciliation** through the
   existing claim path, and only when `remove_old_reward_role` is ON:

   * it happens on the member's own Claim All pass (never on a message, never
     on a timer, never in the background);
   * only the **highest** applicable fulfilled Level reward role is restored —
     never every historical role;
   * with exclusivity ON the lower fulfilled roles the member still wears are
     taken off by the existing progression rule, so the end state is the
     highest role and nothing else;
   * unrelated roles are never touched;
   * the claim ledger is read-only for this: no row is inserted, no status is
     changed, so a fulfilled claim stays exactly one fulfilled claim;
   * with the toggle OFF a role that is gone stays gone (roles accumulate by
     design, so there is nothing to reconcile).

Run from the scripts directory:
    python test_rejoin_reconciliation.py
"""
import asyncio
import re
import sys
from pathlib import Path

from phase1_support import DB_PATH, GUILD, ROOT, USER, execute, reset_database, rows

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse the progression test's fixtures so both files agree on what a member,
# a guild role catalog and a level crossing look like.
from test_level_reward_role_progression import (  # noqa: E402
    MANUAL_ROLE, ROLE_IDS, UNRELATED_ROLE, FakeMember, guild_for, seed_role_reward,
    set_exclusive, xp_to,
)


async def cross(old_xp, new_xp):
    import aiosqlite
    from utils.level_claims import record_crossing
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        created = await record_crossing(db, GUILD, USER, old_xp, new_xp, {},
                                        source="test")
        await db.commit()
    return created


def ledger_snapshot():
    return rows("SELECT id, reward_level, track, status FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=? ORDER BY reward_level, track",
                (GUILD, USER))


def level_state():
    found = rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, USER))
    return found[0] if found else (0, 0)


def source(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


async def main():
    from utils.level_claims import claim_available, ensure_tables

    await reset_database()
    await ensure_tables()

    checks = []

    def check(name, ok, extra=""):
        checks.append((name, ok, extra))
        print(f"  {'PASS' if ok else 'FAIL'} {name}"
              + (f"  [{extra}]" if extra and not ok else ""))

    # ── 1. rejoin cannot touch progress ────────────────────────────────────
    print("== 1. XP/level identity survives leave/rejoin ==")
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, USER, xp_to(7) + 123, 7))
    before = level_state()
    check("fixture: member has real progress", before == (xp_to(7) + 123, 7),
          str(before))

    import database as database_mod
    schema = Path(database_mod.__file__).read_text(encoding="utf-8")
    check("the levels row is keyed by (guild_id, user_id)",
          "PRIMARY KEY (guild_id, user_id)" in schema)

    deletes = [path.name for path in (ROOT / "cogs").glob("*.py")
               if "DELETE FROM levels" in path.read_text(encoding="utf-8")]
    check("no cog deletes a member's levels row", deletes == [], str(deletes))

    lifecycle = {}
    for name in ("welcome.py", "tagpartners.py", "boost.py", "reactionroles.py",
                 "auditlog.py"):
        text = source(f"cogs/{name}")
        if "on_member_join" not in text and "on_member_remove" not in text:
            continue
        lifecycle[name] = sum(text.count(token) for token in
                              ("FROM levels", "INTO levels", "UPDATE levels",
                               "level_reward_claims"))
    check("no member-lifecycle listener touches level state",
          set(lifecycle.values()) == {0}, str(lifecycle))

    check("progress is unchanged by the (no-op) rejoin",
          level_state() == before, str(level_state()))

    # ── 2. message path can never fake a crossing for a role ─────────────
    print("== 2. a chat message cannot fake a crossing ==")
    cog_text = source("cogs/leveling.py")
    check("the message cog never adds or removes roles",
          not re.search(r"\badd_roles\b|\bremove_roles\b", cog_text))
    check("the message cog never calls the role-delivery helper",
          "deliver_role" not in cog_text)
    check("the reconciler is only reachable from the claim module",
          all("reconcile_role_progression" not in source(str(path.relative_to(ROOT)))
              for path in list((ROOT / "cogs").glob("*.py"))
              + list((ROOT / "utils").glob("*.py"))
              + [ROOT / "dashboard" / "app.py"]
              if path.name != "level_claims.py"))

    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from cogs.leveling import Leveling
    leveling = Leveling.__new__(Leveling)
    leveling.bot = SimpleNamespace(get_guild=lambda gid: None)
    leveling._xp_cooldowns, leveling._spam_tracker, leveling._spam_warn_times = {}, {}, {}
    guild = guild_for(list(ROLE_IDS.values()))
    who = FakeMember(guild, [])
    who.roles = []
    message = SimpleNamespace(
        author=SimpleNamespace(id=USER, bot=False, mention=f"<@{USER}>", roles=[]),
        guild=guild, channel=SimpleNamespace(send=AsyncMock()), content="hello there")
    xp_before = level_state()
    claims_before = ledger_snapshot()
    await Leveling.on_activity_message(leveling, message, 3)
    got_xp, got_level = level_state()
    check("the message granted XP the normal way (min 5 per message)",
          got_xp == xp_before[0] + 5 and got_level == xp_before[1],
          f"{xp_before} -> {(got_xp, got_level)}")
    check("the message delivered no role", who.added == [] and who.held() == [],
          f"added={who.added}")
    check("the message created no claim rows", ledger_snapshot() == claims_before,
          str(ledger_snapshot()))

    # ── 3. fixture: fulfilled claims whose roles are missing ─────────────
    print("== 3. a missing fulfilled Level role, toggle OFF ==")
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_rewards")
    seed_role_reward(5, ROLE_IDS[5])
    seed_role_reward(10, ROLE_IDS[10])
    await cross(0, xp_to(11))                    # crosses L5 and L10
    execute("UPDATE level_reward_claims SET status='fulfilled'")
    execute("UPDATE levels SET xp=?, level=7 WHERE guild_id=? AND user_id=?",
            (xp_to(11), GUILD, USER))
    fulfilled = ledger_snapshot()
    check("fixture: L5 + L10 claims are fulfilled",
          [(row[1], row[3]) for row in fulfilled] == [(5, "fulfilled"), (10, "fulfilled")],
          str(fulfilled))

    set_exclusive(False)
    member = FakeMember(guild, [guild.get_role(UNRELATED_ROLE),
                                guild.get_role(MANUAL_ROLE)])
    result = await claim_available(GUILD, USER, member=member)
    check("toggle OFF restores nothing",
          member.added == [] and result["reconciled"] == {"restored": [],
                                                          "removed": [],
                                                          "failed": []},
          f"added={member.added} reconciled={result['reconciled']}")
    check("toggle OFF leaves the claim ledger untouched",
          ledger_snapshot() == fulfilled, str(ledger_snapshot()))
    check("toggle OFF leaves unrelated roles alone",
          member.held() == sorted([UNRELATED_ROLE, MANUAL_ROLE]), str(member.held()))

    # ── 4. toggle ON: the highest fulfilled role comes back ───────────────
    print("== 4. toggle ON reconciles the highest applicable role ==")
    set_exclusive(True)
    member = FakeMember(guild, [guild.get_role(UNRELATED_ROLE),
                                guild.get_role(MANUAL_ROLE)])
    result = await claim_available(GUILD, USER, member=member)
    check("only the highest fulfilled Level role is restored",
          member.added == [ROLE_IDS[10]], str(member.added))
    check("no historical lower role was restored",
          ROLE_IDS[5] not in member.added and ROLE_IDS[5] not in member.held(),
          str(member.added))
    check("the restored role is reported",
          result["reconciled"]["restored"] == [ROLE_IDS[10]],
          str(result["reconciled"]))
    check("unrelated roles survived", member.held() == sorted(
        [ROLE_IDS[10], UNRELATED_ROLE, MANUAL_ROLE]), str(member.held()))
    check("the claim ledger is unchanged (no row added, none re-opened)",
          ledger_snapshot() == fulfilled, str(ledger_snapshot()))

    print("== 5. the reconciliation is idempotent ==")
    member.removed, member.added = [], []
    second = await claim_available(GUILD, USER, member=member)
    check("a second pass adds nothing",
          member.added == [] and second["reconciled"]["restored"] == [],
          f"added={member.added} reconciled={second['reconciled']}")
    check("a second pass removes nothing",
          member.removed == [] and member.held() == sorted(
              [ROLE_IDS[10], UNRELATED_ROLE, MANUAL_ROLE]), str(member.removed))
    check("the ledger still holds exactly the two fulfilled rows",
          ledger_snapshot() == fulfilled, str(ledger_snapshot()))

    # ── 6. an older role still worn is replaced as usual ─────────────────
    print("== 6. a worn lower role is replaced, not stacked ==")
    member = FakeMember(guild, [guild.get_role(ROLE_IDS[5]),
                                guild.get_role(UNRELATED_ROLE)])
    result = await claim_available(GUILD, USER, member=member)
    check("the missing highest role is restored and the lower one removed",
          ROLE_IDS[10] in member.held() and ROLE_IDS[5] not in member.held(),
          f"held={member.held()} removed={member.removed}")
    check("exactly the superseded role was removed", member.removed == [ROLE_IDS[5]],
          str(member.removed))
    check("the unrelated role survived", UNRELATED_ROLE in member.held(),
          str(member.held()))
    check("the ledger is still untouched by reconciliation",
          ledger_snapshot() == fulfilled, str(ledger_snapshot()))

    # ── 7. a role deleted from the guild fails softly ────────────────────
    print("== 7. a deleted reward role fails without side effects ==")
    # the guild knows L5's role but the L10 role is gone from Discord (and the
    # reward row is gone from config too: the reconciler reads the ledger)
    orphan_guild = guild_for([ROLE_IDS[5]])
    execute("DELETE FROM leveling_rewards WHERE level=10")
    member = FakeMember(orphan_guild, [orphan_guild.get_role(UNRELATED_ROLE)])
    result = await claim_available(GUILD, USER, member=member)
    check("a missing Discord role is reported as a retryable failure, not a crash",
          len(result["reconciled"]["failed"]) == 1
          and ROLE_IDS[5] not in member.held(),
          f"reconciled={result['reconciled']} held={member.held()}")
    check("the failed restore leaves the other roles untouched",
          member.held() == [UNRELATED_ROLE] and member.removed == [],
          str(member.held()))
    check("the claim ledger survived the failed restore",
          ledger_snapshot() == fulfilled, str(ledger_snapshot()))

    # ── 8. the member is told what happened ──────────────────────────────
    print("== 8. the Claim All footer reports the reconciliation ==")
    from cogs.leveling import claim_result_footer
    plain = claim_result_footer({"fulfilled": [], "failed": []})
    restored = claim_result_footer(
        {"fulfilled": [1], "failed": [], "reconciled": {"restored": [11],
                                                        "removed": [], "failed": []}})
    broken = claim_result_footer(
        {"fulfilled": [], "failed": [7], "reconciled": {"restored": [],
                                                       "removed": [],
                                                       "failed": [(12, "gone")]}})
    check("an ordinary claim keeps the original wording",
          plain == "Claimed 0. Retryable failures: 0.", plain)
    check("a restored role is reported even though no claim was fulfilled",
          restored == "Claimed 1. Retryable failures: 0. Level role restored: 1.",
          restored)
    check("a role that could not be restored is reported too",
          broken == ("Claimed 0. Retryable failures: 1. "
                     "Level role that could not be restored: 1."), broken)

    # ── 9. only member-initiated full passes reconcile ───────────────────
    print("== 9. reconciliation only on a member-initiated full pass ==")
    target = ledger_snapshot()[0][0]
    member = FakeMember(guild, [])
    targeted = await claim_available(GUILD, USER, member=member,
                                     claim_ids=[target])
    check("a targeted claim pass does not reconcile",
          member.added == [] and targeted["reconciled"]["restored"] == [],
          f"added={member.added} reconciled={targeted['reconciled']}")
    member = FakeMember(guild, [])
    headless = await claim_available(GUILD, USER)
    check("a pass without a member does not reconcile and does not raise",
          headless["reconciled"]["restored"] == [], str(headless["reconciled"]))
    check("no pass wrote to the ledger", ledger_snapshot() == fulfilled,
          str(ledger_snapshot()))

    failed = [c for c in checks if not c[1]]
    print(f"\nREJOIN / RECONCILIATION: {len(checks) - len(failed)} passed, "
          f"{len(failed)} failed")
    for name, _, extra in failed:
        print("  FAILED:", name, extra)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
