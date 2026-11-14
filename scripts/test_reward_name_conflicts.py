#!/usr/bin/env python3
"""
An earned reward is never lost to an item-name clash.

The Inventory holds one stack per item name, and a grant never changes what
an owned stack is. Two safeguards keep that from costing a member a reward,
both driven through the real code on a scratch SQLite database (only Discord
is stubbed):

  A. CREATION TIME - an administrator cannot create or rename a Shop product,
     Mission reward, Event reward or Minigame reward into a name that a
     different KIND of item already uses in that guild
     (utils.item_identity, one shared check).

  B. RUNTIME - if a clash still happens (a definition older than the check,
     an item the member got some other way), the completed mission / round /
     event stands, the reward is parked in pending_rewards, the member and
     staff are told why, and a retry delivers it exactly once after the name
     is free (utils.pending_rewards). Nothing has to be earned twice.

Also locked down here: plain collectibles from different sources stack
safely, and the permanent-role grant (the only caller of set_quantity)
refuses a name clash before it writes.

Run:  python3 scripts/test_reward_name_conflicts.py
"""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

from phase1_support import GUILD, USER, execute, rows, reset_database

from cogs import wallet  # noqa: E402
from cogs.events import Events  # noqa: E402
from database import DB_PATH  # noqa: E402
from utils import (  # noqa: E402
    equip_engine, inventory, item_identity, minigame_store, mission_engine,
    pending_rewards, potion_engine, reward_engine,
)
import dashboard.app as dashboard  # noqa: E402
from test_inventory_actions import (  # noqa: E402
    InventoryActionCase as _ActionCase, FLAME, labels,
)

OTHER_USER = 8202
OTHER_GUILD = 8102
CSRF = "conflict-csrf"
POTION_META = potion_engine.build_metadata("xp_boost", 2.0, 3)


def shop_listing(name, kind, guild=GUILD, **extra):
    columns = {"guild_id": guild, "name": name, "type": kind, "price": 100,
               "enabled": 1, **extra}
    return execute(
        f"INSERT INTO shop_items ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))


def legacy_mission(reward_value, *, reward_type="item", name="Chatter",
                   target=1, guild=GUILD, duration=None):
    """A definition written before the creation check existed."""
    return execute(
        "INSERT INTO missions_definitions (guild_id, name, type, target, "
        "period, reward_type, reward_value, reward_duration_hours) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (guild, name, "messages", target, "once", reward_type,
         str(reward_value), duration))


def pending(user=USER, guild=GUILD):
    return rows("SELECT reward_type, status, attempts, source FROM "
                "pending_rewards WHERE guild_id=? AND user_id=? ORDER BY id",
                (guild, user))


def stack(name, user=USER, guild=GUILD):
    found = rows("SELECT item_type, quantity FROM inventory_items "
                 "WHERE guild_id=? AND user_id=? AND item_name=?",
                 (guild, user, name))
    return found[0] if found else None


# ═══ A. creation-time validation ════════════════════════════════════════

async def fresh_database():
    """init_db plus the Mission and Minigame tables their modules own."""
    await reset_database()
    await mission_engine.ensure_tables()
    await minigame_store.ensure_tables()


class SharedNameCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fresh_database()

    async def conflict(self, name, kind, **kw):
        return await item_identity.find_name_conflict(GUILD, name, kind, **kw)

    async def test_every_definition_source_is_seen(self):
        shop_listing("Tonic", "potion")
        shop_listing("Champion", "title")
        shop_listing("Crimson", "color", role_id=FLAME)
        shop_listing("VIP", "role", role_id=FLAME)
        shop_listing("Guest", "temp_role", role_id=FLAME, duration_hours=5)
        for name in ("Tonic", "Champion", "Crimson", "VIP", "Guest"):
            self.assertIn(name, await self.conflict(name, item_identity.KIND_PLAIN))

        legacy_mission("Ticket")
        execute("INSERT INTO events (guild_id, title, type, reward_type, "
                "reward_value, enabled) VALUES (?,?,?,?,?,1)",
                (GUILD, "Race", "button_race", "item", "Trophy"))
        category = await minigame_store.create_category(
            GUILD, "Games", default_rewards=[
                {"reward_type": "item", "reward_value": "Medal"}])
        await minigame_store.create_template(
            GUILD, category["id"], "Quiz", minigame_store.VALID_GAME_TYPES[0],
            rewards=[{"reward_type": "item", "reward_value": "Badge"}])
        await minigame_store.save_config(GUILD, global_default_rewards=[
            {"reward_type": "item", "reward_value": "Token"}])
        for name, where in (("Ticket", "mission"), ("Trophy", "event"),
                            ("Medal", "minigame category"),
                            ("Badge", "minigame"), ("Token", "minigame default")):
            for kind in (item_identity.KIND_POTION, item_identity.KIND_TITLE,
                         item_identity.KIND_ROLE):
                reason = await self.conflict(name, kind)
                self.assertIsNotNone(reason, (name, kind))
                self.assertIn(where, reason)
            # Plain items from different sources may share the name.
            self.assertIsNone(await self.conflict(name, item_identity.KIND_PLAIN))

    async def test_same_kind_and_non_inventory_products_never_conflict(self):
        shop_listing("Tonic", "potion")
        shop_listing("Prestige II", "prestige", prestige_tier=2)
        shop_listing("Old Boost", "xp_boost")
        self.assertIsNone(await self.conflict("Tonic", item_identity.KIND_POTION))
        # Prestige is a rank and a legacy boost is applied at purchase.
        self.assertIsNone(await self.conflict("Prestige II", item_identity.KIND_PLAIN))
        self.assertIsNone(await self.conflict("Old Boost", item_identity.KIND_PLAIN))
        self.assertIsNone(item_identity.kind_of_shop_type("prestige"))
        self.assertIsNone(item_identity.kind_of_shop_type("xp_boost"))

    async def test_scope_is_the_guild_and_the_exact_stored_name(self):
        shop_listing("Tonic", "potion", guild=OTHER_GUILD)
        self.assertIsNone(await self.conflict("Tonic", item_identity.KIND_PLAIN))
        shop_listing("Tonic", "potion")
        self.assertIsNotNone(await self.conflict("Tonic", item_identity.KIND_PLAIN))
        self.assertIsNotNone(await self.conflict("  Tonic ", item_identity.KIND_PLAIN))
        # The Inventory compares names exactly, so these are different items.
        self.assertIsNone(await self.conflict("tonic", item_identity.KIND_PLAIN))
        self.assertIsNone(await self.conflict("Tonic II", item_identity.KIND_PLAIN))

    async def test_an_item_members_already_own_counts_without_any_listing(self):
        await inventory.give_item(GUILD, OTHER_USER, "Relic", 1,
                                  item_type="potion", metadata=POTION_META)
        reason = await self.conflict("Relic", item_identity.KIND_PLAIN)
        self.assertIn("1 member(s) already own", reason)
        # Used up -> nobody owns it -> the name is free again.
        await inventory.remove_item(GUILD, OTHER_USER, "Relic", 1)
        self.assertIsNone(await self.conflict("Relic", item_identity.KIND_PLAIN))

    async def test_a_finished_event_no_longer_reserves_its_name(self):
        execute("INSERT INTO events (guild_id, title, type, reward_type, "
                "reward_value, enabled) VALUES (?,?,?,?,?,0)",
                (GUILD, "Old race", "button_race", "item", "Trophy"))
        self.assertIsNone(await self.conflict("Trophy", item_identity.KIND_POTION))

    def test_level_rewards_are_shop_products_so_the_shop_check_covers_them(self):
        columns = {r[1] for r in rows("PRAGMA table_info(leveling_shop_rewards)")}
        self.assertIn("item_id", columns)
        self.assertFalse({"name", "item_name", "reward_value"} & columns)


class MissionCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fresh_database()

    async def create(self, reward_value, reward_type="item"):
        return await mission_engine.create_definition(
            GUILD, name="Chatter", mtype="messages", target=5,
            reward_type=reward_type, reward_value=reward_value)

    async def test_item_reward_named_like_a_potion_title_or_role_is_refused(self):
        shop_listing("Tonic", "potion")
        shop_listing("Champion", "title")
        shop_listing("Crimson", "color", role_id=FLAME)
        for name in ("Tonic", "Champion", "Crimson"):
            with self.assertRaisesRegex(ValueError, "cannot share a name"):
                await self.create(name)
        self.assertEqual(rows("SELECT * FROM missions_definitions"), [])

    async def test_allowed_names_and_other_reward_types_still_work(self):
        shop_listing("Tonic", "potion")
        shop_listing("Ticket", "custom")
        self.assertTrue(await self.create("Ticket"))        # plain + plain
        self.assertTrue(await self.create("Brand New"))
        self.assertTrue(await self.create("500", "coins"))  # not an item name
        self.assertEqual(len(rows("SELECT * FROM missions_definitions")), 3)


class EventCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fresh_database()

    async def run_command(self, reward_value):
        cog = Events.__new__(Events)
        cog._launch_event = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=GUILD),
            channel=SimpleNamespace(id=77, mention="#events"),
            response=SimpleNamespace(send_message=AsyncMock()))
        await Events.event_create.callback(
            cog, interaction, "Race", "item", reward_value)
        return cog, interaction.response.send_message.await_args

    async def test_conflicting_item_reward_is_refused_before_anything_is_stored(self):
        shop_listing("Tonic", "potion")
        cog, reply = await self.run_command("Tonic")
        self.assertIn("cannot share a name", reply.args[0])
        self.assertTrue(reply.kwargs["ephemeral"])
        self.assertEqual(rows("SELECT * FROM events"), [])
        cog._launch_event.assert_not_awaited()

    async def test_a_free_name_launches_the_event(self):
        shop_listing("Tonic", "potion")
        cog, reply = await self.run_command("Trophy")
        self.assertIn("Event launched", reply.args[0])
        self.assertEqual(rows("SELECT reward_value FROM events"), [("Trophy",)])
        cog._launch_event.assert_awaited_once()


class MinigameCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fresh_database()
        shop_listing("Tonic", "potion")
        self.category = await minigame_store.create_category(GUILD, "Games")
        self.bad = [{"reward_type": "coins", "reward_value": "5"},
                    {"reward_type": "item", "reward_value": "Tonic"}]
        self.good = [{"reward_type": "item", "reward_value": "Badge"},
                     {"reward_type": "coins", "reward_value": "5"}]

    async def test_every_minigame_reward_surface_refuses_a_conflicting_item(self):
        game = minigame_store.VALID_GAME_TYPES[0]
        with self.assertRaisesRegex(ValueError, "cannot share a name"):
            await minigame_store.create_template(
                GUILD, self.category["id"], "Quiz", game, rewards=self.bad)
        self.assertEqual(rows("SELECT * FROM minigame_templates"), [])
        self.assertEqual(rows("SELECT * FROM minigame_rewards"), [])

        template = await minigame_store.create_template(
            GUILD, self.category["id"], "Quiz", game, rewards=self.good)
        with self.assertRaisesRegex(ValueError, "cannot share a name"):
            await minigame_store.update_template(
                GUILD, template["id"], {"rewards": self.bad})
        self.assertEqual(
            rows("SELECT reward_value FROM minigame_rewards ORDER BY sort_order"),
            [("Badge",), ("5",)])                        # pool left intact

        with self.assertRaisesRegex(ValueError, "cannot share a name"):
            await minigame_store.create_category(
                GUILD, "More", default_rewards=self.bad)
        with self.assertRaisesRegex(ValueError, "cannot share a name"):
            await minigame_store.update_category(
                GUILD, self.category["id"], {"default_rewards": self.bad})
        with self.assertRaisesRegex(ValueError, "cannot share a name"):
            await minigame_store.save_config(
                GUILD, global_default_rewards=self.bad)
        self.assertEqual(rows("SELECT name FROM minigame_categories"), [("Games",)])
        self.assertNotIn("Tonic", json.dumps(rows(
            "SELECT default_rewards_json FROM minigame_categories")))
        self.assertNotIn("Tonic", json.dumps(await minigame_store.get_config(GUILD)))

    async def test_valid_pools_are_still_accepted_everywhere(self):
        await minigame_store.update_category(
            GUILD, self.category["id"], {"default_rewards": self.good})
        await minigame_store.save_config(GUILD, global_default_rewards=self.good)
        template = await minigame_store.create_template(
            GUILD, self.category["id"], "Quiz",
            minigame_store.VALID_GAME_TYPES[0], rewards=self.good)
        self.assertEqual(len(template["rewards"]), 2)


class DashboardCreationTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(fresh_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Staff", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token=CSRF)
        self.headers = {"X-CSRF-Token": CSRF}

    def create_product(self, **payload):
        return self.client.post("/api/shop/item", json=payload, headers=self.headers)

    def test_shop_product_cannot_take_the_name_of_a_plain_reward(self):
        legacy_mission("Ticket")
        for kind, extra in (("potion", {"xp_boost_multiplier": 2, "duration_hours": 3}),
                            ("title", {})):
            response = self.create_product(name="Ticket", type=kind, price=100, **extra)
            self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
            self.assertIn("cannot share a name", response.get_json()["error"])
            self.assertIn("mission", response.get_json()["error"])
        self.assertEqual(rows("SELECT * FROM shop_items"), [])

        # A plain product may share the name; it stacks with the reward.
        response = self.create_product(name="Ticket", type="custom", price=100)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(rows("SELECT name, type FROM shop_items"),
                         [("Ticket", "custom")])

    def test_shop_products_of_different_kinds_cannot_share_a_name(self):
        shop_listing("Aurora", "title")
        response = self.create_product(name="Aurora", type="potion", price=100,
                                       xp_boost_multiplier=2, duration_hours=3)
        self.assertEqual(response.status_code, 400)
        response = self.create_product(name="Aurora", type="custom", price=100)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(rows("SELECT type FROM shop_items"), [("title",)])
        # Same kind, same name is not a type clash.
        response = self.create_product(name="Aurora", type="title", price=100)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_rename_into_a_clash_is_refused_and_other_edits_stay_possible(self):
        legacy_mission("Ticket")
        free = shop_listing("Elixir", "potion", xp_boost_multiplier=2.0,
                            duration_hours=3)
        response = self.client.put(f"/api/shop/item/{free}", json={"name": "Ticket"},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertIn("cannot share a name", response.get_json()["error"])
        self.assertEqual(rows("SELECT name FROM shop_items WHERE id=?", (free,)),
                         [("Elixir",)])
        response = self.client.put(f"/api/shop/item/{free}", json={"name": "Draught"},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        # A listing that already clashes (older than the check) is not locked:
        # its other fields can still be edited, and so can its name.
        clashing = shop_listing("Ticket", "potion", xp_boost_multiplier=2.0,
                                duration_hours=3)
        response = self.client.put(f"/api/shop/item/{clashing}", json={"price": 250},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(rows("SELECT price FROM shop_items WHERE id=?", (clashing,)),
                         [(250,)])

    def test_minigame_api_reports_the_clash_instead_of_failing(self):
        shop_listing("Tonic", "potion")
        bad = [{"reward_type": "item", "reward_value": "Tonic"}]
        response = self.client.post("/api/minigames/config", headers=self.headers,
                                    json={"global_default_rewards": bad})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("cannot share a name", response.get_json()["error"])

        response = self.client.post("/api/minigames/categories", headers=self.headers,
                                    json={"name": "Games", "default_rewards": bad})
        self.assertFalse(response.get_json()["success"])
        self.assertIn("cannot share a name", response.get_json()["error"])
        self.assertEqual(rows("SELECT * FROM minigame_categories"), [])


# ═══ B. runtime safety ══════════════════════════════════════════════════

class RewardPreservationCase(_ActionCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await mission_engine.ensure_tables()

    async def complete_mission(self, user=USER):
        return await mission_engine.record_activities(
            self.bot, GUILD, user, {"messages": 1})

    def progress(self, user=USER):
        return rows("SELECT progress, completed FROM mission_progress "
                    "WHERE guild_id=? AND user_id=?", (GUILD, user))


class MissionRewardPreservationTests(RewardPreservationCase):
    async def test_completed_mission_keeps_its_refused_reward_and_pays_it_later(self):
        legacy_mission("Tonic")                       # plain item reward...
        await self.give_potion("Tonic", 2)            # ...and an owned potion
        potion_before = rows("SELECT item_type, quantity, metadata FROM "
                             "inventory_items WHERE item_name='Tonic'")

        completed = await self.complete_mission()

        # The objective is done and stays done.
        self.assertEqual([d["name"] for d in completed], ["Chatter"])
        self.assertEqual(self.progress(), [(1, 1)])
        # The potion was not converted and no unit was added to it.
        self.assertEqual(rows("SELECT item_type, quantity, metadata FROM "
                              "inventory_items WHERE item_name='Tonic'"),
                         potion_before)
        # The reward is held, with what it is and where it came from.
        self.assertEqual(pending(), [("item", "pending", 1, "mission")])
        held = (await pending_rewards.list_pending(GUILD, USER))[0]
        self.assertEqual(held["label"], "1× Tonic")
        self.assertEqual(held["reason"], "Mission complete: Chatter")
        self.assertIn("already uses this name", held["last_error"])

        # More activity cannot complete it again or hold a second reward.
        self.assertEqual(await self.complete_mission(), [])
        self.assertEqual(self.progress(), [(1, 1)])
        self.assertEqual(len(pending()), 1)

        # Retrying while the clash is still there keeps it pending.
        outcome = await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(outcome, {"delivered": 0, "still_pending": 1, "uncertain": 0})
        self.assertEqual(pending(), [("item", "pending", 2, "mission")])
        self.assertEqual(stack("Tonic"), ("potion", 2))

        # The member uses the potions up: the name is free.
        await inventory.remove_item(GUILD, USER, "Tonic", 2)
        outcome = await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(outcome, {"delivered": 1, "still_pending": 0, "uncertain": 0})
        self.assertEqual(stack("Tonic"), ("custom", 1))
        self.assertEqual(pending(), [("item", "delivered", 3, "mission")])
        self.assertEqual(await pending_rewards.list_pending(GUILD, USER), [])
        self.assertEqual(self.progress(), [(1, 1)])    # never had to redo it

        # Delivered once: further retries pay nothing more.
        await pending_rewards.retry(self.bot, GUILD, USER)
        await pending_rewards.retry(self.bot)
        self.assertEqual(stack("Tonic"), ("custom", 1))
        history = rows("SELECT event_type, item_type, source FROM inventory_activity "
                       "WHERE item_name='Tonic' AND source='mission'")
        self.assertEqual(history, [("acquired", "custom", "mission")])

    async def test_a_mission_without_a_clash_is_delivered_at_once(self):
        legacy_mission("Ticket")
        await self.give_potion("Tonic", 1)
        await self.complete_mission()
        self.assertEqual(stack("Ticket"), ("custom", 1))
        self.assertEqual(pending(), [])

    async def test_opening_the_inventory_explains_and_then_delivers(self):
        legacy_mission("Tonic")
        await self.give_potion("Tonic", 1)
        await self.complete_mission()

        _, embed = await self.open_tab(wallet.TAB_POTIONS)
        self.assertIn("Reward waiting", embed.description)
        self.assertIn("1× Tonic", embed.description)
        self.assertEqual(stack("Tonic"), ("potion", 1))
        self.assertEqual(pending()[0][1], "pending")

        # The member activates the potion from the panel...
        view, _ = await self.open_panel(wallet.TAB_POTIONS, await self.key_of("Tonic"))
        self.assertEqual(labels(view), ["Activate", "Back"])
        self.assertEqual((await self.click(view, wallet.ActivateButton))[0], "rendered")
        # ...and the next time the Inventory opens, the reward has arrived.
        _, embed = await self.open_tab(wallet.TAB_ITEMS)
        self.assertNotIn("Reward waiting", embed.description)
        self.assertIn("Tonic", embed.description)
        self.assertEqual(stack("Tonic"), ("custom", 1))
        self.assertEqual(pending()[0][1], "delivered")

    async def test_scheduler_sweep_delivers_for_every_member(self):
        legacy_mission("Tonic")
        for user in (USER, OTHER_USER):
            await self.give_potion("Tonic", 1, user=user)
            await self.complete_mission(user)
            await inventory.remove_item(GUILD, user, "Tonic", 1)
        outcome = await pending_rewards.retry(self.bot)       # what the loop calls
        self.assertEqual(outcome["delivered"], 2)
        self.assertEqual(stack("Tonic"), ("custom", 1))
        self.assertEqual(stack("Tonic", user=OTHER_USER), ("custom", 1))

    async def test_two_retries_at_once_deliver_exactly_once(self):
        legacy_mission("Tonic")
        await self.give_potion("Tonic", 1)
        await self.complete_mission()
        await inventory.remove_item(GUILD, USER, "Tonic", 1)
        outcomes = await asyncio.gather(
            pending_rewards.retry(self.bot, GUILD, USER),
            pending_rewards.retry(self.bot, GUILD, USER),
            pending_rewards.retry(self.bot))
        self.assertEqual(sum(o["delivered"] for o in outcomes), 1)
        self.assertEqual(stack("Tonic"), ("custom", 1))

    async def test_held_rewards_are_scoped_to_their_member_and_guild(self):
        legacy_mission("Tonic")
        await self.give_potion("Tonic", 1)
        await self.complete_mission()
        self.assertEqual(await pending_rewards.list_pending(GUILD, OTHER_USER), [])
        self.assertEqual(await pending_rewards.list_pending(OTHER_GUILD, USER), [])
        await inventory.remove_item(GUILD, USER, "Tonic", 1)
        untouched = {"delivered": 0, "still_pending": 0, "uncertain": 0}
        self.assertEqual(await pending_rewards.retry(self.bot, GUILD, OTHER_USER), untouched)
        self.assertEqual(await pending_rewards.retry(self.bot, OTHER_GUILD), untouched)
        self.assertEqual(pending()[0][1], "pending")


class OtherSourcesPreservationTests(RewardPreservationCase):
    async def test_minigame_and_event_item_rewards_are_held_with_their_identity(self):
        await inventory.give_item(GUILD, USER, "Champion", 1, item_type="title")
        minigame = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", item_name="Champion",
            reason="Minigame win", source="minigame")
        event = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Champion",
            item_type="event_drop", reason="Event reward: Champion", source="event")
        for result in (minigame, event):
            self.assertFalse(result["success"])
            self.assertTrue(result["pending"])
            self.assertFalse(result["uncertain"])
            self.assertIn("held as pending reward", result["error"])
        self.assertEqual(stack("Champion"), ("title", 1))
        self.assertEqual([p[3] for p in pending()], ["minigame", "event"])

        await inventory.remove_item(GUILD, USER, "Champion", 1)
        outcome = await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(outcome["delivered"], 2)
        # Both are plain collectibles, so they share one stack of 2; the first
        # delivered sets the stored type, each keeps its own source in History.
        self.assertEqual(stack("Champion"), ("custom", 2))
        self.assertEqual(
            rows("SELECT source FROM inventory_activity WHERE item_name='Champion' "
                 "AND source IN ('minigame','event') ORDER BY id"),
            [("minigame",), ("event",)])

    async def test_event_winner_is_told_the_reward_is_held(self):
        from cogs.events import ButtonRaceView
        execute("INSERT INTO events (id, guild_id, title, type, reward_type, "
                "reward_value, max_winners, enabled) VALUES (1,?,?,?,?,?,3,1)",
                (GUILD, "Race", "button_race", "item", "Tonic"))
        await self.give_potion("Tonic", 1)
        view = ButtonRaceView(1, 3, "item", "Tonic", None)
        self._views.append(view)
        view._finish_if_full = AsyncMock()
        itx = SimpleNamespace(
            client=self.bot, guild=self.guild,
            user=SimpleNamespace(id=USER, mention="<@1>", display_name="member"),
            response=SimpleNamespace(send_message=AsyncMock()))
        button = next(c for c in view.children)
        await button.callback(itx)

        message = itx.response.send_message.await_args.args[0]
        self.assertIn("being held", message)
        self.assertNotIn("could not be delivered", message)
        self.assertEqual(rows("SELECT user_id FROM event_winners"), [(USER,)])
        self.assertEqual(pending(), [("item", "pending", 1, "event")])
        self.assertEqual(stack("Tonic"), ("potion", 1))

    async def test_role_reward_clashing_with_an_owned_item_is_held_then_granted(self):
        # A role reward is stored under the Discord role's name ("Flame").
        await self.give_potion("Flame", 1)
        result = await reward_engine.give_reward(
            self.bot, GUILD, USER, "role", role_id=FLAME,
            reason="Mission complete: Chatter", source="mission")
        self.assertFalse(result["success"])
        self.assertTrue(result["pending"])
        self.assertEqual(stack("Flame"), ("potion", 1))
        self.assertEqual(self.member.roles, [])          # nothing half-applied
        self.assertEqual(self.role_slot(), [])
        self.assertEqual(pending(), [("role", "pending", 1, "mission")])

        await inventory.remove_item(GUILD, USER, "Flame", 1)
        outcome = await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(outcome["delivered"], 1)
        self.assertEqual(stack("Flame"), ("role", 1))
        self.assertEqual([r.id for r in self.member.roles], [FLAME])
        self.assertEqual(self.role_slot()[0][:2], ("Flame", FLAME))

    async def test_shop_purchase_is_a_plain_refundable_failure_not_a_held_reward(self):
        await self.give_potion("Tonic", 1)
        result = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Tonic",
            item_type="shop_custom", reason="Shop purchase: Tonic", source="shop",
            op_id="op-1")
        self.assertFalse(result["success"])
        self.assertFalse(result["uncertain"])            # -> the Shop refunds
        self.assertFalse(result["pending"])
        self.assertTrue(result["name_conflict"])
        self.assertEqual(pending(), [])
        self.assertEqual(rows("SELECT * FROM reward_grants"), [])
        self.assertEqual(stack("Tonic"), ("potion", 1))

    async def test_a_failed_retry_never_parks_a_second_copy(self):
        await self.give_potion("Tonic", 1)
        await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", item_name="Tonic", source="mission")
        for _ in range(3):
            await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(pending(), [("item", "pending", 4, "mission")])


class PermanentRoleGrantCollisionTests(RewardPreservationCase):
    """The permanent-role grant is the only caller of inventory.set_quantity."""

    def test_set_quantity_has_exactly_one_caller(self):
        import re
        from phase1_support import ROOT
        callers = []
        for folder in ("cogs", "utils", "dashboard"):
            for path in (ROOT / folder).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if path.name != "inventory.py" and re.search(
                        r"\bset_quantity\(", text):
                    callers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(callers, ["utils/reward_engine.py"])

    async def test_role_grant_refuses_every_kind_of_same_name_stack(self):
        seeds = (("potion", POTION_META), ("title", None), ("event_drop", None),
                 ("role", {"role_id": 4242}))             # a DIFFERENT role
        for item_type, metadata in seeds:
            with self.subTest(owned=item_type):
                await reset_database()
                self.member.roles.clear()
                await inventory.give_item(GUILD, USER, "Flame", 2 if item_type != "role" else 1,
                                          item_type=item_type, metadata=metadata)
                before = rows("SELECT item_type, quantity, metadata FROM inventory_items")
                result = await reward_engine.give_reward(
                    self.bot, GUILD, USER, "role", role_id=FLAME, item_name="Flame",
                    reason="Shop purchase: Flame", source="shop", op_id="op-role")
                self.assertFalse(result["success"])
                self.assertFalse(result["uncertain"])
                self.assertTrue(result["name_conflict"], result)
                # set_quantity never ran: type, quantity and metadata intact.
                self.assertEqual(rows("SELECT item_type, quantity, metadata "
                                      "FROM inventory_items"), before)
                self.assertEqual(self.member.roles, [])
                self.assertEqual(self.role_slot(), [])
                self.assertEqual(pending(), [])

    async def test_role_grant_still_delivers_when_the_name_is_free_or_emptied(self):
        result = await reward_engine.give_reward(
            self.bot, GUILD, USER, "role", role_id=FLAME, item_name="Flame",
            reason="Event reward", source="event")
        self.assertTrue(result["success"], result)
        self.assertEqual(stack("Flame"), ("role", 1))

        await reset_database()
        self.member.roles.clear()
        await self.give_potion("Flame", 1)
        await inventory.remove_item(GUILD, USER, "Flame", 1)   # row left at 0
        result = await reward_engine.give_reward(
            self.bot, GUILD, USER, "role", role_id=FLAME, item_name="Flame",
            reason="Event reward", source="event")
        self.assertTrue(result["success"], result)
        self.assertEqual(stack("Flame"), ("role", 1))


class PlainCollectibleStackingTests(RewardPreservationCase):
    """Event + Mission + Shop collectibles of one name behave as one item."""

    async def seed(self):
        event = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Ticket",
            item_type="event_drop", reason="Event reward", source="event")
        mission = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", item_name="Ticket",
            reason="Mission complete", source="mission")
        shop = await reward_engine.give_reward(
            self.bot, GUILD, USER, "item", amount=1, item_name="Ticket",
            item_type="shop_custom", reason="Shop purchase", source="shop",
            op_id="op-ticket")
        return event, mission, shop

    async def test_ownership_rendering_and_history(self):
        for result in await self.seed():
            self.assertTrue(result["success"], result)
        self.assertEqual(stack("Ticket"), ("event_drop", 3))
        self.assertEqual(pending(), [])
        self.assertTrue(await inventory.has_item(GUILD, USER, "Ticket", 3))

        # Rendering: one entry, Items tab, no action beyond Back.
        entries = [e for e in await inventory.get_inventory_entries(GUILD, USER)
                   if e["item_name"] == "Ticket"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["quantity"], 3)
        tabs = wallet.split_by_tab(entries)
        self.assertEqual([len(tabs[t]) for t in (
            wallet.TAB_ITEMS, wallet.TAB_COLORS, wallet.TAB_POTIONS,
            wallet.TAB_TITLES)], [1, 0, 0, 0])
        _, embed = await self.open_tab(wallet.TAB_ITEMS)
        self.assertIn("Ticket", embed.description)
        view, detail = await self.open_panel(wallet.TAB_ITEMS, entries[0]["entry_key"])
        self.assertEqual(labels(view), ["Back"])
        self.assertEqual(next(f.value for f in detail.fields if f.name == "Type"),
                         "Collectible")

        # History: one row per acquisition, each with its real source.
        self.assertEqual(
            rows("SELECT event_type, quantity, source FROM inventory_activity "
                 "WHERE item_name='Ticket' ORDER BY id"),
            [("acquired", 1, "event"), ("acquired", 1, "mission"),
             ("acquired", 1, "shop")])
        _, history = await self.open_tab(wallet.TAB_HISTORY)
        self.assertEqual(history.description.count("Ticket"), 3)

    async def test_units_leave_the_shared_stack_normally(self):
        await self.seed()
        self.assertEqual(await inventory.remove_item(GUILD, USER, "Ticket", 2), 1)
        self.assertEqual(stack("Ticket"), ("event_drop", 1))

    async def test_shop_refund_takes_back_only_its_own_unit(self):
        from utils import level_ops
        _, _, shop = await self.seed()
        self.assertIsNotNone(shop["grant_id"])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            outcome = await level_ops.reverse_purchase_grant(
                db, operation_id="op-ticket", guild_id=GUILD, user_id=USER,
                item_name="Ticket")
            await db.commit()
        self.assertEqual((outcome["status"], outcome["units_removed"]),
                         ("applied", 1), outcome)
        # The Event and Mission units are untouched by the Shop's refund.
        self.assertEqual(stack("Ticket"), ("event_drop", 2))

    async def test_plain_types_never_merge_with_a_functional_item(self):
        await self.seed()
        for item_type, metadata in (("potion", POTION_META), ("title", None)):
            with self.assertRaises(inventory.ItemTypeConflict):
                await inventory.give_item(GUILD, USER, "Ticket", 1,
                                          item_type=item_type, metadata=metadata)
        self.assertEqual(stack("Ticket"), ("event_drop", 3))
        self.assertFalse(inventory.FUNCTIONAL_ITEM_TYPES
                         & {"custom", "shop_custom", "event_drop", "item"})


class DashboardPendingRewardTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Staff", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token=CSRF)

    def test_staff_see_what_is_held_for_whom_and_why(self):
        async def seed():
            await inventory.give_item(GUILD, OTHER_USER, "Tonic", 1,
                                      item_type="potion", metadata=POTION_META)
            await reward_engine.give_reward(
                None, GUILD, OTHER_USER, "item", item_name="Tonic",
                reason="Mission complete: Chatter", source="mission")
            await inventory.give_item(OTHER_GUILD, OTHER_USER, "Tonic", 1,
                                      item_type="potion", metadata=POTION_META)
            await reward_engine.give_reward(
                None, OTHER_GUILD, OTHER_USER, "item", item_name="Tonic",
                reason="Foreign mission", source="mission")
        asyncio.run(seed())

        for path in ("/inventory", f"/inventory/{OTHER_USER}"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("Rewards waiting for delivery (1)", html, path)
            self.assertIn("1× Tonic", html)
            self.assertIn("Mission complete: Chatter", html)
            self.assertIn("already uses this name", html)
            self.assertNotIn("Foreign mission", html)        # other guild
        clean = self.client.get(f"/inventory/{USER}").get_data(as_text=True)
        self.assertNotIn("Rewards waiting for delivery", clean)


if __name__ == "__main__":
    unittest.main(verbosity=2)
