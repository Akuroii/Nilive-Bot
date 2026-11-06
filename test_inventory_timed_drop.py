"""Timed Drop role reconciliation, using scratch SQLite and Discord stubs."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from phase1_support import GUILD, USER, execute, reset_database, seed_member
from test_timed_ownership import iso_in, purchase_ctx, purge_timed, role_stub
from utils import equip_engine as equip, timed_ownership as timed
from utils.inventory import set_quantity
from utils.inventory_activity import get_activity_page


class TimedRoleDropTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=500)
        purge_timed()
        for table in ('inventory_activity', 'equipped_roles', 'inventory_items', 'timed_holdings'):
            execute(f'DELETE FROM {table}')
        self.role = role_stub(9511, 'DropRole')
        _, self.person, self.bot, _ = purchase_ctx(granted_role=self.role)
        self.person.roles = [self.role]
        ident = await timed.grant_timed_holding(GUILD, USER, 'DropRole',
            expires_at=iso_in(2), item_type='temp_role', role_id=9511,
            metadata={'role_id': 9511})
        self.holding = await timed.get_timed_holding(f'h:{ident}')
        await equip.record_equipped(GUILD, USER, 'DropRole', role_id=9511,
                                    entry_key=self.holding['entry_key'])

    async def events(self):
        return [e['event_type'] for e in await get_activity_page(GUILD, USER)]

    async def test_discord_failure_then_retry_records_one_drop(self):
        self.person.remove_roles = AsyncMock(side_effect=RuntimeError('network'))
        result = await timed.expire_one(self.bot, self.holding, drop_requested=True)
        self.assertEqual(result['status'], 'kept')
        self.assertNotIn('dropped', await self.events())
        self.assertIsNotNone(await timed.get_timed_holding(self.holding['entry_key']))
        self.person.remove_roles = AsyncMock()
        self.assertEqual((await timed.expire_one(self.bot, self.holding, drop_requested=True))['status'], 'removed')
        self.assertEqual((await timed.expire_one(self.bot, self.holding, drop_requested=True))['status'], 'skipped')
        self.assertEqual((await self.events()).count('dropped'), 1)
        self.assertIsNone(await equip.get_equipped(GUILD, USER))

    async def test_independent_entitlement_keeps_discord_role(self):
        await set_quantity(GUILD, USER, 'Permanent', 1, item_type='role', metadata={'role_id': 9511})
        result = await timed.expire_one(self.bot, self.holding, drop_requested=True)
        self.assertEqual(result['status'], 'removed')
        self.person.remove_roles.assert_not_awaited()
        self.assertIn(self.role, self.person.roles)
        self.assertEqual((await self.events()).count('dropped'), 1)

    async def test_cancelled_removal_leaves_retry_marker_and_no_drop(self):
        self.person.remove_roles = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await timed.expire_one(self.bot, self.holding, drop_requested=True)
        self.assertIsNotNone(await timed.get_timed_holding(self.holding['entry_key']))
        self.assertNotIn('dropped', await self.events())
        self.person.remove_roles = AsyncMock()
        self.assertEqual((await asyncio.wait_for(timed.expire_one(self.bot, self.holding, drop_requested=True), 3))['status'], 'removed')

    async def test_expired_confirmation_refused(self):
        execute('UPDATE timed_holdings SET expires_at=? WHERE id=?', (iso_in(-1), self.holding['id']))
        self.assertEqual((await timed.expire_one(self.bot, self.holding, drop_requested=True))['status'], 'skipped')
        self.person.remove_roles.assert_not_awaited()
        self.assertNotIn('dropped', await self.events())

class DuplicateRolePurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=500)
        purge_timed()
        execute('DELETE FROM inventory_activity')

    async def test_duplicate_purchase_refunds_and_keeps_original_grant(self):
        from phase1_support import rows
        from test_timed_ownership import shop_item
        from cogs.shop import process_purchase
        role = role_stub(9512, 'Permanent')
        item = shop_item('role', price=100, role_id=9512, name='Permanent')
        itx, _, _, _ = purchase_ctx(granted_role=role)
        await process_purchase(itx, item)
        original = rows("SELECT id, quantity, metadata FROM inventory_items WHERE item_name='Permanent'")
        self.assertEqual(len(original), 1)
        self.assertEqual(rows('SELECT balance FROM economy WHERE guild_id=? AND user_id=?', (GUILD, USER)), [(400,)])
        itx, _, _, _ = purchase_ctx(granted_role=role)
        await process_purchase(itx, item)
        self.assertEqual(rows("SELECT id, quantity, metadata FROM inventory_items WHERE item_name='Permanent'"), original)
        self.assertEqual(rows('SELECT balance FROM economy WHERE guild_id=? AND user_id=?', (GUILD, USER)), [(400,)])
        self.assertEqual([e['event_type'] for e in await get_activity_page(GUILD, USER)].count('acquired'), 1)
