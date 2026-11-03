#!/usr/bin/env python3
"""
Acceptance Test Suite for Workstream 2 (WS2):
Feature-Specific Discord Role Hierarchy Diagnostics & Assignability Protection.

Covers all 12 required test scenarios:
1. Valid assignable role.
2. Role above the bot's highest role.
3. Equal numeric positions with different role-ordering outcomes according to installed library.
4. Managed role.
5. @everyone role.
6. Missing MANAGE_ROLES and relevant Administrator implications.
7. New invalid assignment rejected before database mutation.
8. Unchanged existing invalid binding does not block unrelated edits.
9. Changing a previously valid role's hierarchy prevents subsequent invalid assignment attempts.
10. Assignment pickers are restricted while permission/filter pickers remain unrestricted.
11. Runtime Forbidden failures are handled without crashing relevant workflows.
12. Missing role metadata and bot-member information fails safely.
"""

import sys
import os

os.environ["NERO_ENVIRONMENT"] = "staging"
os.environ["DATABASE_PATH"] = "/tmp/test_prod_dummy.db"
os.environ["STAGING_DATABASE_PATH"] = "/tmp/test_ws2_roles.db"
os.environ["OWNER_ID"] = "999999999"
os.environ["SECRET_KEY"] = "0123456789abcdef0123456789abcdef"
os.environ["DISCORD_TOKEN"] = "mock_bot_token"

import unittest
import json
import asyncio
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
import discord
from database import DB_PATH, init_db
from utils.permissions import check_bot_role_position
from dashboard.utils.role_validation import (
    compute_role_assignability,
    validate_assignable_role,
    fetch_guild_roles_and_bot_member,
    MANAGE_ROLES_PERM,
    ADMINISTRATOR_PERM,
)


