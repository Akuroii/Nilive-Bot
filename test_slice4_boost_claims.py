"""Slice 4: one level XP boost entitlement is one boost row.

Run from the scripts directory:
    python test_slice4_boost_claims.py
"""
import asyncio
import inspect
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import (
    GUILD, USER, execute, reset_database, rows, seed_member, member,
)


class Fail(Exception):
    pass


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(name)


def boosts(source=None):
    if source is None:
        return rows(
            "SELECT multiplier, source, expires_at "
            "FROM leveling_active_boosts WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    return rows(
        "SELECT multiplier, expires_at, source FROM leveling_active_boosts "
        "WHERE guild_id=? AND user_id=? AND source=?",
        (GUILD, USER, source))


def claims():
    return rows(
        "SELECT reward_level, track, reward_ref, status, payload_json "
        "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY id",
        (GUILD, USER))


async def main():
    from cogs.leveling import Leveling, _level_embed
    from utils.level_claims import LEASE_SECONDS, claim_available
    from utils.reward_engine import give_reward
    from utils.xp_calculator import (
        calculate_message_xp, calculate_voice_xp, xp_for_level, xp_progress,
    )

    await reset_database()
    seed_member()
    execute("UPDATE levels SET xp=0, level=0, prestige=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE economy SET balance=500, diamonds=7 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute(
        "INSERT INTO leveling_boost_rewards (guild_id, level, multiplier, duration_hours) "
        "VALUES (?, 1, 2, 12)", (GUILD,))
    execute(
        "INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount) "
        "VALUES (?, 1, 'balance', 15)", (GUILD,))
    execute(
        "INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?, 1, 55)",
        (GUILD,))
    item = execute(
        "INSERT INTO shop_items (guild_id, name, price, type, max_stock, current_stock) "
        "VALUES (?, 'Satchel', 10, 'custom', 4, 4)", (GUILD,))
    execute(
        "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity) "
        "VALUES (?, 1, ?, 1)", (GUILD, item))
    crossed = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=100,
        reason="cross", source="test")
    pending = [c for c in claims() if c[1] == "boost"]
    check("1 crossing creates one pending boost entitlement",
          crossed.get("success") and pending == [
              (1, "boost", "xp_boost", "pending",
               '{"multiplier": 2.0, "duration_hours": 12}')],
          str(pending))
    check("2 crossing creates no active boost row", boosts() == [])

    role = SimpleNamespace(id=55, name="Cape", position=1)
    who = member()
    who.roles = []
    who.add_roles = AsyncMock()
    who.guild = SimpleNamespace(
        id=GUILD, me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
        get_role=lambda rid: role if int(rid) == 55 else None)
    once = await claim_available(GUILD, USER, member=who)
    boost_id = rows(
        "SELECT id FROM level_reward_claims WHERE track='boost'")[0][0]
    granted = boosts(f"level_claim:{boost_id}")
    check("3 claim creates exactly one boost row from the snapshot",
          once["delivered_boost"] == 1 and len(granted) == 1
          and granted[0][0] == 2.0 and granted[0][2] == f"level_claim:{boost_id}"
          and rows("SELECT quantity FROM inventory_items WHERE item_name='Satchel'")[0][0] == 1
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER))[0][0] == 515
          and rows("SELECT status FROM level_reward_claims WHERE track='role'")[0][0]
          == "fulfilled",
          str(granted))
    expiry = granted[0][1]
    again = await claim_available(GUILD, USER, member=who)
    check("5 retry does not add a row or extend the expiry",
          again["owned"] == 0 and boosts(f"level_claim:{boost_id}") == granted
          and boosts(f"level_claim:{boost_id}")[0][1] == expiry)

    # Concurrent owners on one fresh boost entitlement.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_active_boosts")
    execute("DELETE FROM inventory_items")
    execute("DELETE FROM leveling_currency_rewards")
    execute("DELETE FROM leveling_rewards")
    execute("DELETE FROM leveling_shop_rewards")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    execute("UPDATE economy SET balance=500 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="race", source="test")
    first, second = await asyncio.gather(
        claim_available(GUILD, USER), claim_available(GUILD, USER))
    owners = sorted(item["owned"] for item in (first, second))
    claim_id = rows(
        "SELECT id FROM level_reward_claims WHERE track='boost'")[0][0]
    check("7 one concurrent owner delivers one boost",
          owners == [0, 1]
          and len(boosts(f"level_claim:{claim_id}")) == 1,
          str((first, second)))

    # Active lease blocks the other attempt. Expired lease reclaims once.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_active_boosts")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="lease", source="test")
    claim_id = rows(
        "SELECT id FROM level_reward_claims WHERE track='boost'")[0][0]
    fresh = datetime.now(timezone.utc).isoformat()
    execute(
        "UPDATE level_reward_claims SET status='processing', owner_token='owner-A', "
        "processing_started_at=? WHERE id=?", (fresh, claim_id))
    blocked = await claim_available(GUILD, USER)
    check("8 an active lease blocks the other attempt",
          blocked["owned"] == 0 and boosts() == [])
    old = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS + 5)).isoformat()
    execute(
        "UPDATE level_reward_claims SET processing_started_at=? WHERE id=?",
        (old, claim_id))
    reclaimed = await claim_available(GUILD, USER)
    check("8 an expired lease is reclaimed as exactly one boost",
          reclaimed["delivered_boost"] == 1
          and len(boosts(f"level_claim:{claim_id}")) == 1
          and rows("SELECT status FROM level_reward_claims WHERE id=?",
                   (claim_id,))[0][0] == "fulfilled")

    # Rollback leaves no boost row and stays retryable.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_active_boosts")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="rollback", source="test")
    claim_id = rows(
        "SELECT id FROM level_reward_claims WHERE track='boost'")[0][0]
    import utils.xp_calculator as xpcalc
    import utils.level_claims as claims_mod
    real_grant = xpcalc.grant_xp_boost

    async def explode(*args, **kwargs):
        if kwargs.get("db") is not None:
            await real_grant(*args, **kwargs)
            raise RuntimeError("boost crashed after write")
        return await real_grant(*args, **kwargs)

    xpcalc.grant_xp_boost = explode
    claims_mod.grant_xp_boost = explode
    failed = await claim_available(GUILD, USER)
    xpcalc.grant_xp_boost = real_grant
    check("9 a rolled-back boost write leaves no row and stays retryable",
          failed["delivered_boost"] == 0 and boosts() == []
          and rows("SELECT status FROM level_reward_claims WHERE id=?",
                   (claim_id,))[0][0] == "failed")
    retried = await claim_available(GUILD, USER)
    restored = boosts(f"level_claim:{claim_id}")
    check("9 retry after rollback delivers the snapshot once",
          retried["delivered_boost"] == 1 and len(restored) == 1
          and restored[0][0] == 2.0)

    # Deleted and edited config still pay the frozen snapshot.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_active_boosts")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="snapshot", source="test")
    execute(
        "UPDATE leveling_boost_rewards SET multiplier=9, duration_hours=1 "
        "WHERE guild_id=?", (GUILD,))
    execute("DELETE FROM leveling_boost_rewards WHERE guild_id=?", (GUILD,))
    paid = await claim_available(GUILD, USER)
    claim_id = rows(
        "SELECT id, payload_json FROM level_reward_claims WHERE track='boost'")[0]
    paid_row = boosts(f"level_claim:{claim_id[0]}")
    check("10 deleted config still pays the frozen snapshot",
          '"duration_hours": 12' in claim_id[1]
          and paid["delivered_boost"] == 1
          and paid_row and paid_row[0][0] == 2.0,
          str(paid_row))
    guild = SimpleNamespace(id=GUILD, get_role=lambda rid: None)
    embed = await _level_embed(guild, SimpleNamespace(id=USER), "level")
    shown = "\n".join(field.value for field in embed.fields)
    check("10 /level still shows the frozen boost",
          "2x XP · 12h" in shown, shown)

    # Shop xp_boost purchase stays source=shop and can coexist.
    shop_boost = execute(
        "INSERT INTO shop_items (guild_id, name, price, type, duration_hours, "
        "xp_boost_multiplier, max_stock, current_stock, enabled) "
        "VALUES (?, 'Surge', 25, 'xp_boost', 6, 3, 4, 4, 1)", (GUILD,))
    buyer = member()
    itx = SimpleNamespace(
        user=buyer, guild=SimpleNamespace(id=GUILD),
        client=SimpleNamespace(get_guild=lambda gid: None),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                 is_done=lambda: False))
    from cogs.shop import process_purchase
    before = rows(
        "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
        (GUILD, USER))[0][0]
    await process_purchase(itx, shop_boost)
    shop_rows = rows(
        "SELECT source FROM leveling_active_boosts WHERE source='shop'")
    check("11 shop xp_boost still charges and uses source=shop beside the claim",
          shop_rows == [("shop",)]
          and rows("SELECT current_stock FROM shop_items WHERE id=?",
                   (shop_boost,))[0][0] == 3
          and rows("SELECT COUNT(*) FROM purchase_history WHERE item_id=?",
                   (shop_boost,))[0][0] == 1
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER))[0][0] == before - 25
          and len(boosts(f"level_claim:{claim_id[0]}")) == 1)

    import dashboard.app as dashboard
    app = dashboard.app
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
            "VALUES (?,?,'admin',1)", (GUILD, USER))
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user={"id": USER, "username": "Phase One", "avatar": None},
                       guild_id=GUILD, expires_at=time.time() + 7200,
                       csrf_token="slice4-csrf")
    rejected = client.post(
        "/api/leveling/boost-reward",
        json={"level": 2, "multiplier": 1, "duration_hours": 4},
        headers={"X-CSRF-Token": "slice4-csrf"})
    accepted = client.post(
        "/api/leveling/boost-reward",
        json={"level": 4, "multiplier": 1.5, "duration_hours": 8},
        headers={"X-CSRF-Token": "slice4-csrf"})
    duplicate = client.post(
        "/api/leveling/boost-reward",
        json={"level": 4, "multiplier": 3, "duration_hours": 2},
        headers={"X-CSRF-Token": "slice4-csrf"})
    claims_before = rows("SELECT COUNT(*) FROM level_reward_claims")[0][0]
    reward_id = rows(
        "SELECT id FROM leveling_boost_rewards WHERE level=4")[0][0]
    deleted = client.delete(
        f"/api/leveling/boost-reward/{reward_id}",
        headers={"X-CSRF-Token": "slice4-csrf"})
    check("config rejects a weak multiplier, stores one row per level, and delete keeps claims",
          rejected.get_json().get("success") is False
          and accepted.get_json().get("success") is True
          and duplicate.get_json().get("success") is False
          and deleted.get_json().get("success") is True
          and rows("SELECT COUNT(*) FROM level_reward_claims")[0][0] == claims_before,
          f"{rejected.get_json()} {accepted.get_json()} {duplicate.get_json()}")

    rank_source = inspect.getsource(Leveling.rank.callback)
    voice_source = inspect.getsource(calculate_voice_xp)
    message_source = inspect.getsource(calculate_message_xp)
    check("14 /rank, voice XP, and the message XP formula were not retargeted",
          "claim_available" not in rank_source
          and "leveling_boost_rewards" not in rank_source
          and "level_claim" not in voice_source
          and "leveling_boost_rewards" not in message_source
          and xp_for_level(1) == 100
          and xp_progress(100)[0] == 1)
    print("ALL SLICE 4 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
