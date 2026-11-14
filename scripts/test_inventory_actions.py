#!/usr/bin/env python3
"""
Inventory actions — Equip / Unequip, Potion activation, and Drop removal.

Drives the REAL member surface (cogs/wallet.py panels and their button
callbacks) against the real engines (utils/equip_engine, title_engine,
potion_engine, inventory, timed_ownership) on a scratch SQLite database.
Only Discord itself is stubbed: a guild, a member whose role list records
add_roles/remove_roles, and an interaction that records the reply.

What this locks down
  1. Every item category gets exactly its own action and nothing else:
     role / color / timed color -> Equip or Unequip, title -> Equip or
     Unequip, potion -> Activate, plain items -> no action.
  2. Equip / Unequip change the Discord role, persist the slot, and survive a
     fresh render; they are refused for unowned, expired and non-equippable
     entries, and for another member or another guild.
  3. A potion is consumed only when its effect was applied, its duration
     starts at activation (not acquisition), one rendered panel can use at
     most one potion however fast it is clicked, and the running effect is
     shown with its end time and disappears once expired.
  4. The Drop feature is gone — button, confirmation, backend helper and the
     timed "drop" path — and the remaining actions still work without it.
  5. The dashboard member page shows the persisted state (timed holdings,
     equipped marks, running boost) and stays read-only.

Run:  python3 scripts/test_inventory_actions.py
"""
import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import GUILD, USER, ROOT, execute, rows, reset_database

import discord  # noqa: E402
from cogs import wallet  # noqa: E402
from utils import (  # noqa: E402
    equip_engine, inventory, potion_engine, timed_ownership as timed,
    title_engine, xp_calculator,
)
import dashboard.app as dashboard  # noqa: E402

OTHER_USER = 8202
OTHER_GUILD = 8102
FLAME, BLUE, TIMED_BLUE = 9701, 9702, 9703


def iso_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class Role:
    """Just enough of discord.Role for the role-position and membership checks."""

    def __init__(self, role_id, name, position=1):
        self.id, self.name, self.position = role_id, name, position
        self.mention = f"<@&{role_id}>"
        self.managed = False

    def is_default(self):
        return False

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id

    def __hash__(self):
        return self.id

    def __lt__(self, other):
        return self.position < other.position

    def __le__(self, other):
        return self.position <= other.position

    def __gt__(self, other):
        return self.position > other.position

    def __ge__(self, other):
        return self.position >= other.position


def discord_stub(user_id=USER, guild_id=GUILD):
    roles = [Role(FLAME, "Flame"), Role(BLUE, "Blue"), Role(TIMED_BLUE, "TimedBlue")]
    member = SimpleNamespace(id=user_id, roles=[], premium_since=None, bot=False)

    async def add_roles(role, reason=None):
        if role not in member.roles:
            member.roles.append(role)

    async def remove_roles(role, reason=None):
        if role in member.roles:
            member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    me = SimpleNamespace(
        top_role=Role(9799, "bot", position=100),
        guild_permissions=SimpleNamespace(manage_roles=True, administrator=True))
    guild = SimpleNamespace(
        id=guild_id, name="Scratch", me=me, roles=roles,
        premium_subscriber_role=None,
        get_member=lambda uid: member if uid == user_id else None,
        get_role=lambda rid: next((r for r in roles if r.id == int(rid)), None))
    member.guild = guild
    bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == guild_id else None,
        user=SimpleNamespace(name="Nilive", id=5), application_id=None)
    return member, guild, bot


def interaction_for(bot, guild, *, user_id=USER, guild_id=GUILD):
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
        client=bot, guild=guild, guild_id=guild_id, application_id=None,
        user=SimpleNamespace(id=user_id, name="member", display_name="member"),
        message=object(), original_response=AsyncMock())


def labels(view):
    """Action buttons of an item panel (its only children)."""
    return [child.label for child in view.children]


def field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


class InventoryActionCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        equip_engine._SLOT_LOCKS.clear()
        self.member, self.guild, self.bot = discord_stub()
        self._views = []

    async def asyncTearDown(self):
        for view in self._views:
            view.stop()

    # ── helpers ─────────────────────────────────────────────────────────
    async def key_of(self, name, user=USER, guild=GUILD):
        entries = await inventory.get_inventory_entries(guild, user)
        return next(e["entry_key"] for e in entries if e["item_name"] == name)

    async def open_panel(self, tab, key, *, user=USER, guild=GUILD):
        """Render the item panel. Returns (view, embed) or (None, refusal text)."""
        itx = interaction_for(self.bot, self.guild, user_id=user, guild_id=guild)
        await wallet.open_item_panel(itx, guild, user, tab, key)
        if itx.response.edit_message.await_args is None:
            return None, itx.response.send_message.await_args.args[0]
        kwargs = itx.response.edit_message.await_args.kwargs
        self._views.append(kwargs["view"])
        return kwargs["view"], kwargs["embed"]

    async def open_tab(self, tab, *, user=USER, guild=GUILD):
        itx = interaction_for(self.bot, self.guild, user_id=user, guild_id=guild)
        await wallet.open_inventory_panel(itx, guild, user, tab)
        kwargs = itx.response.edit_message.await_args.kwargs
        self._views.append(kwargs["view"])
        return kwargs["view"], kwargs["embed"]

    async def click(self, view, button_type, *, user=USER, guild=GUILD):
        """Press a button the way discord.py does: ownership guard, then callback.

        Returns ("denied"|"refused", text) when the member only got an
        ephemeral message, or ("rendered", view, embed) when the panel was
        re-rendered.
        """
        button = next(c for c in view.children if isinstance(c, button_type))
        itx = interaction_for(self.bot, self.guild, user_id=user, guild_id=guild)
        if not await view.interaction_check(itx):
            return ("denied", itx.response.send_message.await_args.args[0])
        await button.callback(itx)
        if itx.response.send_message.await_args is not None:
            return ("refused", itx.response.send_message.await_args.args[0])
        kwargs = itx.response.edit_message.await_args.kwargs
        self._views.append(kwargs["view"])
        return ("rendered", kwargs["view"], kwargs["embed"])

    async def give_potion(self, name="Elixir", quantity=1, *, multiplier=2.0,
                          hours=3, user=USER, guild=GUILD):
        await inventory.give_item(
            guild, user, name, quantity, item_type="potion",
            metadata=potion_engine.build_metadata("xp_boost", multiplier, hours),
            source="shop")

    def quantity(self, name, user=USER, guild=GUILD):
        found = rows("SELECT quantity FROM inventory_items "
                     "WHERE guild_id=? AND user_id=? AND item_name=?",
                     (guild, user, name))
        return found[0][0] if found else None

    def boosts(self, user=USER, guild=GUILD):
        return rows("SELECT multiplier, expires_at, source FROM leveling_active_boosts "
                    "WHERE guild_id=? AND user_id=? ORDER BY id", (guild, user))

    def role_slot(self, user=USER, guild=GUILD):
        return rows("SELECT item_name, role_id, entry_key FROM equipped_roles "
                    "WHERE guild_id=? AND user_id=?", (guild, user))

    def events(self, user=USER, guild=GUILD):
        return [r[0] for r in rows(
            "SELECT event_type FROM inventory_activity "
            "WHERE guild_id=? AND user_id=? ORDER BY id", (guild, user))]


class ActionPerCategoryTests(InventoryActionCase):
    async def test_each_category_offers_only_its_own_action(self):
        await inventory.give_item(GUILD, USER, "Flame", 1, "role", {"role_id": FLAME}, "shop")
        await inventory.give_item(GUILD, USER, "Blue", 1, "role",
                                  {"role_id": BLUE, "category": "color"}, "shop")
        await timed.grant_timed_holding(
            GUILD, USER, "TimedBlue", expires_at=iso_in(2), item_type="temp_role",
            role_id=TIMED_BLUE, category="color", metadata={"role_id": TIMED_BLUE})
        await timed.grant_timed_holding(
            GUILD, USER, "TimedThing", expires_at=iso_in(2), item_type="shop_custom")
        await inventory.give_item(GUILD, USER, "Champion", 1, "title", None, "shop")
        await self.give_potion("Elixir", 2)
        await inventory.give_item(GUILD, USER, "Rock", 3, "shop_custom", None, "shop")
        await inventory.give_item(GUILD, USER, "Egg", 1, "event_drop", None, "event")

        expected = {
            (wallet.TAB_ITEMS, "Flame"): ["Equip", "Back"],
            (wallet.TAB_ITEMS, "Rock"): ["Back"],
            (wallet.TAB_ITEMS, "Egg"): ["Back"],
            (wallet.TAB_ITEMS, "TimedThing"): ["Back"],
            (wallet.TAB_COLORS, "Blue"): ["Equip", "Back"],
            (wallet.TAB_COLORS, "TimedBlue"): ["Equip", "Back"],
            (wallet.TAB_POTIONS, "Elixir"): ["Activate", "Back"],
            (wallet.TAB_TITLES, "Champion"): ["Equip", "Back"],
        }
        tabs = wallet.split_by_tab(await inventory.get_inventory_entries(GUILD, USER))
        seen = {(tab, e["item_name"]) for tab, entries in tabs.items() for e in entries}
        self.assertEqual(seen, set(expected), "every owned entry is reachable from exactly one tab")
        for (tab, name), want in expected.items():
            view, _ = await self.open_panel(tab, await self.key_of(name))
            self.assertEqual(labels(view), want, f"{tab}/{name}")

    async def test_plain_item_detail_does_not_claim_an_equip_state(self):
        await inventory.give_item(GUILD, USER, "Rock", 3, "shop_custom", None, "shop")
        _, embed = await self.open_panel(wallet.TAB_ITEMS, await self.key_of("Rock"))
        self.assertIsNone(field(embed, "Status"))
        self.assertEqual(field(embed, "Type"), "Collectible")


