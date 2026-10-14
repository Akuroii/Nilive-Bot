"""Slice 2: claim ownership is an atomic reserve, not a pending SELECT.

Run from the scripts directory:
    python test_slice2_level_claims.py
"""
import asyncio
import inspect
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from phase1_support import (
    GUILD, USER, DB_PATH, execute, reset_database, rows, seed_member, member,
)

OTHER = USER + 1


def level_of(user=USER):
    found = rows(
        "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
        (GUILD, user))
    return found[0] if found else None


def wallet(user=USER):
    return rows(
        "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
        (GUILD, user))[0]


def claims(user=USER):
    return rows(
        "SELECT reward_level, track, reward_ref, status, payload_json, "
        "owner_token FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY reward_level, track, reward_ref",
        (GUILD, user))


def claim_sources(user=USER):
    return rows(
        "SELECT reward_level, track, reward_ref, source "
        "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY reward_level, track, reward_ref",
        (GUILD, user))


def reward_side_effect_snapshot(user=USER):
    """State negative XP administration must not pay, revoke, or redefine."""
    return {
        "claims": rows(
            "SELECT * FROM level_reward_claims WHERE guild_id=? AND user_id=? ORDER BY id",
            (GUILD, user)),
        "wallet": rows(
            "SELECT * FROM economy WHERE guild_id=? AND user_id=?",
            (GUILD, user)),
        "ledger": rows(
            "SELECT * FROM transaction_ledger WHERE guild_id=? AND user_id=? ORDER BY id",
            (GUILD, user)),
        "inventory": rows(
            "SELECT * FROM inventory_items WHERE guild_id=? AND user_id=? ORDER BY id",
            (GUILD, user)),
        "active_boosts": rows(
            "SELECT * FROM leveling_active_boosts WHERE guild_id=? AND user_id=? ORDER BY id",
            (GUILD, user)),
        "role_definitions": rows(
            "SELECT * FROM leveling_rewards WHERE guild_id=? ORDER BY id",
            (GUILD,)),
        "currency_definitions": rows(
            "SELECT * FROM leveling_currency_rewards WHERE guild_id=? ORDER BY id",
            (GUILD,)),
        "shop_definitions": rows(
            "SELECT * FROM leveling_shop_rewards WHERE guild_id=? ORDER BY id",
            (GUILD,)),
        "boost_definitions": rows(
            "SELECT * FROM leveling_boost_rewards WHERE guild_id=? ORDER BY id",
            (GUILD,)),
    }


class Fail(Exception):
    pass


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(name)


def person(user=USER):
    who = member(user=user)
    role = SimpleNamespace(id=55, name="VIP", position=1)
    who.roles = []
    who.add_roles = AsyncMock()
    who.guild = SimpleNamespace(
        id=GUILD, me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
        get_role=lambda rid: role if int(rid) == 55 else None,
        get_member=lambda uid: who if uid == user else None)
    return who


def seed_currency(level=1, amount=100, currency="balance"):
    execute(
        "INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount) "
        "VALUES (?, ?, ?, ?)", (GUILD, level, currency, amount))


def seed_role(level=1, role_id=55):
    execute(
        "INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?, ?, ?)",
        (GUILD, level, role_id))


