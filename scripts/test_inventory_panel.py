"""Decorated six-tab Inventory panel (discord.py object stubs).

Verifies the agreed embed presentation + component behaviour without any
Discord socket. Builds the real embeds/views from cogs.wallet.

Usage:
    PYTHONPATH=. python scripts/test_inventory_panel.py
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OWNER_ID", "999999999")
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

from cogs import wallet  # noqa: E402


def entry(name, key, *, item_type="custom", timed=False, category=None,
          quantity=1, rarity="common", icon_url="", app_emoji="",
          metadata=None):
    return {"id": int(key.split(":")[1]) if ":" in key else 1, "entry_key": key,
            "item_name": name, "item_type": item_type, "quantity": quantity,
            "timed": timed,
            "expires_at": "2035-01-01 00:00:00+00:00" if timed else None,
            "metadata": metadata if metadata is not None else ({"category": category} if category else {}),
            "category": category, "rarity": rarity, "icon_url": icon_url,
            "app_emoji": app_emoji}


class EmbedTests(unittest.TestCase):
    def test_smallcaps_labels_on_all_tab_buttons(self):
        view = wallet.InventoryView(1, 2, wallet.TAB_TITLES, [], None, None, 0, 1)
        buttons = {x.tab: x for x in view.children
                   if isinstance(x, wallet.InventoryTabButton)}
        self.assertEqual(buttons[wallet.TAB_ITEMS].label, "ɪᴛᴇᴍꜱ")
        self.assertEqual(buttons[wallet.TAB_COLORS].label, "ᴄᴏʟᴏʀꜱ")
        self.assertEqual(buttons[wallet.TAB_POTIONS].label, "ᴘᴏᴛɪᴏɴꜱ")
        self.assertEqual(buttons[wallet.TAB_TITLES].label, "ᴛɪᴛʟᴇꜱ")
        self.assertEqual(buttons[wallet.TAB_EQUIPPED].label, "ᴇǫᴜɪᴘᴘᴇᴅ")
        self.assertEqual(buttons[wallet.TAB_HISTORY].label, "ʜɪꜱᴛᴏʀʏ")
        view.stop()

    def test_brand_author_uses_the_agreed_author_word(self):
        interaction = SimpleNamespace(
            client=SimpleNamespace(user=SimpleNamespace(name="Nero")),
            user=SimpleNamespace(display_name="Shadow", name="Shadow"),
            guild_id=1)
        embed = discord.Embed()
        wallet._brand_inventory(embed, interaction, wallet.TAB_TITLES)
        self.assertEqual(embed.author.name, "Nero: \U0001d413\U0001d408\U0001d413\U0001d40b\U0001d404\U0001d412 Shadow")
        self.assertEqual(embed.author.icon_url, wallet.INVENTORY_AUTHOR_ICON)
        self.assertEqual(embed.color.value, wallet.INVENTORY_COLOR)
        # The footer is the branded line, exactly — never a page counter.
        self.assertEqual(embed.footer.text, wallet.INVENTORY_FOOTER_TEXT)
        self.assertEqual(embed.footer.icon_url, wallet.INVENTORY_FOOTER_ICON)

    def test_panel_has_no_competing_title_or_extra_fields(self):
        """The agreed panel: author line, description, colour, footer only."""
        for tab in (wallet.TAB_ITEMS, wallet.TAB_COLORS, wallet.TAB_POTIONS,
                    wallet.TAB_TITLES):
            embed = wallet.build_inventory_embed(
                tab, [entry("Moon Ball", "i:1", rarity="epic")], None, None)
            self.assertIsNone(embed.title, tab)
            self.assertEqual(len(embed.fields), 0, tab)
            self.assertEqual(embed.color.value, wallet.INVENTORY_COLOR, tab)

    def test_every_row_uses_the_agreed_line_template(self):
        entries = [entry("Moon Ball", "i:1", rarity="epic", quantity=3)]
        embed = wallet.build_inventory_embed(wallet.TAB_ITEMS, entries, None, None)
        self.assertEqual(embed.description,
                         "> - 🟣 • ` 3 ` • ` Moon Ball ` • *Epic*")
        # A name carrying a backtick/newline cannot break out of the span.
        self.assertEqual(
            wallet._inventory_line("🎒", 3, "bad`name\nsecond line"),
            "> - 🎒 • ` 3 ` • ` bad'name second line `")
        # Inline code is literal: a backslash must not be doubled into a
        # visible "\\", and whitespace is collapsed.
        self.assertEqual(
            wallet._inventory_line("🎒", 1, "  a\\b\t c "),
            "> - 🎒 • ` 1 ` • ` a\\b c `")

    def test_currency_balances_are_rows_not_a_field(self):
        rows = [{"emoji": "<:silver:111>", "name": "Silvers", "quantity": 11024},
                {"emoji": "<:pearl:222>", "name": "Pearls", "quantity": 399}]
        embed = wallet.build_inventory_embed(
            wallet.TAB_ITEMS, [entry("Moon Ball", "i:1")], None, None,
            currency_rows=rows)
        self.assertEqual(len(embed.fields), 0)
        self.assertIn("> - <:silver:111> • ` 11,024 ` • ` Silvers `",
                      embed.description)
        self.assertIn("> - <:pearl:222> • ` 399 ` • ` Pearls `", embed.description)
        # Balances belong to the Items view only.
        other = wallet.build_inventory_embed(
            wallet.TAB_POTIONS, [], None, None, currency_rows=rows)
        self.assertNotIn("Silvers", other.description or "")

    def test_items_embed_backticks_name_and_prepared_emoji(self):
        entries = [entry("Moon Ball", "i:1", rarity="epic",
                         app_emoji="<:moon:1562903222086279178>")]
        embed = wallet.build_inventory_embed(wallet.TAB_ITEMS, entries, None, None)
        self.assertIn("<:moon:1562903222086279178>", embed.description)
        self.assertIn("` Moon Ball `", embed.description)
        self.assertIn("*Epic*", embed.description)

    def test_items_embed_timed_entry_shows_remaining_time(self):
        entries = [entry("Temporary Cape", "h:2", timed=True, item_type="temp_role")]
        embed = wallet.build_inventory_embed(wallet.TAB_ITEMS, entries, None, None)
        self.assertIn("expires <t:", embed.description)
        self.assertIn("<t:2051222400:R>", embed.description)
        self.assertIn("` Temporary Cape `", embed.description)

    def test_potion_rows_show_effect_and_setup_needed(self):
        good = entry("XP Elixir", "i:3", item_type="potion", quantity=2,
                     metadata={"effect": "xp_boost", "multiplier": 2,
                               "duration_hours": 6},
                     rarity="rare")
        bad = entry("Broken Potion", "i:4", item_type="potion",
                    metadata={"effect": "xp_boost"}, rarity="common")
        embed = wallet.build_inventory_embed(wallet.TAB_POTIONS, [good, bad], None, None)
        self.assertIn("2× XP for 6h", embed.description)
        self.assertIn("Setup needed", embed.description)
        self.assertIn("` 2 `", embed.description)

    def test_titles_embed_marks_equipped(self):
        entries = [entry("The Silent One", "i:5", item_type="title", rarity="mythical"),
                   entry("Early Adopter", "i:6", item_type="title", rarity="rare")]
        embed = wallet.build_inventory_embed(wallet.TAB_TITLES, entries, None,
                                             "The Silent One")
        lines = embed.description.splitlines()
        silent = next(l for l in lines if "The Silent One" in l)
        adopter = next(l for l in lines if "Early Adopter" in l)
        self.assertIn("✅ Equipped", silent)
        self.assertNotIn("✅ Equipped", adopter)

    def test_colors_rows_render_a_real_role_mention(self):
        """Colors are worn Discord roles: the row shows a `<@&id>` pill, not
        the role's display name, so the member sees the colour itself."""
        crimson = entry("Crimson Tide", "i:7", item_type="role",
                        category="colors", rarity="rare")
        crimson["metadata"] = {"category": "colors", "role_id": 4242}
        timed = entry("Ocean Blue", "h:8", item_type="temp_role", timed=True,
                      category="colors")
        timed["metadata"] = {"category": "colors", "role_id": 4343}
        embed = wallet.build_inventory_embed(wallet.TAB_COLORS, [crimson, timed],
                                             "h:8", None)
        lines = embed.description.splitlines()
        self.assertEqual("> - 🔵 • ` 1 ` • <@&4242> • *Rare*", lines[0])
        self.assertEqual(
            "> - ⚪ • ` 1 ` • <@&4343> • *✅ Equipped* · *Common* · expires <t:2051222400:R>",
            lines[1])
        # No invented guidance line: the list IS the panel.
        self.assertFalse(any(l.startswith("-#") for l in lines))

    def test_equipped_embed_shows_slots_and_nothing_when_empty(self):
        empty = wallet.build_inventory_embed(wallet.TAB_EQUIPPED, [], None, None,
                                             slots={"role": None, "title": None})
        self.assertIn("Nothing equipped", empty.description)

        entries = [entry("Sunglasses", "i:9", item_type="role", rarity="rare"),
                   entry("The Silent One", "i:10", item_type="title", rarity="epic")]
        slots = {
            "role": {"name": "Sunglasses", "emoji": "<:shades:1562903222086279178>",
                     "rarity": "rare", "expires": None, "timed": False,
                     "mention": "<@&12345>"},
            "title": {"name": "The Silent One", "emoji": "🏷️",
                      "rarity": "epic", "expires": None, "timed": False,
                      "mention": None},
        }
        embed = wallet.build_inventory_embed(wallet.TAB_EQUIPPED, entries,
                                             "Sunglasses", "The Silent One",
                                             slots=slots)
        self.assertEqual([f.name for f in embed.fields], ["🎭 Role", "🏷️ Title"])
        self.assertIn("`Sunglasses`", embed.fields[0].value)
        self.assertIn("<@&12345>", embed.fields[0].value)
        self.assertIn("<:shades:1562903222086279178>", embed.fields[0].value)
        self.assertIn("`The Silent One`", embed.fields[1].value)

    def test_select_options_carry_product_emoji_and_clean_description(self):
        entries = [entry("Moon Ball", "i:11", rarity="epic", quantity=3,
                         app_emoji="<:moon:1562903222086279178>"),
                   entry("Temporary Cape", "h:12", item_type="temp_role",
                         timed=True, rarity="rare")]
        select = wallet.InventoryItemSelect(1, 2, wallet.TAB_ITEMS, entries,
                                            None, None, page=0)
        moon, cape = select.options
        self.assertIsNotNone(moon.emoji)
        self.assertEqual(str(moon.emoji), "<:moon:1562903222086279178>")
        self.assertIn("×3", moon.description)
        self.assertIn("Permanent", moon.description)
        self.assertIn("until 2035-01-01", cape.description)
        self.assertIsNone(cape.emoji)  # never prepared -> no fake stand-in

    def test_potion_select_shows_quantity(self):
        entries = [entry("XP Elixir", "i:13", item_type="potion", quantity=5,
                         metadata={"effect": "xp_boost", "multiplier": 2,
                                   "duration_hours": 1})]
        select = wallet.InventoryItemSelect(1, 2, wallet.TAB_POTIONS, entries,
                                            None, None, page=0)
        self.assertIn("×5", select.options[0].description)

    def test_history_embed_renders_recorded_events(self):
        events = [
            {"event_type": "acquired", "item_name": "Moon Ball", "item_type": "custom",
             "quantity": 1, "source": "shop", "related_user_id": None,
             "created_at": "2026-10-01 12:00:00"},
            {"event_type": "used", "item_name": "XP Elixir", "item_type": "potion",
             "quantity": 2, "source": "potion", "related_user_id": None,
             "created_at": "2026-10-02 12:00:00"},
            {"event_type": "something_new", "item_name": "Thing", "item_type": None,
             "quantity": 1, "source": None, "related_user_id": None,
             "created_at": "2026-10-03 12:00:00"},
        ]
        embed = wallet.build_history_embed(events, "2026-09-01 00:00:00")
        self.assertIn("📥", embed.description)   # acquired
        self.assertIn("🧪", embed.description)   # used
        self.assertIn("•", embed.description)    # unknown -> neutral bullet
        self.assertIn("<t:", embed.description)  # relative Discord timestamps
        self.assertIn("`Moon Ball`", embed.description)
        self.assertIn("**×2**", embed.description)

    def test_history_embed_empty_state(self):
        embed = wallet.build_history_embed([], None)
        self.assertIsNone(embed.title)
        self.assertTrue(embed.description.startswith(
            "No recorded inventory activity yet."))
        # An absent start row is reported as absent, never invented.
        self.assertIn("recording start not recorded", embed.description)

    def test_history_coverage_note_states_the_recorded_start(self):
        embed = wallet.build_history_embed([], "2026-09-01 00:00:00")
        self.assertIn("-# Partial coverage · recording started "
                      "2026-09-01 00:00:00 UTC", embed.description)
        self.assertNotIn("unknown", embed.description)


