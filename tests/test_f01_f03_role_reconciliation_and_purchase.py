"""Regression and reproduction tests for F-01 + F-03:
- F-01: Safe DB-provenance reconciliation for legacy `equipped_roles` rows with
  `role_added IS NULL` without weakening fail-closed protection on unproven or
  corrupted rows.
- F-03: Safe permanent & timed role purchases during suspended / unconfirmed
  role state (no charge, no stock drain, no active timed countdown while
  delivery is unconfirmed; clean refund on settled timed-swap refusal; durable
  uncertain state on ambiguous swap timeout; clean retry & full lifecycle).
"""
import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from phase1_support import GUILD, USER, execute, rows, reset_database, seed_member  # noqa: E402
import discord  # noqa: E402
from cogs.shop import _process_purchase  # noqa: E402
from utils import equip_engine, inventory, timed_ownership  # noqa: E402

FLAME = 9701
BLUE = 9702
GOLD = 9703


class StubRole:
    def __init__(self, role_id: int, name: str, position: int = 5):
        self.id = int(role_id)
        self.name = name
        self.position = position
        self.mention = f"<@&{role_id}>"
        self.managed = False

    def is_default(self):
        return False

    def __eq__(self, other):
        return isinstance(other, StubRole) and self.id == other.id

    def __hash__(self):
        return hash(self.id)

    def __lt__(self, other):
        return self.position < other.position

    def __le__(self, other):
        return self.position <= other.position

    def __gt__(self, other):
        return self.position > other.position

    def __ge__(self, other):
        return self.position >= other.position


def make_discord_env(user_id: int = USER, guild_id: int = GUILD):
    role_objs = {
        FLAME: StubRole(FLAME, "Flame"),
        BLUE: StubRole(BLUE, "Blue"),
        GOLD: StubRole(GOLD, "Gold"),
    }
    bot_top = StubRole(9999, "BotTop", position=50)
    member = SimpleNamespace(
        id=user_id,
        name="Member",
        display_name="Member",
        mention=f"<@{user_id}>",
        roles=[],
        premium_since=None,
        bot=False,
        display_avatar=SimpleNamespace(url=""),
    )

    async def add_roles(role, reason=None):
        if role not in member.roles:
            member.roles.append(role)

    async def remove_roles(role, reason=None):
        if role in member.roles:
            member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)

    guild = SimpleNamespace(
        id=guild_id,
        owner_id=1,
        me=SimpleNamespace(
            id=777,
            top_role=bot_top,
            guild_permissions=SimpleNamespace(manage_roles=True, administrator=False),
        ),
        roles=[bot_top, *role_objs.values()],
        get_role=lambda rid: role_objs.get(int(rid)),
        get_member=lambda uid: member if int(uid) == user_id else None,
        fetch_member=AsyncMock(side_effect=lambda uid: member if int(uid) == user_id else None),
    )
    member.guild = guild
    bot = SimpleNamespace(
        get_guild=lambda gid: guild if int(gid) == guild_id else None,
    )
    return bot, guild, member, role_objs


def make_interaction(bot, guild, member):
    return SimpleNamespace(
        client=bot,
        guild=guild,
        user=member,
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=lambda: False,
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def seed_shop_role(name: str, kind: str, role_id: int, *, price: int = 250,
                   duration_hours: int | None = None, stock: int | None = 5) -> int:
    return execute(
        "INSERT INTO shop_items (guild_id, name, type, price, role_id, "
        "duration_hours, max_stock, current_stock, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (GUILD, name, kind, price, role_id, duration_hours, stock, stock),
    )


def balance_of(user_id: int = USER) -> int:
    r = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, user_id))
    return int(r[0][0]) if r else 0


def worn_ids(member) -> list[int]:
    return [r.id for r in member.roles]


