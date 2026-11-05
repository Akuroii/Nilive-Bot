"""Focused tests for the current Inventory entry point and action guards."""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('OWNER_ID', '999999999')
os.environ.setdefault('SECRET_KEY', 'testsecretkey0123456789abcdef0123456789')

from cogs import wallet


class InventorySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slot_lock_serializes_same_member_only(self):
        from utils.equip_engine import slot_lock, _serialised_per_member
        entered = asyncio.Event()
        release = asyncio.Event()
        second_entered = asyncio.Event()

        @_serialised_per_member
        async def action(bot, guild_id, user_id, first):
            if first:
                entered.set()
                await release.wait()
            else:
                second_entered.set()

        first = asyncio.create_task(action(None, 1, 2, True))
        await entered.wait()
        second = asyncio.create_task(action(None, 1, 2, False))
        await asyncio.sleep(0)
        self.assertFalse(second_entered.is_set())
        self.assertIsNot(slot_lock(1, 2), slot_lock(1, 3))
        release.set()
        await asyncio.gather(first, second)
        self.assertTrue(second_entered.is_set())

    async def test_entry_points_share_renderer(self):
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(response=response, original_response=AsyncMock(return_value='message'), message='existing')
        with patch.object(wallet, 'cleanup_unowned_title', AsyncMock()), \
             patch.object(wallet, 'get_inventory_entries', AsyncMock(return_value=[])), \
             patch.object(wallet, '_decorate', AsyncMock(return_value=[])), \
             patch.object(wallet, 'get_currency_config', AsyncMock(return_value={'coins': {'name': 'Coins', 'emoji': '🪙'}, 'diamonds': {'name': 'Diamonds', 'emoji': '💎'}})), \
             patch.object(wallet, 'get_balance', AsyncMock(return_value=0)), \
             patch.object(wallet, 'get_equipped', AsyncMock(return_value=None)), \
             patch.object(wallet, 'get_equipped_title', AsyncMock(return_value=None)):
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_ITEMS, first=True)
            self.assertTrue(response.send_message.await_args.kwargs['ephemeral'])
            self.assertEqual(response.send_message.await_count, 1)
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_ITEMS)
            self.assertEqual(response.edit_message.await_count, 1)

    async def test_failed_unequip_never_drops(self):
        interaction = SimpleNamespace(client=object(), response=SimpleNamespace(send_message=AsyncMock()))
        view = wallet.DropConfirmView(1, 2, wallet.TAB_ITEMS, 'role')
        with patch.object(wallet, 'get_equipped', AsyncMock(return_value={'item_name': 'role'})), \
             patch.object(wallet, 'unequip_role', AsyncMock(return_value={'success': False, 'error': 'denied'})), \
             patch.object(wallet, 'drop_item', AsyncMock()) as drop:
            await view.confirm_button.callback(interaction)
            drop.assert_not_awaited()
            self.assertIn('denied', interaction.response.send_message.await_args.args[0])

    async def test_failed_title_unequip_never_drops(self):
        interaction = SimpleNamespace(client=object(), response=SimpleNamespace(send_message=AsyncMock()))
        view = wallet.DropConfirmView(1, 2, wallet.TAB_TITLES, 'title')
        with patch.object(wallet, 'get_equipped_title', AsyncMock(return_value={'item_name': 'title'})), \
             patch.object(wallet, 'unequip_title', AsyncMock(return_value={'success': False, 'error': 'stale'})), \
             patch.object(wallet, 'drop_item', AsyncMock()) as drop:
            await view.confirm_button.callback(interaction)
            drop.assert_not_awaited()
            self.assertIn('stale', interaction.response.send_message.await_args.args[0])

    async def test_history_navigation_and_partial_coverage_label(self):
        from utils import inventory_activity
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(response=response, message='existing')
        with patch.object(inventory_activity, 'count_activity', AsyncMock(return_value=16)), \
             patch.object(inventory_activity, 'get_activity_page', AsyncMock(return_value=[{
                 'item_name': 'Sword', 'source': 'mission', 'event_type': 'acquired',
                 'quantity': 1, 'created_at': '2026-01-01 00:00:00'}])) as get_page, \
             patch.object(inventory_activity, 'get_activity_start', AsyncMock(return_value='2026-01-01 00:00:00')):
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_HISTORY, page=1)
            self.assertEqual(get_page.await_args.kwargs['offset'], 15)
            embed = response.edit_message.await_args.kwargs['embed']
            self.assertIn('Sword', embed.description)
            self.assertIn('Partial coverage', embed.footer.text)
            view = response.edit_message.await_args.kwargs['view']
            self.assertIn('Previous', [child.label for child in view.children])
            self.assertNotIn('Next', [child.label for child in view.children])

    async def test_prepared_emoji_precedes_legacy_and_fallback(self):
        self.assertEqual(wallet._item_display({'app_emoji': '<:ready:123>', 'icon_url': '<:old:456>'}, '⚪'), '<:ready:123>')
        self.assertEqual(wallet._item_display({'icon_url': '<:old:456>'}, '⚪'), '<:old:456>')
        self.assertEqual(wallet._item_display({'icon_url': 'https://example.org/a.png'}, '⚪'), '⚪')

    async def test_custom_item_does_not_offer_equip(self):
        view = wallet.ItemPanelView(1, 2, wallet.TAB_ITEMS,
                                    {'item_name': 'rock', 'item_type': 'custom'}, False)
        self.assertNotIn('Equip', [child.label for child in view.children])
        self.assertIn('Drop', [child.label for child in view.children])


if __name__ == '__main__':
    unittest.main()
