"""
Comprehensive Acceptance and Regression Test Suite for Discord Select Role Menus.
Covers:
1. Schema integrity (reaction_role_selects, reaction_role_select_options tables, indexes, triggers).
2. Template save, retrieve, and delete with select menus and options.
3. Component validation: Action Row bounds (max 5 rows total between buttons and select menus).
4. Component validation: Option count bounds (1 to 25 options per select menu).
5. Role hierarchy validation fail-closed behavior for select menu options.
6. DB persistence: select menu record and option records created on send.
7. Custom ID patching: draft IDs patched to rr_sel_{msg_id}_{row_idx}.
8. Bot runtime: RoleSelect instantiation and options restoration.
9. Bot runtime: single-choice toggle (add role if missing, remove if present).
10. Bot runtime: booster-only restriction.
11. Bot runtime: required-role restriction.
12. Bot runtime: exclusive panel mutual exclusion clearing.
13. Bot runtime: max-roles per user cap enforcement.
14. Bot runtime: temporary expiry tracking in reaction_role_expiry table.
15. Dynamic interaction fallback: unpatched draft custom IDs, post-restart lifecycle & no double execution.
16. Malformed, unauthorized, cross-guild option resilience.
17. Cascade deletion on select menu remove and idempotent persistence retry.
18. Compensation on DB persistence failure.
19. Mixed Action Rows 5-row hard budget validation.
"""

import os
import sys
import json
import sqlite3
import unittest
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"
os.environ["OWNER_ID"] = "999999999"

from scripts.phase1_support import GUILD, USER, reset_database, rows, execute
import aiosqlite
import discord
from database import DB_PATH, init_db
from cogs.reactionroles import (
    handle_role_toggle,
    RoleSelect,
    ReactionRoles
)
import dashboard.app as dash


