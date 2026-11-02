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

    async def test_legacy_unattributed_shop_boost_blocks_refund(self):
        from utils.xp_calculator import grant_xp_boost_details
        item = shop_item('xp_boost',price=10,name='Old Boost',multiplier=2,duration_hours=2)
        op = await TO.open_purchase(GUILD,USER,item_id=item,item_name='Old Boost',currency='balance',amount=10)
        await TO.update_purchase(op,status=TO.PURCHASE_UNCERTAIN,charged=1)
        await grant_xp_boost_details(GUILD,USER,2.0,2,source='shop')
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'],result)
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertEqual(rows('SELECT count(*) FROM leveling_active_boosts'),[(1,)])

    async def test_same_name_legacy_holding_has_unprovable_provenance(self):
        op = await self.op()
        await TO.grant_timed_holding(GUILD,USER,'F3',expires_at='2035-01-01T00:00:00+00:00',source='shop')
        result = await TO.resolve_purchase_operation(op,action='refund',confirmed=True)
        self.assertFalse(result['success'],result)
        self.assertEqual(rows('SELECT balance FROM economy'),[(90,)])
        self.assertEqual(rows('SELECT count(*) FROM timed_holdings'),[(1,)])

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

class PreDeliveryStockRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=0)

    async def test_insufficient_balance_with_legacy_holding_leaves_stock_unreserved(self):
        for timestamp in ('2020-01-01 00:00:00', None):
            with self.subTest(created_at=timestamp):
                await reset_database()
                seed_member(balance=0)
                item = shop_item('temp_role', price=10, name='Gold VIP', role_id=55,
                                 duration_hours=2, stock=1)
                hid = await TO.grant_timed_holding(
                    GUILD, USER, 'Gold VIP', item_type='role', source='shop',
                    expires_at='2035-01-01T00:00:00+00:00', role_id=55)
                execute('UPDATE timed_holdings SET metadata=?, created_at=? WHERE id=?',
                        ('{}', timestamp, hid))
                itx, _ = purchase_context()
                delivery = AsyncMock()
                with patch('cogs.shop._deliver_purchase', delivery), \
                     patch('cogs.shop.refund_purchase', new=AsyncMock(wraps=TO.refund_purchase)) as refund:
                    await process_purchase(itx, item)
                delivery.assert_not_awaited()
                self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])
                self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
                refund.assert_not_awaited()
                self.assertEqual(rows('SELECT status,stock_reserved,charged,stock_returned FROM shop_purchase_operations'),
                                 [(TO.PURCHASE_FAILED,0,0,0)])
                self.assertEqual(rows('SELECT metadata, created_at FROM timed_holdings WHERE id=?', (hid,)), [('{}', timestamp)])

    async def test_uncertain_before_payment_does_not_reserve_charge_or_deliver(self):
        import cogs.shop as shop
        execute('UPDATE economy SET balance=100 WHERE guild_id=? AND user_id=?', (GUILD, USER))
        item = shop_item('title', price=10, name='Uncertain Before Pay', stock=1)
        itx, _ = purchase_context()
        async def open_then_quarantine(*args, **kwargs):
            op = await TO.open_purchase(*args, **kwargs)
            await TO.update_purchase(op, status=TO.PURCHASE_UNCERTAIN)
            return op
        delivery = AsyncMock(return_value={'success': False, 'error': 'fake failure', 'uncertain': True})
        with patch.object(shop, 'open_purchase', side_effect=open_then_quarantine), \
             patch.object(shop, '_deliver_purchase', delivery):
            await process_purchase(itx, item)
        delivery.assert_not_awaited()
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
        self.assertEqual(rows('SELECT status,stock_reserved,charged FROM shop_purchase_operations'),
                         [(TO.PURCHASE_UNCERTAIN,0,0)])

    async def test_uncertain_after_payment_commit_does_not_start_delivery(self):
        """A stale pending-only quarantine cannot steal a committed delivery."""
        import aiosqlite
        import sqlite3
        import cogs.shop as shop
        from database import DB_PATH
        execute('UPDATE economy SET balance=100 WHERE guild_id=? AND user_id=?', (GUILD, USER))
        item = shop_item('title', price=10, name='Quarantined After Pay', stock=1)
        itx, _ = purchase_context()
        original = aiosqlite.Connection.commit
        attempts = []
        async def commit_then_quarantine(conn):
            await original(conn)
            if attempts:
                return
            with sqlite3.connect(DB_PATH) as other:
                row = other.execute("SELECT op_id,status FROM shop_purchase_operations WHERE charged=1 AND stock_reserved=1").fetchone()
                if row:
                    won = other.execute("UPDATE shop_purchase_operations SET status='uncertain' WHERE op_id=? AND status='pending'", (row[0],)).rowcount
                    other.commit()
                    attempts.append((row[1], won))
        delivery = AsyncMock(return_value={'success': False, 'error': 'fake failure', 'uncertain': True})
        with patch.object(aiosqlite.Connection, 'commit', commit_then_quarantine), \
             patch.object(shop, '_deliver_purchase', delivery):
            await process_purchase(itx, item)
        self.assertEqual(attempts, [(TO.PURCHASE_DELIVERING, 0)])
        delivery.assert_awaited_once()
        self.assertEqual(rows('SELECT status FROM shop_purchase_operations'), [('uncertain',)])

    async def test_delivering_blocks_all_refunds_and_timeout(self):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='inflight', currency='balance', amount=10)
        await TO.update_purchase(op, charged=1, stock_reserved=1)
        self.assertTrue(await TO.claim_purchase_transition(op, from_statuses=(TO.PURCHASE_PENDING,), to_status=TO.PURCHASE_DELIVERING))
        self.assertFalse(await TO.claim_purchase_transition(op, from_statuses=(TO.PURCHASE_PENDING,), to_status=TO.PURCHASE_DELIVERING))
        execute("UPDATE shop_purchase_operations SET created_at=datetime('now','-1 day') WHERE op_id=?", (op,))
        result = await TO.refund_purchase(op)
        self.assertFalse(result['complete'], result)
        self.assertFalse((await TO.resolve_purchase_operation(op, action='refund', confirmed=True))['success'])
        await TO.reconcile_purchases(older_than_minutes=1)
        self.assertEqual((await TO.get_purchase(op))['status'], TO.PURCHASE_DELIVERING)
        self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])

    async def test_journal_flag_failure_rolls_back_all_effects(self):
        import cogs.shop as shop
        for field in ('stock_reserved', 'charged'):
            with self.subTest(field=field):
                await reset_database(); seed_member(balance=100)
                item = shop_item('title', price=10, name='Flag failure', stock=1)
                itx, _ = purchase_context()
                real = shop.update_purchase
                async def fail_flag(*args, **kwargs):
                    if kwargs.get(field) == 1:
                        return False
                    return await real(*args, **kwargs)
                with patch.object(shop, 'update_purchase', side_effect=fail_flag), \
                     patch.object(shop, '_deliver_purchase', new=AsyncMock()) as deliver:
                    with self.assertRaisesRegex(RuntimeError, 'Could not record'):
                        await process_purchase(itx, item)
                deliver.assert_not_awaited()
                self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
                self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
                self.assertEqual(rows('SELECT status,stock_reserved,charged FROM shop_purchase_operations'), [('failed',0,0)])
                self.assertEqual(rows('SELECT COUNT(*) FROM transaction_ledger'), [(0,)])

    async def test_commit_error_before_and_after_durable_commit_never_delivers(self):
        import aiosqlite
        import cogs.shop as shop
        original = aiosqlite.Connection.commit
        for after in (False, True):
            with self.subTest(committed=after):
                await reset_database(); seed_member(balance=100)
                item = shop_item('title', price=10, name='Commit failure', stock=1)
                itx, _ = purchase_context()
                injected = []
                async def bad_commit(conn):
                    cursor = await conn.execute("SELECT status FROM shop_purchase_operations WHERE status='delivering'")
                    selected = await cursor.fetchone()
                    if selected and not injected:
                        injected.append(True)
                        if after:
                            await original(conn)
                        raise OSError('injected commit error')
                    await original(conn)
                with patch.object(aiosqlite.Connection, 'commit', bad_commit), \
                     patch.object(shop, '_deliver_purchase', new=AsyncMock()) as deliver:
                    with self.assertRaisesRegex(OSError, 'injected commit error'):
                        await process_purchase(itx, item)
                self.assertEqual(injected, [True])
                deliver.assert_not_awaited()
                self.assertEqual(rows('SELECT balance FROM economy'), [(90 if after else 100,)])
                self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(0 if after else 1,)])
                self.assertEqual(rows('SELECT status,stock_reserved,charged FROM shop_purchase_operations'),
                                 [('delivering',1,1)] if after else [('uncertain',0,0)])

    async def test_retry_same_operation_cannot_charge_reserve_or_deliver_twice(self):
        import cogs.shop as shop
        execute('UPDATE economy SET balance=100 WHERE guild_id=? AND user_id=?', (GUILD, USER))
        item = shop_item('title', price=10, name='Duplicate op', stock=2)
        op = await TO.open_purchase(GUILD, USER, item_id=item, item_name='Duplicate op', currency='balance', amount=10)
        first, _ = purchase_context()
        second, _ = purchase_context()
        deliver = AsyncMock(return_value={'success': False, 'uncertain': True, 'error': 'unknown'})
        with patch.object(shop, 'open_purchase', new=AsyncMock(return_value=op)), \
             patch.object(shop, '_deliver_purchase', deliver):
            await process_purchase(first, item)
            await process_purchase(second, item)
        deliver.assert_awaited_once()
        self.assertEqual(rows('SELECT balance FROM economy'), [(90,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
        self.assertEqual(rows('SELECT status,charged,stock_reserved FROM shop_purchase_operations'), [('uncertain',1,1)])
        self.assertEqual(rows('SELECT COUNT(*) FROM transaction_ledger'), [(1,)])

    async def test_failure_after_deduct_rolls_back_stock_charge_and_ledger(self):
        import cogs.shop as shop
        execute('UPDATE economy SET balance=100 WHERE guild_id=? AND user_id=?', (GUILD, USER))
        item = shop_item('title', price=10, name='After deduction', stock=1)
        itx, _ = purchase_context()
        real = shop.safe_deduct
        async def deduct_then_fail(*args, **kwargs):
            await real(*args, **kwargs)
            raise RuntimeError('failure after deduct')
        with patch.object(shop, 'safe_deduct', side_effect=deduct_then_fail), \
             patch.object(shop, '_deliver_purchase', new=AsyncMock()) as delivery:
            with self.assertRaisesRegex(RuntimeError, 'failure after deduct'):
                await process_purchase(itx, item)
        delivery.assert_not_awaited()
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
        self.assertEqual(rows('SELECT COUNT(*) FROM transaction_ledger'), [(0,)])
        self.assertEqual(rows('SELECT status,charged,stock_reserved FROM shop_purchase_operations'), [('failed',0,0)])

    async def test_delivering_blocks_late_compensation_and_preserves_holding(self):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='protect', currency='balance', amount=10)
        holding = await TO.grant_timed_holding(GUILD, USER, 'protect', item_type='role',
                                               source='shop', expires_at='2035-01-01T00:00:00+00:00', op_id=op)
        self.assertTrue(await TO.claim_purchase_transition(op, from_statuses=(TO.PURCHASE_PENDING,), to_status=TO.PURCHASE_DELIVERING))
        result = await TO.compensate_late_delivery(op, {'guild_id': GUILD, 'user_id': USER,
                                                       'item_name': 'protect', 'internal_type': 'temp_role',
                                                       'entry_key': f'h:{holding}'})
        self.assertFalse(result['complete'], result)
        self.assertIn('delivering', result['error'])
        self.assertEqual(rows('SELECT COUNT(*) FROM timed_holdings WHERE id=?', (holding,)), [(1,)])

    async def test_concurrent_purchases_obey_stock_and_balance_limits(self):
        import asyncio
        import cogs.shop as shop
        for stock, balance, expected_stock in ((1, 100, 0), (2, 10, 1)):
            with self.subTest(stock=stock, balance=balance):
                await reset_database(); seed_member(balance=balance)
                item = shop_item('title', price=10, name='Contended', stock=stock)
                one, _ = purchase_context(); two, _ = purchase_context()
                deliver = AsyncMock(return_value={'success': False, 'uncertain': True, 'error': 'unknown'})
                with patch.object(shop, '_deliver_purchase', deliver):
                    await asyncio.gather(process_purchase(one, item), process_purchase(two, item))
                deliver.assert_awaited_once()
                self.assertEqual(rows('SELECT balance FROM economy'), [(balance-10,)])
                self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(expected_stock,)])
                self.assertEqual(rows('SELECT COUNT(*) FROM transaction_ledger'), [(1,)])
                self.assertEqual(rows("SELECT COUNT(*) FROM shop_purchase_operations WHERE charged=1 AND stock_reserved=1"), [(1,)])

    async def test_generic_status_update_cannot_release_live_delivery_for_refund(self):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='live', currency='balance', amount=10)
        await TO.update_purchase(op, charged=1)
        self.assertTrue(await TO.claim_purchase_transition(op, from_statuses=(TO.PURCHASE_PENDING,), to_status=TO.PURCHASE_DELIVERING))
        won = await TO.update_purchase(op, status=TO.PURCHASE_FAILED)
        refund = await TO.refund_purchase(op)
        self.assertFalse(refund['complete'], f'generic update={won}, refund={refund}')
        self.assertFalse(won, 'generic update must not steal delivery ownership')
        self.assertEqual((await TO.get_purchase(op))['status'], TO.PURCHASE_DELIVERING)
        self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])

    async def test_missing_ledger_cannot_commit_purchase_payment(self):
        import cogs.shop as shop
        execute('UPDATE economy SET balance=100 WHERE guild_id=? AND user_id=?', (GUILD, USER))
        item = shop_item('title', price=10, name='Missing ledger', stock=1)
        execute('DROP TABLE transaction_ledger')
        itx, _ = purchase_context()
        delivery = AsyncMock(return_value={'success': False, 'uncertain': True, 'error': 'unknown'})
        import sqlite3
        with patch.object(shop, '_deliver_purchase', delivery):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'transaction_ledger'):
                await process_purchase(itx, item)
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
        self.assertEqual(rows('SELECT current_stock FROM shop_items'), [(1,)])
        delivery.assert_not_awaited()

    async def test_missing_ledger_cannot_commit_refund_credit(self):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='Refund needs ledger', currency='balance', amount=10)
        await TO.update_purchase(op, status=TO.PURCHASE_FAILED, charged=1)
        execute('DROP TABLE transaction_ledger')
        result = await TO.refund_purchase(op)
        self.assertFalse(result['complete'], result)
        self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])
        self.assertFalse((await TO.get_purchase(op))['refunded'])

    async def test_uncertain_discord_role_side_effect_must_not_be_refunded_as_absent(self):
        import asyncio
        role = role_stub(9123, 'Ambiguous role')
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        execute('UPDATE economy SET balance=100 WHERE guild_id=? AND user_id=?', (GUILD, USER))
        item = shop_item('temp_role', price=10, name='Ambiguous role', role_id=role.id, duration_hours=2, stock=1)
        async def discord_applied_then_timed_out(role_arg, **kwargs):
            person.roles.append(role_arg)
            raise asyncio.TimeoutError('Discord result unknown after external application')
        person.add_roles = AsyncMock(side_effect=discord_applied_then_timed_out)
        await process_purchase(itx, item)
        op = rows('SELECT op_id,status FROM shop_purchase_operations')[0]
        self.assertEqual(op[1], TO.PURCHASE_UNCERTAIN)
        self.assertIn(role, person.roles)
        self.assertEqual(rows('SELECT COUNT(*) FROM timed_holdings'), [(0,)])
        result = await TO.resolve_purchase_operation(op[0], action='refund', confirmed=True, note='assumed absent')
        self.assertFalse(result['success'], f'role still present but refund was allowed: {result}')


