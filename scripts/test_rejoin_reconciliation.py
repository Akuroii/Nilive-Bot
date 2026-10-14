"""Automatic Level-role membership reconciliation on the real join callback.

The callback is driven directly with fake Discord members/guilds and a real
SQLite claim ledger. It must restore already-earned role membership without
claiming/paying again, obey remove_old_reward_role, tolerate partial delivery,
and preserve lower roles until every highest-Level role is present.

Fake Discord callbacks are runtime integration tests, not live Discord tests.
Run with:
    python scripts/test_rejoin_reconciliation.py
"""
import asyncio
import io
from contextlib import redirect_stdout
from types import SimpleNamespace

import aiosqlite

from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows
from test_level_reward_role_progression import (
    MANUAL_ROLE, ROLE_IDS, SECOND_LEVEL10_ROLE, UNRELATED_ROLE,
    FakeMember, guild_for, seed_role_reward, set_exclusive, xp_to,
)


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def claims():
    return rows("SELECT reward_level,track,reward_ref,payload_json,status,source "
                "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                "ORDER BY reward_level,track,reward_ref", (GUILD, USER))


def member_state():
    return rows("SELECT xp,level,prestige FROM levels WHERE guild_id=? AND user_id=?",
                (GUILD, USER))


def durable_snapshot():
    return {
        "levels": rows("SELECT * FROM levels WHERE guild_id=? AND user_id=?",
                        (GUILD, USER)),
        "economy": rows("SELECT * FROM economy WHERE guild_id=? AND user_id=?",
                         (GUILD, USER)),
        "claims": claims(),
        "roles": rows("SELECT * FROM leveling_rewards WHERE guild_id=? ORDER BY id",
                       (GUILD,)),
        "ledger": rows("SELECT * FROM transaction_ledger WHERE guild_id=? AND user_id=? "
                        "ORDER BY id", (GUILD, USER)),
    }


async def cross(old_xp, new_xp):
    from utils.level_claims import record_crossing
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        created = await record_crossing(
            db, GUILD, USER, old_xp, new_xp, {}, source="rejoin-test")
        await db.commit()
    return created


def leveling_cog():
    from cogs.leveling import Leveling
    cog = Leveling.__new__(Leveling)
    cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}
    cog._spam_incidents = {}
    cog._spam_warn_times = {}
    return cog


def save_dashboard_role_setting(value):
    """Save remove_old_reward_role through the real API into the test DB."""
    from flask import Flask
    from unittest.mock import patch
    from dashboard.api import api_bp
    import dashboard.api.leveling as leveling_api
    import dashboard.auth as auth
    import dashboard.permissions as permissions

    app = Flask("rejoin-role-setting-test")
    app.secret_key = "test-only-rejoin-secret"
    app.register_blueprint(api_bp, url_prefix="/api")
    client = app.test_client()

    async def admin_permission(_guild, _user):
        return "admin"

    csrf = "rejoin-role-setting-csrf"
    with client.session_transaction() as session:
        session["user"] = {"id": USER + 500, "username": "rejoin-test"}
        session["guild_id"] = GUILD
        session["csrf_token"] = csrf
    with patch.object(auth, "is_session_valid", return_value=True), \
         patch.object(auth, "refresh_session_if_needed", return_value=None), \
         patch.object(permissions, "_get_permission_level", new=admin_permission), \
         patch.object(leveling_api, "log_action", return_value=None):
        response = client.post(
            "/api/leveling/config",
            json={"remove_old_reward_role": int(bool(value))},
            headers={"X-CSRF-Token": csrf})
    return response


async def prepare_progression(role_definitions):
    await reset_database()
    from utils.level_claims import ensure_tables
    await ensure_tables()
    for level, role_id in role_definitions:
        seed_role_reward(level, role_id)
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,0)",
            (GUILD, USER, xp_to(11), 11))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,1234,56)",
            (GUILD, USER))
    old_xp = 0
    await cross(old_xp, xp_to(11))
    # Treat these as rewards already delivered in the past. Rejoin must not
    # alter their status or replay any reward-side database writes.
    execute("UPDATE level_reward_claims SET status='fulfilled',fulfilled_at='fixture' "
            "WHERE guild_id=? AND user_id=?", (GUILD, USER))