async def main():
    from cogs.leveling import Leveling
    from utils.level_claims import (
        LEASE_SECONDS, backfill_legacy_claims, claim_available, list_claims,
    )
    from utils.reward_engine import give_reward

    await reset_database()
    seed_member()
    execute("UPDATE levels SET xp=0, level=0, prestige=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE economy SET balance=0, diamonds=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    seed_currency(1, 100)
    # 100 XP is level 1. Crossing it must create one pending identity, not pay.
    result = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=100,
        reason="cross", source="test")
    check("cross creates one pending currency entitlement and does not pay",
          result.get("success") and wallet() == (0, 0)
          and len(claims()) == 1 and claims()[0][3] == "pending"
          and "100" in claims()[0][4],
          str(claims()))
    passive_sources = claim_sources()

    # 1-3. Two simultaneous claims: one owner, loser does not deliver,
    # double-click fulfills once.
    async def once():
        return await claim_available(GUILD, USER)

    first, second = await asyncio.gather(once(), once())
    owners = [item["owned"] for item in (first, second)]
    deliveries = [item["delivered_currency"] for item in (first, second)]
    check("1 two simultaneous claims have exactly one owner",
          sorted(owners) == [0, 1], str(owners))
    check("2 the losing attempt does not deliver",
          deliveries.count(0) == 1 and deliveries.count(1) == 1
          and wallet() == (100, 0), str(deliveries))
    again = await claim_available(GUILD, USER)
    check("3 double-click fulfills once",
          again["owned"] == 0 and again["delivered_currency"] == 0
          and wallet() == (100, 0) and claims()[0][3] == "fulfilled")

    # Active lease cannot be stolen. Hold the write lock, then release a
    # processing row whose lease is still live.
    execute("UPDATE level_reward_claims SET status='pending', owner_token=NULL, "
            "processing_started_at=NULL, fulfilled_at=NULL WHERE guild_id=?",
            (GUILD,))
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    claim_id = rows(
        "SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=?",
        (GUILD, USER))[0][0]
    locked = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        db = await aiosqlite.connect(DB_PATH, timeout=10)
        await db.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat()
        await db.execute("""
            UPDATE level_reward_claims
            SET status='processing', owner_token='owner-a',
                processing_started_at=?
            WHERE id=?
        """, (now, claim_id))
        locked.set()
        await release.wait()
        await db.commit()
        await db.close()

    holder = asyncio.create_task(hold())
    await locked.wait()
    loser = asyncio.create_task(claim_available(GUILD, USER))
    await asyncio.sleep(0.05)
    release.set()
    held = await loser
    await holder
    check("5 a second attempt cannot steal an active lease",
          held["owned"] == 0 and held["delivered_currency"] == 0
          and wallet() == (0, 0)
          and rows("SELECT owner_token, status FROM level_reward_claims WHERE id=?",
                   (claim_id,))[0] == ("owner-a", "processing"),
          str(held))

    # 4 and 6. Crash after reservation, then reclaim once the lease expires.
    old = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS + 5)).isoformat()
    execute("UPDATE level_reward_claims SET processing_started_at=? WHERE id=?",
            (old, claim_id))
    recovered = await claim_available(GUILD, USER)
    check("4 expired reservation is reclaimed and delivered once",
          recovered["owned"] == 1 and recovered["delivered_currency"] == 1
          and wallet() == (100, 0) and claims()[0][3] == "fulfilled")
    stolen = await claim_available(GUILD, USER)
    check("6 fulfilled ownership is not reclaimable",
          stolen["owned"] == 0 and wallet() == (100, 0))

    # 7. A credit that rolls back with the reservation cannot duplicate.
    execute("UPDATE level_reward_claims SET status='pending', owner_token=NULL, "
            "processing_started_at=NULL, fulfilled_at=NULL WHERE id=?", (claim_id,))
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    import utils.economy_safe as economy_safe
    real_credit = economy_safe.safe_credit
    calls = {"n": 0}

    async def explode(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("credit crashed")

    economy_safe.safe_credit = explode
    crashed = await claim_available(GUILD, USER)
    after_crash = wallet()
    economy_safe.safe_credit = real_credit
    retried = await claim_available(GUILD, USER)
    check("7 currency credit and fulfillment do not duplicate on retry",
          crashed["delivered_currency"] == 0 and after_crash == (0, 0)
          and retried["delivered_currency"] == 1 and wallet() == (100, 0)
          and calls["n"] == 1 and claims()[0][3] == "fulfilled",
          f"crashed={crashed} after={after_crash} retried={retried} "
          f"calls={calls['n']} wallet={wallet()}")

    # 8-9. Role failure stays retryable. A role already added is finalized
    # by calling add_roles again, once.
    seed_role(2, 55)
    execute("UPDATE levels SET xp=400, level=2 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    # 400 XP is level 2 (100 + 282). Force the pending role row.
    from utils.level_claims import record_crossing
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await record_crossing(
            db, GUILD, USER, 0, 400, {"balance": 1.0, "diamonds": 1.0},
            source="test")
        await db.commit()
    role_rows = [c for c in claims() if c[1] == "role"]
    check("role entitlement exists once", len(role_rows) == 1 and role_rows[0][3] == "pending",
          str(role_rows))
    who = person()

    async def deny_role(member, role_id, reason):
        raise RuntimeError("discord unavailable")

    import utils.level_claims as claims_mod
    real_deliver = claims_mod.deliver_role
    claims_mod.deliver_role = deny_role
    failed = await claim_available(GUILD, USER, member=who)
    claims_mod.deliver_role = real_deliver
    role_state = rows(
        "SELECT status, last_error FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=? AND track='role'", (GUILD, USER))[0]
    check("8 role delivery failure remains retryable",
          failed["delivered_roles"] == 0 and role_state[0] == "failed"
          and "discord unavailable" in (role_state[1] or ""),
          str(role_state))
    who.roles = [who.guild.get_role(55)]
    finalized = await claim_available(GUILD, USER, member=who)
    check("9 already-added role is finalized by a safe repeat add",
          finalized["delivered_roles"] == 1 and who.add_roles.await_count == 1
          and rows("SELECT status FROM level_reward_claims WHERE track='role' "
                   "AND guild_id=? AND user_id=?", (GUILD, USER))[0][0] == "fulfilled")
    second_role = await claim_available(GUILD, USER, member=who)
    check("9 retry after finalize does not add the role again",
          second_role["delivered_roles"] == 0 and who.add_roles.await_count == 1)

    # 10. /setxp and the dashboard member edit each create one identity.
    execute("DELETE FROM level_reward_claims")
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    cog = Leveling.__new__(Leveling)
    cog.bot = SimpleNamespace(get_guild=lambda gid: who.guild)
    ix = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        response=SimpleNamespace(send_message=AsyncMock()))
    await Leveling.setxp.callback(cog, ix, who, 400)
    after_set = claims()
    await Leveling.setxp.callback(cog, ix, who, 400)
    check("10 /setxp creates exactly one entitlement per reward identity",
          len(after_set) == len(claims()) and len(claims()) >= 2
          and len({(c[0], c[1], c[2]) for c in claims()}) == len(claims())
          and all(c[3] == "pending" for c in claims())
          and wallet()[0] == 100,
          str(claims()))

    from utils.xp_calculator import xp_progress
    before_negative_set = level_of()
    set_side_effects_before = reward_side_effect_snapshot()
    await Leveling.setxp.callback(cog, ix, who, -123)
    after_negative_set = level_of()
    check("negative /setxp clamps the existing member to zero without reward side effects",
          before_negative_set is not None and before_negative_set[0] > 0
          and after_negative_set == (0, xp_progress(0)[0])
          and reward_side_effect_snapshot() == set_side_effects_before,
          f"before={before_negative_set} after={after_negative_set}")
    setxp_sources = claim_sources()

    import dashboard.app as dashboard
    app = dashboard.app
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
            "VALUES (?,?,'admin',1)", (GUILD, USER))
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user={"id": USER, "username": "Phase One", "avatar": None},
                       guild_id=GUILD, expires_at=time.time() + 7200,
                       csrf_token="phase1-csrf")
    before_dash = len(claims())
    response = client.post(
        "/api/edit-member",
        json={"user_id": USER, "xp": 400, "coins": 0, "diamonds": 0},
        headers={"X-CSRF-Token": "phase1-csrf"})
    check("10 dashboard XP edit does not duplicate entitlement identities",
          response.status_code < 400 and len(claims()) == before_dash
          and wallet()[0] == 0,
          f"status={response.status_code} body={response.get_data(as_text=True)[:300]} "
          f"claims={claims()}")

    # A dashboard increase across a new configured reward creates exactly one.
    seed_currency(3, 40)
    response = client.post(
        "/api/edit-member",
        json={"user_id": USER, "xp": 1000, "coins": 0, "diamonds": 0},
        headers={"X-CSRF-Token": "phase1-csrf"})
    created = [c for c in claims() if c[0] == 3]
    check("10 dashboard crossing creates one entitlement for the new identity",
          response.status_code < 400 and len(created) == 1
          and created[0][3] == "pending" and wallet()[0] == 0,
          str(created))

    before_negative_dashboard = level_of()
    dashboard_side_effects_before = reward_side_effect_snapshot()
    response = client.post(
        "/api/edit-member",
        json={"user_id": USER, "xp": -987, "coins": 0, "diamonds": 0},
        headers={"X-CSRF-Token": "phase1-csrf"})
    after_negative_dashboard = level_of()
    check("negative Dashboard member edit clamps existing XP without reward or claim side effects",
          response.status_code == 200
          and response.get_json() == {"success": True}
          and before_negative_dashboard is not None and before_negative_dashboard[0] > 0
          and after_negative_dashboard == (0, xp_progress(0)[0])
          and reward_side_effect_snapshot() == dashboard_side_effects_before,
          f"status={response.status_code} before={before_negative_dashboard} "
          f"after={after_negative_dashboard}")
    dashboard_sources = claim_sources()

    # 11. Reset keeps the rows. Re-leveling does not create a second identity.
    kept = claims()
    await Leveling.resetxp.callback(cog, ix, who)
    check("11 reset does not delete claim rows",
          claims() == kept and level_of() == (0, 0))
    await Leveling.setxp.callback(cog, ix, who, 1000)
    check("11 reset then re-level does not duplicate entitlements",
          len(claims()) == len(kept)
          and len({(c[0], c[1], c[2]) for c in claims()}) == len(claims()))

    # 12. Legacy backfill fulfills reached rewards and pays nothing.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM level_claim_migrations")
    execute("UPDATE economy SET balance=0, diamonds=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE levels SET xp=1000, level=1 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    inserted = await backfill_legacy_claims()
    backed = claims()
    check("12 legacy backfill fulfills reached rewards without paying",
          inserted > 0 and backed and all(c[3] == "fulfilled" for c in backed)
          and wallet() == (0, 0)
          and await backfill_legacy_claims() == 0,
          str(backed))
    from utils.level_claims import SOURCE_BACKFILL, SOURCE_DASHBOARD, SOURCE_SETXP
    check("entitlement source is preserved for passive and admin crossings",
          passive_sources == [(1, "currency", "balance", "test")]
          and setxp_sources
          and {row[3] for row in setxp_sources} == {SOURCE_SETXP}
          and dashboard_sources
          and {row[3] for row in dashboard_sources if row[0] == 3} == {SOURCE_DASHBOARD}
          and {row[3] for row in dashboard_sources if row[0] != 3} == {SOURCE_SETXP}
          and claim_sources()
          and {row[3] for row in claim_sources()} == {SOURCE_BACKFILL},
          f"passive={passive_sources} setxp={setxp_sources} "
          f"dashboard={dashboard_sources} backfill={claim_sources()}")

    # Stored level drift must not hide a real xp_progress crossing.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_currency_rewards")
    execute("DELETE FROM leveling_rewards")
    seed_currency(1, 15)
    execute("UPDATE levels SET xp=0, level=9 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    drifted = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=100,
        reason="drift", source="test")
    check("eligibility uses xp_progress, not the stored level",
          drifted.get("success") and any(c[0] == 1 and c[3] == "pending"
                                         for c in claims()),
          str(claims()))

    execute("DELETE FROM leveling_currency_rewards")
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    paid = await claim_available(GUILD, USER)
    check("deleted definition still pays the frozen snapshot",
          paid["delivered_currency"] == 1 and wallet() == (15, 0)
          and claims()[0][3] == "fulfilled")

    execute("INSERT INTO leveling_config (guild_id, message_xp_enabled) VALUES (?, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET message_xp_enabled=0", (GUILD,))
    before_off = claims()
    xp_before_off = level_of()[0]
    direct = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=1,
        reason="independent source", source="mission")
    check("Message XP OFF does not gate mission/event XP grants",
          direct.get("success") and claims() == before_off
          and level_of()[0] == xp_before_off + 1)

    rank_source = inspect.getsource(Leveling.rank.callback)
    check("/rank was not given claim behavior",
          "claim_available" not in rank_source and "level_reward_claims" not in rank_source)
    level_source = inspect.getsource(Leveling.level.callback)
    check("/level passes ephemeral itself",
          "ephemeral=True" in level_source)

    print("ALL SLICE 2 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