# ── Continuation: write-ahead intent, stranded deliveries, provenance, trigger ──
import asyncio
import sqlite3
from test_timed_ownership import reply_text


def _forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'missing permissions')


def _op(**cols):
    sets = ','.join(f'{k}=?' for k in cols)
    return execute(f'UPDATE shop_purchase_operations SET {sets}', tuple(cols.values()))


class RoleIntentTests(unittest.IsolatedAsyncioTestCase):
    """A timeout, cancellation or crash is NOT proof that Discord did nothing."""

    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=100)
        self.role = role_stub(9201, 'Intent role')
        self.itx, self.person, self.bot, self.guild = purchase_ctx(granted_role=self.role)
        self.item = shop_item('temp_role', price=10, name='Intent role', role_id=self.role.id,
                              duration_hours=2, stock=1)
        self.guild.fetch_member = AsyncMock(return_value=self.person)

    def op(self):
        return rows('SELECT op_id,status FROM shop_purchase_operations')[0]

    async def intent(self):
        import json
        raw = rows('SELECT delivery_intent FROM shop_purchase_operations')[0][0]
        return json.loads(raw) if raw else None

    async def refund_blocked(self, op_id):
        res = await TO.resolve_purchase_operation(op_id, action='refund', confirmed=True, note='x')
        self.assertFalse(res['success'], res)
        self.assertEqual(rows('SELECT balance FROM economy'), [(90,)])
        return res

    async def test_timeout_leaves_intent_started_and_blocks_refund(self):
        self.person.add_roles = AsyncMock(side_effect=asyncio.TimeoutError('lost'))
        await process_purchase(self.itx, self.item)
        op_id, status = self.op()
        self.assertEqual(status, TO.PURCHASE_UNCERTAIN)
        self.assertEqual((await self.intent())['state'], TO.INTENT_STARTED)
        res = await self.refund_blocked(op_id)
        self.assertIn('unresolved', res['error'])

    async def test_lookup_failure_and_wrong_guild_keep_intent_started(self):
        self.person.add_roles = AsyncMock(side_effect=asyncio.TimeoutError('lost'))
        await process_purchase(self.itx, self.item)
        op_id, _ = self.op()
        for failure in (OSError('network'),
                        discord.NotFound(SimpleNamespace(status=404, reason='NF'),
                                         {'code': 10004, 'message': 'Unknown Guild'})):
            self.guild.fetch_member = AsyncMock(side_effect=failure)
            stats = await TO.reconcile_delivery_intents(self.bot, settle_seconds=0)
            self.assertEqual(stats['settled'], 0, stats)
            self.assertEqual((await self.intent())['state'], TO.INTENT_STARTED)
            await self.refund_blocked(op_id)
        # A fresh request is also never declared absent inside the settle window.
        self.guild.fetch_member = AsyncMock(return_value=self.person)
        stats = await TO.reconcile_delivery_intents(self.bot, settle_seconds=3600)
        self.assertEqual(stats['settled'], 0)
        self.assertEqual((await self.intent())['state'], TO.INTENT_STARTED)

    async def test_cancellation_mid_request_is_stranded_then_resolved_by_observation(self):
        entered = asyncio.Event()
        async def hang(role, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.person.add_roles = AsyncMock(side_effect=hang)
        task = asyncio.create_task(process_purchase(self.itx, self.item))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        op_id, status = self.op()
        self.assertEqual(status, TO.PURCHASE_DELIVERING)
        self.assertFalse(TO.is_live_delivery(op_id), 'scope must release on cancellation')
        self.assertEqual((await self.intent())['state'], TO.INTENT_STARTED)
        # Stranded: parked, never refunded.
        stats = await TO.reconcile_stranded_deliveries(lease_minutes=0)
        self.assertEqual(stats['parked'], 1, stats)
        self.assertEqual(stats['by_class'], {'external_unresolved': 1})
        self.assertEqual(self.op()[1], TO.PURCHASE_UNCERTAIN)
        self.assertEqual(rows('SELECT refunded,stock_returned FROM shop_purchase_operations'), [(0, 0)])
        await self.refund_blocked(op_id)
        # Role really is absent: an authoritative fetch settles the intent.
        stats = await TO.reconcile_delivery_intents(self.bot, settle_seconds=0)
        self.assertEqual(stats['settled'], 1, stats)
        res = await TO.resolve_purchase_operation(op_id, action='refund', confirmed=True, note='observed absent')
        self.assertTrue(res['success'], res)
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])

    async def test_present_role_is_neither_removed_nor_attributed(self):
        self.person.add_roles = AsyncMock(side_effect=asyncio.TimeoutError('applied then lost'))
        await process_purchase(self.itx, self.item)
        op_id, _ = self.op()
        self.person.roles.append(self.role)
        stats = await TO.reconcile_delivery_intents(self.bot, settle_seconds=0)
        self.assertEqual(stats['present'], 1, stats)
        self.assertEqual((await self.intent())['state'], TO.INTENT_PRESENT)
        self.person.remove_roles.assert_not_awaited()
        self.assertEqual(rows('SELECT COUNT(*) FROM timed_holdings'), [(0,)])
        self.assertEqual(rows('SELECT COUNT(*) FROM inventory_items'), [(0,)])
        await self.refund_blocked(op_id)
        self.assertIn(self.role, self.person.roles)

    async def test_member_left_guild_is_confirmed_absence(self):
        self.person.add_roles = AsyncMock(side_effect=asyncio.TimeoutError('lost'))
        await process_purchase(self.itx, self.item)
        self.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason='NF'), {'code': 10007, 'message': 'Unknown Member'}))
        stats = await TO.reconcile_delivery_intents(self.bot, settle_seconds=0)
        self.assertEqual(stats['settled'], 1, stats)
        self.assertEqual((await self.intent())['state'], TO.INTENT_ABSENT)

    async def test_certain_rejection_is_confirmed_absent_and_refunds(self):
        self.person.add_roles = AsyncMock(side_effect=_forbidden())
        await process_purchase(self.itx, self.item)
        op_id, status = self.op()
        self.assertEqual(status, TO.PURCHASE_REFUNDED if False else TO.PURCHASE_FAILED)
        self.assertEqual((await self.intent())['state'], TO.INTENT_ABSENT)
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
        self.assertEqual(rows('SELECT refunded,stock_returned FROM shop_purchase_operations'), [(1, 1)])

    async def test_permanent_role_ambiguity_blocks_refund(self):
        """give_reward('role') carries no op id; only the intent protects it."""
        perm = shop_item('color', price=10, name='Perm intent', role_id=self.role.id, stock=1)
        self.person.add_roles = AsyncMock(side_effect=asyncio.TimeoutError('applied then lost'))
        await process_purchase(self.itx, perm)
        op_id, status = rows('SELECT op_id,status FROM shop_purchase_operations ORDER BY rowid DESC')[0]
        self.assertEqual(status, TO.PURCHASE_UNCERTAIN)
        res = await TO.resolve_purchase_operation(op_id, action='refund', confirmed=True, note='x')
        self.assertFalse(res['success'], res)
        self.assertEqual(rows('SELECT balance FROM economy'), [(90,)])

    async def test_refund_before_delivery_refuses_to_send_the_role(self):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='x', currency='balance', amount=1)
        _op(status='refunded', refunded=1)
        outcome = await TO.begin_delivery_intent(op, kind=TO.INTENT_KIND_DISCORD_ROLE, guild_id=GUILD,
                                                 user_id=USER, role_id=1)
        self.assertEqual(outcome, 'refused')
        self.assertEqual(await TO.begin_delivery_intent('no-such-op', kind=TO.INTENT_KIND_DISCORD_ROLE,
                                                        guild_id=GUILD, user_id=USER, role_id=1),
                         'not_applicable')


class StrandedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=0)

    async def delivering(self, name='stranded', age='-2 hours'):
        op = await TO.open_purchase(GUILD, USER, item_id=None, item_name=name, currency='balance', amount=10)
        await TO.update_purchase(op, charged=1)
        self.assertTrue(await TO.claim_purchase_transition(
            op, from_statuses=(TO.PURCHASE_PENDING,), to_status=TO.PURCHASE_DELIVERING))
        execute("UPDATE shop_purchase_operations SET updated_at=datetime('now', ?) WHERE op_id=?", (age, op))
        return op

    async def classify(self, op):
        stats = await TO.reconcile_stranded_deliveries(lease_minutes=30)
        op_row = await TO.get_purchase(op)
        return stats, op_row

    async def test_never_started_is_parked_not_refunded(self):
        op = await self.delivering()
        stats, row = await self.classify(op)
        self.assertEqual(stats['by_class'], {'never_started': 1})
        self.assertEqual(row['status'], TO.PURCHASE_UNCERTAIN)
        self.assertIn('never_started', row['detail'])
        self.assertEqual((row['refunded'], row['stock_returned']), (0, 0))
        self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])

    async def test_goods_present_blocks_even_a_manual_refund(self):
        op = await self.delivering()
        await TO.grant_timed_holding(GUILD, USER, 'stranded', item_type='role', source='shop',
                                     expires_at='2035-01-01T00:00:00+00:00', op_id=op)
        stats, row = await self.classify(op)
        self.assertEqual(stats['by_class'], {'goods_present': 1})
        res = await TO.resolve_purchase_operation(op, action='refund', confirmed=True, note='x')
        self.assertFalse(res['success'], res)
        self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])

    async def test_unreadable_intent_is_undeterminable_and_blocks(self):
        op = await self.delivering()
        execute("UPDATE shop_purchase_operations SET delivery_intent='{not json' WHERE op_id=?", (op,))
        stats, row = await self.classify(op)
        self.assertEqual(stats['by_class'], {'undeterminable': 1})
        res = await TO.resolve_purchase_operation(op, action='refund', confirmed=True, note='x')
        self.assertFalse(res['success'], res)

    async def test_live_and_recent_deliveries_are_never_touched(self):
        with TO.live_purchase_scope():
            live = await TO.open_purchase(GUILD, USER, item_id=None, item_name='live',
                                          currency='balance', amount=10)
            await TO.update_purchase(live, charged=1)
            await TO.claim_purchase_transition(live, from_statuses=(TO.PURCHASE_PENDING,),
                                               to_status=TO.PURCHASE_DELIVERING)
            execute("UPDATE shop_purchase_operations SET updated_at=datetime('now','-5 hours') WHERE op_id=?", (live,))
            stats = await TO.reconcile_stranded_deliveries(lease_minutes=30)
            self.assertEqual((stats['parked'], stats['skipped_live']), (0, 1))
            self.assertEqual((await TO.get_purchase(live))['status'], TO.PURCHASE_DELIVERING)
            recent = await self.delivering('recent', age='-1 minutes')
            stats = await TO.reconcile_stranded_deliveries(lease_minutes=30)
            self.assertEqual(stats['parked'], 0)
            self.assertEqual((await TO.get_purchase(recent))['status'], TO.PURCHASE_DELIVERING)
        # Once its worker is gone the old row is no longer live and is parked.
        stats = await TO.reconcile_stranded_deliveries(lease_minutes=30)
        self.assertEqual(stats['parked'], 1)
        self.assertEqual((await TO.get_purchase(live))['status'], TO.PURCHASE_UNCERTAIN)
        self.assertEqual((await TO.get_purchase(recent))['status'], TO.PURCHASE_DELIVERING)

    async def test_reconcile_purchases_parks_but_never_refunds(self):
        op = await self.delivering()
        stats = await TO.reconcile_purchases()
        self.assertEqual(stats['stranded'], 1, stats)
        self.assertEqual(rows('SELECT balance FROM economy'), [(0,)])
        self.assertEqual((await TO.get_purchase(op))['refunded'], 0)

    async def test_inspection_reports_classification(self):
        op = await self.delivering()
        evidence = await TO.inspect_purchase_operation(op)
        self.assertEqual(evidence['delivery_classification']['class'], 'never_started')
        self.assertIn('Delivery Class : never_started', TO.format_purchase_inspection(evidence))


class TriggerBackstopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        self.op = await TO.open_purchase(GUILD, USER, item_id=None, item_name='t', currency='balance', amount=10)
        await TO.update_purchase(self.op, charged=1, stock_reserved=1)
        await TO.claim_purchase_transition(self.op, from_statuses=(TO.PURCHASE_PENDING,),
                                           to_status=TO.PURCHASE_DELIVERING)

    async def test_direct_sql_cannot_compensate_a_delivering_operation(self):
        for stmt in ("UPDATE shop_purchase_operations SET refunded=1",
                     "UPDATE shop_purchase_operations SET stock_returned=1",
                     "UPDATE shop_purchase_operations SET status='failed', refunded=1, stock_returned=1"):
            with self.subTest(stmt=stmt):
                with self.assertRaises(sqlite3.IntegrityError):
                    execute(stmt)
        self.assertEqual(rows('SELECT status,refunded,stock_returned FROM shop_purchase_operations'),
                         [('delivering', 0, 0)])

    async def test_owner_transition_then_compensation_is_still_allowed(self):
        self.assertTrue(await TO.claim_purchase_transition(
            self.op, from_statuses=(TO.PURCHASE_DELIVERING,), to_status=TO.PURCHASE_FAILED))
        self.assertTrue((await TO.refund_purchase(self.op))['complete'])
        self.assertEqual(rows('SELECT balance FROM economy'), [(9010,)])


