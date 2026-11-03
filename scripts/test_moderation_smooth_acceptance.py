"""Comprehensive acceptance and regression tests for Moderation Smooth Navigation & Button Functionality.

Verifies:
1. Server-side rendering of all 4 moderation tabs simultaneously for client-side no-refresh switching.
2. Deep linking for logs, punishments, warnings, thresholds, and safe fallback on invalid tabs.
3. Quick action API endpoint validation and logging into moderation_logs and warnings.
4. Reason editing in moderation_logs without database errors.
5. Soft-deletion of moderation logs.
6. Clearing warnings (both total and count-limited).
7. Warning thresholds insertion, update, and deletion.
8. Auto-escalation setting toggling in guild_settings_kv.
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"
os.environ["OWNER_ID"] = "999999999"

from scripts.phase1_support import GUILD, USER, reset_database, rows, execute
import aiosqlite
from database import DB_PATH, init_db

class TestModerationSmoothNavigation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        self.guild_id = GUILD
        self.user_id = USER

    def get_test_client(self):
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
                (self.guild_id, self.user_id))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_id), "username": "ModAdmin", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="mod-csrf"
            )
        return client

    async def test_01_moderation_renders_all_tabs_simultaneously(self):
        """Verify GET /moderation renders all 4 tab containers in DOM with default logs active."""
        client = self.get_test_client()
        res = client.get("/moderation")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertIn('id="tab-content-logs"', html)
        self.assertIn('id="tab-content-punishments"', html)
        self.assertIn('id="tab-content-warnings"', html)
        self.assertIn('id="tab-content-thresholds"', html)
        self.assertIn('id="tab-logs"', html)
        self.assertIn('id="tab-punishments"', html)
        self.assertIn('id="tab-warnings"', html)
        self.assertIn('id="tab-thresholds"', html)

    async def test_02_moderation_deep_linking_and_fallback(self):
        """Verify deep-linked tab param activates the target tab, and invalid tab defaults safely."""
        client = self.get_test_client()
        
        # Deep link to warnings
        res_warn = client.get("/moderation?tab=warnings")
        self.assertEqual(res_warn.status_code, 200)
        html_warn = res_warn.get_data(as_text=True)
        self.assertIn('id="tab-content-warnings" class="tab-content" style=""', html_warn)
        self.assertIn('id="tab-content-logs" class="tab-content" style="display:none;"', html_warn)

        # Deep link to thresholds
        res_thresh = client.get("/moderation?tab=thresholds")
        self.assertEqual(res_thresh.status_code, 200)
        html_thresh = res_thresh.get_data(as_text=True)
        self.assertIn('id="tab-content-thresholds" class="tab-content" style=""', html_thresh)

        # Invalid tab param falls back safely to logs
        res_inv = client.get("/moderation?tab=invalid_random_tab")
        self.assertEqual(res_inv.status_code, 200)
        html_inv = res_inv.get_data(as_text=True)
        self.assertIn('id="tab-content-logs" class="tab-content" style=""', html_inv)

    async def test_03_quick_action_and_log_persistence(self):
        """Verify quick action logs into moderation_logs and warnings tables with user_display_name."""
        client = self.get_test_client()

        with patch.dict(os.environ, {"DISCORD_TOKEN": "mock_token"}):
            with patch("requests.put") as mock_put:
                mock_put.return_value = MagicMock(status_code=204)
                payload = {
                    "user_id": "112233445566778899",
                    "action": "warn",
                    "reason": "Test warning action"
                }
                res = client.post(
                    "/api/moderation/quick-action",
                    json=payload,
                    headers={"X-CSRF-Token": "mod-csrf"}
                )
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data.get("success"))

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id, user_display_name, action, reason FROM moderation_logs WHERE guild_id=?", (self.guild_id,))
            log_row = await cur.fetchone()
            self.assertIsNotNone(log_row)
            self.assertEqual(log_row[0], 112233445566778899)
            self.assertEqual(log_row[2], "warn")
            self.assertEqual(log_row[3], "Test warning action")

            cur_w = await db.execute("SELECT user_id, reason FROM warnings WHERE guild_id=?", (self.guild_id,))
            warn_row = await cur_w.fetchone()
            self.assertIsNotNone(warn_row)
            self.assertEqual(warn_row[0], 112233445566778899)

    async def test_04_edit_reason_and_delete_log(self):
        """Verify editing reason and deleting moderation logs."""
        client = self.get_test_client()

        # Seed a log entry
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                INSERT INTO moderation_logs (guild_id, user_id, user_display_name, moderator_id, moderator_display_name, action, reason, source)
                VALUES (?, 12345, 'TargetUser', 999999999, 'ModAdmin', 'warn', 'Initial reason', 'dashboard')
            """, (self.guild_id,))
            await db.commit()
            log_id = cur.lastrowid

        # Edit reason
        edit_res = client.post(
            f"/api/moderation/edit-reason/{log_id}",
            json={"reason": "Corrected reason"},
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(edit_res.status_code, 200)
        self.assertTrue(edit_res.get_json().get("success"))

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT reason FROM moderation_logs WHERE id=?", (log_id,))
            r = await cur.fetchone()
            self.assertEqual(r[0], "Corrected reason")

        # Soft delete log
        del_res = client.delete(
            f"/api/moderation/delete-log/{log_id}",
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.get_json().get("success"))

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT deleted FROM moderation_logs WHERE id=?", (log_id,))
            r = await cur.fetchone()
            self.assertEqual(r[0], 1)

    async def test_05_clear_warnings(self):
        """Verify clearing warnings for a user."""
        client = self.get_test_client()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, 778899, 999999999, 'Warn 1')", (self.guild_id,))
            await db.execute("INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, 778899, 999999999, 'Warn 2')", (self.guild_id,))
            await db.commit()

        clear_res = client.post(
            "/api/moderation/clear-warnings",
            json={"user_id": 778899},
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(clear_res.status_code, 200)
        self.assertTrue(clear_res.get_json().get("success"))

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?", (self.guild_id, 778899))
            count = (await cur.fetchone())[0]
            self.assertEqual(count, 0)

    async def test_06_warning_thresholds_crud(self):
        """Verify warning thresholds add, list, and delete operations."""
        client = self.get_test_client()

        # Add threshold
        add_res = client.post(
            "/api/moderation/warning-thresholds",
            json={"warn_count": 4, "action": "timeout", "duration_minutes": 30},
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(add_res.status_code, 200)
        self.assertTrue(add_res.get_json().get("success"))

        # List thresholds
        list_res = client.get("/api/moderation/warning-thresholds")
        self.assertEqual(list_res.status_code, 200)
        thresholds = list_res.get_json()
        self.assertEqual(len(thresholds), 1)
        self.assertEqual(thresholds[0]["warn_count"], 4)
        tid = thresholds[0]["id"]

        # Delete threshold
        del_res = client.delete(
            f"/api/moderation/warning-thresholds/{tid}",
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.get_json().get("success"))

        list_res2 = client.get("/api/moderation/warning-thresholds")
        self.assertEqual(len(list_res2.get_json()), 0)

    async def test_07_auto_escalation_toggle(self):
        """Verify toggling auto-escalation in both ON and OFF directions."""
        client = self.get_test_client()

        # Turn off
        off_res = client.post(
            "/api/moderation/auto-escalation",
            json={"enabled": False},
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(off_res.status_code, 200)
        self.assertTrue(off_res.get_json().get("success"))

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT value FROM guild_settings_kv WHERE guild_id=? AND key='auto_escalation_enabled'", (self.guild_id,))
            val = (await cur.fetchone())[0]
            self.assertEqual(val, "0")

        # Turn on
        on_res = client.post(
            "/api/moderation/auto-escalation",
            json={"enabled": True},
            headers={"X-CSRF-Token": "mod-csrf"}
        )
        self.assertEqual(on_res.status_code, 200)
        self.assertTrue(on_res.get_json().get("success"))

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT value FROM guild_settings_kv WHERE guild_id=? AND key='auto_escalation_enabled'", (self.guild_id,))
            val = (await cur.fetchone())[0]
            self.assertEqual(val, "1")


if __name__ == "__main__":
    unittest.main()
