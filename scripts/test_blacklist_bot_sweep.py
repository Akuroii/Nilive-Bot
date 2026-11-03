"""The dashboard only writes the database; the bot applies blacklist role changes.

The dashboard process has no Discord cache, so it can neither strip nor restore
roles. These tests pin the bot-side half: a periodic sweep that removes Level-only
roles from blacklisted members and restores roles after an unblacklist, while
leaving roles another system justifies untouched.
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"

from scripts.phase1_support import GUILD, USER, reset_database, execute, rows  # noqa: E402
from scripts.test_member_blacklist_d3 import StubGuild, StubMember, StubRole  # noqa: E402
import utils.level_claims as lc  # noqa: E402
from utils.level_claims import (  # noqa: E402
    blacklist_member, unblacklist_member, retry_blacklist_role_removal, process_blacklist_roles,
)

L5, L10 = 505, 1010


class SweepGuild(StubGuild):
    """StubGuild plus the REST lookup every real discord.Guild has."""

    async def fetch_member(self, user_id):
        member = self._members.get(int(user_id))
        if member is None:
            import discord
            raise discord.NotFound(mock.Mock(status=404, reason="Not Found"), "Unknown Member")
        return member


class StubBot:
    def __init__(self, *guilds):
        self._guilds = {g.id: g for g in guilds}

    def get_guild(self, guild_id):
        return self._guilds.get(int(guild_id))


def claim(user, level, role, track="role"):
    execute("INSERT INTO level_reward_claims (guild_id, user_id, reward_level, track, reward_ref, payload_json, "
            "status, created_at, fulfilled_at) VALUES (?, ?, ?, ?, ?, ?, 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (GUILD, user, level, track, str(role), json.dumps({"role_id": role})))


class TestBlacklistBotSweep(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        self.guild = SweepGuild(GUILD)
        self.r5, self.r10 = StubRole(L5, "L5", 10), StubRole(L10, "L10", 15)
        self.guild.add_role(self.r5)
        self.guild.add_role(self.r10)
        self.member = StubMember(USER, self.guild, roles=[self.r5])
        self.guild.add_member(self.member)
        self.bot = StubBot(self.guild)
        claim(USER, 5, L5)

    def queue(self):
        return rows("SELECT guild_id, user_id FROM leveling_blacklist_restores")

    # --- blacklisting -----------------------------------------------------------------
    async def test_01_dashboard_blacklist_is_reported_as_deferred(self):
        res = await blacklist_member(GUILD, USER)  # exactly how the dashboard calls it
        self.assertTrue(res["success"])
        self.assertEqual(res["role_cleanup"], "deferred")
        self.assertIn(self.r5, self.member.roles)  # nothing can happen in the dashboard process

    async def test_02_sweep_removes_a_level_only_role(self):
        await blacklist_member(GUILD, USER)
        summary = await process_blacklist_roles(self.bot)
        self.assertNotIn(self.r5, self.member.roles)
        self.assertEqual(summary["stripped"], 1)

    async def test_03_sweep_keeps_a_shop_owned_role(self):
        execute("INSERT INTO inventory_items (guild_id, user_id, item_name, item_type, quantity, metadata, source) "
                "VALUES (?, ?, 'VIP', 'role', 1, ?, 'shop')", (GUILD, USER, json.dumps({"role_id": L5})))
        await blacklist_member(GUILD, USER)
        await process_blacklist_roles(self.bot)
        self.assertIn(self.r5, self.member.roles)

    async def test_04_sweep_is_idempotent(self):
        await blacklist_member(GUILD, USER)
        await process_blacklist_roles(self.bot)
        summary = await process_blacklist_roles(self.bot)
        self.assertEqual(summary["stripped"], 0)
        self.assertNotIn(self.r5, self.member.roles)

    async def test_05_member_missing_from_cache_is_skipped_without_error(self):
        await blacklist_member(GUILD, USER)
        self.guild._members.clear()
        summary = await process_blacklist_roles(self.bot)
        self.assertEqual(summary["stripped"], 0)
        self.assertEqual(len(rows("SELECT 1 FROM leveling_member_blacklist")), 1)

    async def test_06_one_failing_member_does_not_stop_the_sweep(self):
        other = StubMember(USER + 1, self.guild, roles=[self.r5])
        self.guild.add_member(other)
        claim(USER + 1, 5, L5)
        await blacklist_member(GUILD, USER)
        await blacklist_member(GUILD, USER + 1)
        real = lc.retry_blacklist_role_removal
        calls = []

        async def flaky(guild_id, user_id, **kw):
            calls.append(user_id)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return await real(guild_id, user_id, **kw)
        with mock.patch.object(lc, "retry_blacklist_role_removal", flaky):
            await process_blacklist_roles(self.bot)
        self.assertEqual(len(calls), 2)

    # --- retry endpoint -----------------------------------------------------------------
    async def test_07_dashboard_retry_is_deferred_not_an_error(self):
        await blacklist_member(GUILD, USER)
        res = await retry_blacklist_role_removal(GUILD, USER)  # dashboard shape: no member, no bot
        self.assertTrue(res["success"])
        self.assertTrue(res["deferred"])

    async def test_08_retry_for_a_member_who_is_not_blacklisted_still_fails(self):
        res = await retry_blacklist_role_removal(GUILD, USER)
        self.assertFalse(res["success"])

    # --- unblacklisting -----------------------------------------------------------------
    async def test_09_dashboard_unblacklist_queues_a_restore_and_the_sweep_applies_it(self):
        await blacklist_member(GUILD, USER)
        await process_blacklist_roles(self.bot)
        self.assertNotIn(self.r5, self.member.roles)
        res = await unblacklist_member(GUILD, USER)  # dashboard shape
        self.assertTrue(res["success"])
        self.assertEqual(self.queue(), [(GUILD, USER)])
        self.assertNotIn(self.r5, self.member.roles)
        await process_blacklist_roles(self.bot)
        self.assertIn(self.r5, self.member.roles)
        self.assertEqual(self.queue(), [])

    async def test_10_unblacklist_with_a_member_restores_immediately_and_leaves_no_queue(self):
        await blacklist_member(GUILD, USER)
        self.member.roles = []
        await unblacklist_member(GUILD, USER, member=self.member)
        self.assertIn(self.r5, self.member.roles)
        self.assertEqual(self.queue(), [])

    async def test_11_restore_stays_queued_while_the_guild_is_unavailable(self):
        await blacklist_member(GUILD, USER)
        await unblacklist_member(GUILD, USER)
        await process_blacklist_roles(StubBot())  # bot knows no guilds yet (e.g. just started)
        self.assertEqual(self.queue(), [(GUILD, USER)])

    async def test_12_restore_is_dropped_when_the_member_left(self):
        await blacklist_member(GUILD, USER)
        await unblacklist_member(GUILD, USER)
        self.guild._members.clear()
        await process_blacklist_roles(self.bot)
        self.assertEqual(self.queue(), [])

    async def test_13_failed_restore_stays_queued_for_the_next_sweep(self):
        await blacklist_member(GUILD, USER)
        await unblacklist_member(GUILD, USER)
        failed = {"restored": [], "removed": [], "failed": [{"role_id": L5, "error": "x"}], "blocked": False}
        with mock.patch.object(lc, "restore_reached_level_roles", mock.AsyncMock(return_value=failed)):
            await process_blacklist_roles(self.bot)
        self.assertEqual(self.queue(), [(GUILD, USER)])

    async def test_14_reblacklisting_before_the_sweep_cancels_the_queued_restore(self):
        await blacklist_member(GUILD, USER)
        await unblacklist_member(GUILD, USER)
        await blacklist_member(GUILD, USER)
        self.assertEqual(self.queue(), [])

    async def test_16_dashboard_endpoints_report_deferred_work_end_to_end(self):
        """Through the real Flask routes, exactly as production calls them (no bot, no member)."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)",
                (GUILD, USER + 50))
        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(user={"id": str(USER + 50), "username": "Admin", "avatar": None}, guild_id=GUILD,
                        expires_at=time.time() + 7200, csrf_token="test-csrf")
        hdr = {"X-CSRF-Token": "test-csrf"}
        add = client.post("/api/leveling/member-blacklist", json={"user_id": str(USER), "reason": "t"}, headers=hdr)
        self.assertEqual(add.status_code, 200)
        self.assertEqual(add.get_json()["role_cleanup"], "deferred")
        retry = client.post("/api/leveling/member-blacklist/retry-roles", json={"user_id": str(USER)}, headers=hdr)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.get_json()["deferred"])
        gone = client.delete(f"/api/leveling/member-blacklist/{USER}", headers=hdr)
        self.assertEqual(gone.status_code, 200)
        self.assertEqual(gone.get_json()["role_restore"], "deferred")
        self.assertEqual(self.queue(), [(GUILD, USER)])


class TestCogWiring(unittest.IsolatedAsyncioTestCase):
    async def test_15_leveling_cog_runs_and_stops_the_sweep(self):
        from cogs.leveling import Leveling
        cog = Leveling(mock.MagicMock())
        try:
            self.assertTrue(cog.blacklist_role_task.is_running())
        finally:
            cog.cog_unload()
        await asyncio.sleep(0.05)  # cancellation takes effect on the next loop turn
        self.assertFalse(cog.blacklist_role_task.is_running())


if __name__ == "__main__":
    unittest.main()
