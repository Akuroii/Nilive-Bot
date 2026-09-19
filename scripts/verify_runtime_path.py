#!/usr/bin/env python3
"""RUNTIME-PATH verification for /rank (new 1280x853 card) + Prestige VI shop.

Unlike scripts/test_rank_integration.py (which imports the data/renderer
modules directly against its own seeded DB), this script drives the code
through the paths the REAL processes use:

  * the /rank command is loaded through main.load_cogs() — the exact
    cog-loading path start.sh -> `python -u main.py` uses — and the
    REGISTERED tree command's own callback is then invoked with a mock
    interaction, so the proof covers registration + execution, not a
    copy of the code;
  * the shop item is created through the real Flask dashboard app
    (dashboard.app routes + session + CSRF, via the env-gated
    /demo-login route), then purchased through cogs/shop.py's real
    process_purchase() entry point;
  * the migration check runs init_db() twice against a DB that simulates
    a pre-VI installation (prestige_vi_activations dropped, data present).

What this script can NOT prove (no Discord socket in a sandbox):
  * the live gateway/interaction round-trip, and that Discord actually
    syncs the command — that requires a real token + a real guild;
  * real Discord booster state (premium_since is mocked).

Usage:
    python3 scripts/verify_runtime_path.py            # full run
    python3 scripts/verify_runtime_path.py --migration-only   # internal
"""
import asyncio
import inspect
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="nilive_runtime_verify_")
os.environ["DATABASE_PATH"] = os.path.join(TMP, "verify.db")
os.environ["OWNER_ID"] = "424242"        # == demo user => dashboard super admin
os.environ["DISCORD_TOKEN"] = "x" * 20 + "." + "x" * 20 + "." + "x" * 20  # never connects
os.environ["SECRET_KEY"] = "runtime-verification-only-0123456789abcdef"
os.environ["DASHBOARD_DEMO_LOGIN"] = "1"
os.environ["DASHBOARD_DEMO_USER_ID"] = "424242"
os.environ["DASHBOARD_DEMO_GUILD_ID"] = "777"

GUILD = 777
USER = 424242          # /rank target (also the dashboard admin)
BUYER = 555            # shop buyer
VI_PRICE = 100000

FAILURES = []


def check(section, ok, detail):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {section}: {detail}")
    if not ok:
        FAILURES.append(f"{section}: {detail}")
    return ok


# ─────────────────────────────────────────────────────────────────────────
# Mocks (only what the real code paths actually touch)
# ─────────────────────────────────────────────────────────────────────────
class MockAvatar:
    url = ""   # empty -> renderer draws the placeholder ring, no network


def mock_member(uid, name, booster):
    return SimpleNamespace(
        id=uid, mention=f"<@{uid}>", display_name=name,
        display_avatar=MockAvatar(),
        joined_at=datetime(2024, 1, 12, tzinfo=timezone.utc),
        premium_since=(datetime(2024, 6, 1, tzinfo=timezone.utc)
                       if booster else None),
    )


class MockResponse:
    def __init__(self):
        self.messages = []

    async def defer(self):
        pass

    async def send_message(self, content=None, embed=None, ephemeral=None,
                           **kw):
        self.messages.append({"content": content, "embed": embed})


class MockFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, file=None, **kw):
        self.sent.append({"content": content, "embed": embed, "file": file})


def mock_interaction(guild_id, member):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        user=member,
        client=SimpleNamespace(),
        response=MockResponse(),
        followup=MockFollowup(),
    )


