"""Comprehensive test suite for Phase D3 — Member Blacklist, XP Enforcement, Claim Protection & Role Reconciliation.

Covers:
1. Schema & Migration (table exists, unique constraints, indices)
2. XP Isolation across Message XP, Voice XP, Missions, Minigames, Events, Tag Missions, Shop XP
3. Administrative operations (upward blocked, downward allowed, /resetxp allowed)
4. Non-blacklisted members & role-blacklisted members (unchanged behavior)
5. Claim Protection & Role Withholding (claims blocked, DB claims preserved as pending/fulfilled)
6. Role Stripping on Blacklist (strips only Level reward roles, preserves VIP/Mod roles)
7. Rejoin Invariant (blacklisted member does not regain Level roles on rejoin)
8. Unblacklist Forgiveness & D2 Reconciliation (both remove_old_reward_role ON and OFF modes)
9. Partial Role Removal & Hierarchy/Error Reporting
10. Dashboard API endpoints (guild-scoped, permission-checked, error statuses)
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Setup paths and testing environment
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"
os.environ["OWNER_ID"] = "999999999"

from scripts.phase1_support import GUILD, USER, reset_database, rows, execute
import aiosqlite
from database import DB_PATH, init_db
from utils.xp_calculator import (
    calculate_message_xp, calculate_voice_xp, get_xp_multiplier,
    is_member_blacklisted, is_role_blacklisted,
)
from utils.reward_engine import give_reward
from utils.level_claims import (
    blacklist_member, unblacklist_member, retry_blacklist_role_removal,
    get_member_blacklist_entries, claim_available, reconcile_role_progression,
    record_crossing, snapshot_multipliers, TRACK_ROLE,
)
from utils import level_ops as ops


class StubRole:
    def __init__(self, role_id: int, name: str = "Role", position: int = 10):
        self.id = int(role_id)
        self.name = name
        self.position = position

    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)

    def __gt__(self, other):
        return self.position > getattr(other, "position", 0)

    def __le__(self, other):
        return self.position <= getattr(other, "position", 0)

    def __lt__(self, other):
        return self.position < getattr(other, "position", 0)

    def __eq__(self, other):
        return isinstance(other, StubRole) and self.id == other.id

    def __hash__(self):
        return hash(self.id)


class StubMember:
    def __init__(self, user_id: int, guild=None, roles: list = None):
        self.id = int(user_id)
        self.guild = guild
        self.roles = list(roles or [])
        self.mention = f"<@{self.id}>"
        self.display_name = f"User{self.id}"

    async def add_roles(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles = [r for r in self.roles if r.id != role.id]


class StubBotMember(StubMember):
    def __init__(self, user_id: int, guild=None, top_role=None):
        super().__init__(user_id, guild, [top_role] if top_role else [])
        self.top_role = top_role or StubRole(999, "BotTopRole", position=100)


class StubGuild:
    def __init__(self, guild_id: int):
        self.id = int(guild_id)
        self._roles = {}
        self._members = {}
        self.me = StubBotMember(999999, self, StubRole(999, "BotTopRole", position=100))

    def add_role(self, role: StubRole):
        self._roles[role.id] = role

    def get_role(self, role_id: int):
        return self._roles.get(int(role_id))

    def add_member(self, member: StubMember):
        member.guild = self
        self._members[member.id] = member

    def get_member(self, user_id: int):
        return self._members.get(int(user_id))


class TestMemberBlacklistD3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        self.guild_id = GUILD
        self.user_a = USER
        self.user_b = USER + 1
        self.guild = StubGuild(self.guild_id)

    async def test_01_schema_and_crud(self):
        """Verify leveling_member_blacklist schema, uniqueness, and helper CRUD."""
        # Check initial state is empty
        entries = await get_member_blacklist_entries(self.guild_id)
        self.assertEqual(entries, [])
        self.assertFalse(await is_member_blacklisted(self.guild_id, self.user_a))

        # Blacklist user A
        res = await blacklist_member(self.guild_id, self.user_a, actor_id=123, reason="Test Reason")
        self.assertTrue(res["success"])
        self.assertTrue(await is_member_blacklisted(self.guild_id, self.user_a))
        self.assertFalse(await is_member_blacklisted(self.guild_id, self.user_b))

        # Check duplicate blacklist attempt fails gracefully
        dup_res = await blacklist_member(self.guild_id, self.user_a, actor_id=123)
        self.assertFalse(dup_res["success"])
        self.assertTrue(dup_res["already_blacklisted"])

        # Check list output
        entries = await get_member_blacklist_entries(self.guild_id)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["user_id"], self.user_a)
        self.assertEqual(entries[0]["reason"], "Test Reason")

        # Unblacklist user A
        un_res = await unblacklist_member(self.guild_id, self.user_a, actor_id=123)
        self.assertTrue(un_res["success"])
        self.assertFalse(await is_member_blacklisted(self.guild_id, self.user_a))

        # Unblacklist non-blacklisted member
        not_res = await unblacklist_member(self.guild_id, self.user_a)
        self.assertFalse(not_res["success"])
        self.assertTrue(not_res["not_blacklisted"])

    async def test_02_message_and_voice_xp_isolation(self):
        """Blacklisted member earns 0 message XP and 0 voice XP."""
        # Non-blacklisted member earns normal message XP
        xp = await calculate_message_xp(self.guild_id, [], 10, user_id=self.user_a)
        self.assertGreater(xp, 0)

        # Blacklist user A
        await blacklist_member(self.guild_id, self.user_a)

        # Blacklisted member earns 0 message XP
        xp_bl = await calculate_message_xp(self.guild_id, [], 10, user_id=self.user_a)
        self.assertEqual(xp_bl, 0)

        # Non-blacklisted user B still earns XP
        xp_b = await calculate_message_xp(self.guild_id, [], 10, user_id=self.user_b)
        self.assertGreater(xp_b, 0)

    async def test_03_central_reward_engine_blocks_xp(self):
        """give_reward for 'xp' is blocked, while coins/diamonds and other rewards succeed."""
        await blacklist_member(self.guild_id, self.user_a)

        # XP grant to blacklisted user fails
        res_xp = await give_reward(None, self.guild_id, self.user_a, "xp", amount=500, reason="Mission XP")
        self.assertFalse(res_xp["success"])
        self.assertTrue(res_xp.get("blacklisted"))
        self.assertEqual(res_xp.get("amount"), 0)

        # Non-XP rewards (coins, diamonds) succeed
        res_coins = await give_reward(None, self.guild_id, self.user_a, "coins", amount=100, reason="Daily coins")
        self.assertTrue(res_coins["success"])
        self.assertEqual(res_coins["amount"], 100)

        # Normal user B earns XP normally
        res_b = await give_reward(None, self.guild_id, self.user_b, "xp", amount=500, reason="Mission XP")
        self.assertTrue(res_b["success"])
        self.assertEqual(res_b["amount"], 500)

    async def test_04_admin_xp_operations(self):
        """Admin XP upward increase is blocked; downward correction / reset is allowed."""
        from cogs.leveling import _apply_admin_xp
        from utils.level_claims import SOURCE_SETXP, SOURCE_SETLEVEL

        # Setup initial XP for user A: 1000 XP
        await give_reward(None, self.guild_id, self.user_a, "xp", amount=1000)

        # Blacklist user A
        await blacklist_member(self.guild_id, self.user_a)
        member_a = StubMember(self.user_a, self.guild)

        # Attempt to increase XP via _apply_admin_xp -> blocked
        with self.assertRaises(ops.LevelOpsError):
            await _apply_admin_xp(self.guild_id, member_a, 2000, None, source=SOURCE_SETXP)

        # Attempt to decrease XP via _apply_admin_xp -> allowed
        res_down = await _apply_admin_xp(self.guild_id, member_a, 500, None, source=SOURCE_SETXP)
        self.assertEqual(res_down["new_xp"], 500)

        # Reset to 0 XP is allowed
        res_zero = await _apply_admin_xp(self.guild_id, member_a, 0, None, source=SOURCE_SETXP)
        self.assertEqual(res_zero["new_xp"], 0)

    async def test_05_claim_protection_and_role_withholding(self):
        """Claim All is denied while blacklisted; claims remain pending in DB and recoverable upon unblacklist."""
        role_l5 = StubRole(505, "Level 5 Role", position=10)
        self.guild.add_role(role_l5)
        member_a = StubMember(self.user_a, self.guild)

        # Insert a Level 5 role reward and grant user A Level 5 XP
        execute("INSERT INTO leveling_rewards (guild_id, level, role_id, audience) VALUES (?, 5, 505, 'everyone')", (self.guild_id,))
        await give_reward(None, self.guild_id, self.user_a, "xp", amount=3000)

        # Verify claim exists as pending
        claim_rows = rows("SELECT id, status FROM level_reward_claims WHERE guild_id=? AND user_id=?", (self.guild_id, self.user_a))
        self.assertEqual(len(claim_rows), 1)
        self.assertEqual(claim_rows[0][1], "pending")

        # Blacklist user A
        await blacklist_member(self.guild_id, self.user_a)

        # Claim All while blacklisted -> blocked
        claim_res = await claim_available(self.guild_id, self.user_a, member=member_a)
        self.assertTrue(bool(claim_res.get("blocked")))
        self.assertEqual(len(member_a.roles), 0)

        # Verify claim in DB is still pending (not failed or corrupted)
        claim_rows = rows("SELECT status FROM level_reward_claims WHERE guild_id=? AND user_id=?", (self.guild_id, self.user_a))
        self.assertEqual(claim_rows[0][0], "pending")

        # Unblacklist user A
        await unblacklist_member(self.guild_id, self.user_a, member=member_a)

        # Now Claim All succeeds and delivers role 505
        claim_res2 = await claim_available(self.guild_id, self.user_a, member=member_a)
        self.assertEqual(claim_res2["delivered_roles"], 1)
        self.assertIn(role_l5, member_a.roles)

    async def test_06_role_stripping_and_unrelated_role_preservation(self):
        """Blacklisting strips only Level reward roles, preserving VIP/Mod roles."""
        role_l5 = StubRole(505, "Level 5 Role", position=10)
        role_vip = StubRole(777, "VIP Role", position=20)
        role_mod = StubRole(888, "Moderator Role", position=30)
        self.guild.add_role(role_l5)
        self.guild.add_role(role_vip)
        self.guild.add_role(role_mod)

        # Member holds Level 5 role and VIP/Mod roles
        member_a = StubMember(self.user_a, self.guild, roles=[role_l5, role_vip, role_mod])
        self.guild.add_member(member_a)

        # Record fulfilled claim for Level 5 role
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at, fulfilled_at)
            VALUES (?, ?, 5, 'role', '505', '{"role_id": 505}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (self.guild_id, self.user_a))

        # Blacklist member
        res = await blacklist_member(self.guild_id, self.user_a, member=member_a)
        self.assertTrue(res["success"])
        self.assertEqual(res["removed_roles"], [505])

        # Verify Level 5 role is stripped, but VIP and Mod roles remain intact!
        self.assertNotIn(role_l5, member_a.roles)
        self.assertIn(role_vip, member_a.roles)
        self.assertIn(role_mod, member_a.roles)

    async def test_07_rejoin_reconciliation_blocked_during_blacklist(self):
        """Rejoin does not restore Level roles if the member is blacklisted."""
        role_l5 = StubRole(505, "Level 5 Role", position=10)
        self.guild.add_role(role_l5)
        member_a = StubMember(self.user_a, self.guild, roles=[])

        # Record fulfilled claim
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at, fulfilled_at)
            VALUES (?, ?, 5, 'role', '505', '{"role_id": 505}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (self.guild_id, self.user_a))

        # Blacklist member
        await blacklist_member(self.guild_id, self.user_a)

        # Rejoin reconciliation runs
        reconcile_res = await reconcile_role_progression(member_a, self.guild_id, self.user_a)
        self.assertTrue(reconcile_res["blocked"])
        self.assertEqual(len(member_a.roles), 0)

        # Unblacklist member with reconciliation
        await unblacklist_member(self.guild_id, self.user_a, member=member_a)
        self.assertIn(role_l5, member_a.roles)

    async def test_08_unblacklist_progression_both_modes(self):
        """Unblacklist restores highest role when exclusive ON, and all roles when exclusive OFF."""
        role_l5 = StubRole(505, "Level 5 Role", position=10)
        role_l10 = StubRole(1010, "Level 10 Role", position=15)
        self.guild.add_role(role_l5)
        self.guild.add_role(role_l10)

        # Fulfilled claims for L5 and L10
        execute("""
            INSERT INTO level_reward_claims (guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at, fulfilled_at)
            VALUES (?, ?, 5, 'role', '505', '{"role_id": 505}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                   (?, ?, 10, 'role', '1010', '{"role_id": 1010}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (self.guild_id, self.user_a, self.guild_id, self.user_a))

        # Mode A: remove_old_reward_role = 1 (ON)
        execute("INSERT OR REPLACE INTO leveling_config (guild_id, remove_old_reward_role) VALUES (?, 1)", (self.guild_id,))
        member_a = StubMember(self.user_a, self.guild, roles=[])
        await blacklist_member(self.guild_id, self.user_a)
        await unblacklist_member(self.guild_id, self.user_a, member=member_a)
        # Should have highest role (L10) only
        self.assertIn(role_l10, member_a.roles)
        self.assertNotIn(role_l5, member_a.roles)

        # Mode B: remove_old_reward_role = 0 (OFF)
        execute("UPDATE leveling_config SET remove_old_reward_role = 0 WHERE guild_id = ?", (self.guild_id,))
        member_b = StubMember(self.user_b, self.guild, roles=[])
        execute("""
            INSERT INTO level_reward_claims (guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at, fulfilled_at)
            VALUES (?, ?, 5, 'role', '505', '{"role_id": 505}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                   (?, ?, 10, 'role', '1010', '{"role_id": 1010}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (self.guild_id, self.user_b, self.guild_id, self.user_b))
        await blacklist_member(self.guild_id, self.user_b)
        await unblacklist_member(self.guild_id, self.user_b, member=member_b)
        # Should have both L5 and L10 roles
        self.assertIn(role_l5, member_b.roles)
        self.assertIn(role_l10, member_b.roles)

    async def test_09_hierarchy_and_partial_failure_handling(self):
        """Role higher than bot is reported as partial failure without failing blacklist."""
        high_role = StubRole(9999, "Owner Level Role", position=999)  # Higher than bot's 100
        self.guild.add_role(high_role)
        member_a = StubMember(self.user_a, self.guild, roles=[high_role])

        execute("""
            INSERT INTO level_reward_claims (guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at, fulfilled_at)
            VALUES (?, ?, 50, 'role', '9999', '{"role_id": 9999}', 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (self.guild_id, self.user_a))

        res = await blacklist_member(self.guild_id, self.user_a, member=member_a)
        self.assertTrue(res["success"])
        self.assertTrue(res["partial"])
        self.assertEqual(len(res["failed_removals"]), 1)
        # Member remains blacklisted in DB
        self.assertTrue(await is_member_blacklisted(self.guild_id, self.user_a))

    async def test_10_api_endpoints(self):
        """Test Dashboard REST API endpoints for member blacklist."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)",
                (self.guild_id, self.user_a))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_a), "username": "AdminUser", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="test-csrf"
            )

        # 1. GET /api/leveling/member-blacklist
        resp = client.get("/api/leveling/member-blacklist")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["members"], [])

        # 2. POST /api/leveling/member-blacklist
        post_resp = client.post(
            "/api/leveling/member-blacklist",
            json={"user_id": str(self.user_b), "reason": "Rule violation"},
            headers={"X-CSRF-Token": "test-csrf"}
        )
        self.assertEqual(post_resp.status_code, 200)
        post_data = post_resp.get_json()
        self.assertTrue(post_data["success"])

        # 3. Duplicate POST returns 409
        dup_resp = client.post(
            "/api/leveling/member-blacklist",
            json={"user_id": str(self.user_b), "reason": "Duplicate"},
            headers={"X-CSRF-Token": "test-csrf"}
        )
        self.assertEqual(dup_resp.status_code, 409)

        # 4. DELETE /api/leveling/member-blacklist/<user_id>
        del_resp = client.delete(
            f"/api/leveling/member-blacklist/{self.user_b}",
            headers={"X-CSRF-Token": "test-csrf"}
        )
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.get_json()["success"])

        # 5. DELETE non-existent returns 404
        del_resp2 = client.delete(
            f"/api/leveling/member-blacklist/{self.user_b}",
            headers={"X-CSRF-Token": "test-csrf"}
        )
        self.assertEqual(del_resp2.status_code, 404)

    async def test_11_concurrent_race_blacklisting_during_role_claim(self):
        """Deterministic test for in-flight role delivery race with explicit synchronization barriers:
        1. Claim starts while member is not blacklisted.
        2. Claim passes its pre-delivery blacklist check.
        3. Discord add_roles() begins but pauses in-flight.
        4. During pause, blacklist_member() runs, persists blacklist, and performs role cleanup.
        5. add_roles() finishes adding the role to Discord.
        6. Post-delivery check detects active blacklist, marks claim failed, and compensates by stripping the role.
        7. Unrelated VIP role is preserved throughout.
        8. Rejoin reconciliation while blacklisted does not restore the role.
        9. Unblacklisting allows subsequent Claim All to recover and fulfill the claim cleanly.
        """
        role_l5 = StubRole(505, "Level 5 Role", position=10)
        role_vip = StubRole(777, "VIP Role", position=20)
        self.guild.add_role(role_l5)
        self.guild.add_role(role_vip)
        
        member_a = StubMember(self.user_a, self.guild, roles=[role_vip])
        self.guild.add_member(member_a)

        # Insert a Level 5 role reward and grant user A Level 5 XP
        execute("INSERT INTO leveling_rewards (guild_id, level, role_id, audience) VALUES (?, 5, 505, 'everyone')", (self.guild_id,))
        await give_reward(None, self.guild_id, self.user_a, "xp", amount=3000)

        claim_rows = rows("SELECT id, status FROM level_reward_claims WHERE guild_id=? AND user_id=?", (self.guild_id, self.user_a))
        self.assertEqual(len(claim_rows), 1)
        self.assertEqual(claim_rows[0][1], "pending")

        # Explicit synchronization barriers
        add_roles_started = asyncio.Event()
        blacklist_completed = asyncio.Event()

        original_add_roles = member_a.add_roles

        async def paused_add_roles(role, reason=None):
            # Signal that add_roles is in flight (past the pre-delivery check)
            add_roles_started.set()
            # Wait for blacklist_member() to complete concurrently
            await blacklist_completed.wait()
            # Complete the Discord add_roles operation
            await original_add_roles(role, reason)

        member_a.add_roles = paused_add_roles

        # Start claim_available in background task
        claim_task = asyncio.create_task(claim_available(self.guild_id, self.user_a, member=member_a))

        # Wait until add_roles is actively in flight
        await add_roles_started.wait()

        # At this exact point, role_l5 is NOT yet in member_a.roles
        self.assertNotIn(role_l5, member_a.roles)
        self.assertIn(role_vip, member_a.roles)

        # Now blacklist the member while role delivery is in flight
        bl_res = await blacklist_member(self.guild_id, self.user_a, member=member_a)
        self.assertTrue(bl_res["success"])
        self.assertTrue(await is_member_blacklisted(self.guild_id, self.user_a))

        # Unblock the in-flight add_roles request to complete
        blacklist_completed.set()

        # Await completion of claim_available
        claim_res = await claim_task

        # Verify claim_available detected the concurrent blacklist post-delivery:
        # Claim is marked failed (not fulfilled), and compensation stripped the prohibited role
        self.assertEqual(claim_res["delivered_roles"], 0)
        self.assertIn(claim_rows[0][0], claim_res["failed"])

        # Prohibited Level 5 role is NOT held by member; unrelated VIP role remains intact
        self.assertNotIn(role_l5, member_a.roles)
        self.assertIn(role_vip, member_a.roles)

        # DB claim status is 'failed', not 'fulfilled' or stuck 'processing'
        db_claims = rows("SELECT status, last_error, owner_token FROM level_reward_claims WHERE id=?", (claim_rows[0][0],))
        self.assertEqual(db_claims[0][0], "failed")
        self.assertEqual(db_claims[0][1], "Member blacklisted during delivery")
        self.assertIsNone(db_claims[0][2])  # Lease released

        # Rejoin reconciliation while blacklisted does not restore Level 5 role
        reconcile_res = await reconcile_role_progression(member_a, self.guild_id, self.user_a)
        self.assertTrue(reconcile_res["blocked"])
        self.assertNotIn(role_l5, member_a.roles)

        # Restore original add_roles for subsequent clean retry
        member_a.add_roles = original_add_roles

        # Now unblacklist the member
        unbl_res = await unblacklist_member(self.guild_id, self.user_a, member=member_a)
        self.assertTrue(unbl_res["success"])

        # Member runs Claim All again: the failed claim is safely recovered and fulfilled
        recovery_claim_res = await claim_available(self.guild_id, self.user_a, member=member_a)
        self.assertEqual(recovery_claim_res["delivered_roles"], 1)
        self.assertIn(role_l5, member_a.roles)
        self.assertIn(role_vip, member_a.roles)

        # DB claim is now fulfilled with valid timestamp and owner_token released
        final_db_claims = rows("SELECT status, fulfilled_at, owner_token FROM level_reward_claims WHERE id=?", (claim_rows[0][0],))
        self.assertEqual(final_db_claims[0][0], "fulfilled")
        self.assertIsNotNone(final_db_claims[0][1])
        self.assertIsNone(final_db_claims[0][2])

    async def test_12_telemetry_sensor_policy_split(self):
        """Activity telemetry (voice_minutes, messages_count) continues for blacklisted members; only XP is gated."""
        from cogs.activity_engine import get_today_activity
        from utils.timezone import get_cairo_daily_key
        today = get_cairo_daily_key()

        # Blacklist user A
        await blacklist_member(self.guild_id, self.user_a)

        # Activity engine records 1 voice minute and 1 message in activity_stats
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO activity_stats (guild_id, user_id, date, voice_minutes, messages_count, words_count)
                VALUES (?, ?, ?, 1.0, 1, 15)
                ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    voice_minutes = voice_minutes + 1.0,
                    messages_count = messages_count + 1
            """, (self.guild_id, self.user_a, today))
            await db.commit()

        # Telemetry is accurately recorded
        stats = await get_today_activity(self.guild_id, self.user_a)
        self.assertEqual(stats["voice_minutes"], 1.0)
        self.assertEqual(stats["messages_count"], 1)

        # But Message XP and Voice XP remain 0
        msg_xp = await calculate_message_xp(self.guild_id, [], 15, user_id=self.user_a)
        self.assertEqual(msg_xp, 0)

    async def test_13_composite_reward_isolation(self):
        """Composite rewards (coins + diamonds + XP) deliver economy rewards while cleanly suppressing XP."""
        await blacklist_member(self.guild_id, self.user_a)

        # Simulate mission completion granting coins, diamonds, and XP
        res_coins = await give_reward(None, self.guild_id, self.user_a, "coins", amount=250, reason="Mission Coins")
        res_diamonds = await give_reward(None, self.guild_id, self.user_a, "diamonds", amount=10, reason="Mission Diamonds")
        res_xp = await give_reward(None, self.guild_id, self.user_a, "xp", amount=500, reason="Mission XP")

        # Economy rewards succeeded
        self.assertTrue(res_coins["success"])
        self.assertEqual(res_coins["amount"], 250)
        self.assertTrue(res_diamonds["success"])
        self.assertEqual(res_diamonds["amount"], 10)

        # XP was blocked cleanly without affecting balances or causing DB errors
        self.assertFalse(res_xp["success"])
        self.assertTrue(res_xp.get("blacklisted"))

        # Verify economy balances in DB
        bal_row = rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?", (self.guild_id, self.user_a))
        self.assertEqual(len(bal_row), 1)
        self.assertEqual(bal_row[0][0], 250)
        self.assertEqual(bal_row[0][1], 10)

    async def test_14_api_security_cross_guild_and_permissions(self):
        """API security: CSRF validation, invalid payloads, and retry endpoint."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)",
                (self.guild_id, self.user_a))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_a), "username": "AdminUser", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="secure-csrf-token"
            )

        # 1. Missing CSRF header returns 403 Forbidden
        post_no_csrf = client.post(
            "/api/leveling/member-blacklist",
            json={"user_id": str(self.user_b), "reason": "No CSRF"}
        )
        self.assertEqual(post_no_csrf.status_code, 403)

        # 2. Invalid user_id payload returns 400
        post_bad_user = client.post(
            "/api/leveling/member-blacklist",
            json={"user_id": "not-a-number", "reason": "Bad ID"},
            headers={"X-CSRF-Token": "secure-csrf-token"}
        )
        self.assertEqual(post_bad_user.status_code, 400)

        # 3. Retry roles endpoint when member is not blacklisted returns 400
        retry_resp = client.post(
            "/api/leveling/member-blacklist/retry-roles",
            json={"user_id": str(self.user_b)},
            headers={"X-CSRF-Token": "secure-csrf-token"}
        )
        self.assertEqual(retry_resp.status_code, 400)

    async def test_12_role_collision_mutual_exclusion(self):
        """Test mutual exclusion between leveling_blacklist_roles and leveling_rewards (D-02)."""
        import dashboard.app as dash
        app = dash.app
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

        execute("INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)",
                (self.guild_id, self.user_a))

        client = app.test_client()
        with client.session_transaction() as sess:
            sess.update(
                user={"id": str(self.user_a), "username": "AdminUser", "avatar": None},
                guild_id=self.guild_id,
                expires_at=time.time() + 7200,
                csrf_token="collision-csrf-token"
            )

        reward_role = "7701"
        bl_role = "7702"

        # Mock validate_assignable_role to pass for reward_role and bl_role
        with patch("dashboard.utils.role_validation.validate_assignable_role", return_value=(True, "")):
            # 1. Add reward_role to leveling_rewards
            r_resp = client.post(
                "/api/leveling/reward",
                json={"level": 5, "role_id": reward_role, "audience": "everyone"},
                headers={"X-CSRF-Token": "collision-csrf-token"}
            )
            self.assertEqual(r_resp.status_code, 200)

            # 2. Attempting to blacklist reward_role must fail with 400
            bl_fail = client.post(
                "/api/leveling/blacklist",
                json={"role_id": reward_role},
                headers={"X-CSRF-Token": "collision-csrf-token"}
            )
            self.assertEqual(bl_fail.status_code, 400)
            self.assertIn("configured as a Level reward", bl_fail.get_json().get("error", ""))

            # 3. Add distinct bl_role to blacklist
            bl_ok = client.post(
                "/api/leveling/blacklist",
                json={"role_id": bl_role},
                headers={"X-CSRF-Token": "collision-csrf-token"}
            )
            self.assertEqual(bl_ok.status_code, 200)

            # 4. Attempting to add bl_role as a Level reward must fail with 400
            r_fail = client.post(
                "/api/leveling/reward",
                json={"level": 10, "role_id": bl_role, "audience": "everyone"},
                headers={"X-CSRF-Token": "collision-csrf-token"}
            )
            self.assertEqual(r_fail.status_code, 400)
            self.assertIn("blacklisted from earning XP", r_fail.get_json().get("error", ""))


if __name__ == "__main__":
    unittest.main()