class ProvenanceNarrowingTests(unittest.IsolatedAsyncioTestCase):
    """An unlabelled holding is only independent of a purchase that labels everything."""

    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=100)
        self.role = role_stub(9301, 'Legacy twin')
        self.itx, self.person, self.bot, self.guild = purchase_ctx(granted_role=self.role)
        self.item = shop_item('temp_role', price=10, name='Legacy twin', role_id=self.role.id,
                              duration_hours=2, stock=2)
        # An older, independent holding of the same product with no origin id.
        execute("INSERT INTO timed_holdings (guild_id,user_id,item_name,item_type,quantity,metadata,"
                "source,role_id,expires_at) VALUES (?,?,?,?,1,NULL,'shop',?,'2035-01-01T00:00:00+00:00')",
                (GUILD, USER, 'Legacy twin', 'temp_role', self.role.id))

    async def test_new_protocol_certain_failure_refunds_despite_legacy_holding(self):
        self.person.add_roles = AsyncMock(side_effect=_forbidden())
        await process_purchase(self.itx, self.item)
        self.assertEqual(rows('SELECT status,provenance_tracked,refunded,stock_returned '
                              'FROM shop_purchase_operations'), [('failed', 1, 1, 1)])
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])
        self.assertEqual(rows('SELECT COUNT(*) FROM timed_holdings'), [(1,)], 'legacy holding untouched')

    async def test_untracked_operation_stays_fail_closed(self):
        op = await TO.open_purchase(GUILD, USER, item_id=self.item, item_name='Legacy twin',
                                    currency='balance', amount=10)
        await TO.update_purchase(op, charged=1, status=TO.PURCHASE_FAILED)
        self.assertEqual(rows('SELECT provenance_tracked FROM shop_purchase_operations'), [(0,)])
        result = await TO.refund_purchase(op)
        self.assertFalse(result['complete'], result)
        self.assertIn('unprovable', result['error'])
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])  # nothing returned

    async def test_tracked_operation_still_blocked_by_linked_or_unreadable_holdings(self):
        op = await TO.open_purchase(GUILD, USER, item_id=self.item, item_name='Legacy twin',
                                    currency='balance', amount=10)
        await TO.update_purchase(op, charged=1, status=TO.PURCHASE_FAILED)
        execute('UPDATE shop_purchase_operations SET provenance_tracked=1')
        execute("DELETE FROM timed_holdings")
        await TO.grant_timed_holding(GUILD, USER, 'Legacy twin', item_type='temp_role', source='shop',
                                     expires_at='2035-01-01T00:00:00+00:00', op_id=op)
        self.assertFalse((await TO.refund_purchase(op))['complete'])  # its own goods exist
        execute("DELETE FROM timed_holdings")
        execute("INSERT INTO timed_holdings (guild_id,user_id,item_name,item_type,quantity,metadata,"
                "source,expires_at) VALUES (?,?,?,?,1,'{broken','shop','2035-01-01T00:00:00+00:00')",
                (GUILD, USER, 'Legacy twin', 'temp_role'))
        self.assertFalse((await TO.refund_purchase(op))['complete'])  # unreadable: not provably independent
        self.assertEqual(rows('SELECT balance FROM economy'), [(100,)])  # nothing returned

    async def test_legacy_xp_boost_blocks_untracked_but_not_tracked(self):
        listing = shop_item('xp_boost', price=10, name='Boost', multiplier=2.0, duration_hours=1)
        execute("INSERT INTO leveling_active_boosts (guild_id,user_id,multiplier,expires_at,source,origin_op_id) "
                "VALUES (?,?,2.0,'2035-01-01T00:00:00+00:00','shop',NULL)", (GUILD, USER))
        op = await TO.open_purchase(GUILD, USER, item_id=listing, item_name='Boost',
                                    currency='balance', amount=10)
        await TO.update_purchase(op, charged=1, status=TO.PURCHASE_FAILED)
        self.assertFalse((await TO.refund_purchase(op))['complete'])
        execute('UPDATE shop_purchase_operations SET provenance_tracked=1')
        self.assertTrue((await TO.refund_purchase(op))['complete'])
        self.assertEqual(rows('SELECT COUNT(*) FROM leveling_active_boosts'), [(1,)])


