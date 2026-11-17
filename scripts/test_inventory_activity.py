"""Transaction-level tests for the initial activity log coverage."""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.TemporaryDirectory(prefix="inventory-activity-")
os.environ['DATABASE_PATH'] = os.path.join(_tmp.name, 'test.db')
os.environ.setdefault('OWNER_ID', '999999999')
os.environ.setdefault('SECRET_KEY', 'testsecretkey0123456789abcdef0123456789')

import aiosqlite
from database import init_db, DB_PATH
from utils import inventory, inventory_activity, trade_engine


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await trade_engine.ensure_trade_table()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('DELETE FROM inventory_activity')
            await db.execute('DELETE FROM inventory_items')
            await db.execute('DELETE FROM equipped_roles')
            await db.execute('DELETE FROM equipped_titles')
            await db.execute('DELETE FROM timed_holdings')
            await db.execute('DELETE FROM leveling_active_boosts')
            await db.execute('DELETE FROM trade_history')
            await db.commit()

    async def test_acquire_and_isolation(self):
        await inventory.give_item(1, 2, 'Sword', 3, source='mission')
        await inventory.give_item(1, 3, 'Other', 1, source='event')
        events = await inventory_activity.get_activity_page(1, 2)
        self.assertEqual([(e['event_type'], e['quantity'], e['source']) for e in events],
                         [('acquired', 3, 'mission')])
        self.assertEqual(await inventory_activity.get_activity_page(2, 2), [])
        self.assertTrue(await inventory_activity.get_activity_start())

    async def test_caller_rollback_discards_event_and_inventory(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('BEGIN IMMEDIATE')
            await inventory.give_item(1, 2, 'Rolled back', 1, db=db)
            await db.rollback()
        self.assertEqual(await inventory_activity.get_activity_page(1, 2), [])
        self.assertFalse(await inventory.has_item(1, 2, 'Rolled back'))

    async def test_trade_records_both_sides_in_transaction(self):
        await inventory.give_item(1, 2, 'Sword', 2, source='event')
        with patch.object(trade_engine, 'get_trade_history', AsyncMock()):
            result = await trade_engine.execute_trade(
                1, 2, {'coins': 0, 'diamonds': 0, 'items': {'Sword': 1}},
                3, {'coins': 0, 'diamonds': 0, 'items': {'Other': 1}})
        # The receiver cannot offer an item they don't own; no trade events.
        self.assertFalse(result['success'])
        self.assertEqual([e['event_type'] for e in await inventory_activity.get_activity_page(1, 2)], ['acquired'])
        await inventory.give_item(1, 3, 'Other', 1, source='event')
        result = await trade_engine.execute_trade(
            1, 2, {'coins': 0, 'diamonds': 0, 'items': {'Sword': 1}},
            3, {'coins': 0, 'diamonds': 0, 'items': {'Other': 1}})
        self.assertTrue(result['success'])
        events = await inventory_activity.get_activity_page(1, 2)
        self.assertEqual({e['event_type'] for e in events}, {'acquired', 'trade_sent', 'trade_received'})
        self.assertEqual(next(e['related_user_id'] for e in events if e['event_type'] == 'trade_sent'), 3)

    async def test_level_reversal_and_restoration_record_actual_moves(self):
        from utils import level_ops, level_claims
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('BEGIN IMMEDIATE')
            await inventory.give_item(1, 2, 'Level item', 2, source='level_claim', db=db)
            await level_ops.record_grant(db, guild_id=1, user_id=2, claim_id=91234,
                reward_level=5, track='shop', kind=level_ops.KIND_INVENTORY,
                item_name='Level item', quantity=2)
            await db.commit()
        args = dict(guild_id=1, user_id=2, claim_id=91234, item_name='Level item',
                    quantity=2, operation_id='test-correction', reason='test')
        result = await level_ops.reverse_inventory(**args)
        self.assertEqual(result['status'], level_ops.STATUS_APPLIED)
        result = await level_ops.reverse_inventory(**args)
        self.assertEqual(result['status'], level_ops.STATUS_ALREADY)
        events = await inventory_activity.get_activity_page(1, 2)
        self.assertEqual([e['event_type'] for e in events].count('ownership_reversed'), 1)
        restored = await level_claims._settle_reached_reversals(1, 2, [91234], operation_id='test-correction')
        self.assertEqual(len(restored), 1)
        self.assertEqual((await inventory_activity.get_activity_page(1, 2))[0]['event_type'], 'restored')
        self.assertEqual(await level_claims._settle_reached_reversals(1, 2, [91234], operation_id='test-correction'), [])

    async def test_prepared_emoji_and_ambiguous_shop_fallback(self):
        # Adapted from PR #104's catalog-less and ambiguous listing cases.
        from cogs.wallet import _decorate
        from utils import shop_emoji
        url = 'https://cdn.example.com/legacy.png'
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) VALUES (1,'Legacy',1,'custom',?,1)", (url,))
            await db.execute("INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) VALUES (1,'Ambiguous',1,'custom',?,1)", (url,))
            await db.execute("INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) VALUES (1,'Ambiguous',1,'custom',NULL,1)")
            await db.execute("INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,animated,creator_guild_id,pipeline_version,status) VALUES (?,?,?,?,?,?,?,?)",
                ('123456789012345678', 'a' * 64, 'legacy', '150000000000000010', 0, 1, 'rgba-128-png-v1', 'ready'))
            await db.execute("INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash) VALUES (?,?,?,?)",
                ('123456789012345678', 1, url, 'a' * 64))
            await db.commit()
        with patch.object(shop_emoji, 'configured_application_id', return_value='123456789012345678'):
            entries = await _decorate(1, [dict(item_name='Legacy'), dict(item_name='Ambiguous')])
        by_name = {entry['item_name']: entry for entry in entries}
        self.assertEqual(by_name['Legacy']['app_emoji'], '<:legacy:150000000000000010>')
        self.assertEqual(by_name['Ambiguous']['app_emoji'], '')

    async def test_unified_inventory_is_opt_in_and_preserves_identity(self):
        from datetime import datetime, timedelta, timezone
        from utils.timed_ownership import grant_timed_holding
        await inventory.give_item(1, 2, 'Twin', 2, source='mission')
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        live_id = await grant_timed_holding(1, 2, 'Twin', expires_at=future)
        await grant_timed_holding(1, 2, 'Old', expires_at=past)
        await grant_timed_holding(1, 3, 'Other user', expires_at=future)
        entries = await inventory.get_inventory_entries(1, 2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(await inventory.get_inventory(1, 2)), 1)
        self.assertIsNone(inventory.find_entry(entries, item_name='Twin'))
        self.assertTrue(inventory.find_entry(entries, entry_key=f'h:{live_id}')['timed'])
        self.assertIsNone(inventory.find_entry(entries, entry_key='h:999999'))
        self.assertIsNone(inventory.find_entry(entries, item_name='Twin', item_type='no-such-type'))
        from utils.rank_card_data import get_rank_card_data
        with patch('cogs.minigames.get_user_win_count', AsyncMock(return_value=0)):
            card = await get_rank_card_data(1, 2)
        twin = [entry for entry in card['inventory_grid'] if entry['item_name'] == 'Twin']
        self.assertEqual(len(twin), 1)
        self.assertEqual(twin[0]['quantity'], 3)
        self.assertFalse(any(entry['item_name'] == 'Old' for entry in card['inventory_grid']))

    async def test_key_idempotency_and_page_order(self):
        async with aiosqlite.connect(DB_PATH) as db:
            for _ in range(2):
                await inventory_activity.record_activity(db, guild_id=1, user_id=2,
                    event_type='expired', item_name='X', quantity=1, event_key='holding:1:expired')
            await inventory_activity.record_activity(db, guild_id=1, user_id=2,
                event_type='expired', item_name='Y', quantity=1, event_key='holding:2:expired')
            await db.commit()
        self.assertEqual([e['item_name'] for e in await inventory_activity.get_activity_page(1, 2, limit=1)], ['Y'])
        self.assertEqual([e['item_name'] for e in await inventory_activity.get_activity_page(1, 2, offset=1, limit=1)], ['X'])

    async def test_timed_grant_expiry_and_retry_are_recorded_once(self):
        from datetime import datetime, timedelta, timezone
        from utils import timed_ownership as timed
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        holding_id = await timed.grant_timed_holding(1, 2, 'Clock', expires_at=past)
        holding = await timed.get_timed_holding(f'h:{holding_id}')
        self.assertEqual((await timed.expire_one(None, holding))['status'], 'removed')
        self.assertEqual((await timed.expire_one(None, holding))['status'], 'skipped')
        events = await inventory_activity.get_activity_page(1, 2)
        self.assertEqual([e['event_type'] for e in events], ['expired', 'acquired'])

    async def test_live_timed_holding_cannot_be_removed_before_expiry(self):
        """Only real expiry retires a live timed holding."""
        from datetime import datetime, timedelta, timezone
        from utils import timed_ownership as timed
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        holding_id = await timed.grant_timed_holding(1, 2, 'Clock', expires_at=future)
        holding = await timed.get_timed_holding(f'h:{holding_id}')
        self.assertEqual((await timed.expire_one(None, holding))['status'], 'skipped')
        self.assertIsNotNone(await timed.get_timed_holding(f'h:{holding_id}'))
        self.assertEqual([e['event_type'] for e in await inventory_activity.get_activity_page(1, 2)],
                         ['acquired'])

    async def test_potion_commit_and_stale_identity(self):
        from utils import potion_engine
        meta = potion_engine.build_metadata('xp_boost', 2, 1)
        await inventory.give_item(1, 2, 'Potion', 1, item_type='potion', metadata=meta)
        item = (await inventory.get_inventory_entries(1, 2))[0]
        self.assertFalse((await potion_engine.use_potion(1, 2, 'Potion', entry_key='i:999999'))['success'])
        self.assertTrue((await potion_engine.use_potion(1, 2, 'Potion', entry_key=item['entry_key']))['success'])
        self.assertEqual([e['event_type'] for e in await inventory_activity.get_activity_page(1, 2)],
                         ['used', 'acquired'])

    async def test_title_actions_validate_slot_and_identity(self):
        from utils import title_engine
        await inventory.give_item(1, 2, 'Title', item_type='title')
        item = (await inventory.get_inventory_entries(1, 2))[0]
        self.assertFalse((await title_engine.equip_title(1, 2, 'Title', entry_key='i:999'))['success'])
        self.assertTrue((await title_engine.equip_title(1, 2, 'Title', entry_key=item['entry_key'], expected_title=None))['success'])
        self.assertFalse((await title_engine.equip_title(1, 2, 'Title', entry_key=item['entry_key'], expected_title=None))['success'])
        self.assertFalse((await title_engine.unequip_title(1, 2, 'Title', entry_key='i:999'))['success'])
        self.assertTrue((await title_engine.unequip_title(1, 2, 'Title', entry_key=item['entry_key']))['success'])
        self.assertEqual([e['event_type'] for e in await inventory_activity.get_activity_page(1, 2)],
                         ['unequipped', 'equipped', 'acquired'])

    async def test_trade_preserves_potion_and_refuses_equipped_and_incompatible(self):
        from utils import potion_engine, title_engine
        potion = potion_engine.build_metadata('xp_boost', 2, 1)
        await inventory.give_item(1, 2, 'Potion', 1, item_type='potion', metadata=potion)
        await inventory.give_item(1, 3, 'Gift', 1)
        offer = {'coins': 0, 'diamonds': 0, 'items': {'Potion': 1}}
        counter = {'coins': 0, 'diamonds': 0, 'items': {'Gift': 1}}
        self.assertTrue((await trade_engine.execute_trade(1, 2, offer, 3, counter))['success'])
        received = next(x for x in await inventory.get_inventory(1, 3) if x['item_name'] == 'Potion')
        self.assertEqual((received['item_type'], received['metadata']), ('potion', potion))
        await inventory.give_item(1, 2, 'Title', item_type='title')
        await title_engine.equip_title(1, 2, 'Title')
        self.assertFalse((await trade_engine.execute_trade(1, 2,
            {'items': {'Title': 1}}, 3, {'items': {'Potion': 1}}))['success'])
        await inventory.give_item(1, 2, 'Potion', item_type='custom')
        self.assertFalse((await trade_engine.execute_trade(1, 3,
            {'items': {'Potion': 1}}, 2, {'items': {'Gift': 1}}))['success'])


if __name__ == '__main__':
    unittest.main()
