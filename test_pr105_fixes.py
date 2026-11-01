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
        self.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(response, {'code': 10007, 'message': 'Unknown Member'}))
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(), 0)
        self.assertEqual(result['satisfied'], 1)

    async def test_unknown_guild_notfound_keeps_obligation(self):
        self.guild.get_member = lambda _id: None
        response = SimpleNamespace(status=404, reason='Not Found')
        self.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(response, {'code':10004, 'message':'Unknown Guild'}))
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(),1)
        self.assertTrue(result['errors'])

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

class RefundTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=90)

    async def test_typeerror_from_credit_is_not_retried_outside_transaction(self):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='refund', currency='balance', amount=10)
        await TO.update_purchase(op, status=TO.PURCHASE_FAILED, charged=1)
        credit = AsyncMock(side_effect=TypeError('db'))
        with patch('utils.economy_safe.safe_credit', credit):
            result = await TO.refund_purchase(op)
        self.assertFalse(result['complete'])
        self.assertEqual(credit.await_count, 1)
        self.assertIsNotNone(credit.await_args.kwargs.get('db'))
        self.assertEqual(rows('SELECT balance FROM economy'), [(90,)])
        self.assertFalse((await TO.get_purchase(op))['refunded'])

    async def test_typeerror_from_stock_restore_is_not_retried(self):
        item = shop_item('title',price=10,name='refund stock',stock=2)
        execute('UPDATE shop_items SET current_stock=1 WHERE id=?',(item,))
        op = await TO.open_purchase(GUILD,USER,item_id=item,item_name='refund stock',currency='balance',amount=10)
        await TO.update_purchase(op,status=TO.PURCHASE_FAILED,stock_reserved=1)
        restore=AsyncMock(side_effect=TypeError('db'))
        with patch.object(TO,'restore_stock',restore):
            result=await TO.refund_purchase(op)
        self.assertFalse(result['complete'])
        self.assertEqual(restore.await_count,1)
        self.assertEqual(rows('SELECT current_stock FROM shop_items WHERE id=?',(item,)),[(1,)])
        self.assertFalse((await TO.get_purchase(op))['stock_returned'])

    async def test_typeerror_from_marker_does_not_retry_and_rolls_back_credit(self):
        op=await TO.open_purchase(GUILD,USER,item_id=None,item_name='marker',currency='balance',amount=10)
        await TO.update_purchase(op,status=TO.PURCHASE_FAILED,charged=1)
        update=AsyncMock(side_effect=TypeError('db'))
        with patch.object(TO,'update_purchase',update):
            result=await TO.refund_purchase(op)
        self.assertFalse(result['complete'])
        self.assertEqual(update.await_count,1)
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertFalse((await TO.get_purchase(op))['refunded'])

class TitleRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        TO._PENDING_DELIVERY_ROLLBACKS.clear()

    async def _run(self, other):
        from test_purchase_grant_ledger import open_op, deliver, spec_for, refund_and_compensate
        from utils.reward_engine import give_reward
        from utils.title_engine import equip_title
        name = 'Rollback Title'
        if other:
            self.assertTrue((await give_reward(None,GUILD,USER,'item',amount=1,item_name=name,item_type='title',source='mission'))['success'])
        op = await open_op(name=name)
        delivered = await deliver(op,name=name,item_type='title')
        self.assertTrue((await equip_title(GUILD,USER,name))['success'])
        res = await refund_and_compensate(op,spec_for(op,name=name,internal_type='title', delivered=delivered['delivered_inventory']))
        self.assertTrue(res['complete'],res)
        self.assertEqual(rows('SELECT item_name FROM equipped_titles WHERE guild_id=? AND user_id=?',(GUILD,USER)), [(name,)] if other else [])
        self.assertEqual(rows('SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name=?',(GUILD,USER,name)),[(1 if other else 0,)])

    async def test_unowned_title_is_unequipped(self): await self._run(False)
    async def test_independent_title_ownership_stays_equipped(self): await self._run(True)

class ManualRefundSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=90)

    async def op(self):
        op = await TO.open_purchase(GUILD,USER,item_id=None,item_name='F3',currency='balance',amount=10)
        await TO.update_purchase(op,status=TO.PURCHASE_UNCERTAIN,charged=1)
        return op

    async def test_live_grant_blocks_refund_and_appears_in_inspect(self):
        from test_purchase_grant_ledger import deliver
        op = await self.op()
        await deliver(op,name='F3')
        before = rows('SELECT balance FROM economy'), rows('SELECT quantity FROM inventory_items')
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'],result)
        self.assertEqual((rows('SELECT balance FROM economy'),rows('SELECT quantity FROM inventory_items')),before)
        evidence = await TO.inspect_purchase_operation(op)
        self.assertEqual(evidence['purchase_grant']['item_name'],'F3')

    async def test_live_grant_blocks_stock_restoration_as_well(self):
        from test_purchase_grant_ledger import deliver
        item = shop_item('title',price=10,name='F3 Stock',stock=2)
        execute('UPDATE shop_items SET current_stock=1 WHERE id=?',(item,))
        op = await TO.open_purchase(GUILD,USER,item_id=item,item_name='F3 Stock',currency='balance',amount=10)
        await TO.update_purchase(op,status=TO.PURCHASE_UNCERTAIN,charged=1,stock_reserved=1)
        await deliver(op,name='F3 Stock',item_type='title')
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'],result)
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items WHERE id=?',(item,)),[(1,)])
        self.assertEqual(rows('SELECT quantity FROM inventory_items'),[(1,)])

    async def test_linked_holding_blocks_refund(self):
        op = await self.op()
        await TO.grant_timed_holding(GUILD,USER,'F3',expires_at='2035-01-01T00:00:00+00:00',metadata={'origin_op_id':op},source='shop')
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'],result)
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertEqual(rows('SELECT count(*) FROM timed_holdings'),[(1,)])

    async def test_grant_created_between_precheck_and_refund_is_rechecked(self):
        from test_purchase_grant_ledger import deliver
        op = await self.op()
        real_refund = TO.refund_purchase
        async def race(op_id, **kwargs):
            await deliver(op_id, name='F3')
            return await real_refund(op_id, **kwargs)
        with patch.object(TO, 'refund_purchase', side_effect=race):
            result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'], result)
        self.assertEqual(rows('SELECT balance FROM economy'), [(90,)])
        self.assertEqual(rows('SELECT quantity FROM inventory_items'), [(1,)])

    async def test_linked_boost_blocks_refund(self):
        from utils.xp_calculator import grant_xp_boost_details
        op = await self.op()
        await grant_xp_boost_details(GUILD,USER,2.0,2,source='shop',op_id=op)
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'],result)
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertEqual(rows('SELECT count(*) FROM leveling_active_boosts'),[(1,)])

    async def test_no_goods_refund_still_succeeds(self):
        op = await self.op()
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertTrue(result['success'],result)
        self.assertEqual(rows('SELECT balance FROM economy'),[(100,)])

class RetentionRoleRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        await reset_database()
        seed_member()
        self.role=role_stub(559,'Retention Shop Role')
        _,self.person,self.bot,self.guild=purchase_ctx(self.role,[self.role])
        self.person.premium_since=None
        self.started=asyncio.Event(); self.release=asyncio.Event()
        async def remove(role,reason=None):
            self.started.set()
            await self.release.wait()
            self.person.roles.remove(role)
        async def add(role,reason=None):
            if role not in self.person.roles: self.person.roles.append(role)
        self.person.remove_roles=AsyncMock(side_effect=remove)
        self.person.add_roles=AsyncMock(side_effect=add)
        await TO.record_role_retention(GUILD,USER,self.role.id,reason='test')

    async def grant(self):
        from utils.reward_engine import give_reward
        return await give_reward(self.bot,GUILD,USER,'role',role_id=self.role.id,source='shop',item_name=self.role.name)

    async def test_retention_removal_serializes_with_shop_grant(self):
        import asyncio
        task=asyncio.create_task(TO.reconcile_role_retentions(bot=self.bot))
        await asyncio.wait_for(self.started.wait(),5)
        grant=asyncio.create_task(self.grant())
        try:
            await asyncio.wait_for(asyncio.shield(grant),1)
        except asyncio.TimeoutError:
            pass
        finally:
            finished=grant.done(); self.release.set()
        await asyncio.wait_for(task,5)
        result=await asyncio.wait_for(grant,5)
        self.assertTrue(result['success'],result)
        self.assertIn(self.role,self.person.roles,'retention removed a newly granted Shop role')
        self.assertFalse(finished)

    async def test_retention_keeps_independent_shop_role(self):
        self.assertTrue((await self.grant())['success'])
        self.release.set()
        await TO.reconcile_role_retentions(bot=self.bot)
        self.assertIn(self.role,self.person.roles)
        self.person.remove_roles.assert_not_awaited()