# ─────────────────────────────────────────────────────────────────────────
# SECTION 1 — DASHBOARD (real Flask app object, real routes, real session)
# ─────────────────────────────────────────────────────────────────────────
def section_dashboard():
    print("\n══ 1. DASHBOARD — real app routes (Flask test client) ══")
    import dashboard.app as dash
    from database import DB_PATH
    import aiosqlite

    dash.app.config["TESTING"] = True
    c = dash.app.test_client()

    r = c.get("/demo-login")
    check("dashboard login", r.status_code == 302,
          f"/demo-login -> {r.status_code} (real create_session path)")

    html = c.get("/shop").get_data(as_text=True)
    check("shop page offers tier 6", 'value="6"' in html,
          "GET /shop HTML contains <option value=\"6\">")
    check("shop page labels VI", "VI — Booster-only" in html,
          "GET /shop HTML labels the option 'VI — Booster-only'")

    with c.session_transaction() as s:
        csrf = s.get("csrf_token")
    check("csrf session", bool(csrf), "session carries csrf_token")

    payload = {
        "name": "Prestige VI", "description": "Booster-only prestige tier",
        "price": VI_PRICE, "type": "prestige", "prestige_tier": 6,
        "duration_hours": 0, "required_level": 0, "rarity": "mythical",
        "featured": 0,
    }
    r = c.post("/api/shop/item", json=payload,
               headers={"X-CSRF-Token": csrf})
    body = r.get_json() or {}
    check("API accepts tier 6", r.status_code == 200 and body.get("success"),
          f"POST /api/shop/item prestige_tier=6 -> {r.status_code} {body}")

    r = c.post("/api/shop/item", json={**payload, "name": "bad",
                                       "prestige_tier": 7},
               headers={"X-CSRF-Token": csrf})
    body = r.get_json() or {}
    check("API rejects tier 7", not body.get("success"),
          f"POST prestige_tier=7 -> rejected ({body.get('error')})")

    partial = c.get("/api/shop/items").get_data(as_text=True)
    check("item list shows VI", "Prestige VI" in partial,
          "GET /api/shop/items partial renders 'Prestige VI'")

    async def q():
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT prestige_tier, price FROM shop_items "
                "WHERE guild_id=? AND name='Prestige VI'", (GUILD,)
            )).fetchone()
            return row
    row = asyncio.run(q())
    check("DB persists tier 6", row == (6, VI_PRICE),
          f"shop_items row for 'Prestige VI': {row}")

    async def qid():
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT id FROM shop_items WHERE guild_id=? "
                "AND name='Prestige VI'", (GUILD,)
            )).fetchone()
            return row[0]
    return asyncio.run(qid())


# ─────────────────────────────────────────────────────────────────────────
# SECTION 2 — BOT /rank (real load_cogs registration + real callback)
# ─────────────────────────────────────────────────────────────────────────
async def section_bot_rank():
    print("\n══ 2. BOT /rank — real cog load + registered callback ══")
    import main as main_mod
    from database import init_db, DB_PATH
    import aiosqlite
    from PIL import Image

    await init_db()
    await main_mod.load_cogs()          # the exact startup path of main.py

    cmd = main_mod.bot.tree.get_command("rank")
    check("rank registered", cmd is not None, "tree has a global /rank")
    cb = cmd.callback
    src = inspect.getsource(cb)
    check("registered callback is NEW renderer path",
          "utils.rank_card_renderer" in src and "800, 200" not in src,
          f"callback source @ {os.path.basename(inspect.getsourcefile(cb))}:"
          f"{inspect.getsourcelines(cb)[1]}")

    # seed the target member exactly like a real guild DB would look
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO guild_settings (guild_id) "
                         "VALUES (?)", (GUILD,))
        await db.execute("INSERT OR REPLACE INTO levels (guild_id,user_id,"
                         "xp,level,prestige) VALUES (?,?,?,?,?)",
                         (GUILD, USER, 582450, 84, 4))
        await db.execute("INSERT OR REPLACE INTO economy (guild_id,user_id,"
                         "balance,diamonds) VALUES (?,?,?,?)",
                         (GUILD, USER, 24520, 138))
        await db.execute("INSERT OR REPLACE INTO activity_stats (guild_id,"
                         "user_id,date,messages_count,words_count,"
                         "voice_minutes,forum_posts_count) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (GUILD, USER, "2024-01-01", 34725, 0, 10920, 0))
        await db.commit()

    cog = main_mod.bot.get_cog("Leveling")
    params = list(inspect.signature(cb).parameters)

    async def invoke(member):
        itx = mock_interaction(GUILD, member)
        if params and params[0] == "self":
            await cb(cog, itx, member=member)
        else:
            await cb(itx, member=member)
        return itx

    # 2a. plain member (permanent prestige IV)
    itx = await invoke(mock_member(USER, "Shadow", booster=False))
    sent = itx.followup.sent
    ok = len(sent) == 1 and sent[0]["file"] is not None
    check("rank sends attachment", ok, f"followup.sent = {len(sent)} entries")
    png = sent[0]["file"].fp.read()
    img = Image.open(__import__("io").BytesIO(png))
    check("card is 1280x853", img.size == (1280, 853),
          f"rendered PNG size = {img.size}")

    # 2b. booster + VI activation -> effective VI (different pixels)
    from utils.rank_card_data import get_rank_card_data
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO prestige_vi_activations "
                         "(guild_id, user_id) VALUES (?, ?)", (GUILD, USER))
        await db.commit()
    data = await get_rank_card_data(GUILD, USER,
                                    member=mock_member(USER, "Shadow", True))
    check("effective VI when boosting", data["effective_prestige"] == 6,
          f"effective_prestige = {data['effective_prestige']} (booster + activation)")
    itx_vi = await invoke(mock_member(USER, "Shadow", booster=True))
    png_vi = itx_vi.followup.sent[0]["file"].fp.read()

    # 2c. boost expired (activation row still present) -> permanent tier again
    data = await get_rank_card_data(GUILD, USER,
                                    member=mock_member(USER, "Shadow", False))
    check("expiry returns to permanent", data["effective_prestige"] == 4,
          f"effective_prestige = {data['effective_prestige']} (not boosting)")
    itx_exp = await invoke(mock_member(USER, "Shadow", booster=False))
    png_exp = itx_exp.followup.sent[0]["file"].fp.read()

    check("VI card differs from non-VI", png_vi != png_exp,
          "booster-VI render != post-expiry render (bytes differ)")
    check("re-boost renders VI again",
          (await invoke(mock_member(USER, "Shadow", booster=True))
           ).followup.sent[0]["file"].fp.read() == png_vi,
          "same activation row + new boost reproduces the VI card")

    # save artifacts for eyeballing
    for name, blob in (("verify_rank_normal.png", png),
                       ("verify_rank_vi.png", png_vi),
                       ("verify_rank_expired.png", png_exp)):
        with open(os.path.join(TMP, name), "wb") as fh:
            fh.write(blob)
    print(f"  [info] card artifacts written to {TMP}")


