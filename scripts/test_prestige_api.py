#!/usr/bin/env python3
"""
Finalized Prestige — Dashboard API validation (Issue 3).

Proves the shop item create endpoint rejects a `prestige` item carrying a
diamond price server-side (not only in the UI), and that non-prestige items
still save as before.

Run:  /tmp/p_venv/bin/python scripts/test_prestige_api.py
(needs the venv with flask/aiosqlite/discord — see test_prestige.py header)

Env:  uses a fresh temp DB; OWNER_ID is set to a value no test session uses,
      so every permission decision goes through dashboard_users.
"""
import os
import sys
import json
import time
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="prestige_api_")
DB_PATH = os.path.join(_TMP, "test.db")
os.environ["DATABASE_PATH"] = DB_PATH
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import dashboard.app as dapp           # noqa: E402  (after env setup)
from database import DB_PATH as CONFIRMED  # noqa: E402

assert CONFIRMED == DB_PATH, f"DB path mismatch: {CONFIRMED} != {DB_PATH}"

app = dapp.app
app.config["TESTING"] = True

GUILD_A = 1001
USER_A = 100
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
A = make_client(USER_A, GUILD_A)


def api(client, method, path, payload=None):
    headers = {}
    if method != "GET":
        headers["X-CSRF-Token"] = CSRF
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.open(path, method=method,
                       json=payload if payload is not None else None,
                       headers=headers)


section("PRESTIGE SHOP ITEM VALIDATION (Issue 3)")

# prestige + diamond price → rejected server-side.
r = api(A, "POST", "/api/shop/item", {
    "name": "Prestige w/ diamonds", "price": 500, "price_diamonds": 10,
    "type": "prestige", "prestige_tier": 1,
}).get_json()
check(r.get("success") is False and "diamond" in str(r.get("error", "")).lower(),
      "prestige + diamond price rejected server-side", str(r))

# coin-only prestige item → accepted (and only it must be Coins).
r = api(A, "POST", "/api/shop/item", {
    "name": "Prestige I", "price": 500, "type": "prestige", "prestige_tier": 1,
}).get_json()
check(r.get("success") is True, "coin-only prestige item accepted", str(r))

# regression: a normal coin role item still saves.
r = api(A, "POST", "/api/shop/item", {
    "name": "Roll", "price": 100, "type": "role", "role_id": 123,
}).get_json()
check(r.get("success") is True, "non-prestige coin role item still accepted", str(r))

# regression: a diamond-priced role item still saves (diamonds only banned
# for prestige).
r = api(A, "POST", "/api/shop/item", {
    "name": "Gem", "price": 100, "price_diamonds": 5,
    "type": "role", "role_id": 456,
}).get_json()
check(r.get("success") is True, "non-prestige diamond role item still accepted", str(r))

print(f"\n{'='*50}")
print(f"RESULTS: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("Failures:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1 if FAIL else 0)
print("ALL PRESTIGE API TESTS PASSED")
