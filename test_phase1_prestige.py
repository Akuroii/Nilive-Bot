"""Phase 1 RED/GREEN contract: real Prestige DB/service + simulated shop calls.

Run: python scripts/test_phase1_prestige.py
No expectedFailure/skip decorators: defects must make the executable exit nonzero.
"""
import asyncio
import unittest
from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, seed_listing,
    member, bot_for, interaction, mutation_snapshot, all_embed_text,
)
from utils.prestige import (
    activate_booster_prestige, get_effective_prestige, has_vi_activation,
    purchase_prestige, PrestigeError,
)
from cogs.shop import process_purchase
from cogs.leveling import Leveling


class PrestigePurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()

    async def reject_shop(self, item, booster=True):
        before = mutation_snapshot()
        itx = interaction(booster)
        try:
            await process_purchase(itx, item)
        except Exception as exc:
            self.fail(f"Shop must reject cleanly, not raise {type(exc).__name__}: {exc}")
        self.assertEqual(mutation_snapshot(), before, "rejection partially mutated state")
        itx.response.send_message.assert_awaited_once()
        call = itx.response.send_message.call_args
        self.assertTrue(call.args or call.kwargs.get("content") or call.kwargs.get("embed"),
                        "rejection must answer the user")
        self.assertTrue(call.kwargs.get("ephemeral"), "purchase rejection should be private")

    async def reject_service(self, item, price=2500, booster=True, **extra):
        before = mutation_snapshot()
        try:
            await activate_booster_prestige(
                GUILD, USER, price, item_id=item,
                bot=bot_for(member(booster)), **extra)
        except PrestigeError as exc:
            self.assertTrue(str(exc), "domain rejection needs a useful explanation")
        except Exception as exc:
            self.fail(f"Expected PrestigeError, not {type(exc).__name__}: {exc}")
        else:
            self.fail("invalid direct activation succeeded")
        self.assertEqual(mutation_snapshot(), before, "service rejection changed state")

    async def test_vi_zero_stock_shop_activation_succeeds_without_stock_or_coin_change(self):
        item = seed_listing(stock=0)
        before = mutation_snapshot()
        await process_purchase(interaction(True), item)
        self.assertTrue(await has_vi_activation(GUILD, USER))
        self.assertEqual(mutation_snapshot()["economy"], before["economy"])
        self.assertEqual(mutation_snapshot()["shop_items"], before["shop_items"])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,)])

    async def test_vi_in_stock_booster_preserves_stock_and_all_balances(self):
        item = seed_listing(stock=2)
        itx = interaction(True)
        await process_purchase(itx, item)
        self.assertTrue(await has_vi_activation(GUILD, USER))
        self.assertEqual(rows("SELECT balance,diamonds FROM economy"), [(9000, 138)])
        self.assertEqual(rows("SELECT xp,level,prestige FROM levels"), [(2700055, 84, 3)])
        self.assertEqual(rows("SELECT item_id,price_paid,currency_paid FROM purchase_history"),
                         [(item, 0, "balance")])
        self.assertEqual(rows("SELECT amount,balance_after FROM transaction_ledger"), [])
        self.assertEqual(rows("SELECT * FROM inventory_items"), [], "VI is not inventory")
        self.assertEqual(rows("SELECT current_stock FROM shop_items WHERE id=?", (item,)),
                         [(2,)], "VI must not consume configured stock")
        self.assertIn("VI", all_embed_text(itx.response.send_message.call_args.kwargs["embed"]))

    async def test_vi_eligible_booster_activation_succeeds_for_free(self):
        item = seed_listing(stock=None)
        await process_purchase(interaction(True), item)
        self.assertTrue(await has_vi_activation(GUILD, USER))
        self.assertEqual(await get_effective_prestige(GUILD, USER, bot=bot_for(member(True))), 6)
        self.assertEqual(rows("SELECT balance,diamonds FROM economy"), [(9000, 138)])
        self.assertEqual(rows("SELECT xp,level,prestige FROM levels"), [(2700055, 84, 3)])
        self.assertEqual(rows("SELECT item_id,price_paid FROM purchase_history"), [(item, 0)])
        self.assertEqual(rows("SELECT amount,balance_after FROM transaction_ledger"), [])

    async def test_vi_preserves_every_permanent_tier_and_requires_no_v_prerequisite(self):
        item = seed_listing(stock=None)
        for tier in range(6):
            uid = USER + 10 + tier
            seed_member(user=uid, tier=tier)
            await process_purchase(interaction(True, user=uid), item)
            self.assertEqual(await get_effective_prestige(GUILD, uid, bot=bot_for(member(True, uid))), 6)
            self.assertEqual(await get_effective_prestige(GUILD, uid, bot=bot_for(member(False, uid))), tier)
            self.assertEqual(rows("SELECT prestige,xp,level FROM levels WHERE user_id=?", (uid,)),
                             [(tier, 2700055, 84)])

    async def test_vi_nonbooster_shop_rejects_without_writes(self):
        await self.reject_shop(seed_listing(), booster=False)

    async def test_vi_direct_service_resolves_nonbooster_and_rejects(self):
        await self.reject_service(seed_listing(), booster=False)

    async def test_vi_direct_service_cannot_override_authoritative_nonbooster(self):
        # A contradictory caller flag must not override a supplied real member lookup.
        # This is a service contract, not a claim of an existing public HTTP exploit.
        await self.reject_service(seed_listing(), booster=False, is_booster=True)

    async def test_vi_direct_service_ignores_sold_out_listing_counter(self):
        item = seed_listing(stock=0)
        result = await activate_booster_prestige(GUILD, USER, 0, item_id=item,
                                                bot=bot_for(member(True)))
        self.assertTrue(result["success"])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(0,)])

    async def test_vi_direct_service_rejects_missing_listing(self):
        await self.reject_service(999999)

    async def test_vi_direct_service_rejects_omitted_listing_id(self):
        await self.reject_service(None)

    async def test_vi_direct_service_rejects_other_guild_listing(self):
        await self.reject_service(seed_listing(guild=GUILD + 1))

    async def test_vi_direct_service_rejects_disabled_listing(self):
        await self.reject_service(seed_listing(enabled=0))

    async def test_vi_direct_service_rejects_nonprestige_listing(self):
        await self.reject_service(seed_listing(kind="custom"))

    async def test_vi_direct_service_rejects_non_vi_tier(self):
        await self.reject_service(seed_listing(tier=5))

    async def test_vi_direct_service_cannot_supply_a_cheaper_price(self):
        await self.reject_service(seed_listing(price=10000), price=1)

    async def test_vi_nonzero_canonical_price_is_rejected_without_writes(self):
        await self.reject_shop(seed_listing(price=10000))

    async def test_vi_repurchase_is_rejected_without_second_charge_or_stock_change(self):
        item = seed_listing()
        await process_purchase(interaction(True), item)
        self.assertTrue(await has_vi_activation(GUILD, USER))
        execute("UPDATE economy SET balance=12345")
        await self.reject_shop(item)

    async def test_vi_expiry_requires_new_activation_after_reboost_and_preserves_permanent(self):
        self.assertEqual(await get_effective_prestige(GUILD, USER, bot=bot_for(member(True))), 3)
        item = seed_listing()
        await process_purchase(interaction(True), item)
        for boosting, expected in [(True, 6), (False, 3), (True, 3)]:
            with self.subTest(boosting=boosting):
                self.assertEqual(await get_effective_prestige(
                    GUILD, USER, bot=bot_for(member(boosting))), expected)
        self.assertFalse(await has_vi_activation(GUILD, USER))
        await process_purchase(interaction(True), item)
        self.assertEqual(await get_effective_prestige(GUILD, USER, bot=bot_for(member(True))), 6)
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,), (0,)])
        self.assertEqual(rows("SELECT balance FROM economy"), [(9000,)])
        self.assertEqual(rows("SELECT prestige FROM levels"), [(3,)])

    async def test_historical_receipt_survives_expiry_and_initialization(self):
        execute("INSERT INTO prestige_vi_activations (guild_id,user_id) VALUES (?,?)", (GUILD, USER))
        execute("INSERT INTO purchase_history (guild_id,user_id,user_display_name,item_id,item_name,"
                "price_paid,currency_paid) VALUES (?,?,'Historical member',0,'Historical VI',9000,'balance')",
                (GUILD, USER))
        before = mutation_snapshot()
        self.assertEqual(await get_effective_prestige(GUILD, USER, bot=bot_for(member(True))), 6)
        from database import init_db
        await init_db()
        self.assertEqual(mutation_snapshot(), before, "initialization must not rewrite history")
        self.assertEqual(await get_effective_prestige(GUILD, USER, bot=bot_for(member(False))), 3)
        self.assertFalse(await has_vi_activation(GUILD, USER))
        for table in before.keys() - {"prestige_vi_activations"}:
            self.assertEqual(mutation_snapshot()[table], before[table])

    async def test_i_to_v_stays_sequential_with_minimum_balance_reset(self):
        execute("UPDATE levels SET prestige=0")
        for tier in range(1, 6):
            item = seed_listing(tier=tier, name=f"Prestige {tier}", stock=None)
            execute("UPDATE economy SET balance=9000")
            result = await purchase_prestige(GUILD, USER, tier, 2500, item_id=item)
            self.assertTrue(result["success"])
            self.assertEqual(rows("SELECT xp,level,prestige FROM levels"),
                             [(2700055, 84, tier)])
            self.assertEqual(rows("SELECT balance,diamonds FROM economy"), [(0, 138)])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(9000,)] * 5)
        before = mutation_snapshot()
        with self.assertRaises(PrestigeError):
            await purchase_prestige(GUILD, USER, 6, 2500)
        self.assertEqual(mutation_snapshot(), before)

    async def test_two_boosters_can_both_activate_without_global_stock_contention(self):
        seed_member(user=USER + 1)
        item = seed_listing(stock=1)
        await asyncio.gather(process_purchase(interaction(True), item),
                             process_purchase(interaction(True, user=USER + 1), item))
        self.assertEqual(rows("SELECT COUNT(*) FROM prestige_vi_activations"), [(2,)])
        self.assertEqual(rows("SELECT current_stock FROM shop_items WHERE id=?", (item,)), [(1,)])
        self.assertEqual(rows("SELECT balance FROM economy"), [(9000,), (9000,)])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,), (0,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])


