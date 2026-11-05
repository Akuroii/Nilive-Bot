"""Regression: POST /api/tagmissions/save with role / temp_role rewards.

`row_id` used to be read (existing-role lookup on edit) BEFORE it was assigned,
so every role / temp_role save raised UnboundLocalError and returned HTTP 500.
Coins/diamonds/xp missions never reach that line, which is why the bug went
unnoticed. The real validate_assignable_role runs here; only the Discord HTTP
fetch is replaced with a fixed guild.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase1_support import GUILD, USER, execute, reset_database, rows  # noqa: E402
import asyncio  # noqa: E402

import dashboard.app as dashboard_app  # noqa: E402

MANAGE_ROLES = 0x10000000
ASSIGNABLE = "970000000000000001"      # position 1, below the bot's role
OTHER_ASSIGNABLE = "970000000000000002"  # position 2, below the bot's role
ABOVE_BOT = "970000000000000009"       # position 9, above the bot's role (not assignable)
BOT_ROLE = "970000000000000003"        # position 3, managed, Manage Roles


def fake_fetch(guild_id):
    roles = [
        {"id": str(guild_id), "name": "@everyone", "position": 0, "managed": False, "permissions": "0"},
        {"id": ASSIGNABLE, "name": "Member", "position": 1, "managed": False, "permissions": "0"},
        {"id": OTHER_ASSIGNABLE, "name": "VIP", "position": 2, "managed": False, "permissions": "0"},
        {"id": BOT_ROLE, "name": "Nero", "position": 3, "managed": True, "permissions": str(MANAGE_ROLES)},
        {"id": ABOVE_BOT, "name": "Staff", "position": 9, "managed": False, "permissions": "0"},
    ]
    bot = {"user": {"id": "970000000000000099"}, "roles": [BOT_ROLE], "permissions": str(MANAGE_ROLES)}
    return roles, bot, None


class TagMissionRoleRewardSaveTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'admin',1)", (GUILD, USER))
        app = dashboard_app.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        self.client = app.test_client()
        with self.client.session_transaction() as s:
            s.update(user={"id": USER, "username": "tester", "avatar": None}, guild_id=GUILD,
                     expires_at=time.time() + 7200, csrf_token="t")
        patcher = patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member", fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, **overrides):
        body = dict(title="Mission", starts_at="2030-01-01T10:00", ends_at="2030-01-02T10:00",
                    channel_id="555", reward_type="role", reward_role_id=ASSIGNABLE)
        body.update(overrides)
        return self.client.post("/api/tagmissions/save", json=body, headers={"X-CSRF-Token": "t"})

    def mission(self, mission_id=None):
        q = "SELECT id,title,reward_type,reward_role_id,reward_duration_hours,status FROM tag_missions"
        if mission_id:
            return rows(q + " WHERE id=?", (mission_id,))[0]
        return rows(q + " ORDER BY id")

    # ── create ────────────────────────────────────────────────────────────
    def test_create_role_reward_mission(self):
        r = self.save()
        self.assertEqual(r.status_code, 200, r.get_json())
        (mid, title, rtype, role, dur, status), = self.mission()
        self.assertEqual((title, rtype, role, dur, status), ("Mission", "role", int(ASSIGNABLE), None, "scheduled"))

    def test_create_temp_role_reward_mission(self):
        r = self.save(reward_type="temp_role", reward_duration_hours=24)
        self.assertEqual(r.status_code, 200, r.get_json())
        (mid, title, rtype, role, dur, status), = self.mission()
        self.assertEqual((rtype, role, dur), ("temp_role", int(ASSIGNABLE), 24))

    # ── edit (row_id present: the existing-role lookup actually runs) ─────
    def test_edit_role_reward_mission(self):
        self.assertEqual(self.save().status_code, 200)
        mid = self.mission()[0][0]
        r = self.save(id=mid, title="Renamed", reward_role_id=OTHER_ASSIGNABLE)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(self.mission()), 1, "edit must update in place, not insert")
        row = self.mission(mid)
        self.assertEqual((row[1], row[3]), ("Renamed", int(OTHER_ASSIGNABLE)))

    def test_edit_temp_role_reward_mission(self):
        self.assertEqual(self.save(reward_type="temp_role", reward_duration_hours=24).status_code, 200)
        mid = self.mission()[0][0]
        r = self.save(id=mid, reward_type="temp_role", reward_duration_hours=48, title="Longer")
        self.assertEqual(r.status_code, 200, r.get_json())
        row = self.mission(mid)
        self.assertEqual((row[1], row[2], row[4]), ("Longer", "temp_role", 48))

    def test_edit_keeps_unchanged_legacy_role_that_is_no_longer_assignable(self):
        # The reason the existing role is looked up on edit: a mission whose stored
        # role later became unassignable can still be edited if the role is unchanged.
        execute("INSERT INTO tag_missions (guild_id,title,reward_type,reward_role_id,channel_id,"
                "starts_at,ends_at) VALUES (?,?,?,?,?,?,?)",
                (GUILD, "Old", "role", int(ABOVE_BOT), 555, "2030-01-01T10:00", "2030-01-02T10:00"))
        mid = self.mission()[0][0]
        kept = self.save(id=mid, title="Old renamed", reward_role_id=ABOVE_BOT)
        self.assertEqual(kept.status_code, 200, kept.get_json())
        self.assertEqual(self.mission(mid)[1], "Old renamed")
        switched = self.save(id=mid, title="Switched", reward_role_id=BOT_ROLE)  # managed -> refused
        self.assertEqual(switched.status_code, 400, switched.get_json())
        self.assertEqual(self.mission(mid)[1], "Old renamed", "refused edit wrote nothing")

    # ── validation still behaves ──────────────────────────────────────────
    def test_new_unassignable_role_is_refused_and_nothing_is_written(self):
        r = self.save(reward_role_id=ABOVE_BOT)
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(self.mission(), [])

    def test_role_reward_without_role_and_temp_role_without_duration_are_refused(self):
        self.assertEqual(self.save(reward_role_id=None).status_code, 400)
        self.assertEqual(self.save(reward_type="temp_role").status_code, 400)
        self.assertEqual(self.mission(), [])

    def test_edit_of_missing_mission_is_404(self):
        r = self.save(id=99999)
        self.assertEqual(r.status_code, 404, r.get_json())

    def test_edit_of_started_mission_is_refused(self):
        self.assertEqual(self.save().status_code, 200)
        mid = self.mission()[0][0]
        execute("UPDATE tag_missions SET status='active' WHERE id=?", (mid,))
        r = self.save(id=mid, title="Too late")
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(self.mission(mid)[1], "Mission")

    # ── control: non-role rewards never touched the buggy line ────────────
    def test_coins_reward_mission_still_saves(self):
        r = self.save(reward_type="coins", reward_amount=100, reward_role_id=None)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.mission()[0][2], "coins")


if __name__ == "__main__":
    unittest.main(verbosity=1)