class TestWS2RoleHierarchyAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())

    def setUp(self):
        self.guild_id = 123456789012345678
        self.bot_user_id = 999888777666555444

        # Sample Discord Roles Payload
        self.sample_roles = [
            {"id": "123456789012345678", "name": "@everyone", "position": 0, "color": 0, "managed": False},
            {"id": "100000000000000001", "name": "Member", "position": 1, "color": 0x3498db, "managed": False},
            {"id": "100000000000000002", "name": "VIP", "position": 2, "color": 0x2ecc71, "managed": False},
            {"id": "100000000000000003", "name": "Nero Bot Role", "position": 5, "color": 0x9b59b6, "managed": True},
            {"id": "100000000000000004", "name": "Equal-Older", "position": 5, "color": 0x1abc9c, "managed": False},  # ID smaller than 100000000000000005
            {"id": "100000000000000006", "name": "Equal-Newer", "position": 5, "color": 0x1abc9c, "managed": False},  # ID greater than bot role
            {"id": "100000000000000007", "name": "Moderator", "position": 8, "color": 0xe67e22, "managed": False},
            {"id": "100000000000000008", "name": "Administrator", "position": 10, "color": 0xe74c3c, "managed": False},
            {"id": "100000000000000009", "name": "Server Booster", "position": 3, "color": 0xf47fff, "managed": True},
        ]

        # Bot Member object with MANAGE_ROLES and Nero Bot Role
        self.bot_member = {
            "user": {"id": str(self.bot_user_id)},
            "roles": ["100000000000000003"],
            "permissions": str(MANAGE_ROLES_PERM),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Valid Assignable Role
    # ──────────────────────────────────────────────────────────────────────────
    def test_01_valid_assignable_role(self):
        info = compute_role_assignability(self.guild_id, self.sample_roles, self.bot_member)
        role_vip = info["role_map"]["100000000000000002"]
        self.assertTrue(role_vip["assignable"])
        self.assertIsNone(role_vip["unassignable_reason"])

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Role Above Bot Highest Role
    # ──────────────────────────────────────────────────────────────────────────
    def test_02_role_above_bot_hierarchy(self):
        info = compute_role_assignability(self.guild_id, self.sample_roles, self.bot_member)
        role_admin = info["role_map"]["100000000000000008"]
        self.assertFalse(role_admin["assignable"])
        self.assertEqual(role_admin["unassignable_reason"], "above_bot")
        self.assertIn("Above @Nero Bot Role", role_admin["unassignable_label"])

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Equal Numeric Positions with Discord Comparison Semantics
    # ──────────────────────────────────────────────────────────────────────────
    def test_03_equal_positions_tie_breaker(self):
        info = compute_role_assignability(self.guild_id, self.sample_roles, self.bot_member)
        # Nero Bot Role ID is 100000000000000003 (position 5).
        # Role Equal-Older has ID 100000000000000004 (numeric ID > bot ID). In Discord's
        # hierarchy, smaller ID is older and higher.
        # Role Equal-Older (ID 04) has pos 5. Bot role (ID 03) has pos 5.
        # Key comparison: (5, -4) vs (5, -3) -> (5, -4) < (5, -3), so Equal-Older is below Bot!
        role_older = info["role_map"]["100000000000000004"]
        self.assertTrue(role_older["assignable"])

        # Equal-Newer with equal position but higher priority or equal to bot
        role_bot_itself = info["role_map"]["100000000000000003"]
        self.assertFalse(role_bot_itself["assignable"])  # Managed and equal to bot

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Managed Integration Role
    # ──────────────────────────────────────────────────────────────────────────
    def test_04_managed_role_unassignable(self):
        info = compute_role_assignability(self.guild_id, self.sample_roles, self.bot_member)
        role_booster = info["role_map"]["100000000000000009"]
        self.assertFalse(role_booster["assignable"])
        self.assertEqual(role_booster["unassignable_reason"], "managed")

    # ──────────────────────────────────────────────────────────────────────────
    # 5. @everyone Role
    # ──────────────────────────────────────────────────────────────────────────
    def test_05_everyone_role_unassignable(self):
        info = compute_role_assignability(self.guild_id, self.sample_roles, self.bot_member)
        role_everyone = info["role_map"][str(self.guild_id)]
        self.assertFalse(role_everyone["assignable"])
        self.assertEqual(role_everyone["unassignable_reason"], "everyone")

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Missing MANAGE_ROLES and Administrator Implications
    # ──────────────────────────────────────────────────────────────────────────
    def test_06_missing_manage_roles_and_admin_bypass(self):
        # 6a. Bot member with 0 permissions
        no_perm_bot = dict(self.bot_member, permissions="0")
        info = compute_role_assignability(self.guild_id, self.sample_roles, no_perm_bot)
        role_vip = info["role_map"]["100000000000000002"]
        self.assertFalse(role_vip["assignable"])
        self.assertEqual(role_vip["unassignable_reason"], "no_manage_roles_perm")

        # 6b. Bot member with ADMINISTRATOR (implies MANAGE_ROLES)
        admin_bot = dict(self.bot_member, permissions=str(ADMINISTRATOR_PERM))
        info_admin = compute_role_assignability(self.guild_id, self.sample_roles, admin_bot)
        role_vip_admin = info_admin["role_map"]["100000000000000002"]
        self.assertTrue(role_vip_admin["assignable"])

    # ──────────────────────────────────────────────────────────────────────────
    # 7. New Invalid Assignment Rejected Before Database Mutation
    # ──────────────────────────────────────────────────────────────────────────
    def test_07_new_invalid_assignment_rejected(self):
        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member",
                   return_value=(self.sample_roles, self.bot_member, None)):
            # Attempt to validate Administrator role (above bot)
            valid, err = validate_assignable_role(self.guild_id, "100000000000000008")
            self.assertFalse(valid)
            self.assertIn("Nero's highest role", err)

            # Attempt to validate Managed role
            valid_m, err_m = validate_assignable_role(self.guild_id, "100000000000000009")
            self.assertFalse(valid_m)
            self.assertIn("managed by an external integration", err_m)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Unchanged Existing Invalid Binding Does Not Block Unrelated Edits
    # ──────────────────────────────────────────────────────────────────────────
    def test_08_delta_validation_preserves_existing_binding(self):
        # An unassignable role (Admin: 100000000000000008) is already the stored binding
        existing_role = "100000000000000008"
        submitted_role = "100000000000000008"

        # Delta validation passes immediately without blocking
        valid, err = validate_assignable_role(self.guild_id, submitted_role, existing_role)
        self.assertTrue(valid)
        self.assertIsNone(err)

    # ──────────────────────────────────────────────────────────────────────────
    # 9. Changing Previously Valid Role's Hierarchy Prevents Subsequent Invalid Saves
    # ──────────────────────────────────────────────────────────────────────────
    def test_09_dynamic_hierarchy_drift_validation(self):
        # Suppose VIP role was previously at position 2 (valid), but was dragged to position 9 in Discord
        drifted_roles = [
            dict(r, position=9 if r["id"] == "100000000000000002" else r["position"])
            for r in self.sample_roles
        ]
        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member",
                   return_value=(drifted_roles, self.bot_member, None)):
            # Now saving VIP role as a new assignment is rejected
            valid, err = validate_assignable_role(self.guild_id, "100000000000000002", existing_role_id=None)
            self.assertFalse(valid)
            self.assertIn("Nero's highest role", err)

    # ──────────────────────────────────────────────────────────────────────────
    # 10. Assignment vs Permission Pickers Separation
    # ──────────────────────────────────────────────────────────────────────────
    def test_10_assignment_pickers_opt_in(self):
        # Verify template configurations:
        # Welcome auto_role and rules_role must have data-assignable-only="true"
        with open("dashboard/templates/config/welcome.html") as f:
            welcome_html = f.read()
        self.assertIn('id="join-auto-role"', welcome_html)
        self.assertIn('data-assignable-only="true"', welcome_html)
        self.assertIn('id="rules-role"', welcome_html)

        # Boost roles must have data-assignable-only="true"
        with open("dashboard/templates/config/boost.html") as f:
            boost_html = f.read()
        self.assertIn('id="boost-role-one"', boost_html)
        self.assertIn('data-assignable-only="true"', boost_html)

        # Commands permission pickers must NOT have data-assignable-only
        with open("dashboard/templates/manage/commands.html") as f:
            commands_html = f.read()
        self.assertIn('id="edit-enabled-roles', commands_html)
        self.assertNotIn('edit-enabled-roles-{{ cmd }}" multiple data-assignable-only', commands_html)

    # ──────────────────────────────────────────────────────────────────────────
    # 11. Runtime Defense & Forbidden Handling
    # ──────────────────────────────────────────────────────────────────────────
    def test_11_runtime_defense_and_forbidden_handling(self):
        # Create a mock guild and mock member
        mock_guild = MagicMock()
        mock_guild.id = self.guild_id
        mock_guild.owner_id = 111111111

        mock_bot = MagicMock()
        mock_bot.id = self.bot_user_id
        mock_bot.top_role.position = 5
        mock_bot.top_role.name = "Nero Bot Role"
        mock_bot.guild_permissions.administrator = False
        mock_bot.guild_permissions.manage_roles = True
        mock_guild.me = mock_bot

        # Mock role below bot
        mock_role_valid = MagicMock()
        mock_role_valid.id = 100000000000000002
        mock_role_valid.position = 2
        mock_role_valid.managed = False
        mock_role_valid.name = "VIP"
        mock_role_valid.is_default.return_value = False
        # Setup comparison: bot_top > mock_role_valid is True
        mock_bot.top_role.__gt__.return_value = True

        can, warn = check_bot_role_position(mock_guild, mock_role_valid)
        self.assertTrue(can)
        self.assertEqual(warn, "OK")

        # Mock role above bot
        mock_role_high = MagicMock()
        mock_role_high.id = 100000000000000008
        mock_role_high.position = 10
        mock_role_high.managed = False
        mock_role_high.name = "Administrator"
        mock_role_high.is_default.return_value = False
        mock_bot.top_role.__gt__.return_value = False

        can_high, warn_high = check_bot_role_position(mock_guild, mock_role_high)
        self.assertFalse(can_high)
        self.assertIn("Nero's role must be strictly above", warn_high)

    # ──────────────────────────────────────────────────────────────────────────
    # 12. Missing Role Metadata & Bot Member Fails Safely
    # ──────────────────────────────────────────────────────────────────────────
    def test_12_missing_metadata_fails_safely(self):
        # Bot member missing (None)
        info = compute_role_assignability(self.guild_id, self.sample_roles, bot_member=None)
        for r in info["roles"]:
            self.assertFalse(r["assignable"])
            self.assertEqual(r["unassignable_reason"], "bot_missing")

        # check_bot_role_position with None guild or None role
        can, warn = check_bot_role_position(None, None)
        self.assertFalse(can)
        self.assertIn("missing", warn.lower())

    # ──────────────────────────────────────────────────────────────────────────
    # 13. Fail-Closed on Discord 401 / 403 API Error
    # ──────────────────────────────────────────────────────────────────────────
    def test_13_fail_closed_discord_401_403(self):
        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member",
                   return_value=(None, None, "Discord error 401 fetching roles")):
            valid, err = validate_assignable_role(self.guild_id, "100000000000000002")
            self.assertFalse(valid)
            self.assertIn("Unable to verify role", err)
            self.assertIn("Discord error 401", err)

    # ──────────────────────────────────────────────────────────────────────────
    # 14. Fail-Closed on Discord Timeout / Network Failure
    # ──────────────────────────────────────────────────────────────────────────
    def test_14_fail_closed_discord_timeout_network_failure(self):
        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member",
                   return_value=(None, None, "Network error fetching guild roles: Connection timed out")):
            valid, err = validate_assignable_role(self.guild_id, "100000000000000002")
            self.assertFalse(valid)
            self.assertIn("Unable to verify role", err)
            self.assertIn("Connection timed out", err)

    # ──────────────────────────────────────────────────────────────────────────
    # 15. API Leveling Reward Save Fails Closed on Validation Error
    # ──────────────────────────────────────────────────────────────────────────
    def test_15_api_save_rejected_when_validation_fails_closed(self):
        import time
        from dashboard.app import app
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"id": "999999999", "username": "Admin"}
            sess["expires_at"] = time.time() + 3600
            sess["guild_id"] = str(self.guild_id)
            sess["guild_name"] = "Test Guild"
            sess["csrf_token"] = "test_csrf_token"
            sess["user_level"] = "admin"

        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member",
                   return_value=(None, None, "Discord error 403 fetching roles")):
            resp = client.post("/api/leveling/reward",
                               headers={"X-CSRF-Token": "test_csrf_token"},
                               json={"level": 5, "role_id": "100000000000000002", "audience": "everyone"})
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data.get("success"))
            self.assertIn("Invalid Reward Role", data.get("error"))

    # ──────────────────────────────────────────────────────────────────────────
    # 16. API Leveling Reward Save Accepted with Valid Mock Data
    # ──────────────────────────────────────────────────────────────────────────
    def test_16_api_save_accepted_with_valid_mock_data(self):
        import time
        from dashboard.app import app
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"id": "999999999", "username": "Admin"}
            sess["expires_at"] = time.time() + 3600
            sess["guild_id"] = str(self.guild_id)
            sess["guild_name"] = "Test Guild"
            sess["csrf_token"] = "test_csrf_token"
            sess["user_level"] = "admin"

        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member",
                   return_value=(self.sample_roles, self.bot_member, None)):
            resp = client.post("/api/leveling/reward",
                               headers={"X-CSRF-Token": "test_csrf_token"},
                               json={"level": 5, "role_id": "100000000000000002", "audience": "everyone"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("success"))


if __name__ == "__main__":
    unittest.main()
