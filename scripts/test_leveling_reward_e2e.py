"""Dashboard definition -> persisted reward -> production XP crossing -> claim delivery.

This uses the real Flask API routes, SQLite database, give_reward/record_crossing,
claim_available, and fake Discord roles. No Discord API calls are made.
Run with:
    python scripts/test_leveling_reward_e2e.py
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


class FakeRole:
    def __init__(self, role_id, name):
        self.id = int(role_id)
        self.name = name
        self.position = 1


class FakeMember:
    def __init__(self, guild, user_id, roles=()):
        self.guild = guild
        self.id = int(user_id)
        self.mention = f"<@{self.id}>"
        self.roles = list(roles)
        self.added = []
        self.removed = []
        self.fail_add = None
        self.bot = False
        self.premium_since = None

    async def add_roles(self, role, reason=None):
        if self.fail_add:
            self.fail_add(role)
        if role not in self.roles:
            self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        self.roles = [held for held in self.roles if held.id != role.id]
        self.removed.append(role.id)

    def held(self):
        return sorted(role.id for role in self.roles)


def dashboard_client():
    import dashboard.app as dashboard

    app = dashboard.app
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    admin_id = USER + 500
    execute("INSERT INTO dashboard_users "
            "(guild_id,user_id,permission_level,enabled) VALUES (?,?,'admin',1)",
            (GUILD, admin_id))
    client = app.test_client()
    csrf = "level-reward-e2e-csrf"
    with client.session_transaction() as session:
        session.update(
            user={"id": admin_id, "username": "Level reward E2E", "avatar": None},
            guild_id=GUILD,
            expires_at=time.time() + 7200,
            csrf_token=csrf,
        )
    return client, csrf


def post(client, csrf, path, body):
    return client.post(path, json=body, headers={"X-CSRF-Token": csrf})


def delete(client, csrf, path):
    return client.delete(path, headers={"X-CSRF-Token": csrf})


async def main():
    await reset_database()
    from utils.level_claims import claim_available, ensure_tables, list_claims
    from utils.reward_engine import give_reward
    from utils.xp_calculator import xp_for_level

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,0,0,0)", (GUILD, USER))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,0,0)", (GUILD, USER))

    client, csrf = dashboard_client()
    role_ids = [505, 506, 510, 511, 515]
    role_catalog = {rid: FakeRole(rid, f"Level role {rid}") for rid in role_ids}
    guild = SimpleNamespace(
        id=GUILD,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_role=lambda rid: role_catalog.get(int(rid)),
    )
    member = FakeMember(guild, USER)
    bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if int(guild_id) == GUILD else None)
    guild.get_member = lambda user_id: member if int(user_id) == USER else None

    # Shop products are managed in the Shop catalog; the Leveling Dashboard API
    # attaches an existing eligible product as a reward definition.
    product_id = execute("""
        INSERT INTO shop_items (guild_id,name,price,type,enabled)
        VALUES (?,?,100,'custom',1)
    """, (GUILD, "E2E Cache"))

    # Create every supported Leveling reward track through the production API.
    api_results = [
        post(client, csrf, "/api/leveling/reward",
             {"level": 1, "role_id": role_ids[0]}),
        post(client, csrf, "/api/leveling/reward",
             {"level": 1, "role_id": role_ids[1]}),
        post(client, csrf, "/api/leveling/currency-reward",
             {"level": 1, "currency": "balance", "amount": 250}),
        post(client, csrf, "/api/leveling/currency-reward",
             {"level": 1, "currency": "diamonds", "amount": 25}),
        post(client, csrf, "/api/leveling/shop-reward",
             {"level": 1, "item_id": product_id, "quantity": 2}),
        post(client, csrf, "/api/leveling/boost-reward",
             {"level": 1, "multiplier": 1.5, "duration_hours": 2}),
    ]
    check("Dashboard API accepts role, currency, product, and XP-boost definitions",
          all(response.status_code == 200 and response.get_json().get("success")
              for response in api_results),
          str([response.get_json() for response in api_results]))

    role_defs = rows(
        "SELECT level,role_id FROM leveling_rewards WHERE guild_id=? ORDER BY role_id",
        (GUILD,))
    currency_defs = rows(
        "SELECT level,currency,amount FROM leveling_currency_rewards "
        "WHERE guild_id=? ORDER BY currency", (GUILD,))
    shop_defs = rows(
        "SELECT level,item_id,quantity FROM leveling_shop_rewards WHERE guild_id=?",
        (GUILD,))
    boost_defs = rows(
        "SELECT level,multiplier,duration_hours FROM leveling_boost_rewards "
        "WHERE guild_id=?", (GUILD,))
    currency_list = client.get("/api/leveling/currency-rewards").get_json()["rewards"]
    shop_list = client.get("/api/leveling/shop-rewards").get_json()
    boost_list = client.get("/api/leveling/boost-rewards").get_json()["rewards"]
    with patch("utils.discord_user_cache.resolve_users",
               new=AsyncMock(return_value={})):
        role_page = client.get("/leveling")
    check("Dashboard writes persist and its GET APIs read back the same definitions",
          role_page.status_code == 200 and b"505" in role_page.data
          and b"506" in role_page.data
          and role_defs == [(1, 505), (1, 506)]
          and currency_defs == [(1, "balance", 250), (1, "diamonds", 25)]
          and shop_defs == [(1, product_id, 2)]
          and boost_defs == [(1, 1.5, 2)]
          and {(r["level"], r["currency"], r["amount"]) for r in currency_list}
              == {(1, "balance", 250), (1, "diamonds", 25)}
          and any(r["level"] == 1 and r["item_id"] == product_id
                  and r["quantity"] == 2 for r in shop_list["rewards"])
          and any(p["id"] == product_id and p["name"] == "E2E Cache"
                  for p in shop_list["products"])
          and len(boost_list) == 1
          and boost_list[0]["level"] == 1
          and boost_list[0]["multiplier"] == 1.5
          and boost_list[0]["duration_hours"] == 2,
          f"roles={role_defs} currency={currency_defs} shop={shop_defs} boosts={boost_defs}")

    # The Dashboard's current definition editor replaces rows with DELETE+POST
    # (there is no in-place PUT/PATCH route). Exercise that production workflow
    # before crossing so runtime discovery is proven against the updated values.
    role_row_id = rows("SELECT id FROM leveling_rewards WHERE guild_id=? AND level=1 "
                       "AND role_id=505", (GUILD,))[0][0]
    balance_row_id = next(r["id"] for r in currency_list
                          if r["level"] == 1 and r["currency"] == "balance")
    shop_row_id = next(r["id"] for r in shop_list["rewards"]
                       if r["level"] == 1 and r["item_id"] == product_id)
    boost_row_id = boost_list[0]["id"]
    update_results = [
        delete(client, csrf, f"/api/leveling/reward/{role_row_id}"),
        post(client, csrf, "/api/leveling/reward",
             {"level": 1, "role_id": 515}),
        delete(client, csrf, f"/api/leveling/currency-reward/{balance_row_id}"),
        post(client, csrf, "/api/leveling/currency-reward",
             {"level": 1, "currency": "balance", "amount": 300}),
        delete(client, csrf, f"/api/leveling/shop-reward/{shop_row_id}"),
        post(client, csrf, "/api/leveling/shop-reward",
             {"level": 1, "item_id": product_id, "quantity": 3}),
        delete(client, csrf, f"/api/leveling/boost-reward/{boost_row_id}"),
        post(client, csrf, "/api/leveling/boost-reward",
             {"level": 1, "multiplier": 1.75, "duration_hours": 2}),
    ]
    role_defs = rows(
        "SELECT level,role_id FROM leveling_rewards WHERE guild_id=? ORDER BY role_id",
        (GUILD,))
    currency_defs = rows(
        "SELECT level,currency,amount FROM leveling_currency_rewards "
        "WHERE guild_id=? ORDER BY currency", (GUILD,))
    shop_defs = rows(
        "SELECT level,item_id,quantity FROM leveling_shop_rewards WHERE guild_id=?",
        (GUILD,))
    boost_defs = rows(
        "SELECT level,multiplier,duration_hours FROM leveling_boost_rewards "
        "WHERE guild_id=?", (GUILD,))
    currency_list = client.get("/api/leveling/currency-rewards").get_json()["rewards"]
    shop_list = client.get("/api/leveling/shop-rewards").get_json()
    boost_list = client.get("/api/leveling/boost-rewards").get_json()["rewards"]
    with patch("utils.discord_user_cache.resolve_users",
               new=AsyncMock(return_value={})):
        updated_role_page = client.get("/leveling")
    check("Dashboard delete+create edits persist and are returned to runtime readers",
          all(response.status_code == 200
              and response.get_json().get("success") is True
              for response in update_results)
          and updated_role_page.status_code == 200
          and b"506" in updated_role_page.data and b"515" in updated_role_page.data
          and role_defs == [(1, 506), (1, 515)]
          and currency_defs == [(1, "balance", 300), (1, "diamonds", 25)]
          and shop_defs == [(1, product_id, 3)]
          and boost_defs == [(1, 1.75, 2)]
          and {(r["currency"], r["amount"]) for r in currency_list}
              == {("balance", 300), ("diamonds", 25)}
          and any(r["item_id"] == product_id and r["quantity"] == 3
                  for r in shop_list["rewards"])
          and len(boost_list) == 1 and boost_list[0]["multiplier"] == 1.75,
          f"roles={role_defs} currency={currency_defs} shop={shop_defs} boosts={boost_defs}")

    # This is the real production XP/reward path. It commits XP and reads the
    # just-persisted API definitions through record_crossing in the same tx.
    crossed = await give_reward(
        bot, GUILD, USER, "xp", amount=xp_for_level(1),
        reason="Level reward E2E", source="leveling")
    pending = await list_claims(GUILD, USER)
    check("real XP crossing discovers all same-Level API rewards as pending claims",
          crossed["success"] and crossed["new_level"] == 1
          and rows("SELECT xp,level FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(xp_for_level(1), 1)]
          and len(pending) == 6
          and all(claim["status"] == "pending" for claim in pending)
          and {(claim["track"], claim["reward_ref"]) for claim in pending}
              == {("role", "506"), ("role", "515"),
                  ("currency", "balance"), ("currency", "diamonds"),
                  ("shop", str(product_id)), ("boost", "xp_boost")},
          f"crossing={crossed} claims={pending}")
    check("crossing creates entitlements only; nothing is delivered before Claim All",
          rows("SELECT balance,diamonds FROM economy WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(0, 0)]
          and rows("SELECT COUNT(*) FROM inventory_items WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(0,)]
          and rows("SELECT COUNT(*) FROM leveling_active_boosts WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER)) == [(0,)]
          and member.held() == [], "")

    delivered = await claim_available(GUILD, USER, member=member, bot=bot)
    claims_after = await list_claims(GUILD, USER)
    boost_claim_id = next(claim["id"] for claim in claims_after
                          if claim["track"] == "boost")
    check("existing Claim All delivers each independent reward track once",
          len(delivered["fulfilled"]) == 6 and delivered["failed"] == []
          and delivered["delivered_roles"] == 2
          and delivered["delivered_currency"] == 2
          and delivered["delivered_shop"] == 1
          and delivered["delivered_boost"] == 1
          and member.held() == [506, 515]
          and rows("SELECT balance,diamonds FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(300, 25)]
          and rows("SELECT item_name,item_type,quantity,source FROM inventory_items "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))
              == [("E2E Cache", "shop_custom", 3, "level_claim")]
          and rows("SELECT multiplier,source FROM leveling_active_boosts "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))
              == [(1.75, f"level_claim:{boost_claim_id}")]
          and all(claim["status"] == "fulfilled" for claim in claims_after),
          f"delivery={delivered} claims={claims_after}")

    delivered_again = await claim_available(GUILD, USER, member=member, bot=bot)
    check("a repeat claim pass pays nothing again",
          delivered_again["owned"] == 0 and delivered_again["fulfilled"] == []
          and delivered_again["failed"] == []
          and rows("SELECT balance,diamonds FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(300, 25)]
          and rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
                   "AND item_name='E2E Cache'", (GUILD, USER)) == [(3,)]
          and rows("SELECT COUNT(*) FROM leveling_active_boosts WHERE source=?",
                   (f"level_claim:{boost_claim_id}",)) == [(1,)]
          and member.added == [506, 515],
          f"second={delivered_again} added={member.added}")

    # A second reward level exercises partial Discord failure, retry, and the
    # replacement setting without coupling those role claims to other tracks.
    config_saved = post(client, csrf, "/api/leveling/config",
                        {"remove_old_reward_role": 1})
    higher_defs = [
        post(client, csrf, "/api/leveling/reward",
             {"level": 2, "role_id": role_ids[2]}),
        post(client, csrf, "/api/leveling/reward",
             {"level": 2, "role_id": role_ids[3]}),
    ]
    check("Dashboard config and same-Level higher role definitions are saved",
          config_saved.get_json().get("success") is True
          and all(response.get_json().get("success") for response in higher_defs)
          and rows("SELECT remove_old_reward_role FROM leveling_config WHERE guild_id=?",
                   (GUILD,)) == [(1,)], "")
    crossed_higher = await give_reward(
        bot, GUILD, USER, "xp", amount=xp_for_level(2),
        reason="Higher Level reward E2E", source="leveling")
    member.fail_add = lambda role: (
        (_ for _ in ()).throw(RuntimeError("simulated Discord role add failure"))
        if role.id == 511 else None)
    partial = await claim_available(GUILD, USER, member=member, bot=bot)
    role_claims = rows(
        "SELECT reward_level,reward_ref,status,last_error FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=? AND track='role' "
        "ORDER BY reward_level,reward_ref", (GUILD, USER))
    check("a partial higher-Level role failure stays retryable and preserves L1 roles",
          crossed_higher["new_level"] == 2
          and partial["delivered_roles"] == 1 and len(partial["failed"]) == 1
          and role_claims == [(1, "506", "fulfilled", None),
                              (1, "515", "fulfilled", None),
                              (2, "510", "fulfilled", None),
                              (2, "511", "failed", "simulated Discord role add failure")]
          and partial["reconciled"]["blocked"] is True
          and member.held() == [506, 510, 515],
          f"partial={partial} claims={role_claims} held={member.held()}")

    member.fail_add = None
    retried = await claim_available(GUILD, USER, member=member, bot=bot)
    role_claims = rows(
        "SELECT reward_level,reward_ref,status FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=? AND track='role' "
        "ORDER BY reward_level,reward_ref", (GUILD, USER))
    check("retry fulfills only the failed same-Level role, then replaces old roles",
          retried["delivered_roles"] == 1 and len(retried["fulfilled"]) == 1
          and role_claims == [(1, "506", "fulfilled"),
                              (1, "515", "fulfilled"),
                              (2, "510", "fulfilled"),
                              (2, "511", "fulfilled")]
          and member.held() == [510, 511]
          and member.removed == [506, 515],
          f"retry={retried} claims={role_claims} held={member.held()}")

    # Exercise the real admin reset callback then re-level. The unique claim
    # identities prevent a second payment even though the same Level is crossed.
    from cogs.leveling import Leveling
    leveling = Leveling.__new__(Leveling)
    reset_interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await Leveling.resetxp.callback(leveling, reset_interaction, member)
    before_relevel_ids = rows(
        "SELECT id,reward_level,track,reward_ref,status FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=? ORDER BY id", (GUILD, USER))
    relevel = await give_reward(
        bot, GUILD, USER, "xp", amount=xp_for_level(1),
        reason="Duplicate guard E2E", source="leveling")
    no_duplicate = await claim_available(GUILD, USER, member=member, bot=bot)
    after_relevel_ids = rows(
        "SELECT id,reward_level,track,reward_ref,status FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=? ORDER BY id", (GUILD, USER))
    check("reset/re-level crossing reuses the same identities and cannot repay",
          relevel["new_level"] == 1 and no_duplicate["owned"] == 0
          and before_relevel_ids == after_relevel_ids
          and rows("SELECT xp,level FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(xp_for_level(1), 1)]
          and rows("SELECT balance,diamonds FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(300, 25)]
          and rows("SELECT item_name,quantity FROM inventory_items WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER)) == [("E2E Cache", 3)]
          and rows("SELECT COUNT(*) FROM leveling_active_boosts WHERE source=?",
                   (f"level_claim:{boost_claim_id}",)) == [(1,)],
          f"relevel={relevel} claims_before={before_relevel_ids} "
          f"claims_after={after_relevel_ids} claim={no_duplicate}")

    print("ALL LEVELING REWARD E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
