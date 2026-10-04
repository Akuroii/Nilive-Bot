"""Slice 3: Shop level rewards use the existing claim ownership path.

Run from the scripts directory:
    python test_slice3_shop_claims.py
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


def wallet():
    return rows(
        "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
        (GUILD, USER))[0]


def stock(item_id):
    return rows(
        "SELECT current_stock FROM shop_items WHERE id=?", (item_id,))[0][0]


def inventory(name):
    found = rows(
        "SELECT quantity, item_type FROM inventory_items "
        "WHERE guild_id=? AND user_id=? AND item_name=?",
        (GUILD, USER, name))
    return found[0] if found else (0, None)


def claims():
    return rows(
        "SELECT reward_level, track, reward_ref, status, payload_json "
        "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY reward_level, track, reward_ref",
        (GUILD, USER))


def purchase_count():
    return rows("SELECT COUNT(*) FROM purchase_history WHERE guild_id=?", (GUILD,))[0][0]


def shop_item(name, item_type, **extra):
    return execute(
        "INSERT INTO shop_items (guild_id, name, price, type, role_id, "
        "duration_hours, max_stock, current_stock, enabled, xp_boost_multiplier) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (GUILD, name, extra.get("price", 80), item_type,
         extra.get("role_id"), extra.get("duration_hours"),
         extra.get("max_stock", 5), extra.get("current_stock", 5),
         extra.get("enabled", 1), extra.get("multiplier")))


def shop_reward(level, item_id, quantity):
    execute(
        "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity) "
        "VALUES (?, ?, ?, ?)",
        (GUILD, level, item_id, quantity))


def person():
    who = member()
    role = SimpleNamespace(id=55, name="Cape", position=1)
    who.roles = []
    who.add_roles = AsyncMock()
    who.guild = SimpleNamespace(
        id=GUILD, me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
        get_role=lambda rid: role if int(rid) == 55 else None)
    return who


async def main():
    from cogs.leveling import Leveling, _level_embed
    from utils.level_claims import LEASE_SECONDS, claim_available
    from utils.reward_engine import give_reward

    await reset_database()
    seed_member()
    execute("UPDATE levels SET xp=0, level=0, prestige=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE economy SET balance=40, diamonds=7 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    custom = shop_item("Satchel", "custom")
    stack = shop_item("Token", "title")
    potion = shop_item("Elixir", "potion", duration_hours=2, multiplier=2.0)
    cape = shop_item("Cape", "temp_role", role_id=55, duration_hours=24)
    many = shop_item("Banner", "temp_role", role_id=55, duration_hours=12)
    shop_reward(1, custom, 1)
    shop_reward(1, stack, 4)
    shop_reward(1, potion, 2)
    shop_reward(1, cape, 1)
    shop_reward(1, many, 3)
    execute(
        "INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount) "
        "VALUES (?, 1, 'balance', 15)", (GUILD,))
    execute(
        "INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?, 1, 55)",
        (GUILD,))
    before_wallet = wallet()
    before_stock = stock(custom)
    crossed = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=100,
        reason="cross", source="test")
    pending = [c for c in claims() if c[1] == "shop"]
    check("1 crossing creates shop entitlements and does not pay",
          crossed.get("success") and len(pending) == 5
          and all(c[3] == "pending" for c in pending)
          and wallet() == before_wallet and stock(custom) == before_stock
          and purchase_count() == 0
          and '"quantity": 4' in next(c[4] for c in pending if c[2] == str(stack)),
          str(pending))

    who = person()
    once = await claim_available(GUILD, USER, member=who)
    check("2 quantity 1 inventory reward is delivered once",
          inventory("Satchel") == (1, "shop_custom")
          and once["delivered_shop"] >= 1
          and purchase_count() == 0 and wallet() == (55, 7))
    again = await claim_available(GUILD, USER, member=who)
    check("2 a second claim does not add another copy",
          again["owned"] == 0 and inventory("Satchel") == (1, "shop_custom"))
    check("3 quantity greater than 1 delivers the frozen quantity",
          inventory("Token") == (4, "title")
          and inventory("Elixir")[0] == 2)
    check("4 temp_role quantity 1 writes one expiry unit and one item",
          rows("SELECT COUNT(*) FROM temp_roles WHERE claim_id=("
               "SELECT id FROM level_reward_claims WHERE reward_ref=?)",
               (str(cape),))[0][0] == 1
          and inventory("Cape")[0] == 1)
    banner_units = rows(
        "SELECT COUNT(*), COUNT(DISTINCT expires_at) FROM temp_roles "
        "WHERE claim_id=(SELECT id FROM level_reward_claims WHERE reward_ref=?)",
        (str(many),))[0]
    check("5 temp_role quantity 3 writes exactly three units",
          banner_units == (3, 1) and inventory("Banner")[0] == 3
          and stock(many) == 5 and purchase_count() == 0)
    check("13 currency and permanent role still deliver on the same claim",
          wallet() == (55, 7)
          and rows("SELECT status FROM level_reward_claims WHERE track='role'")[0][0]
          == "fulfilled"
          and who.add_roles.await_count == 3)
    execute("DELETE FROM leveling_currency_rewards")
    execute("DELETE FROM leveling_rewards")
    held_wallet = wallet()

    # Concurrent owners on a fresh pending shop entitlement.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM inventory_items")
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    rare = shop_item("Relic", "custom", max_stock=3, current_stock=3)
    shop_reward(1, rare, 1)
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="race", source="test")
    execute("DELETE FROM level_reward_claims WHERE reward_ref<>?", (str(rare),))
    first, second = await asyncio.gather(
        claim_available(GUILD, USER), claim_available(GUILD, USER))
    owners = sorted(item["owned"] for item in (first, second))
    check("6 two concurrent claims have one owner and one delivery",
          owners == [0, 1] and inventory("Relic") == (1, "shop_custom")
          and stock(rare) == 3, str((first, second)))

    # Failure stays retryable and does not leave a grant behind.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM inventory_items")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="fail", source="test")
    import utils.inventory as inventory_mod
    real_give = inventory_mod.give_item

    async def explode(*args, **kwargs):
        if kwargs.get("db") is not None:
            await real_give(*args, **kwargs)
            raise RuntimeError("inventory crashed after write")
        return await real_give(*args, **kwargs)

    inventory_mod.give_item = explode
    import utils.level_claims as claims_mod
    claims_mod.give_item = explode
    failed = await claim_available(GUILD, USER)
    inventory_mod.give_item = real_give
    claims_mod.give_item = real_give
    relic = [c for c in claims() if c[2] == str(rare)]
    check("7 a delivery failure stays retryable and rolls the grant back",
          failed["delivered_shop"] == 0 and inventory("Relic") == (0, None)
          and relic and relic[0][3] == "failed", str(relic))
    retried = await claim_available(GUILD, USER)
    check("8 retry after a rolled-back grant delivers the quantity once",
          inventory("Relic") == (1, "shop_custom")
          and stock(rare) == 3 and wallet() == held_wallet)

    # Crash after durable temp-role units, before inventory finalization.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM inventory_items")
    execute("DELETE FROM temp_roles")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="crash", source="test")
    claim_id = rows(
        "SELECT id FROM level_reward_claims WHERE reward_ref=?", (str(many),))[0][0]
    execute("DELETE FROM level_reward_claims WHERE id<>?", (claim_id,))
    old = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS + 5)).isoformat()
    expires = "2026-10-04T00:00:00+00:00"
    for index in range(3):
        execute(
            "INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at, "
            "source, claim_id, unit_index) VALUES (?, ?, 55, ?, 'level_claim', ?, ?)",
            (GUILD, USER, expires, claim_id, index))
    execute(
        "UPDATE level_reward_claims SET status='processing', owner_token='stale', "
        "processing_started_at=? WHERE id=?",
        (old, claim_id))
    who.add_roles.reset_mock()
    recovered = await claim_available(GUILD, USER, member=who)
    units = rows(
        "SELECT COUNT(*) FROM temp_roles WHERE claim_id=?", (claim_id,))[0][0]
    check("8 crash after temp-role units does not duplicate them on retry",
          recovered["delivered_shop"] >= 1 and units == 3
          and inventory("Banner")[0] == 3 and who.add_roles.await_count == 1,
          f"units={units} inventory={inventory('Banner')} result={recovered}")
    repeat = await claim_available(GUILD, USER, member=who)
    check("8 a later claim still does not add units",
          repeat["owned"] == 0 and units == rows(
              "SELECT COUNT(*) FROM temp_roles WHERE claim_id=?", (claim_id,))[0][0]
          and inventory("Banner")[0] == 3)

    # Deleted and disabled products still pay the snapshot.
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM inventory_items")
    execute("UPDATE levels SET xp=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
    gone = shop_item("Ghost", "custom")
    quiet = shop_item("Muted", "title")
    shop_reward(1, gone, 2)
    shop_reward(1, quiet, 1)
    await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=100,
                      reason="snapshot", source="test")
    execute("DELETE FROM shop_items WHERE id=?", (gone,))
    execute("UPDATE shop_items SET enabled=0 WHERE id=?", (quiet,))
    paid = await claim_available(GUILD, USER)
    check("9 deleted and disabled products still fulfill the snapshot",
          paid["delivered_shop"] >= 2
          and inventory("Ghost") == (2, "shop_custom")
          and inventory("Muted") == (1, "title")
          and purchase_count() == 0)
    check("10 shop stock is unchanged by level claims",
          stock(custom) == 5 and stock(rare) == 3 and stock(quiet) == 5)
    check("11 no purchase history row is created", purchase_count() == 0)
    check("12 wallet is unchanged except the currency level reward",
          wallet() == held_wallet)

    guild = SimpleNamespace(id=GUILD, get_role=lambda rid: None)
    embed = await _level_embed(guild, SimpleNamespace(id=USER), "level")
    shown = "\n".join(field.value for field in embed.fields)
    check("9 /level shows the shop reward quantity",
          "2x Ghost" in shown or "1x Muted" in shown, shown)

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
    prestige = shop_item("Crown", "prestige")
    rejected = client.post(
        "/api/leveling/shop-reward",
        json={"level": 2, "item_id": prestige, "quantity": 1},
        headers={"X-CSRF-Token": "phase1-csrf"})
    accepted = client.post(
        "/api/leveling/shop-reward",
        json={"level": 2, "item_id": quiet, "quantity": 3},
        headers={"X-CSRF-Token": "phase1-csrf"})
    duplicate = client.post(
        "/api/leveling/shop-reward",
        json={"level": 2, "item_id": quiet, "quantity": 9},
        headers={"X-CSRF-Token": "phase1-csrf"})
    check("config rejects prestige and stores an allowed product once",
          rejected.status_code < 500 and rejected.get_json().get("success") is False
          and accepted.status_code < 400 and accepted.get_json().get("success") is True
          and duplicate.get_json().get("success") is False,
          f"rejected={rejected.get_json()} accepted={accepted.get_json()} "
          f"duplicate={duplicate.get_json()}")

    rank_source = inspect.getsource(Leveling.rank.callback)
    check("14 /rank was not given shop-claim behavior",
          "claim_available" not in rank_source and "leveling_shop_rewards" not in rank_source)
    level_source = inspect.getsource(Leveling.level.callback)
    view_source = inspect.getsource(
        __import__("cogs.leveling", fromlist=["LevelRewardView"]).LevelRewardView.claim_all)
    check("Claim All still defers and uses claim_available",
          "claim_available" in view_source and "defer" in view_source
          and "ephemeral=True" in level_source and "process_purchase" not in level_source)

    print("ALL SLICE 3 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
