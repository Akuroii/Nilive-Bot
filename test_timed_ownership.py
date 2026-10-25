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
        # Discord add_roles fails with a definite pre-application rejection
        # (403 Forbidden). Prior inventory row must be preserved intact and
        # purchase cleanly refunded (not uncertain).
        import discord
        await give_item(GUILD, USER, "PermShade", 1, item_type="role",
                        source="event",
                        metadata={"role_id": 989, "category": "color",
                                  "custom_tag": "keep_me"})
        before_bal = rows("SELECT balance FROM economy")[0][0]
        forbidden_403 = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50013, "message": "Missing Permissions"})
        person.add_roles = AsyncMock(side_effect=forbidden_403)
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

    async def test_finding_1_refund_vs_delivery_race_and_stale_snapshot_guard(self):
        import asyncio

        listing = shop_item("color", price=500, name="RaceHue",
                            role_id=997, duration_hours=2, stock=2)
        start_bal = rows("SELECT balance FROM economy")[0][0]

        # ── Subcase A: Refund commits refunded=1, then a concurrent/interleaved
        # `delivered` resolution (and direct `claim_purchase_transition` with a
        # stale snapshot) attempts to mark the operation `delivered` BEFORE
        # stock restoration or final status update runs.
        op1 = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="RaceHue",
            currency="balance", amount=500)
        await TO.update_purchase(
            op1, charged=1, stock_reserved=1, status=TO.PURCHASE_UNCERTAIN,
            detail="race subcase A")
        execute("UPDATE shop_items SET current_stock=1 WHERE id=?", (listing,))

        real_restore = TO.restore_stock
        race_results = {}

        async def _restore_with_interleaved_delivered(item_id, *, db=None):
            # At this exact point, refund_purchase's first BEGIN IMMEDIATE has
            # committed `refunded=1`, while `status` is still `uncertain` and
            # `stock_returned` is still `0`. Note that `db` here is the open
            # stock-restore transaction, so we test the race right BEFORE
            # `restore_stock` runs by releasing/testing via a separate hook.
            return await real_restore(item_id, db=db)

        real_refund_purchase = TO.refund_purchase

        async def _interleaved_refund(oid):
            # Run money refund step by temporarily failing stock restore, then
            # run concurrent delivered attempts while refunded=1 & status='uncertain',
            # then finish stock restore.
            calls = {"n": 0}

            async def _fail_once(item_id, *, db=None):
                if calls["n"] == 0:
                    calls["n"] += 1
                    raise RuntimeError("pause between money credit and stock restore")
                return await real_restore(item_id, db=db)

            with patch.object(TO, "restore_stock", side_effect=_fail_once):
                partial = await real_refund_purchase(oid)
            self.assertFalse(partial["complete"])
            mid_op = await TO.get_purchase(oid)
            self.assertEqual(mid_op["status"], TO.PURCHASE_UNCERTAIN)
            self.assertEqual(mid_op["refunded"], 1)
            self.assertEqual(mid_op["stock_returned"], 0)

            # Now attempt BOTH a high-level `resolve_purchase_operation(action='delivered')`
            # AND a low-level `claim_purchase_transition(..., to_status=PURCHASE_DELIVERED)`
            # simulating a caller that read `op` before `refunded=1` was set.
            race_results["resolve_delivered"] = await TO.resolve_purchase_operation(
                oid, action="delivered", confirmed=True,
                note="concurrent delivered after refund started")
            race_results["raw_claim_delivered"] = await TO.claim_purchase_transition(
                oid,
                from_statuses=(TO.PURCHASE_UNCERTAIN, TO.PURCHASE_FAILED),
                to_status=TO.PURCHASE_DELIVERED,
                detail="stale snapshot bypass attempt",
            )
            # Now complete the remaining half of refund_purchase:
            return await real_refund_purchase(oid)

        with patch.object(TO, "refund_purchase", side_effect=_interleaved_refund):
            res_refund = await TO.resolve_purchase_operation(
                op1, action="refund", confirmed=True,
                note="operator refund")

        self.assertFalse(
            race_results["resolve_delivered"]["success"],
            "delivered resolution must be rejected once refunded=1 is committed")
        self.assertFalse(
            race_results["raw_claim_delivered"],
            "SQL WHERE clause in claim_purchase_transition must block delivered when refunded=1")
        self.assertTrue(res_refund["success"], str(res_refund))
        final_op1 = await TO.get_purchase(op1)
        self.assertEqual(final_op1["status"], TO.PURCHASE_REFUNDED)
        self.assertEqual(final_op1["refunded"], 1)
        self.assertEqual(final_op1["stock_returned"], 1)
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], start_bal + 500)
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(2,)])

        # ── Subcase B: Also verify that `stock_returned=1` alone (e.g. Free item
        # with charged=0, stock_reserved=1) blocks `delivered` at the SQL level.
        op_free = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="RaceHue",
            currency="balance", amount=0)
        await TO.update_purchase(
            op_free, charged=0, stock_reserved=1, stock_returned=1,
            status=TO.PURCHASE_UNCERTAIN, detail="stock already returned")
        self.assertFalse(await TO.claim_purchase_transition(
            op_free,
            from_statuses=(TO.PURCHASE_UNCERTAIN, TO.PURCHASE_FAILED),
            to_status=TO.PURCHASE_DELIVERED,
            detail="must fail because stock_returned=1",
        ))
        deliv_free = await TO.resolve_purchase_operation(
            op_free, action="delivered", confirmed=True, note="must fail")
        self.assertFalse(deliv_free["success"])
        self.assertEqual((await TO.get_purchase(op_free))["status"],
                         TO.PURCHASE_UNCERTAIN)

        # ── Subcase C: Concurrent `delivered` vs `refund` where `delivered`
        # commits first — `refund` must fail without crediting money or stock.
        bal_before_c = rows("SELECT balance FROM economy")[0][0]
        execute("UPDATE shop_items SET current_stock=1 WHERE id=?", (listing,))
        op2 = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="RaceHue",
            currency="balance", amount=500)
        await TO.update_purchase(
            op2, charged=1, stock_reserved=1, status=TO.PURCHASE_UNCERTAIN,
            detail="race subcase C")

        async def _refund_after_delivered_wins(oid):
            # Delivered wins right before refund_purchase enters its BEGIN IMMEDIATE
            deliv_win = await TO.resolve_purchase_operation(
                oid, action="delivered", confirmed=True,
                note="delivered won first")
            self.assertTrue(deliv_win["success"])
            return await real_refund_purchase(oid)

        with patch.object(TO, "refund_purchase",
                          side_effect=_refund_after_delivered_wins):
            refund_lose = await TO.resolve_purchase_operation(
                op2, action="refund", confirmed=True,
                note="late refund attempt")

        self.assertFalse(refund_lose["success"],
                         "refund must fail when delivered won the race")
        final_op2 = await TO.get_purchase(op2)
        self.assertEqual(final_op2["status"], TO.PURCHASE_DELIVERED)
        self.assertEqual(final_op2["refunded"], 0)
        self.assertEqual(final_op2["stock_returned"], 0)
        self.assertEqual(rows("SELECT balance FROM economy")[0][0], bal_before_c,
                         "no money may be refunded when delivered won")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(1,)],
            "no stock may be restored when delivered won")

        # ── Subcase D: Overlapping concurrent `refund` vs `refund` via asyncio.gather
        # credits money once, restores stock once, and only one caller wins finalization.
        op3 = await TO.open_purchase(
            GUILD, USER, item_id=listing, item_name="RaceHue",
            currency="balance", amount=500)
        await TO.update_purchase(
            op3, charged=1, stock_reserved=1, status=TO.PURCHASE_UNCERTAIN,
            detail="race subcase D")
        r1, r2 = await asyncio.gather(
            TO.resolve_purchase_operation(
                op3, action="refund", confirmed=True, note="concurrent 1"),
            TO.resolve_purchase_operation(
                op3, action="refund", confirmed=True, note="concurrent 2"),
        )
        self.assertEqual(
            sorted([r1["success"], r2["success"]]), [False, True],
            "exactly one concurrent refund resolution may win finalization")
        final_op3 = await TO.get_purchase(op3)
        self.assertEqual(final_op3["status"], TO.PURCHASE_REFUNDED)
        self.assertEqual(final_op3["refunded"], 1)
        self.assertEqual(final_op3["stock_returned"], 1)
        self.assertEqual(
            rows("SELECT balance FROM economy")[0][0], bal_before_c + 500,
            "concurrent refunds must credit balance only once")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (listing,)),
            [(2,)],
            "concurrent refunds must restore stock only once")

    async def test_finding_2_entitlement_checks_fail_safely_and_recover(self):
        from utils.equip_engine import unequip_role

        r_plain = role_stub(998, "PlainTimedColor")
        r_shared = role_stub(999, "SharedLevelColor")
        _, person, bot, guild = purchase_ctx(granted_role=r_plain)
        roles_map = {998: r_plain, 999: r_shared}
        guild.get_role = lambda rid: roles_map.get(int(rid))

        async def _add_role(r, reason=None):
            if r not in person.roles:
                person.roles.append(r)

        async def _rem_role(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.add_roles = AsyncMock(side_effect=_add_role)
        person.remove_roles = AsyncMock(side_effect=_rem_role)

        # ── 1. Successful check with no independent entitlement -> normal removal
        g1 = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="PlainTimedColor", duration_hours=1,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=998)
        self.assertTrue(g1["success"])
        self.assertIn(r_plain, person.roles)
        await self._expire(g1["entry_key"])

        self.assertIsNone(
            await TO.role_still_entitled(
                GUILD, USER, 998, exclude_entry_key=g1["entry_key"],
                bot=bot, member=person))
        stats1 = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats1, {"removed": 1, "kept": 0, "errors": []})
        person.remove_roles.assert_awaited_once()
        self.assertNotIn(r_plain, person.roles)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

        # ── 2. Timed Color + permanent Level role claim sharing the same role_id (999)
        person.remove_roles.reset_mock()
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, created_at)
            VALUES (?, ?, 10, 'role', '10', ?, 'fulfilled', 'xp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 999}), TO.iso()))

        g2 = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="SharedLevelColor", duration_hours=1,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=999)
        self.assertTrue(g2["success"])
        self.assertIn(r_shared, person.roles)
        await self._expire(g2["entry_key"])

        # ── 3. Database/query failure during entitlement verification:
        # Break `level_reward_claims` in SQLite (simulating a failed query/missing
        # table) and verify:
        #   - `level_claim_role_entitled` and `role_still_entitled` raise
        #     `EntitlementCheckError` instead of returning None;
        #   - `cleanup_expired_holdings` does NOT remove the Discord role and
        #     does NOT delete the expired holding's retry marker;
        #   - `unequip_role` also refuses to remove the role while entitlement
        #     verification is failing.
        execute("ALTER TABLE level_reward_claims RENAME TO _broken_level_reward_claims")
        try:
            with self.assertRaises(TO.EntitlementCheckError):
                await TO.level_claim_role_entitled(
                    GUILD, USER, 999, bot=bot, member=person)
            with self.assertRaises(TO.EntitlementCheckError):
                await TO.role_still_entitled(
                    GUILD, USER, 999, exclude_entry_key=g2["entry_key"],
                    bot=bot, member=person)

            stats_fail = await TO.cleanup_expired_holdings(bot)
            self.assertEqual(stats_fail["removed"], 0)
            self.assertEqual(stats_fail["kept"], 1)
            self.assertEqual(len(stats_fail["errors"]), 1)
            self.assertIn("entitlement check failed", stats_fail["errors"][0])
            person.remove_roles.assert_not_awaited()
            self.assertIn(r_shared, person.roles,
                          "Discord role must stay when entitlement check fails")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)],
                "expired holding retry marker must stay when entitlement check fails")

            unequip_fail = await unequip_role(
                bot, GUILD, USER, "SharedLevelColor",
                entry_key=g2["entry_key"])
            self.assertFalse(unequip_fail["success"])
            person.remove_roles.assert_not_awaited()
            self.assertIn(r_shared, person.roles)
        finally:
            execute("ALTER TABLE _broken_level_reward_claims RENAME TO level_reward_claims")

        # Also verify that DB failures in `inventory_items` and `prestige_roles`
        # similarly raise `EntitlementCheckError` and keep the role + retry marker.
        execute("ALTER TABLE inventory_items RENAME TO _broken_inventory_items")
        try:
            with self.assertRaises(TO.EntitlementCheckError):
                await TO.role_still_entitled(
                    GUILD, USER, 999, exclude_entry_key=g2["entry_key"],
                    bot=bot, member=person)
            stats_inv_fail = await TO.cleanup_expired_holdings(bot)
            self.assertEqual(stats_inv_fail["removed"], 0)
            self.assertEqual(stats_inv_fail["kept"], 1)
            self.assertEqual(len(stats_inv_fail["errors"]), 1)
            person.remove_roles.assert_not_awaited()
        finally:
            execute("ALTER TABLE _broken_inventory_items RENAME TO inventory_items")

        execute("ALTER TABLE prestige_roles RENAME TO _broken_prestige_roles")
        try:
            with self.assertRaises(TO.EntitlementCheckError):
                await TO.member_managed_role_ids(
                    GUILD, USER, bot=bot, member=person)
        finally:
            execute("ALTER TABLE _broken_prestige_roles RENAME TO prestige_roles")

        # ── 4. Recovery on a later cleanup pass after the query failure is removed:
        # 4a: With the fulfilled Level role claim present, cleanup now succeeds,
        # retires the expired timed holding, reconciles `equipped_roles`, and
        # retains the Discord role on the member!
        stats_recovered_entitled = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(
            stats_recovered_entitled, {"removed": 1, "kept": 0, "errors": []})
        person.remove_roles.assert_not_awaited()
        self.assertIn(r_shared, person.roles,
                      "permanent Level role claim keeps the Discord role")
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertIsNone(await get_equipped(GUILD, USER))

        # 4b: When no independent entitlement exists and a transient query
        # failure is resolved on the next pass, the role and retry marker are
        # kept during the failure and cleanly removed on the recovery pass.
        execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        g3 = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="SharedLevelColor", duration_hours=1,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=999)
        await self._expire(g3["entry_key"])

        execute("ALTER TABLE level_reward_claims RENAME TO _broken_level_reward_claims")
        try:
            stats_transient = await TO.cleanup_expired_holdings(bot)
            self.assertEqual(stats_transient["removed"], 0)
            self.assertEqual(stats_transient["kept"], 1)
            self.assertEqual(len(stats_transient["errors"]), 1)
            person.remove_roles.assert_not_awaited()
            self.assertIn(r_shared, person.roles)
            self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(1,)])
        finally:
            execute("ALTER TABLE _broken_level_reward_claims RENAME TO level_reward_claims")

        stats_final = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(stats_final, {"removed": 1, "kept": 0, "errors": []})
        person.remove_roles.assert_awaited_once()
        self.assertNotIn(r_shared, person.roles)
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])

    async def test_p1_paused_process_purchase_cannot_overwrite_operator_refund(self):
        """P1 regression: a paused `process_purchase` that resumes after an
        operator refund must not overwrite `status='refunded'` into `delivered`
        or leave a `purchase_history` receipt.
        """
        import asyncio
        import cogs.shop as shop_mod

        item_id = shop_item(
            kind="custom", name="P1 Race Relic", price=300, stock=2,
            duration_hours=24)
        inter, _, _, _ = purchase_ctx()

        entered_delivery = asyncio.Event()
        release_delivery = asyncio.Event()
        real_deliver = shop_mod._deliver_purchase

        async def _slow_deliver(*args, **kwargs):
            entered_delivery.set()
            await release_delivery.wait()
            return await real_deliver(*args, **kwargs)

        with patch.object(shop_mod, "_deliver_purchase", side_effect=_slow_deliver):
            purchase_task = asyncio.create_task(
                process_purchase(inter, item_id))
            await asyncio.wait_for(entered_delivery.wait(), timeout=5.0)

            # While delivery is in flight, charge and stock reservation are committed.
            op_rows = rows("SELECT op_id, status, charged, stock_reserved "
                           "FROM shop_purchase_operations")
            self.assertEqual(len(op_rows), 1)
            op_id, init_status, init_charged, init_reserved = op_rows[0]
            self.assertEqual(init_status, TO.PURCHASE_PENDING)
            self.assertEqual((init_charged, init_reserved), (1, 1))
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)), [(8700,)])
            self.assertEqual(
                rows("SELECT current_stock FROM shop_items WHERE id=?",
                     (item_id,)), [(1,)])

            # Timeout reconciliation parks the stale pending operation as `uncertain`,
            # and an operator resolves it via `refund` before `_deliver_purchase` finishes.
            execute("UPDATE shop_purchase_operations "
                    "SET created_at = datetime('now', '-30 minutes') "
                    "WHERE op_id=?", (op_id,))
            rec = await TO.reconcile_purchases(older_than_minutes=15)
            self.assertEqual(rec["uncertain"], 1)
            self.assertEqual((await TO.get_purchase(op_id))["status"],
                             TO.PURCHASE_UNCERTAIN)

            resolved = await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True,
                note="operator refunded while delivery was stalled")
            self.assertTrue(resolved["success"], str(resolved))
            self.assertEqual(resolved["status"], TO.PURCHASE_REFUNDED)
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)), [(9000,)])
            self.assertEqual(
                rows("SELECT current_stock FROM shop_items WHERE id=?",
                     (item_id,)), [(2,)])

            # Release the stalled delivery coroutine so `process_purchase` tries
            # to write `purchase_history` and mark the operation `delivered`.
            release_delivery.set()
            await asyncio.wait_for(purchase_task, timeout=5.0)

        # Direct database-level guards must also reject any attempt to force
        # `status='delivered'` or `status='uncertain'` on the refunded operation.
        self.assertFalse(await TO.update_purchase(
            op_id, status=TO.PURCHASE_DELIVERED, history_id=999,
            detail="stale direct update"))
        self.assertFalse(await TO.update_purchase(
            op_id, status=TO.PURCHASE_UNCERTAIN, detail="stale uncertain update"))
        self.assertFalse(await TO.claim_purchase_transition(
            op_id,
            from_statuses=(TO.PURCHASE_PENDING, TO.PURCHASE_UNCERTAIN,
                           TO.PURCHASE_REFUNDED),
            to_status=TO.PURCHASE_DELIVERED,
            detail="stale transition"))

        final_op = await TO.get_purchase(op_id)
        self.assertEqual(final_op["status"], TO.PURCHASE_REFUNDED)
        self.assertEqual(final_op["refunded"], 1)
        self.assertEqual(final_op["stock_returned"], 1)
        self.assertIsNone(final_op["history_id"])
        self.assertEqual(rows("SELECT COUNT(*) FROM purchase_history"), [(0,)],
                         "refunded purchase must not leave a purchase_history receipt")
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)],
                         "late timed holding for an already-refunded purchase must be cleaned up")

        text = reply_text(inter)
        self.assertIn("Purchase needs a check", text)
        self.assertNotIn("Purchase successful", text)

        # Subsequent refund/reconcile passes must be complete no-ops.
        again = await TO.refund_purchase(op_id)
        self.assertTrue(again["complete"])
        self.assertEqual(
            rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER)), [(9000,)])
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?",
                 (item_id,)), [(2,)])

    async def test_p2_ambiguous_vs_definite_discord_role_grant_in_helper_and_shop(self):
        """P2 regression: ambiguous `member.add_roles()` outcomes (timeout/transport
        or role observed on member) must return `uncertain=True` and never
        auto-refund, whereas definite 4xx rejections (`discord.Forbidden`) before
        role application follow the known-failure compensation path.
        """
        import asyncio
        import discord
        from utils.reward_engine import give_reward

        # ── 1. Lower-level helper: `grant_timed_product` & `give_reward`
        r_timed = role_stub(951, "AmbiguousTimedColor")
        _, person, bot, _ = purchase_ctx(granted_role=r_timed)

        async def _apply_then_timeout(role_obj, reason=None):
            if role_obj not in person.roles:
                person.roles.append(role_obj)
            raise asyncio.TimeoutError("Discord gateway/HTTP timed out after role apply")

        person.add_roles = AsyncMock(side_effect=_apply_then_timeout)
        res_timed_ambig = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="AmbiguousTimedColor",
            duration_hours=12, item_type="temp_role",
            category=TO.CATEGORY_COLOR, role_id=951)
        self.assertFalse(res_timed_ambig["success"])
        self.assertTrue(res_timed_ambig["uncertain"],
                        "timeout after role application must be uncertain=True")

        # Pure transport timeout even without local cache update must also be uncertain=True
        person.roles = []
        person.add_roles = AsyncMock(
            side_effect=TimeoutError("HTTP read timed out waiting for Discord"))
        res_perm_ambig = await give_reward(
            bot, GUILD, USER, "role", role_id=951,
            item_name="AmbiguousPermColor", source="shop")
        self.assertFalse(res_perm_ambig["success"])
        self.assertTrue(res_perm_ambig["uncertain"],
                        "transport timeout on add_roles must be uncertain=True")

        # Definite pre-application rejection (discord.Forbidden 403, role not on member)
        person.roles = []
        forbidden_exc = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50013, "message": "Missing Permissions"})
        person.add_roles = AsyncMock(side_effect=forbidden_exc)
        res_timed_def = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="ForbiddenTimedColor",
            duration_hours=12, item_type="temp_role",
            category=TO.CATEGORY_COLOR, role_id=951)
        self.assertFalse(res_timed_def["success"])
        self.assertFalse(res_timed_def["uncertain"],
                         "definite 403 Forbidden before role apply must be uncertain=False")

        res_perm_def = await give_reward(
            bot, GUILD, USER, "role", role_id=951,
            item_name="ForbiddenPermColor", source="shop")
        self.assertFalse(res_perm_def["success"])
        self.assertFalse(res_perm_def["uncertain"],
                         "definite 403 Forbidden in give_reward must be uncertain=False")

        # ── 2. Propagation through Shop `process_purchase`:
        # 2a. Ambiguous timeout during timed Color purchase -> parked as `uncertain`, NOT refunded.
        execute("DELETE FROM inventory_items WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        timed_id = shop_item(
            kind="color", name="ShopTimedAmber", price=250, stock=3,
            role_id=952, duration_hours=24)
        r_shop_timed = role_stub(952, "ShopTimedAmber")
        inter_ambig, person_ambig, _, _ = purchase_ctx(granted_role=r_shop_timed)

        async def _shop_apply_then_timeout(role_obj, reason=None):
            person_ambig.roles.append(role_obj)
            raise asyncio.TimeoutError("Timed out after Discord applied role")

        person_ambig.add_roles = AsyncMock(side_effect=_shop_apply_then_timeout)
        await process_purchase(inter_ambig, timed_id)

        op_ambig = rows("SELECT status, charged, refunded, stock_reserved, stock_returned "
                        "FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1")[0]
        self.assertEqual(op_ambig, (TO.PURCHASE_UNCERTAIN, 1, 0, 1, 0),
                         "ambiguous add_roles must park purchase as uncertain without refunding")
        self.assertEqual(
            rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER)), [(8750,)])
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?",
                 (timed_id,)), [(2,)])
        self.assertIn("Purchase needs a check", reply_text(inter_ambig))

        # 2b. Definite 403 Forbidden rejection during timed Color purchase -> `failed` + auto-refunded.
        inter_def, person_def, _, _ = purchase_ctx(granted_role=r_shop_timed)
        person_def.add_roles = AsyncMock(side_effect=forbidden_exc)
        await process_purchase(inter_def, timed_id)
        op_def = rows("SELECT status, charged, refunded, stock_reserved, stock_returned "
                      "FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1")[0]
        self.assertEqual(op_def, (TO.PURCHASE_FAILED, 1, 1, 1, 1),
                         "definite 403 rejection must compensate money and stock")
        self.assertEqual(
            rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER)), [(8750,)],
            "second attempt refunded its 250 charge back to 8750")
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?",
                 (timed_id,)), [(2,)],
            "second attempt returned its reserved stock unit back to 2")
        self.assertIn("Purchase not completed", reply_text(inter_def))

    async def test_p2_level_reversal_entitlement_check_fails_safely_on_locked_db(self):
        """P2 regression: when `timed_holdings` raises `OperationalError('database is locked')`
        during `_role_granted_elsewhere`, `reconcile_level_drop` must retain the
        Discord role, record the reversal as `outstanding`, preserve the claim
        row, and recover cleanly on subsequent passes.
        """
        import aiosqlite
        from utils import level_claims, level_ops

        await level_claims.ensure_tables()
        r_lvl = role_stub(777, "SharedLevelAndTimedRole")
        _, person, bot, server = purchase_ctx(granted_role=r_lvl)
        person.guild = server

        async def _add_role(r, reason=None):
            if r not in person.roles:
                person.roles.append(r)

        async def _rem_role(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.add_roles = AsyncMock(side_effect=_add_role)
        person.remove_roles = AsyncMock(side_effect=_rem_role)

        # Member owns a live timed holding for role 777 AND an admin-created
        # fulfilled Level 10 role claim for the same role 777, now dropped to Level 5.
        granted_th = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="TimedCrown", duration_hours=24,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=777)
        self.assertTrue(granted_th["success"])
        self.assertIn(r_lvl, person.roles)

        execute("UPDATE levels SET xp = 100, level = 5 WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        claim_id = execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref,
                 payload_json, status, source, operation_id, created_at, fulfilled_at)
            VALUES (?, ?, 10, 'role', '777', ?, 'fulfilled', 'setxp', 'op-admin-grant', ?, ?)
        """, (GUILD, USER, json.dumps({"role_id": 777}), TO.iso(), TO.iso()))

        # Pass 1: inject `OperationalError("database is locked")` on the
        # `timed_holdings` query inside `_role_granted_elsewhere`.
        real_execute = aiosqlite.Connection.execute

        async def _locked_timed_holdings(conn, sql, *args, **kwargs):
            if "FROM timed_holdings" in str(sql) and "SELECT id, item_name, expires_at" in str(sql):
                raise aiosqlite.OperationalError("database is locked")
            return await real_execute(conn, sql, *args, **kwargs)

        with patch.object(aiosqlite.Connection, "execute", new=_locked_timed_holdings):
            rep_locked = await level_claims.reconcile_level_drop(
                GUILD, USER, 5, operation_id="op-drop-1", member=person,
                reason="test level drop under locked timed_holdings")

        # Role must NOT be removed, claim must NOT be deleted, and reversal must be `outstanding`.
        person.remove_roles.assert_not_awaited()
        self.assertIn(r_lvl, person.roles)
        self.assertEqual(
            rows("SELECT status FROM level_reward_claims WHERE id=?", (claim_id,)),
            [("fulfilled",)],
            "claim row must survive when entitlement verification fails")
        rev_rows = rows(
            "SELECT status, reason FROM reward_reversals WHERE claim_id=?",
            (claim_id,))
        self.assertEqual(len(rev_rows), 1)
        self.assertEqual(rev_rows[0][0], level_ops.STATUS_OUTSTANDING)
        self.assertIn("database is locked", rev_rows[0][1])
        self.assertEqual(len(rep_locked["role_failures"]), 1)

        # Pass 2: DB lock clears while the independent timed holding is still live.
        # The role stays on the member because the live timed holding justifies it.
        rep_live_holding = await level_claims.reconcile_level_drop(
            GUILD, USER, 5, operation_id="op-drop-2", member=person,
            reason="retry while timed holding is still live")
        person.remove_roles.assert_not_awaited()
        self.assertIn(r_lvl, person.roles)
        self.assertEqual(
            rows("SELECT status FROM level_reward_claims WHERE id=?", (claim_id,)),
            [("fulfilled",)])
        self.assertTrue(
            any("timed holding 'TimedCrown'" in item.get("reason", "")
                for item in rep_live_holding["kept_unresolved"]),
            str(rep_live_holding["kept_unresolved"]))

        # Pass 3: after the timed holding expires and is cleaned up (no other
        # entitlement exists), a subsequent retry reverses the role and deletes the claim.
        await TO.delete_timed_holding(granted_th["entry_key"])
        rep_clean = await level_claims.reconcile_level_drop(
            GUILD, USER, 5, operation_id="op-drop-3", member=person,
            reason="retry after timed holding expired")
        person.remove_roles.assert_awaited_once()
        self.assertNotIn(r_lvl, person.roles)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM level_reward_claims WHERE id=?", (claim_id,)),
            [(0,)],
            "claim row is deleted once the role is cleanly reversed with no other entitlement")
        self.assertEqual(
            rows("SELECT status FROM reward_reversals WHERE claim_id=?", (claim_id,)),
            [(level_ops.STATUS_APPLIED,)])
        self.assertEqual(len(rep_clean["reversed"]["role"]), 1)

    async def test_p3_booster_level_without_fabricating_member_count_and_p5_equip_swap(self):
        """P3 & P5 regression:
        1. When `member` is a standard `discord.Member`-like object without a
           per-member `premium_subscription_count` attribute (even if the guild
           has `premium_subscription_count=14`), `_booster_level_for` does not
           infer from the guild count, protects level-1 booster roles, does not
           fabricate entitlement to unheld level-2 roles, and retains level-2
           booster roles already worn by an active booster.
        2. If `remove_roles(old_role)` fails transiently during
           `swap_and_record_equipped`, timed-holding ownership and subsequent
           expiry cleanup remain consistent.
        """
        execute("""
            INSERT OR REPLACE INTO boost_config
                (guild_id, enabled, boost1_role_id, boost2_role_id)
            VALUES (?, 1, 961, 962)
        """, (GUILD,))
        execute("""
            INSERT INTO boost_color_roles (guild_id, role_id, role_name, requires_boost_level)
            VALUES (?, 963, 'BoostPink', 1), (?, 964, 'BoostGold', 2)
        """, (GUILD, GUILD))

        _, person, bot, server = purchase_ctx()
        server.premium_subscription_count = 14
        # Ensure `person` has NO `premium_subscription_count` attribute, matching `discord.Member`.
        if hasattr(person, "premium_subscription_count"):
            delattr(person, "premium_subscription_count")
        person.premium_since = TO.utcnow()
        person.roles = []

        self.assertEqual(TO._booster_level_for(person), (1, False))
        managed_unworn = await TO.member_managed_role_ids(
            GUILD, USER, bot=bot, member=person)
        self.assertIn(961, managed_unworn)
        self.assertIn(963, managed_unworn)
        self.assertNotIn(962, managed_unworn,
                         "must not infer 2x boost from guild.premium_subscription_count")
        self.assertNotIn(964, managed_unworn)

        # When the active booster is already wearing boost2_role_id (962) or
        # level-2 boost color (964) on Discord, it is retained while boosting.
        person.roles = [role_stub(962, "Boost2"), role_stub(964, "BoostGold")]
        managed_worn = await TO.member_managed_role_ids(
            GUILD, USER, bot=bot, member=person)
        self.assertIn(962, managed_worn)
        self.assertIn(964, managed_worn)

        # Once the member stops boosting (`premium_since = None`), neither is retained.
        person.premium_since = None
        managed_after_unboost = await TO.member_managed_role_ids(
            GUILD, USER, bot=bot, member=person)
        self.assertNotIn(961, managed_after_unboost)
        self.assertNotIn(962, managed_after_unboost)
        self.assertNotIn(963, managed_after_unboost)
        self.assertNotIn(964, managed_after_unboost)

        # ── P5: Fault injection in `swap_and_record_equipped` when `remove_roles(old_role)` fails
        r_first = role_stub(965, "FirstTimedRole")
        r_second = role_stub(966, "SecondTimedRole")
        _, person2, bot2, server2 = purchase_ctx()
        roles_map = {965: r_first, 966: r_second}
        server2.get_role = lambda rid: roles_map.get(int(rid))

        async def _add_role2(r, reason=None):
            if r not in person2.roles:
                person2.roles.append(r)

        person2.add_roles = AsyncMock(side_effect=_add_role2)

        g1 = await TO.grant_timed_product(
            bot2, GUILD, USER, item_name="FirstTimedRole", duration_hours=1,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=965)
        self.assertTrue(g1["success"])
        self.assertIn(r_first, person2.roles)

        # Inject a transient Discord error on `remove_roles` during the second grant's swap
        person2.remove_roles = AsyncMock(
            side_effect=RuntimeError("Transient Discord 502 removing old role"))
        g2 = await TO.grant_timed_product(
            bot2, GUILD, USER, item_name="SecondTimedRole", duration_hours=24,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=966)
        self.assertTrue(g2["success"])
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         g2["entry_key"])
        self.assertIn(r_first, person2.roles)
        self.assertIn(r_second, person2.roles)

        # When `g1` expires and the transient Discord error clears, `cleanup_expired_holdings`
        # still removes `r_first` from Discord, keeps `g2` equipped, and deletes `g1`.
        async def _clean_remove(role_obj, reason=None):
            person2.roles = [r for r in person2.roles if r.id != role_obj.id]

        person2.remove_roles = AsyncMock(side_effect=_clean_remove)
        await self._expire(g1["entry_key"])
        stats = await TO.cleanup_expired_holdings(bot2)
        self.assertEqual(stats, {"removed": 1, "kept": 0, "errors": []})
        self.assertNotIn(r_first, person2.roles)
        self.assertIn(r_second, person2.roles)
        self.assertEqual((await get_equipped(GUILD, USER))["entry_key"],
                         g2["entry_key"])

    async def test_f1_temp_role_cleanup_respects_entitlement_and_fails_safe(self):
        """Priority 1 (F1) regression:
        (a) Expired legacy `temp_roles` row + active entitlement (`timed_holdings`)
            -> retain Discord role, keep active `timed_holdings` row and its
               `equipped_roles` slot, delete expired `temp_roles` row.
        (b) Expired legacy `temp_roles` row + no remaining entitlement
            -> remove Discord role, clean up expired legacy `temp_role` inventory
               item and equipped slot, delete expired `temp_roles` row.
        (c) Entitlement/DB check failure -> retain Discord role and preserve
            the expired `temp_roles` row for retry.
        """
        from cogs.shop import Shop

        r701 = role_stub(701, "Crimson")
        _, person, bot, server = purchase_ctx(granted_role=r701, roles_held=(r701,))
        server.get_role = lambda rid: r701 if int(rid) == 701 else None

        async def _remove_role(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.remove_roles = AsyncMock(side_effect=_remove_role)

        # (a) Expired legacy temp_roles row + active timed_holdings row for role 701
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Crimson", duration_hours=24,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=701)
        self.assertTrue(granted["success"])
        past_iso = (TO.utcnow() - timedelta(hours=2)).isoformat()
        execute(
            "INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at, source) "
            "VALUES (?, ?, 701, ?, 'legacy')",
            (GUILD, USER, past_iso))

        shop_cog = Shop.__new__(Shop)
        shop_cog.bot = bot

        # (c) First inject an entitlement-check failure: role must be retained AND
        # the expired temp_roles row must remain in place for retry.
        real_entitled = TO.role_still_entitled

        async def _broken_entitled(*args, **kwargs):
            raise TO.EntitlementCheckError("simulated DB lock in role_still_entitled")

        TO.role_still_entitled = _broken_entitled
        try:
            await Shop.temp_role_cleanup.coro(shop_cog)
        finally:
            TO.role_still_entitled = real_entitled

        person.remove_roles.assert_not_awaited()
        self.assertIn(r701, person.roles)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=701",
                 (GUILD, USER)),
            [(1,)],
            "retryable temp_roles row must survive when entitlement check fails")

        # Now run (a) with normal entitlement check: active timed_holdings row
        # entitles role 701, so Discord role and equipped slot stay intact while
        # the expired temp_roles row is cleaned up.
        await Shop.temp_role_cleanup.coro(shop_cog)
        person.remove_roles.assert_not_awaited()
        self.assertIn(r701, person.roles)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=701",
                 (GUILD, USER)),
            [(1,)])
        self.assertEqual(
            rows("SELECT COUNT(*) FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=701",
                 (GUILD, USER)),
            [(0,)])
        eq_now = await get_equipped(GUILD, USER)
        self.assertIsNotNone(eq_now)
        self.assertEqual(eq_now["entry_key"], granted["entry_key"])

        # (b) Delete the timed holding so NO entitlement remains, insert a fresh
        # expired legacy temp_roles row + expired legacy temp_role inventory item,
        # and verify temp_role_cleanup removes the Discord role and cleans up.
        await TO.delete_timed_holding(granted["entry_key"])
        execute("DELETE FROM equipped_roles WHERE guild_id=? AND user_id=?", (GUILD, USER))
        execute(
            "INSERT INTO inventory_items (guild_id, user_id, item_name, item_type, quantity, metadata, source) "
            "VALUES (?, ?, 'LegacyTempCrimson', 'temp_role', 1, ?, 'legacy')",
            (GUILD, USER, json.dumps({"role_id": 701, "expires_at": past_iso})))
        inv_id = rows(
            "SELECT id FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='LegacyTempCrimson'",
            (GUILD, USER))[0][0]
        execute(
            "INSERT OR REPLACE INTO equipped_roles (guild_id, user_id, item_name, role_id, entry_key) "
            "VALUES (?, ?, 'LegacyTempCrimson', 701, ?)",
            (GUILD, USER, f"i:{inv_id}"))
        execute(
            "INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at, source) "
            "VALUES (?, ?, 701, ?, 'legacy')",
            (GUILD, USER, past_iso))

        await Shop.temp_role_cleanup.coro(shop_cog)
        person.remove_roles.assert_awaited_once()
        self.assertNotIn(r701, person.roles)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=701",
                 (GUILD, USER)),
            [(0,)])
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE id=?", (inv_id,)),
            [(0,)])
        self.assertIsNone(await get_equipped(GUILD, USER))

    async def test_priority2a_late_delivery_after_refund_rolls_back_all_product_types(self):
        """Priority 2A regression:
        When an operator refunds an operation while `_deliver_purchase()` is in
        flight (so `claim_purchase_transition(..., to_status=PURCHASE_DELIVERED)`
        returns `False` in `process_purchase`), late-delivered goods are rolled
        back for:
          1. Timed Color / timed holding (removes holding, equip slot, and Discord
             role when unentitled; retains Discord role when another active
             entitlement still requires it).
          2. Permanent role (restores `inventory_items`, clears equip slot, and
             removes Discord role when unentitled; retains Discord role when
             an independent entitlement requires it).
          3. Potion, Title, and permanent custom Item (removes the +1 unit added
             by the late delivery without touching pre-existing units or
             consuming level-reward `reward_grants` provenance).
        """
        import cogs.shop as shop_mod
        from utils import level_ops

        await level_ops.ensure_tables()
        r_timed = role_stub(701, "Crimson")
        r_perm = role_stub(702, "Knight")
        roles_map = {701: r_timed, 702: r_perm}
        start_bal = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0]

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_operator_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            op_row = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0]
            op_id = op_row[0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="operator timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True,
                note="refunded before late delivery returned")
            return res

        shop_mod._deliver_purchase = _deliver_then_operator_refund
        try:
            # 1a. Timed Color with NO other entitlement -> holding deleted AND Discord role removed
            color_id = shop_item(
                "color", price=100, name="LateCrimson",
                role_id=701, duration_hours=24, stock=5)
            itx, person, bot, server = purchase_ctx(granted_role=r_timed)
            server.get_role = lambda rid: roles_map.get(int(rid))

            async def _add_r(r, reason=None):
                if r not in person.roles:
                    person.roles.append(r)

            async def _rem_r(r, reason=None):
                person.roles = [x for x in person.roles if x.id != r.id]

            person.add_roles = AsyncMock(side_effect=_add_r)
            person.remove_roles = AsyncMock(side_effect=_rem_r)

            await process_purchase(itx, color_id)
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0],
                start_bal,
                "buyer refunded")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=701",
                     (GUILD, USER)),
                [(0,)],
                "late timed holding removed")
            self.assertNotIn(r_timed, person.roles,
                             "late Discord role removed when no other entitlement exists")
            self.assertIsNone(await get_equipped(GUILD, USER))

            # 1b. Timed Color WITH an independent active timed holding for role 701 -> retain Discord role!
            indep = await TO.grant_timed_product(
                bot, GUILD, USER, item_name="IndependentCrimson", duration_hours=48,
                item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=701)
            self.assertTrue(indep["success"])
            self.assertIn(r_timed, person.roles)
            itx2, _, _, _ = purchase_ctx()
            itx2.user = person
            itx2.guild = server
            itx2.client = bot
            await process_purchase(itx2, color_id)
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0],
                start_bal)
            self.assertIn(r_timed, person.roles,
                          "independent entitlement must keep Discord role 701 on member")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=701",
                     (GUILD, USER)),
                [(1,)],
                "independent timed holding preserved")
            await TO.delete_timed_holding(indep["entry_key"])
            person.roles = []

            # 2. Permanent role with NO prior entitlement -> inventory row deleted, equip cleared, Discord role removed
            perm_id = shop_item(
                "role", price=200, name="LateKnight",
                role_id=702, duration_hours=None, stock=5)
            itx3, _, _, _ = purchase_ctx()
            itx3.user = person
            itx3.guild = server
            itx3.client = bot
            await process_purchase(itx3, perm_id)
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0],
                start_bal,
                "buyer refunded")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='LateKnight' AND quantity > 0",
                     (GUILD, USER)),
                [(0,)],
                "late permanent role removed from inventory_items")
            self.assertNotIn(r_perm, person.roles,
                             "late permanent role removed from Discord member")
            self.assertIsNone(await get_equipped(GUILD, USER))

            # 3. Potion, Title, and permanent custom Item -> +1 unit rolled back,
            #    preserving pre-existing stack and level-reward provenance!
            execute(
                "INSERT INTO inventory_items (guild_id, user_id, item_name, item_type, quantity, source) "
                "VALUES (?, ?, 'LatePotion', 'potion', 2, 'level_claim')",
                (GUILD, USER))
            execute(
                "INSERT INTO reward_grants (grant_id, guild_id, user_id, claim_id, reward_level, track, kind, item_name, quantity, remaining_quantity, operation_id, created_at) "
                "VALUES ('grant-pot-1', ?, ?, 901, 5, 'shop', 'inventory', 'LatePotion', 2, 2, 'op-lvl-1', '2026-01-01T00:00:00+00:00')",
                (GUILD, USER))

            potion_id = shop_item(
                "potion", price=150, name="LatePotion",
                duration_hours=2, multiplier=2.0, stock=5)
            title_id = shop_item(
                "title", price=150, name="LateTitle", stock=5)
            custom_id = shop_item(
                "custom", price=150, name="LateRelic", stock=5)

            for pid in (potion_id, title_id, custom_id):
                itx_item, _, _, _ = purchase_ctx()
                itx_item.user = person
                itx_item.guild = server
                itx_item.client = bot
                await process_purchase(itx_item, pid)

            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0],
                start_bal,
                "buyer refunded after all 3 late item deliveries")
            self.assertEqual(
                rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='LatePotion'",
                     (GUILD, USER)),
                [(2,)],
                "pre-existing 2x LatePotion stack preserved without keeping the late +1 unit")
            self.assertEqual(
                rows("SELECT remaining_quantity, unattributed FROM reward_grants WHERE grant_id='grant-pot-1'"),
                [(2, 0)],
                "level-reward provenance remaining_quantity is untouched by late-shop rollback")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name IN ('LateTitle', 'LateRelic') AND quantity > 0",
                     (GUILD, USER)),
                [(0,)],
                "late Title and custom Item rows removed when no prior stack existed")
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_priority2b_compensate_failed_purchase_never_falsely_claims_refund_when_claim_lost(self):
        """Priority 2B + Audit Fix 6 regression:
        1. When `_compensate_failed_purchase()` loses its `pending -> failed`
           transition claim because the operation was already moved to `uncertain`
           with `refunded=0`, the buyer is NEVER told that their payment was
           returned AND is NEVER told that `uncertain` is retried automatically;
           instead 'Manual review required' explains the real status.
        2. When an operation is in `failed` with `refunded=0` (incomplete refund),
           'Refund pending' explains that reconciliation retries it automatically.
        3. When the operation was already refunded (`refunded=1, status='refunded'`),
           the message accurately reflects the committed refund.
        4. When a `free` item's stock reservation (`stock_reserved=1, stock_returned=0`)
           remains held (either in `failed` or `uncertain`), the message does NOT
           describe the purchase as simply cancelled without noting the held stock
           reservation.
        """
        from cogs.shop import _compensate_failed_purchase

        # Case 1: Paid operation moved to `uncertain` (refunded=0) -> Manual review required (NOT auto-retried)
        itx1, _, _, _ = purchase_ctx()
        op_uncertain = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="Starlight", currency="balance", amount=250)
        await TO.update_purchase(op_uncertain, charged=1, stock_reserved=1)
        await TO.claim_purchase_transition(
            op_uncertain, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_UNCERTAIN, detail="reconcile parked as uncertain")

        await _compensate_failed_purchase(
            itx1, op_id=op_uncertain,
            delivery={"success": False, "uncertain": False, "error": "Role not found"},
            name="Starlight", pay_amount=250, pay_currency="balance", free=False)

        embed1 = itx1.response.send_message.await_args.kwargs["embed"]
        self.assertNotIn("payment was returned", embed1.description)
        self.assertIn("has not been returned yet", embed1.description)
        self.assertTrue(
            any(f.name == "Manual review required" for f in embed1.fields),
            "uncertain operation must report Manual review required")
        self.assertFalse(
            any("retried automatically" in (f.value or "") for f in embed1.fields),
            "must never claim uncertain operations are retried automatically")

        # Case 2: Paid operation in `failed` with `refunded=0` -> Refund pending (auto-retried by reconcile_purchases)
        itx_failed, _, _, _ = purchase_ctx()
        op_failed_pending = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="Starlight", currency="balance", amount=250)
        await TO.update_purchase(op_failed_pending, charged=1, stock_reserved=0)
        await TO.claim_purchase_transition(
            op_failed_pending, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_FAILED, detail="refund_purchase interrupted")

        await _compensate_failed_purchase(
            itx_failed, op_id=op_failed_pending,
            delivery={"success": False, "uncertain": False, "error": "Role not found"},
            name="Starlight", pay_amount=250, pay_currency="balance", free=False)

        embed_failed = itx_failed.response.send_message.await_args.kwargs["embed"]
        self.assertIn("has not been returned yet", embed_failed.description)
        self.assertTrue(
            any(f.name == "Refund pending" and "retried automatically" in f.value
                for f in embed_failed.fields),
            "failed operation with refunded=0 is retried automatically by reconcile_purchases")

        # Case 3: Operation already refunded by operator (`status='refunded', refunded=1`)
        itx2, _, _, _ = purchase_ctx()
        op_refunded = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="Starlight", currency="balance", amount=250)
        await TO.update_purchase(op_refunded, charged=1, stock_reserved=0)
        await TO.claim_purchase_transition(
            op_refunded, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_FAILED, detail="already failed")
        await TO.refund_purchase(op_refunded)

        await _compensate_failed_purchase(
            itx2, op_id=op_refunded,
            delivery={"success": False, "uncertain": False, "error": "Role not found"},
            name="Starlight", pay_amount=250, pay_currency="balance", free=False)

        embed2 = itx2.response.send_message.await_args.kwargs["embed"]
        self.assertIn("payment was returned", embed2.description)
        self.assertFalse(
            any(f.name in ("Refund pending", "Manual review required") for f in embed2.fields),
            "no pending/review warning when refund is already committed")

        # Case 4a: Free item whose stock reservation remains held in `uncertain`
        itx_free_unc, _, _, _ = purchase_ctx()
        op_free_unc = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="FreeGift", currency="balance", amount=0)
        await TO.update_purchase(op_free_unc, charged=0, stock_reserved=1, stock_returned=0)
        await TO.claim_purchase_transition(
            op_free_unc, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_UNCERTAIN, detail="parked with stock held")

        await _compensate_failed_purchase(
            itx_free_unc, op_id=op_free_unc,
            delivery={"success": False, "uncertain": False, "error": "Role not found"},
            name="FreeGift", pay_amount=0, pay_currency="balance", free=True)

        embed_free_unc = itx_free_unc.response.send_message.await_args.kwargs["embed"]
        self.assertIn("stock reservation is still held", embed_free_unc.description)
        self.assertTrue(
            any(f.name == "Manual review required" for f in embed_free_unc.fields),
            "free item in uncertain with held stock must require manual review")

        # Case 4b: Free item whose stock reservation remains held in `failed`
        itx_free_fail, _, _, _ = purchase_ctx()
        op_free_fail = await TO.open_purchase(
            GUILD, USER, item_id=1, item_name="FreeGift", currency="balance", amount=0)
        await TO.update_purchase(op_free_fail, charged=0, stock_reserved=1, stock_returned=0)
        await TO.claim_purchase_transition(
            op_free_fail, from_statuses=(TO.PURCHASE_PENDING,),
            to_status=TO.PURCHASE_FAILED, detail="stock restore failed")

        await _compensate_failed_purchase(
            itx_free_fail, op_id=op_free_fail,
            delivery={"success": False, "uncertain": False, "error": "Role not found"},
            name="FreeGift", pay_amount=0, pay_currency="balance", free=True)

        embed_free_fail = itx_free_fail.response.send_message.await_args.kwargs["embed"]
        self.assertIn("stock reservation is still held", embed_free_fail.description)
        self.assertTrue(
            any(f.name == "Stock release pending" for f in embed_free_fail.fields),
            "free item in failed with held stock must report Stock release pending")

    async def test_priority3_level_test_reset_timed_holding_and_entitlement_failure_lease_cleanup(self):
        """Priority 3 regression for `utils/level_test_reset.py`:
        1. `inspect_member()` / `reset_member()` recognizes an unexpired
           `timed_holdings` row as a competing role grant (`roles_left_on_member`),
           preserving the Discord role on the member.
        2. When an entitlement check fails during `rollback_xp_operation(apply=True)`
           or `reset_member(apply=True)`, the role is NOT removed, the per-member
           lease in `level_op_locks` is released (`finally: release_lock`),
           `rolled_back` remains 0, and a subsequent retry succeeds cleanly.
        """
        import utils.level_test_reset as reset_mod
        from utils import level_claims, level_ops
        from utils.level_test_reset import inspect_member, reset_member, rollback_xp_operation

        await level_claims.ensure_tables()
        await level_ops.ensure_tables()

        r_shared = role_stub(980, "SharedTimedAndLevelRole")
        _, person, bot, server = purchase_ctx(granted_role=r_shared, roles_held=(r_shared,))
        person.guild = server
        server.get_role = lambda rid: r_shared if int(rid) == 980 else None

        async def _rem_role(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.remove_roles = AsyncMock(side_effect=_rem_role)

        # Grant an active timed holding for role 980 AND a fulfilled level role claim for role 980
        th = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="TimedHalo", duration_hours=24,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=980)
        self.assertTrue(th["success"])
        self.assertIn(r_shared, person.roles)

        execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, 5000, 10)",
                (GUILD, USER))
        op_mut = "xp_set-test-p3"
        await level_ops.record_xp_mutation(
            guild_id=GUILD, user_id=USER, source="setxp", actor_id=999,
            reason="grant level 10", xp_before=0, xp_after=5000,
            level_before=0, level_after=10, crossings=[10], operation_id=op_mut)
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref, payload_json,
                 status, created_at, fulfilled_at, source, operation_id)
            VALUES (?, ?, 10, 'role', '980', ?, 'fulfilled',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                    'setxp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 980}), op_mut))

        # 1. `inspect_member` sees the unexpired `timed_holdings` row in `role_other_grants`
        preview = await inspect_member(GUILD, USER)
        self.assertEqual(preview["plan"]["remove_discord_roles"], [])
        self.assertEqual(
            [r["role_id"] for r in preview["plan"]["roles_left_on_member"]],
            [980])

        # 2a. Fault-inject `EntitlementCheckError` in `_role_granted_elsewhere` during
        # `rollback_xp_operation(..., apply=True)` and verify lease cleanup + retryability!
        real_elsewhere = level_claims._role_granted_elsewhere

        async def _failing_elsewhere(*args, **kwargs):
            raise TO.EntitlementCheckError("simulated DB lock checking role entitlements")

        level_claims._role_granted_elsewhere = _failing_elsewhere
        try:
            with self.assertRaises(TO.EntitlementCheckError):
                await rollback_xp_operation(
                    GUILD, USER, operation_id=op_mut, apply=True,
                    reversed_by=999, reason="rollback with entitlement error",
                    member=person)
        finally:
            level_claims._role_granted_elsewhere = real_elsewhere

        person.remove_roles.assert_not_awaited()
        self.assertIn(r_shared, person.roles)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
                 (GUILD, USER)),
            [(0,)],
            "per-member lease in level_op_locks must be released on rollback_xp_operation failure")
        self.assertEqual(
            rows("SELECT rolled_back FROM xp_mutations WHERE operation_id=?", (op_mut,)),
            [(0,)],
            "xp_mutation must remain un-rolled-back so retry is allowed")

        # 2b. Exercise `reset_member(..., apply=True)` and `rollback_xp_operation(..., apply=True)`
        # when `ops.open_operation` raises right after `ops.acquire_lock`, proving the
        # lease-cleanup fix releases the per-member lock in `level_op_locks`!
        real_open_op = level_ops.open_operation
        open_op_calls = {"n": 0}

        async def _failing_open_operation(*args, **kwargs):
            open_op_calls["n"] += 1
            held = rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
                        (GUILD, USER))[0][0]
            if held != 1:
                raise AssertionError(f"Expected active lease in level_op_locks, found {held}")
            raise RuntimeError("simulated DB failure inside ops.open_operation")

        level_ops.open_operation = _failing_open_operation
        try:
            with self.assertRaises(RuntimeError):
                await reset_member(
                    GUILD, USER, apply=True,
                    reversed_by=999, reason="reset_member open_operation failure",
                    allow_production=True,
                    member=person)
            self.assertEqual(
                rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)),
                [(0,)],
                "per-member lease in level_op_locks must be released when ops.open_operation raises in reset_member")

            with self.assertRaises(RuntimeError):
                await rollback_xp_operation(
                    GUILD, USER, operation_id=op_mut, apply=True,
                    reversed_by=999, reason="rollback_xp_operation open_operation failure",
                    member=person)
            self.assertEqual(
                rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)),
                [(0,)],
                "per-member lease in level_op_locks must be released when ops.open_operation raises in rollback_xp_operation")
        finally:
            level_ops.open_operation = real_open_op

        self.assertEqual(open_op_calls["n"], 2)
        self.assertIn(r_shared, person.roles)

        # 2c. Exercise `reset_member(..., apply=True)` while the competing `timed_holdings`
        # grant is active: XP/level resets to 0, but Discord role 980 is preserved on member!
        reset_keep = await reset_member(
            GUILD, USER, apply=True,
            reversed_by=999, reason="reset_member keeps role due to timed_holdings",
            allow_production=True,
            member=person)
        self.assertTrue(reset_keep["applied"])
        person.remove_roles.assert_not_awaited()
        self.assertIn(r_shared, person.roles,
                      "reset_member must preserve Discord role 980 while timed_holdings row is active")
        self.assertEqual(
            rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
                 (GUILD, USER)),
            [(0,)])

        # 3. Re-seed level 10 claim, delete the timed holding, and run `rollback_xp_operation` -> removes role!
        execute("INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, 5000, 10)",
                (GUILD, USER))
        op_mut2 = "xp_set-test-p3-b"
        await level_ops.record_xp_mutation(
            guild_id=GUILD, user_id=USER, source="setxp", actor_id=999,
            reason="grant level 10 again", xp_before=0, xp_after=5000,
            level_before=0, level_after=10, crossings=[10], operation_id=op_mut2)
        execute("""
            INSERT INTO level_reward_claims
                (guild_id, user_id, reward_level, track, reward_ref, payload_json,
                 status, created_at, fulfilled_at, source, operation_id)
            VALUES (?, ?, 10, 'role', '980', ?, 'fulfilled',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                    'setxp', ?)
        """, (GUILD, USER, json.dumps({"role_id": 980}), op_mut2))

        await TO.delete_timed_holding(th["entry_key"])
        rb_ok = await rollback_xp_operation(
            GUILD, USER, operation_id=op_mut2, apply=True,
            reversed_by=999, reason="retry after entitlement error cleared",
            member=person)
        self.assertTrue(rb_ok["applied"])
        person.remove_roles.assert_awaited_once()
        self.assertNotIn(r_shared, person.roles)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
                 (GUILD, USER)),
            [(0,)])
        self.assertEqual(
            rows("SELECT rolled_back FROM xp_mutations WHERE operation_id=?", (op_mut2,)),
            [(1,)])

    async def test_audit_fix_1_permanent_color_and_2_legacy_xp_boost_late_refund(self):
        """Audit Fixes 1 & 2 regression:
        1. Permanent Color listing (`type='color', duration_hours=None`, where
           `itype='color'` and `internal_type='role'`) rolls back its
           `inventory_items` row, `equipped_roles` slot, and Discord role when
           late-refunded.
        2. Legacy `xp_boost` listing (`type='xp_boost'`) deletes the delivered
           `leveling_active_boosts` row when late-refunded while preserving any
           unrelated active boosts.
        """
        import cogs.shop as shop_mod

        r_color = role_stub(711, "PermanentEmerald")
        roles_map = {711: r_color}
        start_bal = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                         (GUILD, USER))[0][0]

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="operator timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True,
                note="operator refunded before late delivery finalized")
            return res

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            # 1. Permanent Color (duration_hours=None -> itype='color', internal_type='role')
            perm_color_id = shop_item(
                "color", price=300, name="PermanentEmerald",
                role_id=711, duration_hours=None, stock=3)
            itx, person, bot, server = purchase_ctx(granted_role=r_color)
            server.get_role = lambda rid: roles_map.get(int(rid))

            async def _add_r(r, reason=None):
                if r not in person.roles:
                    person.roles.append(r)

            async def _rem_r(r, reason=None):
                person.roles = [x for x in person.roles if x.id != r.id]

            person.add_roles = AsyncMock(side_effect=_add_r)
            person.remove_roles = AsyncMock(side_effect=_rem_r)

            await process_purchase(itx, perm_color_id)
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                     (GUILD, USER))[0][0],
                start_bal,
                "permanent Color buyer refunded")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='PermanentEmerald' AND quantity > 0",
                     (GUILD, USER)),
                [(0,)],
                "late permanent Color inventory row must be rolled back")
            self.assertIsNone(
                await get_equipped(GUILD, USER),
                "late permanent Color equip slot must be cleared")
            self.assertNotIn(
                r_color, person.roles,
                "late permanent Color Discord role must be removed when unentitled")

            # 2. Legacy xp_boost listing -> removes only its own boost row, keeps unrelated boost
            execute(
                "INSERT INTO leveling_active_boosts (guild_id, user_id, multiplier, expires_at, source) "
                "VALUES (?, ?, 1.5, ?, 'potion')",
                (GUILD, USER, iso_in(6)))
            existing_boost_ids = [
                r[0] for r in rows(
                    "SELECT id FROM leveling_active_boosts WHERE guild_id=? AND user_id=?",
                    (GUILD, USER))
            ]
            self.assertEqual(len(existing_boost_ids), 1)

            xp_boost_id = shop_item(
                "xp_boost", price=200, name="LegacyDoubleXP",
                duration_hours=4, multiplier=2.0, stock=3)
            itx_boost, _, _, _ = purchase_ctx()
            itx_boost.user = person
            itx_boost.guild = server
            itx_boost.client = bot

            await process_purchase(itx_boost, xp_boost_id)
            self.assertEqual(
                rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                     (GUILD, USER))[0][0],
                start_bal,
                "xp_boost buyer refunded")
            remaining_boosts = rows(
                "SELECT id, multiplier, source FROM leveling_active_boosts WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
            self.assertEqual(
                remaining_boosts,
                [(existing_boost_ids[0], 1.5, "potion")],
                "late xp_boost row removed while unrelated potion boost is preserved")
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_audit_fix_3_safe_inventory_compensation_prior_and_concurrent_grants(self):
        """Audit Fix 3 regression:
        1. When `prior_inventory` exists for a permanent role/Color, late-refund
           compensation restores `prior_inventory` cleanly when no concurrent
           write occurred.
        2. When a concurrent independent role grant (`set_quantity`) or quantity
           change (`give_item`) modifies `inventory_items` before compensation
           runs, compensation does NOT delete the row or overwrite it with the
           stale `prior_inventory` snapshot.
        3. For stackable items (`potion`, `title`, `custom`), a concurrent
           independent `give_item` during late-refund compensation loses 0 units
           (only the 1 unit from the compensated purchase is deducted).
        """
        import cogs.shop as shop_mod
        from utils.inventory import give_item, set_quantity

        r_role = role_stub(712, "SolarCrown")
        roles_map = {712: r_role}

        # Case 1: `prior_inventory` exists (member already owned SolarCrown from level_claim)
        await set_quantity(
            GUILD, USER, "SolarCrown", 1,
            item_type="role", source="level_claim",
            metadata={"role_id": 712, "origin": "level_claim"})
        execute(
            "UPDATE inventory_items SET updated_at='2026-01-01 00:00:00' "
            "WHERE guild_id=? AND user_id=? AND item_name='SolarCrown'",
            (GUILD, USER))

        perm_role_id = shop_item(
            "role", price=250, name="SolarCrown",
            role_id=712, duration_hours=None, stock=5)
        itx1, person1, bot1, server1 = purchase_ctx(
            granted_role=r_role, roles_held=(r_role,))
        server1.get_role = lambda rid: roles_map.get(int(rid))

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True, note="refunded")
            return res

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            await process_purchase(itx1, perm_role_id)
        finally:
            shop_mod._deliver_purchase = real_deliver

        self.assertEqual(
            rows("SELECT quantity, source, updated_at FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='SolarCrown'",
                 (GUILD, USER)),
            [(1, "level_claim", "2026-01-01 00:00:00")],
            "prior_inventory snapshot restored when no concurrent write occurred")
        self.assertIn(r_role, person1.roles)

        # Case 2: No prior row existed, late shop purchase writes SolarCrown, AND
        # a concurrent independent admin grant overwrites SolarCrown before compensation runs!
        execute("DELETE FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='SolarCrown'",
                (GUILD, USER))

        async def _deliver_then_concurrent_admin_grant_and_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            # Simulate concurrent independent admin grant arriving right after shop delivery
            await set_quantity(
                GUILD, USER, "SolarCrown", 1,
                item_type="role", source="admin_grant",
                metadata={"role_id": 712, "granted_by": "admin"})
            execute(
                "UPDATE inventory_items SET source='admin_grant', updated_at='2026-06-01 12:00:00' "
                "WHERE guild_id=? AND user_id=? AND item_name='SolarCrown'",
                (GUILD, USER))
            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True, note="refunded")
            return res

        itx2, person2, bot2, server2 = purchase_ctx(granted_role=r_role)
        server2.get_role = lambda rid: roles_map.get(int(rid))

        async def _add_r2(r, reason=None):
            if r not in person2.roles:
                person2.roles.append(r)

        async def _rem_r2(r, reason=None):
            person2.roles = [x for x in person2.roles if x.id != r.id]

        person2.add_roles = AsyncMock(side_effect=_add_r2)
        person2.remove_roles = AsyncMock(side_effect=_rem_r2)

        shop_mod._deliver_purchase = _deliver_then_concurrent_admin_grant_and_refund
        try:
            await process_purchase(itx2, perm_role_id)
        finally:
            shop_mod._deliver_purchase = real_deliver

        self.assertEqual(
            rows("SELECT quantity, source, updated_at FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='SolarCrown'",
                 (GUILD, USER)),
            [(1, "admin_grant", "2026-06-01 12:00:00")],
            "concurrent independent admin role grant must NOT be deleted by late shop rollback")
        self.assertIn(
            r_role, person2.roles,
            "Discord role must be retained because concurrent independent admin grant entitles it")

        # Case 3: Stackable item with concurrent independent `give_item(+2)` during late-refund compensation
        title_id = shop_item("title", price=100, name="StarVoyager", stock=5)

        async def _deliver_item_then_concurrent_grant_and_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            # Concurrent quest grant adds +2 StarVoyager after the shop's +1
            await give_item(GUILD, USER, "StarVoyager", 2, item_type="title", source="quest")
            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True, note="refunded")
            return res

        itx3, _, _, _ = purchase_ctx()
        itx3.user = person2
        itx3.guild = server2
        itx3.client = bot2

        shop_mod._deliver_purchase = _deliver_item_then_concurrent_grant_and_refund
        try:
            await process_purchase(itx3, title_id)
        finally:
            shop_mod._deliver_purchase = real_deliver

        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='StarVoyager'",
                 (GUILD, USER)),
            [(2,)],
            "only the 1 unit from the compensated shop purchase is removed; the 2 concurrent quest units remain")

    async def test_audit_fix_4_preexisting_role_and_independent_entitlements_protected(self):
        """Audit Fix 4 + (h) regression:
        1. If the member ALREADY held the Discord role before the purchase (e.g.
           an admin/reaction role not recorded in any bot DB table), late-refund
           compensation for both timed Color/role and permanent Color/role never
           calls `remove_roles` and leaves no retry row to strip it later.
        2. Even when the purchase DID add the role (or when `role_still_entitled`
           is evaluated), independent Level (`level_reward_claims`), Prestige
           (`prestige_config` + `levels.prestige`), and Booster (`booster_roles` +
           `premium_since`) entitlements protect the Discord role from removal.
        """
        import cogs.shop as shop_mod

        r_pre = role_stub(713, "AdminGrantedRole")
        r_lvl = role_stub(714, "LevelEntitledRole")
        r_pres = role_stub(715, "PrestigeEntitledRole")
        r_boost = role_stub(716, "BoosterEntitledRole")
        roles_map = {
            713: r_pre,
            714: r_lvl,
            715: r_pres,
            716: r_boost,
        }

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True, note="refunded")
            return res

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            # 1a. Timed Color where member ALREADY held r_pre on Discord (no DB entitlement)
            timed_pre_id = shop_item(
                "color", price=100, name="TimedPreHeld",
                role_id=713, duration_hours=12, stock=5)
            itx, person, bot, server = purchase_ctx(
                granted_role=r_pre, roles_held=(r_pre,))
            server.get_role = lambda rid: roles_map.get(int(rid))
            person.remove_roles = AsyncMock()

            await process_purchase(itx, timed_pre_id)
            person.remove_roles.assert_not_awaited()
            self.assertIn(r_pre, person.roles,
                          "pre-existing Discord role must never be removed by timed Color rollback")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=713",
                     (GUILD, USER)),
                [(0,)],
                "no expired timed_holdings retry row left behind to strip pre-existing role later")

            # 1b. Permanent Color where member ALREADY held r_pre on Discord (no DB entitlement)
            perm_pre_id = shop_item(
                "color", price=100, name="PermPreHeld",
                role_id=713, duration_hours=None, stock=5)
            await process_purchase(itx, perm_pre_id)
            person.remove_roles.assert_not_awaited()
            self.assertIn(r_pre, person.roles,
                          "pre-existing Discord role must never be removed by permanent Color rollback")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=713",
                     (GUILD, USER)),
                [(0,)])

            # 2. Purchase adds the role (`role_added=True`), and before rollback runs the member
            # has an independent Level claim (714), Prestige (715), or Booster (716) entitlement!
            from utils import level_claims
            await level_claims.ensure_tables()
            execute("""
                INSERT INTO level_reward_claims
                    (guild_id, user_id, reward_level, track, reward_ref, payload_json,
                     status, created_at, fulfilled_at, source, operation_id)
                VALUES (?, ?, 15, 'role', '714', ?, 'fulfilled',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                        'level', 'op-lvl-714')
            """, (GUILD, USER, json.dumps({"role_id": 714})))

            # Member is at Prestige tier 3 (from seed_member)
            execute("INSERT OR REPLACE INTO prestige_roles (guild_id, tier, role_id) VALUES (?, 3, 715)",
                    (GUILD,))
            execute("""
                INSERT OR REPLACE INTO boost_config
                    (guild_id, enabled, boost1_role_id, boost2_role_id)
                VALUES (?, 1, 716, NULL)
            """, (GUILD,))

            for rid, r_obj, label in (
                (714, r_lvl, "LevelClaimColor"),
                (715, r_pres, "PrestigeColor"),
                (716, r_boost, "BoosterColor"),
            ):
                cid = shop_item(
                    "color", price=100, name=label,
                    role_id=rid, duration_hours=None, stock=5)
                itx_e, person_e, bot_e, server_e = purchase_ctx(granted_role=r_obj)
                server_e.get_role = lambda x: roles_map.get(int(x))
                if rid == 716:
                    person_e.premium_since = datetime.now(timezone.utc)
                    person_e.premium_subscription_count = 1
                else:
                    person_e.premium_since = None
                    person_e.premium_subscription_count = 0

                async def _add_re(r, reason=None, _p=person_e):
                    if r not in _p.roles:
                        _p.roles.append(r)

                person_e.add_roles = AsyncMock(side_effect=_add_re)
                person_e.remove_roles = AsyncMock()

                await process_purchase(itx_e, cid)
                person_e.remove_roles.assert_not_awaited()
                self.assertIn(
                    r_obj, person_e.roles,
                    f"independent entitlement for {label} (role {rid}) must protect Discord role")
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_audit_fix_5_retryable_role_removal_failures_for_timed_and_permanent_roles(self):
        """Audit Fix 5 regression:
        When `remove_roles` raises a Discord error during late-refund compensation
        for EITHER a timed holding (`h:<id>`) OR a permanent role/Color (`i:<id>`):
          1. No active grant is visible in `get_inventory()` or `get_timed_holdings()`.
          2. A durable expired retry marker remains in `timed_holdings` so the
             failure is not forgotten.
          3. Running `cleanup_expired_holdings(bot)` retries `remove_roles`,
             removes the Discord role once Discord recovers, and deletes the
             retry marker.
          4. If the member acquires an independent entitlement before the retry
             runs, `cleanup_expired_holdings(bot)` preserves the Discord role
             and deletes the retry marker without calling `remove_roles`.
        """
        import cogs.shop as shop_mod

        r_timed = role_stub(717, "RetryTimedRole")
        r_perm = role_stub(718, "RetryPermColor")
        r_ent = role_stub(719, "RetryLaterEntitled")
        roles_map = {717: r_timed, 718: r_perm, 719: r_ent}

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            await TO.claim_purchase_transition(
                op_id, from_statuses=(TO.PURCHASE_PENDING,),
                to_status=TO.PURCHASE_UNCERTAIN, detail="timeout")
            await TO.resolve_purchase_operation(
                op_id, action="refund", confirmed=True, note="refunded")
            return res

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            for kind, dur, rid, r_obj, name in (
                ("color", 24, 717, r_timed, "RetryTimedRole"),
                ("color", None, 718, r_perm, "RetryPermColor"),
            ):
                item_id = shop_item(
                    kind, price=100, name=name,
                    role_id=rid, duration_hours=dur, stock=5)
                itx, person, bot, server = purchase_ctx(granted_role=r_obj)
                server.get_role = lambda x: roles_map.get(int(x))

                async def _add_r(r, reason=None, _p=person):
                    if r not in _p.roles:
                        _p.roles.append(r)

                person.add_roles = AsyncMock(side_effect=_add_r)
                person.remove_roles = AsyncMock(
                    side_effect=RuntimeError("Discord 503 during late role removal"))

                await process_purchase(itx, item_id)

                # 1. No active grant visible in inventory or active timed_holdings
                active_inv = await get_inventory(GUILD, USER, include_empty=False)
                self.assertEqual(
                    [e for e in active_inv if e["item_name"] == name],
                    [],
                    f"{name}: no active inventory grant may remain when remove_roles fails")
                self.assertEqual(
                    [h for h in await TO.get_timed_holdings(GUILD, USER) if h["role_id"] == rid],
                    [],
                    f"{name}: no active timed holding may remain when remove_roles fails")

                # 2. Durable expired retry marker exists in timed_holdings while role is still on member
                self.assertIn(r_obj, person.roles)
                self.assertEqual(
                    rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=?",
                         (GUILD, USER, rid)),
                    [(1,)],
                    f"{name}: expired retry marker must be preserved when remove_roles fails")

                # 3. Retry via `cleanup_expired_holdings(bot)` after Discord recovers
                async def _rem_ok(r, reason=None, _p=person):
                    _p.roles = [x for x in _p.roles if x.id != r.id]

                person.remove_roles = AsyncMock(side_effect=_rem_ok)
                sweep = await TO.cleanup_expired_holdings(bot)
                self.assertEqual(sweep["removed"], 1)
                self.assertNotIn(
                    r_obj, person.roles,
                    f"{name}: retry cleanup must remove the Discord role once Discord recovers")
                self.assertEqual(
                    rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=?",
                         (GUILD, USER, rid)),
                    [(0,)],
                    f"{name}: retry marker deleted after successful role removal")

            # 4. If `remove_roles` fails during late rollback, and then the member earns an
            # independent entitlement before retry runs, retry keeps the role and deletes the marker!
            ent_id = shop_item(
                "color", price=100, name="RetryLaterEntitled",
                role_id=719, duration_hours=None, stock=5)
            itx_e, person_e, bot_e, server_e = purchase_ctx(granted_role=r_ent)
            server_e.get_role = lambda x: roles_map.get(int(x))

            async def _add_re(r, reason=None):
                if r not in person_e.roles:
                    person_e.roles.append(r)

            person_e.add_roles = AsyncMock(side_effect=_add_re)
            person_e.remove_roles = AsyncMock(
                side_effect=RuntimeError("Discord 503 during late role removal"))

            await process_purchase(itx_e, ent_id)
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=719",
                     (GUILD, USER)),
                [(1,)])

            # Grant an independent fulfilled level claim for role 719 before retry runs
            from utils import level_claims
            await level_claims.ensure_tables()
            execute("""
                INSERT INTO level_reward_claims
                    (guild_id, user_id, reward_level, track, reward_ref, payload_json,
                     status, created_at, fulfilled_at, source, operation_id)
                VALUES (?, ?, 20, 'role', '719', ?, 'fulfilled',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                        'level', 'op-lvl-719')
            """, (GUILD, USER, json.dumps({"role_id": 719})))

            person_e.remove_roles = AsyncMock()
            sweep2 = await TO.cleanup_expired_holdings(bot_e)
            self.assertEqual(sweep2["removed"], 1)
            person_e.remove_roles.assert_not_awaited()
            self.assertIn(
                r_ent, person_e.roles,
                "retry must preserve Discord role when member gained an independent entitlement")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=719",
                     (GUILD, USER)),
                [(0,)])
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_fix_f1_late_discord_role_removal_stale_interaction_user_vs_cached_guild_member(self):
        """Fix F1 regression:
        When `interaction.user` is an interaction-start snapshot (separate `Member`
        object whose `.roles` list is never mutated by `grant_timed_product` or
        `give_reward`/`equip_role`), late-refund compensation in `process_purchase`
        must:
          1. Never use `interaction.user.roles` as the deciding snapshot for role removal.
          2. Resolve the current cached guild `Member` (`guild.get_member(user_id)`) and
             remove the Discord role from it on both `h:` (timed) and permanent-role paths.
          3. Retain the durable retry marker in `timed_holdings` when role removal fails,
             until a subsequent retry succeeds or establishes an independent entitlement.
          4. Never remove a role protected by an independent entitlement (e.g. Prestige,
             Level, Booster, another Color, or another inventory grant).
        """
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase
        from utils.inventory import get_inventory

        r_timed = role_stub(721, "StaleItxTimedColor")
        r_perm = role_stub(722, "StaleItxPermColor")
        r_fail = role_stub(723, "StaleItxRetryColor")
        r_prot = role_stub(724, "StaleItxProtectedColor")
        roles_map = {721: r_timed, 722: r_perm, 723: r_fail, 724: r_prot}

        timed_id = shop_item(
            "color", price=100, name="StaleItxTimed",
            role_id=721, duration_hours=24, stock=5)
        perm_id = shop_item(
            "color", price=100, name="StaleItxPerm",
            role_id=722, duration_hours=None, stock=5)
        fail_id = shop_item(
            "color", price=100, name="StaleItxRetry",
            role_id=723, duration_hours=None, stock=5)
        prot_id = shop_item(
            "color", price=100, name="StaleItxProtected",
            role_id=724, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="late refund with separate interaction.user vs guild.get_member")
            return res

        def _make_split_member_ctx(granted_role):
            # `cached_member` is returned by `guild.get_member(USER)` and mutated by delivery.
            # `stale_itx_user` is a separate `SimpleNamespace` on `interaction.user` whose
            # `.roles` remains empty (`[]`) throughout delivery!
            itx, cached_member, bot, guild = purchase_ctx(granted_role=granted_role)
            guild.get_role = lambda rid: roles_map.get(int(rid))

            async def _add_to_cached(r, reason=None):
                if r not in cached_member.roles:
                    cached_member.roles.append(r)

            async def _rem_from_cached(r, reason=None):
                cached_member.roles = [x for x in cached_member.roles if x.id != r.id]

            cached_member.add_roles = AsyncMock(side_effect=_add_to_cached)
            cached_member.remove_roles = AsyncMock(side_effect=_rem_from_cached)

            stale_itx_user = member()
            stale_itx_user.roles = []
            stale_itx_user.add_roles = AsyncMock()
            stale_itx_user.remove_roles = AsyncMock()
            itx.user = stale_itx_user
            return itx, stale_itx_user, cached_member, bot, guild

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            # 1. Timed `h:` path: `interaction.user.roles` is `[]`, `cached_member.roles` has `r_timed`
            itx_t, stale_t, cached_t, _, _ = _make_split_member_ctx(r_timed)
            await process_purchase(itx_t, timed_id)
            self.assertEqual(stale_t.roles, [])
            cached_t.remove_roles.assert_awaited_once()
            self.assertNotIn(
                r_timed, cached_t.roles,
                "Timed h: late rollback must remove role from cached guild member even when interaction.user is stale")
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=721",
                     (GUILD, USER)),
                [(0,)])

            # 2. Permanent role path: `interaction.user.roles` is `[]`, `cached_member.roles` has `r_perm`
            itx_p, stale_p, cached_p, _, _ = _make_split_member_ctx(r_perm)
            await process_purchase(itx_p, perm_id)
            self.assertEqual(stale_p.roles, [])
            cached_p.remove_roles.assert_awaited_once()
            self.assertNotIn(
                r_perm, cached_p.roles,
                "Permanent role late rollback must remove role from cached guild member even when interaction.user is stale")
            self.assertEqual(
                [e for e in await get_inventory(GUILD, USER, include_empty=False) if e["item_name"] == "StaleItxPerm"],
                [])
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=722",
                     (GUILD, USER)),
                [(0,)])

            # 3. Retry marker retention when `cached_member.remove_roles` fails
            itx_f, stale_f, cached_f, bot_f, _ = _make_split_member_ctx(r_fail)
            cached_f.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 503"))
            await process_purchase(itx_f, fail_id)
            self.assertIn(r_fail, cached_f.roles)
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=723",
                     (GUILD, USER)),
                [(1,)],
                "Retry marker must remain in timed_holdings when remove_roles on cached member fails")

            async def _rem_f_ok(r, reason=None):
                cached_f.roles = [x for x in cached_f.roles if x.id != r.id]

            cached_f.remove_roles = AsyncMock(side_effect=_rem_f_ok)
            sweep = await TO.cleanup_expired_holdings(bot_f)
            self.assertEqual(sweep["removed"], 1)
            self.assertNotIn(r_fail, cached_f.roles)
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=? AND role_id=723",
                     (GUILD, USER)),
                [(0,)])

            # 4. Independent entitlement (Prestige tier 3 role 724) protects the role on `cached_member`
            execute("INSERT OR REPLACE INTO prestige_roles (guild_id, tier, role_id) VALUES (?, ?, ?)",
                    (GUILD, 3, 724))
            itx_pr, _, cached_pr, _, _ = _make_split_member_ctx(r_prot)
            await process_purchase(itx_pr, prot_id)
            cached_pr.remove_roles.assert_not_awaited()
            self.assertIn(
                r_prot, cached_pr.roles,
                "Role protected by Prestige entitlement must not be removed during late rollback")
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_fix_f2_failed_inventory_compensation_not_abandoned_and_durably_recovered(self):
        """Fix F2 regression:
        1. `rollback_role_inventory_grant` handles both `BEGIN IMMEDIATE` raising
           `OperationalError` and a statement after `BEGIN IMMEDIATE` raising
           `OperationalError`, rolling back any active transaction and returning `False`.
        2. When `process_purchase` receives a failed `rollback_role_inventory_grant`
           (`False`), it does NOT continue as though compensation succeeded (does not
           run `role_still_entitled` against the un-rolled-back inventory row, does not
           report successful compensation, and keeps a durable `delivery_rollback`
           record on `shop_purchase_operations`).
        3. Once the database recovers, `reconcile_purchases` finishes the inventory
           compensation first and then runs `role_still_entitled` + `remove_roles`
           safely, while preserving any independent inventory grants or separate
           role entitlements acquired before recovery.
        """
        import sqlite3
        import aiosqlite
        import cogs.shop as shop_mod
        import utils.reward_engine as re_mod
        from cogs.shop import process_purchase
        from utils.inventory import get_inventory, give_item, set_quantity

        # Part A: Direct unit tests of `rollback_role_inventory_grant` transaction errors
        await set_quantity(GUILD, USER, "TxFailureRole", 1, item_type="role",
                           metadata={"role_id": 730}, source="shop")
        delivered_snap = rows(
            "SELECT id, quantity, item_type, source, metadata, created_at, updated_at "
            "FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='TxFailureRole'",
            (GUILD, USER))[0]

        real_connect = re_mod.aiosqlite.connect

        class _BeginFailConn:
            def __init__(self, inner_ctx):
                self._inner_ctx = inner_ctx
                self._db = None

            async def __aenter__(self):
                self._db = await self._inner_ctx.__aenter__()
                real_exec = self._db.execute

                async def _exec(sql, *a, **kw):
                    if str(sql).strip().upper().startswith("BEGIN"):
                        raise sqlite3.OperationalError("database is locked on BEGIN IMMEDIATE")
                    return await real_exec(sql, *a, **kw)

                self._db.execute = _exec
                return self._db

            async def __aexit__(self, *exc):
                return await self._inner_ctx.__aexit__(*exc)

        re_mod.aiosqlite.connect = lambda *a, **kw: _BeginFailConn(real_connect(*a, **kw))
        try:
            ok_begin = await re_mod.rollback_role_inventory_grant(
                GUILD, USER, "TxFailureRole",
                prior_inv=None, delivered_inv=delivered_snap)
        finally:
            re_mod.aiosqlite.connect = real_connect
        self.assertFalse(
            ok_begin,
            "BEGIN IMMEDIATE raising OperationalError must be caught and return False")
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='TxFailureRole'",
                 (GUILD, USER)),
            [(1,)])

        class _MidTxFailConn:
            def __init__(self, inner_ctx):
                self._inner_ctx = inner_ctx
                self._db = None

            async def __aenter__(self):
                self._db = await self._inner_ctx.__aenter__()
                real_exec = self._db.execute

                async def _exec(sql, *a, **kw):
                    s = str(sql).strip().upper()
                    if s.startswith("DELETE FROM INVENTORY_ITEMS"):
                        raise sqlite3.OperationalError("simulated failure after transaction begins")
                    return await real_exec(sql, *a, **kw)

                self._db.execute = _exec
                return self._db

            async def __aexit__(self, *exc):
                return await self._inner_ctx.__aexit__(*exc)

        re_mod.aiosqlite.connect = lambda *a, **kw: _MidTxFailConn(real_connect(*a, **kw))
        try:
            ok_mid = await re_mod.rollback_role_inventory_grant(
                GUILD, USER, "TxFailureRole",
                prior_inv=None, delivered_inv=delivered_snap)
        finally:
            re_mod.aiosqlite.connect = real_connect
        self.assertFalse(
            ok_mid,
            "Failure after BEGIN IMMEDIATE must roll back and return False")
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='TxFailureRole'",
                 (GUILD, USER)),
            [(1,)])

        # Cleanup TxFailureRole
        self.assertTrue(await re_mod.rollback_role_inventory_grant(
            GUILD, USER, "TxFailureRole",
            prior_inv=None, delivered_inv=delivered_snap))

        # Part B: End-to-end `process_purchase` caller + durable recovery when DB fails during rollback
        r_f2 = role_stub(731, "DurableRecovColor")
        r_f2_ind = role_stub(732, "DurablePreserveColor")
        roles_map = {731: r_f2, 732: r_f2_ind}

        item_f2 = shop_item(
            "color", price=100, name="DurableRecovColor",
            role_id=731, duration_hours=None, stock=5)
        item_f2_ind = shop_item(
            "color", price=100, name="DurablePreserveColor",
            role_id=732, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="refund before finalize")
            return res

        real_rollback_role = re_mod.rollback_role_inventory_grant

        async def _rollback_with_begin_fail(*a, **kw):
            re_mod.aiosqlite.connect = lambda *ca, **ckw: _BeginFailConn(real_connect(*ca, **ckw))
            try:
                return await real_rollback_role(*a, **kw)
            finally:
                re_mod.aiosqlite.connect = real_connect

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            itx1, person1, bot1, guild1 = purchase_ctx(granted_role=r_f2)
            guild1.get_role = lambda rid: roles_map.get(int(rid))

            async def _add1(r, reason=None):
                if r not in person1.roles:
                    person1.roles.append(r)

            async def _rem1(r, reason=None):
                person1.roles = [x for x in person1.roles if x.id != r.id]

            person1.add_roles = AsyncMock(side_effect=_add1)
            person1.remove_roles = AsyncMock(side_effect=_rem1)

            # Inject `BEGIN IMMEDIATE` OperationalError inside `rollback_role_inventory_grant`
            re_mod.rollback_role_inventory_grant = _rollback_with_begin_fail
            try:
                await process_purchase(itx1, item_f2)
            finally:
                re_mod.rollback_role_inventory_grant = real_rollback_role

            op_f2 = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            op_rec = await TO.get_purchase(op_f2)
            self.assertEqual(op_rec["status"], TO.PURCHASE_REFUNDED)
            self.assertTrue(
                bool(op_rec.get("delivery_rollback")),
                "Failed inventory rollback must leave a durable delivery_rollback record on shop_purchase_operations")

            # Caller must not have continued to remove_roles while inventory rollback failed,
            # and must surface pending rollback reconciliation in the response embed
            person1.remove_roles.assert_not_awaited()
            self.assertIn(r_f2, person1.roles)
            sent_embed = itx1.response.send_message.await_args.kwargs["embed"]
            self.assertTrue(
                any("Rollback pending reconciliation" in (f.name or "") for f in sent_embed.fields),
                "Response embed must explicitly report incomplete rollback pending reconciliation")

            # Now the database is healthy again: `reconcile_purchases` finishes inventory
            # compensation first and then removes the unentitled Discord role!
            rec_stats = await TO.reconcile_purchases(bot=bot1)
            self.assertGreaterEqual(rec_stats["completed"], 1, str(rec_stats))
            self.assertEqual(
                [e for e in await get_inventory(GUILD, USER, include_empty=False)
                 if e["item_name"] == "DurableRecovColor"],
                [],
                "Durable recovery must remove the late-delivered inventory item")
            person1.remove_roles.assert_awaited_once()
            self.assertNotIn(
                r_f2, person1.roles,
                "Durable recovery must remove the Discord role after inventory rollback succeeds")
            self.assertFalse(
                bool((await TO.get_purchase(op_f2)).get("delivery_rollback")),
                "delivery_rollback must be cleared after durable recovery completes")

            # Part C: Preservation of independent inventory grants and role entitlements during recovery
            itx2, person2, bot2, guild2 = purchase_ctx(granted_role=r_f2_ind)
            guild2.get_role = lambda rid: roles_map.get(int(rid))

            async def _add2(r, reason=None):
                if r not in person2.roles:
                    person2.roles.append(r)

            person2.add_roles = AsyncMock(side_effect=_add2)
            person2.remove_roles = AsyncMock()

            re_mod.rollback_role_inventory_grant = _rollback_with_begin_fail
            try:
                await process_purchase(itx2, item_f2_ind)
            finally:
                re_mod.rollback_role_inventory_grant = real_rollback_role

            op_f2_ind = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            self.assertTrue(bool((await TO.get_purchase(op_f2_ind)).get("delivery_rollback")))

            # Before recovery runs, an independent admin grant increments `DurablePreserveColor`
            # (so quantity becomes 2: 1 from the refunded shop delivery + 1 from the independent grant)
            await give_item(
                GUILD, USER, "DurablePreserveColor", 1,
                item_type="role", metadata={"role_id": 732}, source="admin_gift")

            rec_stats2 = await TO.reconcile_purchases(bot=bot2)
            self.assertGreaterEqual(rec_stats2["completed"], 1, str(rec_stats2))
            rem_inv = [
                e for e in await get_inventory(GUILD, USER, include_empty=False)
                if e["item_name"] == "DurablePreserveColor"
            ]
            self.assertEqual(len(rem_inv), 1)
            self.assertEqual(
                rem_inv[0]["quantity"], 1,
                "Recovery must deduct only the refunded purchase unit and preserve the independent grant")
            person2.remove_roles.assert_not_awaited()
            self.assertIn(
                r_f2_ind, person2.roles,
                "Recovery must preserve the Discord role when the independent inventory grant entitles it")
            self.assertFalse(bool((await TO.get_purchase(op_f2_ind)).get("delivery_rollback")))
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_fix_f3_refunded_rebuy_preserves_existing_equipped_permanent_color(self):
        """Fix F3 regression:
        1. The member already owns and equips a permanent Color (`SolarCrown`, role_id=712).
        2. The member buys `SolarCrown` again via `process_purchase`.
        3. Delivery completes, but the second purchase is refunded before finalization.
        4. The prior inventory snapshot (`quantity=1`), the pre-existing `equipped_roles`
           entry (`entry_key = "i:<id>"`), and the Discord role on the member must all
           be preserved.
        """
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase
        from utils.equip_engine import equip_role, get_equipped
        from utils.inventory import get_inventory, set_quantity

        r_solar = role_stub(712, "SolarCrownRole")
        itx, person, bot, guild = purchase_ctx(granted_role=r_solar)
        guild.get_role = lambda rid: r_solar if int(rid) == 712 else None

        async def _add_r(r, reason=None):
            if r not in person.roles:
                person.roles.append(r)

        async def _rem_r(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.add_roles = AsyncMock(side_effect=_add_r)
        person.remove_roles = AsyncMock(side_effect=_rem_r)

        # 1. Pre-existing owned and equipped permanent Color
        await set_quantity(
            GUILD, USER, "SolarCrown", 1,
            item_type="role",
            metadata={"role_id": 712, "category": TO.CATEGORY_COLOR},
            source="shop")
        eq_res = await equip_role(bot, GUILD, USER, "SolarCrown")
        self.assertTrue(eq_res["success"], str(eq_res))
        prior_equipped = await get_equipped(GUILD, USER)
        self.assertIsNotNone(prior_equipped)
        self.assertEqual(prior_equipped["item_name"], "SolarCrown")
        self.assertEqual(prior_equipped["role_id"], 712)
        self.assertTrue(str(prior_equipped["entry_key"]).startswith("i:"))
        self.assertIn(r_solar, person.roles)

        rebuy_id = shop_item(
            "color", price=150, name="SolarCrown",
            role_id=712, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="late refund of permanent Color re-buy")
            return res

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            # 2 & 3. Member re-buys `SolarCrown` and the re-buy is late-refunded
            await process_purchase(itx, rebuy_id)
        finally:
            shop_mod._deliver_purchase = real_deliver

        # 4. Prior inventory snapshot, pre-existing `equipped_roles` entry, and Discord role are preserved!
        inv_after = [
            e for e in await get_inventory(GUILD, USER, include_empty=False)
            if e["item_name"] == "SolarCrown"
        ]
        self.assertEqual(len(inv_after), 1)
        self.assertEqual(inv_after[0]["quantity"], 1)

        equipped_after = await get_equipped(GUILD, USER)
        self.assertIsNotNone(
            equipped_after,
            "Pre-existing equipped_roles entry must NOT be cleared when a re-buy of an owned permanent Color is refunded")
        self.assertEqual(equipped_after["entry_key"], prior_equipped["entry_key"])
        self.assertEqual(equipped_after["item_name"], "SolarCrown")
        self.assertEqual(equipped_after["role_id"], 712)

        person.remove_roles.assert_not_awaited()
        self.assertIn(r_solar, person.roles)

    async def test_fix_n1_departed_member_expiry_and_late_delivery_rollback(self):
        """Fix N1 regression:
        1. `guild.fetch_member` raising `discord.NotFound` during ordinary expiry
           retires the expired holding (`removed=1`) instead of keeping it forever,
           while preserving unrelated ownership and equip state.
        2. Other `fetch_member` exceptions (`discord.HTTPException(503)`,
           `discord.Forbidden`, `RuntimeError`) remain retryable (`kept=1`).
        3. In late-delivery compensation (`process_purchase` / `compensate_late_delivery`),
           when the member has left the guild (`fetch_member` raises `discord.NotFound`)
           on either timed (`h:`) or permanent-role (`internal_type == "role"`) paths,
           inventory/equip rollback still runs and the Discord-role portion is marked
           resolved without attempting `remove_roles` or leaving an endless retry marker.
        4. An older departed-member expired holding does not starve newer expired
           holdings within a small `cleanup_expired_holdings(bot, limit=1)` window.
        """
        import discord
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase
        from utils.equip_engine import equip_role, get_equipped
        from utils.inventory import get_inventory, set_quantity

        def _make_not_found():
            resp = SimpleNamespace(status=404, reason="Not Found")
            return discord.NotFound(resp, "10007: Unknown Member")

        def _make_http_503():
            resp = SimpleNamespace(status=503, reason="Service Unavailable")
            return discord.HTTPException(resp, "503 Service Unavailable")

        r_exp = role_stub(741, "DepartedExpiryRole")
        r_perm_keep = role_stub(742, "DepartedPermKeepRole")
        r_active = role_stub(743, "ActiveMemberRole")
        r_late_t = role_stub(744, "DepartedLateTimedRole")
        r_late_p = role_stub(745, "DepartedLatePermRole")
        roles_map = {
            741: r_exp,
            742: r_perm_keep,
            743: r_active,
            744: r_late_t,
            745: r_late_p,
        }

        # ── Part 1 & 2: Ordinary expiry (`NotFound` vs transient `HTTPException`) ──
        _, person, bot, guild = purchase_ctx(granted_role=r_exp)
        guild.get_role = lambda rid: roles_map.get(int(rid))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="DepartedTimedColor", duration_hours=1,
            item_type="temp_role", category=TO.CATEGORY_COLOR, role_id=741)
        self.assertTrue(granted["success"])
        hid = int(granted["entry_key"].split(":", 1)[1])
        execute(
            "UPDATE timed_holdings SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (hid,))

        # Also give the member an unrelated permanent role item to verify unrelated ownership is untouched
        await set_quantity(
            GUILD, USER, "UnrelatedOwnedRole", 1,
            item_type="role", metadata={"role_id": 742}, source="shop")

        # Member is no longer in `guild.get_member` cache
        guild.get_member = lambda uid: None

        # 2a. Transient 503 from `fetch_member` must keep the holding for retry
        guild.fetch_member = AsyncMock(side_effect=_make_http_503())
        sweep_503 = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(sweep_503["removed"], 0, str(sweep_503))
        self.assertEqual(sweep_503["kept"], 1, str(sweep_503))
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (hid,)),
            [(1,)],
            "Transient fetch_member HTTPException must keep the expired holding for retry")

        # 1a. Confirmed `discord.NotFound` from `fetch_member` retires the expired holding
        guild.fetch_member = AsyncMock(side_effect=_make_not_found())
        sweep_404 = await TO.cleanup_expired_holdings(bot)
        self.assertEqual(sweep_404["removed"], 1, str(sweep_404))
        self.assertEqual(sweep_404["kept"], 0, str(sweep_404))
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (hid,)),
            [(0,)],
            "Confirmed discord.NotFound must retire the expired holding")
        self.assertIsNone(await get_equipped(GUILD, USER))
        self.assertEqual(
            [e["item_name"] for e in await get_inventory(GUILD, USER, include_empty=False)],
            ["UnrelatedOwnedRole"],
            "Retiring departed member's expired holding must not clear unrelated ownership")

        # ── Part 4: Departed member row must not starve newer expired holdings under `limit=1` ──
        departed_uid = USER + 777
        active_uid = USER + 888
        active_member = member(active_uid)
        active_member.roles = [r_active]

        async def _rem_active(r, reason=None):
            active_member.roles = [x for x in active_member.roles if x.id != r.id]

        active_member.remove_roles = AsyncMock(side_effect=_rem_active)

        h_old_departed = await TO.grant_timed_holding(
            GUILD, departed_uid, "OldDepartedHolding",
            expires_at="1999-01-01T00:00:00+00:00",
            item_type="temp_role", role_id=741)
        h_new_active = await TO.grant_timed_holding(
            GUILD, active_uid, "NewerActiveHolding",
            expires_at="2001-01-01T00:00:00+00:00",
            item_type="temp_role", role_id=743)

        guild.get_member = lambda uid: active_member if int(uid) == active_uid else None

        async def _fetch_by_uid(uid):
            if int(uid) == departed_uid:
                raise _make_not_found()
            return active_member

        guild.fetch_member = AsyncMock(side_effect=_fetch_by_uid)

        # Tick 1 with `limit=1` retires the older departed-member holding
        tick1 = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(tick1["removed"], 1, str(tick1))
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (h_old_departed,)),
            [(0,)])
        # Tick 2 with `limit=1` now reaches and retires the newer active member's holding!
        tick2 = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(tick2["removed"], 1, str(tick2))
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (h_new_active,)),
            [(0,)])
        active_member.remove_roles.assert_awaited_once()
        self.assertNotIn(r_active, active_member.roles)

        # ── Part 3: Late-delivery rollback after member has left the guild ──
        execute("DELETE FROM inventory_items WHERE guild_id=? AND user_id=?", (GUILD, USER))
        timed_shop_id = shop_item(
            "color", price=100, name="DepartedLateTimed",
            role_id=744, duration_hours=24, stock=5)
        perm_shop_id = shop_item(
            "color", price=100, name="DepartedLatePerm",
            role_id=745, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase

        def _make_departed_after_delivery_ctx(granted_role):
            itx, live_m, bot_l, guild_l = purchase_ctx(granted_role=granted_role)
            guild_l.get_role = lambda rid: roles_map.get(int(rid))
            departed_flag = {"left": False}

            async def _add_r(r, reason=None):
                if r not in live_m.roles:
                    live_m.roles.append(r)

            live_m.add_roles = AsyncMock(side_effect=_add_r)
            live_m.remove_roles = AsyncMock()

            stale_itx_user = member()
            stale_itx_user.roles = [granted_role]
            stale_itx_user.remove_roles = AsyncMock(
                side_effect=AssertionError("must not call remove_roles on stale interaction.user after NotFound"))
            itx.user = stale_itx_user

            guild_l.get_member = lambda uid: (
                None if departed_flag["left"] else (live_m if int(uid) == USER else None)
            )

            async def _fetch_m(uid):
                if departed_flag["left"]:
                    raise _make_not_found()
                return live_m

            guild_l.fetch_member = AsyncMock(side_effect=_fetch_m)
            return itx, stale_itx_user, live_m, bot_l, guild_l, departed_flag

        itx_lt, stale_lt, live_lt, _, _, flag_lt = _make_departed_after_delivery_ctx(r_late_t)
        itx_lp, stale_lp, live_lp, _, _, flag_lp = _make_departed_after_delivery_ctx(r_late_p)
        active_flag = {"ref": flag_lt}

        async def _deliver_then_depart_and_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            active_flag["ref"]["left"] = True
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="refunded after member left guild")
            return res

        shop_mod._deliver_purchase = _deliver_then_depart_and_refund
        try:
            # 3a. Timed `h:` late-delivery rollback when member has departed
            active_flag["ref"] = flag_lt
            await process_purchase(itx_lt, timed_shop_id)
            live_lt.remove_roles.assert_not_awaited()
            stale_lt.remove_roles.assert_not_awaited()
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)),
                [(0,)],
                "Timed h: late rollback for departed member must retire the holding without endless retry")
            op_lt = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            self.assertFalse(bool((await TO.get_purchase(op_lt)).get("delivery_rollback")))

            # 3b. Permanent role late-delivery rollback when member has departed:
            # inventory rollback and equip reconciliation complete, and no retry marker is left!
            active_flag["ref"] = flag_lp
            await process_purchase(itx_lp, perm_shop_id)
            live_lp.remove_roles.assert_not_awaited()
            stale_lp.remove_roles.assert_not_awaited()
            self.assertEqual(
                [e for e in await get_inventory(GUILD, USER, include_empty=False)
                 if e["item_name"] == "DepartedLatePerm"],
                [],
                "Departed member late rollback must still roll back the permanent role inventory grant")
            self.assertIsNone(await get_equipped(GUILD, USER))
            self.assertEqual(
                rows("SELECT COUNT(*) FROM timed_holdings WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)),
                [(0,)],
                "Departed member permanent-role late rollback must not leave an endless retry marker")
            op_lp = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            self.assertFalse(bool((await TO.get_purchase(op_lp)).get("delivery_rollback")))
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_fix_n2_descriptor_persistence_failure_vs_durable_restart_recovery(self):
        """Fix N2 regression:
        1. When saving the `delivery_rollback` descriptor fails AND inventory rollback
           also fails, `record_pending_delivery_rollback` returns `False`,
           `compensate_late_delivery` reports `durable_persisted=False`, and
           `process_purchase` reports `"Manual recovery required"` with the operation ID
           (never claiming `"Rollback pending reconciliation"` or promising automatic
           recovery after restart), without altering the purchase status to trigger a
           second refund.
        2. When descriptor persistence succeeds and inventory rollback fails, a
           simulated process restart (`_PENDING_DELIVERY_ROLLBACKS.clear()`) followed
           by `reconcile_purchases(bot=bot)` recovers from the durable SQLite
           descriptor and completes both inventory rollback and Discord role removal.
        """
        import sqlite3
        import cogs.shop as shop_mod
        import utils.reward_engine as re_mod
        from cogs.shop import process_purchase
        from utils.inventory import get_inventory

        r_n2_fail = role_stub(751, "N2PersistFailColor")
        r_n2_ok = role_stub(752, "N2DurableRestartColor")
        roles_map = {751: r_n2_fail, 752: r_n2_ok}

        item_n2_fail = shop_item(
            "color", price=100, name="N2PersistFailColor",
            role_id=751, duration_hours=None, stock=5)
        item_n2_ok = shop_item(
            "color", price=100, name="N2DurableRestartColor",
            role_id=752, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase
        real_update_purchase = TO.update_purchase
        real_rollback_role = re_mod.rollback_role_inventory_grant

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="late refund for N2 test")
            return res

        async def _failing_update_delivery_rollback(op_id, **kwargs):
            if kwargs.get("delivery_rollback") is not None and kwargs.get("delivery_rollback") != "":
                raise sqlite3.OperationalError("simulated disk error writing delivery_rollback")
            return await real_update_purchase(op_id, **kwargs)

        async def _failing_inventory_rollback(*args, **kwargs):
            return False

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            # ── Case 1: Descriptor persistence fails AND inventory rollback fails ──
            itx1, person1, bot1, guild1 = purchase_ctx(granted_role=r_n2_fail)
            guild1.get_role = lambda rid: roles_map.get(int(rid))

            async def _add1(r, reason=None):
                if r not in person1.roles:
                    person1.roles.append(r)

            person1.add_roles = AsyncMock(side_effect=_add1)
            person1.remove_roles = AsyncMock()

            TO.update_purchase = _failing_update_delivery_rollback
            re_mod.rollback_role_inventory_grant = _failing_inventory_rollback
            try:
                await process_purchase(itx1, item_n2_fail)
            finally:
                TO.update_purchase = real_update_purchase
                re_mod.rollback_role_inventory_grant = real_rollback_role

            op_fail = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            op_row_fail = await TO.get_purchase(op_fail)
            self.assertEqual(op_row_fail["status"], TO.PURCHASE_REFUNDED)
            self.assertEqual(op_row_fail["refunded"], 1)
            self.assertIsNone(
                op_row_fail.get("delivery_rollback"),
                "DB delivery_rollback column is NULL because descriptor persistence failed")

            embed1 = itx1.response.send_message.await_args.kwargs["embed"]
            field_names1 = [f.name or "" for f in embed1.fields]
            field_values1 = "\n".join(f.value or "" for f in embed1.fields)
            self.assertIn(
                "Manual recovery required",
                field_names1,
                f"Must report Manual recovery required when descriptor write failed; got {field_names1}")
            self.assertNotIn(
                "Rollback pending reconciliation",
                field_names1,
                "Must NOT claim Rollback pending reconciliation when durable descriptor write failed")
            self.assertNotIn(
                "queued for automatic reconciliation",
                field_values1,
                "Must NOT claim rollback is queued for automatic reconciliation when persistence failed")
            self.assertIn(op_fail, field_values1)
            self.assertIn("not guaranteed", field_values1)

            # In-memory retry before restart still works safely and does not double-refund
            balance_before = rows(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (GUILD, USER))[0][0]
            rec_in_mem = await TO.reconcile_purchases(bot=bot1)
            self.assertGreaterEqual(rec_in_mem["completed"], 1)
            balance_after = rows(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (GUILD, USER))[0][0]
            self.assertEqual(
                balance_after, balance_before,
                "Reconciling incomplete delivery rollback must never trigger a second refund")

            # ── Case 2: Normal persisted descriptor survives simulated process restart ──
            itx2, person2, bot2, guild2 = purchase_ctx(granted_role=r_n2_ok)
            guild2.get_role = lambda rid: roles_map.get(int(rid))

            async def _add2(r, reason=None):
                if r not in person2.roles:
                    person2.roles.append(r)

            async def _rem2(r, reason=None):
                person2.roles = [x for x in person2.roles if x.id != r.id]

            person2.add_roles = AsyncMock(side_effect=_add2)
            person2.remove_roles = AsyncMock(side_effect=_rem2)

            re_mod.rollback_role_inventory_grant = _failing_inventory_rollback
            try:
                await process_purchase(itx2, item_n2_ok)
            finally:
                re_mod.rollback_role_inventory_grant = real_rollback_role

            op_ok = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            op_row_ok = await TO.get_purchase(op_ok)
            self.assertTrue(
                bool(op_row_ok.get("delivery_rollback")),
                "Durable descriptor must be saved in shop_purchase_operations when persistence succeeds")
            embed2 = itx2.response.send_message.await_args.kwargs["embed"]
            self.assertIn(
                "Rollback pending reconciliation",
                [f.name or "" for f in embed2.fields],
                "When durable persistence succeeded, embed may report Rollback pending reconciliation")

            # Simulate a process restart by wiping the in-memory dictionary
            TO._PENDING_DELIVERY_ROLLBACKS.clear()

            # Reconcile after simulated restart: loads descriptor from SQLite and finishes recovery!
            rec_restart = await TO.reconcile_purchases(bot=bot2)
            self.assertGreaterEqual(rec_restart["completed"], 1, str(rec_restart))
            self.assertEqual(
                [e for e in await get_inventory(GUILD, USER, include_empty=False)
                 if e["item_name"] == "N2DurableRestartColor"],
                [],
                "Durable retry after simulated restart must roll back the inventory item")
            person2.remove_roles.assert_awaited_once()
            self.assertNotIn(r_n2_ok, person2.roles)
            self.assertFalse(bool((await TO.get_purchase(op_ok)).get("delivery_rollback")))
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_p1_stackable_item_descriptor_clear_failure_no_replay_after_restart(self):
        """Regression P1: Failed final `delivery_rollback=""` clear on a late-refunded
        stackable item must NOT deduct a second unit from subsequent legitimate grants
        after in-memory state is cleared and `reconcile_purchases()` runs."""
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase
        from utils.inventory import give_item, get_inventory

        item_p1 = shop_item(
            "title", price=250, name="P1Title",
            role_id=None, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase
        real_update_purchase = TO.update_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="simulated timeout before finalization")
            return res

        clear_attempts = {"count": 0}

        async def _fail_descriptor_clear(op_id, **kwargs):
            if kwargs.get("delivery_rollback") == "":
                clear_attempts["count"] += 1
                if clear_attempts["count"] == 1:
                    raise RuntimeError("simulated DB failure clearing delivery_rollback")
            return await real_update_purchase(op_id, **kwargs)

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            itx, person, bot, guild = purchase_ctx()
            TO.update_purchase = _fail_descriptor_clear
            try:
                await process_purchase(itx, item_p1)
            finally:
                TO.update_purchase = real_update_purchase

            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            op_row = await TO.get_purchase(op_id)
            self.assertEqual(op_row["status"], TO.PURCHASE_REFUNDED)
            self.assertEqual(op_row["refunded"], 1)
            self.assertTrue(
                bool(op_row.get("delivery_rollback")),
                "Failed clear leaves delivery_rollback populated in SQLite",
            )
            saved_spec = json.loads(op_row["delivery_rollback"])
            self.assertTrue(
                saved_spec.get("inventory_rolled_back"),
                "Atomic item rollback must durably persist inventory_rolled_back=True before clear",
            )
            # Late-delivered unit was rolled back
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P1Title'",
                    (GUILD, USER),
                ),
                [],
            )
            balance_after_refund = rows(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (GUILD, USER),
            )[0][0]

            # Grant 2 legitimate new units of the same stackable item
            await give_item(GUILD, USER, "P1Title", 2, item_type="title", source="shop")
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P1Title'",
                    (GUILD, USER),
                ),
                [(2,)],
            )

            # Simulate a process restart by clearing in-memory retry state
            TO._PENDING_DELIVERY_ROLLBACKS.clear()

            # Run startup/periodic reconciliation
            rec = await TO.reconcile_purchases(bot=bot)
            self.assertGreaterEqual(rec["completed"], 1, str(rec))

            # Both legitimate units remain intact
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P1Title'",
                    (GUILD, USER),
                ),
                [(2,)],
                "Reconciling after failed descriptor clear must not deduct a unit from legitimate grants",
            )
            inv_entries = [
                e for e in await get_inventory(GUILD, USER, include_empty=False)
                if e["item_name"] == "P1Title"
            ]
            self.assertEqual(len(inv_entries), 1)
            self.assertEqual(inv_entries[0]["quantity"], 2)

            # No second refund occurred
            self.assertEqual(
                rows(
                    "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                    (GUILD, USER),
                )[0][0],
                balance_after_refund,
                "Reconciliation must never issue a second refund",
            )

            # Stale descriptor is now resolved
            op_after = await TO.get_purchase(op_id)
            self.assertFalse(
                bool(op_after.get("delivery_rollback")),
                f"Expected delivery_rollback to be cleared, got {op_after.get('delivery_rollback')!r}",
            )
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_p2_stackable_item_atomic_rollback_and_progress_persistence_failure(self):
        """Regression P2: A failure between stackable-item inventory rollback and
        durable progress persistence rolls back the SQLite transaction atomically,
        and preserves both independent units and `reward_grants` provenance across retries."""
        import sqlite3
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase
        from utils.inventory import give_item

        # Seed 1 provenance-protected unit + 1 pre-existing independent shop unit (total = 2)
        await give_item(GUILD, USER, "P2AtomicPotion", 1, item_type="potion", source="level_claim")
        execute(
            "INSERT INTO reward_grants (grant_id, guild_id, user_id, claim_id, reward_level, track, kind, item_name, quantity, remaining_quantity, operation_id, created_at) "
            "VALUES ('grant-p2-1', ?, ?, 902, 5, 'shop', 'inventory', 'P2AtomicPotion', 1, 1, 'op-lvl-p2', '2026-01-01T00:00:00+00:00')",
            (GUILD, USER),
        )
        await give_item(GUILD, USER, "P2AtomicPotion", 1, item_type="potion", source="shop")
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P2AtomicPotion'",
                (GUILD, USER),
            ),
            [(2,)],
        )

        item_p2 = shop_item(
            "potion", price=100, name="P2AtomicPotion",
            duration_hours=2, multiplier=2.0, stock=5)

        real_deliver = shop_mod._deliver_purchase
        real_update_purchase = TO.update_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="simulated timeout before finalization")
            return res

        tx_progress_fail_once = {"failed": False}

        async def _fail_tx_progress_once(op_id, **kwargs):
            raw_dr = kwargs.get("delivery_rollback")
            if (
                kwargs.get("db") is not None
                and raw_dr
                and json.loads(raw_dr).get("inventory_rolled_back")
                and not tx_progress_fail_once["failed"]
            ):
                tx_progress_fail_once["failed"] = True
                raise sqlite3.OperationalError("simulated progress write failure inside atomic tx")
            return await real_update_purchase(op_id, **kwargs)

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            itx, person, bot, guild = purchase_ctx()
            TO.update_purchase = _fail_tx_progress_once
            try:
                await process_purchase(itx, item_p2)
            finally:
                TO.update_purchase = real_update_purchase

            op_id = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            op_row = await TO.get_purchase(op_id)
            # Because the progress write inside the shared transaction failed,
            # the inventory decrement was rolled back atomically (quantity stays 3:
            # 2 pre-existing + 1 late-delivered) and inventory_rolled_back is False.
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P2AtomicPotion'",
                    (GUILD, USER),
                ),
                [(3,)],
                "Failed progress write inside atomic tx must roll back the inventory decrement",
            )
            saved_spec = json.loads(op_row["delivery_rollback"])
            self.assertFalse(saved_spec.get("inventory_rolled_back"))

            # Grant 2 more legitimate independent units before restart reconciliation (total = 5)
            await give_item(GUILD, USER, "P2AtomicPotion", 2, item_type="potion", source="shop")
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P2AtomicPotion'",
                    (GUILD, USER),
                ),
                [(5,)],
            )

            # Simulate restart and reconcile: removes exactly the 1 late-delivered unit (5 -> 4)
            TO._PENDING_DELIVERY_ROLLBACKS.clear()
            rec1 = await TO.reconcile_purchases(bot=bot)
            self.assertGreaterEqual(rec1["completed"], 1, str(rec1))
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P2AtomicPotion'",
                    (GUILD, USER),
                ),
                [(4,)],
                "Reconciliation must remove only the 1 late-delivered unit and preserve all 4 legitimate units",
            )
            self.assertEqual(
                rows(
                    "SELECT remaining_quantity FROM reward_grants WHERE guild_id=? AND user_id=? AND item_name='P2AtomicPotion'",
                    (GUILD, USER),
                ),
                [(1,)],
                "Reward provenance must remain completely intact",
            )
            self.assertFalse(bool((await TO.get_purchase(op_id)).get("delivery_rollback")))
        finally:
            shop_mod._deliver_purchase = real_deliver

    async def test_p3_deleted_discord_role_retires_late_rollback_marker_without_stalling_cleanup(self):
        """Regression P3: When a Discord role no longer exists in the guild,
        late-rollback retry markers are retired only after inventory and equip
        compensation have completed, without inferring deletion from lookup errors
        and without stalling `cleanup_expired_holdings(bot, limit=1)`."""
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase

        r_del = role_stub(98031, "DeletedLateColor")
        r_next = role_stub(98032, "NextExpiredRole")
        roles_map = {r_del.id: r_del, r_next.id: r_next}

        item_p3 = shop_item(
            "color", price=200, name="DeletedLateColor",
            role_id=r_del.id, duration_hours=None, stock=5)

        real_deliver = shop_mod._deliver_purchase

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="simulated timeout before finalization")
            return res

        shop_mod._deliver_purchase = _deliver_then_refund
        try:
            itx, person, bot, guild = purchase_ctx(granted_role=r_del)
            guild.get_role = lambda rid: roles_map.get(int(rid))

            async def _add(r, reason=None):
                if r not in person.roles:
                    person.roles.append(r)

            person.add_roles = AsyncMock(side_effect=_add)
            person.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 503 during initial rollback"))

            await process_purchase(itx, item_p3)
        finally:
            shop_mod._deliver_purchase = real_deliver

        op_id = rows(
            "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
        )[0][0]
        op_row = await TO.get_purchase(op_id)
        spec = json.loads(op_row["delivery_rollback"])
        self.assertTrue(spec["inventory_rolled_back"])
        self.assertTrue(spec["equip_reconciled"])
        self.assertFalse(spec["role_reconciled"])
        marker_key = spec["retry_entry_key"]
        self.assertTrue(marker_key.startswith("h:"))

        # Add a second normal expired holding behind the 1970 late-rollback marker
        person.roles.append(r_next)
        h2_id = await TO.grant_timed_holding(
            GUILD,
            USER,
            "NextExpiredRole",
            expires_at="2000-01-01T00:00:00+00:00",
            role_id=r_next.id,
            category="Color",
        )

        # 1) Cache miss (`get_role` returns None) WITHOUT usable HTTP confirmation:
        # late-rollback marker MUST remain (`removed == 0, kept == 1`).
        roles_map.pop(r_del.id, None)
        res_cache_miss = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(res_cache_miss["removed"], 0, str(res_cache_miss))
        self.assertEqual(res_cache_miss["kept"], 1, str(res_cache_miss))
        self.assertTrue(bool((await TO.get_purchase(op_id)).get("delivery_rollback")))

        # 2) `fetch_role()` raising a transient HTTP error (503) or Unknown Guild (10004):
        # never infer role deletion; marker MUST remain.
        import discord
        resp_503 = SimpleNamespace(status=503, reason="Service Unavailable")
        resp_404 = SimpleNamespace(status=404, reason="Not Found")

        async def _fetch_role_503(rid):
            raise discord.HTTPException(resp_503, "Temporary Discord outage")

        guild.fetch_role = _fetch_role_503
        res_http_err = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(res_http_err["removed"], 0, str(res_http_err))
        self.assertEqual(res_http_err["kept"], 1, str(res_http_err))
        self.assertTrue(
            any("role lookup failed" in e for e in res_http_err["errors"]),
            str(res_http_err),
        )

        async def _fetch_role_unknown_guild(rid):
            raise discord.NotFound(resp_404, {"code": 10004, "message": "Unknown Guild"})

        guild.fetch_role = _fetch_role_unknown_guild
        res_unknown_guild = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(res_unknown_guild["removed"], 0, str(res_unknown_guild))
        self.assertEqual(res_unknown_guild["kept"], 1, str(res_unknown_guild))
        self.assertTrue(
            any("role lookup failed" in e for e in res_unknown_guild["errors"]),
            str(res_unknown_guild),
        )

        # 3) Even when `fetch_role()` confirms Unknown Role (`10011`), if
        # `inventory_rolled_back` is False on the linked op, `expire_one` /
        # `cleanup_expired_holdings` MUST NOT retire the marker.
        async def _fetch_role_unknown_role(rid):
            if int(rid) == r_del.id:
                raise discord.NotFound(resp_404, {"code": 10011, "message": "Unknown Role"})
            return roles_map.get(int(rid))

        guild.fetch_role = _fetch_role_unknown_role
        import utils.reward_engine as re_mod
        unready_spec = dict(spec, inventory_rolled_back=False)
        await TO.update_purchase(op_id, delivery_rollback=json.dumps(unready_spec))
        TO._PENDING_DELIVERY_ROLLBACKS[op_id] = dict(unready_spec)
        real_rollback_role = re_mod.rollback_role_inventory_grant

        async def _fail_role_inv_rollback(*args, **kwargs):
            return False

        re_mod.rollback_role_inventory_grant = _fail_role_inv_rollback
        try:
            res_unready = await TO.cleanup_expired_holdings(bot, limit=1)
        finally:
            re_mod.rollback_role_inventory_grant = real_rollback_role
        self.assertEqual(res_unready["removed"], 0)
        self.assertGreaterEqual(res_unready["kept"], 1)
        self.assertTrue(
            any("inventory or equip rollback pending" in e for e in res_unready["errors"]),
            str(res_unready),
        )

        # Restore completed inventory/equip flags
        await TO.update_purchase(op_id, delivery_rollback=json.dumps(spec))
        TO._PENDING_DELIVERY_ROLLBACKS[op_id] = dict(spec)

        # 4) If `get_role` itself raises a transient error, never infer deletion: keep the marker
        def _broken_get_role(rid):
            if int(rid) == r_del.id:
                raise RuntimeError("transient cache lookup failure")
            return roles_map.get(int(rid))

        guild.get_role = _broken_get_role
        res_err = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(res_err["removed"], 0)
        self.assertEqual(res_err["kept"], 1)
        self.assertTrue(
            any("role lookup failed" in e for e in res_err["errors"]),
            str(res_err),
        )

        # 5) Unknown Role (`10011`) from `fetch_role()` once prerequisites are satisfied:
        # `cleanup_expired_holdings(bot, limit=1)` retires the late-rollback marker
        # and clears the purchase journal's `delivery_rollback` descriptor!
        guild.get_role = lambda rid: roles_map.get(int(rid))
        guild.fetch_role = _fetch_role_unknown_role
        res_deleted = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(res_deleted["removed"], 1, str(res_deleted))
        self.assertEqual(res_deleted["kept"], 0, str(res_deleted))
        self.assertEqual(res_deleted["errors"], [])
        self.assertFalse(bool((await TO.get_purchase(op_id)).get("delivery_rollback")))
        self.assertNotIn(r_del, person.roles)

        # 6) Next `cleanup_expired_holdings(bot, limit=1)` is no longer blocked and retires h2_id!
        async def _rem_next(r, reason=None):
            person.roles = [x for x in person.roles if x.id != r.id]

        person.remove_roles = AsyncMock(side_effect=_rem_next)
        res_next = await TO.cleanup_expired_holdings(bot, limit=1)
        self.assertEqual(res_next["removed"], 1, str(res_next))
        self.assertEqual(res_next["kept"], 0, str(res_next))
        self.assertEqual(rows("SELECT id FROM timed_holdings WHERE id=?", (h2_id,)), [])

        # 7) Also verify `fetch_roles()` omitting the role ID confirms deletion (`role_deleted=True`)
        delattr(guild, "fetch_role")

        async def _fetch_roles_omitting_deleted():
            return [r_next]

        guild.fetch_roles = _fetch_roles_omitting_deleted
        resolved_role, is_deleted, lookup_err = await TO._resolve_guild_role(guild, r_del.id)
        self.assertIsNone(resolved_role)
        self.assertTrue(is_deleted)
        self.assertIsNone(lookup_err)

    async def test_p4_stackable_item_concurrent_and_stale_spec_replay_never_double_deducts(self):
        """Regression P4: Concurrent `compensate_late_delivery` calls and replayed stale
        specifications never deduct a second (legitimate) stackable unit or resurrect
        a cleared `delivery_rollback` descriptor."""
        import asyncio
        import sqlite3
        import cogs.shop as shop_mod
        from cogs.shop import process_purchase
        from utils.inventory import give_item

        item_p4 = shop_item(
            "potion", price=100, name="P4RacePotion",
            duration_hours=2, multiplier=2.0, stock=5)

        real_deliver = shop_mod._deliver_purchase
        real_compensate = TO.compensate_late_delivery

        async def _deliver_then_refund(*args, **kwargs):
            res = await real_deliver(*args, **kwargs)
            last_op = rows(
                "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
            )[0][0]
            execute(
                "UPDATE shop_purchase_operations SET status=? WHERE op_id=?",
                (TO.PURCHASE_UNCERTAIN, last_op))
            await TO.resolve_purchase_operation(
                last_op, action="refund", confirmed=True,
                note="simulated timeout before finalization")
            return res

        async def _defer_initial_compensation(op_id, spec=None, **kwargs):
            return {
                "complete": False,
                "durable_persisted": True,
                "error": "deferred to exercise concurrent race",
            }

        shop_mod._deliver_purchase = _deliver_then_refund
        TO.compensate_late_delivery = _defer_initial_compensation
        try:
            itx, person, bot, guild = purchase_ctx()
            await process_purchase(itx, item_p4)
        finally:
            shop_mod._deliver_purchase = real_deliver
            TO.compensate_late_delivery = real_compensate

        op_id = rows(
            "SELECT op_id FROM shop_purchase_operations ORDER BY rowid DESC LIMIT 1"
        )[0][0]
        balance_after_refund = rows(
            "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        )[0][0]

        # Grant 2 legitimate units after the 1 late-delivered unit (total = 3; owed rollback = 1 -> final must be 2)
        await give_item(GUILD, USER, "P4RacePotion", 2, item_type="potion", source="shop")
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P4RacePotion'",
                (GUILD, USER),
            ),
            [(3,)],
        )

        op_row_initial = await TO.get_purchase(op_id)
        stale_spec = json.loads(op_row_initial["delivery_rollback"])
        self.assertFalse(stale_spec.get("inventory_rolled_back"))

        # (A) Coordinate 3 concurrent calls via asyncio.gather so all 3 read initial state before BEGIN IMMEDIATE
        real_get_purchase = TO.get_purchase
        entered = 0
        all_entered = asyncio.Event()

        async def _barrier_get_purchase(oid):
            nonlocal entered
            res = await real_get_purchase(oid)
            if str(oid) == str(op_id) and entered < 3:
                entered += 1
                if entered == 3:
                    all_entered.set()
                await all_entered.wait()
            return res

        TO.get_purchase = _barrier_get_purchase
        try:
            concurrent_results = await asyncio.gather(
                TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot),
                TO.compensate_late_delivery(op_id, None, bot=bot),
                TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot),
            )
        finally:
            TO.get_purchase = real_get_purchase

        self.assertEqual([r["complete"] for r in concurrent_results], [True, True, True])
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P4RacePotion'",
                (GUILD, USER),
            ),
            [(2,)],
            "3 concurrent compensations must deduct only 1 unit and preserve both legitimate units",
        )
        self.assertIsNone((await TO.get_purchase(op_id)).get("delivery_rollback"))

        # (B) Replay previously captured stale specification after completion & simulated restart:
        # must not mutate inventory or resurrect the cleared descriptor.
        TO._PENDING_DELIVERY_ROLLBACKS.clear()
        replay1 = await TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot)
        replay2 = await TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot)
        self.assertTrue(replay1["complete"])
        self.assertTrue(replay2["complete"])
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P4RacePotion'",
                (GUILD, USER),
            ),
            [(2,)],
            "Replaying a stale specification after completion must not deduct legitimate units",
        )
        self.assertIsNone((await TO.get_purchase(op_id)).get("delivery_rollback"))
        self.assertNotIn(op_id, TO._PENDING_DELIVERY_ROLLBACKS)
        self.assertEqual(
            rows(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (GUILD, USER),
            )[0][0],
            balance_after_refund,
            "Stale replay must never issue a second refund",
        )

        # (C) Missing/unpersisted descriptor (`SQL NULL` without in-memory `_durable_unpersisted`)
        # or malformed non-empty descriptor must fail closed without mutating inventory or recreating descriptor.
        execute("UPDATE shop_purchase_operations SET delivery_rollback=NULL WHERE op_id=?", (op_id,))
        TO._PENDING_DELIVERY_ROLLBACKS.clear()
        absent_res = await TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot)
        self.assertFalse(absent_res["complete"])
        self.assertFalse(absent_res["durable_persisted"])
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P4RacePotion'",
                (GUILD, USER),
            ),
            [(2,)],
        )
        self.assertIsNone((await TO.get_purchase(op_id)).get("delivery_rollback"))

        execute("UPDATE shop_purchase_operations SET delivery_rollback='{corrupt-json' WHERE op_id=?", (op_id,))
        corrupt_res = await TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot)
        self.assertFalse(corrupt_res["complete"])
        self.assertFalse(corrupt_res["durable_persisted"])
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='P4RacePotion'",
                (GUILD, USER),
            ),
            [(2,)],
        )

    async def test_p5_late_rollback_prereqs_complete_fails_closed_on_corrupt_json_or_lookup_failure(self):
        """Regression P5: `_late_rollback_prereqs_complete()` returns False when
        stored non-empty rollback JSON is malformed or when rollback state cannot
        be read, while returning True once prerequisites are satisfied or no
        rollback is pending."""
        op_id = await TO.open_purchase(
            guild_id=GUILD,
            user_id=USER,
            item_id=501,
            item_name="P5Role",
            currency="main",
            amount=100,
        )
        await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)

        # 1) Confirmed no pending rollback -> True
        self.assertTrue(await TO._late_rollback_prereqs_complete(op_id))
        self.assertTrue(await TO._late_rollback_prereqs_complete(None, entry_key="h:9999"))

        # 2) Malformed non-empty JSON in delivery_rollback -> False (both op_id and entry_key paths)
        execute(
            "UPDATE shop_purchase_operations SET delivery_rollback='{not-valid-json' WHERE op_id=?",
            (op_id,),
        )
        TO._PENDING_DELIVERY_ROLLBACKS.clear()
        self.assertFalse(await TO._late_rollback_prereqs_complete(op_id))
        self.assertFalse(await TO._late_rollback_prereqs_complete(None, entry_key="h:9999"))

        # 3) Lookup/read failure (`get_purchase` raising or missing op_id row) -> False
        self.assertFalse(await TO._late_rollback_prereqs_complete("nonexistent-op-id"))
        real_get_purchase = TO.get_purchase

        async def _failing_get_purchase(oid):
            raise RuntimeError("simulated DB read error")

        TO.get_purchase = _failing_get_purchase
        try:
            self.assertFalse(await TO._late_rollback_prereqs_complete(op_id))
        finally:
            TO.get_purchase = real_get_purchase

        # 4) Valid pending rollback with satisfied prerequisites -> True, and cleared rollback -> True
        valid_spec = {
            "guild_id": GUILD,
            "user_id": USER,
            "item_name": "P5Role",
            "internal_type": "role",
            "retry_entry_key": "h:9999",
            "inventory_rolled_back": True,
            "equip_reconciled": True,
            "role_reconciled": False,
        }
        await TO.update_purchase(op_id, delivery_rollback=json.dumps(valid_spec))
        self.assertTrue(await TO._late_rollback_prereqs_complete(op_id))
        self.assertTrue(await TO._late_rollback_prereqs_complete(None, entry_key="h:9999"))

        await TO.update_purchase(op_id, delivery_rollback="")
        self.assertTrue(await TO._late_rollback_prereqs_complete(op_id))
        self.assertTrue(await TO._late_rollback_prereqs_complete(None, entry_key="h:9999"))


    # ── Audit P6–P13: full compensation-branch crash/race matrix ─────────

    async def test_p6_permanent_role_concurrent_compensation_preserves_legitimate_units(self):
        """Regression P6: two compensations that both read `inventory_rolled_back=False`
        must not each decrement the permanent-role inventory row.

        The durable journal state is re-read inside `BEGIN IMMEDIATE` by
        `rollback_role_inventory_grant(..., tx_guard=...)`, and the progress flag is
        written on that same transaction, so the second invocation observes the first
        one's committed progress and performs no inventory mutation.
        """
        import asyncio
        import utils.reward_engine as re_mod
        from utils.inventory import give_item

        name = "P6PermRoleColor"
        rid = 96001
        role = role_stub(rid, name)
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        person.roles.append(role)
        guild.get_role = lambda r: role if int(r) == rid else None
        person.remove_roles = AsyncMock()

        await give_item(GUILD, USER, name, 1, item_type="role", source="shop")
        delivered = rows(
            "SELECT id, quantity, item_type, source, metadata, created_at, updated_at "
            "FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name=?",
            (GUILD, USER, name))[0]
        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7701, item_name=name,
            currency="balance", amount=200)
        await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)
        stale_spec = {
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7701,
            "item_name": name,
            "internal_type": "role",
            "category": "Color",
            "role_id": rid,
            "role_added": False,
            "prior_inventory": None,
            "delivered_inventory": list(delivered),
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
        }
        await TO.record_pending_delivery_rollback(op_id, stale_spec, bot=bot)
        balance_before = rows(
            "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
            (GUILD, USER))[0][0]

        # Two legitimate, independently granted units with reward provenance.
        await give_item(GUILD, USER, name, 2, item_type="role", source="level_claim")
        execute(
            "INSERT INTO reward_grants (grant_id, guild_id, user_id, claim_id, reward_level,"
            " track, kind, item_name, quantity, remaining_quantity, unattributed,"
            " operation_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
            ("p6-claim", GUILD, USER, 7701, 5, "levels", "inventory", name, 2, 2,
             "p6-op", "2026-01-01T00:00:00+00:00"))
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=?"
                 " AND item_name=?", (GUILD, USER, name)),
            [(3,)])

        real_gp = TO.get_purchase
        real_rb = re_mod.rollback_role_inventory_grant
        state = {"reads": 0, "rb": 0}
        both_read = asyncio.Event()
        first_done = asyncio.Event()

        async def _gp(oid):
            res = await real_gp(oid)
            if str(oid) == str(op_id) and state["reads"] < 2:
                state["reads"] += 1
                if state["reads"] == 2:
                    both_read.set()
                await both_read.wait()
            return res

        async def _rb(gid, uid, iname, **kw):
            state["rb"] += 1
            if state["rb"] == 1:
                ok = await real_rb(gid, uid, iname, **kw)
                first_done.set()
                return ok
            await first_done.wait()
            return await real_rb(gid, uid, iname, **kw)

        TO.get_purchase = _gp
        re_mod.rollback_role_inventory_grant = _rb
        try:
            res1, res2 = await asyncio.gather(
                TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot),
                TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot),
            )
        finally:
            TO.get_purchase = real_gp
            re_mod.rollback_role_inventory_grant = real_rb

        self.assertTrue(res1["complete"], str(res1))
        self.assertTrue(res2["complete"], str(res2))
        # Exactly ONE unit refunded (the late delivery); both legitimate units kept.
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=?"
                 " AND item_name=?", (GUILD, USER, name)),
            [(2,)],
            "concurrent permanent-role compensations deducted a legitimate unit")
        self.assertEqual(
            rows("SELECT remaining_quantity FROM reward_grants WHERE grant_id='p6-claim'"),
            [(2,)],
            "reward provenance for independently granted units was modified")
        self.assertEqual(
            rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER))[0][0],
            balance_before,
            "compensation changed the member's balance")
        # Assert the *raw* durable value: the normalized `delivery_rollback`
        # field is None for both SQL NULL and the cleared empty-string
        # sentinel, so it cannot prove which state was persisted.
        raw_after = (await TO.get_purchase(op_id))["_raw_delivery_rollback"]
        self.assertEqual(
            raw_after, "",
            f"durable descriptor was not cleared to the sentinel (raw={raw_after!r})")
        self.assertEqual(
            rows("SELECT delivery_rollback FROM shop_purchase_operations "
                 "WHERE op_id=?", (op_id,)),
            [("",)],
            "the persisted delivery_rollback column does not hold the sentinel")

    async def test_p7_xp_boost_stale_spec_replay_deletes_exactly_one_row(self):
        """Regression P7: a caller holding a stale specification must not delete a
        second XP-boost row that belongs to an independent (legitimate) grant."""
        exp = "2099-01-01T00:00:00+00:00"
        execute(
            "INSERT INTO leveling_active_boosts"
            " (guild_id, user_id, multiplier, expires_at, source)"
            " VALUES (?,?,2.0,?,'level_claim'), (?,?,2.0,?,'shop')",
            (GUILD, USER, exp, GUILD, USER, exp))
        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7702, item_name="P7RaceBoost",
            currency="balance", amount=100)
        await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)
        stale_spec = {
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7702,
            "item_name": "P7RaceBoost",
            "internal_type": "xp_boost",
            "boost_id": None,
            "boost_expires_at": exp,
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
        }
        await TO.record_pending_delivery_rollback(op_id, stale_spec, bot=None)

        # First invocation completes and durably clears the descriptor.
        res1 = await TO.compensate_late_delivery(op_id, dict(stale_spec))
        self.assertTrue(res1["complete"], str(res1))
        remaining = rows(
            "SELECT id, source FROM leveling_active_boosts"
            " WHERE guild_id=? AND user_id=?", (GUILD, USER))
        self.assertEqual(len(remaining), 1, f"first pass deleted too much: {remaining}")
        self.assertEqual(remaining[0][1], "level_claim")

        # Second invocation: a caller whose snapshot predates the completion.
        stale_row = dict(await TO.get_purchase(op_id))
        self.assertEqual(stale_row["_raw_delivery_rollback"], "")
        stale_row["_raw_delivery_rollback"] = json.dumps(stale_spec)
        stale_row["delivery_rollback"] = json.dumps(stale_spec)
        real_gp = TO.get_purchase
        calls = {"n": 0}

        async def _stale_once(oid):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(stale_row)
            return await real_gp(oid)

        TO.get_purchase = _stale_once
        try:
            res2 = await TO.compensate_late_delivery(op_id, dict(stale_spec))
        finally:
            TO.get_purchase = real_gp

        self.assertTrue(res2["complete"], str(res2))
        remaining = rows(
            "SELECT id, source FROM leveling_active_boosts"
            " WHERE guild_id=? AND user_id=?", (GUILD, USER))
        self.assertEqual(
            len(remaining), 1,
            f"a stale specification deleted a second boost row: {remaining}")
        self.assertEqual(remaining[0][1], "level_claim",
                         "the legitimate boost row was deleted")
        self.assertEqual(
            (await TO.get_purchase(op_id))["_raw_delivery_rollback"], "",
            "a stale invocation resurrected a cleared descriptor")

    async def test_p8_concurrent_role_rollback_creates_one_retry_marker(self):
        """Regression P8: concurrent role compensations must not each create a
        `1970` late-rollback retry marker for the same operation."""
        import asyncio

        rid = 96002
        role = role_stub(rid, "P8MarkerRole")
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        person.roles.append(role)
        guild.get_role = lambda r: role if int(r) == rid else None
        person.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 503"))

        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7703, item_name="P8MarkerRole",
            currency="balance", amount=100)
        await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)
        stale_spec = {
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7703,
            "item_name": "P8MarkerRole",
            "internal_type": "role",
            "category": "Color",
            "role_id": rid,
            "role_added": True,
            "prior_inventory": None,
            "delivered_inventory": None,
            "inventory_rolled_back": True,
            "equip_reconciled": True,
            "role_reconciled": False,
        }
        await TO.record_pending_delivery_rollback(op_id, stale_spec, bot=bot)

        real_gp = TO.get_purchase
        state = {"reads": 0}
        both_read = asyncio.Event()

        async def _gp(oid):
            res = await real_gp(oid)
            if str(oid) == str(op_id) and state["reads"] < 2:
                state["reads"] += 1
                if state["reads"] == 2:
                    both_read.set()
                await both_read.wait()
            return res

        TO.get_purchase = _gp
        try:
            await asyncio.gather(
                TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot),
                TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot),
            )
        finally:
            TO.get_purchase = real_gp

        markers = rows(
            "SELECT id, metadata FROM timed_holdings WHERE source='shop_late_rollback'")
        self.assertEqual(
            len(markers), 1,
            f"expected exactly one retry marker, found {markers}")
        spec_row = await TO.get_purchase(op_id)
        self.assertIsNotNone(spec_row.get("delivery_rollback"))
        persisted = json.loads(spec_row["delivery_rollback"])
        self.assertEqual(persisted.get("retry_entry_key"), f"h:{markers[0][0]}",
                         "durable descriptor does not reference the retry marker")

    async def test_p9_failing_late_markers_do_not_starve_ordinary_expiry_cleanup(self):
        """Regression P9: permanently failing late-rollback markers sort first in
        `expired_holdings`; a cleanup pass bounded by `limit` must still retire
        unrelated ordinary expired holdings instead of starving them."""
        rid_fail1, rid_fail2, rid_ok = 96010, 96011, 96012
        r_fail1 = role_stub(rid_fail1, "P9FailOne")
        r_fail2 = role_stub(rid_fail2, "P9FailTwo")
        r_ok = role_stub(rid_ok, "P9Ordinary")
        itx, person, bot, guild = purchase_ctx()
        roles_map = {rid_fail1: r_fail1, rid_fail2: r_fail2, rid_ok: r_ok}
        guild.get_role = lambda rid: roles_map.get(int(rid))
        person.roles.extend([r_fail1, r_fail2, r_ok])

        async def _selective_remove(role_obj, reason=None):
            if role_obj.id in (rid_fail1, rid_fail2):
                raise RuntimeError("Discord 503 on retry marker role")
            if role_obj in person.roles:
                person.roles.remove(role_obj)

        person.remove_roles = AsyncMock(side_effect=_selective_remove)

        for rid, name in ((rid_fail1, "P9FailOne"), (rid_fail2, "P9FailTwo")):
            await TO.grant_timed_holding(
                GUILD, USER, name,
                expires_at="1970-01-01T00:00:00+00:00",
                role_id=rid,
                source="shop_late_rollback",
                metadata={"late_rollback": True},
            )
        ordinary_id = await TO.grant_timed_holding(
            GUILD, USER, "P9Ordinary",
            expires_at="2000-01-01T00:00:00+00:00",
            role_id=rid_ok,
            source="shop")

        res = await TO.cleanup_expired_holdings(bot, limit=2)
        self.assertEqual(
            rows("SELECT id FROM timed_holdings WHERE id=?", (ordinary_id,)),
            [],
            f"ordinary expired holding was starved by failing late markers: {res}")
        self.assertGreaterEqual(res["removed"], 1, str(res))
        self.assertEqual(
            len(rows("SELECT id FROM timed_holdings WHERE source='shop_late_rollback'")),
            2,
            "failing late-rollback markers must be kept for retry")

    async def test_p10_unrelated_corrupt_descriptor_does_not_block_valid_marker(self):
        """P10: a corrupt `delivery_rollback` row must not authorize retiring its own
        marker, but must not block an unrelated marker whose own state is intact."""
        corrupt_op = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7710,
            item_name="P10CorruptItem", currency="balance", amount=50)
        await TO.update_purchase(
            corrupt_op, status=TO.PURCHASE_REFUNDED, refunded=1)
        execute(
            "UPDATE shop_purchase_operations SET delivery_rollback='{corrupt'"
            " WHERE op_id=?", (corrupt_op,))

        valid_op = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7711,
            item_name="P10ValidRole", currency="balance", amount=50)
        await TO.update_purchase(
            valid_op, status=TO.PURCHASE_REFUNDED, refunded=1,
            delivery_rollback=json.dumps({
                "guild_id": GUILD,
                "user_id": USER,
                "item_name": "P10ValidRole",
                "internal_type": "role",
                "retry_entry_key": "h:96050",
                "inventory_rolled_back": True,
                "equip_reconciled": True,
                "role_reconciled": False,
            }))
        TO._PENDING_DELIVERY_ROLLBACKS.clear()

        self.assertTrue(
            await TO._late_rollback_prereqs_complete(None, entry_key="h:96050"),
            "an unrelated corrupt descriptor blocked a valid, independently "
            "resolvable marker")

        # The corrupt descriptor must still block its OWN marker: same guild/user/item.
        execute(
            "UPDATE shop_purchase_operations SET item_name='P10CorruptItem'"
            " WHERE op_id=?", (corrupt_op,))
        blocker = await TO.grant_timed_holding(
            GUILD, USER, "P10CorruptItem",
            expires_at="1970-01-01T00:00:00+00:00",
            source="shop_late_rollback",
            metadata={"late_rollback": True, "op_id": corrupt_op})
        self.assertFalse(
            await TO._late_rollback_prereqs_complete(None, entry_key=f"h:{blocker}"),
            "a corrupt descriptor must not allow its own marker to be retired")
        self.assertFalse(
            await TO._late_rollback_prereqs_complete(corrupt_op),
            "a corrupt descriptor must fail closed on the op_id path")

    async def test_p11_stale_invocation_cannot_resurrect_cleared_descriptor(self):
        """Regression P11: an invocation started before a completed rollback cleared
        the descriptor must not write its stale specification back."""
        rid = 96020
        role = role_stub(rid, "P11StaleColor")
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        person.roles.append(role)
        guild.get_role = lambda r: role if int(r) == rid else None
        person.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 403"))

        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7720, item_name="P11StaleColor",
            currency="balance", amount=100)
        await TO.update_purchase(
            op_id, status=TO.PURCHASE_REFUNDED, refunded=1,
            delivery_rollback="")
        stale_row = {
            "op_id": op_id,
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7720,
            "item_name": "P11StaleColor",
            "currency": "balance",
            "amount": 100,
            "stock_reserved": 0,
            "charged": 1,
            "refunded": 1,
            "stock_returned": 0,
            "status": TO.PURCHASE_REFUNDED,
            "detail": None,
            "history_id": None,
            "delivery_rollback": json.dumps({
                "guild_id": GUILD,
                "user_id": USER,
                "item_id": 7720,
                "item_name": "P11StaleColor",
                "internal_type": "role",
                "role_id": rid,
                "role_added": True,
                "inventory_rolled_back": True,
                "equip_reconciled": True,
                "role_reconciled": False,
            }),
            "_raw_delivery_rollback": json.dumps({
                "guild_id": GUILD,
                "user_id": USER,
                "item_id": 7720,
                "item_name": "P11StaleColor",
                "internal_type": "role",
                "role_id": rid,
                "role_added": True,
                "inventory_rolled_back": True,
                "equip_reconciled": True,
                "role_reconciled": False,
            }),
        }

        real_gp = TO.get_purchase
        calls = {"n": 0}

        async def _stale_once(oid):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(stale_row)
            return await real_gp(oid)

        TO.get_purchase = _stale_once
        try:
            res = await TO.compensate_late_delivery(
                op_id, dict(json.loads(stale_row["delivery_rollback"])), bot=bot)
        finally:
            TO.get_purchase = real_gp

        self.assertTrue(res["complete"], str(res))
        self.assertEqual(
            (await TO.get_purchase(op_id))["_raw_delivery_rollback"], "",
            "a stale invocation resurrected an already-cleared descriptor")
        self.assertNotIn(op_id, TO._PENDING_DELIVERY_ROLLBACKS)
        self.assertEqual(
            rows("SELECT id FROM timed_holdings WHERE source='shop_late_rollback'"),
            [],
            "a stale invocation created a retry marker for completed work")

    async def test_p12_permanent_role_fails_closed_when_descriptor_absent(self):
        """Regression P12: when the durable descriptor is genuinely absent and no
        failed-persistence marker explains it, the permanent-role branch must not
        mutate inventory on the strength of a stale caller-supplied specification."""
        import utils.reward_engine as re_mod

        name = "P12AbsentDescriptorRole"
        rid = 96030
        role = role_stub(rid, name)
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        person.roles.append(role)
        guild.get_role = lambda r: role if int(r) == rid else None
        person.remove_roles = AsyncMock()

        from utils.inventory import give_item
        await give_item(GUILD, USER, name, 1, item_type="role", source="shop")
        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7730, item_name=name,
            currency="balance", amount=100)
        await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)
        # Durable descriptor is SQL NULL: nothing was ever persisted.
        self.assertIsNone(
            (await TO.get_purchase(op_id))["_raw_delivery_rollback"])
        stale_row = {
            "op_id": op_id,
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7730,
            "item_name": name,
            "currency": "balance",
            "amount": 100,
            "stock_reserved": 0,
            "charged": 1,
            "refunded": 1,
            "stock_returned": 0,
            "status": TO.PURCHASE_REFUNDED,
            "detail": None,
            "history_id": None,
            "delivery_rollback": json.dumps({
                "guild_id": GUILD,
                "user_id": USER,
                "item_id": 7730,
                "item_name": name,
                "internal_type": "role",
                "role_id": rid,
                "role_added": True,
                "inventory_rolled_back": False,
                "equip_reconciled": False,
                "role_reconciled": False,
            }),
        }
        stale_row["_raw_delivery_rollback"] = stale_row["delivery_rollback"]
        TO._PENDING_DELIVERY_ROLLBACKS.clear()

        real_gp = TO.get_purchase
        calls = {"n": 0}

        async def _stale_once(oid):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(stale_row)
            return await real_gp(oid)

        TO.get_purchase = _stale_once
        try:
            res = await TO.compensate_late_delivery(
                op_id, dict(json.loads(stale_row["delivery_rollback"])), bot=bot)
        finally:
            TO.get_purchase = real_gp

        self.assertFalse(res["complete"], str(res))
        self.assertIn("absent", str(res.get("error") or "").lower(), str(res))
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=?"
                 " AND item_name=?", (GUILD, USER, name)),
            [(1,)],
            "inventory was mutated without a durable rollback descriptor")
        self.assertIsNone(
            (await TO.get_purchase(op_id))["_raw_delivery_rollback"],
            "a rollback descriptor was fabricated from a stale specification")

    async def test_p13_role_lookup_only_confirms_deletion_on_unknown_role(self):
        """P13: `_resolve_guild_role` reports a deleted role only for Unknown Role
        (`10011`) or a successful `fetch_roles` that omits it — never for Unknown
        Guild, cache misses, permissions, rate limits, timeouts or transport errors."""
        import asyncio
        import discord

        async def _resolve(guild):
            return await TO._resolve_guild_role(guild, 96040)

        role = role_stub(96040, "P13Role")

        # 1) cache hit
        guild = SimpleNamespace(get_role=lambda rid: role)
        self.assertEqual(await _resolve(guild), (role, False, None))

        # 2) cache miss, no HTTP confirmation available
        guild = SimpleNamespace(get_role=lambda rid: None)
        obj, deleted, err = await _resolve(guild)
        self.assertIsNone(obj)
        self.assertFalse(deleted)
        self.assertIsNone(err)

        # 3) fetch_role returns the role
        async def _fetch_ok(rid):
            return role
        guild = SimpleNamespace(get_role=lambda rid: None, fetch_role=_fetch_ok)
        self.assertEqual(await _resolve(guild), (role, False, None))

        # 4) Unknown Role 10011 confirms deletion
        not_found_10011 = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10011, "message": "Unknown Role"})
        self.assertTrue(TO._is_role_not_found(not_found_10011))

        async def _fetch_unknown_role(rid):
            raise not_found_10011
        guild = SimpleNamespace(get_role=lambda rid: None,
                                fetch_role=_fetch_unknown_role)
        self.assertEqual(await _resolve(guild), (None, True, None))

        # 5) Unknown Guild 10004 does NOT confirm deletion
        not_found_10004 = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10004, "message": "Unknown Guild"})
        self.assertFalse(TO._is_role_not_found(not_found_10004))

        async def _fetch_unknown_guild(rid):
            raise not_found_10004
        guild = SimpleNamespace(get_role=lambda rid: None,
                                fetch_role=_fetch_unknown_guild)
        obj, deleted, err = await _resolve(guild)
        self.assertFalse(deleted)
        self.assertTrue(err)

        # 6) Forbidden / rate limited / server error / timeout / transport error
        transient = [
            discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                              "Missing Permissions"),
            discord.HTTPException(
                SimpleNamespace(status=429, reason="Too Many Requests"),
                "You are being rate limited"),
            discord.HTTPException(
                SimpleNamespace(status=500, reason="Internal Server Error"),
                "server error"),
            asyncio.TimeoutError(),
            OSError("transport error"),
        ]
        for exc in transient:
            async def _raise(rid, _exc=exc):
                raise _exc
            guild = SimpleNamespace(get_role=lambda rid: None, fetch_role=_raise)
            obj, deleted, err = await _resolve(guild)
            self.assertFalse(deleted, f"{type(exc).__name__} must not confirm deletion")
            self.assertTrue(err, f"{type(exc).__name__} must be reported as a lookup error")

        # 7) fetch_roles succeeding while omitting the role confirms deletion
        async def _fetch_roles_without(rid=None):
            return [role_stub(96041, "Other")]
        guild = SimpleNamespace(get_role=lambda rid: None,
                                fetch_roles=_fetch_roles_without)
        self.assertEqual(await _resolve(guild), (None, True, None))

        # 8) fetch_roles including the role resolves it
        async def _fetch_roles_with():
            return [role]
        guild = SimpleNamespace(get_role=lambda rid: None,
                                fetch_roles=_fetch_roles_with)
        self.assertEqual(await _resolve(guild), (role, False, None))

        # 9) Missing fetch methods after a cache miss never imply deletion
        guild = SimpleNamespace(get_role=lambda rid: None)
        obj, deleted, err = await _resolve(guild)
        self.assertFalse(deleted)


    async def test_p14_delivered_or_missing_operation_is_never_compensated(self):
        """P14 (characterization): compensation must never undo delivered goods and
        must never fabricate success for an operation row that does not exist."""
        name = "P14DeliveredItem"
        from utils.inventory import give_item
        await give_item(GUILD, USER, name, 1, item_type="potion", source="shop")
        delivered = rows(
            "SELECT id, quantity, item_type, source, metadata, created_at, updated_at "
            "FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name=?",
            (GUILD, USER, name))[0]
        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7740, item_name=name,
            currency="balance", amount=100)
        await TO.update_purchase(op_id, status=TO.PURCHASE_DELIVERED, charged=1)
        spec = {
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7740,
            "item_name": name,
            "internal_type": "potion",
            "prior_inventory": None,
            "delivered_inventory": list(delivered),
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
        }
        res = await TO.compensate_late_delivery(op_id, dict(spec))
        self.assertFalse(res["complete"], str(res))
        self.assertIn("delivered", str(res.get("error") or "").lower(), str(res))
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=?"
                 " AND item_name=?", (GUILD, USER, name)),
            [(1,)],
            "compensation mutated inventory for a delivered operation")
        self.assertIsNone(
            (await TO.get_purchase(op_id))["_raw_delivery_rollback"],
            "compensation fabricated a descriptor for a delivered operation")

        # Missing operation row: explicit failure, never a fabricated success.
        missing = await TO.compensate_late_delivery("no-such-op-id", dict(spec))
        self.assertFalse(missing["complete"], str(missing))
        self.assertIn("not found", str(missing.get("error") or "").lower(), str(missing))
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=?"
                 " AND item_name=?", (GUILD, USER, name)),
            [(1,)])


    # ── P15–P18: findings from the independent review ────────────────────

    async def test_p15_completion_landing_between_check_and_write_wins(self):
        """Regression P15: completion that lands after the stale invocation's
        completion check must survive its descriptor write.

        The write is refused by the SQL guard on `update_purchase`, the stale
        invocation re-reads durable state and reports completion, and it leaves
        no retry marker or inventory/role side effect behind.
        """
        rid = 98001
        role = role_stub(rid, "P15RaceColor")
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        person.roles.append(role)
        guild.get_role = lambda r: role if int(r) == rid else None
        person.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 503"))

        from utils.inventory import give_item
        name = "P15RaceColor"
        await give_item(GUILD, USER, name, 1, item_type="role", source="shop")
        await give_item(GUILD, USER, name, 2, item_type="role", source="level_claim")

        op_id = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7801, item_name=name,
            currency="balance", amount=100)
        await TO.update_purchase(op_id, status=TO.PURCHASE_REFUNDED, refunded=1)
        stale_spec = {
            "guild_id": GUILD,
            "user_id": USER,
            "item_id": 7801,
            "item_name": name,
            "internal_type": "role",
            "role_id": rid,
            "role_added": True,
            "inventory_rolled_back": True,
            "equip_reconciled": True,
            "role_reconciled": False,
        }
        await TO.record_pending_delivery_rollback(op_id, stale_spec, bot=bot)
        TO._PENDING_DELIVERY_ROLLBACKS.clear()
        self.assertNotEqual(
            (await TO.get_purchase(op_id))["_raw_delivery_rollback"], "")

        real_update = TO.update_purchase
        raced = {"done": False, "blocked": 0}

        async def _racing_update(oid, **kwargs):
            # A competing invocation finishes the rollback exactly between the
            # stale invocation's completion check and its descriptor write.
            if not raced["done"] and kwargs.get("delivery_rollback") not in (None, ""):
                raced["done"] = True
                await real_update(oid, delivery_rollback="")
            res = await real_update(oid, **kwargs)
            if not res and kwargs.get("delivery_rollback") not in (None, ""):
                raced["blocked"] += 1
            return res

        TO.update_purchase = _racing_update
        try:
            res = await TO.compensate_late_delivery(op_id, dict(stale_spec), bot=bot)
        finally:
            TO.update_purchase = real_update

        self.assertTrue(raced["done"], "the probe never staged the completion")
        self.assertGreaterEqual(
            raced["blocked"], 1,
            "the guard never refused the stale descriptor write")
        self.assertTrue(res["complete"], str(res))
        self.assertTrue(res["durable_persisted"], str(res))
        self.assertIsNone(res.get("error"), str(res))

        self.assertEqual(
            rows("SELECT delivery_rollback FROM shop_purchase_operations"
                 " WHERE op_id=?", (op_id,)),
            [("",)],
            "a stale invocation resurrected a completed descriptor")
        self.assertEqual(
            rows("SELECT id FROM timed_holdings WHERE source='shop_late_rollback'"),
            [],
            "a stale invocation left a retry marker for finished work")
        self.assertNotIn(op_id, TO._PENDING_DELIVERY_ROLLBACKS)
        self.assertEqual(
            rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=?"
                 " AND item_name=?", (GUILD, USER, name)),
            [(3,)],
            "the stale invocation performed an inventory side effect")
        self.assertIn(role, person.roles,
                      "the stale invocation removed the Discord role")

    async def test_p16_handoff_persistence_status_is_recorded_both_paths(self):
        """Regression P16: both `handoff_to_holdings` paths must mark a failed or
        zero-row descriptor write as non-durable, clear the flag after a
        successful write, and never label a completed operation recoverable."""
        rid = 98010
        role = role_stub(rid, "P16HandoffColor")
        itx, person, bot, guild = purchase_ctx(granted_role=role)
        person.roles.append(role)
        guild.get_role = lambda r: role if int(r) == rid else None
        person.remove_roles = AsyncMock()

        real_update = TO.update_purchase

        def _fail_writes():
            async def _failing(oid, **kwargs):
                if kwargs.get("delivery_rollback") not in (None, ""):
                    raise RuntimeError("simulated disk error")
                return await real_update(oid, **kwargs)
            return _failing

        def _zero_row_writes():
            async def _zero(oid, **kwargs):
                if kwargs.get("delivery_rollback") not in (None, ""):
                    return False
                return await real_update(oid, **kwargs)
            return _zero

        async def _role_op():
            op = await TO.open_purchase(
                guild_id=GUILD, user_id=USER, item_id=7802,
                item_name="P16HandoffColor", currency="balance", amount=100)
            await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
            spec = {
                "guild_id": GUILD, "user_id": USER, "item_id": 7802,
                "item_name": "P16HandoffColor", "internal_type": "role",
                "role_id": rid, "role_added": True,
                "inventory_rolled_back": True, "equip_reconciled": True,
                "role_reconciled": False,
            }
            await TO.record_pending_delivery_rollback(op, spec, bot=bot)
            TO._PENDING_DELIVERY_ROLLBACKS.clear()
            return op, spec

        async def _holding_op():
            holding_id = await TO.grant_timed_holding(
                GUILD, USER, "P16HandoffHolding",
                expires_at=iso_in(5), role_id=rid, source="shop")
            op = await TO.open_purchase(
                guild_id=GUILD, user_id=USER, item_id=7803,
                item_name="P16HandoffHolding", currency="balance", amount=100)
            await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
            spec = {
                "guild_id": GUILD, "user_id": USER, "item_id": 7803,
                "item_name": "P16HandoffHolding", "internal_type": "temp_role",
                "role_id": rid, "role_added": True,
                "entry_key": f"h:{holding_id}",
                "inventory_rolled_back": False, "equip_reconciled": False,
                "role_reconciled": False,
            }
            await TO.record_pending_delivery_rollback(op, spec, bot=bot)
            TO._PENDING_DELIVERY_ROLLBACKS.clear()
            return op, spec

        # ── A) raising write, permanent-role handoff ──
        op, spec = await _role_op()
        TO.update_purchase = _fail_writes()
        try:
            res = await TO.compensate_late_delivery(
                op, dict(spec), bot=bot, handoff_to_holdings=True)
        finally:
            TO.update_purchase = real_update
        self.assertTrue(res.get("handed_off"), str(res))
        self.assertFalse(res["durable_persisted"], str(res))
        mem = TO._PENDING_DELIVERY_ROLLBACKS.get(op)
        self.assertIsNotNone(mem, "the handoff dropped the in-memory retry state")
        self.assertTrue(mem.get("_durable_unpersisted"),
                        "a failed handoff write was not marked non-durable")

        # ── B) zero-row write, permanent-role handoff ──
        op, spec = await _role_op()
        TO.update_purchase = _zero_row_writes()
        try:
            res = await TO.compensate_late_delivery(
                op, dict(spec), bot=bot, handoff_to_holdings=True)
        finally:
            TO.update_purchase = real_update
        self.assertFalse(res["durable_persisted"], str(res))
        mem = TO._PENDING_DELIVERY_ROLLBACKS.get(op)
        self.assertTrue(
            mem and mem.get("_durable_unpersisted"),
            "a zero-row handoff write was not marked non-durable")

        # ── C) successful retry clears the flag ──
        res = await TO.compensate_late_delivery(
            op, dict(spec), bot=bot, handoff_to_holdings=True)
        self.assertTrue(res.get("handed_off"), str(res))
        self.assertTrue(res["durable_persisted"], str(res))
        mem = TO._PENDING_DELIVERY_ROLLBACKS.get(op)
        self.assertTrue(mem, str(res))
        self.assertFalse(
            mem.get("_durable_unpersisted"),
            "a confirmed durable handoff write left the non-durable marker set")

        # ── D) timed-holding handoff, raising write ──
        op_h, spec_h = await _holding_op()
        TO.update_purchase = _fail_writes()
        try:
            res_h = await TO.compensate_late_delivery(
                op_h, dict(spec_h), bot=bot, handoff_to_holdings=True)
        finally:
            TO.update_purchase = real_update
        self.assertTrue(res_h.get("handed_off"), str(res_h))
        self.assertFalse(res_h["durable_persisted"], str(res_h))
        mem_h = TO._PENDING_DELIVERY_ROLLBACKS.get(op_h)
        self.assertIsNotNone(mem_h, str(res_h))
        self.assertTrue(mem_h.get("_durable_unpersisted"),
                        "the timed-holding handoff did not mark its write failure")

        # ── E) completion racing the handoff write: not recoverable pending work ──
        op_r, spec_r = await _role_op()
        raced = {"done": False}

        async def _racing(oid, **kwargs):
            if not raced["done"] and kwargs.get("delivery_rollback") not in (None, ""):
                raced["done"] = True
                await real_update(oid, delivery_rollback="")
            return await real_update(oid, **kwargs)

        TO.update_purchase = _racing
        try:
            res_r = await TO.compensate_late_delivery(
                op_r, dict(spec_r), bot=bot, handoff_to_holdings=True)
        finally:
            TO.update_purchase = real_update
        self.assertTrue(res_r["complete"], str(res_r))
        self.assertNotIn(op_r, TO._PENDING_DELIVERY_ROLLBACKS,
                         "a completed handoff race left recoverable pending state")
        self.assertEqual(
            rows("SELECT delivery_rollback FROM shop_purchase_operations"
                 " WHERE op_id=?", (op_r,)),
            [("",)],
            "the handoff race overwrote the completion sentinel")
        self.assertEqual(
            [r for r in rows(
                "SELECT metadata FROM timed_holdings WHERE source='shop_late_rollback'")
             if op_r in (r[0] or "")],
            [],
            "the handoff race left an orphan retry marker for the completed operation")

    async def test_p17_repeated_failures_do_not_starve_later_markers(self):
        """Regression P17: with more permanently failing markers than the pass
        limit, later markers must still be attempted while the failures keep
        their retry state. This is marker-set fairness, distinct from the
        ordinary-expiry starvation covered by P9."""
        rid = 98020
        role = role_stub(rid, "P17FailRole")
        itx, person, bot, guild = purchase_ctx()
        guild.get_role = lambda r: role if int(r) == rid else None
        person.roles.append(role)
        person.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 503"))

        failing_ops = []
        for idx in range(3):
            op = await TO.open_purchase(
                guild_id=GUILD, user_id=USER, item_id=7810 + idx,
                item_name=f"P17Fail{idx}", currency="balance", amount=100)
            await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
            await TO.record_pending_delivery_rollback(op, {
                "guild_id": GUILD, "user_id": USER, "item_id": 7810 + idx,
                "item_name": f"P17Fail{idx}", "internal_type": "role",
                "role_id": rid, "role_added": True,
                "inventory_rolled_back": True, "equip_reconciled": True,
                "role_reconciled": False,
            }, bot=bot)
            failing_ops.append(op)

        # A non-actionable row that used to consume a slot of the limited window.
        decoy = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7819,
            item_name="P17Decoy", currency="balance", amount=100)
        execute(
            "UPDATE shop_purchase_operations SET delivery_rollback=? WHERE op_id=?",
            (json.dumps({"guild_id": GUILD, "user_id": USER,
                         "item_name": "P17Decoy"}), decoy))

        # The later, resolvable marker.
        good = await TO.open_purchase(
            guild_id=GUILD, user_id=USER, item_id=7820,
            item_name="P17Good", currency="balance", amount=100)
        await TO.update_purchase(
            good, status=TO.PURCHASE_REFUNDED, refunded=1,
            delivery_rollback=json.dumps({
                "guild_id": GUILD, "user_id": USER, "item_id": 7820,
                "item_name": "P17Good", "internal_type": "role",
                "role_id": None, "role_added": False,
                "inventory_rolled_back": True, "equip_reconciled": True,
                "role_reconciled": False}))
        TO._PENDING_DELIVERY_ROLLBACKS.clear()

        # 4 candidates, limit=2: three passes guarantee every one a turn.
        for _ in range(3):
            await TO.reconcile_delivery_rollbacks(bot=bot, limit=2)

        self.assertEqual(
            (await TO.get_purchase(good))["_raw_delivery_rollback"], "",
            "the later marker was never attempted across repeated limited passes")
        # The failing markers keep their durable retry state and their rows.
        for op in failing_ops:
            raw = (await TO.get_purchase(op))["_raw_delivery_rollback"]
            self.assertNotEqual(
                raw, "", f"a failing marker lost its durable retry state: {op}")
        self.assertEqual(
            len(rows("SELECT id FROM timed_holdings WHERE source='shop_late_rollback'")),
            3,
            "failing markers did not retain exactly one retry marker each")

    async def test_p18_xp_boost_fallback_only_removes_the_shop_boost(self):
        """Regression P18: when `boost_id` is unavailable the fallback must still
        identify the purchased boost by provenance, in both insertion orders."""
        exp = "2099-01-01T00:00:00+00:00"

        async def _run(order, include_source):
            execute("DELETE FROM leveling_active_boosts")
            TO._PENDING_DELIVERY_ROLLBACKS.clear()
            seq = (("shop", "level_claim:77") if order == "shop_first"
                   else ("level_claim:77", "shop"))
            for src in seq:
                execute(
                    "INSERT INTO leveling_active_boosts"
                    " (guild_id, user_id, multiplier, expires_at, source)"
                    " VALUES (?,?,2.0,?,?)", (GUILD, USER, exp, src))
            op = await TO.open_purchase(
                guild_id=GUILD, user_id=USER, item_id=7830,
                item_name="P18Boost", currency="balance", amount=100)
            await TO.update_purchase(op, status=TO.PURCHASE_REFUNDED, refunded=1)
            spec = {
                "guild_id": GUILD, "user_id": USER, "item_id": 7830,
                "item_name": "P18Boost", "internal_type": "xp_boost",
                "boost_id": None, "boost_expires_at": exp,
                "inventory_rolled_back": False, "equip_reconciled": False,
                "role_reconciled": False,
            }
            if include_source:
                spec["boost_source"] = "shop"
            await TO.record_pending_delivery_rollback(op, spec, bot=None)
            res = await TO.compensate_late_delivery(op, dict(spec))
            remaining = [r[1] for r in rows(
                "SELECT id, source FROM leveling_active_boosts ORDER BY id")]
            label = f"order={order}, boost_source_recorded={include_source}"
            self.assertNotIn("shop", remaining,
                             f"the purchased boost was not rolled back ({label})")
            self.assertTrue(
                any(s.startswith("level_claim") for s in remaining),
                f"the independent level_claim boost was deleted ({label}): {remaining}")
            self.assertTrue(res["complete"], str(res))

        for order in ("shop_first", "claim_first"):
            for include_source in (False, True):
                await _run(order, include_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
