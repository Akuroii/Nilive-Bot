"""Real Discord-flow tests: /shop paging, published purchase controls, item panel.

Run as its own process (like the other suites): the scratch DB is bound at import.
    OWNER_ID=1 python3 -m pytest tests/test_inventory_shop_flows.py -q
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts", ROOT / "tests"):
    sys.path.insert(0, str(p))

from phase1_support import GUILD, USER, execute, reset_database, rows, seed_listing, seed_member  # noqa: E402
from test_f01_f03_role_reconciliation_and_purchase import (  # noqa: E402
    BLUE, FLAME, balance_of, make_discord_env, make_interaction, worn_ids)
from utils import inventory, potion_engine, shop_publication, shop_publisher  # noqa: E402
from utils.potion_engine import get_active_effects  # noqa: E402
import cogs.shop as shop_cog  # noqa: E402
import cogs.wallet as wallet  # noqa: E402

CURRENCY = {"coins": {"name": "Coins", "emoji": "🪙"}, "diamonds": {"name": "Diamonds", "emoji": "💎"}}


def add_product(name, price, *, stock=None, desc="A product", kind="custom"):
    return execute(
        "INSERT INTO shop_items (guild_id, name, type, price, description, max_stock, current_stock, enabled) "
        "VALUES (?,?,?,?,?,?,?,1)",
        # max_stock 0/NULL means "unlimited"; sold out is max_stock >= 1 with current_stock 0
        (GUILD, name, kind, price, desc, None if stock is None else max(1, stock), stock))


def components_of(view):
    return [c for row in view.to_components() for c in row["components"]]


class ShopFlowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=100000)
        self.bot, self.guild, self.member, self.roles = make_discord_env()
        self.guild.name = "Test Guild"

    def shop_interaction(self):
        ix = make_interaction(self.bot, self.guild, self.member)
        ix.response.edit_message = AsyncMock()
        return ix

    async def open_shop(self):
        from cogs.shop import _show_shop_page
        ix = self.shop_interaction()
        with patch.object(shop_cog, "get_currency_config", AsyncMock(return_value=CURRENCY)):
            await _show_shop_page(ix, 0)
        return ix.response.send_message.await_args.kwargs

    async def click_nav(self, view, label):
        button = next(c for c in view.children if getattr(c, "label", "") == label)
        ix = self.shop_interaction()
        with patch.object(shop_cog, "get_currency_config", AsyncMock(return_value=CURRENCY)):
            await button.callback(ix)
        return ix.response.edit_message.await_args.kwargs

    def purchase_click(self, custom_id, values=None):
        ix = make_interaction(self.bot, self.guild, self.member)
        ix.type = discord.InteractionType.component
        ix.data = {"custom_id": custom_id, **({"values": values} if values is not None else {})}
        return ix


class ShopPagingTests(ShopFlowBase):
    async def test_five_or_fewer_products_keep_the_original_buttons(self):
        ids = [add_product(f"Item{i}", 10 * (i + 1)) for i in range(4)]
        sold_out = add_product("SoldOut", 5, stock=0)  # out of stock: field shown, no button
        message = await self.open_shop()
        view, embed = message["view"], message["embed"]
        self.assertEqual(len(embed.fields), 5)
        self.assertEqual(sorted(c.custom_id for c in view.children if hasattr(c, "custom_id")),
                         sorted(f"shop_buy_{i}" for i in ids))
        self.assertNotIn(f"shop_buy_{sold_out}", [c.custom_id for c in view.children])
        self.assertFalse(any(isinstance(c, discord.ui.Select) for c in view.children))

    async def test_every_in_stock_product_is_reachable_across_pages(self):
        ids = [add_product(f"Item{i:02d}", 10 * (i + 1)) for i in range(27)]
        add_product("SoldOut", 1, stock=0)
        seed_listing(tier=6, stock=2)  # Prestige VI: Booster only, this member is not a booster
        reachable, page_count, message = set(), 0, await self.open_shop()
        while True:
            page_count += 1
            embed, view = message["embed"], message["view"]
            self.assertLessEqual(len(embed.fields), 25)
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(view.to_components()), 5)
            for select in (c for c in view.children if isinstance(c, discord.ui.Select)):
                self.assertLessEqual(len(select.options), 25)
                self.assertTrue(select.custom_id.startswith("shop_buy_sel_"))
                self.assertTrue(select.custom_id[len("shop_buy_sel_"):].isdigit())
                reachable.update(int(o.value.removeprefix("shop_buy_")) for o in select.options)
            nxt = next(c for c in view.children if getattr(c, "label", "") == "Next ▶")
            if nxt.disabled:
                break
            message = await self.click_nav(view, "Next ▶")
        self.assertGreater(page_count, 1)
        self.assertEqual(reachable, set(ids), "every purchasable product must be reachable; sold-out and booster-only are not offered")

    async def test_long_descriptions_never_exceed_discord_limits(self):
        for i in range(12):
            add_product(f"Long{i:02d}", i + 1, desc="x" * 1500)
        message = await self.open_shop()
        while True:
            self.assertLessEqual(len(message["embed"]), 6000)
            for f in message["embed"].fields:
                self.assertLessEqual(len(f.value), 1024)
                self.assertLessEqual(len(f.name), 256)
            nxt = next(c for c in message["view"].children if getattr(c, "label", "") == "Next ▶")
            if nxt.disabled:
                break
            message = await self.click_nav(message["view"], "Next ▶")

    async def test_select_on_a_later_page_completes_a_real_purchase(self):
        ids = [add_product(f"Item{i:02d}", 10 * (i + 1)) for i in range(14)]
        message = await self.open_shop()
        message = await self.click_nav(message["view"], "Next ▶")
        select = next(c for c in message["view"].children if isinstance(c, discord.ui.Select))
        value = select.options[0].value
        item_id = int(value.removeprefix("shop_buy_"))
        self.assertIn(item_id, ids[10:])
        before = balance_of()
        price = rows("SELECT price FROM shop_items WHERE id=?", (item_id,))[0][0]
        ix = self.purchase_click(select.custom_id, [value])
        await shop_cog.Shop.on_interaction(None, ix)
        self.assertEqual(balance_of(), before - price)
        name = rows("SELECT name FROM shop_items WHERE id=?", (item_id,))[0][0]
        owned = rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name=?",
                     (GUILD, USER, name))
        self.assertEqual(owned, [(1,)])


class PublishedControlTests(ShopFlowBase):
    def build(self, kind, ids, *, root=None):
        rows_by_id = {i: {"id": i, "name": f"P{i}", "price": 10, "type": "custom", "enabled": 1,
                          **({"option_of_id": root} if root else {})} for i in ids}
        roster = {i for i in ids}
        if root:  # option_select: the entries are options of a root product that is in the roster
            rows_by_id[root] = {"id": root, "name": "Root", "price": 10, "type": "custom", "enabled": 1}
            roster = {root}
        cfg = {"kind": kind, "entries": [{"product_id": i} for i in ids]}
        action = shop_publisher.build_purchase_action(cfg, rows_by_id, CURRENCY, context_id=0)
        self.assertEqual(shop_publisher.validate_action_entries(cfg, rows_by_id, roster), [])
        return action

    async def dispatch_all(self, components, expected_ids):
        seen = []
        for comp in components:
            if comp["type"] == 2:
                clicks = [self.purchase_click(comp["custom_id"])]
            else:
                clicks = [self.purchase_click(comp["custom_id"], [o["value"]]) for o in comp["options"]]
            for ix in clicks:
                with patch.object(shop_cog, "process_purchase", AsyncMock()) as buy:
                    await shop_cog.Shop.on_interaction(None, ix)
                buy.assert_awaited_once()
                seen.append(buy.await_args.args[1])
        self.assertEqual(sorted(seen), sorted(expected_ids))

    async def test_25_published_buttons_fit_five_rows_and_each_buys_its_product(self):
        ids = list(range(100, 125))
        rows_ = shop_publication.action_components(self.build("buttons", ids))
        self.assertEqual(len(rows_), 5)
        self.assertTrue(all(len(r["components"]) <= 5 for r in rows_))
        await self.dispatch_all([c for r in rows_ for c in r["components"]], ids)

    async def test_published_selects_dispatch_every_option_to_its_product(self):
        for kind in ("product_select", "option_select"):
            ids = list(range(200, 225))
            root = 199 if kind == "option_select" else None
            rows_ = shop_publication.action_components(self.build(kind, ids, root=root))
            comp = rows_[0]["components"][0]
            self.assertEqual(comp["custom_id"], "shop_buy_sel_0")
            self.assertEqual(len(comp["options"]), 25)
            await self.dispatch_all([comp], ids)

    async def test_oversized_actions_are_rejected_before_publishing(self):
        ids = list(range(300, 326))
        rows_by_id = {i: {"id": i, "name": f"P{i}", "price": 10, "type": "custom", "enabled": 1} for i in ids}
        for kind in ("buttons", "product_select"):
            cfg = {"kind": kind, "entries": [{"product_id": i} for i in ids]}
            problems = shop_publisher.validate_action_entries(cfg, rows_by_id, set(ids))
            self.assertIn("action_entry_count", [p["code"] for p in problems])


class ItemPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=1000)
        self.bot, self.guild, self.member, self.roles = make_discord_env()
        good = potion_engine.build_metadata("xp_boost", 2.0, 2)
        self.stock = [
            ("RoleA", 1, "role", {"role_id": FLAME}),
            ("ColorRole", 1, "role", {"role_id": BLUE, "category": "color"}),
            ("TitleA", 1, "title", {}),
            ("Tonic", 2, "potion", good),
            ("ColorTonic", 1, "potion", {**good, "category": "color"}),
            ("BadTonic", 1, "potion", {"effect": "xp_boost"}),
            ("Trinket", 1, "shop_custom", {}),
            ("Drop", 1, "event_drop", {}),
        ]
        for name, qty, kind, meta in self.stock:
            await inventory.set_quantity(GUILD, USER, name, qty, item_type=kind, source="shop", metadata=meta)
        self.keys = {e["item_name"]: e["entry_key"] for e in await inventory.get_inventory_entries(GUILD, USER)}

    def itx(self):
        return SimpleNamespace(
            user=self.member, guild=self.guild, guild_id=GUILD, client=self.bot, application_id=1,
            message=SimpleNamespace(),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False))

    async def render(self, name, tab):
        ix = self.itx()
        await wallet.open_item_panel(ix, GUILD, USER, tab, self.keys[name])
        self.assertFalse(ix.response.send_message.await_count, "panel was rejected")
        kw = ix.response.edit_message.await_args.kwargs
        return ([(c.label, c.disabled) for c in kw["view"].children],
                {f.name: f.value for f in kw["embed"].fields}, kw["view"])

    async def test_each_item_type_exposes_only_its_own_action(self):
        T = wallet
        expected = [
            ("RoleA", T.TAB_ITEMS, [("Equip", False), ("Back", False)]),
            ("ColorRole", T.TAB_COLORS, [("Equip", False), ("Back", False)]),
            ("TitleA", T.TAB_TITLES, [("Equip", False), ("Back", False)]),
            ("Tonic", T.TAB_POTIONS, [("Activate", False), ("Back", False)]),
            ("ColorTonic", T.TAB_POTIONS, [("Activate", False), ("Back", False)]),
            ("ColorTonic", T.TAB_COLORS, [("Activate", False), ("Back", False)]),
            ("BadTonic", T.TAB_POTIONS, [("Activate", True), ("Back", False)]),
            ("Trinket", T.TAB_ITEMS, [("Back", False)]),
            ("Drop", T.TAB_ITEMS, [("Back", False)]),
        ]
        for name, tab, buttons in expected:
            labels, fields, _ = await self.render(name, tab)
            self.assertEqual(labels, buttons, f"{name} on {tab}")
            if name.endswith("Tonic") and not name.startswith("Bad"):
                self.assertEqual(fields.get("Type"), "Consumable", f"{name} on {tab}")
                self.assertIn("2× XP", fields.get("Effect", ""), f"{name} on {tab}")
            if name in ("Trinket", "Drop"):
                self.assertEqual(fields.get("Type"), "Collectible")
                self.assertNotIn("Status", fields)

    async def press(self, view, label):
        button = next(c for c in view.children if c.label == label)
        ix = self.itx()
        await button.callback(ix)
        return ix

    async def test_role_equip_then_unequip_through_the_buttons(self):
        _, _, view = await self.render("RoleA", wallet.TAB_ITEMS)
        ix = await self.press(view, "Equip")
        self.assertEqual(worn_ids(self.member), [FLAME])
        view = ix.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([c.label for c in view.children], ["Unequip", "Back"])
        ix = await self.press(view, "Unequip")
        self.assertEqual(worn_ids(self.member), [])
        view = ix.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([c.label for c in view.children], ["Equip", "Back"])

    async def test_title_equip_then_unequip_through_the_buttons(self):
        _, _, view = await self.render("TitleA", wallet.TAB_TITLES)
        ix = await self.press(view, "Equip")
        view = ix.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([c.label for c in view.children], ["Unequip", "Back"])
        self.assertEqual(rows("SELECT item_name FROM equipped_titles WHERE guild_id=? AND user_id=?", (GUILD, USER)),
                         [("TitleA",)])
        ix = await self.press(view, "Unequip")
        self.assertEqual(rows("SELECT item_name FROM equipped_titles WHERE guild_id=? AND user_id=?", (GUILD, USER)), [])

    async def test_potion_activation_consumes_one_and_shows_the_active_boost(self):
        _, _, view = await self.render("Tonic", wallet.TAB_POTIONS)
        ix = await self.press(view, "Activate")
        left = rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Tonic'",
                    (GUILD, USER))
        self.assertEqual(left, [(1,)])
        self.assertEqual(len(await get_active_effects(GUILD, USER)), 1)
        embed = ix.response.edit_message.await_args.kwargs["embed"]
        self.assertIn("Active XP boost", [f.name for f in embed.fields])
        # a second click on the same, now stale, panel must not use another potion
        ix2 = await self.press(view, "Activate")
        self.assertTrue(ix2.response.send_message.await_count)
        left = rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Tonic'",
                    (GUILD, USER))
        self.assertEqual(left, [(1,)])


if __name__ == "__main__":
    unittest.main()
