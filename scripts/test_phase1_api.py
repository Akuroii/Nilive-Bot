"""Real Flask route + permissions + CSRF + scratch SQLite; no HTTP server."""
import asyncio
import time
import unittest
from phase1_support import GUILD, USER, execute, reset_database, rows, mutation_snapshot
import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)


class ListingApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'admin',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Phase One", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="phase1-csrf")

    def post(self, payload, csrf=True):
        return self.client.post("/api/shop/item", json=payload,
                                headers={"X-CSRF-Token": "phase1-csrf"} if csrf else {})

    def assert_rejected(self, payload):
        before = mutation_snapshot()
        response = self.post(payload)
        self.assertLess(response.status_code, 500, "invalid input must not produce HTTP 500")
        self.assertEqual(mutation_snapshot(), before, "invalid listing was persisted")
        data = response.get_json(silent=True)
        self.assertIsInstance(data, dict, "validation must return a structured error")
        self.assertIs(data.get("success"), False)
        self.assertTrue(data.get("error"), "validation needs an actionable message")

    def test_valid_free_vi_listing_persists_zero_and_unmetered_values(self):
        response = self.post(dict(name="Prestige VI", type="prestige", price=0,
                                  prestige_tier=6, max_stock=2))
        self.assertLess(response.status_code, 400)
        self.assertIs(response.get_json()["success"], True)
        self.assertEqual(rows("SELECT guild_id,type,price,prestige_tier,current_stock "
                              "FROM shop_items"), [(GUILD, "prestige", 0, 6, None)])

    def test_nonprestige_zero_price_listing_is_rejected(self):
        response = self.post(dict(name="Earnable title", type="title", price=0))
        self.assertIs(response.get_json()["success"], False)
        self.assertEqual(rows("SELECT type,price FROM shop_items"), [])

    def test_csrf_still_required(self):
        before = mutation_snapshot()
        response = self.post(dict(name="VI", type="prestige", price=0, prestige_tier=6),
                             csrf=False)
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(mutation_snapshot(), before)

    def test_nonadmin_cannot_create_listing(self):
        execute("UPDATE dashboard_users SET permission_level='viewer'")
        before = mutation_snapshot()
        response = self.post(dict(name="VI", type="prestige", price=0, prestige_tier=6))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(mutation_snapshot(), before)


MISSING = object()
INVALID = {
    "nonzero_price": ("price", 1), "negative_price": ("price", -1),
    "malformed_price": ("price", "garbage"), "null_price": ("price", None),
    "missing_price": ("price", MISSING), "fractional_price": ("price", 12.5),
    "boolean_price": ("price", True), "list_price": ("price", [2500]),
    "missing_tier": ("prestige_tier", MISSING), "null_tier": ("prestige_tier", None),
    "malformed_tier": ("prestige_tier", "garbage"), "zero_tier": ("prestige_tier", 0),
    "negative_tier": ("prestige_tier", -1), "above_vi_tier": ("prestige_tier", 7),
    "fractional_tier": ("prestige_tier", 6.5), "boolean_tier": ("prestige_tier", True),
    "diamond_price": ("price_diamonds", 10),
}
for label, (field, value) in INVALID.items():
    def make_case(field, value):
        def test(self):
            payload = dict(name="Invalid Prestige", type="prestige", price=0, prestige_tier=6)
            if value is MISSING:
                payload.pop(field)
            else:
                payload[field] = value
            self.assert_rejected(payload)
        return test
    setattr(ListingApiTests, "test_invalid_listing_" + label, make_case(field, value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
