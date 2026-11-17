"""Six-tab navigation and stable selection regressions (Discord stubs)."""
import os
import unittest
os.environ.setdefault('OWNER_ID', '999999999')
os.environ.setdefault('SECRET_KEY', 'testsecretkey0123456789abcdef0123456789')
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from cogs import wallet


def entry(name, key, *, item_type='custom', timed=False, category=None):
    return {'id': int(key.split(':')[1]), 'entry_key': key, 'item_name': name,
            'item_type': item_type, 'quantity': 1, 'timed': timed,
            'expires_at': '2035-01-01T00:00:00+00:00' if timed else None,
            'metadata': {'category': category} if category else {},
            'category': category, 'rarity': 'common', 'icon_url': ''}


class SixTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_tabs_fit_discord_rows_and_history_has_no_select(self):
        tabs = [wallet.TAB_ITEMS, wallet.TAB_COLORS, wallet.TAB_POTIONS,
                wallet.TAB_TITLES, wallet.TAB_EQUIPPED, wallet.TAB_HISTORY]
        for tab in tabs:
            view = wallet.InventoryView(1, 2, tab, [], None, None, 1, 3)
            buttons = [x for x in view.children if isinstance(x, wallet.InventoryTabButton)]
            self.assertEqual([x.tab for x in buttons], tabs)
            self.assertTrue(next(x for x in buttons if x.tab == tab).disabled)
            self.assertEqual(sum(isinstance(x, wallet.InventoryItemSelect) for x in view.children),
                             int(tab != wallet.TAB_HISTORY))
            for row in range(5):
                self.assertLessEqual(sum(getattr(x, 'row', None) == row for x in view.children), 5)
            view.stop()

    async def test_selection_identifies_duplicate_holdings_and_preserves_page(self):
        entries = [entry('Ocean', 'i:1'), entry('Ocean', 'h:2', timed=True)]
        select = wallet.InventoryItemSelect(1, 2, wallet.TAB_ITEMS, entries, None, None, page=2)
        self.assertEqual([o.value for o in select.options], ['i:1', 'h:2'])
        self.assertNotEqual(select.options[0].description, select.options[1].description)
        select._values = ['h:2']
        interaction = object()
        with patch.object(wallet, 'open_item_panel', AsyncMock()) as opened:
            await select.callback(interaction)
            opened.assert_awaited_once_with(interaction, 1, 2, wallet.TAB_ITEMS, 'h:2', page=2)

    async def test_color_classification_does_not_guess_from_role(self):
        ordinary = entry('Legacy role', 'i:1', item_type='role')
        color = entry('Blue', 'h:2', item_type='temp_role', timed=True, category='colors')
        tabs = wallet.split_by_tab([ordinary, color])
        self.assertEqual(tabs[wallet.TAB_ITEMS], [ordinary])
        self.assertEqual(tabs[wallet.TAB_COLORS], [color])

    async def test_ordinary_pagination_and_return_page(self):
        items = [entry(f'Item {n:02}', f'i:{n+1}') for n in range(31)]
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(response=response, message=object())
        with patch.object(wallet, 'get_inventory_entries', AsyncMock(return_value=items)), \
             patch.object(wallet, 'cleanup_unowned_title', AsyncMock()), \
             patch.object(wallet, '_decorate', AsyncMock(side_effect=lambda gid, entries, **kwargs: entries)), \
             patch.object(wallet, 'get_currency_config', AsyncMock(return_value={'coins': {'name': 'Coins', 'emoji': '🪙'}, 'diamonds': {'name': 'Diamonds', 'emoji': '💎'}})), \
             patch.object(wallet, 'get_balance', AsyncMock(return_value=0)), \
             patch.object(wallet, 'get_equipped', AsyncMock(return_value=None)), \
             patch.object(wallet, 'get_equipped_title', AsyncMock(return_value=None)):
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_ITEMS, page=1)
            view = response.edit_message.await_args.kwargs['view']
            select = next(x for x in view.children if isinstance(x, wallet.InventoryItemSelect))
            self.assertEqual(len(select.options), 15)
            self.assertEqual(select.options[0].value, 'i:16')
            self.assertEqual(wallet.INVENTORY_FOOTER_TEXT,
                             response.edit_message.await_args.kwargs['embed'].footer.text)
            self.assertEqual(view.page, 1)
            view.stop()

    async def test_pagination_boundaries_cover_every_entry_once(self):
        for count in (0, 1, 15, 16, 17, 30, 31, 34):
            items = [entry(f'Item {n:02}', f'i:{n+1}') for n in range(count)]
            seen = []
            pages = max(1, (count + 14) // 15)
            for page in range(pages):
                visible = items[page * 15:(page + 1) * 15]
                view = wallet.InventoryView(1, 2, wallet.TAB_ITEMS, visible, None, None, page, pages)
                select = next(x for x in view.children if isinstance(x, wallet.InventoryItemSelect))
                if visible:
                    seen.extend(o.value for o in select.options)
                else:
                    self.assertTrue(select.disabled)
                self.assertEqual(sum(isinstance(x, wallet.HistoryPageButton) for x in view.children),
                                 int(page > 0) + int(page + 1 < pages))
                view.stop()
            self.assertEqual(seen, [i['entry_key'] for i in items])

    async def test_page_and_detail_back_callbacks_preserve_tab_page(self):
        interaction = object()
        view = wallet.InventoryView(1, 2, wallet.TAB_COLORS, [], None, None, 1, 3)
        arrow = next(x for x in view.children if isinstance(x, wallet.HistoryPageButton) and x.direction == 1)
        with patch.object(wallet, 'open_inventory_panel', AsyncMock()) as opened:
            await arrow.callback(interaction)
            opened.assert_awaited_once_with(interaction, 1, 2, wallet.TAB_COLORS, page=2)
        item = entry('Blue', 'i:5', item_type='role', category='color')
        item['_page'] = 2
        detail = wallet.ItemPanelView(1, 2, wallet.TAB_COLORS, item, False)
        back = next(x for x in detail.children if isinstance(x, wallet.ItemBackButton))
        with patch.object(wallet, 'open_inventory_panel', AsyncMock()) as opened:
            await back.callback(interaction)
            opened.assert_awaited_once_with(interaction, 1, 2, wallet.TAB_COLORS, page=2)
        detail.stop(); view.stop()

    async def test_balance_is_not_a_selectable_item(self):
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(response=response, message=object())
        items = [entry('Sword', 'i:1')]
        config = {'coins': {'name': 'Gold', 'emoji': '🟡'}, 'diamonds': {'name': 'Gems', 'emoji': '💠'}}
        with patch.object(wallet, 'get_inventory_entries', AsyncMock(return_value=items)), \
             patch.object(wallet, 'cleanup_unowned_title', AsyncMock()), \
             patch.object(wallet, '_decorate', AsyncMock(side_effect=lambda gid, values, **kwargs: values)), \
             patch.object(wallet, 'get_currency_config', AsyncMock(return_value=config)), \
             patch.object(wallet, 'get_balance', AsyncMock(side_effect=[1234, 3])), \
             patch.object(wallet, 'get_equipped', AsyncMock(return_value=None)), \
             patch.object(wallet, 'get_equipped_title', AsyncMock(return_value=None)):
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_ITEMS)
            rendered = response.edit_message.await_args.kwargs
            # PR #120 restored balances as description rows, not embed fields.
            self.assertEqual(rendered['embed'].fields, [])
            self.assertIn('1,234', rendered['embed'].description)
            self.assertIn('Gems', rendered['embed'].description)
            select = next(x for x in rendered['view'].children if isinstance(x, wallet.InventoryItemSelect))
            self.assertEqual([o.value for o in select.options], ['i:1'])
            rendered['view'].stop()

    async def test_owner_guard_rejects_other_user(self):
        view = wallet.InventoryView(1, 2, wallet.TAB_ITEMS, [], None, None)
        interaction = SimpleNamespace(user=SimpleNamespace(id=3), guild_id=1,
                                      response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()
        view.stop()

    async def test_no_destructive_action_on_any_item_type(self):
        expected = {'custom': ['Back'], 'temp_role': ['Equip', 'Back'],
                    'potion': ['Activate', 'Back'], 'title': ['Equip', 'Back']}
        for kind, labels in expected.items():
            for key, timed in (('h:2', True), ('i:2', False)):
                item = entry('Thing', key, item_type=kind, timed=timed)
                view = wallet.ItemPanelView(1, 2, wallet.TAB_ITEMS, item, False)
                self.assertEqual([x.label for x in view.children], labels)
                view.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