async def main():
    from cogs.leveling import Leveling

    # 1. Automatic reconciliation restores the accumulated role set while
    # remove_old_reward_role is OFF, without depending on Claim All.
    print("== OFF: restore every earned role and retain the accumulated set ==")
    await prepare_progression([(5, ROLE_IDS[5]), (10, ROLE_IDS[10])])
    off_saved = save_dashboard_role_setting(False)
    check("Dashboard/API stores replacement OFF before rejoin runtime",
          off_saved.status_code == 200
          and off_saved.get_json().get("success") is True
          and rows("SELECT remove_old_reward_role FROM leveling_config "
                   "WHERE guild_id=?", (GUILD,)) == [(0,)])
    check("rejoin fixture has real, nonzero XP and Level progress",
          member_state() == [(xp_to(11), 11, 0)], str(member_state()))
    guild = guild_for(list(ROLE_IDS.values()))
    member = FakeMember(guild, [guild.get_role(UNRELATED_ROLE),
                                guild.get_role(MANUAL_ROLE)])
    member.id = USER
    member.bot = False
    before = durable_snapshot()
    await Leveling.on_member_join(leveling_cog(), member)
    check("join callback restores all fulfilled roles without Claim All",
          ROLE_IDS[5] in member.held() and ROLE_IDS[10] in member.held()
          and member.added == [ROLE_IDS[5], ROLE_IDS[10]],
          f"held={member.held()}, added={member.added}")
    check("replacement OFF keeps every accumulated role and unrelated roles",
          member.removed == [] and UNRELATED_ROLE in member.held()
          and MANUAL_ROLE in member.held(),
          f"held={member.held()}, removed={member.removed}")
    check("join changes only Discord role membership, not XP/economy/claims/rewards/ledger",
          durable_snapshot() == before,
          f"before={before}, after={durable_snapshot()}")
    await Leveling.on_member_join(leveling_cog(), member)
    check("rejoin reconciliation is idempotent for already-held roles",
          member.added == [ROLE_IDS[5], ROLE_IDS[10]] and member.removed == []
          and durable_snapshot() == before)

    # 2. Replacement ON adds the highest role first, then removes only the
    # superseded fulfilled Level role. Same ledger, no second payment.
    print("== ON: restore highest role and replace lower reward roles ==")
    await prepare_progression([(5, ROLE_IDS[5]), (10, ROLE_IDS[10])])
    on_saved = save_dashboard_role_setting(True)
    check("Dashboard/API stores replacement ON before rejoin runtime",
          on_saved.status_code == 200
          and on_saved.get_json().get("success") is True
          and rows("SELECT remove_old_reward_role FROM leveling_config "
                   "WHERE guild_id=?", (GUILD,)) == [(1,)])
    guild = guild_for(list(ROLE_IDS.values()))
    member = FakeMember(guild, [guild.get_role(ROLE_IDS[5]),
                                guild.get_role(UNRELATED_ROLE),
                                guild.get_role(MANUAL_ROLE)])
    member.id = USER
    member.bot = False
    before = durable_snapshot()
    await Leveling.on_member_join(leveling_cog(), member)
    check("replacement ON restores L10 and removes the superseded L5 role",
          member.held() == sorted([ROLE_IDS[10], UNRELATED_ROLE, MANUAL_ROLE])
          and member.added == [ROLE_IDS[10]]
          and member.removed == [ROLE_IDS[5]],
          f"held={member.held()}, added={member.added}, removed={member.removed}")
    check("replacement preserves all claim/reward rows and economic state",
          durable_snapshot() == before)
    member.added.clear()
    member.removed.clear()
    await Leveling.on_member_join(leveling_cog(), member)
    check("replacement ON rejoin is idempotent after the highest role is restored",
          member.held() == sorted([ROLE_IDS[10], UNRELATED_ROLE, MANUAL_ROLE])
          and member.added == [] and member.removed == []
          and durable_snapshot() == before)

    # A deleted Discord role is reported without losing the lower role or
    # mutating the fulfilled claim; restoring the role makes the next join retry.
    print("== missing highest role then rejoin retry ==")
    await prepare_progression([(5, ROLE_IDS[5]), (10, ROLE_IDS[10])])
    save_dashboard_role_setting(True)
    catalog = {ROLE_IDS[5]: guild_for([ROLE_IDS[5]]).get_role(ROLE_IDS[5]),
               UNRELATED_ROLE: guild_for([UNRELATED_ROLE]).get_role(UNRELATED_ROLE)}
    guild = SimpleNamespace(
        id=GUILD,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_role=lambda role_id: catalog.get(int(role_id)),
    )
    member = FakeMember(guild, [catalog[ROLE_IDS[5]]])
    member.id = USER
    member.bot = False
    before = durable_snapshot()
    join_log = io.StringIO()
    with redirect_stdout(join_log):
        await Leveling.on_member_join(leveling_cog(), member)
    check("missing highest Discord role is reported for retry",
          "[LEVEL ROLE REJOIN]" in join_log.getvalue()
          and "Role no longer exists" in join_log.getvalue(),
          join_log.getvalue().strip())
    check("missing highest Discord role does not crash, remove L5, or mutate claims",
          member.held() == [ROLE_IDS[5]] and member.removed == []
          and durable_snapshot() == before)
    catalog[ROLE_IDS[10]] = guild_for([ROLE_IDS[10]]).get_role(ROLE_IDS[10])
    await Leveling.on_member_join(leveling_cog(), member)
    check("restoring the missing Discord role lets rejoin retry and replace L5",
          ROLE_IDS[10] in member.held() and ROLE_IDS[5] not in member.held()
          and member.added == [ROLE_IDS[10]] and member.removed == [ROLE_IDS[5]]
          and durable_snapshot() == before)

    # 3. Highest-Level group with multiple same-Level rewards: simulate a
    # partial Discord add failure. No lower role may be removed until the retry
    # delivers every required role. All claims were already fulfilled.
    print("== partial highest-Level delivery failure then retry ==")
    await prepare_progression([
        (5, ROLE_IDS[5]), (10, ROLE_IDS[10]), (10, SECOND_LEVEL10_ROLE)])
    on_saved = save_dashboard_role_setting(True)
    check("Dashboard/API keeps replacement ON for partial-delivery retry",
          on_saved.status_code == 200
          and rows("SELECT remove_old_reward_role FROM leveling_config "
                   "WHERE guild_id=?", (GUILD,)) == [(1,)])
    guild = guild_for(list(ROLE_IDS.values()) + [SECOND_LEVEL10_ROLE])
    member = FakeMember(guild, [guild.get_role(ROLE_IDS[5]),
                                guild.get_role(UNRELATED_ROLE)])
    member.id = USER
    member.bot = False
    member.fail_add = lambda role: (
        (_ for _ in ()).throw(RuntimeError("simulated Discord add-role failure"))
        if role.id == SECOND_LEVEL10_ROLE else None)
    before = durable_snapshot()
    await Leveling.on_member_join(leveling_cog(), member)
    check("partial highest-Level delivery retains the lower role",
          ROLE_IDS[10] in member.held() and SECOND_LEVEL10_ROLE not in member.held()
          and ROLE_IDS[5] in member.held() and member.removed == [],
          f"held={member.held()}, removed={member.removed}")
    check("a failed highest-role add does not mutate claims or pay/revoke rewards",
          durable_snapshot() == before,
          f"claims={claims()}, before={before['claims']}")

    member.fail_add = None
    member.added.clear()
    await Leveling.on_member_join(leveling_cog(), member)
    check("a later join/reconciliation retries only the missing same-Level role",
          SECOND_LEVEL10_ROLE in member.held()
          and ROLE_IDS[10] in member.held()
          and member.added == [SECOND_LEVEL10_ROLE]
          and ROLE_IDS[5] not in member.held()
          and member.removed == [ROLE_IDS[5]],
          f"held={member.held()}, added={member.added}, removed={member.removed}")
    check("retry leaves both same-Level rewards fulfilled and all persistent data unchanged",
          durable_snapshot() == before
          and [(row[0], row[1], row[4]) for row in claims()]
          == [(5, "role", "fulfilled"),
              (10, "role", "fulfilled"), (10, "role", "fulfilled")],
          f"claims={claims()}")
    member.added.clear()
    member.removed.clear()
    await Leveling.on_member_join(leveling_cog(), member)
    check("a successful partial-retry reconciliation is idempotent",
          member.added == [] and member.removed == []
          and durable_snapshot() == before)

    # Automatic join reconciliation is separate from the existing Claim All
    # contract: a targeted/headless pass must not become a broad role sweep.
    print("== targeted/headless Claim All passes stay scoped ==")
    await prepare_progression([(5, ROLE_IDS[5]), (10, ROLE_IDS[10])])
    save_dashboard_role_setting(True)
    guild = guild_for(list(ROLE_IDS.values()))
    scoped_member = FakeMember(guild, [])
    scoped_member.id = USER
    scoped_member.bot = False
    before = durable_snapshot()
    from utils.level_claims import claim_available
    target = rows("SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                  "AND track='role' ORDER BY reward_level LIMIT 1", (GUILD, USER))[0][0]
    targeted = await claim_available(
        GUILD, USER, member=scoped_member, claim_ids=[target])
    check("targeted claim pass does not reconcile unrelated fulfilled roles",
          scoped_member.held() == []
          and targeted["reconciled"]["restored"] == []
          and durable_snapshot() == before)
    headless = await claim_available(GUILD, USER)
    check("headless full pass does not reconcile or mutate fulfilled role claims",
          headless["reconciled"]["restored"] == []
          and headless["reconciled"]["failed"] == []
          and durable_snapshot() == before)

    # Claim All keeps explicit, user-facing reporting for a no-op, a restored
    # role, and reconciliation failures distinct from claim-delivery failures.
    from cogs.leveling import claim_result_footer
    plain = claim_result_footer({"fulfilled": [], "failed": []})
    restored = claim_result_footer({
        "fulfilled": [1], "failed": [],
        "reconciled": {"restored": [ROLE_IDS[10]], "removed": [], "failed": []},
    })
    failed = claim_result_footer({
        "fulfilled": [], "failed": [7],
        "reconciled": {"restored": [], "removed": [],
                        "failed": [(ROLE_IDS[10], "Role no longer exists")]},
    })
    check("ordinary Claim All footer retains its base count wording",
          plain == "Claimed 0. Retryable failures: 0.", plain)
    check("Claim All footer reports a restored role separately from paid claims",
          restored == "Claimed 1. Retryable failures: 0. Level role restored: 1.",
          restored)
    check("Claim All footer reports reconciliation failures separately from claim failures",
          failed == ("Claimed 0. Retryable failures: 1. "
                    "Level role reconciliation failure(s): 1."), failed)

    print("ALL AUTOMATIC REJOIN RECONCILIATION CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
