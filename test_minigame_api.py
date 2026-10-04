#!/usr/bin/env python3
"""
Minigames v2 — Phase 4 — Dashboard API behavioral tests (Flask test client).

Covers the Phase 4 test list against the REAL app + REAL store (no
mocks):

  config        GET/POST, validation, global preset persistence
  categories    create / nested / rename / move / cycle-check /
                disable-enable / delete (409 conflict) / IDOR
  templates     create / edit / duplicate / delete / empty pool /
                prefill chain / validation matrix / atomic reward
                replace / idempotent double-save
  runs          template delete + category edit while a run exists
                (snapshot untouched, plan §14)
  spawn         test/manual modes, D12 (disabled manual refused),
                duplicate-queue guard, broken-template guard
  preview       rows == utils/minigame_engine output (parity)
  history       v2 + legacy rows, ordering, category filter
  security      guild isolation, moderator 403, CSRF 403, anon 401,
                retired v1 routes 404
  audit         log_action rows written for mutations

Run:  /tmp/mgv/bin/python scripts/test_minigame_api.py
(needs the flask venv — see header comment; system python lacks flask)
Env:  uses a fresh temp DB per run; OWNER_ID left unset so the
      explicit dashboard_users rows are the only permission source.
"""
import os
import sys
import json
import time
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="mgv4_")
DB_PATH = os.path.join(_TMP, "test.db")
os.environ["DATABASE_PATH"] = DB_PATH
# database.py refuses to boot without OWNER_ID; set it to an ID no
# test session ever uses (the developer bypass then never triggers —
# every permission decision below goes through dashboard_users).
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

GUILD_A, GUILD_B = 1001, 2002
USER_A, USER_B, USER_MOD = 100, 200, 300
CSRF = "testcsrf"

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


import dashboard.app as dapp  # noqa: E402  (after env setup)
from database import DB_PATH as CONFIRMED_DB  # noqa: E402
from dashboard.utils.async_utils import run_async  # noqa: E402

assert CONFIRMED_DB == DB_PATH, f"DB path mismatch: {CONFIRMED_DB} != {DB_PATH}"

app = dapp.app
app.config["TESTING"] = True


def make_client(user_id, guild_id, username="tester"):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": user_id, "username": username, "avatar": None}
        s["guild_id"] = guild_id
        s["expires_at"] = time.time() + 7200
        s["csrf_token"] = CSRF
    return c


def seed_user(user_id, guild_id, level):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled)"
        " VALUES (?,?,?,1)", (guild_id, user_id, level))
    conn.commit()
    conn.close()


seed_user(USER_A, GUILD_A, "admin")
seed_user(USER_B, GUILD_B, "admin")
seed_user(USER_MOD, GUILD_A, "moderator")

A = make_client(USER_A, GUILD_A, "adminA")
B = make_client(USER_B, GUILD_B, "adminB")
MOD = make_client(USER_MOD, GUILD_A, "modA")


def api(client, method, path, payload=None, csrf=True):
    headers = {}
    if method not in ("GET", "HEAD", "OPTIONS"):
        if csrf:
            headers["X-CSRF-Token"] = CSRF
        if payload is not None:
            headers["Content-Type"] = "application/json"
    return client.open(path, method=method, json=payload if payload is not None else None,
                       headers=headers)


def get(client, path):
    return api(client, "GET", path)


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


def flat_tree(client):
    """All nodes of the (nested) category tree as {id: node}."""
    out = {}

    def walk(nodes):
        for n in nodes:
            out[n["id"]] = n
            walk(n.get("children") or [])

    walk(get(client, "/api/minigames/categories").get_json()["tree"])
    return out


# ═══════════════════════════════════════════════════════════════════
section("CONFIG")
r = get(A, "/api/minigames/config").get_json()
check(r["config"]["enabled"] == 1 and r["config"]["min_events_per_week"] == 5
      and r["config"]["max_events_per_week"] == 10
      and r["config"]["global_default_rewards"] == [],
      "fresh config defaults")

r = api(A, "POST", "/api/minigames/config",
        {"min_events_per_week": 8, "max_events_per_week": 3}).get_json()