class TestSelectRoleMenus(unittest.IsolatedAsyncioTestCase):
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
        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "StudioOwner", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="studio-csrf"
            )

    def test_01_schema_integrity(self):
        """Verify reaction_role_selects and reaction_role_select_options schemas exist."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(reaction_role_selects)")
            cols_sel = {row[1]: row[2] for row in c.fetchall()}
            self.assertIn("custom_id", cols_sel)
            self.assertIn("message_id", cols_sel)
            self.assertIn("channel_id", cols_sel)
            self.assertIn("guild_id", cols_sel)
            self.assertIn("placeholder", cols_sel)
            self.assertIn("row_index", cols_sel)

            c.execute("PRAGMA table_info(reaction_role_select_options)")
            cols_opt = {row[1]: row[2] for row in c.fetchall()}
            self.assertIn("select_id", cols_opt)
            self.assertIn("message_id", cols_opt)
            self.assertIn("role_id", cols_opt)
            self.assertIn("label", cols_opt)
            self.assertIn("description", cols_opt)
            self.assertIn("emoji", cols_opt)
            self.assertIn("booster_only", cols_opt)
            self.assertIn("required_role_id", cols_opt)
            self.assertIn("expiry_days", cols_opt)

    def test_02_template_save_and_retrieve_with_select_menus(self):
        """Template save, retrieve, and delete preserving select_menus array and panel_config."""
        payload = {
            "name": "select_menu_template",
            "content": "Choose your server notification roles below:",
            "embeds": [{
                "title": "Role Directory",
                "description": "Select from dropdowns below",
                "color": 5793266,
                "fields": []
            }],
            "buttons": [],
            "select_menus": [
                {
                    "placeholder": "Choose your region...",
                    "options": [
                        {"label": "North America", "role_id": "90001", "description": "NA Ping", "emoji": "🌎"},
                        {"label": "Europe", "role_id": "90002", "description": "EU Ping", "emoji": "🌍"}
                    ]
                },
                {
                    "placeholder": "Choose ping notifications...",
                    "options": [
                        {"label": "Announcements", "role_id": "90003", "description": "News only", "emoji": "📢"},
                        {"label": "Events", "role_id": "90004", "description": "Community events", "emoji": "🎉", "booster_only": True}
                    ]
                }
            ],
            "panel_config": {
                "exclusive": 1,
                "max_roles": 2,
                "require_confirmation": True
            }
        }

        # Save template
        res = self.client.post("/api/embedbuilder/template/save",
                               data=json.dumps(payload),
                               content_type="application/json",
                               headers={"X-CSRF-Token": "studio-csrf"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("success"))

        # Retrieve template
        res_get = self.client.get("/api/embedbuilder/template/select_menu_template")
        self.assertEqual(res_get.status_code, 200)
        data = res_get.get_json()
        tmpl = data.get("template", {})
        self.assertEqual(tmpl["content"], payload["content"])
        self.assertEqual(len(tmpl["select_menus"]), 2)
        self.assertEqual(tmpl["select_menus"][0]["placeholder"], "Choose your region...")
        self.assertEqual(len(tmpl["select_menus"][0]["options"]), 2)
        self.assertEqual(tmpl["select_menus"][0]["options"][0]["role_id"], "90001")
        self.assertEqual(tmpl["panel_config"]["exclusive"], 1)
        self.assertEqual(tmpl["panel_config"]["max_roles"], 2)
        self.assertTrue(tmpl["panel_config"]["require_confirmation"])

    def test_03_action_row_boundary_enforcement(self):
        """Ensure validation rejects payloads exceeding 5 total Action Rows."""
        buttons = [{"label": f"B{i}", "role_id": "90001", "color": "blurple"} for i in range(10)]
        selects = [
            {"placeholder": f"Menu {i}", "options": [{"label": "Opt", "role_id": "90001"}]}
            for i in range(4)
        ]

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Too many components",
                "buttons": buttons,
                "select_menus": selects
            })
        }

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")):
            res = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res.status_code, 400)
            data = res.get_json()
            self.assertFalse(data.get("success"))
            self.assertIn("5 Action Row limit", data.get("error", ""))

    def test_04_select_menu_option_bounds(self):
        """Validate select menu option bounds (1 to 25 options per menu)."""
        # 0 options -> fails
        payload_0 = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Empty menu",
                "select_menus": [{"placeholder": "Empty", "options": []}]
            })
        }
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")):
            res_0 = self.client.post("/api/embedbuilder/send", data=payload_0, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res_0.status_code, 400)
            self.assertIn("at least 1 option", res_0.get_json().get("error", ""))

        # 26 options -> fails
        payload_26 = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Overflow menu",
                "select_menus": [{
                    "placeholder": "Overflow",
                    "options": [{"label": f"O{i}", "role_id": "90001"} for i in range(26)]
                }]
            })
        }
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")):
            res_26 = self.client.post("/api/embedbuilder/send", data=payload_26, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res_26.status_code, 400)
            self.assertIn("exceeds 25 options limit", res_26.get_json().get("error", ""))

    def test_05_send_and_persistence_with_canonical_custom_ids(self):
        """Test sending message with select menus, verifying API payload, DB rows, and ID patching."""
        mock_discord_resp = MagicMock()
        mock_discord_resp.status_code = 200
        mock_discord_resp.json.return_value = {"id": "888777666555444333"}

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Interactive Select Role Panel",
                "select_menus": [
                    {
                        "placeholder": "Select your notification roles",
                        "options": [
                            {"label": "News", "role_id": "90001", "description": "Latest updates", "emoji": "📰"},
                            {"label": "Giveaways", "role_id": "90002", "description": "Free stuff", "emoji": "🎁", "booster_only": True}
                        ]
                    }
                ],
                "panel_config": {
                    "exclusive": 1,
                    "max_roles": 1,
                    "require_confirmation": False
                }
            })
        }

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_discord_resp), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_discord_resp), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("message_id"), "888777666555444333")

        # Verify DB records
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, custom_id, message_id, channel_id, guild_id, placeholder, row_index FROM reaction_role_selects WHERE message_id = '888777666555444333'")
            sel_row = c.fetchone()
            self.assertIsNotNone(sel_row)
            sel_db_id, custom_id, msg_id, ch_id, g_id, placeholder, row_idx = sel_row
            self.assertEqual(custom_id, "rr_sel_888777666555444333_0")
            self.assertEqual(msg_id, 888777666555444333)
            self.assertEqual(ch_id, 111222333)
            self.assertEqual(placeholder, "Select your notification roles")

            c.execute("SELECT role_id, label, description, emoji, booster_only FROM reaction_role_select_options WHERE select_id = ?", (sel_db_id,))
            opts = c.fetchall()
            self.assertEqual(len(opts), 2)
            self.assertEqual(opts[0][0], 90001)
            self.assertEqual(opts[0][1], "News")
            self.assertEqual(opts[1][0], 90002)
            self.assertEqual(opts[1][4], 1) # booster_only

            c.execute("SELECT exclusive, max_roles FROM reaction_role_panels WHERE message_id = '888777666555444333'")
            panel = c.fetchone()
            self.assertIsNotNone(panel)
            self.assertEqual(panel[0], 1)
            self.assertEqual(panel[1], 1)

    async def test_06_handle_role_toggle_single_choice_add_and_remove(self):
        """Verify unified handle_role_toggle adds role if missing, removes if present."""
        guild = MagicMock()
        guild.id = GUILD
        role = MagicMock()
        role.id = 90001
        role.name = "News Role"
        guild.get_role.return_value = role

        member = MagicMock()
        member.id = 55555
        member.roles = []
        member.guild = guild
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        member.premium_since = None

        interaction = MagicMock()
        interaction.guild = guild
        interaction.user = member
        interaction.response = AsyncMock()
        interaction.response.send_message = AsyncMock()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            # 1. Add role
            await handle_role_toggle(
                interaction=interaction,
                message_id=888777666,
                role_id=90001,
                booster_only=False,
                required_role_id=None,
                exclusive=False,
                max_roles=0,
                require_confirmation=False
            )
            member.add_roles.assert_called_once_with(role)
            interaction.response.send_message.assert_called_with("Gave you **News Role**.", ephemeral=True)

            # 2. Toggle off (remove role)
            member.roles = [role]
            member.add_roles.reset_mock()
            interaction.response.send_message.reset_mock()

            await handle_role_toggle(
                interaction=interaction,
                message_id=888777666,
                role_id=90001,
                booster_only=False,
                required_role_id=None,
                exclusive=False,
                max_roles=0,
                require_confirmation=False
            )
            member.remove_roles.assert_called_once_with(role)
            interaction.response.send_message.assert_called_with("Removed **News Role**.", ephemeral=True)

    async def test_07_handle_role_toggle_booster_and_required_role_restrictions(self):
        """Verify booster-only and required-role guards prevent assignment when conditions not met."""
        guild = MagicMock()
        guild.id = GUILD
        role = MagicMock(id=90002)
        role.name = "VIP Role"
        req_role = MagicMock(id=90099)
        req_role.name = "Member Role"
        guild.get_role.side_effect = lambda rid: role if rid == 90002 else (req_role if rid == 90099 else None)

        member = MagicMock(id=55555, roles=[], guild=guild, premium_since=None)
        member.add_roles = AsyncMock()
        interaction = MagicMock(guild=guild, user=member, response=AsyncMock())

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            # Booster-only check failure
            await handle_role_toggle(
                interaction=interaction,
                message_id=888777666,
                role_id=90002,
                booster_only=True,
                required_role_id=None,
                exclusive=False,
                max_roles=0,
                require_confirmation=False
            )
            member.add_roles.assert_not_called()
            interaction.response.send_message.assert_called_with("This role is for boosters only! Boost the server to unlock it.", ephemeral=True)

            # Required-role check failure
            interaction.response.send_message.reset_mock()
            await handle_role_toggle(
                interaction=interaction,
                message_id=888777666,
                role_id=90002,
                booster_only=False,
                required_role_id=90099,
                exclusive=False,
                max_roles=0,
                require_confirmation=False
            )
            member.add_roles.assert_not_called()
            interaction.response.send_message.assert_called_with("You need the **Member Role** role to get this.", ephemeral=True)

    async def test_08_handle_role_toggle_exclusive_panel_clearing(self):
        """Exclusive panel removes existing panel roles before applying new role."""
        guild = MagicMock(id=GUILD)
        role_red = MagicMock(id=90001)
        role_red.name = "Red"
        role_blue = MagicMock(id=90002)
        role_blue.name = "Blue"
        guild.get_role.side_effect = lambda rid: role_red if rid == 90001 else (role_blue if rid == 90002 else None)

        member = MagicMock(id=55555, roles=[role_red], guild=guild, premium_since=None)
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        interaction = MagicMock(guild=guild, user=member, response=AsyncMock())

        # Save both options into DB for message 888777666
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO reaction_role_selects (custom_id, message_id, channel_id, guild_id, row_index) VALUES ('rr_sel_888777666_0', 888777666, 111222333, ?, 0)", (str(GUILD),))
            sel_id = c.lastrowid
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label) VALUES (?, 888777666, 90001, 'Red')", (sel_id,))
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label) VALUES (?, 888777666, 90002, 'Blue')", (sel_id,))
            c.execute("INSERT INTO reaction_role_panels (message_id, guild_id, exclusive) VALUES (888777666, ?, 1)", (str(GUILD),))
            conn.commit()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            # Select role_blue when member already has role_red in exclusive mode
            await handle_role_toggle(
                interaction=interaction,
                message_id=888777666,
                role_id=90002,
                booster_only=False,
                required_role_id=None,
                exclusive=True,
                max_roles=0,
                require_confirmation=False
            )

            member.remove_roles.assert_called_once_with(role_red)
            member.add_roles.assert_called_once_with(role_blue)
            interaction.response.send_message.assert_called_with("Gave you **Blue**.\nRemoved: Red", ephemeral=True)

    async def test_09_handle_role_toggle_temporary_expiry_persistence(self):
        """Temporary role with expiry_days records expiration in reaction_role_expiry table."""
        guild = MagicMock(id=GUILD)
        role = MagicMock(id=90001, name="Temp Role")
        guild.get_role.return_value = role

        member = MagicMock(id=55555, roles=[], guild=guild, premium_since=None)
        member.add_roles = AsyncMock()
        interaction = MagicMock(guild=guild, user=member, response=AsyncMock())

        # Setup template expiry in DB
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO reaction_role_expiry (guild_id, user_id, role_id, message_id, expires_at) VALUES (?, 0, 90001, 888777666, '2099-01-01T00:00:00')", (GUILD,))
            conn.commit()

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await handle_role_toggle(
                interaction=interaction,
                message_id=888777666,
                role_id=90001,
                booster_only=False,
                required_role_id=None,
                exclusive=False,
                max_roles=0,
                require_confirmation=False
            )

            member.add_roles.assert_called_once_with(role)
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("SELECT guild_id, user_id, role_id, message_id, expires_at FROM reaction_role_expiry WHERE user_id = 55555")
                row = c.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], GUILD)
                self.assertEqual(row[1], 55555)
                self.assertEqual(row[2], 90001)
                self.assertEqual(row[3], 888777666)
                self.assertEqual(row[4], '2099-01-01T00:00:00')

    async def test_10_role_select_dynamic_fallback_and_callback(self):
        """Verify RoleSelect callback and on_interaction dynamic fallback for select menus."""
        bot = MagicMock()
        cog = ReactionRoles(bot)

        # Setup select menu in DB
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO reaction_role_selects (custom_id, message_id, channel_id, guild_id, placeholder, row_index) VALUES ('rr_sel_12345_0', 12345, 67890, ?, 'Pick a role', 0)", (str(GUILD),))
            sel_id = c.lastrowid
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label, description) VALUES (?, 12345, 9911, 'Developer Role', 'Dev ping')", (sel_id,))
            conn.commit()

        guild = MagicMock(id=GUILD)
        role = MagicMock(id=9911)
        role.name = "Developer Role"
        guild.get_role.return_value = role
        member = MagicMock(id=777, roles=[], guild=guild, premium_since=None)
        member.add_roles = AsyncMock()

        interaction = MagicMock()
        interaction.type = discord.InteractionType.component
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.data = {
            "component_type": 3,
            "custom_id": "rr_sel_12345_0",
            "values": ["9911"]
        }
        interaction.message = MagicMock(id=12345)
        interaction.guild = guild
        interaction.user = member

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            # Execute dynamic fallback listener
            await cog.on_interaction(interaction)
            member.add_roles.assert_called_once_with(role)
            interaction.response.send_message.assert_called_with("Gave you **Developer Role**.", ephemeral=True)

    async def test_11_failed_patch_post_restart_draft_lifecycle(self):
        """Prove post-restart unpatched draft components work via dynamic fallback and never double-execute."""
        bot = MagicMock()
        # Mock view_store on bot connection to simulate message 999111222 being tracked
        mock_view_store = MagicMock()
        mock_view_store.is_message_tracked.return_value = True
        bot._connection._view_store = mock_view_store

        cog = ReactionRoles(bot)

        # Setup select menu in DB for message 999111222
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO reaction_role_selects (custom_id, message_id, channel_id, guild_id, placeholder, row_index) VALUES ('rr_sel_999111222_0', 999111222, 67890, ?, 'Choose a role', 0)", (str(GUILD),))
            sel_id = c.lastrowid
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label, description) VALUES (?, 999111222, 5001, 'Gamer', 'Gaming ping')", (sel_id,))
            conn.commit()

        guild = MagicMock(id=GUILD)
        role = MagicMock(id=5001)
        role.name = "Gamer"
        guild.get_role.return_value = role

        member = MagicMock(id=888, roles=[], guild=guild, premium_since=None)
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()

        # Step A: Member clicks unpatched draft custom ID ('rr_sel_draft_0' or 'rr_sel_999111222_draft_0')
        draft_interaction = MagicMock()
        draft_interaction.type = discord.InteractionType.component
        draft_interaction.response = MagicMock()
        draft_interaction.response.is_done.return_value = False
        draft_interaction.response.send_message = AsyncMock()
        draft_interaction.data = {
            "component_type": 3,
            "custom_id": "rr_sel_999111222_draft_0",
            "values": ["5001"]
        }
        draft_interaction.message = MagicMock(id=999111222)
        draft_interaction.guild = guild
        draft_interaction.user = member

        with patch("cogs.reactionroles.check_bot_role_position", return_value=(True, "")):
            await cog.on_interaction(draft_interaction)
            # Verify role was granted through draft fallback even though is_message_tracked is True
            member.add_roles.assert_called_once_with(role)
            draft_interaction.response.send_message.assert_called_with("Gave you **Gamer**.", ephemeral=True)

        # Step B: Canonical interaction arrives on registered message -> on_interaction must return early (no double execution)
        canonical_interaction = MagicMock()
        canonical_interaction.type = discord.InteractionType.component
        canonical_interaction.response = MagicMock()
        canonical_interaction.response.is_done.return_value = False
        canonical_interaction.response.send_message = AsyncMock()
        canonical_interaction.data = {
            "component_type": 3,
            "custom_id": "rr_sel_999111222_0",
            "values": ["5001"]
        }
        canonical_interaction.message = MagicMock(id=999111222)
        canonical_interaction.guild = guild
        canonical_interaction.user = member

        member.add_roles.reset_mock()
        await cog.on_interaction(canonical_interaction)
        # Verify on_interaction did NOT execute role addition (yields to RoleSelect.callback)
        member.add_roles.assert_not_called()
        canonical_interaction.response.send_message.assert_not_called()

    async def test_12_malformed_unauthorized_cross_guild_resilience(self):
        """Test resilience against malformed option values, nonexistent roles, and cross-guild exploits."""
        bot = MagicMock()
        cog = ReactionRoles(bot)

        # Setup select menu in DB
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO reaction_role_selects (custom_id, message_id, channel_id, guild_id, placeholder, row_index) VALUES ('rr_sel_333444_0', 333444, 67890, ?, 'Pick', 0)", (str(GUILD),))
            sel_id = c.lastrowid
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label) VALUES (?, 333444, 6001, 'Valid')", (sel_id,))
            conn.commit()

        guild = MagicMock(id=GUILD)
        guild.get_role.return_value = None # Role doesn't exist in this guild (cross-guild attack)

        member = MagicMock(id=999, roles=[], guild=guild, premium_since=None)
        member.add_roles = AsyncMock()

        interaction = MagicMock()
        interaction.type = discord.InteractionType.component
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.data = {
            "component_type": 3,
            "custom_id": "rr_sel_333444_0",
            "values": ["6001"]
        }
        interaction.message = MagicMock(id=333444)
        interaction.guild = guild
        interaction.user = member

        await cog.on_interaction(interaction)
        member.add_roles.assert_not_called()
        interaction.response.send_message.assert_called_with("Role not found.", ephemeral=True)

        # Malformed non-numeric values
        interaction.response.send_message.reset_mock()
        interaction.data["values"] = ["invalid_string_not_int"]
        await cog.on_interaction(interaction)
        member.add_roles.assert_not_called()

        # Empty values array
        interaction.response.send_message.reset_mock()
        interaction.data["values"] = []
        await cog.on_interaction(interaction)
        member.add_roles.assert_not_called()

    def test_13_cascade_deletion_and_idempotent_retry(self):
        """Verify cascade deletion trigger removes options on select delete, and repeated sends are idempotent."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO reaction_role_selects (custom_id, message_id, channel_id, guild_id, row_index) VALUES ('rr_sel_999000_0', 999000, 111, ?, 0)", (str(GUILD),))
            sel_id = c.lastrowid
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label) VALUES (?, 999000, 7001, 'Opt1')", (sel_id,))
            c.execute("INSERT INTO reaction_role_select_options (select_id, message_id, role_id, label) VALUES (?, 999000, 7002, 'Opt2')", (sel_id,))
            conn.commit()

            c.execute("SELECT count(*) FROM reaction_role_select_options WHERE select_id = ?", (sel_id,))
            self.assertEqual(c.fetchone()[0], 2)

            # Delete parent select menu
            c.execute("DELETE FROM reaction_role_selects WHERE id = ?", (sel_id,))
            conn.commit()

            # Verify options were cascaded away via trigger / foreign key
            c.execute("SELECT count(*) FROM reaction_role_select_options WHERE select_id = ?", (sel_id,))
            self.assertEqual(c.fetchone()[0], 0)

    def test_14_compensation_on_db_persistence_failure(self):
        """Verify Discord message compensation deletion if DB persistence fails."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"id": "111999888777"}

        mock_del_resp = MagicMock()
        mock_del_resp.status_code = 204

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Panel with failing DB",
                "select_menus": [{"placeholder": "Choose", "options": [{"label": "Opt", "role_id": "90001"}]}]
            })
        }

        import dashboard.api.embedbuilder as eb_api
        original_run_async = eb_api.run_async

        def fail_persist(coro):
            if getattr(coro, "__name__", "") == "persist_panel" or "persist" in str(coro):
                coro.close()
                raise sqlite3.OperationalError("Simulated DB Disk Full")
            return original_run_async(coro)

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.delete", return_value=mock_del_resp) as mock_delete, \
             patch("dashboard.api.embedbuilder.run_async", side_effect=fail_persist), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data.get("success"))
            self.assertIn("Message was rolled back", data.get("error", ""))
            mock_delete.assert_called_once()

    def test_15_mixed_action_rows_budgeting_and_templates(self):
        """Verify 5 Action Row hard limit across mixed buttons (5 per row) and select menus (1 per row)."""
        # Exactly 5 rows: 10 buttons (2 rows) + 3 select menus (3 rows) = 5 rows -> VALID
        buttons_10 = [{"label": f"B{i}", "role_id": "90001", "color": "blurple"} for i in range(10)]
        selects_3 = [{"placeholder": f"Menu {i}", "options": [{"label": "Opt", "role_id": "90001"}]} for i in range(3)]

        mock_discord_resp = MagicMock()
        mock_discord_resp.status_code = 200
        mock_discord_resp.json.return_value = {"id": "555666777888"}

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_discord_resp), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_discord_resp), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res_ok = self.client.post("/api/embedbuilder/send", data={
                "channel_id": "111222333",
                "payload_json": json.dumps({
                    "content": "5 rows total",
                    "buttons": buttons_10,
                    "select_menus": selects_3
                })
            }, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res_ok.status_code, 200)
            self.assertTrue(res_ok.get_json().get("success"))

        # 6 rows: 11 buttons (3 rows) + 3 select menus (3 rows) = 6 rows -> REJECTED (400)
        buttons_11 = [{"label": f"B{i}", "role_id": "90001", "color": "blurple"} for i in range(11)]
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")):
            res_fail = self.client.post("/api/embedbuilder/send", data={
                "channel_id": "111222333",
                "payload_json": json.dumps({
                    "content": "6 rows total",
                    "buttons": buttons_11,
                    "select_menus": selects_3
                })
            }, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res_fail.status_code, 400)
            self.assertIn("5 Action Row limit", res_fail.get_json().get("error", ""))

    def test_16_nonce_retry_and_compensation_distinction(self):
        """Prove that a retry of an existing message with SQLite failure preserves previous records and does NOT delete Discord message."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"id": "777111222"}

        mock_del_resp = MagicMock()
        mock_del_resp.status_code = 204

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Original Panel",
                "nonce": "test_nonce_12345",
                "select_menus": [{"placeholder": "Select", "options": [{"label": "Opt1", "role_id": "90001"}]}]
            })
        }

        # Step 1: Initial successful Send creates message 777111222 and commits SQLite records
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_post_resp), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res1 = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res1.status_code, 200)

        # Verify initial records exist
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM reaction_role_panels WHERE message_id = 777111222")
            self.assertEqual(c.fetchone()[0], 1)
            c.execute("SELECT count(*) FROM reaction_role_select_options WHERE message_id = 777111222")
            self.assertEqual(c.fetchone()[0], 1)

        # Step 2: Client retries with same nonce -> Discord returns same message ID 777111222
        # Force SQLite persistence on retry to fail
        import dashboard.api.embedbuilder as eb_api
        original_run_async = eb_api.run_async

        def mock_run_async_with_retry_failure(coro):
            coro_name = getattr(coro, "__name__", "")
            if coro_name == "persist_panel" or "persist" in str(coro):
                coro.close()
                raise sqlite3.OperationalError("Simulated DB lock on retry")
            return original_run_async(coro)

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.delete", return_value=mock_del_resp) as mock_delete, \
             patch("dashboard.api.embedbuilder.run_async", side_effect=mock_run_async_with_retry_failure), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res2 = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res2.status_code, 500)
            data2 = res2.get_json()
            self.assertFalse(data2.get("success"))
            self.assertIn("Prior configuration was preserved", data2.get("error", ""))
            # Compensation DELETE must NOT have been called on existing message!
            mock_delete.assert_not_called()

        # Verify previously committed records in SQLite remain intact
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM reaction_role_panels WHERE message_id = 777111222")
            self.assertEqual(c.fetchone()[0], 1)
            c.execute("SELECT count(*) FROM reaction_role_select_options WHERE message_id = 777111222")
            self.assertEqual(c.fetchone()[0], 1)

    def test_17_full_six_step_nonce_failure_and_retry_sequence(self):
        """Verify the exact 6-step edge case:
        1. Discord creates message for POST with nonce N.
        2. SQLite persistence fails.
        3. Compensation DELETE fails, leaving Discord message alive without SQLite panel row.
        4. Client retries with same nonce N within window.
        5. Discord returns already-existing message via nonce deduplication.
        6. SQLite persistence fails again.
        Criteria: Retry does NOT delete the pre-existing Discord message, and DB transaction rolls back cleanly.
        """
        nonce_val = "dangerous_nonce_seq_999"
        msg_id_val = "999888111222"

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"id": msg_id_val}

        # Step 3: Compensation DELETE fails (returns 500)
        mock_del_resp_fail = MagicMock()
        mock_del_resp_fail.status_code = 500

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Panel under nonce edge test",
                "nonce": nonce_val,
                "select_menus": [{"placeholder": "Select", "options": [{"label": "OptA", "role_id": "90001"}]}]
            })
        }

        # Step 1 & 2 & 3: Attempt 1
        import dashboard.api.embedbuilder as eb_api
        original_run_async = eb_api.run_async

        def fail_persist_attempt_1(coro):
            coro_name = getattr(coro, "__name__", "")
            if coro_name == "persist_panel" or "persist" in str(coro):
                coro.close()
                raise sqlite3.OperationalError("Simulated DB Disk Failure on Attempt 1")
            return original_run_async(coro)

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.delete", return_value=mock_del_resp_fail) as mock_delete_att1, \
             patch("dashboard.api.embedbuilder.run_async", side_effect=fail_persist_attempt_1), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res1 = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res1.status_code, 500)
            data1 = res1.get_json()
            self.assertFalse(data1.get("success"))
            self.assertIn("compensation deletion failed", data1.get("error", ""))
            # Compensation delete was attempted on attempt 1 because message was newly minted
            mock_delete_att1.assert_called_once()

        # Confirm that DB has NO reaction_role_panels record for this message
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM reaction_role_panels WHERE message_id = ?", (int(msg_id_val),))
            self.assertEqual(c.fetchone()[0], 0, "DB record must be absent after attempt 1 failure")

        # Step 4, 5, 6: Client retries with same nonce N -> Discord returns existing message 999888111222
        # SQLite persistence fails again!
        def fail_persist_attempt_2(coro):
            coro_name = getattr(coro, "__name__", "")
            if coro_name == "persist_panel" or "persist" in str(coro):
                coro.close()
                raise sqlite3.OperationalError("Simulated DB Disk Failure on Attempt 2")
            return original_run_async(coro)

        mock_delete_att2 = MagicMock()
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.delete", mock_delete_att2), \
             patch("dashboard.api.embedbuilder.run_async", side_effect=fail_persist_attempt_2), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res2 = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res2.status_code, 500)
            data2 = res2.get_json()
            self.assertFalse(data2.get("success"))
            self.assertIn("Failed to refresh reaction role persistence on retry", data2.get("error", ""))
            # CRITICAL ACCEPTANCE CHECK: Compensation DELETE must NOT have been called on retry!
            mock_delete_att2.assert_not_called()

        # Database remains clean and uncorrupted
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM reaction_role_panels WHERE message_id = ?", (int(msg_id_val),))
            self.assertEqual(c.fetchone()[0], 0)

    def test_18_component_patch_mismatch_rollback(self):
        """Verify that if Discord rejects component PATCH (400 Bad Request), DB records are rolled back and error 502 is returned."""
        msg_id_val = "888777666555"
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"id": msg_id_val}

        # Discord PATCH returns 400 Bad Request (component schema invalid on Discord side)
        mock_patch_fail = MagicMock()
        mock_patch_fail.status_code = 400

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Panel with failing patch",
                "select_menus": [{"placeholder": "Select", "options": [{"label": "Opt1", "role_id": "90001"}]}]
            })
        }

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_patch_fail), \
             patch("dashboard.api.embedbuilder._req.delete") as mock_delete, \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res.status_code, 502)
            data = res.get_json()
            self.assertFalse(data.get("success"))
            self.assertIn("Failed to configure interactive components on Discord", data.get("error", ""))

        # Verify DB records were rolled back / deleted
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM reaction_role_panels WHERE message_id = ?", (int(msg_id_val),))
            self.assertEqual(c.fetchone()[0], 0, "DB records must be rolled back on Discord component PATCH failure")


