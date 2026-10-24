"""Timed ownership, Colors, purchase saga and expiry cleanup.

Covers the four behaviours the Shop's new product model depends on:

  1. a permanent and a timed copy of the same name are DIFFERENT records
     (`entry_key`), with independent expiries, and an expired one is invisible
     to every read path before any cleanup has run;
  2. a Color is a category on a role-backed record — permanent or timed by its
     Duration only — and an unrelated role reward never becomes one;
  3. a failed purchase is compensated exactly once (money and stock) and an
     uncertain one is parked instead of guessed at;
  4. expiry removes the Discord role only when nothing else entitles the member
     to it, and the record survives as the retry marker until that succeeded.

Run: python test_timed_ownership.py
     python -m pytest test_timed_ownership.py
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, member,
)

from utils import timed_ownership as TO
from utils.inventory import get_inventory, find_entry
from utils.equip_engine import get_equipped, equip_role
from utils.reward_engine import give_reward
from utils.shop_validation import shop_input, ShopValidationError
from cogs.shop import process_purchase


# ── fixtures ────────────────────────────────────────────────────────────────

def shop_item(kind="custom", price=0, name="Thing", role_id=None,
              duration_hours=None, multiplier=None, stock=None, enabled=1,
              current=None):
    return execute(
        "INSERT INTO shop_items (guild_id,name,type,price,role_id,duration_hours,"
        "max_stock,current_stock,enabled,required_level,required_role_id,"
        "price_diamonds,xp_boost_multiplier) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,NULL,NULL,?)",
        (GUILD, name, kind, price, role_id, duration_hours, stock,
         stock if current is None else current, enabled, multiplier))


def role_stub(role_id, name="Flame"):
    return SimpleNamespace(id=role_id, name=name, position=1,
                           mention=f"<@&{role_id}>")


def purchase_ctx(granted_role=None, roles_held=()):
    person = member()
    person.roles = list(roles_held)
    person.add_roles = AsyncMock()
    person.remove_roles = AsyncMock()
    known = [granted_role] if granted_role is not None else []

    guild = SimpleNamespace(
        id=GUILD, owner_id=99,
        me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
        get_role=lambda rid: next(
            (r for r in known if r.id == int(rid)), None),
        get_member=lambda uid: person if uid == person.id else None,
    )
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)
    itx = SimpleNamespace(
        user=person, guild=guild, client=bot,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                 is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return itx, person, bot, guild


def replies(itx):
    return [c.kwargs for c in itx.response.send_message.call_args_list]


def reply_text(itx):
    parts = []
    for kw in replies(itx):
        embed = kw.get("embed")
        if embed is not None:
            parts.append(embed.title or "")
            parts.append(embed.description or "")
            for f in embed.fields:
                parts.append(f"{f.name} {f.value}")
        if kw.get("content"):
            parts.append(str(kw["content"]))
    for call in itx.response.send_message.call_args_list:
        if call.args and isinstance(call.args[0], str):
            parts.append(call.args[0])
    return " \n".join(parts)


def iso_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def purge_timed():
    execute("DELETE FROM timed_holdings")
    execute("DELETE FROM inventory_items")
    execute("DELETE FROM equipped_roles")


# ── 1. ownership identity ───────────────────────────────────────────────────

class OwnershipIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        purge_timed()

    async def test_permanent_and_timed_same_name_are_separate_records(self):
        from utils.inventory import give_item
        await give_item(GUILD, USER, "Golden Key", 2, item_type="shop_custom")
        granted = await TO.grant_timed_product(
            None, GUILD, USER, item_name="Golden Key", duration_hours=8,
            item_type="shop_custom")
        self.assertTrue(granted["success"])
        self.assertTrue(granted["entry_key"].startswith("h:"))

        entries = await get_inventory(GUILD, USER, include_empty=False)
        same = [e for e in entries if e["item_name"] == "Golden Key"]
        self.assertEqual(len(same), 2, "one permanent + one timed record")
        self.assertEqual({e["timed"] for e in same}, {False, True})
        permanent = next(e for e in same if not e["timed"])
        timed = next(e for e in same if e["timed"])
        self.assertEqual(permanent["quantity"], 2)
        self.assertEqual(timed["quantity"], 1)
        self.assertIsNotNone(timed["expires_at"])
        self.assertNotEqual(permanent["entry_key"], timed["entry_key"])

        # The name alone no longer identifies one of them.
        self.assertIsNone(find_entry(entries, item_name="Golden Key"))
        self.assertEqual(
            find_entry(entries, entry_key=timed["entry_key"])["entry_key"],
            timed["entry_key"])
        self.assertEqual(
            find_entry(entries, entry_key=permanent["entry_key"])["quantity"], 2)

    async def test_two_timed_grants_keeps_independent_expiries(self):
        first = await TO.grant_timed_product(
            None, GUILD, USER, item_name="Rope", duration_hours=1,
            item_type="shop_custom")
        second = await TO.grant_timed_product(
            None, GUILD, USER, item_name="Rope", duration_hours=5,
            item_type="shop_custom")
        holdings = await TO.get_timed_holdings(GUILD, USER)
        self.assertEqual(len(holdings), 2, "a grant must never merge into a row")
        self.assertNotEqual(first["expires_at"], second["expires_at"])
        self.assertEqual(
            rows("SELECT COUNT(*) FROM inventory_items"), [(0,)],
            "a timed grant must not create a permanent row")

        # Expiring the short one leaves the long one intact.
        execute("UPDATE timed_holdings SET expires_at=? WHERE id=?",
                (iso_in(-1), int(first["entry_key"].split(":")[1])))
        live = await get_inventory(GUILD, USER, include_empty=False)
        self.assertEqual([e["entry_key"] for e in live], [second["entry_key"]])

    async def test_expired_holding_is_invisible_before_cleanup(self):
        granted = await TO.grant_timed_product(
            None, GUILD, USER, item_name="Lantern", duration_hours=1,
            item_type="shop_custom")
        execute("UPDATE timed_holdings SET expires_at=? WHERE id=?",
                (iso_in(-0.5), int(granted["entry_key"].split(":")[1])))
        self.assertEqual(await get_inventory(GUILD, USER, include_empty=False), [],
                         "reads must filter on the deadline, not on cleanup")
        # ... and the row is still there for the cleanup loop to retire.
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])

    async def test_unparseable_expiry_counts_as_expired(self):
        granted = await TO.grant_timed_product(
            None, GUILD, USER, item_name="Odd", duration_hours=1,
            item_type="shop_custom")
        execute("UPDATE timed_holdings SET expires_at='not-a-date' WHERE id=?",
                (int(granted["entry_key"].split(":")[1]),))
        self.assertTrue(TO.is_expired("not-a-date"))
        self.assertEqual(await get_inventory(GUILD, USER, include_empty=False), [])

    async def test_remaining_time_is_formatted_from_the_record(self):
        granted = await TO.grant_timed_product(
            None, GUILD, USER, item_name="Torch", duration_hours=3,
            item_type="shop_custom")
        remaining = TO.format_remaining(granted["expires_at"])
        self.assertIsNotNone(remaining)
        self.assertIn("h", remaining)
        self.assertIsNone(TO.format_remaining(iso_in(-1)))


# ── 2. Colors ───────────────────────────────────────────────────────────────

class ColorCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        purge_timed()

    async def test_timed_color_listing_stamps_the_category(self):
        listing = shop_item("color", price=500, name="Flame", role_id=777,
                            duration_hours=24)
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(777))
        execute("UPDATE economy SET balance=5000 WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        itx, person, bot, _ = purchase_ctx(granted_role=role_stub(777))
        await process_purchase(itx, listing)

        holdings = await TO.get_timed_holdings(GUILD, USER)
        self.assertEqual(len(holdings), 1)
        holding = holdings[0]
        self.assertEqual(holding["item_type"], "temp_role")
        self.assertEqual(holding["category"], TO.CATEGORY_COLOR)
        self.assertEqual(holding["product_id"], listing)
        self.assertEqual(holding["role_id"], 777)
        self.assertIsNotNone(holding["expires_at"])

        entries = await get_inventory(GUILD, USER, include_empty=False)
        self.assertEqual(entries[0]["category"], TO.CATEGORY_COLOR)

    async def test_permanent_color_keeps_the_role_identity(self):
        listing = shop_item("color", price=500, name="Sea", role_id=888)
        execute("UPDATE economy SET balance=5000 WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        itx, person, bot, _ = purchase_ctx(granted_role=role_stub(888, "Sea"))
        await process_purchase(itx, listing)
        entries = await get_inventory(GUILD, USER, include_empty=False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["item_type"], "role")
        self.assertEqual(entries[0]["category"], TO.CATEGORY_COLOR)
        self.assertIsNone(entries[0]["expires_at"], "a permanent Color has no end")

    async def test_role_without_a_color_listing_never_becomes_a_color(self):
        meta = await TO.color_metadata(GUILD, 4242, {"role_id": 4242})
        self.assertNotIn("category", meta)

    async def test_ambiguous_listings_do_not_guess_the_variant(self):
        shop_item("color", price=100, name="Blue A", role_id=900, duration_hours=5)
        shop_item("color", price=200, name="Blue B", role_id=900, duration_hours=9)
        resolved = await TO.resolve_color_category(GUILD, 900)
        self.assertIsNone(resolved["product_id"],
                          "two listings for one role must not pick a variant/expiry")
        self.assertIsNone(resolved["item_name"])
        meta = await TO.color_metadata(GUILD, 900, {"role_id": 900})
        self.assertEqual(meta["category"], TO.CATEGORY_COLOR)
        self.assertNotIn("product_id", meta)

    async def test_single_listing_answers_with_its_product_id(self):
        listing = shop_item("color", price=100, name="Gold", role_id=901,
                            duration_hours=5)
        resolved = await TO.resolve_color_category(GUILD, 901)
        self.assertEqual(resolved["product_id"], listing)

    async def test_disabled_listing_does_not_answer(self):
        execute("UPDATE shop_items SET enabled=0")
        self.assertIsNone(await TO.resolve_color_category(GUILD, 4242))

    async def test_reward_path_stamps_the_category_but_not_an_expiry(self):
        shop_item("color", price=100, name="Rose", role_id=902, duration_hours=5)
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(902, "Rose"))
        result = await give_reward(
            bot, GUILD, USER, "role", role_id=902, item_name="Rose",
            reason="event", source="event")
        self.assertTrue(result["success"], str(result))
        entry = (await get_inventory(GUILD, USER, include_empty=False))[0]
        self.assertEqual(entry["category"], TO.CATEGORY_COLOR)
        self.assertIsNone(entry["expires_at"],
                          "the reward path applies its own duration, never the "
                          "listing's")
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])


# ── 3. expiry cleanup ───────────────────────────────────────────────────────

class ExpiryCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        purge_timed()

    async def _expire(self, entry_key):
        execute("UPDATE timed_holdings SET expires_at=? WHERE id=?",
                (iso_in(-1), int(entry_key.split(":")[1])))

    async def test_role_is_removed_once_and_the_record_dies_last(self):
        _, person, bot, guild = purchase_ctx(granted_role=role_stub(950))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=1,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=950)
        self.assertTrue(granted["success"], granted)
        person.roles = [role_stub(950)]
        await self._expire(granted["entry_key"])

        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 1, str(stats))
        person.remove_roles.assert_awaited_once()
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

    async def test_another_unexpired_holding_keeps_the_role(self):
        _, person, bot, guild = purchase_ctx(granted_role=role_stub(951))
        first = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=1,
            item_type="temp_role", role_id=951)
        second = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=48,
            item_type="temp_role", role_id=951)
        person.roles = [role_stub(951)]
        await self._expire(first["entry_key"])

        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 1, str(stats))
        person.remove_roles.assert_not_awaited()
        left = await TO.get_timed_holdings(GUILD, USER)
        self.assertEqual([h["entry_key"] for h in left], [second["entry_key"]])

    async def test_permanent_role_item_keeps_the_role(self):
        from utils.inventory import give_item
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(952))
        await give_item(GUILD, USER, "Flame", 1, item_type="role",
                        metadata={"role_id": 952})
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=1,
            item_type="temp_role", role_id=952)
        person.roles = [role_stub(952)]
        await self._expire(granted["entry_key"])
        await TO.cleanup_expired_holdings(bot)
        person.remove_roles.assert_not_awaited()

    async def test_guild_managed_role_is_never_stripped(self):
        # seed_member() defaults to Prestige tier=3; tier 3's role is protected
        # for this member, while a different tier's role (tier 1) is not.
        execute("INSERT OR REPLACE INTO prestige_roles "
                "(guild_id, tier, role_id) VALUES (?,?,?)", (GUILD, 3, 953))
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(953))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Tier", duration_hours=1,
            item_type="temp_role", role_id=953)
        person.roles = [role_stub(953)]
        await self._expire(granted["entry_key"])
        await TO.cleanup_expired_holdings(bot)
        person.remove_roles.assert_not_awaited()
        self.assertIsNone(
            await get_equipped(GUILD, USER),
            "expiring the timed holding must not leave a dangling equipped_roles.entry_key")

    async def test_discord_failure_keeps_the_record_as_the_retry_marker(self):
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(954))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=1,
            item_type="temp_role", role_id=954)
        person.roles = [role_stub(954)]
        person.remove_roles = AsyncMock(side_effect=RuntimeError("rate limited"))
        await self._expire(granted["entry_key"])

        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 0, str(stats))
        self.assertEqual(stats["kept"], 1, str(stats))
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)],
                         "the row is the only marker that the removal is pending")

        # A later pass, with Discord healthy again, finishes the job.
        person.remove_roles = AsyncMock()
        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 1, str(stats))
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

    async def test_equipped_slot_is_cleared_with_the_holding(self):
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(955))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=1,
            item_type="temp_role", role_id=955)
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         granted["entry_key"])
        person.roles = [role_stub(955)]
        await self._expire(granted["entry_key"])
        await TO.cleanup_expired_holdings(bot)
        self.assertIsNone(await get_equipped(GUILD, USER))

    async def test_cleanup_is_safe_to_run_twice(self):
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(956))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=1,
            item_type="temp_role", role_id=956)
        person.roles = [role_stub(956)]
        await self._expire(granted["entry_key"])
        await TO.cleanup_expired_holdings(bot)
        second = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(second, {"removed": 0, "kept": 0, "errors": []})


# ── 4. entry-key targeted equipping ─────────────────────────────────────────

class EquipIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        purge_timed()

    async def test_equipping_a_timed_entry_records_that_entry(self):
        from utils.inventory import give_item
        await give_item(GUILD, USER, "Flame", 1, item_type="role",
                        metadata={"role_id": 700})
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(701))
        timed = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=4,
            item_type="temp_role", role_id=701)
        person.roles = [role_stub(701)]

        result = await equip_role(bot, GUILD, USER, "Flame",
                                  entry_key=timed["entry_key"])
        self.assertTrue(result["success"], str(result))
        current = await get_equipped(GUILD, USER)
        self.assertEqual(current["entry_key"], timed["entry_key"])
        self.assertEqual(current["role_id"], 701)

    async def test_ambiguous_name_refuses_instead_of_guessing(self):
        from utils.inventory import give_item
        await give_item(GUILD, USER, "Flame", 1, item_type="role",
                        metadata={"role_id": 700})
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(701))
        await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Flame", duration_hours=4,
            item_type="temp_role", role_id=701)
        before = await get_equipped(GUILD, USER)
        result = await equip_role(bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"])
        self.assertIn("more than one", result["error"])
        self.assertEqual(await get_equipped(GUILD, USER), before,
                         "a refused equip must not move the equipped slot")


# ── 5. purchase saga ────────────────────────────────────────────────────────

class PurchaseSagaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=9000)
        purge_timed()
        execute("DELETE FROM purchase_history")
        execute("DELETE FROM shop_purchase_operations")
        execute("DELETE FROM transaction_ledger")

    def _ops(self):
        return rows("SELECT op_id, status, charged, refunded, stock_reserved,"
                    " stock_returned, amount FROM shop_purchase_operations")

    def _history(self):
        return rows("SELECT item_name, price_paid FROM purchase_history")

    async def test_successful_timed_purchase_records_one_finalized_receipt(self):
        listing = shop_item("custom", price=1000, name="Cape",
                            duration_hours=6)
        before = rows("SELECT balance FROM economy")[0][0]
        itx, person, bot, _ = purchase_ctx()
        await process_purchase(itx, listing)

        self.assertEqual(self._history(), [("Cape", 1000)])
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before - 1000)
        statuses = [row[1] for row in self._ops()]
        self.assertEqual(statuses, [TO.PURCHASE_DELIVERED])
        holding = (await TO.get_timed_holdings(GUILD, USER))[0]
        self.assertEqual(
            rows("SELECT expires_at FROM purchase_history")[0][0],
            holding["expires_at"],
            "receipt expiry must be the one delivery actually created")
        self.assertIn("Temporary ownership", reply_text(itx))

    async def test_insufficient_balance_refunds_the_stock_claim(self):
        listing = shop_item("title", price=999999, name="Crown", stock=1)
        itx, person, bot, _ = purchase_ctx()
        await process_purchase(itx, listing)
        self.assertEqual(self._history(), [])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"),
                         [(1,)], "the reserved unit must be given back")
        self.assertEqual(self._ops()[0][1], TO.PURCHASE_FAILED)
        self.assertIn("You need", reply_text(itx))

    async def test_out_of_stock_fails_without_charging_or_answering_success(self):
        listing = shop_item("title", price=100, name="Sold", stock=1, current=0)
        itx, person, bot, _ = purchase_ctx()
        await process_purchase(itx, listing)
        self.assertEqual(self._ops()[0][1], TO.PURCHASE_FAILED)
        self.assertIn("out of stock", reply_text(itx))
        self.assertNotIn("Purchase successful", reply_text(itx))
        self.assertEqual(self._history(), [])

    async def test_delivery_failure_refunds_money_and_stock_once(self):
        # A colour whose Discord role no longer exists: the charge went through,
        # the delivery cannot.
        listing = shop_item("color", price=750, name="Ghost", role_id=4321,
                            duration_hours=5, stock=1)
        itx, person, bot, _ = purchase_ctx(granted_role=None)
        before = rows("SELECT balance FROM economy")[0][0]
        await process_purchase(itx, listing)

        self.assertEqual(self._history(), [], "no receipt for a failed delivery")
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before,
                         "the payment must be returned")
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(1,)],
                         "the stock claim must be returned")
        self.assertEqual(self._ops()[0][1], TO.PURCHASE_FAILED)
        self.assertIn("Purchase not completed", reply_text(itx))
        self.assertNotIn("Purchase successful", reply_text(itx))
        refunds = rows("SELECT amount FROM transaction_ledger "
                       "WHERE source='shop_refund'")
        self.assertEqual(len(refunds), 1, "exactly one refund ledger row")

    async def test_uncertain_delivery_is_parked_and_not_refunded(self):
        # A role-backed product: the Discord role was applied, the ownership
        # row was not saved. Refunding here would hand out a free Color.
        listing = shop_item("color", price=500, name="Fog", role_id=611,
                            duration_hours=2)
        itx, person, bot, _ = purchase_ctx(granted_role=role_stub(611, "Fog"))
        before = rows("SELECT balance FROM economy")[0][0]
        with patch.object(TO, "grant_timed_holding",
                          AsyncMock(side_effect=RuntimeError("db gone"))):
            await process_purchase(itx, listing)

        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before - 500,
                         "an uncertain delivery must NOT be refunded")
        self.assertEqual(self._ops()[0][1], TO.PURCHASE_UNCERTAIN)
        self.assertIn("needs a check", reply_text(itx))
        self.assertEqual(self._history(), [])

    async def test_role_added_but_row_lost_is_uncertain(self):
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(610))
        with patch.object(TO, "grant_timed_holding",
                          AsyncMock(side_effect=RuntimeError("db gone"))):
            granted = await TO.grant_timed_product(
                bot, GUILD, USER, item_name="Flame", duration_hours=1,
                item_type="temp_role", role_id=610)
        self.assertFalse(granted["success"])
        self.assertTrue(granted["uncertain"],
                        "the role landed on the member, so a refund would be free")
        person.add_roles.assert_awaited()

    async def test_compensation_is_exactly_once_under_retry(self):
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="Thing", currency="balance",
            amount=400)
        await TO.update_purchase(op_id, charged=1, stock_reserved=1)
        first = await TO.claim_purchase_transition(
            op_id, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_FAILED, detail="boom")
        second = await TO.claim_purchase_transition(
            op_id, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_FAILED, detail="boom")
        self.assertTrue(first)
        self.assertFalse(second, "only one caller may win the transition")

        before = rows("SELECT balance FROM economy")[0][0]
        await TO.refund_purchase(op_id)
        once = rows("SELECT balance FROM economy")[0][0]
        await TO.refund_purchase(op_id)
        twice = rows("SELECT balance FROM economy")[0][0]
        self.assertEqual(once, before + 400)
        self.assertEqual(twice, once, "a second refund pass must credit nothing")

    async def test_reconciliation_finishes_an_interrupted_compensation(self):
        listing = shop_item("title", price=300, name="Half", stock=1)
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="Half",
            currency="balance", amount=300)
        # Simulates: charged, stock taken, then the process died mid-refund.
        await TO.update_purchase(op_id, charged=1, stock_reserved=1,
                                 refunded=1, status=TO.PURCHASE_FAILED)
        execute("UPDATE shop_items SET current_stock=0 WHERE id=?", (listing,))
        stats = await TO.reconcile_purchases()
        self.assertGreaterEqual(stats["completed"], 1, str(stats))
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(1,)])

    async def test_stale_pending_purchase_is_parked_not_refunded(self):
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="Old", currency="balance",
            amount=50)
        execute("UPDATE shop_purchase_operations SET created_at=datetime('now','-2 hours') "
                "WHERE op_id=?", (op_id,))
        before = rows("SELECT balance FROM economy")[0][0]
        stats = await TO.reconcile_purchases(older_than_minutes=15)
        self.assertEqual(stats["uncertain"], 1)
        op = await TO.get_purchase(op_id)
        self.assertEqual(op["status"], TO.PURCHASE_UNCERTAIN)
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before)


# ── 6. shop type model ──────────────────────────────────────────────────────

class ShopTypeModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        purge_timed()

    def test_public_types_are_the_five_ui_types(self):
        from utils.shop_validation import PUBLIC_SHOP_TYPES
        self.assertEqual(tuple(PUBLIC_SHOP_TYPES),
                         ("custom", "color", "potion", "title", "prestige"))

    def test_color_requires_a_role_and_normalizes_zero_duration(self):
        with self.assertRaises(ShopValidationError):
            shop_input({"name": "C", "type": "color", "price": 10,
                        "duration_hours": 5})
        data = shop_input({"name": "C", "type": "color", "price": 10,
                           "role_id": 5, "duration_hours": 0})
        self.assertIsNone(data.get("duration_hours"),
                          "a zero Duration is permanent, not an instant expiry")

    def test_prestige_and_title_cannot_carry_a_duration(self):
        with self.assertRaises(ShopValidationError):
            shop_input({"name": "P", "type": "prestige", "price": 10,
                        "duration_hours": 5, "prestige_tier": 2})
        with self.assertRaises(ShopValidationError):
            shop_input({"name": "T", "type": "title", "price": 10,
                        "duration_hours": 5})

    def test_legacy_xp_boost_is_still_accepted(self):
        data = shop_input({"name": "B", "type": "xp_boost", "price": 10,
                           "duration_hours": 5, "xp_boost_multiplier": 2})
        self.assertEqual(data["type"], "xp_boost")


# ── 7. audit fixes A–F and uncertain purchase recovery ──────────────────────

class AuditFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=9000)
        purge_timed()
        execute("DELETE FROM purchase_history")
        execute("DELETE FROM shop_purchase_operations")
        execute("DELETE FROM transaction_ledger")
        execute("DELETE FROM level_reward_claims")
        execute("DELETE FROM reward_reversals")
        execute("DELETE FROM prestige_roles")
        execute("DELETE FROM boost_config")
        execute("DELETE FROM boost_color_roles")

    async def _expire(self, entry_key: str):
        past = (TO.utcnow() - timedelta(hours=2)).isoformat()
        execute("UPDATE timed_holdings SET expires_at=? WHERE id=?",
                (past, TO.entry_key_id(entry_key)))

    # ── Fix A: Atomic compensation / reservation / charge crash windows ──

    async def test_fix_a_refund_money_crash_before_journal_flag_rolls_back_credit(self):
        listing = shop_item("title", price=500, name="CrashMoney", stock=2)
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="CrashMoney",
            currency="balance", amount=500)
        await TO.update_purchase(
            op_id, charged=1, stock_reserved=1, status=TO.PURCHASE_FAILED)
        execute("UPDATE shop_items SET current_stock=1 WHERE id=?", (listing,))
        before = rows("SELECT balance FROM economy")[0][0]

        real_update = TO.update_purchase
        calls = {"refund_update": 0}

        async def flaky_update(oid, **kwargs):
            if kwargs.get("refunded") == 1 and calls["refund_update"] == 0:
                calls["refund_update"] += 1
                raise RuntimeError("injected crash before refunded=1 commits")
            return await real_update(oid, **kwargs)

        with patch.object(TO, "update_purchase", side_effect=flaky_update):
            first = await TO.refund_purchase(op_id)

        self.assertFalse(first["complete"])
        self.assertEqual(
            rows("SELECT balance FROM economy")[0][0], before,
            "injected failure before refunded=1 must roll back safe_credit")
        op_mid = await TO.get_purchase(op_id)
        self.assertEqual(op_mid["refunded"], 0)
        self.assertEqual(op_mid["stock_returned"], 1)
        self.assertEqual(rows("SELECT current_stock FROM shop_items WHERE id=?",
                              (listing,)), [(2,)])

        # Retry completes the money half once without re-restoring stock.
        second = await TO.refund_purchase(op_id)
        self.assertTrue(second["complete"], str(second))
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before + 500)
        self.assertEqual(rows("SELECT current_stock FROM shop_items WHERE id=?",
                              (listing,)), [(2,)])

    async def test_fix_a_restore_stock_crash_before_journal_flag_rolls_back_stock(self):
        listing = shop_item("title", price=500, name="CrashStock", stock=3)
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="CrashStock",
            currency="balance", amount=500)
        await TO.update_purchase(
            op_id, charged=1, stock_reserved=1, status=TO.PURCHASE_FAILED)
        execute("UPDATE shop_items SET current_stock=2 WHERE id=?", (listing,))
        before = rows("SELECT balance FROM economy")[0][0]

        real_update = TO.update_purchase
        calls = {"stock_update": 0}

        async def flaky_update(oid, **kwargs):
            if kwargs.get("stock_returned") == 1 and calls["stock_update"] == 0:
                calls["stock_update"] += 1
                raise RuntimeError("injected crash before stock_returned=1 commits")
            return await real_update(oid, **kwargs)

        with patch.object(TO, "update_purchase", side_effect=flaky_update):
            first = await TO.refund_purchase(op_id)

        self.assertFalse(first["complete"])
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before + 500,
                         "money refund committed in its own atomic transaction")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(2,)],
            "stock increment must roll back when stock_returned=1 fails")

        second = await TO.refund_purchase(op_id)
        self.assertTrue(second["complete"], str(second))
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before + 500,
                         "retry must not refund money a second time")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(3,)],
            "retry restores stock exactly once")

    async def test_fix_a_stock_reservation_and_charge_are_atomic_with_journal_flags(self):
        import cogs.shop as shop_cog
        listing = shop_item("title", price=400, name="AtomicBuy", stock=2)
        before_bal = rows("SELECT balance FROM economy")[0][0]
        itx, _, _, _ = purchase_ctx()

        real_update = shop_cog.update_purchase

        async def fail_on_stock_flag(oid, **kwargs):
            if kwargs.get("stock_reserved") == 1:
                raise RuntimeError("crash before stock_reserved=1")
            return await real_update(oid, **kwargs)

        with patch.object(shop_cog, "update_purchase", side_effect=fail_on_stock_flag):
            with self.assertRaises(RuntimeError):
                await process_purchase(itx, listing)

        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(2,)],
            "stock decrement must roll back if stock_reserved=1 fails")
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before_bal)

        async def fail_on_charge_flag(oid, **kwargs):
            if kwargs.get("charged") == 1:
                raise RuntimeError("crash before charged=1")
            return await real_update(oid, **kwargs)

        with patch.object(shop_cog, "update_purchase", side_effect=fail_on_charge_flag):
            with self.assertRaises(RuntimeError):
                await process_purchase(itx, listing)

        self.assertEqual(
            rows("SELECT balance FROM economy")[0][0], before_bal,
            "balance debit must roll back if charged=1 fails")

    # ── Fix B: Permanent Level role claims & legacy temp_role rows ──

    async def test_fix_b_fulfilled_level_role_claim_prevents_expiry_strip(self):
        role_obj = role_stub(960, "LevelHero")
        _, person, bot, _ = purchase_ctx(granted_role=role_obj)
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, created_at)
            VALUES (?, ?, 10, 'role', '10', ?, 'fulfilled', 'xp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 960}), TO.iso()))

        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="HeroGlow", duration_hours=1,
            item_type="temp_role", role_id=960)
        person.roles = [role_obj]
        await self._expire(granted["entry_key"])

        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 1)
        person.remove_roles.assert_not_awaited()
        self.assertIsNone(
            await get_equipped(GUILD, USER),
            "expired holding slot is cleared while Level role stays on member")

    async def test_fix_b_booster_level_claim_requires_active_boost_and_respects_exclusivity(self):
        role_low = role_stub(961, "Lvl5Role")
        role_high = role_stub(962, "Lvl20Role")
        role_boost = role_stub(963, "BoosterLvlRole")
        _, person, bot, _ = purchase_ctx(granted_role=role_boost)

        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, created_at)
            VALUES (?, ?, 15, 'role', 'booster:15', ?, 'fulfilled', 'xp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 963}), TO.iso()))

        # Non-booster: booster level claim does NOT entitle the role.
        person.premium_since = None
        self.assertIsNone(await TO.role_still_entitled(
            GUILD, USER, 963, bot=bot, member=person))

        # Active booster: booster level claim DOES entitle the role.
        person.premium_since = TO.utcnow()
        self.assertIsNotNone(await TO.role_still_entitled(
            GUILD, USER, 963, bot=bot, member=person))

        # Exclusive progression (`remove_old_reward_role=1`): lower level (5)
        # is superseded by fulfilled higher level (20).
        execute("""
            INSERT INTO leveling_config (guild_id, remove_old_reward_role)
            VALUES (?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET remove_old_reward_role=1
        """, (GUILD,))
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, created_at)
            VALUES
                (?, ?, 5, 'role', '5', ?, 'fulfilled', 'xp', ?),
                (?, ?, 20, 'role', '20', ?, 'fulfilled', 'xp', ?)
        """, (GUILD, USER, json.dumps({"role_id": role_low.id}), TO.iso(),
              GUILD, USER, json.dumps({"role_id": role_high.id}), TO.iso()))

        self.assertIsNone(
            await TO.role_still_entitled(
                GUILD, USER, role_low.id, bot=bot, member=person),
            "superseded lower-level role must not block expiry removal")
        self.assertIsNotNone(
            await TO.role_still_entitled(
                GUILD, USER, role_high.id, bot=bot, member=person),
            "highest fulfilled level role must remain entitled")

    async def test_fix_b_stale_legacy_temp_role_inventory_does_not_block_expiry(self):
        from utils.inventory import give_item
        role_obj = role_stub(964, "LegacyTemp")
        _, person, bot, _ = purchase_ctx(granted_role=role_obj)
        past = (TO.utcnow() - timedelta(hours=5)).isoformat()
        await give_item(
            GUILD, USER, "LegacyTemp", 1, item_type="temp_role",
            source="shop", metadata={"role_id": 964, "expires_at": past})
        execute("""
            INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at, source)
            VALUES (?, ?, 964, ?, 'shop')
        """, (GUILD, USER, past))

        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="FreshTemp", duration_hours=1,
            item_type="temp_role", role_id=964)
        person.roles = [role_obj]
        await self._expire(granted["entry_key"])

        self.assertIsNone(
            await TO.role_still_entitled(
                GUILD, USER, 964, exclude_entry_key=granted["entry_key"],
                bot=bot, member=person),
            "expired legacy temp_role inventory row must not keep role alive forever")

    # ── Fix C: Timed Color equip swap on Discord ──

    async def test_fix_c_timed_color_grant_swaps_out_previous_equipped_role(self):
        from utils.inventory import give_item, get_inventory
        old_role = role_stub(970, "OldCrimson")
        new_role = role_stub(971, "NewAzure")
        _, person, bot, guild = purchase_ctx(granted_role=new_role)
        roles_map = {970: old_role, 971: new_role}
        guild.get_role = lambda rid: roles_map.get(int(rid))

        await give_item(GUILD, USER, "OldCrimson", 1, item_type="role",
                        metadata={"role_id": 970, "category": "color"})
        person.roles = [old_role]
        eq_res = await equip_role(bot, GUILD, USER, "OldCrimson")
        self.assertTrue(eq_res["success"], str(eq_res))

        async def _add_roles(r, reason=None):
            if r not in person.roles:
                person.roles.append(r)

        async def _remove_roles(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.add_roles = AsyncMock(side_effect=_add_roles)
        person.remove_roles = AsyncMock(side_effect=_remove_roles)

        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="NewAzure", duration_hours=4,
            item_type="temp_role", category="color", role_id=971)
        self.assertTrue(granted["success"], str(granted))

        person.add_roles.assert_awaited_once()
        person.remove_roles.assert_awaited_once()
        self.assertEqual([r.id for r in person.roles], [971],
                         "old Color role removed from Discord, new Color worn")
        eq_now = await get_equipped(GUILD, USER)
        self.assertEqual(eq_now["entry_key"], granted["entry_key"])
        self.assertEqual(eq_now["role_id"], 971)

        inv = await get_inventory(GUILD, USER)
        names = [it["item_name"] for it in inv]
        self.assertIn("OldCrimson", names,
                      "swapping out the old Color must keep its ownership")
        self.assertIn("NewAzure", names)

    async def test_fix_c_timed_color_grant_keeps_same_role_and_protected_role(self):
        role_same = role_stub(972, "SameEmerald")
        role_next = role_stub(973, "NextGold")
        _, person, bot, guild = purchase_ctx(granted_role=role_same)
        roles_map = {972: role_same, 973: role_next}
        guild.get_role = lambda rid: roles_map.get(int(rid))
        person.roles = [role_same]

        first = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="SameEmerald", duration_hours=2,
            item_type="temp_role", category="color", role_id=972)
        second = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="SameEmerald", duration_hours=6,
            item_type="temp_role", category="color", role_id=972)
        self.assertTrue(first["success"] and second["success"])
        person.remove_roles.assert_not_awaited()
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         second["entry_key"])

        # Now make role 972 a fulfilled Level role claim and grant role 973:
        # role 972 must NOT be removed from Discord during the swap!
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, created_at)
            VALUES (?, ?, 10, 'role', '10', ?, 'fulfilled', 'xp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 972}), TO.iso()))
        third = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="NextGold", duration_hours=4,
            item_type="temp_role", category="color", role_id=973)
        self.assertTrue(third["success"])
        person.remove_roles.assert_not_awaited()
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         third["entry_key"])

    # ── Fix D: Timezone-safe & malformed expiry cleanup selection ──

    async def test_fix_d_expired_holdings_selects_malformed_and_offset_timestamps(self):
        role_a = role_stub(974, "MalformedRole")
        role_b = role_stub(975, "OffsetRole")
        _, person, bot, guild = purchase_ctx(granted_role=role_a)
        roles_map = {974: role_a, 975: role_b}
        guild.get_role = lambda rid: roles_map.get(int(rid))
        person.roles = [role_a, role_b]

        h1 = await TO.grant_timed_holding(
            GUILD, USER, "BadDate", expires_at="not-a-timestamp",
            item_type="temp_role", role_id=974)
        # 1 hour ago in UTC, expressed in +05:00 (lexicographically > UTC now!)
        plus5 = timezone(timedelta(hours=5))
        offset_past = (TO.utcnow() - timedelta(hours=1)).astimezone(plus5).isoformat()
        h2 = await TO.grant_timed_holding(
            GUILD, USER, "OffsetPast", expires_at=offset_past,
            item_type="temp_role", role_id=975)

        exp_keys = {h["entry_key"] for h in await TO.expired_holdings()}
        self.assertIn(f"h:{h1}", exp_keys)
        self.assertIn(f"h:{h2}", exp_keys)

        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 2, str(stats))
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

    # ── Fix E: Per-member Prestige/Booster checks & equip repointing ──

    async def test_fix_e_per_member_prestige_and_booster_entitlements(self):
        # Member is at Prestige tier 3 (from seed_member).
        execute("INSERT INTO prestige_roles (guild_id, tier, role_id) VALUES "
                "(?, 1, 981), (?, 3, 983)", (GUILD, GUILD))
        execute("""
            INSERT OR REPLACE INTO boost_config
                (guild_id, enabled, boost1_role_id, boost2_role_id)
            VALUES (?, 1, 984, 985)
        """, (GUILD,))
        execute("""
            INSERT INTO boost_color_roles (guild_id, role_id, role_name, requires_boost_level)
            VALUES (?, 986, 'Pink', 1), (?, 987, 'Gold', 2)
        """, (GUILD, GUILD))

        _, person, bot, _ = purchase_ctx()
        person.premium_since = None
        person.premium_subscription_count = 0

        managed_non_booster = await TO.member_managed_role_ids(
            GUILD, USER, bot=bot, member=person)
        self.assertEqual(
            managed_non_booster, {983},
            "only effective Prestige tier 3 role is protected; tier 1 and booster roles are not")

        # 1x Booster: gains boost1_role_id (984) and level-1 boost color (986),
        # plus effective Prestige becomes VI (6) if VI is not configured or 3 if not.
        person.premium_since = TO.utcnow()
        person.premium_subscription_count = 1
        managed_b1 = await TO.member_managed_role_ids(
            GUILD, USER, bot=bot, member=person)
        self.assertIn(984, managed_b1)
        self.assertIn(986, managed_b1)
        self.assertNotIn(985, managed_b1, "1 boost must not protect 2-boost role")
        self.assertNotIn(987, managed_b1, "1 boost must not protect level-2 boost color")

        # 2x Booster: gains boost2_role_id (985) and level-2 boost color (987).
        person.premium_subscription_count = 2
        managed_b2 = await TO.member_managed_role_ids(
            GUILD, USER, bot=bot, member=person)
        self.assertIn(985, managed_b2)
        self.assertIn(987, managed_b2)

    async def test_fix_e_repoint_equipped_entry_key_when_expired_holding_retained_by_permanent(self):
        from utils.inventory import give_item, get_inventory
        role_obj = role_stub(988, "SharedColor")
        _, person, bot, _ = purchase_ctx(granted_role=role_obj)
        await give_item(GUILD, USER, "SharedColor", 1, item_type="role",
                        metadata={"role_id": 988, "category": "color"})
        perm_entry = [it for it in await get_inventory(GUILD, USER)
                      if not it["timed"]][0]

        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="SharedColor", duration_hours=1,
            item_type="temp_role", category="color", role_id=988)
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         granted["entry_key"])

        person.roles = [role_obj]
        await self._expire(granted["entry_key"])
        stats = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats["removed"], 1)
        person.remove_roles.assert_not_awaited()

        eq_after = await get_equipped(GUILD, USER)
        self.assertIsNotNone(eq_after)
        self.assertEqual(
            eq_after["entry_key"], perm_entry["entry_key"],
            "equipped_roles.entry_key must repoint to the surviving permanent entry")

    # ── Fix F: Permanent role partial delivery & pre-existing item safety ──

    async def test_fix_f_permanent_role_failure_restores_prior_inventory_and_marks_uncertain_when_role_added(self):
        from utils.inventory import give_item, get_inventory
        role_obj = role_stub(989, "PermShade")
        listing = shop_item("color", price=600, name="PermShade",
                            role_id=989, duration_hours=None, stock=2)
        itx, person, bot, _ = purchase_ctx(granted_role=role_obj)

        # Case 1: Member already owns a permanent item named "PermShade", and
        # Discord add_roles fails. Prior inventory row must be preserved intact
        # and purchase cleanly refunded (not uncertain).
        await give_item(GUILD, USER, "PermShade", 1, item_type="role",
                        source="event",
                        metadata={"role_id": 989, "category": "color",
                                  "custom_tag": "keep_me"})
        before_bal = rows("SELECT balance FROM economy")[0][0]
        person.add_roles = AsyncMock(side_effect=RuntimeError("discord 500"))
        await process_purchase(itx, listing)

        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before_bal,
                         "clean failure before role_added must refund")
        inv = [it for it in await get_inventory(GUILD, USER)
               if it["item_name"] == "PermShade"]
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0]["source"], "event",
                         "pre-existing permanent item must be restored, not deleted")
        self.assertEqual(inv[0]["metadata"].get("custom_tag"), "keep_me")

        # Case 2: Discord role IS added, but swap_and_record_equipped raises
        # after add_roles succeeded -> purchase must be marked UNCERTAIN and
        # NOT refunded!
        execute("DELETE FROM inventory_items WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        execute("DELETE FROM shop_purchase_operations")

        async def _add_ok(r, reason=None):
            if r not in person.roles:
                person.roles.append(r)

        person.add_roles = AsyncMock(side_effect=_add_ok)
        before_bal2 = rows("SELECT balance FROM economy")[0][0]
        import utils.equip_engine as eq_mod
        with patch.object(eq_mod, "swap_and_record_equipped",
                          AsyncMock(side_effect=RuntimeError("db locked after role add"))):
            await process_purchase(itx, listing)

        op = (await TO.list_purchase_operations(status=TO.PURCHASE_UNCERTAIN))[0]
        self.assertEqual(op["status"], TO.PURCHASE_UNCERTAIN)
        self.assertEqual(
            rows("SELECT balance FROM economy")[0][0], before_bal2 - 600,
            "must NOT refund when Discord role was already added")

    # ── Section 4: Uncertain purchase inspection & recovery CLI ──

    async def test_section_4_uncertain_purchase_inspection_and_operator_recovery(self):
        listing = shop_item("color", price=550, name="MysteryHue",
                            role_id=990, duration_hours=3, stock=2)
        itx, person, bot, _ = purchase_ctx(granted_role=role_stub(990, "MysteryHue"))
        before_bal = rows("SELECT balance FROM economy")[0][0]

        with patch.object(TO, "grant_timed_holding",
                          AsyncMock(side_effect=RuntimeError("db interrupted"))):
            await process_purchase(itx, listing)

        ops = await TO.list_purchase_operations(status=TO.PURCHASE_UNCERTAIN)
        self.assertEqual(len(ops), 1)
        op_id = ops[0]["op_id"]

        # 1. Read-only inspection mutates nothing.
        evidence = await TO.inspect_purchase_operation(op_id)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["operation"]["status"], TO.PURCHASE_UNCERTAIN)
        self.assertEqual(evidence["shop_item"]["id"], listing)
        self.assertEqual(evidence["member_wallet"]["balance"], before_bal - 550)
        rendered = TO.format_purchase_inspection(evidence)
        self.assertIn(op_id, rendered)
        self.assertIn("MysteryHue", rendered)

        # 2. Refuses without confirmed=True (or --yes on CLI).
        unconfirmed = await TO.resolve_purchase_operation(
            op_id, action="refund", confirmed=False)
        self.assertFalse(unconfirmed["success"])
        self.assertEqual((await TO.get_purchase(op_id))["status"],
                         TO.PURCHASE_UNCERTAIN)
        cli_dry = await TO._cli_async(["--op-id", op_id, "--resolve", "refund"])
        self.assertEqual(cli_dry, 2)
        self.assertEqual((await TO.get_purchase(op_id))["status"],
                         TO.PURCHASE_UNCERTAIN)

        # 3. Resolve via refund (--yes): refunds balance + restores stock once,
        # transitions to refunded, and preserves audit trail.
        cli_ok = await TO._cli_async([
            "--op-id", op_id, "--resolve", "refund", "--yes",
            "--note", "verified role removed manually"
        ])
        self.assertEqual(cli_ok, 0)
        resolved_op = await TO.get_purchase(op_id)
        self.assertEqual(resolved_op["status"], TO.PURCHASE_REFUNDED)
        self.assertEqual(resolved_op["refunded"], 1)
        self.assertEqual(resolved_op["stock_returned"], 1)
        self.assertIn("verified role removed manually", resolved_op["detail"])
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before_bal)
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(2,)])

        # 4. Re-running resolution on an already-refunded operation is refused.
        again = await TO.resolve_purchase_operation(
            op_id, action="refund", confirmed=True)
        self.assertFalse(again["success"])
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before_bal)

        # 5. Resolve a second uncertain operation as `delivered`.
        op2 = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="MysteryHue",
            currency="balance", amount=550)
        await TO.update_purchase(
            op2, charged=1, stock_reserved=1, status=TO.PURCHASE_UNCERTAIN,
            detail="manual check pending")
        res_deliv = await TO.resolve_purchase_operation(
            op2, action="delivered", confirmed=True,
            note="holding granted manually")
        self.assertTrue(res_deliv["success"], str(res_deliv))
        self.assertEqual((await TO.get_purchase(op2))["status"],
                         TO.PURCHASE_DELIVERED)
        self.assertIn("holding granted manually",
                      (await TO.get_purchase(op2))["detail"])

    async def test_fix_b_reversed_level_claim_does_not_block_expiry(self):
        from utils import level_ops as ops
        role_obj = role_stub(991, "ReversedLvlRole")
        _, person, bot, _ = purchase_ctx(granted_role=role_obj)
        claim_id = execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, created_at)
            VALUES (?, ?, 10, 'role', '10', ?, 'fulfilled', 'xp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 991}), TO.iso()))
        execute("""
            INSERT INTO reward_reversals
                (reversal_id, operation_id, guild_id, user_id, claim_id,
                 kind, quantity, status, reason, created_at)
            VALUES ('rev-991', 'op-991', ?, ?, ?, 'role',
                    1, ?, 'rolled back', ?)
        """, (GUILD, USER, claim_id, ops.STATUS_APPLIED, TO.iso()))

        self.assertIsNone(
            await TO.role_still_entitled(
                GUILD, USER, 991, bot=bot, member=person),
            "a reversed Level role claim must not keep the Discord role")

    async def test_section_4_cli_script_subprocess_end_to_end(self):
        import os
        import subprocess
        import sys
        from database import DB_PATH

        listing = shop_item("color", price=300, name="SubprocHue",
                            role_id=992, duration_hours=2, stock=1)
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="SubprocHue",
            currency="balance", amount=300)
        await TO.update_purchase(
            op_id, charged=1, stock_reserved=1, status=TO.PURCHASE_UNCERTAIN,
            detail="subprocess test")
        execute("UPDATE shop_items SET current_stock=0 WHERE id=?", (listing,))
        before_bal = rows("SELECT balance FROM economy")[0][0]

        env = dict(os.environ, DATABASE_PATH=DB_PATH)
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "scripts", "recover_shop_purchases.py")

        # 1. List uncertain operations (JSON)
        listed = subprocess.run(
            [sys.executable, script, "--json"],
            env=env, capture_output=True, text=True, check=True)
        payload = json.loads(listed.stdout)
        self.assertEqual(len(payload["operations"]), 1)
        self.assertEqual(payload["operations"][0]["op_id"], op_id)

        # 2. Dry-run without --yes exits with code 2 and does not mutate
        dry = subprocess.run(
            [sys.executable, script, "--op-id", op_id, "--resolve", "refund"],
            env=env, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 2)
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], before_bal)

        # 3. Resolve with --yes refunds once and exits 0
        resolved = subprocess.run(
            [sys.executable, script, "--op-id", op_id, "--resolve", "refund",
             "--yes", "--note", "cli resolved", "--json"],
            env=env, capture_output=True, text=True, check=True)
        res_json = json.loads(resolved.stdout)
        self.assertTrue(res_json["success"])
        self.assertEqual(res_json["status"], TO.PURCHASE_REFUNDED)
        self.assertEqual(rows("SELECT balance FROM economy")[0][0],
                         before_bal + 300)
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(1,)])

    async def test_adversarial_audit_transaction_and_ledger_boundaries(self):
        import aiosqlite
        from database import DB_PATH
        from utils.economy_safe import (
            safe_credit, safe_deduct, safe_decrement_stock,
        )
        import utils.ledger as ledger_mod

        listing = shop_item("color", price=400, name="BoundHue",
                            role_id=993, duration_hours=2, stock=3)
        start_bal = rows("SELECT balance FROM economy")[0][0]
        start_ledger = len(rows("SELECT id FROM transaction_ledger"))

        # 1. Caller-owned BEGIN IMMEDIATE rollback undoes safe_credit,
        # safe_deduct, safe_decrement_stock, restore_stock, log_transaction,
        # and update_purchase together.
        op_tx = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="BoundHue",
            currency="balance", amount=400)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            await safe_decrement_stock(listing, db=db)
            await safe_deduct(GUILD, USER, 400, currency="balance",
                              reason="tx test", source="shop", db=db)
            await safe_credit(GUILD, USER, 100, currency="balance",
                              reason="tx credit", source="shop", db=db)
            await TO.restore_stock(listing, db=db)
            await TO.update_purchase(
                op_tx, stock_reserved=1, charged=1, db=db)
            await db.execute("ROLLBACK")

        self.assertEqual(rows("SELECT balance FROM economy")[0][0], start_bal)
        self.assertEqual(len(rows("SELECT id FROM transaction_ledger")),
                         start_ledger)
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(3,)])
        op_after = await TO.get_purchase(op_tx)
        self.assertEqual(op_after["stock_reserved"], 0)
        self.assertEqual(op_after["charged"], 0)

        # 2. If log_transaction raises inside caller-owned `db=`, safe_deduct
        # propagates the failure so process_purchase rolls back the debit AND
        # releases reserved stock via refund_purchase.
        itx, _, _, _ = purchase_ctx(granted_role=role_stub(993, "BoundHue"))
        with patch.object(ledger_mod, "log_transaction",
                          AsyncMock(side_effect=RuntimeError("ledger disk full"))):
            with self.assertRaises(RuntimeError):
                await process_purchase(itx, listing)

        self.assertEqual(rows("SELECT balance FROM economy")[0][0], start_bal,
                         "ledger failure in caller-owned tx must roll back debit")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(3,)],
            "reserved stock must be released when charge tx fails after stock_reserved=1")
        latest_op = rows(
            "SELECT status, stock_reserved, charged, stock_returned "
            "FROM shop_purchase_operations WHERE op_id != ? "
            "ORDER BY rowid DESC LIMIT 1", (op_tx,))[0]
        self.assertEqual(latest_op, (TO.PURCHASE_FAILED, 1, 0, 1))

        # 3. Interruption after stock_returned=1 before operator finalizes
        # status=refunded: resolve_purchase_operation(action='refund') finalizes
        # status=refunded without double-crediting or double-restoring stock.
        op_half = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="BoundHue",
            currency="balance", amount=400)
        await TO.update_purchase(
            op_half, stock_reserved=1, charged=1,
            refunded=1, stock_returned=1, status=TO.PURCHASE_FAILED)
        retry_res = await TO.resolve_purchase_operation(
            op_half, action="refund", confirmed=True,
            note="finalize interrupted refund")
        self.assertTrue(retry_res["success"])
        self.assertEqual((await TO.get_purchase(op_half))["status"],
                         TO.PURCHASE_REFUNDED)
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], start_bal)
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(3,)])

    async def test_adversarial_audit_swap_order_legacy_null_key_and_db_failure(self):
        from utils.inventory import give_item
        from utils.equip_engine import unequip_role
        import utils.equip_engine as eq_mod

        r_old = role_stub(994, "LegacyFlame")
        r_new = role_stub(995, "AzureWave")
        _, person, bot, guild = purchase_ctx(granted_role=r_old)
        roles_map = {994: r_old, 995: r_new}
        guild.get_role = lambda rid: roles_map.get(int(rid))
        person.roles.append(r_old)

        # Seed two permanent role items and a legacy equipped_roles row with entry_key=NULL
        await give_item(GUILD, USER, "LegacyFlame", 1, item_type="role",
                        source="shop", metadata={"role_id": 994, "category": "color"})
        await give_item(GUILD, USER, "AzureWave", 1, item_type="role",
                        source="shop", metadata={"role_id": 995, "category": "color"})
        execute("""
            INSERT OR REPLACE INTO equipped_roles
                (guild_id, user_id, item_name, role_id, entry_key)
            VALUES (?, ?, 'LegacyFlame', 994, NULL)
        """, (GUILD, USER))

        inv = await get_inventory(GUILD, USER)
        flame_entry = next(it for it in inv if it["item_name"] == "LegacyFlame")
        azure_entry = next(it for it in inv if it["item_name"] == "AzureWave")

        # 1. Re-equipping the same permanent item upgrades legacy NULL entry_key
        # without removing the Discord role.
        call_order = []

        async def _track_add(r, reason=None):
            call_order.append(("add", r.id))
            if r not in person.roles:
                person.roles.append(r)

        async def _track_rem(r, reason=None):
            call_order.append(("remove", r.id))
            person.roles = [x for x in person.roles if x.id != r.id]

        person.add_roles = AsyncMock(side_effect=_track_add)
        person.remove_roles = AsyncMock(side_effect=_track_rem)

        res_same = await equip_role(
            bot, GUILD, USER, "LegacyFlame",
            entry_key=flame_entry["entry_key"])
        self.assertTrue(res_same["success"])
        self.assertEqual(call_order, [], "re-equipping already worn role must not add/remove")
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         flame_entry["entry_key"])

        # Reset back to legacy NULL entry_key to test DB failure & swap order
        execute("UPDATE equipped_roles SET entry_key = NULL WHERE guild_id=? AND user_id=?",
                (GUILD, USER))

        # 2. If record_equipped fails with a DB error after add_roles(new),
        # remove_roles(old) must NOT run (member never loses old_role!).
        with patch.object(eq_mod, "record_equipped",
                          AsyncMock(side_effect=RuntimeError("sqlite disk I/O error"))):
            res_dbfail = await equip_role(
                bot, GUILD, USER, "AzureWave",
                entry_key=azure_entry["entry_key"])
        self.assertFalse(res_dbfail["success"])
        self.assertIn(r_old, person.roles,
                      "old role must remain on Discord if DB write fails")
        self.assertEqual((await get_equipped(GUILD, USER))["role_id"], 994)

        # Remove r_new from person.roles to test clean swap call order
        if r_new in person.roles:
            person.roles.remove(r_new)
        call_order.clear()

        # 3. Clean swap from LegacyFlame (994, entry_key=NULL) -> AzureWave (995):
        # add_roles(995) MUST be called strictly BEFORE remove_roles(994).
        res_swap = await equip_role(
            bot, GUILD, USER, "AzureWave",
            entry_key=azure_entry["entry_key"])
        self.assertTrue(res_swap["success"])
        self.assertEqual(call_order, [("add", 995), ("remove", 994)],
                         "new role must be added before old role is removed")
        self.assertIn(r_new, person.roles)
        self.assertNotIn(r_old, person.roles)

        # 4. Legacy NULL entry_key row can be unequipped via permanent entry_key="i:<id>",
        # but refuses a timed holding entry_key="h:<id>".
        execute("""
            UPDATE equipped_roles
            SET item_name='AzureWave', role_id=995, entry_key=NULL
            WHERE guild_id=? AND user_id=?
        """, (GUILD, USER))
        refuse_timed = await unequip_role(
            bot, GUILD, USER, "AzureWave", entry_key="h:999")
        self.assertFalse(refuse_timed["success"])
        ok_legacy_unequip = await unequip_role(
            bot, GUILD, USER, "AzureWave", entry_key=azure_entry["entry_key"])
        self.assertTrue(ok_legacy_unequip["success"])
        self.assertIsNone(await get_equipped(GUILD, USER))

    async def test_adversarial_audit_cli_db_safety_and_note_requirement(self):
        import os
        import subprocess
        import sys
        from database import DB_PATH

        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "scripts", "recover_shop_purchases.py")

        # 1. Importing scripts/recover_shop_purchases.py has no side effects.
        import importlib.util
        spec = importlib.util.spec_from_file_location("recover_cli_mod", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.main))

        # 2. Running without --db and without DATABASE_PATH refuses with exit code 2.
        env_no_db = {k: v for k, v in os.environ.items() if k != "DATABASE_PATH"}
        no_db = subprocess.run(
            [sys.executable, script, "--json"],
            env=env_no_db, capture_output=True, text=True)
        self.assertEqual(no_db.returncode, 2)
        self.assertIn("explicit database target", no_db.stdout)

        # 3. Running with --db pointing to a nonexistent file refuses with exit code 2
        # and does not create the file.
        missing_path = DB_PATH + ".does_not_exist.sqlite"
        if os.path.exists(missing_path):
            os.remove(missing_path)
        missing_res = subprocess.run(
            [sys.executable, script, "--db", missing_path, "--json"],
            env=env_no_db, capture_output=True, text=True)
        self.assertEqual(missing_res.returncode, 2)
        self.assertFalse(os.path.exists(missing_path))

        # 4. --resolve --yes without --note refuses with exit code 2 and does not mutate.
        listing = shop_item("color", price=250, name="CliGuardHue",
                            role_id=996, duration_hours=2, stock=1)
        op_id = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="CliGuardHue",
            currency="balance", amount=250)
        await TO.update_purchase(
            op_id, charged=1, stock_reserved=1, status=TO.PURCHASE_UNCERTAIN,
            detail="awaiting note")
        no_note = subprocess.run(
            [sys.executable, script, "--db", DB_PATH, "--op-id", op_id,
             "--resolve", "delivered", "--yes", "--json"],
            env=env_no_db, capture_output=True, text=True)
        self.assertEqual(no_note.returncode, 2)
        self.assertEqual((await TO.get_purchase(op_id))["status"],
                         TO.PURCHASE_UNCERTAIN)

        # 5. --resolve delivered --yes --note succeeds with explicit --db (no env var).
        ok_deliv = subprocess.run(
            [sys.executable, script, "--db", DB_PATH, "--op-id", op_id,
             "--resolve", "delivered", "--yes",
             "--note", "verified Discord role present", "--json"],
            env=env_no_db, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(ok_deliv.stdout)["success"])
        self.assertEqual((await TO.get_purchase(op_id))["status"],
                         TO.PURCHASE_DELIVERED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