class FooterContractTests(unittest.IsolatedAsyncioTestCase):
    """The agreed footer is the branded line — no page counter, no note."""

    async def test_paged_tab_keeps_the_agreed_footer_verbatim(self):
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(
            response=response, message=object(),
            user=SimpleNamespace(id=2, display_name="Shadow", name="Shadow"),
            client=SimpleNamespace(user=SimpleNamespace(name="Nilive")))
        items = [entry(f"Item {n:02}", f"i:{n+1}") for n in range(31)]
        with patch.object(wallet, "get_inventory_entries", AsyncMock(return_value=items)), \
             patch.object(wallet, "cleanup_unowned_title", AsyncMock()), \
             patch.object(wallet, "_decorate", AsyncMock(side_effect=lambda gid, e: e)), \
             patch.object(wallet, "_attach_prepared_app_emojis", AsyncMock()), \
             patch.object(wallet, "get_currency_config", AsyncMock(
                 return_value={"coins": {"name": "Coins", "emoji": "🪙"},
                               "diamonds": {"name": "Diamonds", "emoji": "💎"}})), \
             patch.object(wallet, "get_balance", AsyncMock(return_value=0)), \
             patch.object(wallet, "get_equipped", AsyncMock(return_value=None)), \
             patch.object(wallet, "get_equipped_title", AsyncMock(return_value=None)):
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_ITEMS, page=1)
            embed = response.edit_message.await_args.kwargs["embed"]
            self.assertEqual(embed.footer.text, wallet.INVENTORY_FOOTER_TEXT)
            self.assertNotIn("Page", embed.footer.text)

    async def test_history_tab_keeps_the_agreed_footer_verbatim(self):
        """It knows the recording start, and still does not put it in the footer."""
        response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(
            response=response, message=object(),
            user=SimpleNamespace(id=2, display_name="Shadow", name="Shadow"),
            client=SimpleNamespace(user=SimpleNamespace(name="Nilive")))
        with patch("utils.inventory_activity.count_activity", AsyncMock(return_value=40)), \
             patch("utils.inventory_activity.get_activity_page", AsyncMock(return_value=[])), \
             patch("utils.inventory_activity.get_activity_start",
                   AsyncMock(return_value="2026-10-01 00:00:00")):
            await wallet.open_inventory_panel(interaction, 1, 2, wallet.TAB_HISTORY, page=1)
            embed = response.edit_message.await_args.kwargs["embed"]
            self.assertEqual(embed.footer.text, wallet.INVENTORY_FOOTER_TEXT)
            self.assertNotIn("Page", embed.footer.text)
            self.assertNotIn("Recording started", embed.footer.text)
            # ...but the coverage statement is still told, as a description
            # footnote, and it states the recorded start rather than a guess.
            self.assertIn("-# Partial coverage · recording started "
                          "2026-10-01 00:00:00 UTC", embed.description)