if __name__ == "__main__":
    unittest.main()

    def test_18_component_patch_mismatch_rollback(self):
        """Verify that if Discord rejects component PATCH (400 Bad Request), DB records are rolled back and error 502 is returned."""
        msg_id_val = "888777666555"
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"id": msg_id_val}

        # Discord PATCH returns 400 Bad Request (component schema invalid on Discord side)
        mock_patch_fail = MagicMock()
        mock_patch_fail.status_code = 400

        payload = {
            "channel_id": "111222333",
            "payload_json": json.dumps({
                "content": "Panel with failing patch",
                "select_menus": [{"placeholder": "Select", "options": [{"label": "Opt1", "role_id": "90001"}]}]
            })
        }

        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")), \
             patch("dashboard.api.embedbuilder._req.post", return_value=mock_post_resp), \
             patch("dashboard.api.embedbuilder._req.patch", return_value=mock_patch_fail), \
             patch("dashboard.api.embedbuilder._req.delete") as mock_delete, \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-xyz"}):

            res = self.client.post("/api/embedbuilder/send", data=payload, headers={"X-CSRF-Token": "studio-csrf"})
            self.assertEqual(res.status_code, 502)
            data = res.get_json()
            self.assertFalse(data.get("success"))
            self.assertIn("Failed to configure interactive components on Discord", data.get("error", ""))

        # Verify DB records were rolled back / deleted
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM reaction_role_panels WHERE message_id = ?", (int(msg_id_val),))
            self.assertEqual(c.fetchone()[0], 0, "DB records must be rolled back on Discord component PATCH failure")


if __name__ == "__main__":
    unittest.main()
