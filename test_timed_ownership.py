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
        execute("INSERT OR REPLACE INTO prestige_roles "
                "(guild_id, tier, role_id) VALUES (?,?,?)", (GUILD, 1, 953))
        _, person, bot, _ = purchase_ctx(granted_role=role_stub(953))
        granted = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="Tier", duration_hours=1,
            item_type="temp_role", role_id=953)
        person.roles = [role_stub(953)]
        await self._expire(granted["entry_key"])
        await TO.cleanup_expired_holdings(bot)
        person.remove_roles.assert_not_awaited()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