class EquipTests(InventoryActionCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await inventory.give_item(GUILD, USER, "Flame", 1, "role", {"role_id": FLAME}, "shop")
        await inventory.give_item(GUILD, USER, "Blue", 1, "role",
                                  {"role_id": BLUE, "category": "color"}, "shop")
        self.flame = await self.key_of("Flame")
        self.blue = await self.key_of("Blue")

    def worn(self):
        return sorted(r.name for r in self.member.roles)

    async def test_equip_owned_role_applies_discord_role_and_persists(self):
        view, embed = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        self.assertEqual(field(embed, "Status"), "Not equipped")
        outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual(labels(outcome[1]), ["Unequip", "Back"])
        self.assertEqual(field(outcome[2], "Status"), "**Equipped**")
        self.assertEqual(self.worn(), ["Flame"])
        self.assertEqual(self.role_slot(), [("Flame", FLAME, self.flame)])
        self.assertEqual(self.events()[-1], "equipped")

    async def test_equipping_a_second_role_swaps_the_single_slot(self):
        view, _ = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        await self.click(view, wallet.EquipToggleButton)
        view, _ = await self.open_panel(wallet.TAB_COLORS, self.blue)
        outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual(self.worn(), ["Blue"], "the previous role is taken off")
        self.assertEqual(self.role_slot(), [("Blue", BLUE, self.blue)])
        # Ownership of the swapped-out item is untouched.
        self.assertEqual(self.quantity("Flame"), 1)

    async def test_unequip_removes_discord_role_and_clears_slot(self):
        view, _ = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        _, view, _ = await self.click(view, wallet.EquipToggleButton)
        outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual(labels(outcome[1]), ["Equip", "Back"])
        self.assertEqual(self.worn(), [])
        self.assertEqual(self.role_slot(), [])
        self.assertEqual(self.quantity("Flame"), 1, "unequip never removes ownership")
        self.assertEqual(self.events()[-2:], ["equipped", "unequipped"])

    async def test_timed_color_equips_and_unequips_by_holding_identity(self):
        holding = await timed.grant_timed_holding(
            GUILD, USER, "TimedBlue", expires_at=iso_in(2), item_type="temp_role",
            role_id=TIMED_BLUE, category="color", metadata={"role_id": TIMED_BLUE})
        key = f"h:{holding}"
        view, embed = await self.open_panel(wallet.TAB_COLORS, key)
        self.assertIsNotNone(field(embed, "Expires"))
        _, view, _ = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(self.role_slot(), [("TimedBlue", TIMED_BLUE, key)])
        self.assertEqual(self.worn(), ["TimedBlue"])
        await self.click(view, wallet.EquipToggleButton)
        self.assertEqual((self.role_slot(), self.worn()), ([], []))
        self.assertIsNotNone(await timed.get_timed_holding(key))

    async def test_stale_panel_cannot_change_the_slot_twice(self):
        view, _ = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        await self.click(view, wallet.EquipToggleButton)
        again = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(again[0], "refused")
        self.assertIn("changed", again[1])
        self.assertEqual(self.role_slot(), [("Flame", FLAME, self.flame)])
        self.assertEqual(self.events().count("equipped"), 1)

    async def test_failed_discord_role_change_is_not_reported_as_equipped(self):
        self.member.add_roles = AsyncMock(side_effect=RuntimeError("Missing Permissions"))
        view, _ = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "refused")
        self.assertIn("Could not change equip state", outcome[1])
        self.assertEqual(self.role_slot(), [])
        _, embed = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        self.assertEqual(field(embed, "Status"), "Not equipped")

    async def test_non_equippable_and_unowned_items_are_rejected(self):
        await inventory.give_item(GUILD, USER, "Rock", 1, "shop_custom", None, "shop")
        await self.give_potion("Elixir")
        for name in ("Rock", "Elixir"):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, name)
            self.assertFalse(result["success"], name)
            self.assertIn("isn't an equippable role item", result["error"])
            result = await title_engine.equip_title(GUILD, USER, name)
            self.assertFalse(result["success"], name)
        unowned = await equip_engine.equip_role(self.bot, GUILD, USER, "Ghost")
        self.assertFalse(unowned["success"])
        self.assertIn("don't own", unowned["error"])
        stranger = await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame", entry_key="i:999999")
        self.assertFalse(stranger["success"])
        self.assertEqual((self.role_slot(), self.worn()), ([], []))
        self.member.add_roles.assert_not_awaited()

    async def test_expired_timed_role_cannot_be_equipped_and_is_cleaned_up(self):
        holding = await timed.grant_timed_holding(
            GUILD, USER, "TimedBlue", expires_at=iso_in(2), item_type="temp_role",
            role_id=TIMED_BLUE, category="color", metadata={"role_id": TIMED_BLUE})
        key = f"h:{holding}"
        view, _ = await self.open_panel(wallet.TAB_COLORS, key)
        await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(self.worn(), ["TimedBlue"])

        execute("UPDATE timed_holdings SET expires_at=? WHERE id=?", (iso_in(-1), holding))
        # Reads treat it as gone at the deadline, before any sweep runs.
        refused, text = await self.open_panel(wallet.TAB_COLORS, key)
        self.assertIsNone(refused)
        self.assertIn("no longer available", text)
        late = await equip_engine.equip_role(self.bot, GUILD, USER, "TimedBlue", entry_key=key)
        self.assertFalse(late["success"])
        # The sweep then retires the role, the slot and the holding.
        stats = await timed.cleanup_expired_holdings(self.bot)
        self.assertEqual(stats.get("removed"), 1, stats)
        self.assertEqual((self.role_slot(), self.worn()), ([], []))
        self.assertIsNone(await timed.get_timed_holding(key))
        self.assertEqual(self.events()[-1], "expired")

    async def test_title_slot_is_independent_from_the_role_slot(self):
        await inventory.give_item(GUILD, USER, "Champion", 1, "title", None, "shop")
        champion = await self.key_of("Champion")
        view, _ = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        await self.click(view, wallet.EquipToggleButton)
        view, _ = await self.open_panel(wallet.TAB_TITLES, champion)
        _, view, embed = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(field(embed, "Status"), "**Equipped**")
        self.assertEqual((await title_engine.get_equipped_title(GUILD, USER))["item_name"],
                         "Champion")
        self.assertEqual(self.role_slot(), [("Flame", FLAME, self.flame)],
                         "a title must not disturb the role slot")
        self.assertEqual(self.worn(), ["Flame"], "a title never touches Discord roles")
        await self.click(view, wallet.EquipToggleButton)
        self.assertIsNone(await title_engine.get_equipped_title(GUILD, USER))
        self.assertEqual(self.quantity("Champion"), 1)
        ghost = await title_engine.equip_title(GUILD, USER, "Ghost")
        self.assertFalse(ghost["success"])

    async def test_equip_state_is_read_back_from_the_database_after_a_restart(self):
        await inventory.give_item(GUILD, USER, "Champion", 1, "title", None, "shop")
        view, _ = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        await self.click(view, wallet.EquipToggleButton)
        view, _ = await self.open_panel(wallet.TAB_TITLES, await self.key_of("Champion"))
        await self.click(view, wallet.EquipToggleButton)

        # "Restart": every in-memory view and lock is gone; only SQLite remains.
        for stale in self._views:
            stale.stop()
        self._views.clear()
        equip_engine._SLOT_LOCKS.clear()

        _, embed = await self.open_tab(wallet.TAB_EQUIPPED)
        self.assertIn("Flame", embed.description)
        self.assertIn("Champion", embed.description)
        view, embed = await self.open_panel(wallet.TAB_ITEMS, self.flame)
        self.assertEqual(labels(view), ["Unequip", "Back"])
        _, embed = await self.open_tab(wallet.TAB_ITEMS)
        self.assertIn("Equipped", embed.description)