class RetentionRoleCacheTests(RetentionCacheTests):
    async def test_missing_role_cache_and_fetch_failure_keeps_obligation(self):
        self.guild.get_role = lambda _id: None
        self.guild.fetch_roles = AsyncMock(side_effect=OSError('role cache unavailable'))
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(),1)
        self.assertTrue(result['errors'])

    async def test_missing_role_cache_and_fetched_role_keeps_unknown(self):
        self.guild.get_role = lambda _id: None
        self.guild.fetch_roles = AsyncMock(return_value=[self.role])
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(),1)
        self.assertEqual(result['kept'],1)

    async def test_missing_role_confirmed_by_fetch_clears(self):
        self.guild.get_role = lambda _id: None
        self.guild.fetch_roles = AsyncMock(return_value=[])
        result = await TO.reconcile_role_retentions(bot=self.bot)
        self.assertEqual(self.count(),0)
        self.assertEqual(result['satisfied'],1)

class ExpirySweepFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        TO._PENDING_DELIVERY_ROLLBACKS.clear()

    async def test_stuck_expired_role_does_not_starve_later_holding_and_is_retried(self):
        first = await TO.grant_timed_holding(GUILD,USER,'stuck',expires_at='2000-01-01T00:00:00Z',item_type='role',role_id=9001,source='shop')
        second = await TO.grant_timed_holding(GUILD,USER,'later',expires_at='2001-01-01T00:00:00Z',item_type='custom',source='shop')
        # No guild means the role cannot be removed, but an ordinary holding
        # requires no Discord access. Both passes use the real expiry path.
        first_pass = await TO.cleanup_expired_holdings(None,limit=1)
        second_pass = await TO.cleanup_expired_holdings(None,limit=1)
        self.assertEqual(first_pass['kept'],1)
        self.assertEqual(second_pass['removed'],1)
        self.assertEqual(rows('SELECT id FROM timed_holdings ORDER BY id'),[(first,)])
        third_pass = await TO.cleanup_expired_holdings(None,limit=1)
        self.assertEqual(third_pass['kept'],1, 'the stuck entry must remain retryable')

class StockEditSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    async def test_refund_does_not_exceed_admins_new_stock_limit(self):
        item = shop_item('title',price=10,name='Edited Stock',stock=2)
        op = await TO.open_purchase(GUILD,USER,item_id=item,item_name='Edited Stock',currency='balance',amount=10)
        await TO.update_purchase(op,status=TO.PURCHASE_FAILED,stock_reserved=1)
        # Admin reset the listing to full after the reservation.
        execute('UPDATE shop_items SET max_stock=1,current_stock=1 WHERE id=?',(item,))
        first = await TO.refund_purchase(op)
        second = await TO.refund_purchase(op)
        self.assertTrue(first['complete'],first)
        self.assertTrue(second['complete'],second)
        self.assertEqual(rows('SELECT max_stock,current_stock FROM shop_items WHERE id=?',(item,)),[(1,1)])

class LegacyRoleApiTests(unittest.TestCase):
    setUp = phase1_api.ListingApiTests.setUp
    post = phase1_api.ListingApiTests.post

    def test_legacy_role_with_duration_is_accepted(self):
        response = self.post(dict(name='Legacy Timed Role',type='role',role_id=551,price=10,duration_hours=2,max_stock=3,required_level=4,required_role_id=550))
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(rows('SELECT type,role_id,duration_hours,price,max_stock,current_stock,required_level,required_role_id FROM shop_items WHERE name=?',('Legacy Timed Role',)), [('role',551,2,10,3,3,4,550)])


class LegacyRoleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_role_duration_delivers_timed_holding(self):
        await reset_database(); seed_member(balance=100)
        from test_free_shop_items import role_stub
        role=role_stub(551,'Legacy Role')
        item=shop_item('role',price=10,name='Legacy Timed Role',role_id=551,duration_hours=2,stock=1)
        interaction, person = purchase_context(granted_role=role)
        await process_purchase(interaction,item)
        self.assertEqual(rows("SELECT item_type,role_id FROM timed_holdings WHERE item_name='Legacy Timed Role'"), [('temp_role',551)])
        self.assertEqual(rows("SELECT quantity FROM inventory_items WHERE item_name='Legacy Timed Role'"), [])
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items WHERE id=?',(item,)),[(0,)])
        person.add_roles.assert_awaited_once()
