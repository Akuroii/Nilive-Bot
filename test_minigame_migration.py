#!/usr/bin/env python3
"""
Minigames v2 — Phase 5 (plan §3/§21.6) — migration verification suite.

Builds a REALISTIC v1 production-shaped database (tiers, config,
legacy log rows, duplicate tier rows, disabled tiers, temp-role
durations, a hand-built-category guild, a tiers-only guild), then
verifies the tier→category migration per the Phase 5 gate:

  1. migration runs against REAL v1 data (not only fresh DBs)
  2. every important v1 datum maps into v2 (category name/weight/
     enabled, preset rewards in canonical shape, log/history, config)
  3. fully idempotent — 4 consecutive runs, nothing duplicated
  4. failure/rollback — a fault injected mid-migration leaves the
     guild in a clean pre-migration state (no partial rows), other
     guilds still migrate, startup (ensure_tables) never dies
  5. legacy tables/columns kept (rollback path per plan §3.4)
  6. legacy history readable after migration (v2 history API shape)
  7. v2 snapshots stay independent of live category/template changes
  8. read-time normalization of legacy-shaped preset rows

Run:  python3 scripts/test_minigame_migration.py
(uses the system python — aiosqlite only, no flask needed)
"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="mgv5_")
DB_PATH = os.path.join(_TMP, "v1prod.db")
os.environ["DATABASE_PATH"] = DB_PATH
os.environ["OWNER_ID"] = "999999999"          # boots the app layer only
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

G_A, G_B, G_C, G_D, G_E = 1001, 2002, 3003, 4004, 5005

PASS = FAIL = 0
FAILURES = []


def check(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name + (f" — {extra}" if extra else ""))
        print(f"  FAIL  {name}  {extra}")


def section(t):
    print(f"\n== {t} ==")


def db_exec(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def db_query(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════════
# FIXTURE — a production-shaped v1 database, as the OLD cog would
# have created it (exact v1 schema from git history).
# ═══════════════════════════════════════════════════════════════════
db_exec("""
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
        updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
