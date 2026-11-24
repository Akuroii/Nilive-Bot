"""Free VI API / server-rendered presentation, using real permission+CSRF route."""
import unittest
from phase1_support import GUILD, USER, execute, rows, mutation_snapshot
import test_phase1_api as api_tests


class FreeVIShopApiTests(unittest.TestCase):
    setUp = api_tests.ListingApiTests.setUp
    post = api_tests.ListingApiTests.post

    def test_vi_ignored_fields_are_normalized_not_validated_as_restrictions(self):
        # Blank / zero form values normalize to None on unmetered VI, while real
        # positive restriction values or malformed types are rejected explicitly.
        response = self.post(dict(name="VI", type="prestige", prestige_tier=6, price=0,
            max_stock="", current_stock="garbage", required_level=0,
            required_role_id=0, role_id="", duration_hours=0))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIs(response.get_json()["success"], True)
        self.assertEqual(rows("SELECT price,max_stock,current_stock,required_level,required_role_id,"
                              "role_id,duration_hours FROM shop_items"), [(0, None, None, 0, None, None, None)])
        for bad_field, bad_val in (("max_stock", 3), ("required_role_id", 88), ("duration_hours", 2)):
            bad_resp = self.post(dict(name=f"BadVI-{bad_field}", type="prestige",
                                      prestige_tier=6, price=0, **{bad_field: bad_val}))
            self.assertEqual(bad_resp.status_code, 400)

    def test_i_to_v_api_values_are_unchanged(self):
        for tier in range(1, 6):
            response = self.post(dict(name=f"Paid {tier}", type="prestige", prestige_tier=tier,
                price=2500, max_stock=4, required_level=50, required_role_id=77))
            self.assertEqual(response.status_code, 200)
        self.assertEqual(rows("SELECT price,max_stock,current_stock,required_level,required_role_id "
                              "FROM shop_items"), [(2500, 4, 4, 50, 77)] * 5)

    def test_zero_price_is_a_normal_free_listing_and_not_a_vi_shortcut(self):
        # Zero (with no Diamond price) is the normal Free listing for active
        # creatable kinds, keeping their own restrictions without VI normalization.
        # Title is permanent (no duration_hours), and new xp_boost creation is retired.
        for kind in ("custom", "color", "role", "temp_role", "potion"):
            with self.subTest(kind=kind):
                before = len(rows("SELECT id FROM shop_items"))
                response = self.post(dict(name=f"Free {kind}", type=kind, price=0, role_id=77,
                                           duration_hours=2, xp_boost_multiplier=2,
                                           max_stock=4, required_level=5,
                                           required_role_id=88))
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(len(rows("SELECT id FROM shop_items")), before + 1)
        self.assertEqual(
            rows("SELECT price,max_stock,current_stock,required_level,required_role_id,"
                 "duration_hours,price_diamonds FROM shop_items"),
            [(0, 4, 4, 5, 88, 2, None)] * 5,
            "a normal Free listing keeps its own restrictions — no VI normalization")
        # Permanent Title: rejects positive duration, accepts Free with stock/level/role.
        self.assertEqual(
            self.post(dict(name="Timed Title", type="title", price=0, duration_hours=2)).status_code,
            400,
        )
        title_resp = self.post(dict(name="Free title", type="title", price=0,
                                    max_stock=4, required_level=5, required_role_id=88))
        self.assertEqual(title_resp.status_code, 200, title_resp.get_json())
        # Retired xp_boost creation remains blocked even when price=0.
        self.assertEqual(
            self.post(dict(name="Free xp_boost", type="xp_boost", price=0,
                           duration_hours=2, xp_boost_multiplier=2)).status_code,
            400,
        )
        # A positive diamond price is still a normal paid item, not a free item.
        response = self.post(dict(name="Paid diamonds", type="title", price=0, price_diamonds=5))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(rows("SELECT price_diamonds FROM shop_items WHERE name='Paid diamonds'"),
                         [(5,)])

    def test_zero_price_permanent_tiers_remain_invalid(self):
        # Prestige I allows 0 (Free); II–V require a positive Coin price.
        self.assertEqual(
            self.post(dict(name="Free Prestige I", type="prestige", prestige_tier=1, price=0)).status_code,
            200,
        )
        for tier in range(2, 6):
            before = mutation_snapshot()
            response = self.post(dict(name="Paid Prestige", type="prestige", prestige_tier=tier, price=0))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(mutation_snapshot(), before)

    def test_dashboard_and_partial_show_free_not_a_coin_price_or_permanent_vi(self):
        self.assertEqual(self.post(dict(name="Free Booster VI", type="prestige", prestige_tier=6, price=0)).status_code, 200)
        for url in ("/shop", "/api/shop/items"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                start = html.index("Free Booster VI")
                row = html[start:html.index("</tr>", start)]
                self.assertIn("Free", row)
                self.assertIn("While boosting", row)
                self.assertNotIn("Permanent", row)
                self.assertNotIn("Out of stock", row)

    def test_free_receipts_do_not_present_a_coin_debit_or_permanent_entitlement(self):
        execute("INSERT INTO purchase_history (guild_id,user_id,user_display_name,item_id,item_name,"
                "price_paid,currency_paid) VALUES (?,?,'Tester',1,'VI',0,'balance')", (GUILD, USER))
        response = self.client.get("/api/shop/purchase-history")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<td>Free</td>", html)
        self.assertNotIn("Permanent", html)
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