check(r["success"] is False and "max_events" in r["error"], "config max<min rejected")

PRESET_GLOBAL = [{"reward_type": "coins", "reward_value": "50", "weight": 1}]
r = api(A, "POST", "/api/minigames/config", {
    "enabled": True, "channel_id": 777, "min_events_per_week": 3,
    "max_events_per_week": 7, "global_default_rewards": PRESET_GLOBAL,
}).get_json()
check(r["success"] is True, "config save ok")
r = get(A, "/api/minigames/config").get_json()
c = r["config"]
check(c["channel_id"] == 777 and c["min_events_per_week"] == 3
      and c["max_events_per_week"] == 7
      and c["global_default_rewards"] == PRESET_GLOBAL,
      "config persisted (channel/min/max/global preset)")

r = api(A, "POST", "/api/minigames/config",
        {"global_default_rewards": [{"reward_type": "hologram", "reward_value": "x", "weight": 1}]}
        ).get_json()
check(r["success"] is False and "reward_type" in r["error"], "bad global reward type rejected")

# ═══════════════════════════════════════════════════════════════════
section("CATEGORIES")
PRESET_A1 = [{"reward_type": "xp", "reward_value": "100", "weight": 2}]
r = api(A, "POST", "/api/minigames/categories", {
    "name": "Alpha", "weight": 2, "emoji": "🎯", "color": "#ff0000",
    "default_rewards": PRESET_A1,
}).get_json()
check(r["success"] is True, "create root category")
ALPHA = r["category"]["id"]

r = api(A, "POST", "/api/minigames/categories", {
    "name": "Alpha/Sub", "parent_id": ALPHA, "weight": 1,
}).get_json()
check(r["success"] is True, "create nested category")
SUB = r["category"]["id"]

r = get(A, "/api/minigames/categories").get_json()
tree = {n["id"]: n for n in r["tree"]}
check(ALPHA in tree and tree[ALPHA]["children"] and tree[ALPHA]["children"][0]["id"] == SUB,
      "tree nests sub under parent")
check(tree[ALPHA]["default_rewards"] == PRESET_A1, "category preset persisted in tree node")

r = api(A, "POST", "/api/minigames/categories", {"name": "   "}).get_json()
check(r["success"] is False, "empty name rejected")
r = api(A, "POST", "/api/minigames/categories", {"name": "X", "parent_id": 999999}).get_json()
check(r["success"] is False and "parent" in r["error"].lower(), "missing parent rejected")

r = api(A, "PATCH", f"/api/minigames/categories/{ALPHA}",
        {"name": "Alpha Renamed", "weight": 3}).get_json()
check(r["success"] is True and r["category"]["name"] == "Alpha Renamed"
      and r["category"]["weight"] == 3, "rename + weight update")

r = api(A, "PATCH", f"/api/minigames/categories/{SUB}", {"parent_id": None}).get_json()
check(r["success"] is True and r["category"]["parent_id"] is None, "move to root")

# cycle check: C1 > C2 > C3, then try C1 under C3
r1 = api(A, "POST", "/api/minigames/categories", {"name": "C1"}).get_json()
r2 = api(A, "POST", "/api/minigames/categories",
         {"name": "C2", "parent_id": r1["category"]["id"]}).get_json()
r3 = api(A, "POST", "/api/minigames/categories",
         {"name": "C3", "parent_id": r2["category"]["id"]}).get_json()
C1, C2, C3 = (x["category"]["id"] for x in (r1, r2, r3))
r = api(A, "PATCH", f"/api/minigames/categories/{C1}", {"parent_id": C3}).get_json()
check(r["success"] is False and "descendant" in r["error"], "cycle move rejected (C1 under its own C3)")
r = api(A, "PATCH", f"/api/minigames/categories/{C1}", {"parent_id": C1}).get_json()
check(r["success"] is False and "own parent" in r["error"], "self-parent rejected")
# legal move: C1 under SUB (root)
r = api(A, "PATCH", f"/api/minigames/categories/{C1}", {"parent_id": SUB}).get_json()
check(r["success"] is True, "legal move ok")
api(A, "PATCH", f"/api/minigames/categories/{C1}", {"parent_id": None})  # back to root