db_exec("""
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
        fired_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
db_exec("""
    CREATE TABLE minigames_tiers (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id               INTEGER NOT NULL,
        tier                   TEXT NOT NULL,
        weight                 INTEGER DEFAULT 1,
        reward_type            TEXT NOT NULL,
        reward_value           TEXT NOT NULL,
        reward_duration_hours  INTEGER,
        enabled                INTEGER DEFAULT 1,
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

# ── Guild A: a well-used server — 4 tiers (+ a legacy duplicate
#    bronze row the old UI allowed), one DISABLED tier, a temp-role
#    with duration, configured channel/pacing, 6 varied log rows.
db_exec("INSERT INTO minigames_config "
        "(guild_id, enabled, channel_id, min_events_per_week, max_events_per_week, "
        " events_this_week, week_start_date, last_check_date, claim_seconds) "
        "VALUES (?,?,?,3,9,2,'2026-08-24','2026-08-29',300)", (G_A, 1, 5000))
for tier, w, rt, rv, dur, en in [
    ("bronze", 50, "coins", "100", None, 1),
    ("silver", 30, "xp", "250", None, 1),
    ("gold", 10, "diamonds", "5", None, 1),
    ("platinum", 5, "temp_role", "111222333444555666", 24, 0),   # disabled
    ("bronze", 12, "coins", "999", None, 1),                     # legacy duplicate
]:
    db_exec("INSERT INTO minigames_tiers "
            "(guild_id, tier, weight, reward_type, reward_value, reward_duration_hours, enabled) "
            "VALUES (?,?,?,?,?,?,?)", (G_A, tier, w, rt, rv, dur, en))
for date, tier, chan, mid, wid, wname, forced, fired in [
    ("2026-08-01", "bronze", 5000, 90001, 111, "Alice", 0, "2026-08-01T18:22:00Z"),
    ("2026-08-03", "gold", 5000, 90002, None, None, 0, "2026-08-03T20:05:00Z"),  # unclaimed
    ("2026-08-05", "silver", 5000, 90003, 222, "Bob", 0, "2026-08-05T19:40:00Z"),
    ("2026-08-08", "bronze", 5000, 90004, 333, "Carol", 0, "2026-08-08T21:10:00Z"),
    ("2026-08-12", "gold", 5000, 90005, 444, "Dave", 1, "2026-08-12T23:59:00Z"),  # forced
    ("2026-08-15", "silver", 5000, 90006, 555, "Eve", 0, "2026-08-15T17:30:00Z"),
]:
    db_exec("INSERT INTO minigames_log "
            "(guild_id, event_date, tier, channel_id, message_id, winner_id, "
            " winner_display_name, forced, fired_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (G_A, date, tier, chan, mid, wid, wname, forced, fired))

# ── Guild B: sparse — no config row, single tier, 2 log rows.
for tier, w, rt, rv in [("gold", 20, "coins", "50")]:
    db_exec("INSERT INTO minigames_tiers "
            "(guild_id, tier, weight, reward_type, reward_value) VALUES (?,?,?,?,?)",
            (G_B, tier, w, rt, rv))
for date, tier, fired in [("2026-08-02", "gold", "2026-08-02T12:00:00Z"),
                          ("2026-08-06", "gold", "2026-08-06T12:00:00Z")]:
    db_exec("INSERT INTO minigames_log "
            "(guild_id, event_date, tier, fired_at) VALUES (?,?,?,?)",
            (G_B, date, tier, fired))

# ── Guild C: already has a HAND-BUILT category (post-v2 usage) plus
#    legacy tiers — the migration must NOT touch it.
db_exec("""
    CREATE TABLE minigame_categories (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id             INTEGER NOT NULL,
        parent_id            INTEGER,
        name                 TEXT NOT NULL,
        weight               INTEGER NOT NULL DEFAULT 1,
        sort_order           INTEGER NOT NULL DEFAULT 0,
        emoji                TEXT,
        color                TEXT,
        default_rewards_json TEXT,
        enabled              INTEGER NOT NULL DEFAULT 1,
        created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
db_exec("INSERT INTO minigames_tiers "
        "(guild_id, tier, weight, reward_type, reward_value) VALUES (?,?,?,?,?)",
        (G_C, "silver", 10, "coins", "25"))
db_exec("INSERT INTO minigame_categories (guild_id, parent_id, name, weight, sort_order, enabled) "
        "VALUES (?, NULL, 'Hand Built', 7, 0, 1)", (G_C,))

# ── Guild D: tiers only, nothing else (minimal).
db_exec("INSERT INTO minigames_tiers "
        "(guild_id, tier, weight, reward_type, reward_value) VALUES (?,?,?,?,?)",
        (G_D, "bronze", 3, "xp", "75"))

# Guild E (the fault-injection target) gets its tier rows LATER — the
# fault test must be guild E's FIRST migration attempt, so its rows are
# added after the run-1 / idempotency sections.

V1_TIER_COUNT = db_query("SELECT COUNT(*) FROM minigames_tiers")[0][0]
V1_LOG_COUNT = db_query("SELECT COUNT(*) FROM minigames_log")[0][0]
print(f"fixture ready: {V1_TIER_COUNT} tier rows, {V1_LOG_COUNT} log rows, 5 guilds")

import aiosqlite  # noqa: E402
from utils import minigame_store as store  # noqa: E402
from dashboard.utils.async_utils import run_async  # noqa: E402


def cats_of(guild):
    return db_query("SELECT name, weight, enabled, default_rewards_json, sort_order "
                    "FROM minigame_categories WHERE guild_id=? ORDER BY sort_order, id",
                    (guild,))


# ═══════════════════════════════════════════════════════════════════
section("MIGRATION — run 1 on realistic v1 data")
run_async(store.ensure_tables())

ca = cats_of(G_A)
check(len(ca) == 4, "guild A: 4 categories from 5 tier rows (duplicate skipped)",
      str([c[0] for c in ca]))
names = [c[0] for c in ca]
check(names == ["Bronze", "Silver", "Gold", "Platinum"],
      "guild A: tier names → category names (title case, insertion order)", str(names))
by_name = {c[0]: c for c in ca}
check(by_name["Bronze"][1] == 50 and by_name["Silver"][1] == 30
      and by_name["Gold"][1] == 10 and by_name["Platinum"][1] == 5,
      "guild A: weights carried over exactly")
check(by_name["Platinum"][2] == 0 and all(by_name[n][2] == 1 for n in ("Bronze", "Silver", "Gold")),
      "guild A: enabled state carried over (platinum stays disabled)")
import json  # noqa: E402
bronze_preset = json.loads(by_name["Bronze"][3])
plat_preset = json.loads(by_name["Platinum"][3])
check(bronze_preset == [{"reward_type": "coins", "reward_value": "100", "weight": 1}],
      "guild A: preset in CANONICAL shape (reward_type/reward_value)", str(bronze_preset))
check(plat_preset == [{"reward_type": "temp_role", "reward_value": "111222333444555666",
                       "weight": 1, "duration_hours": 24}],
      "guild A: temp_role duration preserved in preset", str(plat_preset))
check(db_query("SELECT COUNT(*) FROM minigame_categories WHERE guild_id=?", (G_A,))[0][0] == 4,
      "guild A: duplicate tier row did not create a second Bronze")

cb = cats_of(G_B)
check(len(cb) == 1 and cb[0][0] == "Gold" and cb[0][1] == 20,
      "guild B: single tier migrated (no config row needed)")
cc = cats_of(G_C)
check(len(cc) == 1 and cc[0][0] == "Hand Built" and cc[0][1] == 7,
      "guild C: hand-built category untouched (no re-migration)")
cd = cats_of(G_D)
check(len(cd) == 1 and cd[0][0] == "Bronze", "guild D: minimal guild migrated")
check(cats_of(G_E) == [], "guild E: untouched (its tiers arrive with the fault test)")

# ── v1 data untouched by the migration ──────────────────────────────
check(db_query("SELECT COUNT(*) FROM minigames_tiers")[0][0] == V1_TIER_COUNT,
      "v1 tiers table row count unchanged (read-only)")
check(db_query("SELECT COUNT(*) FROM minigames_log")[0][0] == V1_LOG_COUNT,
      "v1 log row count unchanged")
cfg = db_query("SELECT enabled, channel_id, min_events_per_week, max_events_per_week, "
               "events_this_week, claim_seconds FROM minigames_config WHERE guild_id=?", (G_A,))[0]
check(list(cfg) == [1, 5000, 3, 9, 2, 300], "v1 config row byte-identical")

# ═══════════════════════════════════════════════════════════════════
section("IDEMPOTENCY — 3 more runs (4 total, plan §3.5 gate)")
for i in range(3):
    run_async(store.ensure_tables())
counts = {g: db_query("SELECT COUNT(*) FROM minigame_categories WHERE guild_id=?", (g,))[0][0]
          for g in (G_A, G_B, G_C, G_D)}
check(counts == {G_A: 4, G_B: 1, G_C: 1, G_D: 1},
      "re-runs create nothing new", str(counts))
dup = db_query("SELECT name, COUNT(*) c FROM minigame_categories WHERE guild_id=? "
               "GROUP BY name HAVING c > 1", (G_A,))
check(dup == [], "no duplicate category names after 4 runs")
check(db_query("SELECT COUNT(*) FROM minigames_tiers")[0][0] == V1_TIER_COUNT,
      "tiers table still untouched after re-runs")
run_async(store.run_migration())          # direct call, same result
check(db_query("SELECT COUNT(*) FROM minigame_categories WHERE guild_id=?", (G_A,))[0][0] == 4,
      "direct run_migration() call is a no-op when categories exist")

# ═══════════════════════════════════════════════════════════════════
section("FAILURE / ROLLBACK — injected fault mid-migration (guild E)")
# Guild E's FIRST migration attempt happens now.
for tier, w, rt, rv in [("bronze", 10, "coins", "10"),
                        ("silver", 10, "xp", "20"),
                        ("gold", 10, "diamonds", "1")]:
    db_exec("INSERT INTO minigames_tiers "
            "(guild_id, tier, weight, reward_type, reward_value) VALUES (?,?,?,?,?)",
            (G_E, tier, w, rt, rv))

real_connect = aiosqlite.connect
ERROR = sqlite3.OperationalError("injected fault (test)")


class EvilWrap:
    """Wraps an aiosqlite Connection; the 2nd INSERT of guild E dies.
    aiosqlite.connect is a SYNC factory returning the Connection
    (an async context manager) — so the wrapper is sync too."""

    def __init__(self, conn):
        self._conn = conn
        self._n = 0

    async def __aenter__(self):
        await self._conn.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._conn.__aexit__(*exc)

    async def execute(self, sql, args=()):
        s = " ".join(str(sql).split()).upper()
        if s.startswith("INSERT INTO MINIGAME_CATEGORIES") and args and args[0] == G_E:
            self._n += 1
            if self._n == 2:
                raise ERROR
        return await self._conn.execute(sql, args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def evil_connect(*args, **kwargs):
    return EvilWrap(real_connect(*args, **kwargs))


aiosqlite.connect = evil_connect
try:
    crashed = False
    try:
        run_async(store.ensure_tables())
    except Exception as exc:  # noqa: BLE001
        crashed = True
        print("   (ensure_tables raised: %r)" % exc)
finally:
    aiosqlite.connect = real_connect

check(not crashed, "ensure_tables never propagates a migration fault (bot startup survives)")
check(cats_of(G_E) == [], "guild E left CLEAN — no partial categories (rollback worked)",
      str(cats_of(G_E)))
check(db_query("SELECT COUNT(*) FROM minigame_categories WHERE guild_id=?", (G_A,))[0][0] == 4,
      "guild A unaffected by guild E's failure")

# recovery: next startup retries and completes
run_async(store.ensure_tables())
ce = cats_of(G_E)
check(len(ce) == 3 and [c[0] for c in ce] == ["Bronze", "Silver", "Gold"],
      "guild E fully migrated on the retry run", str([c[0] for c in ce]))

# ═══════════════════════════════════════════════════════════════════
section("LEGACY KEPT — rollback path (plan §3.4)")
tables = {r[0] for r in db_query("SELECT name FROM sqlite_master WHERE type='table'")}
check({"minigames_tiers", "minigames_config", "minigames_log"} <= tables,
      "all legacy tables still present (old code redeploy finds its data)")
log_cols = {r[1] for r in db_query("PRAGMA table_info(minigames_log)")}
check({"event_date", "tier", "winner_id", "winner_display_name", "forced", "fired_at"} <= log_cols,
      "legacy log columns intact")
check({"template_id", "category_id", "game_type", "mode", "status",
       "started_at", "ended_at", "participants_json", "winners_json"} <= log_cols,
      "v2 log columns added (guarded ALTERs ran once)")

# ═══════════════════════════════════════════════════════════════════
section("HISTORY — legacy rows readable, v2 snapshots independent")
hist = run_async(store.get_history(G_A, limit=50))
check(len(hist) == 6 and all(h["legacy"] for h in hist),
      "all 6 legacy rows returned, flagged legacy=True")
check(hist[0]["fired_at"] == "2026-08-15T17:30:00Z"
      and hist[0]["winner_display_name"] == "Eve",
      "legacy rows ordered newest first, content intact")
check(any(h["forced"] for h in hist) and any(h["winner_id"] is None for h in hist),
      "forced flag + unclaimed rows preserved")
hist_b = run_async(store.get_history(G_B, limit=50))
check(len(hist_b) == 2, "guild B legacy history readable")

# v2 snapshot independence (store level, plan §14)
CAT_A = db_query("SELECT id FROM minigame_categories WHERE guild_id=? AND name='Bronze'",
                 (G_A,))[0][0]
tpl = run_async(store.create_template(
    G_A, CAT_A, "Snapshot Check", "wheel", config={"join_seconds": 30},
    rewards=[{"reward_type": "coins", "reward_value": "5", "weight": 1}]))
run_id = run_async(store.start_run(G_A, tpl["id"], "Snapshot Check", CAT_A,
                                   "Bronze", "wheel", "auto", 5000))
run_async(store.update_category(G_A, CAT_A, {"name": "Bronze Renamed"}))
run_async(store.delete_template(G_A, tpl["id"]))
run_row = db_query("SELECT template_name, category_name, status FROM minigames_log WHERE id=?",
                   (run_id,))[0]
check(run_row == ("Snapshot Check", "Bronze", "running"),
      "run row keeps NAME SNAPSHOTS after category rename + template delete", str(run_row))
run_async(store.finish_run(run_id, "completed"))
hist_after = run_async(store.get_history(G_A, limit=50))
v2_row = next(h for h in hist_after if not h["legacy"])
check(v2_row["status"] == "completed" and v2_row["template_name"] == "Snapshot Check",
      "v2 history row readable alongside legacy rows after lifecycle")

# ═══════════════════════════════════════════════════════════════════
section("READ-TIME NORMALIZATION — legacy-shaped preset rows")
db_exec("INSERT INTO minigame_categories "
        "(guild_id, parent_id, name, weight, sort_order, default_rewards_json, enabled) "
        "VALUES (?, NULL, 'Legacy Shape', 1, 99, ?, 1)",
        (G_D, json.dumps([{"type": "coins", "value": "42", "weight": 2}]))
)
tree = run_async(store.get_categories_tree(G_D))
node = next(n for n in tree if n["name"] == "Legacy Shape")
check(node["default_rewards"] == [{"reward_type": "coins", "reward_value": "42", "weight": 2}],
      "legacy type/value preset keys normalized to canonical at read time",
      str(node["default_rewards"]))
db_exec("DELETE FROM minigame_categories WHERE guild_id=? AND name='Legacy Shape'", (G_D,))

# ═══════════════════════════════════════════════════════════════════
print(f"\nminigames migration: {PASS} passed, {FAIL} failed")
if FAIL:
    print("Failures:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL MIGRATION TESTS PASSED")