class PotionTests(InventoryActionCase):
    async def test_activation_applies_effect_and_consumes_exactly_one(self):
        await self.give_potion("Elixir", 2, multiplier=2.0, hours=3)
        self.assertEqual(self.boosts(), [], "owning a potion applies nothing")
        view, embed = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Elixir"))
        self.assertIsNone(field(embed, "Active XP boost"))
        outcome = await self.click(view, wallet.ActivateButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual(self.quantity("Elixir"), 1)
        self.assertEqual([(b[0], b[2]) for b in self.boosts()], [(2.0, "potion")])
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 2.0)
        self.assertEqual(self.events()[-1], "used")
        # The re-rendered panel shows what is now running and when it ends.
        shown = field(outcome[2], "Active XP boost")
        self.assertIn("2× XP", shown)
        expiry = timed.parse_time(self.boosts()[0][1])
        self.assertIn(f"<t:{int(expiry.timestamp())}:R>", shown)

    async def test_duration_starts_at_activation_not_at_acquisition(self):
        await self.give_potion("Elixir", 1, hours=3)
        # Acquired ten days ago: longer than the potion's whole 3h duration.
        execute("UPDATE inventory_items SET created_at=datetime('now','-10 days'), "
                "updated_at=datetime('now','-10 days') WHERE item_name='Elixir'")
        self.assertEqual(self.boosts(), [])
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 1.0)

        before = datetime.now(timezone.utc)
        result = await potion_engine.use_potion(GUILD, USER, "Elixir")
        after = datetime.now(timezone.utc)
        self.assertTrue(result["success"], result)
        expiry = timed.parse_time(self.boosts()[0][1])
        self.assertGreaterEqual(expiry, before + timedelta(hours=3))
        self.assertLessEqual(expiry, after + timedelta(hours=3))
        self.assertEqual(result["expires_at"], self.boosts()[0][1])

    async def test_last_potion_returns_to_the_tab_and_keeps_showing_the_effect(self):
        await self.give_potion("Elixir", 1)
        view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Elixir"))
        outcome = await self.click(view, wallet.ActivateButton)
        self.assertEqual(outcome[0], "rendered")
        description = outcome[2].description
        self.assertIn("No potions yet", description)
        self.assertIn("Active XP boost", description)
        self.assertIn("2× XP", description)
        self.assertEqual(self.quantity("Elixir"), 0)

    async def test_rapid_double_click_on_one_panel_uses_one_potion(self):
        await self.give_potion("Elixir", 3)
        view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Elixir"))
        outcomes = await asyncio.gather(
            self.click(view, wallet.ActivateButton),
            self.click(view, wallet.ActivateButton),
            self.click(view, wallet.ActivateButton))
        self.assertEqual(sorted(o[0] for o in outcomes), ["refused", "refused", "rendered"])
        self.assertTrue(all("stack changed" in o[1] for o in outcomes if o[0] == "refused"))
        self.assertEqual(self.quantity("Elixir"), 2)
        self.assertEqual(len(self.boosts()), 1)
        self.assertEqual(self.events().count("used"), 1)

    async def test_two_open_panels_cannot_both_spend_the_same_render(self):
        await self.give_potion("Elixir", 2)
        key = await self.key_of("Elixir")
        first, _ = await self.open_panel(wallet.TAB_POTIONS, key)
        second, _ = await self.open_panel(wallet.TAB_POTIONS, key)
        self.assertEqual((await self.click(first, wallet.ActivateButton))[0], "rendered")
        self.assertEqual((await self.click(second, wallet.ActivateButton))[0], "refused")
        self.assertEqual(self.quantity("Elixir"), 1)
        # A deliberate second use, from a freshly rendered panel, still works.
        fresh, _ = await self.open_panel(wallet.TAB_POTIONS, key)
        self.assertEqual((await self.click(fresh, wallet.ActivateButton))[0], "rendered")
        self.assertEqual(self.quantity("Elixir"), 0)
        self.assertEqual(len(self.boosts()), 2)

    async def test_concurrent_engine_calls_never_overspend_the_stack(self):
        await self.give_potion("Elixir", 2)
        results = await asyncio.gather(*[
            potion_engine.use_potion(GUILD, USER, "Elixir") for _ in range(5)])
        self.assertEqual(sum(r["success"] for r in results), 2)
        self.assertEqual(self.quantity("Elixir"), 0)
        self.assertEqual(len(self.boosts()), 2)

    async def test_failed_effect_consumes_nothing_and_is_not_shown_as_success(self):
        await self.give_potion("Elixir", 2)
        view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Elixir"))
        with patch.object(xp_calculator, "grant_xp_boost",
                          AsyncMock(side_effect=RuntimeError("disk I/O error"))):
            outcome = await self.click(view, wallet.ActivateButton)
        self.assertEqual(outcome[0], "refused")
        self.assertIn("Could not activate", outcome[1])
        self.assertIn("Nothing was used", outcome[1])
        self.assertEqual(self.quantity("Elixir"), 2)
        self.assertEqual(self.boosts(), [])
        self.assertNotIn("used", self.events())
        # The same panel still works once the fault is gone: nothing was spent.
        self.assertEqual((await self.click(view, wallet.ActivateButton))[0], "rendered")
        self.assertEqual(self.quantity("Elixir"), 1)

    async def test_unowned_empty_and_non_potion_activation_is_rejected(self):
        ghost = await potion_engine.use_potion(GUILD, USER, "Ghost")
        self.assertFalse(ghost["success"])
        self.assertIn("don't own", ghost["error"])

        await self.give_potion("Elixir", 1)
        self.assertTrue((await potion_engine.use_potion(GUILD, USER, "Elixir"))["success"])
        empty = await potion_engine.use_potion(GUILD, USER, "Elixir")
        self.assertFalse(empty["success"], "a used-up stack is not usable")

        await inventory.give_item(GUILD, USER, "Rock", 1, "shop_custom", None, "shop")
        rock = await potion_engine.use_potion(GUILD, USER, "Rock")
        self.assertFalse(rock["success"])
        self.assertIn("isn't a potion", rock["error"])
        self.assertEqual(self.quantity("Rock"), 1)
        self.assertEqual(len(self.boosts()), 1)

    async def test_misconfigured_potion_is_disabled_and_never_consumed(self):
        await inventory.give_item(GUILD, USER, "Dud", 1, "potion",
                                  {"effect": "invisibility"}, "shop")
        await inventory.give_item(GUILD, USER, "Flat", 1, "potion",
                                  {"effect": "xp_boost", "multiplier": 1.0,
                                   "duration_hours": 3}, "shop")
        for name in ("Dud", "Flat"):
            view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of(name))
            button = next(c for c in view.children if isinstance(c, wallet.ActivateButton))
            self.assertTrue(button.disabled, name)
            result = await potion_engine.use_potion(GUILD, USER, name)
            self.assertFalse(result["success"], name)
            self.assertEqual(self.quantity(name), 1)
        self.assertEqual(self.boosts(), [])

    async def test_stacking_rule_is_unchanged_highest_running_boost_applies(self):
        await self.give_potion("Small", 1, multiplier=1.5, hours=6)
        await self.give_potion("Big", 1, multiplier=3.0, hours=1)
        await potion_engine.use_potion(GUILD, USER, "Small")
        await potion_engine.use_potion(GUILD, USER, "Big")
        self.assertEqual(len(self.boosts()), 2, "each activation is its own row")
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 3.0)
        _, embed = await self.open_tab(wallet.TAB_POTIONS)
        text = embed.description
        self.assertLess(text.index("3× XP"), text.index("1.5× XP"))
        self.assertIn("in effect", text.split("3× XP")[1].splitlines()[0])

    async def test_expired_effect_stops_applying_and_is_swept(self):
        await self.give_potion("Elixir", 2, hours=1)
        key = await self.key_of("Elixir")
        await potion_engine.use_potion(GUILD, USER, "Elixir")
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 2.0)

        execute("UPDATE leveling_active_boosts SET expires_at=?", (iso_in(-0.01),))
        # At the deadline, before any sweep: no multiplier, nothing displayed.
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 1.0)
        self.assertEqual(await potion_engine.get_active_effects(GUILD, USER), [])
        _, embed = await self.open_panel(wallet.TAB_POTIONS, key)
        self.assertIsNone(field(embed, "Active XP boost"))
        _, embed = await self.open_tab(wallet.TAB_POTIONS)
        self.assertNotIn("Active XP boost", embed.description)

        # The Shop cog's 10-minute sweep (its real body) removes the dead row.
        from cogs.shop import Shop
        await Shop.temp_role_cleanup.coro(SimpleNamespace(bot=self.bot))
        self.assertEqual(self.boosts(), [])
        # The unused potion is untouched by an effect expiring.
        self.assertEqual(self.quantity("Elixir"), 1)

    async def test_activation_survives_a_restart(self):
        await self.give_potion("Elixir", 2, hours=3)
        key = await self.key_of("Elixir")
        view, _ = await self.open_panel(wallet.TAB_POTIONS, key)
        await self.click(view, wallet.ActivateButton)
        for stale in self._views:
            stale.stop()
        self._views.clear()
        self.assertEqual(self.quantity("Elixir"), 1)
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 2.0)
        _, embed = await self.open_panel(wallet.TAB_POTIONS, key)
        self.assertIn("2× XP", field(embed, "Active XP boost"))