class F01F03RoleReconciliationAndPurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=1000)
        self.bot, self.guild, self.member, self.roles = make_discord_env()

    # ── F-01: Legacy reconciliation with verifiable DB provenance ────────

    async def test_f01_legacy_shop_permanent_role_reconciles_for_unequip_and_swap(self):
        item_id = seed_shop_role("Flame", "role", FLAME, price=200)
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="shop",
            metadata={"role_id": FLAME, "product_id": item_id},
        )
        await inventory.set_quantity(
            GUILD, USER, "Blue", 1, item_type="role", source="shop",
            metadata={"role_id": BLUE},
        )
        execute(
            "INSERT INTO purchase_history "
            "(guild_id, user_id, user_display_name, item_id, item_name, price_paid) "
            "VALUES (?, ?, 'Member', ?, 'Flame', 200)",
            (GUILD, USER, item_id),
        )
        # Pre-migration legacy slot: entry_key IS NULL and role_added IS NULL.
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, NULL, NULL, NULL)",
            (GUILD, USER, FLAME),
        )
        self.member.roles.append(self.roles[FLAME])

        # Swap from legacy Flame -> Blue succeeds safely and removes Flame.
        swap_res = await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertTrue(swap_res["success"], swap_res)
        self.assertEqual(worn_ids(self.member), [BLUE])

        # Seed legacy Flame again and verify direct Unequip also reconciles & removes.
        execute("DELETE FROM equipped_roles WHERE guild_id=? AND user_id=?", (GUILD, USER))
        self.member.roles = [self.roles[FLAME]]
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, NULL, NULL, NULL)",
            (GUILD, USER, FLAME),
        )
        unequip_res = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(unequip_res["success"], unequip_res)
        self.assertTrue(unequip_res["role_removed"])
        self.assertEqual(worn_ids(self.member), [])
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))

    async def test_f01_legacy_mission_and_event_roles_reconcile_safely(self):
        from utils import mission_engine
        await mission_engine.ensure_tables()
        # Mission role grant before role_added existed
        mid = execute(
            "INSERT INTO missions_definitions "
            "(guild_id, name, type, period, target, reward_type, reward_value, enabled) "
            "VALUES (?, 'Chatter', 'messages', 'daily', 5, 'role', ?, 1)",
            (GUILD, str(FLAME)),
        )
        execute(
            "INSERT INTO mission_progress "
            "(guild_id, user_id, mission_id, period_key, progress, completed) "
            "VALUES (?, ?, ?, '2026-11-20', 5, 1)",
            (GUILD, USER, mid),
        )
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="mission",
            metadata={"role_id": FLAME},
        )
        inv_id = rows(
            "SELECT id FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Flame'",
            (GUILD, USER),
        )[0][0]
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, ?, NULL, NULL)",
            (GUILD, USER, FLAME, f"i:{inv_id}"),
        )
        self.member.roles = [self.roles[FLAME]]

        res = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(res["success"], res)
        self.assertEqual(worn_ids(self.member), [])

        # Event winner role grant before role_added existed
        ev_id = execute(
            "INSERT INTO events (guild_id, title, type, reward_type, reward_value, enabled) "
            "VALUES (?, 'Trivia', 'trivia', 'role', ?, 1)",
            (GUILD, str(BLUE)),
        )
        execute(
            "INSERT INTO event_winners (event_id, guild_id, user_id, user_display_name) "
            "VALUES (?, ?, ?, 'Member')",
            (ev_id, GUILD, USER),
        )
        await inventory.set_quantity(
            GUILD, USER, "Blue", 1, item_type="role", source="event",
            metadata={"role_id": BLUE},
        )
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Blue', ?, NULL, NULL, NULL)",
            (GUILD, USER, BLUE),
        )
        self.member.roles = [self.roles[BLUE]]
        res_ev = await equip_engine.unequip_role(self.bot, GUILD, USER, "Blue")
        self.assertTrue(res_ev["success"], res_ev)
        self.assertEqual(worn_ids(self.member), [])

    async def test_f01_delivery_intent_role_present_before_true_preserves_role(self):
        item_id = seed_shop_role("Flame", "role", FLAME, price=100)
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="shop",
            metadata={"role_id": FLAME, "product_id": item_id},
        )
        intent_json = json.dumps({
            "kind": "discord_role",
            "state": "recorded",
            "guild_id": GUILD,
            "user_id": USER,
            "role_id": FLAME,
            "role_present_before": True,
        })
        execute(
            "INSERT INTO shop_purchase_operations "
            "(op_id, guild_id, user_id, item_id, item_name, currency, amount, status, delivery_intent) "
            "VALUES ('op-preexisting', ?, ?, ?, 'Flame', 'balance', 100, 'delivered', ?)",
            (GUILD, USER, item_id, intent_json),
        )
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, NULL, NULL, NULL)",
            (GUILD, USER, FLAME),
        )
        self.member.roles = [self.roles[FLAME]]

        res = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(res["success"], res)
        self.assertTrue(res["role_kept"])
        self.assertEqual(worn_ids(self.member), [FLAME])
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))

    async def test_f01_fail_closed_preserved_for_unproven_or_corrupted_rows(self):
        # 1) Bare inventory row with no receipt/grant/subsystem history -> stays fail-closed.
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="manual_seed",
            metadata={"role_id": FLAME},
        )
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, NULL, NULL, NULL)",
            (GUILD, USER, FLAME),
        )
        self.member.roles = [self.roles[FLAME]]
        res1 = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(res1["success"], res1)
        self.assertTrue(res1["uncertain"])
        self.assertEqual(worn_ids(self.member), [FLAME])

        # 2) Corrupted/stale metadata (role_added_role_id points to BLUE while slot is FLAME)
        #    even when purchase_history exists for FLAME -> stays fail-closed!
        item_id = seed_shop_role("Flame", "role", FLAME, price=100)
        execute(
            "INSERT INTO purchase_history "
            "(guild_id, user_id, user_display_name, item_id, item_name, price_paid) "
            "VALUES (?, ?, 'Member', ?, 'Flame', 100)",
            (GUILD, USER, item_id),
        )
        execute(
            "UPDATE equipped_roles SET role_added=1, role_added_role_id=? "
            "WHERE guild_id=? AND user_id=?",
            (BLUE, GUILD, USER),
        )
        res2 = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(res2["success"], res2)
        self.assertTrue(res2["uncertain"])
        self.assertEqual(worn_ids(self.member), [FLAME])

    # ── F-03: Purchasing roles during suspended / unconfirmed state ──────

    async def test_f03_buying_permanent_or_timed_role_while_slot_suspended_charges_nothing_and_starts_no_timer(self):
        # Put member's equip slot into irreconcilable suspended state on FLAME.
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="unknown",
            metadata={"role_id": FLAME},
        )
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, NULL, NULL, NULL)",
            (GUILD, USER, FLAME),
        )
        self.member.roles = [self.roles[FLAME]]

        perm_id = seed_shop_role("BlueRole", "role", BLUE, price=300, stock=4)
        timed_id = seed_shop_role("TimedGold", "temp_role", GOLD, price=400, duration_hours=2, stock=4)

        # Attempt permanent role purchase during suspended state
        itx1 = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx1, perm_id)
        self.assertEqual(balance_of(), 1000, "permanent role purchase must not deduct money while suspended")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (perm_id,)),
            [(4,)],
        )

        # Attempt timed role purchase during suspended state
        itx2 = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx2, timed_id)
        self.assertEqual(balance_of(), 1000, "timed role purchase must not deduct money while suspended")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (timed_id,)),
            [(4,)],
        )
        self.assertEqual(
            await timed_ownership.get_timed_holdings(GUILD, USER),
            [],
            "timed role countdown must NEVER start while slot is suspended",
        )
        self.assertEqual(worn_ids(self.member), [FLAME])

    async def test_f03_buying_role_while_concurrent_role_delivery_in_flight_is_blocked_without_charge(self):
        perm_id = seed_shop_role("BlueRole", "role", BLUE, price=300, stock=3)
        timed_id = seed_shop_role("TimedGold", "temp_role", GOLD, price=350, duration_hours=3, stock=3)
        execute(
            "INSERT INTO shop_purchase_operations "
            "(op_id, guild_id, user_id, item_id, item_name, currency, amount, "
            "stock_reserved, charged, refunded, stock_returned, status, delivery_intent) "
            "VALUES ('op-delivering', ?, ?, ?, 'BlueRole', 'balance', 300, 1, 1, 0, 0, 'delivering', ?)",
            (GUILD, USER, perm_id, json.dumps({"kind": "discord_role", "state": "started", "role_id": BLUE})),
        )

        itx = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx, timed_id)
        self.assertEqual(balance_of(), 1000)
        self.assertEqual(await timed_ownership.get_timed_holdings(GUILD, USER), [])
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (timed_id,)),
            [(3,)],
        )

    async def test_f03_inflight_timed_role_forbidden_refunds_and_starts_no_timer(self):
        # Equip Flame normally (role_added=1).
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="shop",
            metadata={"role_id": FLAME},
        )
        self.assertTrue((await equip_engine.equip_role(self.bot, GUILD, USER, "Flame"))["success"])

        # Make removing Flame fail with 403 Forbidden during timed role purchase.
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
        )

        async def forbid_flame(role, reason=None):
            if role.id == FLAME:
                raise forbidden
            if role in self.member.roles:
                self.member.roles.remove(role)

        self.member.remove_roles = AsyncMock(side_effect=forbid_flame)

        timed_id = seed_shop_role("TimedGold", "temp_role", GOLD, price=400, duration_hours=2, stock=3)
        itx = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx, timed_id)

        # Cleanly refunded, stock returned, and NO active timed countdown!
        self.assertEqual(balance_of(), 1000)
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (timed_id,)),
            [(3,)],
        )
        self.assertEqual(await timed_ownership.get_timed_holdings(GUILD, USER), [])
        self.assertEqual(worn_ids(self.member), [FLAME])
        op_rows = rows(
            "SELECT status, refunded, stock_returned FROM shop_purchase_operations WHERE item_id=?",
            (timed_id,),
        )
        self.assertEqual(len(op_rows), 1)
        self.assertIn(op_rows[0][0], ("failed", "refunded"))
        self.assertEqual(op_rows[0][1:], (1, 1))

    async def test_f03_inflight_timed_role_timeout_parks_uncertain_with_no_active_timer_and_full_lifecycle_after_resolution(self):
        # 1. Start with a reconcilable legacy slot (Flame bought in shop before role_added).
        flame_shop_id = seed_shop_role("Flame", "role", FLAME, price=150)
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="shop",
            metadata={"role_id": FLAME, "product_id": flame_shop_id},
        )
        execute(
            "INSERT INTO purchase_history "
            "(guild_id, user_id, user_display_name, item_id, item_name, price_paid) "
            "VALUES (?, ?, 'Member', ?, 'Flame', 150)",
            (GUILD, USER, flame_shop_id),
        )
        execute(
            "INSERT INTO equipped_roles "
            "(guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, NULL, NULL, NULL)",
            (GUILD, USER, FLAME),
        )
        self.member.roles = [self.roles[FLAME]]

        # 2. First attempt to buy TimedGold hits a Discord timeout removing Flame:
        #    reconciliation succeeds first, then timeout parks op in `uncertain` with NO active timer!
        async def timeout_on_flame(role, reason=None):
            if role.id == FLAME:
                raise asyncio.TimeoutError("gateway timeout")
            if role in self.member.roles:
                self.member.roles.remove(role)

        self.member.remove_roles = AsyncMock(side_effect=timeout_on_flame)
        timed_id = seed_shop_role("TimedGold", "temp_role", GOLD, price=250, duration_hours=2, stock=4)
        itx1 = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx1, timed_id)

        self.assertEqual(await timed_ownership.get_timed_holdings(GUILD, USER), [])
        op_row = rows(
            "SELECT op_id, status FROM shop_purchase_operations WHERE item_id=?",
            (timed_id,),
        )
        self.assertEqual(op_row[0][1], "uncertain")

        # 3. Resolve/refund the uncertain operation (simulating staff/reconciler resolution)
        #    and restore normal Discord remove_roles; then retry purchase -> full lifecycle!
        op_id = op_row[0][0]
        self.member.roles = [self.roles[FLAME]]
        execute(
            "UPDATE shop_purchase_operations SET status='refunded', refunded=1, stock_returned=1 WHERE op_id=?",
            (op_id,),
        )
        execute("UPDATE economy SET balance=1000 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        execute("UPDATE shop_items SET current_stock=4 WHERE id=?", (timed_id,))

        async def normal_remove(role, reason=None):
            if role in self.member.roles:
                self.member.roles.remove(role)

        self.member.remove_roles = AsyncMock(side_effect=normal_remove)

        # Retry purchase: succeeds, equips TimedGold, removes legacy Flame!
        itx2 = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx2, timed_id)
        self.assertEqual(balance_of(), 750)
        self.assertEqual(worn_ids(self.member), [GOLD])
        holdings = await timed_ownership.get_timed_holdings(GUILD, USER)
        self.assertEqual(len(holdings), 1)
        timed_key = holdings[0]["entry_key"]

        # Swap to permanent Flame, then back to TimedGold, then Unequip, then Re-equip, then Expire!
        self.assertTrue((await equip_engine.equip_role(self.bot, GUILD, USER, "Flame"))["success"])
        self.assertEqual(worn_ids(self.member), [FLAME])

        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "TimedGold", entry_key=timed_key
        ))["success"])
        self.assertEqual(worn_ids(self.member), [GOLD])

        self.assertTrue((await equip_engine.unequip_role(
            self.bot, GUILD, USER, "TimedGold", entry_key=timed_key
        ))["success"])
        self.assertEqual(worn_ids(self.member), [])

        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "TimedGold", entry_key=timed_key
        ))["success"])
        self.assertEqual(worn_ids(self.member), [GOLD])

        # Expire TimedGold and verify cleanup removes Discord role and clears slot.
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        execute("UPDATE timed_holdings SET expires_at=? WHERE id=?", (past, holdings[0]["id"]))
        expired = await timed_ownership.expired_holdings()
        self.assertEqual(len(expired), 1)
        exp_res = await timed_ownership.expire_one(self.bot, expired[0])
        self.assertEqual(exp_res["status"], "removed")
        self.assertEqual(worn_ids(self.member), [])
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))

    async def test_f01_conflicting_delivery_intents_stay_fail_closed(self):
        item_id = seed_shop_role("Flame", "role", FLAME, price=200)
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="shop",
            metadata={"role_id": FLAME, "product_id": item_id},
        )
        inv_id = rows(
            "SELECT id FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Flame'",
            (GUILD, USER),
        )[0][0]
        # Op 1 observed role already present before; Op 2 observed absent before -> conflicting proof!
        for op_id, present in (("op-conf-1", True), ("op-conf-2", False)):
            execute(
                "INSERT INTO shop_purchase_operations "
                "(op_id, guild_id, user_id, item_id, item_name, currency, amount, status, "
                "stock_reserved, charged, refunded, stock_returned, delivery_intent, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'Flame', 'balance', 200, 'delivered', 0, 1, 0, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    op_id, GUILD, USER, item_id,
                    json.dumps({
                        "kind": "discord_role",
                        "role_id": FLAME,
                        "role_present_before": present,
                        "state": "recorded",
                    }),
                ),
            )
        execute(
            "INSERT INTO equipped_roles (guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, ?, NULL, NULL)",
            (GUILD, USER, FLAME, f"i:{inv_id}"),
        )
        self.member.roles = [self.roles[FLAME]]

        slot_dict = await equip_engine.get_equipped(GUILD, USER)
        rec = await equip_engine.reconcile_legacy_slot_observation(GUILD, USER, slot_dict)
        self.assertIsNone(rec)
        un = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(un["success"])
        self.assertTrue(un["uncertain"])
        self.assertEqual(worn_ids(self.member), [FLAME])

    async def test_f01_stale_non_role_refunded_or_unlinked_records_never_reconcile(self):
        # Unverified role row + legacy NULL slot
        potion_shop_id = execute(
            "INSERT INTO shop_items (guild_id, name, type, price, duration_hours, xp_boost_multiplier, enabled) "
            "VALUES (?, 'Flame', 'potion', 50, 2, 2.0, 1)",
            (GUILD,),
        )
        role_shop_id = seed_shop_role("RefundedFlame", "role", FLAME, price=200)
        await inventory.set_quantity(
            GUILD, USER, "Flame", 1, item_type="role", source="shop",
            metadata={"role_id": FLAME, "product_id": potion_shop_id},
        )
        inv_id = rows(
            "SELECT id FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Flame'",
            (GUILD, USER),
        )[0][0]
        # 1. Purchase history for a POTION sharing name/product_id
        execute(
            "INSERT INTO purchase_history (guild_id, user_id, user_display_name, item_id, item_name, price_paid) "
            "VALUES (?, ?, 'Member', ?, 'Flame', 50)",
            (GUILD, USER, potion_shop_id),
        )
        # 2. Purchase history for FLAME role that was REFUNDED in shop_purchase_operations
        ref_hist_id = execute(
            "INSERT INTO purchase_history (guild_id, user_id, user_display_name, item_id, item_name, price_paid) "
            "VALUES (?, ?, 'Member', ?, 'RefundedFlame', 200)",
            (GUILD, USER, role_shop_id),
        )
        execute(
            "INSERT INTO shop_purchase_operations "
            "(op_id, guild_id, user_id, item_id, item_name, currency, amount, status, "
            "stock_reserved, charged, refunded, stock_returned, history_id, created_at, updated_at) "
            "VALUES ('op-ref-1', ?, ?, ?, 'RefundedFlame', 'balance', 200, 'refunded', 0, 1, 1, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (GUILD, USER, role_shop_id, ref_hist_id),
        )
        # 3. Expired temp_roles row from the past
        past_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        execute(
            "INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at, source) "
            "VALUES (?, ?, ?, ?, 'legacy')",
            (GUILD, USER, FLAME, past_iso),
        )
        # 4. Non-role reward_grants row with the same item_name ('Flame' potion claim)
        #    AND a reversed role reward_grants row -> neither reconciles!
        claim_id = execute(
            "INSERT INTO level_reward_claims "
            "(guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at) "
            "VALUES (?, ?, 5, 'shop', 'all:5:potion', ?, 'fulfilled', CURRENT_TIMESTAMP)",
            (GUILD, USER, json.dumps({"type": "potion", "name": "Flame", "quantity": 1})),
        )
        execute(
            "INSERT INTO reward_grants "
            "(grant_id, guild_id, user_id, claim_id, reward_level, track, kind, item_name, quantity, remaining_quantity, created_at) "
            "VALUES ('rg-pot-1', ?, ?, ?, 5, 'shop', 'inventory_item', 'Flame', 1, 1, CURRENT_TIMESTAMP)",
            (GUILD, USER, claim_id),
        )
        rev_claim_id = execute(
            "INSERT INTO level_reward_claims "
            "(guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at) "
            "VALUES (?, ?, 6, 'shop', 'all:6:temp_role', ?, 'fulfilled', CURRENT_TIMESTAMP)",
            (GUILD, USER, json.dumps({"type": "temp_role", "name": "Flame", "role_id": FLAME, "quantity": 1, "duration_hours": 12})),
        )
        execute(
            "INSERT INTO reward_grants "
            "(grant_id, guild_id, user_id, claim_id, reward_level, track, kind, item_name, quantity, remaining_quantity, created_at) "
            "VALUES ('rg-rev-1', ?, ?, ?, 6, 'shop', 'inventory_item', 'Flame', 1, 1, CURRENT_TIMESTAMP)",
            (GUILD, USER, rev_claim_id),
        )
        execute(
            "INSERT INTO reward_reversals "
            "(reversal_id, operation_id, guild_id, user_id, claim_id, grant_id, kind, item_name, quantity, status, created_at) "
            "VALUES ('rr-1', 'op-corr-1', ?, ?, ?, 'rg-rev-1', 'inventory_item', 'Flame', 1, 'reversed', CURRENT_TIMESTAMP)",
            (GUILD, USER, rev_claim_id),
        )
        # 5. Unrelated minigame win in the same guild where another template awards FLAME
        from utils import minigame_store
        await minigame_store.ensure_tables()
        tmpl_id = execute(
            "INSERT INTO minigame_templates (guild_id, category_id, name, game_type, embed_json, channel_id, enabled) "
            "VALUES (?, 1, 'RoleGame', 'trivia', '{}', 111, 1)",
            (GUILD,),
        )
        execute(
            "INSERT INTO minigame_rewards (template_id, reward_type, reward_value) "
            "VALUES (?, 'role', ?)",
            (tmpl_id, str(FLAME)),
        )
        execute(
            "INSERT INTO minigames_log (guild_id, event_date, tier, channel_id, winner_id) "
            "VALUES (?, '2026-11-24', 'common', 222, ?)",
            (GUILD, USER),
        )

        execute(
            "INSERT INTO equipped_roles (guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'Flame', ?, ?, NULL, NULL)",
            (GUILD, USER, FLAME, f"i:{inv_id}"),
        )
        self.member.roles = [self.roles[FLAME]]

        slot_dict = await equip_engine.get_equipped(GUILD, USER)
        self.assertIsNone(
            await equip_engine.reconcile_legacy_slot_observation(GUILD, USER, slot_dict)
        )
        un = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(un["success"])
        self.assertTrue(un["uncertain"])
        self.assertEqual(worn_ids(self.member), [FLAME])

        # Conversely, an ACTIVE unreversed reward_grants role claim DOES reconcile!
        ok_claim_id = execute(
            "INSERT INTO level_reward_claims "
            "(guild_id, user_id, reward_level, track, reward_ref, payload_json, status, created_at) "
            "VALUES (?, ?, 7, 'shop', 'all:7:temp_role', ?, 'fulfilled', CURRENT_TIMESTAMP)",
            (GUILD, USER, json.dumps({"type": "temp_role", "name": "Flame", "role_id": FLAME, "quantity": 1, "duration_hours": 12})),
        )
        execute(
            "INSERT INTO reward_grants "
            "(grant_id, guild_id, user_id, claim_id, reward_level, track, kind, item_name, quantity, remaining_quantity, created_at) "
            "VALUES ('rg-ok-1', ?, ?, ?, 7, 'shop', 'inventory_item', 'Flame', 1, 1, CURRENT_TIMESTAMP)",
            (GUILD, USER, ok_claim_id),
        )
        self.assertIs(
            await equip_engine.reconcile_legacy_slot_observation(GUILD, USER, slot_dict),
            True,
        )

    async def test_f01_idempotent_repeat_reconciliation_and_same_role_renewal(self):
        item_id = seed_shop_role("TimedFlame", "temp_role", FLAME, price=150, duration_hours=12)
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        hid = await timed_ownership.grant_timed_holding(
            GUILD, USER, "TimedFlame", expires_at=future_iso,
            item_type="temp_role", metadata={"role_id": FLAME, "product_id": item_id},
            role_id=FLAME, product_id=item_id, source="shop",
        )
        execute(
            "INSERT INTO purchase_history (guild_id, user_id, user_display_name, item_id, item_name, price_paid) "
            "VALUES (?, ?, 'Member', ?, 'TimedFlame', 150)",
            (GUILD, USER, item_id),
        )
        execute(
            "INSERT INTO equipped_roles (guild_id, user_id, item_name, role_id, entry_key, role_added, role_added_role_id) "
            "VALUES (?, ?, 'TimedFlame', ?, ?, NULL, NULL)",
            (GUILD, USER, FLAME, f"h:{hid}"),
        )
        self.member.roles = [self.roles[FLAME]]

        # Renewing / buying a second holding of the SAME role while legacy slot has NULL
        # must reconcile (1, FLAME) instead of overwriting with (0, FLAME)!
        itx = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx, item_id)
        self.assertEqual(balance_of(), 850)
        slot_after = await equip_engine.get_equipped(GUILD, USER)
        self.assertEqual(
            (slot_after["role_added"], slot_after["role_added_role_id"]),
            (1, FLAME),
        )
        # Idempotency: calling reconcile_legacy_slot_observation repeatedly returns True!
        self.assertIs(
            await equip_engine.reconcile_legacy_slot_observation(GUILD, USER, slot_after),
            True,
        )
        self.assertIs(
            await equip_engine.reconcile_legacy_slot_observation(GUILD, USER, slot_after),
            True,
        )

    async def test_f03_delivering_level_claim_block_403_single_refund_timeout_and_db_fail(self):
        from unittest.mock import patch
        timed_id = seed_shop_role("TimedBlue", "temp_role", BLUE, price=200, duration_hours=12, stock=5)

        # 1) Active pending_rewards 'delivering' row of reward_type='level_claim' with role_id blocks BEFORE charge
        pr_id = execute(
            "INSERT INTO pending_rewards (guild_id, user_id, reward_type, payload, source, status, created_at, updated_at) "
            "VALUES (?, ?, 'level_claim', ?, 'level_claim', 'delivering', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (GUILD, USER, json.dumps({"claim_id": 99, "role_id": GOLD, "item_name": "Crown"})),
        )
        itx0 = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx0, timed_id)
        self.assertEqual(balance_of(), 1000)
        execute("DELETE FROM pending_rewards WHERE id=?", (pr_id,))

        # 2) Discord 403 Forbidden on assigning role -> certain failure, 1x refund, never double-refunded
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
        )
        self.member.add_roles = AsyncMock(side_effect=forbidden)
        itx_403 = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx_403, timed_id)
        self.assertEqual(balance_of(), 1000)
        op_403 = rows(
            "SELECT op_id, status, refunded, stock_returned FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
        )[0]
        self.assertEqual(op_403[1:], ("failed", 1, 1))
        # Second refund attempts via both helpers are strict no-ops
        await timed_ownership.refund_purchase(op_403[0])
        await timed_ownership.resolve_purchase_operation(op_403[0], action="refund", confirmed=True)
        self.assertEqual(balance_of(), 1000)
        self.assertEqual(rows("SELECT current_stock FROM shop_items WHERE id=?", (timed_id,)), [(5,)])

        # 3) Discord TimeoutError -> PURCHASE_UNCERTAIN (no auto-refund); operator resolve 'delivered' settles once
        self.member.add_roles = AsyncMock(side_effect=asyncio.TimeoutError("Discord REST timeout"))
        itx_to = make_interaction(self.bot, self.guild, self.member)
        await _process_purchase(itx_to, timed_id)
        self.assertEqual(balance_of(), 800)
        op_to = rows(
            "SELECT op_id, status, refunded FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
        )[0]
        self.assertEqual(op_to[1:], ("uncertain", 0))
        res_del = await timed_ownership.resolve_purchase_operation(
            op_to[0], action="delivered", confirmed=True, note="Verified role on Discord"
        )
        self.assertTrue(res_del["success"])
        self.assertEqual(
            rows("SELECT status, refunded FROM shop_purchase_operations WHERE op_id=?", (op_to[0],)),
            [("delivered", 0)],
        )

        # 4) Discord adds role (role_added=True) THEN DB write fails -> PURCHASE_UNCERTAIN (never auto-refunded!)
        async def normal_add(role, reason=None):
            if role not in self.member.roles:
                self.member.roles.append(role)

        self.member.add_roles = AsyncMock(side_effect=normal_add)
        with patch.object(
            timed_ownership,
            "grant_timed_holding",
            AsyncMock(side_effect=RuntimeError("disk full after Discord role grant")),
        ):
            itx_dbfail = make_interaction(self.bot, self.guild, self.member)
            await _process_purchase(itx_dbfail, timed_id)
        self.assertEqual(balance_of(), 600)
        op_dbf = rows(
            "SELECT op_id, status, refunded FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
        )[0]
        self.assertEqual(op_dbf[1:], ("uncertain", 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)

