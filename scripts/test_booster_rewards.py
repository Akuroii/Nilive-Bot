"""Booster-only level rewards: dashboard API -> DB -> XP crossing -> claim -> /level.

Uses the real Flask API routes, SQLite database, give_reward/record_crossing,
claim_available, the /level embed and the Claim All button callback, with fake
Discord members/roles. No Discord API calls are made.
Run with:
    python scripts/test_booster_rewards.py
"""
import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows

PLAIN = USER + 1
BOOSTING = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


class FakeRole:
    def __init__(self, role_id):
        self.id = int(role_id)
        self.name = f"role {role_id}"
        self.position = 1
        self.mention = f"<@&{role_id}>"


class FakeMember:
    def __init__(self, guild, user_id, boosting=False):
        self.guild = guild
        self.id = int(user_id)
        self.mention = f"<@{self.id}>"
        self.roles = []
        self.added = []
        self.removed = []
        self.fail_add = None
        self.bot = False
        self.premium_since = BOOSTING if boosting else None

    async def add_roles(self, role, reason=None):
        if self.fail_add:
            self.fail_add(role)
        if role not in self.roles:
            self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        self.roles = [r for r in self.roles if r.id != role.id]
        self.removed.append(role.id)

    def held(self):
        return sorted(r.id for r in self.roles)


def dashboard_client():
    import dashboard.app as dashboard

    app = dashboard.app
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    admin_id = USER + 500
    execute("INSERT INTO dashboard_users "
            "(guild_id,user_id,permission_level,enabled) VALUES (?,?,'admin',1)",
            (GUILD, admin_id))
    client = app.test_client()
    csrf = "booster-reward-csrf"
    with client.session_transaction() as session:
        session.update(
            user={"id": admin_id, "username": "Booster E2E", "avatar": None},
            guild_id=GUILD, expires_at=time.time() + 7200, csrf_token=csrf)
    return client, csrf


def post(client, csrf, path, body):
    return client.post(path, json=body, headers={"X-CSRF-Token": csrf})


def claim_rows(user):
    return rows(
        "SELECT reward_level,track,reward_ref,status FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=? ORDER BY reward_level,track,reward_ref",
        (GUILD, user))