class IsolationTests(InventoryActionCase):
    async def test_other_member_and_other_guild_cannot_press_the_buttons(self):
        await inventory.give_item(GUILD, USER, "Flame", 1, "role", {"role_id": FLAME}, "shop")
        await self.give_potion("Elixir", 1)
        equip_view, _ = await self.open_panel(wallet.TAB_ITEMS, await self.key_of("Flame"))
        potion_view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Elixir"))
        for view, button in ((equip_view, wallet.EquipToggleButton),
                             (potion_view, wallet.ActivateButton)):
            self.assertEqual((await self.click(view, button, user=OTHER_USER))[0], "denied")
            self.assertEqual((await self.click(view, button, guild=OTHER_GUILD))[0], "denied")
        self.assertEqual(self.role_slot(), [])
        self.assertEqual(self.quantity("Elixir"), 1)
        self.assertEqual(self.boosts(), [])
        self.member.add_roles.assert_not_awaited()

    async def test_engines_scope_every_action_to_guild_and_user(self):
        await inventory.give_item(GUILD, USER, "Flame", 1, "role", {"role_id": FLAME}, "shop")
        await inventory.give_item(GUILD, USER, "Champion", 1, "title", None, "shop")
        await self.give_potion("Elixir", 1)
        flame = await self.key_of("Flame")
        champion = await self.key_of("Champion")
        elixir = await self.key_of("Elixir")
        for guild, user in ((GUILD, OTHER_USER), (OTHER_GUILD, USER)):
            for kwargs in ({}, {"entry_key": elixir}):
                used = await potion_engine.use_potion(guild, user, "Elixir", **kwargs)
                self.assertFalse(used["success"], (guild, user, kwargs))
            for kwargs in ({}, {"entry_key": champion}):
                worn = await title_engine.equip_title(guild, user, "Champion", **kwargs)
                self.assertFalse(worn["success"], (guild, user, kwargs))
            for kwargs in ({}, {"entry_key": flame}):
                role = await equip_engine.equip_role(self.bot, guild, user, "Flame", **kwargs)
                self.assertFalse(role["success"], (guild, user, kwargs))
            self.assertEqual(await inventory.get_inventory_entries(guild, user), [])
            refused, text = await self.open_panel(wallet.TAB_ITEMS, flame, user=user, guild=guild)
            self.assertIsNone(refused)
            self.assertIn("no longer available", text)
        self.assertEqual(self.quantity("Elixir"), 1)
        self.assertEqual(rows("SELECT COUNT(*) FROM leveling_active_boosts"), [(0,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM equipped_roles"), [(0,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM equipped_titles"), [(0,)])

    async def test_a_boost_belongs_to_one_member_in_one_guild(self):
        await self.give_potion("Elixir", 1)
        await potion_engine.use_potion(GUILD, USER, "Elixir")
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, USER), 2.0)
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(GUILD, OTHER_USER), 1.0)
        self.assertEqual(await xp_calculator.get_active_boost_multiplier(OTHER_GUILD, USER), 1.0)
        self.assertEqual(await potion_engine.get_active_effects(OTHER_GUILD, USER), [])


