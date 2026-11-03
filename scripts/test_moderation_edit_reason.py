"""Editing a moderation log's reason must work, and stay scoped to the session's guild.

moderation_logs has no updated_at column, so the edit-reason query must not set one.
"""
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"

from scripts.phase1_support import GUILD, USER, reset_database, execute, rows  # noqa: E402


class TestModerationEditReason(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        for guild, uid in ((GUILD, 1), (GUILD + 1, 2)):
            execute("INSERT INTO moderation_logs (id, guild_id, user_id, user_display_name, moderator_id, "
                    "moderator_display_name, action, reason, source) VALUES (?, ?, 10, 'u', 20, 'm', 'warn', 'old', 'dashboard')",
                    (uid, guild))
        execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)",
                (GUILD, USER))
        import dashboard.app as dash
        dash.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = dash.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user={"id": str(USER), "username": "Admin", "avatar": None}, guild_id=GUILD,
                     expires_at=time.time() + 7200, csrf_token="t")

    def reason(self, log_id):
        return rows("SELECT reason FROM moderation_logs WHERE id = ?", (log_id,))[0][0]

    async def test_edit_reason_updates_the_log(self):
        resp = self.client.post("/api/moderation/edit-reason/1", json={"reason": "new reason"},
                                headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:200])
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(self.reason(1), "new reason")

    async def test_edit_reason_cannot_touch_another_guilds_log(self):
        self.client.post("/api/moderation/edit-reason/2", json={"reason": "hijack"},
                         headers={"X-CSRF-Token": "t"})
        self.assertEqual(self.reason(2), "old")


if __name__ == "__main__":
    unittest.main()
