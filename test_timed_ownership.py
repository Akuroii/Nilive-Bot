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
        """Priority 2B regression:
        When `_compensate_failed_purchase()` loses its `pending -> failed`
        transition claim because the operation was already moved to `uncertain`
        with `refunded=0`, the buyer is NEVER told that their payment was
        returned, and the 'Refund pending' field is shown. Conversely, if the
        operation was already refunded (`refunded=1, status='refunded'`), the
        message accurately reflects the committed refund.
        """
        from cogs.shop import _compensate_failed_purchase

        # Case 1: Operation moved to `uncertain` (refunded=0) before `_compensate_failed_purchase`
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
            any(f.name == "Refund pending" for f in embed1.fields),
            "must include Refund pending field when refund was not committed")

        # Case 2: Operation already refunded by operator (`status='refunded', refunded=1`)
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
            any(f.name == "Refund pending" for f in embed2.fields),
            "no Refund pending warning when refund is already committed")

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

        # 2. Fault-inject `EntitlementCheckError` in `_role_granted_elsewhere` during
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
            "per-member lease in level_op_locks must be released on failure")
        self.assertEqual(
            rows("SELECT rolled_back FROM xp_mutations WHERE operation_id=?", (op_mut,)),
            [(0,)],
            "xp_mutation must remain un-rolled-back so retry is allowed")

        # 3. Delete the timed holding and retry `rollback_xp_operation` -> succeeds cleanly!
        await TO.delete_timed_holding(th["entry_key"])
        rb_ok = await rollback_xp_operation(
            GUILD, USER, operation_id=op_mut, apply=True,
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
            rows("SELECT rolled_back FROM xp_mutations WHERE operation_id=?", (op_mut,)),
            [(1,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