# disable / enable
r = api(A, "PATCH", f"/api/minigames/categories/{C3}", {"enabled": False}).get_json()
check(r["success"] is True and r["category"]["enabled"] == 0, "category disable")
check(flat_tree(A)[C3]["enabled"] == 0, "tree reflects disabled category")
api(A, "PATCH", f"/api/minigames/categories/{C3}", {"enabled": True})

# delete conflict (409)
tmpl_c3 = api(A, "POST", "/api/minigames/templates", {
    "name": "In C3", "category_id": C3, "game_type": "wheel",
    "config": {"join_seconds": 30}, "rewards": [],
}).get_json()
r = api(A, "DELETE", f"/api/minigames/categories/{C3}").get_json()
resp_code = api(A, "DELETE", f"/api/minigames/categories/{C3}")
check(resp_code.status_code == 409 and r["success"] is False and "template" in r["error"],
      "delete category with content → 409", f"got {resp_code.status_code} {r}")
api(A, "DELETE", f"/api/minigames/templates/{tmpl_c3['template']['id']}")
r = api(A, "DELETE", f"/api/minigames/categories/{C3}").get_json()
check(r["success"] is True, "empty category deletes")
check(C3 not in flat_tree(A), "deleted category gone from tree")

# IDOR: guild B cannot see or touch guild A's categories
check(get(B, "/api/minigames/categories").get_json()["tree"] == [],
      "guild B sees an empty tree (isolation)")
r = api(B, "DELETE", f"/api/minigames/categories/{ALPHA}").get_json()
check(r["success"] is False and "not found" in r["error"],
      "guild B cannot delete guild A's category")

# ═══════════════════════════════════════════════════════════════════
section("TEMPLATES")
# C1 gets its own preset → prefill chain level 2
api(A, "PATCH", f"/api/minigames/categories/{C1}",
    {"default_rewards": [{"reward_type": "coins", "reward_value": "10", "weight": 1}]})

MATH_EMBED = {"title": "Math Rush", "description": "What is 7 × 8?",
              "color": 5789650, "fields": [{"name": "n", "value": "v"}]}
r = api(A, "POST", "/api/minigames/templates", {
    "name": "Math A", "category_id": C1, "game_type": "math",
    "config": {"answers": ["54", "56"], "correct": 1, "seconds": 45},
    "embed": MATH_EMBED, "rewards": [],
    "enabled": True, "auto_spawn": False, "channel_id": 888,
}).get_json()
check(r["success"] is True, "create math template (empty pool valid)")
T1 = r["template"]["id"]
check(r["template"]["rewards"] == [] and r["template"]["auto_spawn"] == 0
      and r["template"]["channel_id"] == 888, "empty pool + fields stored")

r = get(A, f"/api/minigames/templates/{T1}").get_json()
t = r["template"]
check(t["embed"] == MATH_EMBED and t["config"]["correct"] == 1
      and t["config"]["answers"] == ["54", "56"] and t["embed"]["description"] == "What is 7 × 8?",
      "template GET round-trip (embed/config parsed)")

# prefill chain: omitted rewards → category preset
r = api(A, "POST", "/api/minigames/templates", {
    "name": "Prefill Cat", "category_id": C1, "game_type": "wheel",
    "config": {"join_seconds": 20},
}).get_json()
T2 = r["template"]["id"]
check(r["template"]["rewards"] and r["template"]["rewards"][0]["reward_value"] == "10"
      and r["template"]["rewards"][0]["reward_type"] == "coins",
      "omitted rewards → category preset", json.dumps(r["template"]["rewards"]))
api(A, "DELETE", f"/api/minigames/templates/{T2}")

# prefill chain: category w/o preset → global preset
D1 = api(A, "POST", "/api/minigames/categories", {"name": "NoPreset"}).get_json()["category"]["id"]
r = api(A, "POST", "/api/minigames/templates", {
    "name": "Prefill Global", "category_id": D1, "game_type": "wheel",
    "config": {"join_seconds": 20},
}).get_json()
T3 = r["template"]["id"]
check(any(x["reward_value"] == "50" and x["reward_type"] == "coins" for x in r["template"]["rewards"]),
      "omitted rewards in no-preset category → global preset")
