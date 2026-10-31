"""Durable role-retention obligations (R2).

A Booster reward role is retired when its source ends. Discord exposes
`premium_since` but no per-member boost count, so eligibility can be
UNKNOWN: the member is an active booster and already wears the role, but
nothing proves they still qualify for it.

Keeping the role then is correct. What was wrong is that the decision lived
only in process memory, so a restart lost it and the role stayed on the
member forever with nothing left to retry. These tests pin the durable
obligation that replaces that memory, its retry behaviour, and the reader
classification that keeps an obligation from being mistaken for a grant.

This is NEW work, not a restoration of the lost Phase 1.5 patch.
"""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from phase1_support import GUILD, USER, DB_PATH, reset_database, execute, rows
from test_timed_ownership import purchase_ctx, role_stub
from cogs.twitch import Twitch
from utils import timed_ownership as TO
from utils import creator_notify_engine as engine
from utils import level_test_reset
from utils.level_claims import _role_granted_elsewhere

ROLE = 557


class RetentionTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    def unknown_booster(self, role, *, worn=True):
        """A live `discord.Member`-shaped stub: active booster, no per-member
        boost count, so eligibility cannot be determined."""
        _, person, bot, guild = purchase_ctx(
            granted_role=role, roles_held=[role] if worn else [])
        person.premium_since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Deliberately NOT set: discord.Member has no per-member count.
        assert not hasattr(person, "premium_subscription_count")
        execute(
            "INSERT INTO boost_config(guild_id,enabled,boost2_role_id) "
            "VALUES (?,1,?)", (GUILD, role.id))
        return person, bot, guild

    async def go_offline(self, bot, guild, role_id=ROLE, user_id=USER):
        with patch.object(engine, "get_watch_group",
                          AsyncMock(return_value=None)), \
                patch.object(engine, "end_live_session", AsyncMock()):
            await Twitch._go_offline(
                SimpleNamespace(bot=bot), guild, GUILD, 123, "scratch",
                role_id, user_id)

    def obligation_rows(self):
        return rows(
            "SELECT source, role_id, item_type, expires_at FROM timed_holdings "
            "WHERE guild_id=? AND user_id=?", (GUILD, USER))


# ── 1. Obligation lifecycle ───────────────────────────────────────────────

class ObligationLifecycleTests(RetentionTestBase):

    async def test_obligation_recorded_when_eligibility_unknown(self):
        """The regression the original reproducer demonstrated: the role is
        kept AND a durable obligation now exists."""
        role = role_stub(ROLE, "Shared Twitch/Booster")
        person, bot, guild = self.unknown_booster(role)
        await self.go_offline(bot, guild)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(
            self.obligation_rows(),
            [("booster_retention", ROLE, "role_retention",
              TO.RETENTION_NEVER_EXPIRES)])

    async def test_obligation_is_idempotent(self):
        """Re-running the same decision must not duplicate work or reset the
        retry counters."""
        role = role_stub(ROLE)
        person, bot, guild = self.unknown_booster(role)
        await self.go_offline(bot, guild)
        await self.go_offline(bot, guild)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])

    async def test_no_obligation_when_eligibility_is_known(self):
        """A known, satisfied entitlement is a fact, not a decision, so no
        obligation is owed."""
        role = role_stub(ROLE)
        person, bot, guild = self.unknown_booster(role)
        person.premium_subscription_count = 2      # eligibility is KNOWN
        await self.go_offline(bot, guild)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

    async def test_no_obligation_when_role_is_actually_removed(self):
        """A non-booster loses the role; there is nothing to retain."""
        role = role_stub(ROLE)
        _, person, bot, guild = purchase_ctx(
            granted_role=role, roles_held=[role])
        person.premium_since = None
        await self.go_offline(bot, guild)
        person.remove_roles.assert_awaited_once()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

    async def test_obligation_never_expires(self):
        """The expiry sweep must never be able to collect an obligation."""
        role = role_stub(ROLE)
        _, bot, guild = self.unknown_booster(role)
        await self.go_offline(bot, guild)
        self.assertEqual(await TO.expired_holdings(limit=None), [])
        self.assertFalse(
            TO.is_expired(TO.RETENTION_NEVER_EXPIRES))


