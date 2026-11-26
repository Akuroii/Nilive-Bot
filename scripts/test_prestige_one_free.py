"""Prestige I is Free (price 0) — and still resets Coins like every I-V tier.

Two independent facts are proven separately:
  1. PURCHASE PRICE: Prestige I costs 0 Coins / 0 Diamonds (a buyer with an
     empty wallet can take it; nothing is charged as a price).
  2. PRESTIGE RESET: buying I-V still sets the Coins balance to 0 (unchanged
     lifecycle); Level/XP/Diamonds are untouched.
Plus: dashboard API create/edit + DB round-trip, Free display, II-V still
paid, VI untouched, repeated/failed purchases safe.

Run: python scripts/test_prestige_one_free.py
"""
import asyncio
import time
import unittest

from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, mutation_snapshot,
)
import dashboard.app as dashboard
from utils import prestige
from utils.prestige import PrestigeError

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)


class PrestigeOneApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'admin',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "P1 Test", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="p1-csrf")

    def post(self, payload):
        return self.client.post("/api/shop/item", json=payload,
                                headers={"X-CSRF-Token": "p1-csrf"})

    def put(self, item_id, payload):
        return self.client.put(f"/api/shop/item/{item_id}", json=payload,
                               headers={"X-CSRF-Token": "p1-csrf"})

    @staticmethod
    def body(tier, price, **extra):
        return dict(name=f"Prestige {tier}", type="prestige", prestige_tier=tier,
                    price=price, **extra)

    def test_create_prestige_one_at_zero_persists_as_zero(self):
        response = self.post(self.body(1, 0))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(rows("SELECT price, price_diamonds, prestige_tier FROM shop_items"),
                         [(0, None, 1)])

    def test_edit_prestige_one_to_zero_round_trips(self):
        self.assertEqual(self.post(self.body(1, 750)).status_code, 200)
        item_id = rows("SELECT id FROM shop_items")[0][0]
        response = self.put(item_id, self.body(1, 0))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(rows("SELECT price, prestige_tier FROM shop_items WHERE id=?",
                              (item_id,)), [(0, 1)])
        # and back to a (still allowed) positive price, then to 0 again
        self.assertEqual(self.put(item_id, self.body(1, 40)).status_code, 200)
        self.assertEqual(self.put(item_id, self.body(1, 0)).status_code, 200)
        self.assertEqual(rows("SELECT price FROM shop_items WHERE id=?", (item_id,)), [(0,)])

    def test_prestige_one_missing_blank_or_invalid_price_is_rejected(self):
        for bad in (None, "", "free", -1, 2.5, True):
            with self.subTest(price=bad):
                before = mutation_snapshot()
                response = self.post(self.body(1, bad))
                self.assertEqual(response.status_code, 400, (bad, response.get_json()))
                self.assertEqual(mutation_snapshot(), before)
        before = mutation_snapshot()
        response = self.post(dict(name="No price", type="prestige", prestige_tier=1))
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(mutation_snapshot(), before)

    def test_prestige_two_to_five_reject_zero_negative_missing_and_keep_prices(self):
        prices = {2: 30, 3: 140, 4: 270, 5: 450}
        ids = {}
        for tier, price in prices.items():
            self.assertEqual(self.post(self.body(tier, price)).status_code, 200)
            ids[tier] = rows("SELECT id FROM shop_items WHERE prestige_tier=?", (tier,))[0][0]
        for tier in prices:
            for bad in (0, "0", -5, None, "", "x"):
                with self.subTest(tier=tier, price=bad):
                    before = mutation_snapshot()
                    self.assertEqual(self.post(self.body(tier, bad)).status_code, 400)
                    self.assertEqual(self.put(ids[tier], self.body(tier, bad)).status_code, 400)
                    self.assertEqual(mutation_snapshot(), before)
        self.assertEqual(dict(rows("SELECT prestige_tier, price FROM shop_items")), prices)

    def test_prestige_six_keeps_its_special_free_rules(self):
        self.assertEqual(self.post(self.body(6, 0)).status_code, 200)
        before = mutation_snapshot()
        self.assertEqual(self.post(self.body(6, 100)).status_code, 400)
        self.assertEqual(self.post(self.body(6, 0, max_stock=5)).status_code, 400)
        self.assertEqual(mutation_snapshot(), before)
        self.assertEqual(rows("SELECT price, prestige_tier FROM shop_items"), [(0, 6)])

    def test_prestige_one_displays_as_free_and_paid_tiers_show_their_price(self):
        self.assertEqual(self.post(self.body(1, 0)).status_code, 200)
        self.assertEqual(self.post(self.body(2, 30)).status_code, 200)
        for url in ("/shop", "/api/shop/items"):
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                start = html.index("Prestige 1")
                self.assertIn("Free", html[start:html.index("</tr>", start)])
                start = html.index("Prestige 2")
                row = html[start:html.index("</tr>", start)]
                self.assertIn("30", row)
                self.assertNotIn("Free", row)


class PrestigeOnePurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    def state(self):
        return (rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)),
                rows("SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
                     (GUILD, USER)))

    async def test_fact_1_prestige_one_costs_nothing_to_purchase(self):
        # Empty wallet: a free listing must not require or spend any price.
        seed_member(balance=0, tier=0)
        result = await prestige.purchase_prestige(GUILD, USER, 1, 0)
        self.assertEqual(result["new_tier"], 1)
        (eco, lvl) = self.state()
        self.assertEqual(eco, [(0, 138)], "no Coin/Diamond charge")
        self.assertEqual(lvl, [(2700055, 84, 1)])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,)])

    async def test_fact_2_prestige_one_still_resets_coins_like_i_to_v(self):
        seed_member(balance=500, tier=0)
        result = await prestige.purchase_prestige(GUILD, USER, 1, 0)
        self.assertEqual((result["old_balance"], result["new_balance"]), (500, 0))
        (eco, lvl) = self.state()
        self.assertEqual(eco, [(0, 138)], "Coins reset to 0, Diamonds untouched")
        self.assertEqual(lvl, [(2700055, 84, 1)], "XP/Level untouched, tier set")
        # II-V keep the same reset after their own positive minimum balance.
        execute("UPDATE economy SET balance=35 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        await prestige.purchase_prestige(GUILD, USER, 2, 30)
        self.assertEqual(self.state()[0], [(0, 138)])

    async def test_ii_to_v_still_need_their_positive_minimum_balance(self):
        seed_member(balance=29, tier=1)
        before = self.state()
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(GUILD, USER, 2, 30)
        self.assertEqual(self.state(), before)

    async def test_repeated_purchase_is_refused_and_changes_nothing(self):
        seed_member(balance=500, tier=0)
        await prestige.purchase_prestige(GUILD, USER, 1, 0)
        execute("UPDATE economy SET balance=77 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        before = (self.state(), rows("SELECT COUNT(*) FROM purchase_history"))
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(GUILD, USER, 1, 0)
        self.assertEqual((self.state(), rows("SELECT COUNT(*) FROM purchase_history")), before)

    async def test_failure_inside_the_transaction_rolls_everything_back(self):
        seed_member(balance=500, tier=0)
        execute("CREATE TRIGGER boom BEFORE INSERT ON purchase_history "
                "BEGIN SELECT RAISE(ABORT,'boom'); END")
        before = self.state()
        with self.assertRaises(Exception):
            await prestige.purchase_prestige(GUILD, USER, 1, 0)
        self.assertEqual(self.state(), before, "no reset, no tier, on failure")
        self.assertEqual(rows("SELECT COUNT(*) FROM purchase_history"), [(0,)])
        execute("DROP TRIGGER boom")
        await prestige.purchase_prestige(GUILD, USER, 1, 0)   # retry is safe
        self.assertEqual(self.state()[1], [(2700055, 84, 1)])
        self.assertEqual(rows("SELECT COUNT(*) FROM purchase_history"), [(1,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
