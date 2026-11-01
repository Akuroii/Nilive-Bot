"""Targeted regression tests for the PR 105 repair branch."""
import unittest
from unittest.mock import AsyncMock, patch

from phase1_support import reset_database, seed_member, rows
from test_free_shop_items import purchase_context, shop_item
from cogs.shop import process_purchase


class PurchaseTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=100)

    async def test_typeerror_from_deduct_does_not_retry_outside_transaction(self):
        item = shop_item('title', price=10, name='S1', stock=1)
        interaction, _ = purchase_context()
        deduct = AsyncMock(side_effect=TypeError('db'))
        with patch('cogs.shop.safe_deduct', deduct):
            with self.assertRaisesRegex(TypeError, 'db'):
                await process_purchase(interaction, item)
        self.assertEqual(deduct.await_count, 1)
        self.assertIsNotNone(deduct.await_args.kwargs.get('db'))
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
        self.assertEqual(rows('SELECT count(*) FROM purchase_history'), [(0,)])

from types import SimpleNamespace
from datetime import datetime, timezone
import discord
from phase1_support import GUILD, USER, execute
from test_timed_ownership import purchase_ctx, role_stub
from utils import timed_ownership as TO


class RetentionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        self.role = role_stub(557)
        _, self.person, self.bot, self.guild = purchase_ctx(
            granted_role=self.role, roles_held=[self.role])
        self.person.premium_since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        execute('INSERT INTO boost_config(guild_id, enabled, boost2_role_id) VALUES (?,1,?)', (GUILD, 557))
        await TO.record_role_retention(GUILD, USER, 557, reason='test unknown eligibility')

    def count(self):
        return rows("SELECT COUNT(*) FROM timed_holdings WHERE item_type='role_retention'")[0][0]

    async def test_guild_cache_miss_keeps_obligation(self):
        self.bot.get_guild = lambda _id: None
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(), 1)
        self.assertGreater(result['kept'] + result['failed'], 0)

    async def test_member_cache_miss_fetches_member(self):
        self.guild.get_member = lambda _id: None
        self.guild.fetch_member = AsyncMock(return_value=self.person)
        await TO.reconcile_role_retentions(bot=self.bot)
        self.guild.fetch_member.assert_awaited_once_with(USER)
        self.assertEqual(self.count(), 1)

    async def test_member_notfound_satisfies_obligation(self):
        self.guild.get_member = lambda _id: None
        response = SimpleNamespace(status=404, reason='Not Found')
        self.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(response, 'not found'))
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(), 0)
        self.assertEqual(result['satisfied'], 1)

    async def test_fetch_error_keeps_obligation(self):
        self.guild.get_member = lambda _id: None
        self.guild.fetch_member = AsyncMock(side_effect=OSError('network unavailable'))
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(), 1)
        self.assertTrue(result['errors'])

    async def test_fetch_forbidden_keeps_obligation(self):
        self.guild.get_member = lambda _id: None
        response = SimpleNamespace(status=403, reason='Forbidden')
        self.guild.fetch_member = AsyncMock(side_effect=discord.Forbidden(response, 'forbidden'))
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(), 1)
        self.assertTrue(result['errors'])

class BoosterDefaultConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_color_obligation_respects_default_and_disabled_config(self):
        for enabled in (None, 1, 0):
            with self.subTest(enabled=enabled):
                await reset_database()
                role = role_stub(558)
                _, person, bot, _ = purchase_ctx(granted_role=role, roles_held=[role])
                person.premium_since = datetime(2026, 1, 1, tzinfo=timezone.utc)
                execute('INSERT INTO boost_color_roles (guild_id,role_id,role_name,requires_boost_level) VALUES (?,?,?,2)', (GUILD, role.id, 'Test'))
                if enabled is not None:
                    execute('INSERT INTO boost_config(guild_id,enabled) VALUES (?,?)', (GUILD, enabled))
                await TO.role_removal_blocked(GUILD, USER, role.id, retiring='twitch', bot=bot, member=person)
                obligations = rows("SELECT role_id FROM timed_holdings WHERE item_type='role_retention'")
                self.assertEqual(obligations, [] if enabled == 0 else [(role.id,)])

from phase1_support import mutation_snapshot
import test_phase1_api as phase1_api


class ShopNegativeValidationTests(unittest.TestCase):
    setUp = phase1_api.ListingApiTests.setUp
    post = phase1_api.ListingApiTests.post

    def test_invalid_product_combinations_do_not_mutate_database(self):
        cases = [
            dict(type='title', duration_hours=2),
            dict(type='title', xp_boost_multiplier=2),
            dict(type='custom', role_id=5),
            dict(type='custom', xp_boost_multiplier=2),
            dict(type='potion', role_id=5, xp_boost_multiplier=2, duration_hours=2),
            dict(type='color', role_id=5, xp_boost_multiplier=2),
            dict(type='prestige', prestige_tier=1, duration_hours=2),
        ]
        for case in cases:
            with self.subTest(case=case):
                before = mutation_snapshot()
                response = self.post(dict(name='invalid', price=10, **case))
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(mutation_snapshot(), before)

class StuckRefundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=90)

    async def test_failed_refund_is_visible_and_records_error(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from phase1_support import DB_PATH
        import json
        op = await TO.open_purchase(GUILD, USER, item_id=None,
                                    item_name='test refund', currency='balance', amount=10)
        await TO.update_purchase(op, status=TO.PURCHASE_FAILED, charged=1)
        with patch('utils.economy_safe.safe_credit', new=AsyncMock(side_effect=RuntimeError('credit outage'))):
            stats = await TO.reconcile_purchases()
        self.assertGreater(stats['errors'], 0)
        journal = await TO.get_purchase(op)
        self.assertIn('credit outage', journal['detail'] or '')
        out = StringIO()
        with redirect_stdout(out):
            rc = await TO._cli_async(['--db', str(DB_PATH), '--json'])
        self.assertEqual(rc, 0)
        self.assertIn(op, [r['op_id'] for r in json.loads(out.getvalue())['operations']])
        for filters in (['--status', 'failed'], ['--all-statuses']):
            out = StringIO()
            with redirect_stdout(out):
                rc = await TO._cli_async(['--db', str(DB_PATH), '--json', *filters])
            self.assertEqual(rc, 0)
            self.assertIn(op, [r['op_id'] for r in json.loads(out.getvalue())['operations']])
        self.assertEqual(rows('SELECT balance FROM economy'), [(90,)])