class DropRemovalTests(InventoryActionCase):
    async def test_no_drop_surface_or_backend_remains(self):
        for name in ("DropButton", "DropConfirmView", "open_drop_confirm",
                     "build_drop_confirm_embed", "drop_item"):
            self.assertFalse(hasattr(wallet, name), name)
        self.assertFalse(hasattr(inventory, "drop_item"))
        import inspect
        self.assertNotIn("drop_requested", inspect.signature(timed.expire_one).parameters)
        for relative in ("cogs/wallet.py", "utils/inventory.py", "utils/timed_ownership.py"):
            source = (Path(ROOT) / relative).read_text(encoding="utf-8")
            for needle in ('label="Drop"', "drop_item", "drop_requested", "DropConfirm"):
                self.assertNotIn(needle, source, f"{needle} in {relative}")

    def test_no_drop_implementation_anywhere_in_the_repository(self):
        # Every source file, including the unimported root-level copies. Test
        # files are excluded: they name these symbols to assert their absence.
        needles = ("drop_item", "DropButton", "DropConfirm", "open_drop_confirm",
                   "build_drop_confirm", "drop_requested", 'label="Drop"')
        skipped = {".git", "node_modules", "__pycache__", "docs"}
        hits = []
        for path in Path(ROOT).rglob("*"):
            if (path.suffix not in (".py", ".html", ".js", ".json")
                    or skipped & set(path.relative_to(ROOT).parts)
                    or path.name.startswith("test_")):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            hits += [f"{path.relative_to(ROOT)}: {n}" for n in needles if n in text]
        self.assertEqual(hits, [])

    async def test_no_panel_offers_drop_and_ownership_is_untouched(self):
        await inventory.give_item(GUILD, USER, "Flame", 1, "role", {"role_id": FLAME}, "shop")
        await inventory.give_item(GUILD, USER, "Champion", 1, "title", None, "shop")
        await inventory.give_item(GUILD, USER, "Rock", 3, "shop_custom", None, "shop")
        await self.give_potion("Elixir", 2)
        holding = await timed.grant_timed_holding(
            GUILD, USER, "TimedThing", expires_at=iso_in(2), item_type="shop_custom")
        before = rows("SELECT item_name, item_type, quantity FROM inventory_items ORDER BY id")
        tabs = wallet.split_by_tab(await inventory.get_inventory_entries(GUILD, USER))
        for tab, entries in tabs.items():
            list_view, _ = await self.open_tab(tab)
            self.assertNotIn("Drop", [getattr(c, "label", None) for c in list_view.children])
            for entry in entries:
                view, _ = await self.open_panel(tab, entry["entry_key"])
                self.assertNotIn("Drop", labels(view))
                self.assertEqual(labels(view)[-1], "Back")
        self.assertEqual(
            rows("SELECT item_name, item_type, quantity FROM inventory_items ORDER BY id"), before)
        self.assertIsNotNone(await timed.get_timed_holding(f"h:{holding}"))

    async def test_live_timed_holding_can_only_leave_by_expiry(self):
        holding = await timed.grant_timed_holding(
            GUILD, USER, "TimedThing", expires_at=iso_in(2), item_type="shop_custom")
        record = await timed.get_timed_holding(f"h:{holding}")
        self.assertEqual((await timed.expire_one(self.bot, record))["status"], "skipped")
        with self.assertRaises(TypeError):
            await timed.expire_one(self.bot, record, drop_requested=True)
        self.assertIsNotNone(await timed.get_timed_holding(f"h:{holding}"))

    async def test_history_still_renders_previously_recorded_drops(self):
        embed = wallet.build_history_embed(
            [{"event_type": "dropped", "item_name": "Old Hat", "quantity": 2,
              "source": "inventory", "created_at": "2026-01-01 00:00:00"}], None)
        self.assertIn("Dropped", embed.description)
        self.assertIn("Old Hat", embed.description)