class LiveDeliveryP1Tests(unittest.IsolatedAsyncioTestCase):
    """The operator-vs-live-delivery race, for BOTH protocols."""

    async def asyncSetUp(self):
        await reset_database()
        seed_member()

    async def _stalled(self, legacy):
        import cogs.shop as shop_mod
        from test_timed_ownership import shop_item as tto_shop_item
        item_id = tto_shop_item(kind='custom', name='P1 Relic', price=300, stock=2, duration_hours=24)
        inter, _, _, _ = purchase_ctx()
        entered, release = asyncio.Event(), asyncio.Event()
        async def stalled(*args, **kwargs):
            entered.set()
            await release.wait()
            return await shop_mod._deliver_purchase_effects(*args, **kwargs)
        return shop_mod, item_id, inter, entered, release, stalled

    async def test_new_protocol_operator_cannot_refund_live_delivery(self):
        shop_mod, item_id, inter, entered, release, stalled = await self._stalled(False)
        with patch.object(shop_mod, '_deliver_purchase', side_effect=stalled):
            task = asyncio.create_task(process_purchase(inter, item_id))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                op_id = rows('SELECT op_id FROM shop_purchase_operations')[0][0]
                self.assertTrue(TO.is_live_delivery(op_id))

                execute("UPDATE shop_purchase_operations SET created_at=datetime('now','-30 minutes'), "
                        "updated_at=datetime('now','-30 minutes')")
                self.assertEqual((await TO.reconcile_purchases(older_than_minutes=15))['stranded'], 0)
                self.assertFalse((await TO.refund_purchase(op_id))['complete'])
                self.assertFalse((await TO.resolve_purchase_operation(op_id, action='refund', confirmed=True))['success'])
                with self.assertRaises(sqlite3.IntegrityError):
                    execute('UPDATE shop_purchase_operations SET refunded=1')
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
        self.assertEqual((await TO.get_purchase(op_id))['status'], TO.PURCHASE_DELIVERED)
        self.assertFalse(TO.is_live_delivery(op_id))
        self.assertEqual(rows('SELECT COUNT(*) FROM purchase_history'), [(1,)])
        self.assertEqual(rows('SELECT balance FROM economy'), [(8700,)])

    async def test_legacy_worker_refund_during_delivery_is_still_rolled_back(self):
        """The original P1 scenario, for a record created by the pre-`delivering` protocol."""
        shop_mod, item_id, inter, entered, release, stalled = await self._stalled(True)
        with patch.object(shop_mod, '_deliver_purchase', side_effect=stalled):
            task = asyncio.create_task(process_purchase(inter, item_id))
            await asyncio.wait_for(entered.wait(), 5)
            op_id = rows('SELECT op_id FROM shop_purchase_operations')[0][0]
            # A legacy worker never wrote the delivering state, an intent or the marker.
            execute("UPDATE shop_purchase_operations SET status='pending', delivery_intent=NULL, "
                    "provenance_tracked=0, created_at=datetime('now','-30 minutes') WHERE op_id=?", (op_id,))
            self.assertEqual((await TO.reconcile_purchases(older_than_minutes=15))['uncertain'], 1)
            resolved = await TO.resolve_purchase_operation(op_id, action='refund', confirmed=True, note='stalled')
            self.assertTrue(resolved['success'], resolved)
            self.assertEqual(rows('SELECT balance FROM economy'), [(9000,)])
            release.set()
            await asyncio.wait_for(task, 5)
        final = await TO.get_purchase(op_id)
        self.assertEqual((final['status'], final['refunded'], final['stock_returned']), ('refunded', 1, 1))
        self.assertIsNone(final['history_id'])
        self.assertEqual(rows('SELECT COUNT(*) FROM purchase_history'), [(0,)])
        self.assertEqual(rows('SELECT COUNT(*) FROM timed_holdings'), [(0,)], 'late holding must be cleaned up')
        self.assertIn('Purchase needs a check', reply_text(inter))
        self.assertEqual(rows('SELECT balance FROM economy'), [(9000,)])
        self.assertTrue((await TO.refund_purchase(op_id))['complete'])  # idempotent
        self.assertEqual(rows('SELECT balance FROM economy'), [(9000,)])