# ─────────────────────────────────────────────────────────────────────────
# SECTION 3 — BOT SHOP (real process_purchase on the dashboard-made item)
# ─────────────────────────────────────────────────────────────────────────
async def section_bot_shop(item_id):
    print("\n══ 3. BOT SHOP — real process_purchase() on dashboard item ══")
    from database import DB_PATH
    import aiosqlite
    from cogs.shop import process_purchase
    from utils.prestige import get_effective_prestige

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO economy (guild_id,user_id,"
                         "balance,diamonds) VALUES (?,?,?,?)",
                         (GUILD, BUYER, 150000, 0))
        await db.execute("INSERT OR REPLACE INTO levels (guild_id,user_id,"
                         "xp,level,prestige) VALUES (?,?,?,?,?)",
                         (GUILD, BUYER, 1000, 5, 3))
        await db.commit()

    # 3a. non-booster -> rejected before any DB write
    itx = mock_interaction(GUILD, mock_member(BUYER, "Muffin", booster=False))
    await process_purchase(itx, item_id)
    msg = itx.response.messages[0]["content"]
    check("non-booster rejected", "Booster-only" in msg, f"reply: {msg!r}")

    async def balance():
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (GUILD, BUYER))).fetchone()
            return row[0]
    check("no charge on rejection", await balance() == 150000,
          f"balance still {await balance()}")

    # 3b. booster -> purchase succeeds
    itx = mock_interaction(GUILD, mock_member(BUYER, "Muffin", booster=True))
    await process_purchase(itx, item_id)
    check("booster purchase succeeds",
          itx.response.messages and itx.response.messages[0]["embed"] is not None
          and "VI" in (itx.response.messages[0]["embed"].title or ""),
          f"embed title: {itx.response.messages[0]['embed'].title!r}")

    async def q(sql, args):
        async with aiosqlite.connect(DB_PATH) as db:
            return await (await db.execute(sql, args)).fetchall()
    rows = await q("SELECT 1 FROM prestige_vi_activations WHERE guild_id=? "
                   "AND user_id=?", (GUILD, BUYER))
    check("activation persisted", len(rows) == 1,
          "prestige_vi_activations row exists for buyer")
    check("balance reset to 0 by purchase", await balance() == 0,
          f"balance after purchase = {await balance()} "
          "(price acts as minimum; reset to 0 like Prestige I-V)")
    rows = await q("SELECT item_name, price_paid FROM purchase_history "
                   "WHERE guild_id=? AND user_id=?", (GUILD, BUYER))
    check("purchase history logged",
          any(r[0] == "Prestige VI" and r[1] == 150000 for r in rows),
          f"purchase_history: {rows} "
          "(price_paid = whole pre-purchase balance, as for I-V)")
    rows = await q("SELECT prestige FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, BUYER))
    check("permanent tier untouched", rows[0][0] == 3,
          f"levels.prestige still {rows[0][0]} (VI never written permanent)")

    # 3c. double purchase -> rejected
    itx = mock_interaction(GUILD, mock_member(BUYER, "Muffin", booster=True))
    await process_purchase(itx, item_id)
    msg = itx.response.messages[0]["content"]
    check("re-purchase rejected", "already active" in msg, f"reply: {msg!r}")

    # 3d. effective tier follows boost status
    eff_boost = await get_effective_prestige(GUILD, BUYER, is_booster=True)
    eff_unboost = await get_effective_prestige(GUILD, BUYER, is_booster=False)
    check("effective tier semantics",
          eff_boost == 6 and eff_unboost == 3,
          f"boosting -> {eff_boost}, not boosting -> {eff_unboost}")


# ─────────────────────────────────────────────────────────────────────────
# SECTION 4 — MIGRATION (pre-VI database upgraded by init_db)
# ─────────────────────────────────────────────────────────────────────────
def section_migration():
    print("\n══ 4. MIGRATION — init_db on a pre-VI database ══")
    env = dict(os.environ)
    env["DATABASE_PATH"] = os.path.join(TMP, "pre_vi.db")
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--migration-only"],
        env=env, capture_output=True, text=True, timeout=180)
    print("  " + proc.stdout.strip().replace("\n", "\n  "))
    check("migration subprocess", proc.returncode == 0,
          f"exit={proc.returncode}")
    if proc.returncode != 0:
        print(proc.stderr)