# Individual test names keep each malformed-input failure visible in the result.
_BAD_LISTINGS = {
    "nonzero_price": {"price": 1},
    "negative_price": {"price": -1},
    "malformed_price": {"price": "garbage"},
    "missing_tier": {"tier": None},
    "invalid_tier": {"tier": 7},
    "malformed_tier": {"tier": "garbage"},
}
for _label, _kwargs in _BAD_LISTINGS.items():
    def _make_shop_case(kwargs):
        async def test(self):
            await self.reject_shop(seed_listing(**kwargs))
        return test
    setattr(PrestigePurchaseTests, "test_vi_invalid_listing_shop_" + _label,
            _make_shop_case(_kwargs))
    def _make_service_case(kwargs):
        async def test(self):
            await self.reject_service(seed_listing(**kwargs), price=kwargs.get("price", 2500))
        return test
    setattr(PrestigePurchaseTests, "test_vi_invalid_listing_service_" + _label,
            _make_service_case(_kwargs))


class PrestigeMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()

    async def message(self, boosting, activated):
        if activated:
            execute("INSERT INTO prestige_vi_activations (guild_id,user_id) VALUES (?,?)",
                    (GUILD, USER))
        before = mutation_snapshot()
        itx = interaction(boosting)
        # Invoke real command body without starting unrelated background loops.
        await Leveling.prestige.callback(object.__new__(Leveling), itx)
        itx.response.defer.assert_awaited_once()
        itx.followup.send.assert_awaited_once()
        after = mutation_snapshot()
        for table in before.keys() - {"prestige_vi_activations"}:
            self.assertEqual(after[table], before[table], "/prestige cannot mutate economy/history")
        if not boosting:
            self.assertFalse(await has_vi_activation(GUILD, USER))
        else:
            self.assertEqual(after["prestige_vi_activations"], before["prestige_vi_activations"])
        embed = itx.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        self.assertEqual(fields["Permanent Prestige"], "**III**")
        self.assertEqual(fields["Effective Prestige"], "**VI**" if boosting and activated else "**III**")
        # Only explanatory text, not the tier field, is checked below.
        explanation = " ".join(f.value for f in embed.fields
                               if f.name not in ("Permanent Prestige", "Effective Prestige"))
        return explanation.lower()

    async def test_nonbooster_does_not_receive_automatic_vi_claim(self):
        text = await self.message(False, False)
        self.assertNotIn("you get effective prestige vi", text)

    async def test_booster_without_activation_is_told_eligible_not_activated(self):
        text = await self.message(True, False)
        self.assertIn("eligible", text)
        self.assertRegex(text, r"purchas|activat")
        self.assertRegex(text, r"alone|not automatic|does not.*activat")
        self.assertNotIn("you get effective prestige vi", text)

    async def test_activated_booster_is_told_vi_requires_boost_and_activation(self):
        text = await self.message(True, True)
        self.assertIn("vi", text)
        self.assertRegex(text, r"activat|purchas")
        self.assertIn("boost", text)
        self.assertIn("permanent", text)

    async def test_expired_booster_is_told_activation_ended_and_reactivation_is_free(self):
        text = await self.message(False, True)
        self.assertIn("activation", text)
        self.assertRegex(text, r"removed|ended")
        self.assertRegex(text, r"re.?boost|boost.*again")
        self.assertIn("free", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
