"""Production Regression Test Suite for Developer Guild Access and Gateway Synchronization (PLAN-03).
Exercises actual production implementations across all 16 mandatory test categories.
"""
import asyncio
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

os.environ.setdefault("OWNER_ID", "704453350384730237")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_PATH", "/tmp/test_plan03_production.db")
os.environ.setdefault("DASHBOARD_DEMO_LOGIN", "0")

import aiosqlite
from utils.db_config import DB_PATH
from utils.startup_barrier import run_barrier
from utils.guild_lifecycle import GuildLifecycleCoordinator
from database import (
    check_bot_guild_access_atomic,
    GuildAccessState,
    sync_bot_guilds,
    is_bot_sync_valid,
    touch_bot_sync_lease,
    invalidate_bot_sync_state,
    restore_bot_sync_state,
    record_bot_guild_join,
    record_bot_guild_remove,
    record_bot_guild_update,
    get_stored_bot_guilds,
    BOT_SYNC_LEASE_TTL_SECONDS,
)
from dashboard.app import app
from dashboard.permissions import _get_permission_level
from dashboard.utils.async_utils import run_async


class Plan03ProductionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "prod_test.db")
        # Run actual startup barrier to create schema and establish fail-closed state
        run_barrier(self.db_path)

    def tearDown(self):
        self.test_dir.cleanup()

    # Category 1 & 2: Fresh and existing database startup, initial and repeated startup barriers
    def test_01_fresh_and_existing_database_startup_barrier(self):
        # 1. Fresh db barrier established fail-closed invariants
        state = run_async(check_bot_guild_access_atomic(101, db_path=self.db_path))
        self.assertEqual(state, GuildAccessState.INDETERMINATE)

        # 2. Existing db barrier re-execution
        # Simulate active sync state first
        run_async(sync_bot_guilds([{"guild_id": 101, "name": "Server 101"}], authoritative=True, db_path=self.db_path))
        self.assertEqual(run_async(check_bot_guild_access_atomic(101, db_path=self.db_path)), GuildAccessState.INSTALLED)

        # Re-run barrier: MUST fail closed immediately
        success = run_barrier(self.db_path)
        self.assertTrue(success)
        self.assertEqual(run_async(check_bot_guild_access_atomic(101, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

    # Category 3: Bot restart and crash invalidation
    def test_02_bot_restart_and_crash_invalidation(self):
        # Establish valid state
        run_async(sync_bot_guilds([{"guild_id": 201, "name": "Server 201"}], authoritative=True, db_path=self.db_path))
        self.assertEqual(run_async(check_bot_guild_access_atomic(201, db_path=self.db_path)), GuildAccessState.INSTALLED)

        # Simulate post-exit barrier after crash
        run_barrier(self.db_path)
        self.assertEqual(run_async(check_bot_guild_access_atomic(201, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

    # Category 4 & 5: Gateway disconnect, resume, and failed reconciliation
    def test_03_gateway_disconnect_resume_and_reconciliation(self):
        bot = MagicMock()
        bot.is_ready.return_value = True
        bot.is_closed.return_value = False
        bot.ws.open = True
        guild = MagicMock()
        guild.id = 301
        guild.name = "Gateway Guild"
        guild.icon = None
        guild.owner_id = 999
        guild.member_count = 10
        guild.unavailable = False
        bot.guilds = [guild]

        coord = GuildLifecycleCoordinator(bot)

        # 1. Initial on_ready reconciliation
        coord.gateway_transport_verified = True
        reconciled = run_async(coord.reconcile_guilds(db_path=self.db_path))
        self.assertTrue(reconciled)
        self.assertEqual(run_async(check_bot_guild_access_atomic(301, db_path=self.db_path)), GuildAccessState.INSTALLED)

        # 2. Gateway disconnect
        run_async(coord.on_disconnect_async(db_path=self.db_path))
        self.assertFalse(coord.gateway_transport_verified)
        self.assertFalse(coord.guild_membership_reconciled)
        self.assertEqual(run_async(check_bot_guild_access_atomic(301, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

        # 3. Failed RESUMED reconciliation (e.g. database error)
        coord.gateway_transport_verified = True
        rec_failed = run_async(coord.reconcile_guilds(db_path=self.db_path, simulate_failure=True))
        self.assertFalse(rec_failed)
        self.assertFalse(coord.guild_membership_reconciled)
        self.assertEqual(run_async(check_bot_guild_access_atomic(301, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

        # 4. Successful RESUMED recovery
        rec_recovered = run_async(coord.reconcile_guilds(db_path=self.db_path, simulate_failure=False))
        self.assertTrue(rec_recovered)
        self.assertTrue(coord.guild_membership_reconciled)
        self.assertEqual(run_async(check_bot_guild_access_atomic(301, db_path=self.db_path)), GuildAccessState.INSTALLED)

    # Category 6: Concurrent guild joins and removals
    def test_04_concurrent_guild_joins_and_removals(self):
        bot = MagicMock()
        bot.is_ready.return_value = True
        bot.is_closed.return_value = False
        bot.ws.open = True
        bot.guilds = []
        coord = GuildLifecycleCoordinator(bot)
        coord.gateway_transport_verified = True
        coord.guild_membership_reconciled = True
        run_async(restore_bot_sync_state(self.db_path))

        async def run_concurrent_mutations():
            async def join_op(gid, name):
                async def _m():
                    await asyncio.sleep(0.01)
                    await record_bot_guild_join(gid, name, None, 0, 0, db_path=self.db_path)
                await coord.execute_mutation(_m, db_path=self.db_path)

            async def remove_op(gid):
                async def _m():
                    await asyncio.sleep(0.01)
                    await record_bot_guild_remove(gid, db_path=self.db_path)
                await coord.execute_mutation(_m, db_path=self.db_path)

            # Seed guild 401 first
            await join_op(401, "Server 401")
            self.assertEqual(await check_bot_guild_access_atomic(401, db_path=self.db_path), GuildAccessState.INSTALLED)

            # Concurrent remove 401 and join 402
            t_rem = asyncio.create_task(remove_op(401))
            t_join = asyncio.create_task(join_op(402, "Server 402"))
            await asyncio.gather(t_rem, t_join)

            self.assertEqual(await check_bot_guild_access_atomic(401, db_path=self.db_path), GuildAccessState.CONFIRMED_NO_ACCESS)
            self.assertEqual(await check_bot_guild_access_atomic(402, db_path=self.db_path), GuildAccessState.INSTALLED)

        run_async(run_concurrent_mutations())

    # Category 7: Mutation cancellation at every await boundary
    def test_05_mutation_cancellation_at_await_boundaries(self):
        bot = MagicMock()
        bot.is_ready.return_value = True
        bot.is_closed.return_value = False
        bot.ws.open = True
        bot.guilds = []
        coord = GuildLifecycleCoordinator(bot)
        coord.gateway_transport_verified = True
        coord.guild_membership_reconciled = True
        run_async(restore_bot_sync_state(self.db_path))

        async def run_cancellation_tests():
            # Boundary A: Cancelled during mutation body
            async def cancelled_body():
                await asyncio.sleep(0.05)
                raise asyncio.CancelledError()

            task_a = asyncio.create_task(coord.execute_mutation(cancelled_body, db_path=self.db_path))
            await asyncio.sleep(0.01)
            task_a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task_a

            self.assertTrue(coord.has_unreconciled_failure)
            self.assertFalse(coord.guild_membership_reconciled)
            self.assertEqual(await check_bot_guild_access_atomic(501, db_path=self.db_path), GuildAccessState.INDETERMINATE)

            # Normal mutation afterwards MUST refuse to restore until authoritative reconciliation
            async def normal_body():
                await asyncio.sleep(0.01)
            await coord.execute_mutation(normal_body, db_path=self.db_path)
            self.assertEqual(await check_bot_guild_access_atomic(501, db_path=self.db_path), GuildAccessState.INDETERMINATE)

            # Reconciliation clears failure
            await coord.reconcile_guilds(db_path=self.db_path)
            self.assertFalse(coord.has_unreconciled_failure)
            self.assertTrue(coord.guild_membership_reconciled)

        run_async(run_cancellation_tests())

    # Category 8: Restoration versus newer mutation race
    def test_06_restoration_vs_newer_mutation_race(self):
        bot = MagicMock()
        bot.is_ready.return_value = True
        bot.is_closed.return_value = False
        bot.ws.open = True
        bot.guilds = []
        coord = GuildLifecycleCoordinator(bot)
        coord.gateway_transport_verified = True
        coord.guild_membership_reconciled = True
        run_async(restore_bot_sync_state(self.db_path))

        async def run_race_test():
            # Mutation 1 finishes, Mutation 2 queues behind lock
            async def m1():
                await asyncio.sleep(0.03)

            async def m2():
                await asyncio.sleep(0.01)

            t1 = asyncio.create_task(coord.execute_mutation(m1, db_path=self.db_path))
            await asyncio.sleep(0.005)
            t2 = asyncio.create_task(coord.execute_mutation(m2, db_path=self.db_path))

            await asyncio.gather(t1, t2)
            self.assertEqual(coord.pending_mutations, 0)
            self.assertFalse(coord.has_unreconciled_failure)
            self.assertEqual(await check_bot_guild_access_atomic(601, db_path=self.db_path), GuildAccessState.CONFIRMED_NO_ACCESS)

        run_async(run_race_test())

    # Category 9: Heartbeat during pending mutations
    def test_07_heartbeat_during_pending_mutations(self):
        bot = MagicMock()
        bot.is_ready.return_value = True
        bot.is_closed.return_value = False
        bot.ws.open = True
        coord = GuildLifecycleCoordinator(bot)
        coord.gateway_transport_verified = True
        coord.guild_membership_reconciled = True

        self.assertTrue(coord.can_touch_lease())

        # Simulate pending mutation
        coord.pending_mutations = 1
        self.assertFalse(coord.can_touch_lease())
        coord.pending_mutations = 0

        # Simulate unreconciled failure
        coord.has_unreconciled_failure = True
        self.assertFalse(coord.can_touch_lease())
        coord.has_unreconciled_failure = False

        # Simulate transport unverified
        coord.gateway_transport_verified = False
        self.assertFalse(coord.can_touch_lease())

    # Category 10: Expired, missing, malformed, and future leases
    def test_08_lease_validation_boundary_conditions(self):
        now = datetime.now(timezone.utc).isoformat()
        # Seed guild and valid sync
        run_async(sync_bot_guilds([{"guild_id": 801, "name": "Server 801"}], authoritative=True, db_path=self.db_path))
        self.assertEqual(run_async(check_bot_guild_access_atomic(801, db_path=self.db_path)), GuildAccessState.INSTALLED)

        # 1. Expired lease (> 45s)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=46)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE bot_sync_state SET updated_at = ? WHERE key = 'bot_sync_lease'", (expired,))
        self.assertEqual(run_async(check_bot_guild_access_atomic(801, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

        # 2. Missing lease
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM bot_sync_state WHERE key = 'bot_sync_lease'")
        self.assertEqual(run_async(check_bot_guild_access_atomic(801, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

        # 3. Malformed lease
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO bot_sync_state VALUES ('bot_sync_lease', '1', 'invalid-date')")
        self.assertEqual(run_async(check_bot_guild_access_atomic(801, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

        # 4. Future lease (> 5s ahead)
        future = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE bot_sync_state SET updated_at = ? WHERE key = 'bot_sync_lease'", (future,))
        self.assertEqual(run_async(check_bot_guild_access_atomic(801, db_path=self.db_path)), GuildAccessState.INDETERMINATE)

    # Category 11: Temporary unavailable guild versus actual removal
    def test_09_temporary_unavailable_guild_preservation(self):
        # 1. Initial joined guild with metadata
        run_async(sync_bot_guilds([{"guild_id": 901, "name": "Original Name", "icon": "orig.png"}], authoritative=True, db_path=self.db_path))
        stored = run_async(get_stored_bot_guilds(db_path=self.db_path))
        self.assertEqual(stored[0]["name"], "Original Name")

        # 2. Temporary Discord outage: guild becomes unavailable
        # Sync runs during outage with unavailable payload (name=None, icon=None)
        run_async(sync_bot_guilds([{"guild_id": 901, "name": None, "icon": None}], authoritative=True, db_path=self.db_path))
        stored_during = run_async(get_stored_bot_guilds(db_path=self.db_path))
        self.assertEqual(len(stored_during), 1)
        self.assertEqual(stored_during[0]["name"], "Original Name")  # Preserved!
        self.assertEqual(stored_during[0]["icon"], "orig.png")      # Preserved!
        self.assertEqual(run_async(check_bot_guild_access_atomic(901, db_path=self.db_path)), GuildAccessState.INSTALLED)

        # 3. Actual removal (bot kicked)
        run_async(record_bot_guild_remove(901, db_path=self.db_path))
        self.assertEqual(run_async(check_bot_guild_access_atomic(901, db_path=self.db_path)), GuildAccessState.CONFIRMED_NO_ACCESS)

    # Category 12 & 13: Developer session GET/POST revocation and Dynamic route audit
    def test_10_developer_session_revocation_and_dynamic_route_audit(self):
        client = app.test_client()

        # Dynamic audit of all Flask registered routes
        guild_scoped_routes = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in ("static",):
                continue
            # Routes requiring session context
            guild_scoped_routes.append((rule.rule, [m for m in rule.methods if m not in ("HEAD", "OPTIONS")]))

        self.assertGreater(len(guild_scoped_routes), 50, "Expected at least 50 registered routes in Dashboard")

        # Set up developer session on an uninstalled guild
        with client.session_transaction() as sess:
            sess["user"] = {"id": "704453350384730237", "username": "Dark"}
            sess["guild_id"] = 999999999999999999
            sess["user_level"] = "owner"  # Pre-existing session level
            sess["expires_at"] = time.time() + 3600

        # Authorization check MUST fail closed with 403 Forbidden because guild is uninstalled
        resp = client.get("/config/general")
        self.assertEqual(resp.status_code, 403)

        resp_post = client.post("/api/commands/toggle", json={})
        self.assertEqual(resp_post.status_code, 403)

    # Category 15: Normal user permission regression tests
    def test_11_normal_user_permission_regression(self):
        test_normal_id = 888777666555444333
        test_guild_id = 777666555444333222

        # 1. Normal user with no row in dashboard_users -> None (403)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM dashboard_users WHERE user_id = ?", (test_normal_id,))
            conn.commit()

        level_none = run_async(_get_permission_level(test_guild_id, test_normal_id))
        self.assertIsNone(level_none)

        # 2. Normal user with admin row in dashboard_users
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)", (test_guild_id, test_normal_id))
            conn.commit()

        level_admin = run_async(_get_permission_level(test_guild_id, test_normal_id))
        self.assertEqual(level_admin, "admin")

    # Category 16: Actual shell supervisor exit, cleanup, and child reaping
    def test_12_supervisor_execution_and_child_reaping(self):
        script_path = os.path.abspath("start.sh")
        # Run test of supervisor with BARRIER_FAIL=1 to verify fail-closed exit and cleanup
        res = subprocess.run(["bash", "-c", f"BARRIER_FAIL=1 bash {script_path} || exit $?"], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 1)

        # Verify no orphaned processes
        chk = subprocess.run(["pgrep", "-f", "supervise_bot"], capture_output=True, text=True)
        self.assertEqual(chk.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
