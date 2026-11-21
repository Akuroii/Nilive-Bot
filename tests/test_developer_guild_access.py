"""Tests for Developer Dashboard Guild Access and Synchronization (PLAN-02).
Covers all 10 scenarios in the Test Matrix.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("OWNER_ID", "704453350384730237")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_PATH", "/tmp/test_dev_access.db")
os.environ.setdefault("DASHBOARD_DEMO_LOGIN", "0")

import aiosqlite
from database import (
    DB_PATH, init_db, sync_bot_guilds, record_bot_guild_join,
    record_bot_guild_remove, record_bot_guild_update, get_stored_bot_guilds,
    is_bot_guild_synced, is_stored_bot_guild,
)
from dashboard.app import app
from dashboard import auth
from dashboard.utils.async_utils import run_async

DEV_USER_ID = 704453350384730237
NORMAL_USER_ID = 1111222233334444
INSTALLED_GUILD_1 = 900100100100100101  # Dev is NOT a member
INSTALLED_GUILD_2 = 900100100100100102  # Dev IS a member (regular member, 0 admin)
INSTALLED_GUILD_3 = 900100100100100103  # Dev IS server owner
UNINSTALLED_GUILD = 900100100100100999  # Bot is NOT installed


class DeveloperGuildAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_async(init_db())

    def setUp(self):
        os.environ["OWNER_ID"] = str(DEV_USER_ID)
        import database
        database.OWNER_DISCORD_ID = DEV_USER_ID
        import dashboard.permissions
        dashboard.permissions.OWNER_DISCORD_ID = DEV_USER_ID

        self.client = app.test_client()
        async def reset_db():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM dashboard_users")
                await db.commit()
        run_async(reset_db())

        # Seed test bot guilds
        run_async(sync_bot_guilds([
            {
                "guild_id": INSTALLED_GUILD_1,
                "name": "Bot Server 1 (No Dev Membership)",
                "icon": "icon1",
                "owner_id": 999999999,
                "member_count": 50,
            },
            {
                "guild_id": INSTALLED_GUILD_2,
                "name": "Bot Server 2 (Dev Regular Member)",
                "icon": "icon2",
                "owner_id": 888888888,
                "member_count": 100,
            },
            {
                "guild_id": INSTALLED_GUILD_3,
                "name": "Bot Server 3 (Dev Owned)",
                "icon": "icon3",
                "owner_id": DEV_USER_ID,
                "member_count": 25,
            },
        ]))

    def _login(self, user_id: int, username: str = "TestUser", access_token: str = "fake-token"):
        with self.client.session_transaction() as sess:
            sess["user"] = {"id": str(user_id), "username": username, "avatar": None}
            sess["access_token"] = access_token
            sess["expires_at"] = time.time() + 3600
            sess["csrf_token"] = "test-csrf-token"

    def test_01_trusted_dev_bot_installed_dev_not_in_guild(self):
        """Scenario 1: Trusted developer accesses server where bot is installed but dev is NOT in guild."""
        self._login(DEV_USER_ID, "Developer")
        user_guilds = [
            {"id": str(INSTALLED_GUILD_2), "name": "Bot Server 2", "permissions": "0"},
            {"id": str(INSTALLED_GUILD_3), "name": "Bot Server 3", "permissions": "8", "owner": True},
        ]
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}

            self.assertIn(INSTALLED_GUILD_1, servers_by_id)
            s1 = servers_by_id[INSTALLED_GUILD_1]
            self.assertEqual(s1["category"], "active")
            self.assertEqual(s1["permission_level"], "owner")
            self.assertTrue(s1["has_dashboard_access"])

            # POST /select-guild should succeed (302 redirect)
            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_1}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 302)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["guild_id"], INSTALLED_GUILD_1)
                self.assertEqual(sess["user_level"], "owner")

            # Middleware verify: protected page /config/general allows access
            page_resp = self.client.get("/config/general")
            self.assertEqual(page_resp.status_code, 200)

    def test_02_trusted_dev_bot_installed_dev_regular_member(self):
        """Scenario 2: Developer is a regular member of the Discord server with 0 admin permissions."""
        self._login(DEV_USER_ID, "Developer")
        user_guilds = [
            {"id": str(INSTALLED_GUILD_2), "name": "Bot Server 2", "permissions": "0"},
        ]
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}

            self.assertIn(INSTALLED_GUILD_2, servers_by_id)
            s2 = servers_by_id[INSTALLED_GUILD_2]
            self.assertEqual(s2["category"], "active")
            self.assertEqual(s2["permission_level"], "owner")

            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_2}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 302)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["guild_id"], INSTALLED_GUILD_2)
                self.assertEqual(sess["user_level"], "owner")

    def test_03_trusted_dev_bot_installed_dev_server_owner(self):
        """Scenario 3: Developer is the Discord server owner."""
        self._login(DEV_USER_ID, "Developer")
        user_guilds = [
            {"id": str(INSTALLED_GUILD_3), "name": "Bot Server 3", "permissions": "8", "owner": True},
        ]
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}

            self.assertIn(INSTALLED_GUILD_3, servers_by_id)
            s3 = servers_by_id[INSTALLED_GUILD_3]
            self.assertEqual(s3["category"], "active")
            self.assertEqual(s3["permission_level"], "owner")

            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_3}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 302)

    def test_04_trusted_dev_bot_not_installed_arbitrary_guild_denied(self):
        """Scenario 4: Developer cannot access arbitrary guild ID where bot is NOT installed."""
        self._login(DEV_USER_ID, "Developer")
        sel_resp = self.client.post(
            f"/select-guild/{UNINSTALLED_GUILD}",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(sel_resp.status_code, 403)

    def test_05_ordinary_user_not_in_guild_denied(self):
        """Scenario 5: Ordinary user cannot see or access server they do not belong to."""
        self._login(NORMAL_USER_ID, "NormalUser")
        user_guilds = []
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}
            self.assertNotIn(INSTALLED_GUILD_1, servers_by_id)

            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_1}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 403)

    def test_06_ordinary_user_in_guild_without_required_permissions(self):
        """Scenario 6: Ordinary user in guild with no manage permissions and no DB grant."""
        self._login(NORMAL_USER_ID, "NormalUser")
        user_guilds = [
            {"id": str(INSTALLED_GUILD_2), "name": "Bot Server 2", "permissions": "0"},
        ]
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}
            # User with 0 permissions and no DB grant is omitted
            self.assertNotIn(INSTALLED_GUILD_2, servers_by_id)

            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_2}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 403)

    def test_07_ordinary_user_in_guild_manage_server_no_db_grant(self):
        """Scenario 7: Ordinary user with Manage Server (0x20) but no DB grant gets 'access_required'."""
        self._login(NORMAL_USER_ID, "NormalUser")
        user_guilds = [
            {"id": str(INSTALLED_GUILD_1), "name": "Bot Server 1", "permissions": "32"},
        ]
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}
            self.assertIn(INSTALLED_GUILD_1, servers_by_id)
            self.assertEqual(servers_by_id[INSTALLED_GUILD_1]["category"], "access_required")
            self.assertFalse(servers_by_id[INSTALLED_GUILD_1]["has_dashboard_access"])

            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_1}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 403)

    def test_08_ordinary_user_authorized_server_admin_granted(self):
        """Scenario 8: Ordinary user with DB grant gets access."""
        async def add_grant():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO dashboard_users
                    (guild_id, user_id, permission_level, enabled)
                    VALUES (?, ?, 'admin', 1)
                """, (INSTALLED_GUILD_1, NORMAL_USER_ID))
                await db.commit()
        run_async(add_grant())

        self._login(NORMAL_USER_ID, "NormalUser")
        user_guilds = [
            {"id": str(INSTALLED_GUILD_1), "name": "Bot Server 1", "permissions": "32"},
        ]
        with patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            data = resp.get_json()
            servers_by_id = {int(s["id"]): s for s in data["servers"]}
            self.assertIn(INSTALLED_GUILD_1, servers_by_id)
            self.assertEqual(servers_by_id[INSTALLED_GUILD_1]["category"], "active")
            self.assertEqual(servers_by_id[INSTALLED_GUILD_1]["permission_level"], "admin")

            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_1}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 302)

    def test_09_bot_removal_and_gateway_safety(self):
        """Scenario 9: Bot removal revokes access; incomplete state preserves records; authoritative empty sync purges and denies access."""
        self._login(DEV_USER_ID, "Developer")

        # 9a. Bot removal revokes access
        run_async(record_bot_guild_remove(INSTALLED_GUILD_1))
        sel_resp = self.client.post(
            f"/select-guild/{INSTALLED_GUILD_1}",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(sel_resp.status_code, 403)

        # 9b. Incomplete Gateway snapshot (authoritative=False) with empty guild list does NOT wipe database
        run_async(sync_bot_guilds([], authoritative=False))
        stored = run_async(get_stored_bot_guilds())
        stored_ids = {g["id_num"] for g in stored}
        # Guild 2 and 3 must still exist
        self.assertIn(INSTALLED_GUILD_2, stored_ids)
        self.assertIn(INSTALLED_GUILD_3, stored_ids)

        # 9c. Temporary guild unavailability preserves known name and icon
        run_async(sync_bot_guilds([
            {"guild_id": INSTALLED_GUILD_2, "name": None, "icon": None, "owner_id": 0, "member_count": 0},
        ], authoritative=False))
        stored2 = run_async(get_stored_bot_guilds())
        g2 = next(g for g in stored2 if g["id_num"] == INSTALLED_GUILD_2)
        self.assertEqual(g2["name"], "Bot Server 2 (Dev Regular Member)")
        self.assertEqual(g2["icon"], "icon2")

        # 9d. Authoritative empty sync ([] with authoritative=True): confirms zero-guild bot state,
        # purges all stale records, sets guilds_synced = '1', and Dashboard denies access.
        run_async(sync_bot_guilds([], authoritative=True))
        stored_empty = run_async(get_stored_bot_guilds())
        self.assertEqual(len(stored_empty), 0)
        self.assertTrue(run_async(is_bot_guild_synced()))

        # Dashboard immediately treats empty authoritative sync as confirmed zero membership
        sel_resp2 = self.client.post(
            f"/select-guild/{INSTALLED_GUILD_2}",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(sel_resp2.status_code, 403)

    def test_10_zero_discord_bot_rest_requests_when_synced(self):
        """Scenario 10: Server select and guild check make ZERO outbound Discord REST calls with bot token."""
        self._login(DEV_USER_ID, "Developer")
        with patch("dashboard.auth.requests.get") as mock_req:
            # Server discovery
            resp = self.client.get("/api/user/servers")
            self.assertEqual(resp.status_code, 200)

            # Guild selection
            sel_resp = self.client.post(
                f"/select-guild/{INSTALLED_GUILD_2}",
                data={"csrf_token": "test-csrf-token"},
            )
            self.assertEqual(sel_resp.status_code, 302)

            # Assert requests.get was NEVER called for bot endpoints (Bot <token>)
            bot_calls = [c for c in mock_req.call_args_list if "Bot " in str(c)]
            self.assertEqual(len(bot_calls), 0)

    def test_11_throttle_enforcement_under_unsynced_and_empty_states(self):
        """Scenario 11 (Blocker 1): Repeated calls during unsynchronized or empty states respect 60s throttle."""
        async def clear_sync_state():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM bot_guilds")
                await db.execute("DELETE FROM bot_sync_state")
                await db.commit()
        run_async(clear_sync_state())

        auth._last_bot_rest_attempt = 0.0
        with patch.dict(os.environ, {"DISCORD_TOKEN": "mock-bot-token"}), \
             patch("dashboard.auth._fetch_paginated_guilds") as mock_fetch:
            from dashboard.auth import GuildDiscoveryList
            mock_fetch.return_value = GuildDiscoveryList([], ok=True, complete=True, status="ok", http_status=200)

            current_time = 1000.0
            with patch("dashboard.auth.time.time", side_effect=lambda: current_time):
                # Call 1: First attempt allowed
                res1 = auth.fetch_bot_guilds_full()
                self.assertEqual(mock_fetch.call_count, 1)

                # Call 2: Immediate repeat must be throttled without REST call
                res2 = auth.fetch_bot_guilds_full()
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(res2.status, "sync_pending")

                # Call 3: Advance 30s (< 60s) - still throttled
                current_time = 1030.0
                res3 = auth.fetch_bot_guilds_full()
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(res3.status, "sync_pending")

                # Call 4: Advance to 1061s (> 60s) - allowed
                current_time = 1061.0
                res4 = auth.fetch_bot_guilds_full()
                self.assertEqual(mock_fetch.call_count, 2)

    def test_12_database_exceptions_never_trigger_discord_rest_requests(self):
        """Scenario 12 (Blocker 1): Database exceptions in fetch_bot_guilds_full or check_bot_guild_membership do not fall through to REST."""
        with patch.dict(os.environ, {"DISCORD_TOKEN": "mock-bot-token"}), \
             patch("database.is_bot_guild_synced", side_effect=Exception("Simulated SQLite lock error")), \
             patch("database.is_stored_bot_guild", side_effect=Exception("Simulated SQLite lock error")), \
             patch("dashboard.auth.requests.get") as mock_http, \
             patch("dashboard.auth._fetch_paginated_guilds") as mock_fetch:

            # 1. fetch_bot_guilds_full should return db_error without calling Discord REST
            res = auth.fetch_bot_guilds_full()
            self.assertFalse(res.ok)
            self.assertEqual(res.status, "db_error")
            self.assertIn("Database error", res.error)
            mock_fetch.assert_not_called()
            mock_http.assert_not_called()

            # 2. check_bot_guild_membership should return INDETERMINATE without calling Discord REST
            status = auth.check_bot_guild_membership(INSTALLED_GUILD_1)
            self.assertEqual(status, auth.BotMembershipStatus.INDETERMINATE)
            mock_http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