class ComponentBehaviourTests(unittest.IsolatedAsyncioTestCase):
    async def test_back_to_wallet_calls_hub(self):
        view = wallet.InventoryView(1, 2, wallet.TAB_ITEMS, [], None, None)
        back = next(x for x in view.children
                    if isinstance(x, wallet.InventoryBackButton))
        interaction = SimpleNamespace()
        with patch.object(wallet, "render_hub", AsyncMock()) as hub:
            await back.callback(interaction)
            hub.assert_awaited_once_with(interaction, 1, 2)
        view.stop()

    async def test_category_button_switches_tab_via_open_inventory_panel(self):
        view = wallet.InventoryView(1, 2, wallet.TAB_ITEMS, [], None, None)
        potions = next(x for x in view.children
                       if isinstance(x, wallet.InventoryTabButton)
                       and x.tab == wallet.TAB_POTIONS)
        interaction = SimpleNamespace()
        with patch.object(wallet, "open_inventory_panel", AsyncMock()) as opened:
            await potions.callback(interaction)
            opened.assert_awaited_once_with(
                interaction, 1, 2, wallet.TAB_POTIONS)
        view.stop()

    async def test_owner_guard_blocks_stranger(self):
        view = wallet.InventoryView(1, 2, wallet.TAB_ITEMS, [], None, None)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999), guild_id=1, response=response)
        allowed = await view.interaction_check(interaction)
        self.assertFalse(allowed)
        response.send_message.assert_awaited_once()
        view.stop()

    async def test_timed_holdings_have_no_drop_button(self):
        item = entry("Temporary Cape", "h:20", item_type="temp_role", timed=True)
        view = wallet.ItemPanelView(1, 2, wallet.TAB_ITEMS, item, False)
        self.assertFalse(any(isinstance(x, wallet.DropButton)
                             for x in view.children))
        view.stop()

    async def test_equipped_view_shows_actual_equip_only(self):
        """Entries reaching the Equipped view are exactly what is worn."""
        items = [entry("Sunglasses", "i:1", item_type="role"),
                 entry("Other Role", "i:2", item_type="role")]
        slots = wallet._equipped_slots(
            items,
            {"item_name": "Sunglasses", "role_id": 12345,
             "equipped_at": "2026-10-01 00:00:00", "entry_key": "i:1"},
            None)
        self.assertEqual(slots["role"]["name"], "Sunglasses")
        self.assertEqual(slots["role"]["mention"], "<@&12345>")
        self.assertIsNone(slots["title"])


class SlashCommandWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_command_delegates_to_shared_panel(self):
        """`/inventory` and Wallet → Inventory are the SAME renderer."""
        from cogs import shop as shop_cog
        shop = shop_cog.Shop.__new__(shop_cog.Shop)  # bypass __init__ (needs bot)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=7), user=SimpleNamespace(id=42))
        with patch.object(wallet, "open_inventory_panel",
                          AsyncMock()) as opened:
            await shop_cog.Shop.inventory.callback(shop, interaction)
            opened.assert_awaited_once_with(
                interaction, 7, 42, wallet.TAB_ITEMS, first=True)


if __name__ == "__main__":
    unittest.main()
