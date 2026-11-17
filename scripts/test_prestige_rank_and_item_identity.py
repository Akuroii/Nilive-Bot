#!/usr/bin/env python3
"""
Prestige is a rank, and a grant never changes what an owned item is.

Two rules, both driven through the real code on a scratch SQLite database
(only Discord is stubbed):

  1. PRESTIGE IS A PROGRESSION RANK, NOT AN EQUIPPABLE ITEM.
     One permanent tier per member on levels.prestige (I-V, bought in
     order), plus a Booster-only VI activation that sits on top of it while
     the boost lasts. Exactly one tier is in effect at a time and it is
     DERIVED from that state: its multipliers are that tier's alone (never
     combined with a lower tier's), the member wears that tier's role and no
     other Prestige role, and nothing about Prestige lives in the Inventory
     or in an equip slot.

  2. ITEM TYPE COLLISIONS.
     inventory_items is one row per (guild, user, item_name). A grant whose
     name matches an owned stack of a different kind is refused and writes
     nothing; it never converts the stack. Plain collectibles of the same
     name still stack, and the stored type stays.

Run:  python3 scripts/test_prestige_rank_and_item_identity.py
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, seed_listing,
)

from cogs import wallet  # noqa: E402
from utils import (  # noqa: E402
    equip_engine, inventory, potion_engine, prestige, reward_engine,
    title_engine,
)
from test_inventory_actions import (  # noqa: E402
    InventoryActionCase as _ActionCase, Role, labels,
)

OTHER_USER = 8202
OTHER_GUILD = 8102
TIER_ROLES = {1: 9801, 2: 9802, 3: 9803, 4: 9804, 5: 9805, 6: 9806}


def prestige_world(*, boosting=False, user=USER, guild_id=GUILD):
    """A guild with one role per Prestige tier and a member who may boost."""
    roles = [Role(rid, f"Prestige {tier}") for tier, rid in TIER_ROLES.items()]
    member = SimpleNamespace(
        id=user, roles=[], bot=False,
        premium_since=(datetime(2026, 1, 1, tzinfo=timezone.utc)
                       if boosting else None))

    async def add_roles(role, reason=None):
        if role not in member.roles:
            member.roles.append(role)

    async def remove_roles(role, reason=None):
        if role in member.roles:
            member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    me = SimpleNamespace(
        id=5, top_role=Role(9899, "bot", position=100),
        guild_permissions=SimpleNamespace(manage_roles=True, administrator=True))
    guild = SimpleNamespace(
        id=guild_id, name="Scratch", me=me, roles=roles, owner_id=99,
        premium_subscriber_role=None,
        get_member=lambda uid: member if uid == user else None,
        get_role=lambda rid: next((r for r in roles if r.id == int(rid)), None))
    member.guild = guild
    bot = SimpleNamespace(
        get_guild=lambda gid: guild if gid == guild_id else None,
        user=SimpleNamespace(name="Nilive", id=5), application_id=None)
    return member, guild, bot


def configure_tier_roles(guild_id=GUILD):
    for tier, role_id in TIER_ROLES.items():
        execute("INSERT INTO prestige_roles (guild_id, tier, role_id) "
                "VALUES (?,?,?)", (guild_id, tier, role_id))


def worn_tier_roles(member):
    return sorted(r.id for r in member.roles if r.id in TIER_ROLES.values())


class PrestigeIsARankTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        equip_engine._SLOT_LOCKS.clear()

    def stored_tier(self, user=USER, guild=GUILD):
        found = rows("SELECT prestige FROM levels WHERE guild_id=? AND user_id=?",
                     (guild, user))
        return found[0][0] if found else None

    # ── architecture ────────────────────────────────────────────────────
    async def test_prestige_is_stored_as_one_rank_per_member_not_as_items(self):
        seed_member(balance=9000, tier=0)
        for tier in (1, 2, 3):
            execute("UPDATE economy SET balance=9000 WHERE guild_id=? AND user_id=?",
                    (GUILD, USER))
            await prestige.purchase_prestige(GUILD, USER, tier, 2500)

        # One row, one value: buying II and III replaced the rank, it did not
        # add a second and third Prestige next to the first.
        self.assertEqual(rows("SELECT prestige FROM levels WHERE guild_id=? "
                              "AND user_id=?", (GUILD, USER)), [(3,)])
        self.assertEqual(await prestige.get_permanent_prestige(GUILD, USER), 3)
        # Nothing was put in the Inventory and no equip slot was used.
        self.assertEqual(rows("SELECT * FROM inventory_items"), [])
        self.assertEqual(rows("SELECT * FROM timed_holdings"), [])
        self.assertEqual(rows("SELECT * FROM equipped_roles"), [])
        self.assertEqual(rows("SELECT * FROM equipped_titles"), [])
        self.assertEqual(await inventory.get_inventory_entries(GUILD, USER), [])

    def test_there_is_no_prestige_equip_surface(self):
        # No slot table and no equip API: the tier in effect is always derived.
        tables = {r[0] for r in rows(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({t for t in tables if "prestige" in t and "equip" in t})
        self.assertFalse({t for t in tables if t.startswith("equipped_")}
                         - {"equipped_roles", "equipped_titles"})
        for name in dir(prestige):
            self.assertNotIn("equip", name.lower())
        self.assertNotIn("prestige", wallet.EQUIPPABLE_TYPES)
        self.assertNotIn("prestige", inventory.FUNCTIONAL_ITEM_TYPES)

    # ── I-V balance and order rules (unchanged) ─────────────────────────
    async def test_tiers_one_to_five_stay_sequential_and_balance_gated(self):
        seed_member(balance=2499, tier=1)
        with self.assertRaisesRegex(prestige.PrestigeError, "at least 2,500"):
            await prestige.purchase_prestige(GUILD, USER, 2, 2500)
        execute("UPDATE economy SET balance=9000 WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        with self.assertRaisesRegex(prestige.PrestigeError, "Prestige II next"):
            await prestige.purchase_prestige(GUILD, USER, 3, 2500)   # no skipping
        with self.assertRaisesRegex(prestige.PrestigeError, "already own"):
            await prestige.purchase_prestige(GUILD, USER, 1, 2500)   # no re-buy
        with self.assertRaisesRegex(prestige.PrestigeError, "Invalid"):
            await prestige.purchase_prestige(GUILD, USER, 6, 2500)   # VI not buyable
        self.assertEqual(self.stored_tier(), 1)
        self.assertEqual(rows("SELECT balance, diamonds FROM economy WHERE "
                              "guild_id=? AND user_id=?", (GUILD, USER)),
                         [(9000, 138)])

        done = await prestige.purchase_prestige(GUILD, USER, 2, 2500)
        self.assertEqual((done["old_tier"], done["new_tier"]), (1, 2))
        # Coins reset to 0; Diamonds, Level and XP untouched.
        self.assertEqual(rows("SELECT balance, diamonds FROM economy WHERE "
                              "guild_id=? AND user_id=?", (GUILD, USER)),
                         [(0, 138)])
        self.assertEqual(rows("SELECT xp, level, prestige FROM levels WHERE "
                              "guild_id=? AND user_id=?", (GUILD, USER)),
                         [(2700055, 84, 2)])

    async def test_five_is_the_highest_permanent_tier(self):
        seed_member(balance=9000, tier=5)
        with self.assertRaisesRegex(prestige.PrestigeError, "maximum permanent"):
            await prestige.purchase_prestige(GUILD, USER, 4, 2500)
        self.assertEqual(self.stored_tier(), 5)

    # ── VI Booster condition (unchanged) ────────────────────────────────
    async def test_six_needs_a_current_boost_and_is_never_the_permanent_tier(self):
        seed_member(balance=9000, tier=2)
        listing = seed_listing(tier=6, stock=None)

        _, _, plain_bot = prestige_world(boosting=False)
        with self.assertRaisesRegex(prestige.PrestigeError, "Booster-only"):
            await prestige.activate_booster_prestige(
                GUILD, USER, 0, item_id=listing, bot=plain_bot)
        self.assertEqual(rows("SELECT * FROM prestige_vi_activations"), [])

        _, _, boost_bot = prestige_world(boosting=True)
        await prestige.activate_booster_prestige(
            GUILD, USER, 0, item_id=listing, bot=boost_bot)
        self.assertEqual(
            await prestige.get_effective_prestige(GUILD, USER, bot=boost_bot), 6)
        # The permanent tier is kept underneath, untouched, and VI was free.
        self.assertEqual(self.stored_tier(), 2)
        self.assertEqual(await prestige.get_permanent_prestige(GUILD, USER), 2)
        self.assertEqual(rows("SELECT balance FROM economy WHERE guild_id=? "
                              "AND user_id=?", (GUILD, USER)), [(9000,)])
        with self.assertRaisesRegex(prestige.PrestigeError, "already activated"):
            await prestige.activate_booster_prestige(
                GUILD, USER, 0, item_id=listing, bot=boost_bot)
        self.assertEqual(len(rows("SELECT * FROM prestige_vi_activations")), 1)

    async def test_when_the_boost_ends_the_kept_permanent_tier_is_in_effect_again(self):
        seed_member(balance=9000, tier=3)
        listing = seed_listing(tier=6, stock=None)
        member, _, bot = prestige_world(boosting=True)
        await prestige.activate_booster_prestige(
            GUILD, USER, 0, item_id=listing, bot=bot)
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=bot), 6)

        member.premium_since = None                      # boost ended
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=bot), 3)
        self.assertEqual(self.stored_tier(), 3)
        self.assertEqual(rows("SELECT * FROM prestige_vi_activations"), [])
        # A new boost alone does not bring VI back: it needs a new activation.
        member.premium_since = datetime.now(timezone.utc)
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=bot), 3)

    # ── exactly one tier in effect: no stacking ─────────────────────────
    async def test_multipliers_come_from_the_one_effective_tier_only(self):
        seed_member(balance=9000, tier=5)
        listing = seed_listing(tier=6, stock=None)
        await prestige.set_prestige_multipliers(GUILD, tiers={
            3: {"coins": 1.5, "diamonds": 1.3},
            5: {"coins": 2.0, "diamonds": 1.5},
            6: {"coins": 3.0, "diamonds": 4.0},
        })
        member, _, bot = prestige_world(boosting=True)

        # Tier V in effect: V's numbers, not III's and V's together.
        self.assertEqual(
            await prestige.get_effective_prestige_multipliers(GUILD, USER, bot=bot),
            {"coins": 2.0, "diamonds": 1.5})

        await prestige.activate_booster_prestige(
            GUILD, USER, 0, item_id=listing, bot=bot)
        # VI in effect while V is still owned: VI's numbers alone (3.0 / 4.0),
        # neither multiplied with V's (6.0 / 6.0) nor added to them (5.0 / 5.5).
        self.assertEqual(
            await prestige.get_effective_prestige_multipliers(GUILD, USER, bot=bot),
            {"coins": 3.0, "diamonds": 4.0})
        self.assertEqual(
            await prestige.get_prestige_earn_multiplier(GUILD, USER, "balance", bot=bot),
            3.0)

        member.premium_since = None
        self.assertEqual(
            await prestige.get_effective_prestige_multipliers(GUILD, USER, bot=bot),
            {"coins": 2.0, "diamonds": 1.5})

    async def test_a_coin_reward_is_scaled_once_by_the_effective_tier(self):
        seed_member(balance=0, tier=5)
        listing = seed_listing(tier=6, stock=None)
        await prestige.set_prestige_multipliers(GUILD, tiers={
            5: {"coins": 2.0, "diamonds": 1.0},
            6: {"coins": 3.0, "diamonds": 1.0},
        })
        _, _, bot = prestige_world(boosting=True)
        await prestige.activate_booster_prestige(
            GUILD, USER, 0, item_id=listing, bot=bot)

        result = await reward_engine.give_reward(
            bot, GUILD, USER, "coins", amount=100, reason="test", source="event")
        self.assertTrue(result["success"], result)
        self.assertEqual(rows("SELECT balance FROM economy WHERE guild_id=? "
                              "AND user_id=?", (GUILD, USER)), [(300,)])

    async def test_member_wears_exactly_the_role_of_the_effective_tier(self):
        seed_member(balance=9000, tier=1)
        configure_tier_roles()
        listing = seed_listing(tier=6, stock=None)
        member, guild, bot = prestige_world(boosting=True)

        await prestige.sync_prestige_roles(bot, guild, member)
        self.assertEqual(worn_tier_roles(member), [TIER_ROLES[1]])

        await prestige.purchase_prestige(GUILD, USER, 2, 2500)
        await prestige.sync_prestige_roles(bot, guild, member)
        self.assertEqual(worn_tier_roles(member), [TIER_ROLES[2]])   # I replaced

        await prestige.activate_booster_prestige(
            GUILD, USER, 0, item_id=listing, bot=bot)
        await prestige.sync_prestige_roles(bot, guild, member)
        self.assertEqual(worn_tier_roles(member), [TIER_ROLES[6]])   # II replaced
        self.assertEqual(self.stored_tier(), 2)                      # ...but kept

        member.premium_since = None
        await prestige.sync_prestige_roles(bot, guild, member)
        self.assertEqual(worn_tier_roles(member), [TIER_ROLES[2]])   # back to II

    async def test_a_stray_prestige_role_grants_nothing_and_is_removed(self):
        seed_member(balance=9000, tier=1)
        configure_tier_roles()
        await prestige.set_prestige_multipliers(GUILD, tiers={
            1: {"coins": 1.0, "diamonds": 1.0},
            4: {"coins": 9.0, "diamonds": 9.0},
        })
        member, guild, bot = prestige_world()
        member.roles.append(guild.get_role(TIER_ROLES[4]))   # handed out by hand

        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=bot), 1)
        self.assertEqual(
            await prestige.get_effective_prestige_multipliers(GUILD, USER, bot=bot),
            {"coins": 1.0, "diamonds": 1.0})
        await prestige.sync_prestige_roles(bot, guild, member)
        self.assertEqual(worn_tier_roles(member), [TIER_ROLES[1]])

    async def test_rank_is_per_member_and_per_guild(self):
        seed_member(balance=9000, tier=0)
        seed_member(user=OTHER_USER, balance=9000, tier=0)
        seed_member(guild=OTHER_GUILD, balance=9000, tier=0)
        await prestige.purchase_prestige(GUILD, USER, 1, 2500)
        self.assertEqual(self.stored_tier(), 1)
        self.assertEqual(self.stored_tier(user=OTHER_USER), 0)
        self.assertEqual(self.stored_tier(guild=OTHER_GUILD), 0)
        self.assertEqual(rows("SELECT balance FROM economy WHERE user_id=? "
                              "OR guild_id=?", (OTHER_USER, OTHER_GUILD)),
                         [(9000,), (9000,)])


class ItemTypeCollisionTests(_ActionCase):
    def stack(self, name, user=USER, guild=GUILD):
        found = rows("SELECT item_type, quantity, metadata FROM inventory_items "
                     "WHERE guild_id=? AND user_id=? AND item_name=?",
                     (guild, user, name))
        return found[0] if found else None

    # ── refused: a functional item meets a different kind ───────────────
    async def test_plain_grant_cannot_convert_an_owned_potion(self):
        await self.give_potion("Tonic", 2)
        before = self.stack("Tonic")
        with self.assertRaisesRegex(inventory.ItemTypeConflict, "already uses this name"):
            await inventory.give_item(GUILD, USER, "Tonic", 1,
                                      item_type="custom", source="mission")
        self.assertEqual(self.stack("Tonic"), before)
        self.assertEqual(self.events(), ["acquired"])     # nothing recorded

        view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Tonic"))
        self.assertEqual(labels(view), ["Activate", "Back"])
        outcome = await self.click(view, wallet.ActivateButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual(self.quantity("Tonic"), 1)
        self.assertEqual(len(self.boosts()), 1)

    async def test_event_item_cannot_convert_an_owned_title(self):
        await inventory.give_item(GUILD, USER, "Champion", 1,
                                  item_type="title", source="shop")
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(GUILD, USER, "Champion", 1,
                                      item_type="event_drop", source="event")
        self.assertEqual(self.stack("Champion")[:2], ("title", 1))
        view, _ = await self.open_panel(wallet.TAB_TITLES, await self.key_of("Champion"))
        self.assertEqual(labels(view), ["Equip", "Back"])

    async def test_functional_types_cannot_convert_each_other_or_a_plain_item(self):
        await self.give_potion("Aurora", 1)
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(GUILD, USER, "Aurora", 1, item_type="title")
        self.assertEqual(self.stack("Aurora")[:2], ("potion", 1))

        await inventory.give_item(GUILD, USER, "Relic", 3,
                                  item_type="event_drop", source="event")
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(
                GUILD, USER, "Relic", 1, item_type="potion",
                metadata=potion_engine.build_metadata("xp_boost", 2.0, 1))
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(GUILD, USER, "Relic", 1, item_type="title")
        self.assertEqual(self.stack("Relic"), ("event_drop", 3, None))

    async def test_refused_grant_inside_a_caller_transaction_writes_nothing(self):
        import aiosqlite
        from database import DB_PATH
        await self.give_potion("Tonic", 1)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(inventory.ItemTypeConflict):
                await inventory.give_item(GUILD, USER, "Tonic", 5,
                                          item_type="custom", db=db)
            await db.commit()
        self.assertEqual(self.stack("Tonic")[:2], ("potion", 1))

    async def test_reward_engine_reports_the_refusal_instead_of_converting(self):
        await self.give_potion("Tonic", 1)
        # Mission and minigame item rewards: no explicit type -> plain item.
        mission = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Tonic",
            reason="Mission complete", source="mission")
        # Refused, never converted - and held for a later retry rather than
        # dropped (scripts/test_reward_name_conflicts.py covers the recovery).
        self.assertFalse(mission["success"])
        self.assertTrue(mission["name_conflict"])
        self.assertTrue(mission["pending"])
        self.assertFalse(mission["uncertain"])
        # Event item reward.
        event = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Tonic",
            item_type="event_drop", reason="Event reward", source="event")
        self.assertFalse(event["success"])
        # A Shop purchase leaves no purchase grant behind for the refused unit.
        shop = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Tonic",
            item_type="shop_custom", reason="Shop purchase", source="shop",
            op_id="op-collision-1")
        self.assertFalse(shop["success"])
        self.assertFalse(shop["pending"])            # refunded by the Shop instead
        self.assertIn("already uses this name", shop["error"])
        self.assertEqual(rows("SELECT * FROM reward_grants"), [])
        self.assertEqual(self.stack("Tonic")[:2], ("potion", 1))

    # ── allowed: same kind, or two plain collectibles ───────────────────
    async def test_same_type_still_stacks(self):
        await self.give_potion("Tonic", 1)
        await self.give_potion("Tonic", 2)
        self.assertEqual(self.stack("Tonic")[:2], ("potion", 3))
        await inventory.give_item(GUILD, USER, "Champion", 1, item_type="title")
        await inventory.give_item(GUILD, USER, "Champion", 1, item_type="title")
        self.assertEqual(self.stack("Champion")[:2], ("title", 2))

    async def test_plain_collectibles_stack_and_keep_the_stored_type(self):
        await inventory.give_item(GUILD, USER, "Golden Ticket", 1,
                                  item_type="event_drop", source="event")
        await inventory.give_item(GUILD, USER, "Golden Ticket", 2,
                                  item_type="custom", source="mission")
        await inventory.give_item(GUILD, USER, "Golden Ticket", 1,
                                  item_type="shop_custom", source="shop")
        self.assertEqual(self.stack("Golden Ticket")[:2], ("event_drop", 4))
        view, _ = await self.open_panel(wallet.TAB_ITEMS,
                                        await self.key_of("Golden Ticket"))
        self.assertEqual(labels(view), ["Back"])

    async def test_grant_without_a_type_keeps_the_stored_type(self):
        # The Level-correction restore only knows the item name.
        await self.give_potion("Tonic", 2)
        metadata = self.stack("Tonic")[2]
        await inventory.give_item(GUILD, USER, "Tonic", 1,
                                  source="level_correction_restore")
        self.assertEqual(self.stack("Tonic"), ("potion", 3, metadata))

        # ...including when the reversal had emptied the stack.
        await inventory.remove_item(GUILD, USER, "Tonic", 3)
        self.assertEqual(self.stack("Tonic")[:2], ("potion", 0))
        await inventory.give_item(GUILD, USER, "Tonic", 1,
                                  source="level_correction_restore")
        self.assertEqual(self.stack("Tonic"), ("potion", 1, metadata))
        view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Tonic"))
        self.assertEqual(labels(view), ["Activate", "Back"])

    async def test_new_name_without_a_type_is_a_plain_item(self):
        await inventory.give_item(GUILD, USER, "Pebble", 2)
        self.assertEqual(self.stack("Pebble"), ("custom", 2, None))

    async def test_an_emptied_row_is_not_owned_and_can_be_reused(self):
        await self.give_potion("Tonic", 1)
        await inventory.remove_item(GUILD, USER, "Tonic", 1)
        await inventory.give_item(GUILD, USER, "Tonic", 1,
                                  item_type="event_drop", source="event")
        # The old potion effect does not ride along on the new plain item.
        self.assertEqual(self.stack("Tonic"), ("event_drop", 1, None))
        view, _ = await self.open_panel(wallet.TAB_ITEMS, await self.key_of("Tonic"))
        self.assertEqual(labels(view), ["Back"])

    # ── separation that already exists ──────────────────────────────────
    async def test_collision_is_scoped_to_one_member_and_one_guild(self):
        await self.give_potion("Tonic", 1)
        await inventory.give_item(GUILD, OTHER_USER, "Tonic", 1,
                                  item_type="custom", source="mission")
        await inventory.give_item(OTHER_GUILD, USER, "Tonic", 1,
                                  item_type="title", source="shop")
        self.assertEqual(self.stack("Tonic")[:2], ("potion", 1))
        self.assertEqual(self.stack("Tonic", user=OTHER_USER)[:2], ("custom", 1))
        self.assertEqual(self.stack("Tonic", guild=OTHER_GUILD)[:2], ("title", 1))

    async def test_timed_holding_coexists_with_a_same_named_permanent_item(self):
        # Timed holdings live in their own table under their own key, so a
        # same-named permanent item of another kind is a separate entry.
        from utils import timed_ownership as timed
        await self.give_potion("Sky", 1)
        await timed.grant_timed_holding(
            GUILD, USER, "Sky", expires_at=(
                datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            item_type="shop_custom", source="shop")
        entries = [e for e in await inventory.get_inventory_entries(GUILD, USER)
                   if e["item_name"] == "Sky"]
        self.assertEqual(sorted(e["item_type"] for e in entries),
                         ["potion", "shop_custom"])
        self.assertEqual(len({e["entry_key"] for e in entries}), 2)
        self.assertEqual(self.stack("Sky")[:2], ("potion", 1))

    async def test_equipped_title_survives_a_refused_same_name_grant(self):
        await inventory.give_item(GUILD, USER, "Champion", 1,
                                  item_type="title", source="shop")
        await title_engine.equip_title(GUILD, USER, "Champion")
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(GUILD, USER, "Champion", 1,
                                      item_type="custom", source="mission")
        self.assertEqual(rows("SELECT item_name FROM equipped_titles WHERE "
                              "guild_id=? AND user_id=?", (GUILD, USER)),
                         [("Champion",)])
        self.assertEqual(
            (await title_engine.get_equipped_title(GUILD, USER))["item_name"],
            "Champion")


if __name__ == "__main__":
    unittest.main(verbosity=2)
