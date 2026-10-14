#!/usr/bin/env python3
"""
Phase 5 — manual browser preview launcher for the Minigames v2 UI.

Discord OAuth cannot complete inside a sandboxed preview, so this
launcher:
  1. builds a FRESH realistic database — a guild seeded with REAL v1
     data (config, 4 tiers incl. one disabled + a temp-role duration,
     legacy log rows), then runs the production migration
     (utils/minigame_store.ensure_tables → run_migration) exactly as
     the first production deploy would;
  2. adds representative v2 content on top (nested category, six
     templates covering every game type / enabled / rotation / pool
     state, v2 + legacy history rows);
  3. boots the real Flask dashboard with the env-gated /demo-login
     route (DASHBOARD_DEMO_LOGIN=1 — off in production) so a browser
     session can be created without OAuth.

Run:
    python3 scripts/preview_minigames_dashboard.py [port]
Then open:  http://localhost:<port>/demo-login
            (or the sandbox preview host) and follow the redirect.

Nothing here is production code — the demo-login route only exists
when DASHBOARD_DEMO_LOGIN=1 and this script is the only thing that
sets it.
"""
import os
import sys
import time
import sqlite3
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5377
DEMO_GUILD = 900000000000000001
DEMO_USER = 900000000000000001
DEMO_CHANNEL = 910000000000000001

_tmp = tempfile.mkdtemp(prefix="mg_preview_")
DB_PATH = os.path.join(_tmp, "preview.db")

os.environ["DATABASE_PATH"] = DB_PATH
os.environ["OWNER_ID"] = str(DEMO_USER)          # guild-blind dev bypass
os.environ.setdefault("SECRET_KEY",
                      "preview-only-not-for-production-0123456789abcdef")
os.environ["DASHBOARD_DEMO_LOGIN"] = "1"
os.environ["DASHBOARD_DEMO_USER_ID"] = str(DEMO_USER)
os.environ["DASHBOARD_DEMO_GUILD_ID"] = str(DEMO_GUILD)

# ── 1. seed the v1 production-shaped data (as the OLD cog wrote it) ─
conn = sqlite3.connect(DB_PATH)
conn.executescript("""
CREATE TABLE minigames_config (
    guild_id            INTEGER PRIMARY KEY,
    enabled             INTEGER DEFAULT 1,
    channel_id          INTEGER,
    min_events_per_week INTEGER DEFAULT 5,
    max_events_per_week INTEGER DEFAULT 10,
    events_this_week    INTEGER DEFAULT 0,
    week_start_date     TEXT,
    last_check_date     TEXT,
    claim_seconds       INTEGER DEFAULT 300,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE minigames_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    event_date         TEXT NOT NULL,
    tier               TEXT NOT NULL,
    channel_id         INTEGER,
    message_id         INTEGER,
    winner_id          INTEGER,
    winner_display_name TEXT,
    forced             INTEGER DEFAULT 0,
    fired_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE minigames_tiers (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id               INTEGER NOT NULL,
    tier                   TEXT NOT NULL,
    weight                 INTEGER DEFAULT 1,
    reward_type            TEXT NOT NULL,
    reward_value           TEXT NOT NULL,
    reward_duration_hours  INTEGER,
    enabled                INTEGER DEFAULT 1,
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
""")
G = DEMO_GUILD
conn.execute("INSERT INTO minigames_config "
             "(guild_id, enabled, channel_id, min_events_per_week, "
             " max_events_per_week, events_this_week, week_start_date) "
             "VALUES (?,?,?,3,8,1,'2026-08-24')", (G, 1, DEMO_CHANNEL))
for tier, w, rt, rv, dur, en in [
    ("bronze", 50, "coins", "100", None, 1),
    ("silver", 30, "xp", "250", None, 1),
    ("gold", 10, "diamonds", "5", None, 1),
    ("platinum", 5, "temp_role", "970000000000000099", 24, 0),
]:
    conn.execute("INSERT INTO minigames_tiers "
                 "(guild_id, tier, weight, reward_type, reward_value, "
                 " reward_duration_hours, enabled) VALUES (?,?,?,?,?,?,?)",
                 (G, tier, w, rt, rv, dur, en))