api(A, "DELETE", f"/api/minigames/templates/{T3}")

# validation matrix
BAD = {
    "one answer": {"name": "x", "category_id": C1, "game_type": "math",
                   "config": {"answers": ["a"], "correct": 0, "seconds": 30}},
    "empty answer": {"name": "x", "category_id": C1, "game_type": "math",
                     "config": {"answers": ["a", "  "], "correct": 0, "seconds": 30}},
    "correct out of range": {"name": "x", "category_id": C1, "game_type": "math",
                             "config": {"answers": ["a", "b"], "correct": 5, "seconds": 30}},
    "no correct": {"name": "x", "category_id": C1, "game_type": "math",
                   "config": {"answers": ["a", "b"], "correct": -1, "seconds": 30}},
    "7 answers": {"name": "x", "category_id": C1, "game_type": "math",
                  "config": {"answers": ["a", "b", "c", "d", "e", "f", "g"], "correct": 0}},
    "colors no image": {"name": "x", "category_id": C1, "game_type": "colors",
                        "config": {"answers": ["red", "blue"], "correct": 0, "seconds": 30},
                        "embed": {"description": "which color?"}},
    "colors bad image": {"name": "x", "category_id": C1, "game_type": "colors",
                         "config": {"answers": ["red", "blue"], "correct": 0, "seconds": 30},
                         "embed": {"description": "q", "image": {"url": "ftp://nope"}}},
    "buttons 9": {"name": "x", "category_id": C1, "game_type": "quick_click",
                  "config": {"buttons": 9}},
    "reveal max<min": {"name": "x", "category_id": C1, "game_type": "quick_click",
                       "config": {"buttons": 3, "reveal_min": 9, "reveal_max": 4}},
    "bad game_type": {"name": "x", "category_id": C1, "game_type": "chess"},
    "no name": {"name": "  ", "category_id": C1, "game_type": "wheel",
                "config": {"join_seconds": 20}},
    "no category": {"name": "x", "game_type": "wheel", "config": {"join_seconds": 20}},
    "bad reward type": {"name": "x", "category_id": C1, "game_type": "wheel",
                        "config": {"join_seconds": 20},
                        "rewards": [{"reward_type": "hologram", "reward_value": "1", "weight": 1}]},
    "reward no value": {"name": "x", "category_id": C1, "game_type": "wheel",
                        "config": {"join_seconds": 20},
                        "rewards": [{"reward_type": "coins", "reward_value": "", "weight": 1}]},
    "weight 0": {"name": "x", "category_id": C1, "game_type": "wheel",
                 "config": {"join_seconds": 20},
                 "rewards": [{"reward_type": "coins", "reward_value": "5", "weight": 0}]},
    "desc too long": {"name": "x", "category_id": C1, "game_type": "wheel",
                      "config": {"join_seconds": 20},
                      "embed": {"description": "x" * 4097}},
    "26 fields": {"name": "x", "category_id": C1, "game_type": "wheel",
                  "config": {"join_seconds": 20},
                  "embed": {"fields": [{"name": f"f{i}", "value": "v"} for i in range(26)]}},
    "seconds 1": {"name": "x", "category_id": C1, "game_type": "math",
                  "config": {"answers": ["a", "b"], "correct": 0, "seconds": 1}},
}
for label, payload in BAD.items():
    r = api(A, "POST", "/api/minigames/templates", payload).get_json()
    check(r["success"] is False, f"create rejected: {label}", r.get("error", ""))

# API-shape embed (author/footer as dicts — what the composer sends)
r = api(A, "POST", "/api/minigames/templates", {
    "name": "Dict embed", "category_id": C1, "game_type": "wheel",
    "config": {"join_seconds": 20},
    "embed": {"title": "t", "author": {"name": "Nero"},
              "footer": {"text": "fine"}, "image": {"url": "https://i"}},
    "rewards": [],
}).get_json()
check(r["success"] is True, "API-shape embed (dict author/footer) accepted",
      r.get("error", ""))
