#!/usr/bin/env python3
"""Focused tests for Shop Select component dispatch.

These tests exercise only the on_interaction routing boundary. Existing
process_purchase tests remain the source of truth for purchase behavior.

Run: python3 scripts/test_shop_select_dispatch.py
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Establish the repository's isolated scratch environment before importing
# database-backed cogs; this dispatch test itself performs no DB operations.
import phase1_support  # noqa: F401,E402

import discord
from cogs.shop import Shop


class ShopSelectDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Bypass Shop.__init__, which starts the unrelated cleanup task.
        self.shop = Shop.__new__(Shop)

    def interaction(self, custom_id, values=None, component=True):
        data = {"custom_id": custom_id}
        if values is not None:
            data["values"] = values
        return SimpleNamespace(
            type=(discord.InteractionType.component if component else "other"),
            data=data,
        )

    async def dispatch(self, interaction):
        with patch("cogs.shop.process_purchase", new_callable=AsyncMock) as purchase:
            await Shop.on_interaction(self.shop, interaction)
            return purchase

    async def test_button_dispatch_is_unchanged(self):
        interaction = self.interaction("shop_buy_123")
        purchase = await self.dispatch(interaction)
        purchase.assert_awaited_once_with(interaction, 123)

    async def test_product_select_dispatches_exact_root_id_once(self):
        interaction = self.interaction(
            "shop_buy_sel_42", ["shop_buy_17"])
        purchase = await self.dispatch(interaction)
        purchase.assert_awaited_once_with(interaction, 17)

    async def test_option_select_dispatches_exact_option_id_once(self):
        interaction = self.interaction(
            "shop_buy_sel_314", ["shop_buy_902"])
        purchase = await self.dispatch(interaction)
        purchase.assert_awaited_once_with(interaction, 902)

    async def test_missing_values_are_rejected(self):
        for values in (None, []):
            with self.subTest(values=values):
                interaction = self.interaction("shop_buy_sel_1", values)
                purchase = await self.dispatch(interaction)
                purchase.assert_not_awaited()

    async def test_multiple_values_are_rejected(self):
        interaction = self.interaction(
            "shop_buy_sel_1", ["shop_buy_7", "shop_buy_8"])
        purchase = await self.dispatch(interaction)
        purchase.assert_not_awaited()

    async def test_malformed_values_are_rejected(self):
        for value in ("shop_buy_", "shop_buy_x", "shop_buy_7x",
                      "shop_buy_-7", "shop_buy_7.0", 7, None):
            with self.subTest(value=value):
                interaction = self.interaction("shop_buy_sel_1", [value])
                purchase = await self.dispatch(interaction)
                purchase.assert_not_awaited()

    async def test_unrelated_values_are_rejected(self):
        for value in ("other_7", "shop_buy_sel_7", "inventory_equip_select"):
            with self.subTest(value=value):
                interaction = self.interaction("shop_buy_sel_1", [value])
                purchase = await self.dispatch(interaction)
                purchase.assert_not_awaited()

    async def test_malformed_select_component_id_is_rejected(self):
        interaction = self.interaction("shop_buy_sel_context", ["shop_buy_7"])
        purchase = await self.dispatch(interaction)
        purchase.assert_not_awaited()

    async def test_non_component_interaction_is_ignored(self):
        interaction = self.interaction(
            "shop_buy_sel_1", ["shop_buy_7"], component=False)
        purchase = await self.dispatch(interaction)
        purchase.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
