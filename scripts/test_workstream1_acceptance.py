"""
Comprehensive acceptance test suite for Workstream 1:
- Discord permission helpers (0x8, 0x20, 0x28, unprivileged)
- Bot membership classification (INSTALLED, CONFIRMED_NO_ACCESS, INDETERMINATE)
- Server categorization in /api/user/servers
- Dual-context consistency verification (X-Guild-ID vs session)
- Non-API form context verification & value preservation on conflict
- Explicit server switching with CSRF and open-redirect protection
- Stale GET navigation conflict detection
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["NERO_ENVIRONMENT"] = "staging"
os.environ["STAGING_DATABASE_PATH"] = "/tmp/test_ws1.db"
os.environ["OWNER_ID"] = "999999999"
os.environ["SECRET_KEY"] = "0123456789abcdef0123456789abcdef"
os.environ["DISCORD_TOKEN"] = "mock_bot_token"

from database import init_db
from dashboard.utils.async_utils import run_async
run_async(init_db())

from dashboard.app import app
from dashboard.auth import (
    DISCORD_PERMISSION_ADMINISTRATOR, DISCORD_PERMISSION_MANAGE_GUILD,
    BotMembershipStatus, guild_permissions_can_manage_server,
    guild_permissions_include_admin, check_bot_guild_membership,
    bot_is_in_guild,
)
import aiosqlite
from database import DB_PATH


class TestWorkstream1(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.user_id = 123456789
        self.guild_a = 900000000000000001
        self.guild_b = 900000000000000002

        # Seed test database grants
        async def seed():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM dashboard_users")
                await db.execute("DELETE FROM guild_settings")
                await db.execute("""
                    INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled)
                    VALUES (?, ?, 'owner', 1), (?, ?, 'admin', 1)
                """, (self.guild_a, self.user_id, self.guild_b, self.user_id))
                await db.execute("""
                    INSERT INTO guild_settings (guild_id, prefix, timezone)
                    VALUES (?, '!', 'Africa/Cairo'), (?, '?', 'UTC')
                """, (self.guild_a, self.guild_b))
                await db.commit()
        run_async(seed())

    def _login(self, client, active_guild=None):
        with client.session_transaction() as sess:
            sess["user"] = {
                "id": str(self.user_id),
                "username": "TestUser",
                "avatar": None,
            }
            sess["access_token"] = "mock_access_token"
            sess["expires_at"] = 9999999999
            sess["csrf_token"] = "valid_csrf_token_hex"
            if active_guild:
                sess["guild_id"] = active_guild
                sess["user_level"] = "owner"
                sess["guild_name"] = f"Server {active_guild}"

    def test_permission_helpers(self):
        # 0x8 = Admin, 0x20 = Manage Guild, 0x28 = Both, 0x400 = unprivileged
        self.assertTrue(guild_permissions_can_manage_server(0x8))
        self.assertTrue(guild_permissions_can_manage_server("8"))
        self.assertTrue(guild_permissions_can_manage_server(0x20))
        self.assertTrue(guild_permissions_can_manage_server("32"))
        self.assertTrue(guild_permissions_can_manage_server(0x28))
        self.assertTrue(guild_permissions_can_manage_server("40"))
        self.assertFalse(guild_permissions_can_manage_server(0x400))
        self.assertFalse(guild_permissions_can_manage_server(0))
        self.assertFalse(guild_permissions_can_manage_server(None))
        self.assertFalse(guild_permissions_can_manage_server("invalid"))

        # Legacy helper checks only 0x8
        self.assertTrue(guild_permissions_include_admin(0x8))
        self.assertFalse(guild_permissions_include_admin(0x20))
        self.assertTrue(guild_permissions_include_admin(0x28))
        self.assertFalse(guild_permissions_include_admin(0x400))

    @patch("dashboard.auth.requests.get")
    def test_conservative_bot_membership_classification(self, mock_get):
        # 1. 200 -> INSTALLED
        mock_get.return_value = MagicMock(status_code=200)
        self.assertEqual(check_bot_guild_membership(self.guild_a), BotMembershipStatus.INSTALLED)
        self.assertTrue(bot_is_in_guild(self.guild_a))

        # 2. 404 with code 10004 -> CONFIRMED_NO_ACCESS
        mock_get.return_value = MagicMock(status_code=404, json=lambda: {"code": 10004, "message": "Unknown Guild"})
        self.assertEqual(check_bot_guild_membership(self.guild_a), BotMembershipStatus.CONFIRMED_NO_ACCESS)
        self.assertFalse(bot_is_in_guild(self.guild_a))

        # 3. 403 with code 50001 -> CONFIRMED_NO_ACCESS
        mock_get.return_value = MagicMock(status_code=403, json=lambda: {"code": 50001, "message": "Missing Access"})
        self.assertEqual(check_bot_guild_membership(self.guild_a), BotMembershipStatus.CONFIRMED_NO_ACCESS)

        # 4. 429 Rate Limit -> INDETERMINATE (Not assumed removed!)
        mock_get.return_value = MagicMock(status_code=429, json=lambda: {"retry_after": 2.5})
        self.assertEqual(check_bot_guild_membership(self.guild_a), BotMembershipStatus.INDETERMINATE)

        # 5. 500 Internal Error -> INDETERMINATE
        mock_get.return_value = MagicMock(status_code=500, json=lambda: {})
        self.assertEqual(check_bot_guild_membership(self.guild_a), BotMembershipStatus.INDETERMINATE)

    @patch("dashboard.app.fetch_discord_guilds")
    @patch("dashboard.auth.fetch_bot_guilds_full")
    def test_api_user_servers_categorization(self, mock_bot_guilds, mock_user_guilds):
        mock_user_guilds.return_value = [
            {"id": str(self.guild_a), "name": "Active Server", "permissions": "8", "icon": "a_icon1"},
            {"id": "900000000000000003", "name": "Manageable But Not In Bot", "permissions": "32", "icon": None},
            {"id": "900000000000000004", "name": "In Bot But No DB Grant", "permissions": "32", "icon": None},
        ]
        mock_bot_guilds.return_value = [
            {"id": str(self.guild_a), "name": "Active Server", "icon": "a_icon1"},
            {"id": "900000000000000004", "name": "In Bot But No DB Grant", "icon": None},
        ]

        self._login(self.client)
        res = self.client.get("/api/user/servers")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        servers = data.get("servers", [])
        self.assertEqual(len(servers), 4)

        cat_map = {s["id"]: s["category"] for s in servers}
        self.assertEqual(cat_map[self.guild_a], "active")
        self.assertEqual(cat_map[900000000000000003], "invite")
        self.assertEqual(cat_map[900000000000000004], "access_required")
        self.assertEqual(cat_map[self.guild_b], "dangling")

    def test_cross_tab_api_context_guard(self):
        self._login(self.client, active_guild=self.guild_a)

        # 1. Matching X-Guild-ID -> 200 OK
        res = self.client.get(
            "/api/leveling/config",
            headers={"X-Guild-ID": str(self.guild_a)}
        )
        self.assertEqual(res.status_code, 200)

        # 2. Mismatched X-Guild-ID -> 409 Conflict
        res = self.client.get(
            "/api/leveling/config",
            headers={"X-Guild-ID": str(self.guild_b)}
        )
        self.assertEqual(res.status_code, 409)
        data = res.get_json()
        self.assertEqual(data.get("code"), "GUILD_CONTEXT_MISMATCH")

        # 3. Malformed X-Guild-ID -> 400 Bad Request
        res = self.client.get(
            "/api/leveling/config",
            headers={"X-Guild-ID": "abc_not_an_id"}
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data.get("code"), "MALFORMED_GUILD_CONTEXT")

    def test_global_routes_exemption(self):
        # Global routes should succeed without X-Guild-ID and without active session guild
        self._login(self.client, active_guild=None)
        res = self.client.get("/server-select")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/user/servers")
        self.assertEqual(res.status_code, 200)
        res = self.client.get(f"/api/invite-bot/{self.guild_a}")
        self.assertEqual(res.status_code, 200)

    def test_non_api_form_mutation_context_guard(self):
        self._login(self.client, active_guild=self.guild_a)

        # 1. Matching context -> 302 Redirect, updates settings
        res = self.client.post("/config/general", data={
            "guild_id": str(self.guild_a),
            "csrf_token": "valid_csrf_token_hex",
            "prefix": "+",
            "timezone": "Africa/Cairo",
        })
        self.assertEqual(res.status_code, 302)

        # Verify DB updated for Guild A
        async def check_prefix():
            async with aiosqlite.connect(DB_PATH) as db:
                c = await db.execute("SELECT prefix FROM guild_settings WHERE guild_id=?", (self.guild_a,))
                return (await c.fetchone())[0]
        self.assertEqual(run_async(check_prefix()), "+")

        # 2. Mismatched context (Guild B in form, session is Guild A) -> 409 Conflict
        # Must re-render with submitted values preserved, without modifying Guild A or Guild B
        res = self.client.post("/config/general", data={
            "guild_id": str(self.guild_b),
            "csrf_token": "valid_csrf_token_hex",
            "prefix": "###",
            "timezone": "Africa/Cairo",
        })
        self.assertEqual(res.status_code, 409)
        html = res.get_data(as_text=True)
        # Verify submitted value is preserved in returned HTML
        self.assertIn('value="###"', html)
        self.assertIn("Server context mismatch", html)

        # Verify Guild A still has '+' and Guild B still has '?'
        async def check_both():
            async with aiosqlite.connect(DB_PATH) as db:
                ca = await (await db.execute("SELECT prefix FROM guild_settings WHERE guild_id=?", (self.guild_a,))).fetchone()
                cb = await (await db.execute("SELECT prefix FROM guild_settings WHERE guild_id=?", (self.guild_b,))).fetchone()
                return ca[0], cb[0]
        pref_a, pref_b = run_async(check_both())
        self.assertEqual(pref_a, "+")
        self.assertEqual(pref_b, "?")

        # 3. Missing guild_id -> 400 Bad Request
        res = self.client.post("/config/general", data={
            "csrf_token": "valid_csrf_token_hex",
            "prefix": "invalid",
        })
        self.assertEqual(res.status_code, 400)

        # 4. Malformed guild_id -> 400 Bad Request
        res = self.client.post("/config/general", data={
            "guild_id": "malformed",
            "csrf_token": "valid_csrf_token_hex",
            "prefix": "invalid",
        })
        self.assertEqual(res.status_code, 400)

        # 5. Welcome config context mismatch -> 409 and preserved
        res = self.client.post("/config/welcome", data={
            "guild_id": str(self.guild_b),
            "csrf_token": "valid_csrf_token_hex",
            "rules_button_text": "Custom Button Text",
        })
        self.assertEqual(res.status_code, 409)
        html = res.get_data(as_text=True)
        self.assertIn('value="Custom Button Text"', html)

        # 6. Boost config context mismatch -> 409
        res = self.client.post("/config/boost", data={
            "guild_id": str(self.guild_b),
            "csrf_token": "valid_csrf_token_hex",
            "boost1_role_id": "999888777",
        })
        self.assertEqual(res.status_code, 409)

        # 7. Access config context mismatch -> 409
        res = self.client.post("/config/access", data={
            "guild_id": str(self.guild_b),
            "csrf_token": "valid_csrf_token_hex",
            "action": "add",
            "user_id": "555555555",
            "level": "moderator",
        })
        self.assertEqual(res.status_code, 409)

    def test_stale_get_navigation_and_explicit_switch(self):
        self._login(self.client, active_guild=self.guild_a)

        # 1. Stale GET navigation with ?g=GuildB when session is GuildA
        res = self.client.get(f"/moderation?g={self.guild_b}")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Active Server Mismatch", html)
        self.assertIn(str(self.guild_b), html)

        # Verify session was NOT silently mutated
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("guild_id"), self.guild_a)

        # 2. Explicit CSRF-protected POST to switch server
        res = self.client.post(
            f"/select-guild/{self.guild_b}",
            data={"csrf_token": "valid_csrf_token_hex", "next": "/moderation"}
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/moderation")

        # Verify session is now Guild B
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("guild_id"), self.guild_b)

        # 3. Open-redirect attack in `next` is safely rejected
        res = self.client.post(
            f"/select-guild/{self.guild_a}",
            data={"csrf_token": "valid_csrf_token_hex", "next": "https://evil.com/hack"}
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("Location"), "/")

    def test_unauthorized_server_selection_blocked(self):
        self._login(self.client, active_guild=self.guild_a)
        # Attempt to select guild 999 where user has no grant
        res = self.client.post(
            "/select-guild/999999999999999999",
            data={"csrf_token": "valid_csrf_token_hex"}
        )
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