for date, tier, wid, wname, forced, fired in [
    ("2026-07-20", "bronze", 920000000000000101, "Amina", 0, "2026-07-20T18:22:00Z"),
    ("2026-07-24", "gold", None, None, 0, "2026-07-24T20:05:00Z"),          # unclaimed
    ("2026-07-29", "silver", 920000000000000102, "Omar", 0, "2026-07-29T19:40:00Z"),
    ("2026-08-02", "bronze", 920000000000000103, "Laila", 1, "2026-08-02T23:59:00Z"),
    ("2026-08-07", "silver", 920000000000000104, "Yusuf", 0, "2026-08-07T17:30:00Z"),
]:
    conn.execute("INSERT INTO minigames_log "
                 "(guild_id, event_date, tier, channel_id, winner_id, "
                 " winner_display_name, forced, fired_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (G, date, tier, DEMO_CHANNEL, wid, wname, forced, fired))
conn.commit()
conn.close()
print(f"v1 data seeded at {DB_PATH}")

# ── 2. run the PRODUCTION migration path, then add v2 content ──────
import asyncio  # noqa: E402
import aiosqlite  # noqa: E402
from utils import minigame_store as store  # noqa: E402


async def build_v2():
    await store.ensure_tables()            # creates v2 tables + migrates tiers
    G = DEMO_GUILD
    cats = {c["name"]: c for c in await store.get_categories_tree(G)}
    bronze = cats["Bronze"]
    silver = cats["Silver"]
    gold = cats["Gold"]
    plat = cats["Platinum"]

    btn = await store.create_category(G, "Button Games",
                                      parent_id=bronze["id"], weight=30,
                                      emoji="🔘")
    await store.create_category(G, "Word Games", parent_id=gold["id"],
                                weight=8, emoji="📝")

    # math — full pool incl. a temp role
    await store.create_template(
        G, bronze["id"], "Weekend Math", "math",
        embed={"title": "🧮 Weekend Math",
               "description": "What is 7 × 8?", "color": 0x5865f2,
               "author": {"name": "Nero Games"}},
        config={"answers": ["54", "56", "63"], "correct": 1, "seconds": 45},
        rewards=[{"reward_type": "xp", "reward_value": "300", "weight": 3},
                 {"reward_type": "coins", "reward_value": "100", "weight": 2},
                 {"reward_type": "temp_role",
                  "reward_value": "970000000000000099", "weight": 1,
                  "duration_hours": 24}],
        channel_id=DEMO_CHANNEL)
    # colors — image in the embed (the puzzle)
    await store.create_template(
        G, btn["id"], "Colors: Middle One", "colors",
        embed={"title": "🎨 Which color is in the middle?",
               "description": "Three swatches — pick the middle one.",
               "image": {"url": "https://picsum.photos/seed/colorgame/640/360"}},
        config={"answers": ["Red", "Green", "Blue"], "correct": 1,
                "seconds": 30})
    # quick click
    await store.create_template(
        G, bronze["id"], "Quick Click Rush", "quick_click",
        embed={"title": "⚡ Quick Click Rush",
               "description": "A button appears — click it before "
                              "anyone else!"},
        config={"buttons": 5, "reveal_min": 3, "reveal_max": 8,
                "wait_after": 10},
        rewards=[{"reward_type": "coins", "reward_value": "50", "weight": 1}])
    # wheel — EMPTY pool (valid, D11)
    await store.create_template(
        G, silver["id"], "Lucky Wheel", "wheel",
        embed={"title": "🎡 Lucky Wheel",
               "description": "Join the wheel — one lucky winner."},
        config={"join_seconds": 30}, rewards=[])
    # emoji — DISABLED template (D9 state)
    await store.create_template(
        G, gold["id"], "Emoji Guess", "emoji", enabled=False,
        embed={"title": "😀 Emoji Guess",
               "description": "What does this mean: 🎩"},
        config={"answers": ["A party", "A magic trick", "Hide and seek"],
                "correct": 1, "seconds": 30},
        rewards=[{"reward_type": "xp", "reward_value": "150", "weight": 1}])
    # rps — enabled but rotation OFF (build→test→enable-later flow)
    await store.create_template(
        G, plat["id"], "RPS Showdown", "rps", auto_spawn=False,
        embed={"title": "✊ Rock Paper Scissors",
               "description": "Take a seat. Best play wins."},
        config={"seating_seconds": 60, "choice_seconds": 60},
        rewards=[{"reward_type": "diamonds", "reward_value": "1", "weight": 1}])

    # v2 history rows (completed with winners, no_winner, test mode)
    tpls = await store.list_templates(G)
    by_name = {t["name"]: t for t in tpls}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async def hist(tpl, mode, status, hours_ago, winners, participants):
        ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - hours_ago * 3600))
        rid = await store.start_run(G, tpl["id"], tpl["name"],
                                    tpl["category_id"],
                                    (await store.get_category(
                                        G, tpl["category_id"]))["name"],
                                    tpl["game_type"], mode, DEMO_CHANNEL)
        # backdate started_at so the history spans a couple of days
        async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
            await db.execute(
                "UPDATE minigames_log SET started_at=? WHERE id=?",
                (ts, rid))
            await db.commit()
        await store.finish_run(rid, status, participants, winners)
        return rid

    await hist(by_name["Weekend Math"], "auto", "completed", 50,
               [{"id": 920000000000000101, "name": "Amina",
                 "reward_type": "xp", "reward_value": "300", "status": "won"},
                {"id": 920000000000000102, "name": "Omar",
                 "reward_type": "coins", "reward_value": "100",
                 "status": "won"},
                {"id": 920000000000000103, "name": "Laila",
                 "reward_type": "xp", "reward_value": "300",
                 "status": "failed", "error": "already at cap"}],
               [{"id": 920000000000000101, "display_name": "Amina"},
                {"id": 920000000000000102, "display_name": "Omar"},
                {"id": 920000000000000103, "display_name": "Laila"},
                {"id": 920000000000000104, "display_name": "Yusuf"}])
    await hist(by_name["Lucky Wheel"], "auto", "no_winner", 26, [], [])
    await hist(by_name["Quick Click Rush"], "test", "completed", 3,
               [{"id": 920000000000000104, "name": "Yusuf",
                 "reward_type": "coins", "reward_value": "50",
                 "status": "won"}],
               [{"id": 920000000000000104, "display_name": "Yusuf"},
                {"id": 920000000000000101, "display_name": "Amina"}])

    hist_count = len(await store.get_history(G, limit=50))
    print(f"v2 content ready: {len(tpls)} templates, {hist_count} history rows")


asyncio.run(build_v2())

# ── 3. boot the real dashboard ─────────────────────────────────────
import dashboard.app as dapp  # noqa: E402

print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  Minigames v2 — manual preview                                   ║
║                                                                  ║
║  Open:   http://localhost:{PORT}/demo-login                          ║
║          (or the sandbox preview host, same path)                ║
║                                                                  ║
║  The demo guild contains: migrated v1 tiers (4, one disabled),   ║
║  6 templates (all game types, enabled/disabled/rotation-off,     ║
║  empty pool), v1 + v2 history. Channel pickers are EMPTY in the  ║
║  sandbox (no Discord bot token) — everything else is live.       ║
╚══════════════════════════════════════════════════════════════════╝
""")
dapp.app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