def migration_only():
    """Runs in a fresh process with its own DATABASE_PATH."""
    import aiosqlite
    from database import DB_PATH, init_db

    async def main():
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            # simulate a real pre-VI installation: table absent, data present
            await db.execute("DROP TABLE prestige_vi_activations")
            await db.execute("INSERT INTO levels (guild_id,user_id,xp,level,"
                             "prestige) VALUES (1, 10, 500, 7, 2)")
            await db.execute("INSERT INTO economy (guild_id,user_id,balance,"
                             "diamonds) VALUES (1, 10, 999, 5)")
            await db.commit()

        await init_db()   # what main.py / dashboard/app.py run at startup

        async with aiosqlite.connect(DB_PATH) as db:
            tbl = await (await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='prestige_vi_activations'")).fetchone()
            print(f"[{'PASS' if tbl else 'FAIL'}] table created on re-init: "
                  f"{bool(tbl)}")
            lvl = await (await db.execute(
                "SELECT xp, level, prestige FROM levels "
                "WHERE guild_id=1 AND user_id=10")).fetchone()
            eco = await (await db.execute(
                "SELECT balance, diamonds FROM economy "
                "WHERE guild_id=1 AND user_id=10")).fetchone()
            print(f"[{'PASS' if lvl == (500, 7, 2) else 'FAIL'}] "
                  f"existing levels row intact: {lvl}")
            print(f"[{'PASS' if eco == (999, 5) else 'FAIL'}] "
                  f"existing economy row intact: {eco}")
            return bool(tbl) and lvl == (500, 7, 2) and eco == (999, 5)

    return 0 if asyncio.run(main()) else 1


# ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--migration-only" in sys.argv:
        sys.exit(migration_only())

    print("Nilive runtime-path verification")
    print(f"temp DB: {os.environ['DATABASE_PATH']}")
    item_id = section_dashboard()
    asyncio.run(section_bot_rank())
    asyncio.run(section_bot_shop(item_id))
    section_migration()

    print("\n" + "═" * 64)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("RESULT: ALL RUNTIME CHECKS PASSED")
    print("(proves registration + execution paths of the real bot cog and")
    print(" the real dashboard app; the live Discord gateway round-trip")
    print(" still requires a real token/guild and cannot run here.)")