# ── 2. Retry behaviour (the reconciler) ──────────────────────────────────

class ReconcilerTests(RetentionTestBase):

    async def _obligation(self):
        role = role_stub(ROLE)
        person, bot, guild = self.unknown_booster(role)
        await self.go_offline(bot, guild)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])
        return person, bot, guild

    async def test_removes_role_when_eligibility_resolves_against_member(self):
        person, bot, guild = await self._obligation()
        person.premium_since = None        # now known NOT to be a booster
        stats = await TO.reconcile_role_retentions(bot=bot)
        person.remove_roles.assert_awaited_once()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(stats["removed"], 1)

    async def test_keeps_obligation_while_eligibility_stays_unknown(self):
        person, bot, guild = await self._obligation()
        stats = await TO.reconcile_role_retentions(bot=bot)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])
        self.assertEqual(stats["kept"], 1)

    async def test_member_gone_satisfies_without_any_discord_call(self):
        person, bot, guild = await self._obligation()
        guild.get_member = lambda uid: None
        stats = await TO.reconcile_role_retentions(bot=bot)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(stats["satisfied"], 1)

    async def test_role_gone_satisfies_without_any_discord_call(self):
        person, bot, guild = await self._obligation()
        guild.get_role = lambda rid: None
        stats = await TO.reconcile_role_retentions(bot=bot)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(stats["satisfied"], 1)

    async def test_role_already_absent_satisfies(self):
        person, bot, guild = await self._obligation()
        person.roles = []
        stats = await TO.reconcile_role_retentions(bot=bot)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(stats["satisfied"], 1)

    async def test_member_becomes_entitled_satisfies(self):
        person, bot, guild = await self._obligation()
        person.premium_subscription_count = 2     # now provably a booster
        stats = await TO.reconcile_role_retentions(bot=bot)
        person.remove_roles.assert_not_awaited()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(stats["satisfied"], 1)

    async def test_remove_failure_keeps_obligation_and_records_the_error(self):
        """A failed removal must leave the obligation in place so the next
        pass retries, and must record why."""
        person, bot, guild = await self._obligation()
        person.premium_since = None
        person.remove_roles = AsyncMock(
            side_effect=RuntimeError("discord 503"))
        stats = await TO.reconcile_role_retentions(bot=bot)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])
        self.assertEqual(stats["kept"], 1)
        meta = json.loads(rows("SELECT metadata FROM timed_holdings")[0][0])
        self.assertEqual(meta["attempts"], 1)
        self.assertIn("discord 503", meta["last_error"])
        # A second failure increments rather than resets.
        await TO.reconcile_role_retentions(bot=bot)
        meta = json.loads(rows("SELECT metadata FROM timed_holdings")[0][0])
        self.assertEqual(meta["attempts"], 2)

    async def test_reconcile_recovers_after_transient_failure(self):
        person, bot, guild = await self._obligation()
        person.premium_since = None
        person.remove_roles = AsyncMock(side_effect=RuntimeError("503"))
        await TO.reconcile_role_retentions(bot=bot)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])
        person.remove_roles = AsyncMock()          # Discord recovers
        stats = await TO.reconcile_role_retentions(bot=bot)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(stats["removed"], 1)

    async def test_reconcile_survives_a_restart(self):
        """The point of the whole change: a fresh in-memory view (a new bot
        object) still sees the obligation."""
        person, bot, guild = await self._obligation()
        fresh_bot = SimpleNamespace(get_guild=lambda gid: guild)
        stats = await TO.reconcile_role_retentions(bot=fresh_bot)
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])


# ── 3. Reset / read behaviour (the confirmed reader defect) ───────────────