r = api(A, "POST", "/api/minigames/templates", {
    "name": "Dict embed long", "category_id": C1, "game_type": "wheel",
    "config": {"join_seconds": 20},
    "embed": {"author": {"name": "x" * 257}}, "rewards": [],
}).get_json()
check(r["success"] is False and "author" in r["error"], "oversized dict author rejected")

# colors WITH a valid image passes
r = api(A, "POST", "/api/minigames/templates", {
    "name": "Colors ok", "category_id": C1, "game_type": "colors",
    "config": {"answers": ["red", "blue"], "correct": 0, "seconds": 30},
    "embed": {"description": "middle?", "image": {"url": "https://img.example/1.png"}},
    "rewards": [],
}).get_json()
check(r["success"] is True, "colors with valid image accepted")
TC = r["template"]["id"]
api(A, "DELETE", f"/api/minigames/templates/{TC}")

# PATCH: full update incl. atomic reward replace
NEW_REWARDS = [
    {"reward_type": "xp", "reward_value": "300", "weight": 3},
    {"reward_type": "temp_role", "reward_value": "123456789012345678", "weight": 1, "duration_hours": 24},
]
r = api(A, "PATCH", f"/api/minigames/templates/{T1}", {
    "name": "Math A v2", "game_type": "math",
    "config": {"answers": ["56", "58", "63"], "correct": 0, "seconds": 60},
    "embed": {"title": "Math Rush v2", "description": "What is 7 × 8?"},
    "rewards": NEW_REWARDS, "enabled": False, "auto_spawn": True,
    "channel_id": None,
}).get_json()
check(r["success"] is True, "PATCH full update ok")
t = get(A, f"/api/minigames/templates/{T1}").get_json()["template"]
check(t["name"] == "Math A v2" and t["enabled"] == 0 and t["auto_spawn"] == 1
      and t["channel_id"] is None and t["config"]["answers"] == ["56", "58", "63"]
      and t["embed"]["title"] == "Math Rush v2", "PATCH persisted all fields")
check(len(t["rewards"]) == 2 and {x["reward_type"] for x in t["rewards"]} == {"xp", "temp_role"}
      and any(x["duration_hours"] == 24 for x in t["rewards"]),
      "atomic reward replace (old pool gone, new pool exact)")

# idempotent double-save (double submission must not duplicate/lose)
r1 = api(A, "PATCH", f"/api/minigames/templates/{T1}",
         {"name": "Math A v3", "config": {"answers": ["56", "58", "63"], "correct": 0, "seconds": 60}})
r2 = api(A, "PATCH", f"/api/minigames/templates/{T1}",
         {"name": "Math A v3", "config": {"answers": ["56", "58", "63"], "correct": 0, "seconds": 60}})
check(r1.get_json()["success"] and r2.get_json()["success"], "double PATCH both succeed")
n = db_query("SELECT COUNT(*) FROM minigame_templates WHERE id=?", (T1,))[0][0]
check(n == 1, "double PATCH leaves exactly one row")
check(db_query("SELECT name FROM minigame_templates WHERE id=?", (T1,))[0][0] == "Math A v3",
      "double PATCH keeps the latest value")

# invalid PATCH (effective-type combination)
r = api(A, "PATCH", f"/api/minigames/templates/{T1}",
        {"config": {"answers": ["a"], "correct": 0}}).get_json()
check(r["success"] is False and "2 to 6" in r["error"], "PATCH caught by effective-type validation")

# duplicate
r = api(A, "POST", f"/api/minigames/templates/{T1}/duplicate", {}).get_json()
check(r["success"] is True, "duplicate (auto name)")
TD = r["template"]["id"]
check("Math A v3" in r["template"]["name"] and (r["template"]["name"].endswith("(copy)") or "#" in r["template"]["name"]),
      f"duplicate auto-name → {r['template']['name']}")
td = get(A, f"/api/minigames/templates/{TD}").get_json()["template"]
check(td["config"] == t["config"] and td["rewards"] and
      {x["reward_type"] for x in td["rewards"]} == {"xp", "temp_role"}
      and td["embed"]["title"] == "Math Rush v2" and td["category_id"] == C1,
      "duplicate is a deep copy (config/rewards/embed/category)")
