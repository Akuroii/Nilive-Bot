"""Regression: the Leveling page's role-reward refresh endpoint must exist.

Production (fd9c452) shipped a systems/leveling.html whose role-reward table
reloads from GET /api/leveling/rewards on page load and after every add/delete,
next to a dashboard/api/leveling.py that had no such route. Every add/delete
saved, then the reload got a 404 HTML page and the admin saw "Reward added /
deleted, but the table could not be refreshed". The template and the API were
each tested in a different tree, so nothing checked the deployed combination.

This test runs the real Flask app against a throwaway SQLite file and checks:

1. every /api/leveling/... URL the Leveling page script calls is a registered
   route (template <-> API drift, the actual root cause);
2. an Admin gets 200 JSON {"rewards": [{id, level, role_id, audience}]} for the
   selected server only, ordered by level, with role_id as the exact stored
   string (Discord IDs exceed 2^53; a JSON number would be rounded by the
   browser's JSON.parse);
3. the same access model as the sibling endpoints: no session / expired
   session -> 401, no server selected -> 400, no access, disabled access,
   Moderator, or a server the user cannot manage -> 403, never leaking rows;
4. the endpoint is read-only: the whole database is byte-for-byte identical
   (iterdump) before and after every GET, allowed or refused, and the route
   accepts no write methods;
5. the page's real sequence: POST add -> GET list shows the row -> DELETE ->
   GET list no longer shows it.

Run with:
    python scripts/test_leveling_runtime_contract.py
"""
import re
import sqlite3
import time
from contextlib import closing

from phase1_support import GUILD, ROOT, execute, rows
from database import DB_PATH, init_db
from werkzeug.exceptions import MethodNotAllowed, NotFound

TEMPLATE = ROOT / "dashboard" / "templates" / "systems" / "leveling.html"
OTHER_GUILD = 8102
ADMIN, MODERATOR, DISABLED_ADMIN, STRANGER = 8301, 8302, 8303, 8304
CSRF = "contract-csrf"
BIG_ROLE = 1234567890123456789          # real-size snowflake, > 2^53
OTHER_ROLE = 1234567890123456790
URL = "/api/leveling/rewards"
FAILED = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def snapshot():
    with closing(sqlite3.connect(DB_PATH)) as db:
        return list(db.iterdump())


def template_api_urls():
    """Literal /api/leveling/... URLs in fetch()/rewardApiCall() calls."""
    text = TEMPLATE.read_text(encoding="utf-8")
    urls = set()
    for match in re.finditer(r"""(?:fetch|rewardApiCall)\(\s*(['`])(/api/leveling/[^'`]*)\1""", text):
        urls.add(re.sub(r"\$\{[^}]*\}", "1", match.group(2)).split("?")[0])
    return sorted(urls)


def client_for(app, user_id=None, guild_id=GUILD, expires_in=7200, **extra):
    client = app.test_client()
    if user_id is None:
        return client
    with client.session_transaction() as session:
        session.update(user={"id": user_id, "username": "C", "avatar": None},
                       guild_name="G", expires_at=time.time() + expires_in,
                       csrf_token=CSRF, **extra)
        if guild_id is not None:
            session["guild_id"] = guild_id
    return client