class DashboardMemberPageTests(unittest.TestCase):
    """The staff page mirrors persisted state and exposes no mutation."""

    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Staff", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="inventory-csrf")

    def seed(self):
        async def run():
            await inventory.give_item(GUILD, OTHER_USER, "Flame", 1, "role",
                                      {"role_id": FLAME}, "shop")
            await inventory.give_item(GUILD, OTHER_USER, "Spare", 1, "role",
                                      {"role_id": BLUE}, "shop")
            await inventory.give_item(GUILD, OTHER_USER, "Champion", 1, "title", None, "shop")
            await inventory.give_item(
                GUILD, OTHER_USER, "Elixir", 2, "potion",
                potion_engine.build_metadata("xp_boost", 2.0, 3), "shop")
            await timed.grant_timed_holding(
                GUILD, OTHER_USER, "TimedThing", expires_at=iso_in(5),
                item_type="shop_custom")
            flame = next(e for e in await inventory.get_inventory_entries(GUILD, OTHER_USER)
                         if e["item_name"] == "Flame")
            await equip_engine.record_equipped(GUILD, OTHER_USER, "Flame", role_id=FLAME,
                                               entry_key=flame["entry_key"])
            await title_engine.equip_title(GUILD, OTHER_USER, "Champion")
            await potion_engine.use_potion(GUILD, OTHER_USER, "Elixir")
            # Another guild's rows must never leak into this guild's page.
            await inventory.give_item(OTHER_GUILD, OTHER_USER, "Foreign", 1, "title", None, "shop")
        asyncio.run(run())

    def row_for(self, html, name):
        start = html.index(f"<strong>{name}</strong>")
        return html[start:html.index("</tr>", start)]

    def test_member_page_shows_timed_holdings_equipped_marks_and_running_boost(self):
        self.seed()
        response = self.client.get(f"/inventory/{OTHER_USER}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Equipped", self.row_for(html, "Flame"))
        self.assertIn("Equipped", self.row_for(html, "Champion"))
        self.assertNotIn("Equipped", self.row_for(html, "Spare"))
        self.assertNotIn("Equipped", self.row_for(html, "Elixir"))
        self.assertIn("×1", self.row_for(html, "Elixir"), "one of two potions was used")
        self.assertIn("Expires", self.row_for(html, "TimedThing"))
        self.assertIn("Active XP boost", html)
        self.assertIn("2×", html)
        self.assertNotIn("Foreign", html)

    def test_page_reflects_an_unequip_and_an_expired_boost(self):
        self.seed()
        execute("DELETE FROM equipped_roles")
        execute("UPDATE leveling_active_boosts SET expires_at=?", (iso_in(-1),))
        html = self.client.get(f"/inventory/{OTHER_USER}").get_data(as_text=True)
        self.assertNotIn("Equipped", self.row_for(html, "Flame"))
        self.assertNotIn("Active XP boost", html)

    def test_inventory_stays_read_only_and_has_no_drop_endpoint(self):
        inventory_rules = [rule for rule in dashboard.app.url_map.iter_rules()
                           if "inventory" in rule.rule]
        self.assertTrue(inventory_rules)
        for rule in inventory_rules:
            self.assertLessEqual(rule.methods, {"GET", "HEAD", "OPTIONS"}, rule.rule)
        self.assertFalse([rule.rule for rule in dashboard.app.url_map.iter_rules()
                          if "drop" in rule.rule.lower()])
        template = (Path(ROOT) / "dashboard/templates/systems/inventory.html").read_text(
            encoding="utf-8")
        self.assertNotIn("drop", template.lower())
        self.assertNotIn("<form", template.lower())

    def test_page_requires_a_signed_in_dashboard_user(self):
        anonymous = dashboard.app.test_client()
        response = anonymous.get(f"/inventory/{OTHER_USER}")
        self.assertIn(response.status_code, (301, 302, 401, 403))


if __name__ == "__main__":
    unittest.main(verbosity=2)