api(A, "DELETE", f"/api/minigames/templates/{TD}")
r = api(A, "POST", f"/api/minigames/templates/{T1}/duplicate", {"new_name": "Explicit Copy"}).get_json()
check(r["success"] is True and r["template"]["name"] == "Explicit Copy", "duplicate with explicit name")
api(A, "DELETE", f"/api/minigames/templates/{r['template']['id']}")
r = api(A, "POST", "/api/minigames/templates/999999/duplicate", {}).get_json()
check(r["success"] is False and "not found" in r["error"], "duplicate missing template → error")

# template list: summary + live/queued flags
r = get(A, "/api/minigames/templates?include_disabled=1").get_json()
row = next(x for x in r["templates"] if x["id"] == T1)
check(row["category_name"] and row["has_reward_pool"] is True
      and row["live_run"] is False and row["queued"] is False,
      "template list row shape (category_name/has_reward_pool/flags)")

# ═══════════════════════════════════════════════════════════════════
section("RUNS — snapshot protection (plan §14)")
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
from utils import minigame_store as store  # noqa: E402

# Open the run through the REAL bot path (same call the spawn engine
# makes) — it fills the legacy NOT NULL columns and snapshots names.
RUN_ID = run_async(store.start_run(
    GUILD_A, T1, "Math A v3", C1, "C1", "math", "auto", 888))
check(RUN_ID is not None, "seeded a live 'running' row via store.start_run")

r = get(A, "/api/minigames/templates").get_json()
row = next(x for x in r["templates"] if x["id"] == T1)
check(row["live_run"] is True, "live_run flag set while a run is in progress")

# delete the template while the run is live → allowed, run untouched
r = api(A, "DELETE", f"/api/minigames/templates/{T1}").get_json()
check(r["success"] is True, "template delete while run in progress (snapshot)")
run_row = db_query("SELECT status, template_id FROM minigames_log WHERE id=?", (RUN_ID,))[0]
check(run_row[0] == "running" and run_row[1] == T1, "run row untouched by template delete")

# category edit while the run is live → run untouched
r = api(A, "PATCH", f"/api/minigames/categories/{C1}", {"name": "C1 renamed", "weight": 5}).get_json()
check(r["success"] is True, "category edit while run in progress")
run_row = db_query("SELECT status, template_id, category_id FROM minigames_log WHERE id=?", (RUN_ID,))[0]
check(run_row[0] == "running" and run_row[2] == C1, "run row untouched by category edit")

# ═══════════════════════════════════════════════════════════════════
section("SPAWN (test / manual — queue, D12, guards)")
# a fresh ENABLED template to spawn (T1 was deleted above)
T4 = api(A, "POST", "/api/minigames/templates", {
    "name": "Spawnable", "category_id": C1, "game_type": "wheel",
    "config": {"join_seconds": 30}, "rewards": [], "enabled": True,
}).get_json()["template"]["id"]

r = api(A, "POST", f"/api/minigames/templates/{T4}/spawn", {"mode": "test"}).get_json()
check(r["success"] is True and r["request_id"], "test spawn queued")
REQ = r["request_id"]
r = get(A, "/api/minigames/spawn-requests").get_json()
req = next((x for x in r["requests"] if x["id"] == REQ), None)
check(req and req["mode"] == "test" and req["template_name"] == "Spawnable"
      and req["status"] in ("pending", "processing"),
      "spawn-requests shows the queued row with template name")

r = api(A, "POST", f"/api/minigames/templates/{T4}/spawn", {"mode": "test"}).get_json()
check(r["success"] is False and "already queued" in r["error"],
      "duplicate pending spawn refused (abuse guard)")

# resolve the queue, then manual on a DISABLED template must fail (D12)
db_exec("UPDATE minigame_spawn_requests SET status='completed' WHERE id=?", (REQ,))
api(A, "PATCH", f"/api/minigames/templates/{T4}", {"enabled": False})
r = api(A, "POST", f"/api/minigames/templates/{T4}/spawn", {"mode": "manual"}).get_json()
check(r["success"] is False and "disabled" in r["error"], "manual spawn of disabled template refused")
r = api(A, "POST", f"/api/minigames/templates/{T4}/spawn", {"mode": "test"}).get_json()
check(r["success"] is True, "TEST spawn of disabled template allowed (build→test flow)")
REQ2 = r["request_id"]