class ReaderClassificationTests(RetentionTestBase):

    async def _obligation(self):
        role = role_stub(ROLE)
        _, bot, guild = self.unknown_booster(role)
        await self.go_offline(bot, guild)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])

    async def test_retention_is_not_counted_as_a_role_other_grant(self):
        """The confirmed defect: level_test_reset.py read timed_holdings with
        only guild/user filters, so a retention row was reported as a 'timed
        holding' that justifies keeping the role."""
        await self._obligation()
        report = await level_test_reset.inspect_member(GUILD, USER)
        self.assertNotIn(
            str(ROLE), report["role_other_grants"],
            "an obligation is not a grant and must not appear here")

    async def test_retention_is_reported_separately(self):
        """Excluded from the grants, but still visible to the operator."""
        await self._obligation()
        report = await level_test_reset.inspect_member(GUILD, USER)
        self.assertEqual(
            report["role_retention_obligations"],
            [{"holding_id": 1, "role_id": ROLE,
              "item_name": "Booster role retention"}])

    async def test_get_timed_holdings_excludes_retention(self):
        """An obligation is not something the member owns."""
        await self._obligation()
        self.assertEqual(await TO.get_timed_holdings(GUILD, USER), [])

    async def test_get_timed_holding_by_key_excludes_retention(self):
        await self._obligation()
        holding_id = rows("SELECT id FROM timed_holdings")[0][0]
        self.assertIsNone(await TO.get_timed_holding(f"h:{holding_id}"))

    async def test_delete_timed_holding_refuses_retention(self):
        """The UI delete path must not be able to drop an obligation."""
        await self._obligation()
        holding_id = rows("SELECT id FROM timed_holdings")[0][0]
        self.assertFalse(await TO.delete_timed_holding(f"h:{holding_id}"))
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])

    async def test_role_still_entitled_excludes_retention(self):
        await self._obligation()
        self.assertIsNone(
            await TO.role_still_entitled(GUILD, USER, ROLE))

    async def test_shop_role_still_owned_excludes_retention(self):
        await self._obligation()
        self.assertIsNone(
            await TO.shop_role_still_owned(GUILD, USER, ROLE))

    async def test_level_claims_keeps_the_role_for_a_pending_obligation(self):
        """The one reader that must RECOGNIZE an obligation: the bot still
        owes the role, so no reversal may take it away."""
        await self._obligation()
        async with aiosqlite.connect(DB_PATH) as db:
            reason = await _role_granted_elsewhere(
                db, GUILD, USER, ROLE, exclude_claim_id=0)
        self.assertIn("role-retention obligation", reason)

    async def test_cleanup_expired_holdings_leaves_the_obligation_alone(self):
        """The periodic sweep must not delete or expire an obligation, and
        its published return shape must not change."""
        role = role_stub(ROLE)
        _, bot, guild = self.unknown_booster(role)
        await self.go_offline(bot, guild)
        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats, {"removed": 0, "kept": 0, "errors": []})
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])

    async def test_inventory_view_excludes_retention(self):
        """`get_inventory` builds its unified view on `get_timed_holdings`,
        so it inherits the exclusion."""
        from utils import inventory
        await self._obligation()
        entries = await inventory.get_inventory(
            GUILD, USER, include_timed=True)
        self.assertEqual(
            [e for e in entries
             if e.get("item_type") == "role_retention"], [])

    async def test_reader_policy_covers_every_production_reader(self):
        """Guard against the audit that sampled instead of enumerating: every
        reader named in the policy must still exist in the source."""
        import pathlib
        import re
        source = {}
        for path in pathlib.Path("utils").glob("*.py"):
            source[path.name] = path.read_text()
        checked = 0
        for location, _policy, _why in TO.RETENTION_READER_POLICY:
            location_path, _, func = location.partition(":")
            if not func:
                continue
            filename = pathlib.Path(location_path).name
            if filename not in source:
                self.fail(f"{location}: file {filename} not found")
            if not re.search(rf"\b{re.escape(func)}\b", source[filename]):
                self.fail(f"{location}: function not found in {filename}")
            checked += 1
        self.assertGreaterEqual(checked, 10)


if __name__ == "__main__":
    unittest.main()