def main():
    import asyncio
    asyncio.run(init_db())
    for guild in (GUILD, OTHER_GUILD):
        execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild,))
    for user, level, enabled in ((ADMIN, "admin", 1), (MODERATOR, "moderator", 1),
                                 (DISABLED_ADMIN, "admin", 0)):
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,?,?)", (GUILD, user, level, enabled))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) VALUES (?,?,?,?)",
            (GUILD, 10, BIG_ROLE, "boosters"))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) VALUES (?,?,?,?)",
            (GUILD, 3, 777, "everyone"))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) VALUES (?,?,?,?)",
            (OTHER_GUILD, 1, OTHER_ROLE, "everyone"))

    import dashboard.app as dash
    app = dash.app
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

    # 1. template <-> API: the root cause.
    adapter = app.url_map.bind("localhost")
    urls = template_api_urls()
    check("the Leveling page script calls the role-reward list endpoint",
          URL in urls, str(urls))
    missing = []
    for url in urls:
        try:
            adapter.match(url, method="GET")
        except MethodNotAllowed:
            pass                      # path exists for another method (POST/DELETE)
        except NotFound:
            missing.append(url)
    check("every /api/leveling URL the page script calls is a registered route",
          not missing, f"missing={missing}")
    methods = set()
    for rule in app.url_map.iter_rules():
        if rule.rule == URL:
            methods |= rule.methods
    check("the list route accepts reads only (no POST/PUT/PATCH/DELETE)",
          methods and not methods & {"POST", "PUT", "PATCH", "DELETE"}, str(sorted(methods)))

    # 2. Admin read: shape, guild scoping, order, exact snowflake strings.
    before = snapshot()
    admin = client_for(app, ADMIN)
    res = admin.get(URL)
    check("an Admin gets 200 JSON", res.status_code == 200 and res.is_json,
          f"{res.status_code} {res.content_type}")
    body = res.get_json(silent=True) or {}
    got = body.get("rewards")
    check("the body is {'rewards': [...]} with exactly id/level/role_id/audience",
          isinstance(got, list) and got
          and all(set(r) == {"id", "level", "role_id", "audience"} for r in got),
          str(body))
    got = got or []
    check("only the selected server's rewards, ordered by level",
          [(r.get("level"), r.get("audience")) for r in got] == [(3, "everyone"), (10, "boosters")],
          str(got))
    check("role_id values are the exact stored IDs, as JSON strings",
          [r.get("role_id") for r in got] == ["777", str(BIG_ROLE)], str(got))
    check("another server's reward never appears",
          str(OTHER_ROLE) not in res.get_data(as_text=True))
    check("an allowed GET changes nothing in the database", snapshot() == before)

    # 3. Same access model as the sibling endpoints; refusals leak nothing.
    cases = [
        ("no dashboard session", client_for(app), 401),
        ("an expired session", client_for(app, ADMIN, expires_in=-60), 401),
        ("a session with no server selected", client_for(app, ADMIN, guild_id=None), 400),
        ("a user with no access to the server", client_for(app, STRANGER), 403),
        ("a disabled Admin entry", client_for(app, DISABLED_ADMIN), 403),
        ("a Moderator (below Admin)", client_for(app, MODERATOR), 403),
        ("an Admin whose session points at a server they cannot manage",
         client_for(app, ADMIN, guild_id=OTHER_GUILD), 403),
        ("a non-numeric server id in the session",
         client_for(app, ADMIN, guild_id="not-a-server"), 403),
        ("a cached user_level=admin claim without a real Admin entry",
         client_for(app, STRANGER, user_level="admin"), 403),
    ]
    for label, client, expected in cases:
        before = snapshot()
        r = client.get(URL)
        text = r.get_data(as_text=True)
        check(f"{label} -> {expected}, no reward data",
              r.status_code == expected and r.is_json and "rewards" not in (r.get_json(silent=True) or {})
              and str(BIG_ROLE) not in text and str(OTHER_ROLE) not in text,
              f"{r.status_code} {text[:120]!r}")
        check(f"{label}: the refused GET changes nothing", snapshot() == before)

    # 5. The page's real add -> refresh -> delete -> refresh sequence.
    headers = {"X-CSRF-Token": CSRF}
    added = admin.post("/api/leveling/reward", headers=headers,
                       json={"level": 5, "role_id": "1234567890123456791", "audience": "everyone"})
    check("add succeeds (unchanged existing route)", added.status_code == 200,
          f"{added.status_code} {added.get_data(as_text=True)[:120]!r}")
    listed = admin.get(URL)
    after_add = [(r["level"], r["role_id"]) for r in (listed.get_json(silent=True) or {}).get("rewards", [])]
    check("the refresh after add is 200 and shows the new row with its exact ID",
          listed.status_code == 200 and (5, "1234567890123456791") in after_add,
          f"{listed.status_code} {after_add}")
    new_id = rows("SELECT id FROM leveling_rewards WHERE guild_id=? AND level=5", (GUILD,))[0][0]
    deleted = admin.delete(f"/api/leveling/reward/{new_id}", headers=headers)
    check("delete succeeds (unchanged existing route)", deleted.status_code == 200,
          f"{deleted.status_code}")
    listed = admin.get(URL)
    after_delete = [(r["level"], r["role_id"]) for r in (listed.get_json(silent=True) or {}).get("rewards", [])]
    check("the refresh after delete is 200 and the row is gone, others untouched",
          listed.status_code == 200 and after_delete == [(3, "777"), (10, str(BIG_ROLE))],
          f"{listed.status_code} {after_delete}")

    if FAILED:
        print(f"FAILED: {len(FAILED)} check(s): {FAILED}")
        raise SystemExit(1)
    print("ALL LEVELING RUNTIME CONTRACT CHECKS PASSED")


if __name__ == "__main__":
    main()