# broken-template guard at spawn time. The API itself refuses to
# CREATE/EDIT a template with no answers (§17) — so seed one through
# the store directly (what legacy/broken data looks like) and prove
# the spawn path refuses it instead of queueing a doomed run.
T5BROKEN = run_async(store.create_template(
    GUILD_A, C1, "Broken MC", "math",
    config={"answers": [], "correct": 0}, rewards=[]))
r = api(A, "POST", f"/api/minigames/templates/{T5BROKEN['id']}/spawn", {"mode": "test"}).get_json()
check(r["success"] is False and "no answers" in r["error"],
      "broken legacy template (no answers) refused at spawn")
api(A, "DELETE", f"/api/minigames/templates/{T5BROKEN['id']}")

# bad mode / missing template
r = api(A, "POST", f"/api/minigames/templates/{T4}/spawn", {"mode": "auto"}).get_json()
check(r["success"] is False and "mode" in r["error"], "invalid mode rejected")
r = api(A, "POST", "/api/minigames/templates/999999/spawn", {"mode": "test"}).get_json()
check(r["success"] is False and "not found" in r["error"], "spawn on missing template → error")
db_exec("DELETE FROM minigame_spawn_requests WHERE id=?", (REQ2,))

# ═══════════════════════════════════════════════════════════════════
section("PREVIEW ROWS — engine parity (plan §12)")
from utils import minigame_engine as engine  # noqa: E402

cases = [
    ("quick_click", {"buttons": 5}),
    ("quick_click", {"buttons": 99}),          # clamped to 6
    ("wheel", {}),
    ("rps", {}),
    ("math", {"answers": ["42", "43"]}),
    ("colors", {"answers": ["red", "blue", "green"]}),
    ("emoji", {"answers": ["a", "b", "c", "d"]}),
]
for gtype, cfg in cases:
    r = api(A, "POST", "/api/minigames/preview-rows",
            {"game_type": gtype, "config": cfg}).get_json()
    expected = engine.initial_component_rows(gtype, cfg)
    check(r.get("rows") == expected,
          f"preview rows == engine rows ({gtype} {list(cfg)})",
          f"api={r.get('rows')} engine={expected}")

r = api(A, "POST", "/api/minigames/preview-rows",
        {"game_type": "chess", "config": {}}).get_json()
check(r["success"] is False, "preview rows unknown type → error")
qc = api(A, "POST", "/api/minigames/preview-rows",
         {"game_type": "quick_click", "config": {"buttons": 3}}).get_json()["rows"]
check(len(qc[0]) == 3 and all(b["disabled"] for b in qc[0])
      and [b["label"] for b in qc[0]] == ["1", "2", "3"],
      "quick click preview: 3 disabled numbered buttons")

# ═══════════════════════════════════════════════════════════════════
section("HISTORY")
# a legacy v1 row (deliberately OLDER than the run row)
db_exec("""
    INSERT INTO minigames_log (guild_id, event_date, tier, channel_id, message_id,
                               winner_id, winner_display_name, forced, fired_at)
    VALUES (?,?,?,?,?,?,?,0,?)
""", (GUILD_A, "2026-08-01", "gold", 111, 222, 555, "Legacy Winner",
         "2026-08-01T12:00:00Z"))
r = get(A, "/api/minigames/history?limit=50").get_json()
rows = r["history"]
check(len(rows) >= 2, "history has v2 + legacy rows")
by_status = {x["id"]: x for x in rows}
run_hist = by_status.get(RUN_ID)
check(run_hist and run_hist["legacy"] is False and run_hist["template_id"] == T1
      and run_hist["status"] == "running" and run_hist["template_name"] == "Math A v3"
      and run_hist["category_name"] == "C1",
      "v2 history row keeps its NAME SNAPSHOTS after template delete + category rename",
      json.dumps({k: run_hist.get(k) for k in ("template_name", "category_name")} if run_hist else "row missing"))
