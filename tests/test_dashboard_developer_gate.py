"""Dashboard-level regression tests for the developer guild gate (real Flask app, real SQLite).
Run: python tests/test_dashboard_developer_gate.py
"""
import asyncio
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="nilive-gate-")
os.environ["OWNER_ID"] = "704453350384730237"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "gate.db")
os.environ["DASHBOARD_DEMO_LOGIN"] = "0"
os.environ.pop("SUPER_ADMIN_USER_IDS", None)

import aiosqlite
from database import (DB_PATH, init_db, sync_bot_guilds, restore_bot_sync_state, invalidate_bot_sync_state,
                      record_bot_guild_join, record_bot_guild_remove)
from utils.guild_lifecycle import GuildLifecycleCoordinator
from dashboard.app import app
from dashboard.utils.async_utils import run_async

DEV = 704453350384730237
ORDINARY = 1111222233334444
INSTALLED = 900100100100100101
OTHER_INSTALLED = 900100100100100102
UNINSTALLED = 900100100100100999
CSRF = "test-csrf-token"

# Routes that are NOT demonstrably denied for an unprivileged / uninstalled-guild session, reviewed
# one by one: public pages, OAuth/login flow, guild picker/switcher, health probes and static assets.
# A NEW route outside this set that is not denied fails the audit and must be reviewed.
REVIEWED_NOT_GUILD_GATED = {"GET /demo-login"}  # login-flow route, disabled unless DASHBOARD_DEMO_LOGIN=1; no guild data


def _seed():
    run_async(init_db())

    async def reset():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM dashboard_users")
            await db.execute("DELETE FROM bot_guilds")
            await db.execute("DELETE FROM bot_sync_state")
            await db.commit()
    run_async(reset())
    run_async(sync_bot_guilds([
        {"guild_id": INSTALLED, "name": "Installed One", "icon": None, "owner_id": 5, "member_count": 9},
        {"guild_id": OTHER_INSTALLED, "name": "Installed Two", "icon": None, "owner_id": 6, "member_count": 9},
    ], authoritative=True))
    run_async(restore_bot_sync_state())


def _login(client, user_id, guild_id=None, level=None):
    with client.session_transaction() as s:
        s.clear()
        s["user"] = {"id": str(user_id), "username": "u", "avatar": None}
        s["expires_at"] = time.time() + 3600
        s["csrf_token"] = CSRF
        if guild_id is not None:
            s["guild_id"] = guild_id
        if level is not None:
            s["user_level"] = level            # stale cached level must never authorize


def _make_coordinator():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.is_closed.return_value = False
    bot.ws.open = True
    coord = GuildLifecycleCoordinator(bot)
    coord.gateway_transport_verified = True
    coord.guild_membership_reconciled = True
    return coord


def _concrete_url(rule) -> str:
    return re.sub(r"<(?:[^:>]+:)?[^>]+>", "1", rule.rule)


def _probe_all_routes(client):
    """{(method, rule): status} for every registered route, with a valid CSRF token on mutations."""
    out = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            kwargs = {"headers": {"X-CSRF-Token": CSRF}}
            if method != "GET":
                kwargs["json"] = {}
                kwargs["data"] = None
            resp = client.open(_concrete_url(rule), method=method, follow_redirects=False, **kwargs)
            out[(method, rule.rule)] = resp.status_code
    return out


class DeveloperGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def setUp(self):
        _seed()
        self.client = app.test_client()

    # -- the /server-select deadlock (sync run_async called from a coroutine on the loop thread) --
    def test_01_developer_server_select_does_not_deadlock_the_shared_loop(self):
        _login(self.client, DEV)
        result = {}
        t = threading.Thread(target=lambda: result.update(resp=self.client.get("/server-select")), daemon=True)
        t.start()
        t.join(15)
        self.assertFalse(t.is_alive(), "GET /server-select hung: shared dashboard event loop is deadlocked")
        self.assertEqual(result["resp"].status_code, 200)
        body = result["resp"].get_data(as_text=True)
        self.assertIn("Installed One", body)
        self.assertIn("Installed Two", body)
        # the shared loop still serves everyone else
        probe = threading.Thread(target=lambda: result.update(ok=run_async(asyncio.sleep(0, "alive"))), daemon=True)
        probe.start()
        probe.join(5)
        self.assertEqual(result.get("ok"), "alive")

    def test_02_run_async_fails_loudly_instead_of_deadlocking_on_the_loop_thread(self):
        async def reenter():
            async def inner():
                return 1
            try:
                run_async(inner())
            except RuntimeError as exc:
                return str(exc)
            return "no error"
        self.assertIn("event-loop thread", run_async(reenter()))

    # -- dynamic audit of every registered route --------------------------------------------------
    def test_03_dynamic_audit_every_route_denied_for_uninstalled_guild_and_unprivileged_user(self):
        dev_uninstalled = app.test_client()
        _login(dev_uninstalled, DEV, UNINSTALLED, level="owner")
        stranger = app.test_client()
        _login(stranger, ORDINARY, INSTALLED, level="owner")        # stale cached level, no DB row
        with patch("requests.sessions.Session.request", side_effect=AssertionError("outbound HTTP")):
            dev_results = _probe_all_routes(dev_uninstalled)
            stranger_results = _probe_all_routes(stranger)

        self.assertGreater(len(dev_results), 200, "audit must cover every registered route")
        denied = lambda code: code in (401, 403) or 300 <= code < 400
        not_denied_dev = {k for k, v in dev_results.items() if not denied(v)}
        not_denied_stranger = {k for k, v in stranger_results.items() if not denied(v)}
        # An unauthorized developer session on an uninstalled guild must be denied exactly where an
        # unprivileged user is: the gate may not be weaker for the developer anywhere.
        self.assertEqual(not_denied_dev, not_denied_stranger,
                         "developer on an uninstalled guild reaches routes an unprivileged user cannot")
        unexpected = {f"{m} {r}" for (m, r) in not_denied_dev} - REVIEWED_NOT_GUILD_GATED
        self.assertEqual(unexpected, set(), "route(s) not denied for an unauthorized session; review and gate them")

    # -- existing-session revocation --------------------------------------------------------------
    def test_04_existing_developer_session_is_revoked_on_removal_and_on_lost_sync(self):
        _login(self.client, DEV, INSTALLED)
        self.assertEqual(self.client.get("/config/general").status_code, 200)
        coord = _make_coordinator()

        run_async(coord.execute_mutation(lambda: record_bot_guild_remove(INSTALLED)))
        self.assertEqual(self.client.get("/config/general").status_code, 403)
        self.assertEqual(self.client.post("/api/commands/toggle", json={},
                                          headers={"X-CSRF-Token": CSRF}).status_code, 403)

        run_async(coord.execute_mutation(lambda: record_bot_guild_join(INSTALLED, "Installed One", None)))
        self.assertEqual(self.client.get("/config/general").status_code, 200)   # same session, installed again

        run_async(invalidate_bot_sync_state())                                  # gateway lost / bot restarting
        self.assertEqual(self.client.get("/config/general").status_code, 403)

    # -- ordinary users are unaffected by bot synchronization state --------------------------------
    def test_05_ordinary_user_permissions_invariant_under_sync_states(self):
        async def grant():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) "
                                 "VALUES (?, ?, 'admin', 1)", (INSTALLED, ORDINARY))
                await db.commit()
        run_async(grant())
        for label, prepare in (("synced", lambda: None),
                               ("sync lost", lambda: run_async(invalidate_bot_sync_state()))):
            with self.subTest(state=label):
                prepare()
                c = app.test_client()
                _login(c, ORDINARY, INSTALLED)
                self.assertEqual(c.get("/").status_code, 200)                   # admin row still grants moderator pages
                self.assertEqual(c.get("/config/general").status_code, 403)     # owner-only page, unchanged
                c2 = app.test_client()
                _login(c2, ORDINARY, OTHER_INSTALLED)                           # no row there
                self.assertEqual(c2.get("/").status_code, 403)

    # -- zero outbound Discord REST from the dashboard ---------------------------------------------
    def test_06_developer_flows_make_zero_outbound_http_requests(self):
        _login(self.client, DEV)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("outbound HTTP")) as http, \
             patch("dashboard.app.fetch_discord_guilds", return_value=[]):
            self.assertEqual(self.client.get("/server-select").status_code, 200)
            self.assertEqual(self.client.get("/api/user/servers").status_code, 200)
            self.assertEqual(self.client.post(f"/select-guild/{INSTALLED}", data={"csrf_token": CSRF}).status_code, 302)
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.post(f"/select-guild/{UNINSTALLED}", data={"csrf_token": CSRF}).status_code, 403)
            http.assert_not_called()

    def test_07_select_guild_csrf_and_arbitrary_ids(self):
        _login(self.client, DEV)
        self.assertEqual(self.client.post(f"/select-guild/{INSTALLED}").status_code, 403)               # no CSRF
        self.assertEqual(self.client.post(f"/select-guild/{2**63}", data={"csrf_token": CSRF}).status_code, 400)
        self.assertEqual(self.client.post("/select-guild/0", data={"csrf_token": CSRF}).status_code, 400)
        self.assertEqual(self.client.post("/select-guild/-5", data={"csrf_token": CSRF}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