def balance(user):
    found = rows("SELECT balance,diamonds FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else (0, 0)


async def main():
    await reset_database()
    from utils.level_claims import claim_available, ensure_tables, list_claims
    from utils.reward_engine import give_reward
    from utils.xp_calculator import xp_for_level, xp_progress
    import cogs.leveling as leveling_cog

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    for uid in (USER, PLAIN):
        execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
                "VALUES (?,?,0,0,0)", (GUILD, uid))
        execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
                "VALUES (?,?,0,0)", (GUILD, uid))

    roles = {rid: FakeRole(rid) for rid in (600, 601, 700)}
    guild = SimpleNamespace(
        id=GUILD, me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_role=lambda rid: roles.get(int(rid)))
    booster = FakeMember(guild, USER, boosting=True)
    plain = FakeMember(guild, PLAIN, boosting=False)
    people = {USER: booster, PLAIN: plain}
    guild.get_member = lambda uid: people.get(int(uid))
    bot = SimpleNamespace(get_guild=lambda gid: guild if int(gid) == GUILD else None)

    async def reach(user, level):
        current = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                       (GUILD, user))[0][0]
        total = sum(xp_for_level(n) for n in range(1, level + 1))
        res = await give_reward(bot, GUILD, user, "xp",
                                amount=total - current,
                                reason="booster test", source="leveling")
        check(f"user {user} reached level {level}",
              res["success"] and xp_progress(res["new_xp"])[0] == level)

    client, csrf = dashboard_client()
    item_a = execute("INSERT INTO shop_items (guild_id,name,price,type,enabled) "
                     "VALUES (?,?,100,'custom',1)", (GUILD, "Normal Cache"))
    item_b = execute("INSERT INTO shop_items (guild_id,name,price,type,enabled) "
                     "VALUES (?,?,100,'custom',1)", (GUILD, "Booster Cache"))

    # ── 1. Dashboard API: audience accepted, validated, persisted, listed ──
    defs = [
        # level 1: normal + booster in every supported track
        ("/api/leveling/reward", {"level": 1, "role_id": 600}),
        ("/api/leveling/reward", {"level": 1, "role_id": 601, "audience": "boosters"}),
        ("/api/leveling/currency-reward", {"level": 1, "currency": "balance", "amount": 100}),
        ("/api/leveling/currency-reward",
         {"level": 1, "currency": "balance", "amount": 500, "audience": "boosters"}),
        ("/api/leveling/shop-reward", {"level": 1, "item_id": item_a, "quantity": 1}),
        ("/api/leveling/shop-reward",
         {"level": 1, "item_id": item_b, "quantity": 2, "audience": "boosters"}),
        # level 2: SAME role id in both audiences (ledger identities must not collide)
        ("/api/leveling/reward", {"level": 2, "role_id": 600}),
        ("/api/leveling/reward", {"level": 2, "role_id": 600, "audience": "boosters"}),
        # level 3: currency only
        ("/api/leveling/currency-reward", {"level": 3, "currency": "balance", "amount": 10}),
        ("/api/leveling/currency-reward",
         {"level": 3, "currency": "balance", "amount": 20, "audience": "boosters"}),
        # level 4: concurrency target
        ("/api/leveling/currency-reward", {"level": 4, "currency": "balance", "amount": 1}),
        ("/api/leveling/currency-reward",
         {"level": 4, "currency": "balance", "amount": 1000, "audience": "boosters"}),
        # level 5: a real (non-lock) failure alongside a locked reward
        ("/api/leveling/reward", {"level": 5, "role_id": 700}),
        ("/api/leveling/currency-reward",
         {"level": 5, "currency": "balance", "amount": 7, "audience": "boosters"}),
    ]
    results = [post(client, csrf, path, body) for path, body in defs]
    check("API accepts normal and Boosters-only definitions in all three tracks",
          all(r.status_code == 200 and r.get_json().get("success") for r in results),
          str([r.get_json() for r in results]))

    bad = [post(client, csrf, "/api/leveling/reward",
                {"level": 9, "role_id": 600, "audience": "vip"}),
           post(client, csrf, "/api/leveling/currency-reward",
                {"level": 9, "currency": "balance", "amount": 5, "audience": "vip"}),
           post(client, csrf, "/api/leveling/shop-reward",
                {"level": 9, "item_id": item_a, "quantity": 1, "audience": "vip"})]
    check("API rejects an unknown audience and stores nothing",
          all(r.get_json().get("success") is False for r in bad)
          and rows("SELECT COUNT(*) FROM leveling_rewards WHERE level=9")[0][0] == 0
          and rows("SELECT COUNT(*) FROM leveling_currency_rewards WHERE level=9")[0][0] == 0
          and rows("SELECT COUNT(*) FROM leveling_shop_rewards WHERE level=9")[0][0] == 0)

    dup = post(client, csrf, "/api/leveling/shop-reward",
               {"level": 1, "item_id": item_a, "quantity": 1, "audience": "boosters"})
    check("accepted limitation: same shop item cannot be Everyone and Boosters at one level",
          dup.get_json().get("success") is False
          and "already" in dup.get_json().get("error", ""))

    cur_list = client.get("/api/leveling/currency-rewards").get_json()["rewards"]
    shop_list = client.get("/api/leveling/shop-rewards").get_json()["rewards"]
    with patch("utils.discord_user_cache.resolve_users", new=AsyncMock(return_value={})):
        page = client.get("/leveling")
    page_html = page.data.decode()
    check("GET APIs and the live page expose the audience",
          {(r["level"], r["amount"], r["audience"]) for r in cur_list
           if r["level"] == 1} == {(1, 100, "everyone"), (1, 500, "boosters")}
          and {(r["item_id"], r["audience"]) for r in shop_list}
          == {(item_a, "everyone"), (item_b, "boosters")}
          and page.status_code == 200 and 'data-audience="boosters"' in page_html
          and 'data-audience="everyone"' in page_html
          and "reward-filter-audience" in page_html and "reward-filter-level" in page_html
          and "reward-audience" in page_html
          and "currency-reward-audience" in page_html
          and "shop-reward-audience" in page_html)

    # ── 2. Crossing: entitlement only while Discord confirms boosting ──
    await reach(USER, 1)      # boosting
    await reach(PLAIN, 1)     # not boosting
    b_rows = claim_rows(USER)
    p_rows = claim_rows(PLAIN)
    booster_refs = {r[2] for r in b_rows if r[2].startswith("booster:")}
    check("boosting member gets normal AND Boosters-only entitlements at level 1",
          len(b_rows) == 6 and booster_refs
          == {"booster:601", "booster:balance", f"booster:{item_b}"},
          str(b_rows))
    check("non-boosting member gets only the normal entitlements",
          len(p_rows) == 3 and not any(r[2].startswith("booster:") for r in p_rows),
          str(p_rows))

    # ── 3. Claims: additive for the booster, normal-only for the rest ──
    r_boost = await claim_available(GUILD, USER, member=booster, bot=bot)
    check("booster claim fulfils normal + Boosters-only (additive, nothing locked)",
          len(r_boost["fulfilled"]) == 6 and not r_boost["failed"]
          and r_boost["locked"] == 0 and balance(USER)[0] == 600
          and booster.held() == [600, 601],
          f"{r_boost} bal={balance(USER)} roles={booster.held()}")
    r_plain = await claim_available(GUILD, PLAIN, member=plain, bot=bot)
    check("non-booster still receives the normal rewards unchanged",
          len(r_plain["fulfilled"]) == 3 and not r_plain["failed"]
          and r_plain["locked"] == 0 and balance(PLAIN)[0] == 100
          and plain.held() == [600], str(r_plain))

    # ── 4. No retroactive Booster rewards ──
    plain.premium_since = BOOSTING
    again = await claim_available(GUILD, PLAIN, member=plain, bot=bot)
    check("starting to boost later does NOT unlock Booster rewards for past levels",
          again["owned"] == 0 and not any(
              r[2].startswith("booster:") for r in claim_rows(PLAIN)),
          str(claim_rows(PLAIN)))
    await reach(PLAIN, 2)     # now boosting when crossing level 2
    p2 = [r for r in claim_rows(PLAIN) if r[0] == 2]
    check("crossing while boosting creates Booster entitlements for that level only",
          {r[2] for r in p2} == {"600", "booster:600"}, str(p2))
    plain.premium_since = None
    await claim_available(GUILD, PLAIN, member=plain, bot=bot)  # normal level-2 role
    p2 = [r for r in claim_rows(PLAIN) if r[0] == 2]
    check("same role id in both audiences keeps two distinct ledger identities",
          sorted(p2) == [(2, "role", "600", "fulfilled"),
                         (2, "role", "booster:600", "pending")], str(p2))

    # ── 5. Locked vs failed; stop / restart boosting; fail closed ──
    await reach(USER, 3)
    booster.premium_since = None
    lvl3 = await claim_available(GUILD, USER, member=booster, bot=bot)
    pend3 = [r for r in claim_rows(USER) if r[0] == 3]
    check("boosting stopped: normal claim delivers, Booster reward is locked, not failed",
          lvl3["locked"] == 2 and not lvl3["failed"] and balance(USER)[0] == 610
          and (3, "currency", "booster:balance", "pending") in pend3,
          f"{lvl3} {pend3}")
    old = [r for r in claim_rows(USER) if r[0] == 1]
    check("already-fulfilled Booster rewards are never revoked",
          all(r[3] == "fulfilled" for r in old) and booster.held() == [600, 601]
          and booster.removed == [])
    booster.premium_since = BOOSTING
    back = await claim_available(GUILD, USER, member=booster, bot=bot)
    check("boosting again makes the locked reward claimable",
          len(back["fulfilled"]) >= 1 and back["locked"] == 0
          and balance(USER)[0] == 630, f"{back} bal={balance(USER)}")

    # fail closed when status cannot be verified (no member)
    execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=? AND reward_level=3",
            (GUILD, PLAIN))
    plain.premium_since = BOOSTING
    await reach(PLAIN, 3)
    before = balance(PLAIN)[0]
    unverified = await claim_available(GUILD, PLAIN, member=None, bot=bot)
    pend = [r for r in claim_rows(PLAIN) if r[0] == 3]
    check("unverifiable boost status: Booster reward fails closed, normal still pays",
          unverified["booster_unverified"] and unverified["locked"] == 2
          and not unverified["failed"] and balance(PLAIN)[0] == before + 10
          and (3, "currency", "booster:balance", "pending") in pend, f"{unverified}")

    # level 5: a real delivery failure is reported separately from a lock
    await reach(USER, 4)
    await reach(USER, 5)
    booster.premium_since = None
    booster.fail_add = lambda role: (_ for _ in ()).throw(
        RuntimeError("simulated Discord failure")) if role.id == 700 else None
    mixed = await claim_available(GUILD, USER, member=booster, bot=bot)
    footer = leveling_cog.claim_result_footer(mixed)
    check("locked and failed are reported separately",
          len(mixed["failed"]) == 1 and mixed["locked"] >= 2
          and "Retryable failures: 1." in footer
          and f"Locked (Boosters only): {mixed['locked']}." in footer, footer)
    booster.fail_add = None
    check("no locked text when nothing is locked",
          "Locked" not in leveling_cog.claim_result_footer(
              {"fulfilled": [1], "failed": []}))

    # ── 6. Duplicate and concurrent claims ──
    booster.premium_since = BOOSTING
    # level 4 normal (1) was delivered in the mixed claim; 1000 + 7 remain
    before = balance(USER)[0]
    swarm = await asyncio.gather(*[
        claim_available(GUILD, USER, member=booster, bot=bot) for _ in range(6)])
    fulfilled_total = sum(len(r["fulfilled"]) for r in swarm)
    ledger = rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                  "AND status!='fulfilled' AND status!='failed'", (GUILD, USER))
    check("concurrent Claim All delivers every reward exactly once",
          balance(USER)[0] - before == 1000 + 7 and fulfilled_total >= 3
          and ledger[0][0] == 0,
          f"delta={balance(USER)[0] - before} fulfilled={fulfilled_total}")
    dup_rows = rows("SELECT reward_level,track,reward_ref,COUNT(*) FROM level_reward_claims "
                    "WHERE guild_id=? AND user_id=? GROUP BY 1,2,3 HAVING COUNT(*)>1",
                    (GUILD, USER))
    check("no duplicate ledger identities", dup_rows == [])
    replay = await claim_available(GUILD, USER, member=booster, bot=bot)
    check("repeat claim is a no-op", replay["owned"] == 0 and balance(USER)[0] - before == 1007)

    # ── 7. /level display + Claim All button on the real runtime path ──
    # PLAIN: level 3 booster row is pending; PLAIN is not boosting now
    plain.premium_since = None
    embed = await leveling_cog._level_embed(guild, plain, "level")
    text = "\n".join(f.value for f in embed.fields)
    check("/level hides the locked Booster reward on the Level page for a non-booster",
          "Nothing to claim right now." in text and "Boosters only" not in text, text)
    plain.premium_since = BOOSTING
    embed = await leveling_cog._level_embed(guild, plain, "level")
    text = "\n".join(f.value for f in embed.fields)
    check("/level shows the same reward as claimable for a booster",
          "Boosters only" in text and "pending" in text and "locked" not in text, text)

    claims_now = await list_claims(GUILD, PLAIN)
    check("Claim All button stays disabled when only locked rewards remain",
          not await leveling_cog._claim_button_available(GUILD, claims_now, False)
          and await leveling_cog._claim_button_available(GUILD, claims_now, True))
    plain.premium_since = None
    stats = await leveling_cog._level_embed(guild, plain, "stats")
    stats_text = "\n".join(f"{f.name}\n{f.value}" for f in stats.fields)
    check("Stats page counts locked rewards separately from waiting ones",
          "locked (Boosters only)" in stats_text, stats_text)

    view = leveling_cog.LevelRewardView(bot, GUILD, PLAIN)
    interaction = SimpleNamespace(
        user=plain, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock())
    await view.claim_all.callback(interaction)
    sent = interaction.edit_original_response.await_args.kwargs
    check("Claim All callback reports locked, not failed, for a non-booster",
          "Locked (Boosters only)" in sent["embed"].footer.text
          and "Retryable failures: 0." in sent["embed"].footer.text
          and view.claim_all.disabled, sent["embed"].footer.text)

    # ── 8. Migration: idempotent, legacy rows default to Everyone ──
    from database import init_db
    with closing(sqlite3.connect(DB_PATH)) as db:
        for table in ("leveling_rewards", "leveling_currency_rewards",
                      "leveling_shop_rewards"):
            db.execute(f"ALTER TABLE {table} DROP COLUMN audience")
        db.execute("INSERT INTO leveling_rewards (guild_id,level,role_id) VALUES (?,?,?)",
                   (GUILD + 1, 5, 123))
        db.commit()
    await init_db()
    await init_db()
    with closing(sqlite3.connect(DB_PATH)) as db:
        cols = {t: [c[1] for c in db.execute(f"PRAGMA table_info({t})")]
                for t in ("leveling_rewards", "leveling_currency_rewards",
                          "leveling_shop_rewards")}
        legacy = db.execute("SELECT audience FROM leveling_rewards WHERE guild_id=?",
                            (GUILD + 1,)).fetchall()
        boost_cols = [c[1] for c in db.execute("PRAGMA table_info(leveling_boost_rewards)")]
    check("migration is idempotent; legacy rows become Everyone; XP-boost table untouched",
          all(c.count("audience") == 1 for c in cols.values())
          and legacy == [("everyone",)] and "audience" not in boost_cols, str(cols))
    from utils.level_claims import _definitions
    async with __import__("aiosqlite").connect(DB_PATH) as db:
        plain_defs = await _definitions(db, GUILD + 1)
        check("legacy definition still produces its normal identity",
              (5, "role", "123") in plain_defs)

    print("ALL BOOSTER REWARD CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