legacy = next((x for x in rows if x["legacy"]), None)
check(legacy and legacy["tier"] == "gold" and legacy["winner_display_name"] == "Legacy Winner"
      and legacy["participants"] == [] and legacy["winners"] == [],
      "legacy row rendered as-is (tier/winner/empty participants)")
# ordering: newest first by COALESCE(started_at, fired_at) — the run
# row (started_at = now) must precede the legacy row (fired_at = Aug 1)
check(rows[0]["id"] == RUN_ID, "history ordered newest first (run row before legacy row)")

# category filter
other = db_query("SELECT id FROM minigame_categories WHERE guild_id=? AND id!=?",
                 (GUILD_A, C1))[0][0]
r = get(A, f"/api/minigames/history?category_id={other}").get_json()
check(all(x["category_id"] == other for x in r["history"]),
      "history category filter")

# ═══════════════════════════════════════════════════════════════════
section("SECURITY")
check(MOD.open("/api/minigames/categories", method="POST",
               json={"name": "Nope"}, headers={"X-CSRF-Token": CSRF}).status_code == 403,
      "moderator cannot create categories (403)")
# The minigames routes are LEVEL_ADMIN for BOTH read and write (admin
# surface, same as v1) — a moderator gets 403 on GET too, and no row
# may have been created by the attempt above.
check(MOD.get("/api/minigames/categories").status_code == 403,
      "moderator GET is 403 (minigames is an admin surface)")
check(db_query("SELECT COUNT(*) FROM minigame_categories WHERE name='Nope'")[0][0] == 0,
      "moderator's failed create wrote nothing")

r = get(B, f"/api/minigames/templates/{T4}").get_json()
check(r["template"] is None, "guild B GET of guild A template → null (no leak)")
api(B, "PATCH", f"/api/minigames/templates/{T4}", {"name": "hijack"})
check(db_query("SELECT name FROM minigame_templates WHERE id=?", (T4,))[0][0] == "Spawnable",
      "guild B PATCH of guild A template is a no-op (IDOR)")

no_csrf = A.open("/api/minigames/categories", method="POST",
                 json={"name": "x"}, content_type="application/json")
check(no_csrf.status_code == 403, "missing CSRF header → 403")
anon = app.test_client()
check(anon.get("/api/minigames/categories").status_code == 401, "anonymous → 401")
no_guild = make_client(USER_A, GUILD_A)
with no_guild.session_transaction() as s:
    s.pop("guild_id")
check(no_guild.get("/api/minigames/categories").status_code == 400, "no guild selected → 400")

for retired in ["/api/minigames/tiers", "/api/minigames/tier", "/api/minigames/log"]:
    code = A.get(retired).status_code
    check(code == 404, f"retired v1 route {retired} → 404", f"got {code}")
# CSRF header present so the guard lets the request reach routing
# (without it the blueprint CSRF hook answers 403 before the 404).
code = A.post("/api/minigames/tier", json={"tier": "gold"},
              headers={"X-CSRF-Token": CSRF})
check(code.status_code == 404, "retired POST /api/minigames/tier → 404", f"got {code.status_code}")

# ═══════════════════════════════════════════════════════════════════
section("AUDIT")
audit = db_query("SELECT action, target_id, target_name FROM audit_log "
                 "WHERE guild_id=? AND page='minigames' ORDER BY id", (GUILD_A,))
joined = " | ".join(f"{a}→{t}" for a, i, t in audit)
check(any("Created minigame category" in a for a, _, _ in audit), "audit: category create", joined[:200])
check(any("Created minigame template" in a for a, _, _ in audit), "audit: template create")
check(any("Updated minigame template" in a for a, _, _ in audit), "audit: template update")
check(any("Deleted minigame template" in a for a, _, _ in audit), "audit: template delete")
check(any("Queued" in a for a, _, _ in audit), "audit: spawn queued")
check(all(g == GUILD_A for (g,) in db_query(
    "SELECT DISTINCT guild_id FROM audit_log WHERE page='minigames'")),
      "audit rows are guild-scoped")

# ═══════════════════════════════════════════════════════════════════
print(f"\nminigames API: {PASS} passed, {FAIL} failed")
if FAIL:
    print("Failures:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL MINIGAMES API TESTS PASSED")
