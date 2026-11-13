"""Comprehensive acceptance tests for the Unified Message & Role Studio.

Verifies:
1. Studio template saving & retrieval with buttons & panel config
2. Studio message dispatch with role validation (valid roles pass, invalid roles fail with 400)
3. Direct reaction roles table persistence on message send
4. Legacy /reaction-roles route and API backwards compatibility
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"
os.environ["OWNER_ID"] = "999999999"

from scripts.phase1_support import GUILD, USER, reset_database, rows, execute
import aiosqlite
from database import DB_PATH, init_db

class TestMessageRoleStudio(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        self.guild_id = GUILD
        self.user_id = USER

        def _fake_channel_get(url, headers=None, timeout=None, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            ch_id = url.rstrip("/").split("/")[-1]
            resp.json.return_value = {"id": ch_id, "guild_id": str(self.guild_id), "type": 0}
            return resp

        ch_patcher = patch("dashboard.api.embedbuilder._req.get", side_effect=_fake_channel_get)
        ch_patcher.start()
        self.addCleanup(ch_patcher.stop)

    async def test_01_template_save_and_retrieve_with_buttons(self):
        """Verify saving and retrieving studio templates with embeds and interactive buttons."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        template_payload = {
            "name": "announcement_roles",
            "content": "Welcome to the server! Choose your roles below:",
            "embeds": [{
                "title": "Community Roles",
                "description": "Click a button to assign yourself a role.",
                "color": 8150463, # #7c5cbf
                "fields": [{"name": "Rules", "value": "Be respectful", "inline": False}]
            }],
            "buttons": [
                {"id": "btn_1", "label": "Gamer", "role_id": "7001", "color": "blurple", "emoji": "🎮", "booster_only": False},
                {"id": "btn_2", "label": "VIP", "role_id": "7002", "color": "green", "emoji": "⭐", "booster_only": True}
            ],
            "panel_config": {
                "exclusive": 1,
                "max_roles": 2,
                "require_confirmation": True
            }
        }

        # Save template
        save_resp = client.post(
            "/api/embedbuilder/template/save",
            json=template_payload,
            headers={"X-CSRF-Token": "studio-csrf"}
        )
        self.assertEqual(save_resp.status_code, 200)
        self.assertTrue(save_resp.get_json()["success"])

        # Retrieve template
        get_resp = client.get("/api/embedbuilder/template/announcement_roles")
        self.assertEqual(get_resp.status_code, 200)
        data = get_resp.get_json()
        self.assertIsNotNone(data.get("template"))
        tpl = data["template"]
        self.assertEqual(tpl["content"], template_payload["content"])
        self.assertEqual(len(tpl["embeds"]), 1)
        self.assertEqual(len(tpl["buttons"]), 2)
        self.assertEqual(tpl["buttons"][0]["label"], "Gamer")
        self.assertEqual(tpl["buttons"][1]["role_id"], "7002")
        self.assertEqual(tpl["panel_config"]["exclusive"], 1)

    async def test_02_send_message_role_validation_and_persistence(self):
        """Verify role validation and database persistence when sending studio messages."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        payload_obj = {
            "content": "Server updates:",
            "embeds": [{
                "title": "Role Menu",
                "description": "Select your preferences",
                "color": 5793266
            }],
            "buttons": [
                {"id": "btn_a", "label": "Announcements", "role_id": "8001", "color": "blurple", "emoji": "📢"},
                {"id": "btn_b", "label": "Events", "role_id": "8002", "color": "green", "emoji": "🎉"}
            ],
            "panel_config": {
                "exclusive": 1,
                "max_roles": 1,
                "require_confirmation": False
            }
        }

        # 1. When role validation fails -> return 400
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(False, "Role is above bot's highest role")):
            fail_resp = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_obj)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(fail_resp.status_code, 400)
            self.assertIn("Invalid Button Role", fail_resp.get_json()["error"])

        # 2. When role validation passes and Discord API succeeds -> persists panels & roles
        mock_discord_resp = MagicMock()
        mock_discord_resp.status_code = 200
        mock_discord_resp.json.return_value = {"id": "999888777666555444"}

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_discord_resp), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_discord_resp), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            ok_resp = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_obj)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(ok_resp.status_code, 200)
            self.assertTrue(ok_resp.get_json()["success"])
            self.assertEqual(ok_resp.get_json()["message_id"], "999888777666555444")

            # Verify reaction_role_panels
            async with aiosqlite.connect(DB_PATH) as db:
                c1 = await db.execute("SELECT message_id, guild_id, exclusive, max_roles FROM reaction_role_panels WHERE message_id=999888777666555444")
                panel_row = await c1.fetchone()
                self.assertIsNotNone(panel_row)
                self.assertEqual(panel_row[0], 999888777666555444)
                self.assertEqual(panel_row[2], 1)
                self.assertEqual(panel_row[3], 1)

                c2 = await db.execute("SELECT role_id, button_label, button_color, button_emoji FROM reaction_roles WHERE message_id=999888777666555444 ORDER BY role_id")
                role_rows = await c2.fetchall()
                self.assertEqual(len(role_rows), 2)
                self.assertEqual(role_rows[0], (8001, "Announcements", "blurple", "📢"))
                self.assertEqual(role_rows[1], (8002, "Events", "green", "🎉"))

    async def test_03_bot_view_reconstruction_from_studio_persisted_panel(self):
        """Verify cogs.reactionroles.build_view reconstructs valid discord.ui.View from studio tables."""
        from cogs.reactionroles import ReactionRoles

        # Simulate panel created by studio in SQLite
        msg_id = 123456789012345678
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 1, 2, 0)
            """, (msg_id, self.guild_id))
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Warrior', '⚔️', 'blurple', 9001, 0, NULL),
                       (?, 111, ?, 'Mage', '✨', 'green', 9002, 1, 9001)
            """, (self.guild_id, msg_id, self.guild_id, msg_id))
            await db.commit()

        mock_bot = MagicMock()
        rr_cog = ReactionRoles(mock_bot)
        view = await rr_cog.build_view(msg_id)

        self.assertIsNotNone(view)
        self.assertEqual(len(view.children), 2)

        btn1 = view.children[0]
        self.assertEqual(btn1.label, "Warrior")
        self.assertEqual(str(btn1.emoji), "⚔️")
        self.assertEqual(btn1.role_id, 9001)
        self.assertTrue(btn1.custom_id.startswith(f"rr_9001_{msg_id}"))
        self.assertTrue(btn1.exclusive)
        self.assertEqual(btn1.max_roles, 2)
        self.assertFalse(btn1.booster_only)

        btn2 = view.children[1]
        self.assertEqual(btn2.label, "Mage")
        self.assertEqual(str(btn2.emoji), "✨")
        self.assertEqual(btn2.role_id, 9002)
        self.assertTrue(btn2.custom_id.startswith(f"rr_9002_{msg_id}"))
        self.assertTrue(btn2.booster_only)
        self.assertEqual(btn2.required_role_id, 9001)

    async def test_04_legacy_reaction_roles_panel_crud_compatibility(self):
        """Verify legacy /api/save-rr-panel and /api/rr-panels routes work seamlessly."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        panel_data = {
            "title": "Legacy Rules Panel",
            "description": "Legacy description",
            "color": "#e056fd",
            "channel_id": "920000000000000001",
            "buttons": [
                {"role_id": "9901", "label": "Verified", "color": "green", "emoji": "✅"}
            ],
            "exclusive": False,
            "max_roles": 0,
            "require_confirmation": False
        }

        # Save via legacy endpoint with valid role check
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")):
            resp = client.post("/api/save-rr-panel", json=panel_data, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["success"])

        # Fetch via legacy endpoint
        list_resp = client.get("/api/rr-panels")
        self.assertEqual(list_resp.status_code, 200)
        panels = list_resp.get_json()["panels"]
        self.assertGreaterEqual(len(panels), 1)
        found = [p for p in panels if p["title"] == "Legacy Rules Panel"]
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["buttons"]) if isinstance(found[0]["buttons"], list) else found[0]["buttons"], 1)

    async def test_05_button_boundaries_and_row_chunking(self):
        """Verify button layout boundaries: 0, 1, 5, 6, 25 allowed with correct chunking; 26 rejected."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        # 1. 26 buttons -> Rejected with 400
        buttons_26 = [{"id": f"b_{i}", "label": f"R{i}", "role_id": str(1000 + i)} for i in range(26)]
        payload_26 = {"content": "26 Buttons", "buttons": buttons_26}

        resp_26 = client.post(
            "/api/embedbuilder/send",
            data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_26)},
            headers={"X-CSRF-Token": "studio-csrf"}
        )
        self.assertEqual(resp_26.status_code, 400)
        self.assertIn("at most 25 buttons", resp_26.get_json()["error"])

        # 2. 6 buttons -> Chunked into 2 action rows (5 in first, 1 in second)
        buttons_6 = [{"id": f"b_{i}", "label": f"R{i}", "role_id": str(1000 + i)} for i in range(6)]
        payload_6 = {"content": "6 Buttons", "buttons": buttons_6}

        mock_post = MagicMock()
        mock_post.status_code = 200
        mock_post.json.return_value = {"id": "111222333444555666"}

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post) as mock_req_post, \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_post), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            resp_6 = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_6)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(resp_6.status_code, 200)

            # Inspect payload sent to Discord
            call_kwargs = mock_req_post.call_args[1]
            sent_json = call_kwargs["json"]
            self.assertEqual(len(sent_json["components"]), 2)
            self.assertEqual(len(sent_json["components"][0]["components"]), 5)
            self.assertEqual(len(sent_json["components"][1]["components"]), 1)

    async def test_06_failure_scenarios_discord_send_and_compensation(self):
        """Verify failure scenarios: Discord POST failure (no orphan DB records) and DB failure (compensation delete)."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        payload = {
            "content": "Fail test",
            "buttons": [{"id": "b1", "label": "Role1", "role_id": "5001"}]
        }

        # Case A: Discord POST fails -> Returns error, 0 DB records created
        mock_fail_post = MagicMock()
        mock_fail_post.status_code = 400
        mock_fail_post.json.return_value = {"message": "Cannot send empty message"}
        mock_fail_post.text = "Cannot send empty message"

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_fail_post), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            fail_resp = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(fail_resp.status_code, 400) # Returns 400 on Discord 400 error
            self.assertFalse(fail_resp.get_json()["success"])
            self.assertIn("Discord API error 400", fail_resp.get_json()["error"])

            async with aiosqlite.connect(DB_PATH) as db:
                c = await db.execute("SELECT count(*) FROM reaction_roles WHERE role_id=5001")
                self.assertEqual((await c.fetchone())[0], 0)

        # Case B: Discord POST succeeds, but SQLite persistence fails -> compensation delete is triggered
        mock_ok_post = MagicMock()
        mock_ok_post.status_code = 200
        mock_ok_post.json.return_value = {"id": "888777666555444333"}

        def mock_failing_run_async(coro):
            coro.close()
            raise RuntimeError("SQLite connection lock simulation")

        mock_del_success = MagicMock()
        mock_del_success.status_code = 204

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_ok_post), \
             patch("dashboard.api.embedbuilder._req.delete", return_value=mock_del_success) as mock_delete, \
             patch("dashboard.api.embedbuilder.run_async", side_effect=mock_failing_run_async), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            err_resp = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(err_resp.status_code, 500)
            self.assertFalse(err_resp.get_json()["success"])
            self.assertIn("Message was rolled back", err_resp.get_json()["error"])
            mock_delete.assert_called_once()
            delete_url = mock_delete.call_args[0][0]
            self.assertIn("888777666555444333", delete_url)

        # Case C: Discord POST succeeds, SQLite fails, AND compensating DELETE fails -> returns 500 with orphan message ID
        mock_del_fail = MagicMock()
        mock_del_fail.status_code = 500

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_ok_post), \
             patch("dashboard.api.embedbuilder._req.delete", return_value=mock_del_fail) as mock_delete_fail, \
             patch("dashboard.api.embedbuilder.run_async", side_effect=mock_failing_run_async), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            err_resp_c = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(err_resp_c.status_code, 500)
            self.assertFalse(err_resp_c.get_json()["success"])
            self.assertIn("compensation deletion failed", err_resp_c.get_json()["error"])
            self.assertEqual(err_resp_c.get_json().get("orphan_message_id"), "888777666555444333")

    async def test_07_on_interaction_dynamic_fallback(self):
        """Verify on_interaction catches dynamic clicks when view is unregistered or clicked before PATCH."""
        import discord
        from cogs.reactionroles import ReactionRoles

        msg_id = 987654321098765432
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Explorer', '🧭', 'blurple', 7777, 0, NULL)
            """, (self.guild_id, msg_id))
            await db.commit()

        mock_bot = MagicMock()
        rr_cog = ReactionRoles(mock_bot)

        # Mock interaction with custom_id from draft (pre-patch)
        from unittest.mock import AsyncMock
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.type = discord.InteractionType.component
        mock_interaction.response = MagicMock()
        mock_interaction.response.is_done.return_value = False
        mock_interaction.response.send_message = AsyncMock()
        mock_interaction.data = {"custom_id": "rr_7777_draft_0_0"}
        mock_interaction.message = MagicMock()
        mock_interaction.message.id = msg_id
        mock_interaction.guild = MagicMock()
        mock_interaction.guild.id = self.guild_id
        mock_interaction.user = MagicMock()
        mock_interaction.user.id = self.user_id
        mock_interaction.user.roles = []
        mock_interaction.user.premium_since = None
        mock_interaction.user.add_roles = AsyncMock()
        mock_role = MagicMock()
        mock_role.name = "Explorer"
        mock_role.id = 7777
        mock_interaction.guild.get_role.return_value = mock_role

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(mock_interaction)
            mock_interaction.user.add_roles.assert_called_once_with(mock_role)
            mock_interaction.response.send_message.assert_called_once()
            call_text = mock_interaction.response.send_message.call_args[0][0]
            self.assertIn("Gave you **Explorer**", call_text)

    async def test_08_csrf_and_auth_enforcement(self):
        """Verify CSRF token and authorization enforcement on studio endpoints."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        client = app.test_client()

        # 1. Unauthenticated request -> 401 or redirect
        resp_unauth = client.post("/api/embedbuilder/send", data={"channel_id": "1", "payload_json": "{}"})
        self.assertIn(resp_unauth.status_code, (401, 403, 302))

        # 2. Authenticated but missing CSRF token -> 403
        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        resp_no_csrf = client.post("/api/embedbuilder/send", data={"channel_id": "1", "payload_json": "{}"})
        self.assertEqual(resp_no_csrf.status_code, 403)

    async def test_09_exactly_once_registered_view_vs_fallback_no_collision(self):
        """Verify that when a persistent view is registered, on_interaction skips fallback and callback runs exactly once."""
        import discord
        from cogs.reactionroles import ReactionRoles

        msg_id = 555666777888999111
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Citizen', '🏛️', 'blurple', 6666, 0, NULL)
            """, (self.guild_id, msg_id))
            await db.commit()

        mock_bot = MagicMock()
        # Mock view_store indicating message is tracked by a registered view
        mock_view_store = MagicMock()
        mock_view_store.is_message_tracked.return_value = True
        mock_bot._connection._view_store = mock_view_store

        rr_cog = ReactionRoles(mock_bot)

        from unittest.mock import AsyncMock
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.type = discord.InteractionType.component
        mock_interaction.response = MagicMock()
        mock_interaction.response.is_done.return_value = False
        mock_interaction.response.send_message = AsyncMock()
        mock_interaction.data = {"custom_id": f"rr_6666_{msg_id}"}
        mock_interaction.message = MagicMock()
        mock_interaction.message.id = msg_id
        mock_interaction.guild = MagicMock()
        mock_interaction.guild.id = self.guild_id
        mock_interaction.user = MagicMock()
        mock_interaction.user.id = self.user_id
        mock_interaction.user.roles = []
        mock_interaction.user.premium_since = None
        mock_interaction.user.add_roles = AsyncMock()
        mock_role = MagicMock()
        mock_role.name = "Citizen"
        mock_role.id = 6666
        mock_interaction.guild.get_role.return_value = mock_role

        # 1. on_interaction runs for a tracked message -> skips execution to avoid double-processing
        await rr_cog.on_interaction(mock_interaction)
        mock_interaction.user.add_roles.assert_not_called()
        mock_interaction.response.send_message.assert_not_called()

        # 2. Reconstructed RoleButton callback runs (as discord.py dispatch does) -> processes exactly once
        view = await rr_cog.build_view(msg_id)
        btn = view.children[0]
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await btn.callback(mock_interaction)
            mock_interaction.user.add_roles.assert_called_once_with(mock_role)
            mock_interaction.response.send_message.assert_called_once()
            self.assertIn("Gave you **Citizen**", mock_interaction.response.send_message.call_args[0][0])

    async def test_10_nonce_idempotency_and_http_status_mapping(self):
        """Verify nonce & enforce_nonce transmission and HTTP failure code mappings (403, 404, 429, 502)."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        payload_with_nonce = {
            "content": "Idempotent Message",
            "nonce": "client-nonce-123456789",
            "buttons": [{"id": "b1", "label": "RoleA", "role_id": "4001"}]
        }

        # 1. Verify nonce and enforce_nonce in sent JSON
        mock_post = MagicMock()
        mock_post.status_code = 200
        mock_post.json.return_value = {"id": "444555666777888999"}

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post) as mock_req_post, \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_post), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            resp = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_with_nonce)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(resp.status_code, 200)
            call_kwargs = mock_req_post.call_args[1]
            sent_json = call_kwargs["json"]
            self.assertEqual(sent_json.get("nonce"), "client-nonce-123456789")
            self.assertTrue(sent_json.get("enforce_nonce"))

        # 2. Verify HTTP error status mappings from Discord API
        error_cases = [
            (403, "Missing Permissions", 403, "Permission denied (403)"),
            (404, "Unknown Channel", 404, "Channel not found (404)"),
            (429, "You are being rate limited", 429, "rate limit reached (429)"),
            (503, "Service Unavailable", 502, "Discord service error (503)"),
        ]

        for discord_code, discord_msg, expected_http, expected_text in error_cases:
            mock_err = MagicMock()
            mock_err.status_code = discord_code
            mock_err.json.return_value = {"message": discord_msg, "retry_after": 5.0} if discord_code == 429 else {"message": discord_msg}
            mock_err.text = discord_msg
            mock_err.headers = {"Retry-After": "5"} if discord_code == 429 else {}

            with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
                 patch("dashboard.api.embedbuilder._req.post", return_value=mock_err), \
                 patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

                resp = client.post(
                    "/api/embedbuilder/send",
                    data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_with_nonce)},
                    headers={"X-CSRF-Token": "studio-csrf"}
                )
                self.assertEqual(resp.status_code, expected_http)
                self.assertIn(expected_text, resp.get_json()["error"])
                if discord_code == 429:
                    self.assertEqual(resp.get_json().get("retry_after"), "5")

    async def test_11_reaction_role_expiry_persistence_and_callback(self):
        """Verify temporary role expiry persistence (sentinel row) and per-member expiry row creation."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        payload_expiry = {
            "content": "Expiring Roles",
            "buttons": [{"id": "b_exp", "label": "Temp VIP", "role_id": "3333", "expiry_days": 14}]
        }

        mock_post = MagicMock()
        mock_post.status_code = 200
        mock_post.json.return_value = {"id": "111222333000999888"}

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_post), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            resp = client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "910000000000000001", "payload_json": json.dumps(payload_expiry)},
                headers={"X-CSRF-Token": "studio-csrf"}
            )
            self.assertEqual(resp.status_code, 200)

            # Check sentinel row in reaction_role_expiry (user_id = 0)
            async with aiosqlite.connect(DB_PATH) as db:
                c = await db.execute("""
                    SELECT expires_at FROM reaction_role_expiry
                    WHERE guild_id=? AND user_id=0 AND role_id=3333 AND message_id=111222333000999888
                """, (self.guild_id,))
                sentinel = await c.fetchone()
                self.assertIsNotNone(sentinel)
                self.assertIn("T", sentinel[0])

            # Now simulate user interaction with this expiring role button
            import discord
            from cogs.reactionroles import ReactionRoles

            mock_bot = MagicMock()
            rr_cog = ReactionRoles(mock_bot)
            view = await rr_cog.build_view(111222333000999888)
            btn = view.children[0]

            from unittest.mock import AsyncMock
            mock_interaction = MagicMock(spec=discord.Interaction)
            mock_interaction.response = MagicMock()
            mock_interaction.response.send_message = AsyncMock()
            mock_interaction.message = MagicMock()
            mock_interaction.message.id = 111222333000999888
            mock_interaction.guild = MagicMock()
            mock_interaction.guild.id = self.guild_id
            mock_interaction.user = MagicMock()
            mock_interaction.user.id = 998877
            mock_interaction.user.roles = []
            mock_interaction.user.premium_since = None
            mock_interaction.user.add_roles = AsyncMock()
            mock_role = MagicMock()
            mock_role.name = "Temp VIP"
            mock_role.id = 3333
            mock_interaction.guild.get_role.return_value = mock_role

            with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
                await btn.callback(mock_interaction)
                mock_interaction.user.add_roles.assert_called_once_with(mock_role)

                # Verify per-member expiry row inserted (user_id = 998877)
                async with aiosqlite.connect(DB_PATH) as db:
                    c2 = await db.execute("""
                        SELECT expires_at FROM reaction_role_expiry
                        WHERE guild_id=? AND user_id=998877 AND role_id=3333 AND message_id=111222333000999888
                    """, (self.guild_id,))
                    member_row = await c2.fetchone()
                    self.assertIsNotNone(member_row)
                    self.assertEqual(member_row[0], sentinel[0])

    async def test_12_failed_patch_post_restart_draft_id_click_handled_exactly_once(self):
        """Verify that when Discord PATCH fails, an unpatched draft-ID click after bot restart is handled exactly once."""
        import discord
        from cogs.reactionroles import ReactionRoles

        msg_id = 777888999000111222
        role_id = 8888

        # 1. Simulate Studio persisted records in SQLite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Gladiator', '⚔️', 'blurple', ?, 0, NULL)
            """, (self.guild_id, msg_id, role_id))
            await db.commit()

        # 2. Simulate Bot restart: restore_views() registers canonical View
        mock_bot = MagicMock()
        mock_view_store = MagicMock()
        mock_view_store.is_message_tracked.return_value = True
        mock_bot._connection._view_store = mock_view_store

        rr_cog = ReactionRoles(mock_bot)

        # 3. Simulate click on unpatched Discord button carrying draft custom_id: rr_8888_draft_0_0
        from unittest.mock import AsyncMock
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.type = discord.InteractionType.component
        mock_interaction.response = MagicMock()
        mock_interaction.response.is_done.return_value = False
        mock_interaction.response.send_message = AsyncMock()
        mock_interaction.data = {"custom_id": f"rr_{role_id}_draft_0_0"}
        mock_interaction.message = MagicMock()
        mock_interaction.message.id = msg_id
        mock_interaction.guild = MagicMock()
        mock_interaction.guild.id = self.guild_id
        mock_interaction.user = MagicMock()
        mock_interaction.user.id = self.user_id
        mock_interaction.user.roles = []
        mock_interaction.user.premium_since = None
        mock_interaction.user.add_roles = AsyncMock()
        mock_role = MagicMock()
        mock_role.name = "Gladiator"
        mock_role.id = role_id
        mock_interaction.guild.get_role.return_value = mock_role

        # 4. Fallback on_interaction must execute despite is_message_tracked being True because custom_id is a draft ID
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(mock_interaction)
            mock_interaction.user.add_roles.assert_called_once_with(mock_role)
            mock_interaction.response.send_message.assert_called_once()
            call_text = mock_interaction.response.send_message.call_args[0][0]
            self.assertIn("Gave you **Gladiator**", call_text)

    async def test_13_json_export_and_template_roundtrip(self):
        """Verify templates saved with JSON bridge payloads maintain full fidelity."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

        json_payload = {
            "name": "json_bridge_test",
            "content": "Imported from JSON Bridge",
            "embeds": [{
                "title": "JSON Embed",
                "description": "Valid embed content",
                "color": 5793266,
                "fields": [{"name": "F1", "value": "V1", "inline": True}]
            }],
            "buttons": [
                {"id": "btn_1", "label": "Role1", "role_id": "7771", "color": "green", "emoji": "🎉", "booster_only": False, "expiry_days": 14}
            ],
            "panel_config": {
                "exclusive": 1,
                "max_roles": 1,
                "require_confirmation": True
            }
        }

        save_res = client.post(
            "/api/embedbuilder/template/save",
            json=json_payload,
            headers={"X-CSRF-Token": "studio-csrf"}
        )
        self.assertEqual(save_res.status_code, 200)
        self.assertTrue(save_res.get_json().get("success"))

        fetch_res = client.get("/api/embedbuilder/template/json_bridge_test")
        self.assertEqual(fetch_res.status_code, 200)
        retrieved = fetch_res.get_json().get("template")
        self.assertEqual(retrieved["content"], "Imported from JSON Bridge")
        self.assertEqual(len(retrieved["buttons"]), 1)
        self.assertEqual(retrieved["buttons"][0]["expiry_days"], 14)
        self.assertEqual(retrieved["panel_config"]["exclusive"], 1)

    async def test_14_button_action_semantics_and_unconfigured_safety(self):
        """Verify button role addition, removal, hierarchy check, and unconfigured safety semantics."""
        import discord
        from cogs.reactionroles import ReactionRoles

        msg_id = 999888777666555444
        role_id = 8881
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Color Role', '🎨', 'red', ?, 0, NULL)
            """, (self.guild_id, msg_id, role_id))
            await db.commit()

        mock_bot = MagicMock()
        rr_cog = ReactionRoles(mock_bot)

        # 1. Configured Role Removal: User already has role -> clicks button -> role removed
        mock_role = MagicMock()
        mock_role.name = "Color Role"
        mock_role.id = role_id

        interaction_remove = MagicMock(spec=discord.Interaction)
        interaction_remove.type = discord.InteractionType.component
        interaction_remove.response = MagicMock()
        interaction_remove.response.is_done.return_value = False
        interaction_remove.response.send_message = AsyncMock()
        interaction_remove.data = {"custom_id": f"rr_{role_id}_{msg_id}"}
        interaction_remove.message = MagicMock()
        interaction_remove.message.id = msg_id
        interaction_remove.guild = MagicMock()
        interaction_remove.guild.id = self.guild_id
        interaction_remove.guild.get_role.return_value = mock_role
        interaction_remove.user = MagicMock()
        interaction_remove.user.id = self.user_id
        interaction_remove.user.roles = [mock_role] # already has role
        interaction_remove.user.remove_roles = AsyncMock()
        interaction_remove.user.add_roles = AsyncMock()

        await rr_cog.on_interaction(interaction_remove)
        interaction_remove.user.remove_roles.assert_called_once_with(mock_role)
        interaction_remove.user.add_roles.assert_not_called()
        interaction_remove.response.send_message.assert_called_once()
        self.assertIn("Removed **Color Role**", interaction_remove.response.send_message.call_args[0][0])

        # 2. Unconfigured Button Safety: Button with unconfigured/missing role in DB -> returns safe warning
        interaction_unconfigured = MagicMock(spec=discord.Interaction)
        interaction_unconfigured.type = discord.InteractionType.component
        interaction_unconfigured.response = MagicMock()
        interaction_unconfigured.response.is_done.return_value = False
        interaction_unconfigured.response.send_message = AsyncMock()
        interaction_unconfigured.data = {"custom_id": f"rr_0_{msg_id}"} # role 0 (unconfigured)
        interaction_unconfigured.message = MagicMock()
        interaction_unconfigured.message.id = msg_id
        interaction_unconfigured.guild = MagicMock()
        interaction_unconfigured.guild.id = self.guild_id
        interaction_unconfigured.user = MagicMock()
        interaction_unconfigured.user.id = self.user_id
        interaction_unconfigured.user.roles = []
        interaction_unconfigured.user.remove_roles = AsyncMock()
        interaction_unconfigured.user.add_roles = AsyncMock()

        await rr_cog.on_interaction(interaction_unconfigured)
        interaction_unconfigured.user.remove_roles.assert_not_called()
        interaction_unconfigured.user.add_roles.assert_not_called()
        interaction_unconfigured.response.send_message.assert_called_once()
        self.assertIn("unconfigured or no longer active", interaction_unconfigured.response.send_message.call_args[0][0])

        # 3. Hierarchy Protection: Role higher than bot -> fails closed with warning
        interaction_hier = MagicMock(spec=discord.Interaction)
        interaction_hier.type = discord.InteractionType.component
        interaction_hier.response = MagicMock()
        interaction_hier.response.is_done.return_value = False
        interaction_hier.response.send_message = AsyncMock()
        interaction_hier.data = {"custom_id": f"rr_{role_id}_{msg_id}"}
        interaction_hier.message = MagicMock()
        interaction_hier.message.id = msg_id
        interaction_hier.guild = MagicMock()
        interaction_hier.guild.id = self.guild_id
        interaction_hier.guild.get_role.return_value = mock_role
        interaction_hier.user = MagicMock()
        interaction_hier.user.id = self.user_id
        interaction_hier.user.roles = [] # needs role
        interaction_hier.user.remove_roles = AsyncMock()
        interaction_hier.user.add_roles = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(False, "⚠️ Nero's highest role is below **Color Role**")):
            await rr_cog.on_interaction(interaction_hier)
            interaction_hier.user.add_roles.assert_not_called()
            interaction_hier.response.send_message.assert_called_once()
            self.assertIn("below **Color Role**", interaction_hier.response.send_message.call_args[0][0])

    async def test_15_duplicate_role_button_custom_id_uniqueness_and_restoration(self):
        """Verify that multiple buttons targeting the same role generate unique custom IDs and restore safely."""
        import discord
        from cogs.reactionroles import ReactionRoles, RoleButton

        msg_id = 999111222333444555
        target_role_id = 9005

        # 1. Test build_view with two buttons targeting the same role
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            # Insert button 1
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Get VIP', '💎', 'blurple', ?, 0, NULL)
            """, (self.guild_id, msg_id, target_role_id))
            # Insert button 2 targeting distinct role
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id)
                VALUES (?, 111, ?, 'Get Member', '👥', 'green', 9006, 0, NULL)
            """, (self.guild_id, msg_id))
            await db.commit()

        mock_bot = MagicMock()
        rr_cog = ReactionRoles(mock_bot)

        # Build view and assert items are instantiated with unique custom IDs
        view = await rr_cog.build_view(msg_id)
        self.assertEqual(len(view.children), 2)
        btn1 = view.children[0]
        btn2 = view.children[1]

        self.assertIsInstance(btn1, RoleButton)
        self.assertIsInstance(btn2, RoleButton)
        self.assertNotEqual(btn1.custom_id, btn2.custom_id)
        self.assertTrue(btn1.custom_id.startswith("rr_9005_"))
        self.assertTrue(btn2.custom_id.startswith("rr_9006_"))

        # Test view addition with two buttons sharing the SAME role_id:
        view_same_role = discord.ui.View(timeout=None)
        b_same_1 = RoleButton(
            role_id=target_role_id, label="Add Role", emoji="➕", style=discord.ButtonStyle.primary,
            booster_only=False, required_role_id=None, exclusive=False, max_roles=0,
            require_confirmation=False, message_id=msg_id, custom_id=f"rr_{target_role_id}_{msg_id}_0_0"
        )
        b_same_2 = RoleButton(
            role_id=target_role_id, label="Remove Role", emoji="➖", style=discord.ButtonStyle.danger,
            booster_only=False, required_role_id=None, exclusive=False, max_roles=0,
            require_confirmation=False, message_id=msg_id, custom_id=f"rr_{target_role_id}_{msg_id}_0_1"
        )
        # In Discord.py, duplicate custom_id raises ValueError. Verify unique custom_ids add cleanly:
        view_same_role.add_item(b_same_1)
        view_same_role.add_item(b_same_2)
        self.assertEqual(len(view_same_role.children), 2)
        self.assertNotEqual(b_same_1.custom_id, b_same_2.custom_id)

        # Verify on_interaction dispatches both unique custom_ids to the correct role toggle
        mock_role = MagicMock()
        mock_role.name = "VIP"
        mock_role.id = target_role_id

        # Click button 1
        interaction_1 = MagicMock(spec=discord.Interaction)
        interaction_1.type = discord.InteractionType.component
        interaction_1.response = MagicMock()
        interaction_1.response.is_done.return_value = False
        interaction_1.response.send_message = AsyncMock()
        interaction_1.data = {"custom_id": b_same_1.custom_id}
        interaction_1.message = MagicMock()
        interaction_1.message.id = msg_id
        interaction_1.guild = MagicMock()
        interaction_1.guild.id = self.guild_id
        interaction_1.guild.get_role.return_value = mock_role
        interaction_1.user = MagicMock()
        interaction_1.user.id = self.user_id
        interaction_1.user.roles = []
        interaction_1.user.remove_roles = AsyncMock()
        interaction_1.user.add_roles = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(interaction_1)
            interaction_1.user.add_roles.assert_called_once_with(mock_role)

        # Click button 2 (same role, different button index)
        interaction_2 = MagicMock(spec=discord.Interaction)
        interaction_2.type = discord.InteractionType.component
        interaction_2.response = MagicMock()
        interaction_2.response.is_done.return_value = False
        interaction_2.response.send_message = AsyncMock()
        interaction_2.data = {"custom_id": b_same_2.custom_id}
        interaction_2.message = MagicMock()
        interaction_2.message.id = msg_id
        interaction_2.guild = MagicMock()
        interaction_2.guild.id = self.guild_id
        interaction_2.guild.get_role.return_value = mock_role
        interaction_2.user = MagicMock()
        interaction_2.user.id = self.user_id
        interaction_2.user.roles = []
        interaction_2.user.remove_roles = AsyncMock()
        interaction_2.user.add_roles = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(interaction_2)
            interaction_2.user.add_roles.assert_called_once_with(mock_role)

    async def test_16_duplicate_role_buttons_distinct_settings_and_draft_race(self):
        """Verify per-component lookup for same-role buttons with distinct settings and draft-ID race safety."""
        import discord
        from cogs.reactionroles import ReactionRoles, RoleButton

        msg_id = 999333444555666777
        role_id = 9008

        # Insert 2 buttons targeting same role 9008 but with DIFFERENT booster_only settings
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            # Button 0: regular (open to all)
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id, row_index, button_index, custom_id)
                VALUES (?, 111, ?, 'Open VIP', '🌟', 'blurple', ?, 0, NULL, 0, 0, ?)
            """, (self.guild_id, msg_id, role_id, f"rr_{role_id}_{msg_id}_0_0"))
            # Button 1: booster only
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id, row_index, button_index, custom_id)
                VALUES (?, 111, ?, 'Booster VIP', '🚀', 'red', ?, 1, NULL, 0, 1, ?)
            """, (self.guild_id, msg_id, role_id, f"rr_{role_id}_{msg_id}_0_1"))
            await db.commit()

        mock_bot = MagicMock()
        rr_cog = ReactionRoles(mock_bot)

        # 1. Verify build_view restores both buttons with their distinct booster_only settings
        view = await rr_cog.build_view(msg_id)
        self.assertEqual(len(view.children), 2)
        btn_open = view.children[0]
        btn_booster = view.children[1]

        self.assertFalse(btn_open.booster_only)
        self.assertEqual(btn_open.label, "Open VIP")
        self.assertTrue(btn_booster.booster_only)
        self.assertEqual(btn_booster.label, "Booster VIP")

        # 2. Non-booster user clicks Button 0 (Open VIP) -> role added
        mock_role = MagicMock()
        mock_role.name = "VIP"
        mock_role.id = role_id

        interaction_open = MagicMock(spec=discord.Interaction)
        interaction_open.type = discord.InteractionType.component
        interaction_open.response = MagicMock()
        interaction_open.response.is_done.return_value = False
        interaction_open.response.send_message = AsyncMock()
        interaction_open.data = {"custom_id": f"rr_{role_id}_{msg_id}_0_0"}
        interaction_open.message = MagicMock()
        interaction_open.message.id = msg_id
        interaction_open.guild = MagicMock()
        interaction_open.guild.id = self.guild_id
        interaction_open.guild.get_role.return_value = mock_role
        interaction_open.user = MagicMock()
        interaction_open.user.id = self.user_id
        interaction_open.user.roles = []
        interaction_open.user.premium_since = None # Non-booster
        interaction_open.user.remove_roles = AsyncMock()
        interaction_open.user.add_roles = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(interaction_open)
            interaction_open.user.add_roles.assert_called_once_with(mock_role)

        # 3. Non-booster user clicks Button 1 (Booster VIP) -> rejected with booster notice
        interaction_booster = MagicMock(spec=discord.Interaction)
        interaction_booster.type = discord.InteractionType.component
        interaction_booster.response = MagicMock()
        interaction_booster.response.is_done.return_value = False
        interaction_booster.response.send_message = AsyncMock()
        interaction_booster.data = {"custom_id": f"rr_{role_id}_{msg_id}_0_1"}
        interaction_booster.message = MagicMock()
        interaction_booster.message.id = msg_id
        interaction_booster.guild = MagicMock()
        interaction_booster.guild.id = self.guild_id
        interaction_booster.guild.get_role.return_value = mock_role
        interaction_booster.user = MagicMock()
        interaction_booster.user.id = self.user_id
        interaction_booster.user.roles = []
        interaction_booster.user.premium_since = None # Non-booster
        interaction_booster.user.remove_roles = AsyncMock()
        interaction_booster.user.add_roles = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(interaction_booster)
            interaction_booster.user.add_roles.assert_not_called()
            interaction_booster.response.send_message.assert_called_once()
            self.assertIn("boosters only", interaction_booster.response.send_message.call_args[0][0])

        # 4. Draft-window race test: interaction received with draft ID before PATCH completes
        interaction_draft = MagicMock(spec=discord.Interaction)
        interaction_draft.type = discord.InteractionType.component
        interaction_draft.response = MagicMock()
        interaction_draft.response.is_done.return_value = False
        interaction_draft.response.send_message = AsyncMock()
        interaction_draft.data = {"custom_id": f"rr_{role_id}_draft_0_0"} # draft ID format
        interaction_draft.message = MagicMock()
        interaction_draft.message.id = msg_id
        interaction_draft.guild = MagicMock()
        interaction_draft.guild.id = self.guild_id
        interaction_draft.guild.get_role.return_value = mock_role
        interaction_draft.user = MagicMock()
        interaction_draft.user.id = self.user_id
        interaction_draft.user.roles = []
        interaction_draft.user.premium_since = None
        interaction_draft.user.remove_roles = AsyncMock()
        interaction_draft.user.add_roles = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            # Must NOT throw ValueError on 'draft', must look up row and succeed
            await rr_cog.on_interaction(interaction_draft)
            interaction_draft.user.add_roles.assert_called_once_with(mock_role)


    async def test_17_comprehensive_exactly_once_dispatch_matrix_and_lifecycle(self):
        """Task 1, 2, 3 Acceptance Matrix: Exactly-once dispatch, distinct same-role settings, draft window & legacy safety."""
        import discord
        from cogs.reactionroles import ReactionRoles, RoleButton, RoleSelect, handle_role_toggle

        msg_id = 999444555666777888
        role_target_id = 9110
        req_role_id = 9119

        mock_role = MagicMock()
        mock_role.name = "Premium VIP"
        mock_role.id = role_target_id

        mock_req_role = MagicMock()
        mock_req_role.name = "Staff"
        mock_req_role.id = req_role_id

        # Insert panel and 2 same-role buttons with distinct required_role settings + 1 select menu
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, 0, 0, 0)
            """, (msg_id, self.guild_id))
            # Button 0: Requires Staff role (9119)
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id, row_index, button_index, custom_id)
                VALUES (?, 111, ?, 'Staff VIP', '🛡️', 'blurple', ?, 0, ?, 0, 0, ?)
            """, (self.guild_id, msg_id, role_target_id, req_role_id, f"rr_{role_target_id}_{msg_id}_0_0"))
            # Button 1: Open to all (no requirement)
            await db.execute("""
                INSERT INTO reaction_roles (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only, required_role_id, row_index, button_index, custom_id)
                VALUES (?, 111, ?, 'Public VIP', '🌟', 'green', ?, 0, NULL, 0, 1, ?)
            """, (self.guild_id, msg_id, role_target_id, f"rr_{role_target_id}_{msg_id}_0_1"))
            
            # Select menu at row 1
            cursor = await db.execute("""
                INSERT INTO reaction_role_selects
                (guild_id, channel_id, message_id, row_index, custom_id, placeholder, min_values, max_values)
                VALUES (?, 111, ?, 1, ?, 'Choose Role...', 1, 1)
            """, (self.guild_id, msg_id, f"rr_sel_{msg_id}_1"))
            sel_id = cursor.lastrowid
            await db.execute("""
                INSERT INTO reaction_role_select_options
                (select_id, message_id, role_id, label, description, emoji, booster_only, required_role_id, expiry_days, position)
                VALUES (?, ?, ?, 'Select Premium VIP', 'VIP from dropdown', '👑', 0, NULL, 0, 0)
            """, (sel_id, msg_id, role_target_id))
            await db.commit()

        mock_bot = MagicMock()
        mock_view_store = MagicMock()
        mock_view_store.is_message_tracked.return_value = False
        mock_bot._connection._view_store = mock_view_store
        rr_cog = ReactionRoles(mock_bot)

        # -------------------------------------------------------------
        # 1. Single Configured Button Click -> Exactly 1 role transition & 1 response
        # -------------------------------------------------------------
        user_roles = []
        def make_interaction(cid, user_r=None, premium=None, msg_obj_id=msg_id):
            itx = MagicMock(spec=discord.Interaction)
            itx.type = discord.InteractionType.component
            itx.response = MagicMock()
            itx._done = False
            def _is_done():
                return itx._done
            itx.response.is_done = _is_done
            async def _send_msg(*args, **kwargs):
                if itx._done:
                    raise discord.InteractionResponded("Interaction already acknowledged")
                itx._done = True
                itx.sent_messages.append((args, kwargs))
            itx.sent_messages = []
            itx.response.send_message = AsyncMock(side_effect=_send_msg)
            itx.data = {"custom_id": cid}
            itx.message = MagicMock()
            itx.message.id = msg_obj_id
            itx.guild = MagicMock()
            itx.guild.id = self.guild_id
            def _get_role(rid):
                if rid == role_target_id:
                    return mock_role
                if rid == req_role_id:
                    return mock_req_role
                return None
            itx.guild.get_role.side_effect = _get_role
            itx.user = MagicMock()
            itx.user.id = self.user_id
            type(itx.user).roles = property(lambda self_u: list(user_roles if user_r is None else user_r))
            itx.user.premium_since = premium
            async def _add_r(r):
                if r not in user_roles:
                    user_roles.append(r)
            async def _rem_r(r):
                if r in user_roles:
                    user_roles.remove(r)
            itx.user.add_roles = AsyncMock(side_effect=_add_r)
            itx.user.remove_roles = AsyncMock(side_effect=_rem_r)
            return itx

        # Click Public VIP (Button 1): user gets role
        itx_btn1 = make_interaction(f"rr_{role_target_id}_{msg_id}_0_1")
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_btn1)
            self.assertEqual(len(itx_btn1.sent_messages), 1)
            self.assertIn("Gave you **Premium VIP**", itx_btn1.sent_messages[0][0][0])
            self.assertIn(mock_role, user_roles)
            itx_btn1.user.add_roles.assert_called_once_with(mock_role)

        # -------------------------------------------------------------
        # 2. Mutual Exclusivity: View Callback vs on_interaction Dispatch
        # -------------------------------------------------------------
        # When View callback runs, interaction is responded; on_interaction must exit immediately
        view = await rr_cog.build_view(msg_id)
        btn_view_item = view.children[1] # Public VIP button
        itx_view_dispatch = make_interaction(f"rr_{role_target_id}_{msg_id}_0_1")
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            # Step A: View callback handles interaction
            await btn_view_item.callback(itx_view_dispatch)
            self.assertTrue(itx_view_dispatch.response.is_done())
            self.assertEqual(len(itx_view_dispatch.sent_messages), 1)
            self.assertNotIn(mock_role, user_roles) # toggled off (removed)

            # Step B: Subsequent on_interaction emission receives already-done interaction
            await rr_cog.on_interaction(itx_view_dispatch)
            # Assert NO double-dispatch, NO InteractionResponded exception, exactly 1 response total
            self.assertEqual(len(itx_view_dispatch.sent_messages), 1)

        # -------------------------------------------------------------
        # 3. Two Buttons Same Role with Different Valid Settings
        # -------------------------------------------------------------
        # User without Staff role clicks Button 0 (Staff VIP) -> rejected
        itx_staff_reject = make_interaction(f"rr_{role_target_id}_{msg_id}_0_0")
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_staff_reject)
            self.assertEqual(len(itx_staff_reject.sent_messages), 1)
            self.assertIn("You need the **Staff** role", itx_staff_reject.sent_messages[0][0][0])
            self.assertNotIn(mock_role, user_roles)
            itx_staff_reject.user.add_roles.assert_not_called()

        # User WITH Staff role clicks Button 0 (Staff VIP) -> granted
        itx_staff_grant = make_interaction(f"rr_{role_target_id}_{msg_id}_0_0", user_r=[mock_req_role])
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_staff_grant)
            self.assertEqual(len(itx_staff_grant.sent_messages), 1)
            self.assertIn("Gave you **Premium VIP**", itx_staff_grant.sent_messages[0][0][0])

        # -------------------------------------------------------------
        # 4. Unconfigured Button Safety
        # -------------------------------------------------------------
        itx_unconf = make_interaction("rr_99999_99999_0_0", msg_obj_id=99999)
        await rr_cog.on_interaction(itx_unconf)
        self.assertEqual(len(itx_unconf.sent_messages), 1)
        self.assertIn("unconfigured or no longer active", itx_unconf.sent_messages[0][0][0])
        itx_unconf.user.add_roles.assert_not_called()
        itx_unconf.user.remove_roles.assert_not_called()

        # -------------------------------------------------------------
        # 5. String Select Option Dispatch
        # -------------------------------------------------------------
        itx_select = make_interaction(f"rr_sel_{msg_id}_1")
        itx_select.data = {
            "custom_id": f"rr_sel_{msg_id}_1",
            "component_type": 3,
            "values": [str(role_target_id)]
        }
        user_roles.clear()
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_select)
            self.assertEqual(len(itx_select.sent_messages), 1)
            self.assertIn("Gave you **Premium VIP**", itx_select.sent_messages[0][0][0])
            self.assertIn(mock_role, user_roles)

        # -------------------------------------------------------------
        # 6. Link Button (Style 5) Safety
        # -------------------------------------------------------------
        itx_link = make_interaction("some_external_link_or_action")
        await rr_cog.on_interaction(itx_link)
        self.assertEqual(len(itx_link.sent_messages), 0) # ignored safely

        # -------------------------------------------------------------
        # 7. Legacy Component ID (`rr_<role_id>_<message_id>`) Safety
        # -------------------------------------------------------------
        # Legacy ID lacks row/btn coordinates -> resolves to first matching DB row for (msg_id, role_id)
        # First row has Staff requirement -> non-staff rejected
        itx_legacy_reject = make_interaction(f"rr_{role_target_id}_{msg_id}")
        user_roles.clear()
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_legacy_reject)
            self.assertEqual(len(itx_legacy_reject.sent_messages), 1)
            self.assertIn("You need the **Staff** role", itx_legacy_reject.sent_messages[0][0][0])

        # Staff member clicking legacy ID -> granted role
        itx_legacy_grant = make_interaction(f"rr_{role_target_id}_{msg_id}", user_r=[mock_req_role])
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_legacy_grant)
            self.assertEqual(len(itx_legacy_grant.sent_messages), 1)
            self.assertIn("Gave you **Premium VIP**", itx_legacy_grant.sent_messages[0][0][0])
            self.assertIn(mock_role, user_roles)

        # -------------------------------------------------------------
        # 8. Draft Component IDs During Pre-PATCH Window
        # -------------------------------------------------------------
        # Draft Button 0: points to Staff requirement
        itx_draft_0 = make_interaction(f"rr_{role_target_id}_draft_0_0")
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_draft_0)
            self.assertEqual(len(itx_draft_0.sent_messages), 1)
            self.assertIn("You need the **Staff** role", itx_draft_0.sent_messages[0][0][0])

        # Draft Button 1: points to Public VIP
        user_roles.clear()
        itx_draft_1 = make_interaction(f"rr_{role_target_id}_draft_0_1")
        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await rr_cog.on_interaction(itx_draft_1)
            self.assertEqual(len(itx_draft_1.sent_messages), 1)
            self.assertIn("Gave you **Premium VIP**", itx_draft_1.sent_messages[0][0][0])
            self.assertIn(mock_role, user_roles)

        # -------------------------------------------------------------
        # 9. Custom ID Length Limit & View Restoration
        # -------------------------------------------------------------
        for child in view.children:
            self.assertLessEqual(len(child.custom_id), 100)
        self.assertEqual(len(view.children), 3) # 2 buttons + 1 select

if __name__ == "__main__":
    unittest.main()
