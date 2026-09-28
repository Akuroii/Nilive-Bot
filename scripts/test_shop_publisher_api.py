#!/usr/bin/env python3
"""
Shop Publisher — Phase 1: the catalog + preview API, on the real Flask app,
real permissions, real CSRF and a scratch SQLite database (no HTTP server).

What this locks down
  1.  GET  /api/shop-publisher/catalog serves BOTH pickers plus the fixed
      token catalog; products arrive pre-grouped by the existing `type` column
      only (locked: no new category system).
  2.  POST /api/shop-publisher/preview runs the ONE server-side resolver and
      returns the resolved message + the purchase action that will be
      published (`shop_buy_<id>`) + the deterministic warnings — the same
      module Phase 2's publish path must reuse.
  3.  The routes are READ-ONLY: no table changes, no audit-log rows.
  4.  Access is LEVEL_OWNER (like the Embed Builder routes) and CSRF still
      applies to the POST.
  5.  Legacy embed_templates rows (bare embed dict) preview correctly.

Run:  python3 scripts/test_shop_publisher_api.py
"""

import asyncio
import time
import unittest

from phase1_support import (
    GUILD, USER, execute, rows, mutation_snapshot, reset_database,
)
import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)


def seed_template(name, data_json):
    execute("INSERT INTO embed_templates (guild_id, name, data) VALUES (?, ?, ?)",
            (GUILD, name, data_json))


def seed_product(name="VIP Role", kind="role", price=1500, **over):
    columns = {
        "description": "The VIP role.", "price_diamonds": None, "role_id": 99,
        "duration_hours": None, "max_stock": 20, "current_stock": 12,
        "required_level": 5, "rarity": "epic", "icon_url": "https://x/i.png",
        "prestige_tier": None, "enabled": 1, "featured": 0,
        "xp_boost_multiplier": None,
    }
    columns.update(over)
    return execute(
        "INSERT INTO shop_items (guild_id, name, type, price, description, "
        "price_diamonds, role_id, duration_hours, max_stock, current_stock, "
        "required_level, rarity, icon_url, prestige_tier, enabled, featured, "
        "xp_boost_multiplier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (GUILD, name, kind, price, columns["description"],
         columns["price_diamonds"], columns["role_id"], columns["duration_hours"],
         columns["max_stock"], columns["current_stock"], columns["required_level"],
         columns["rarity"], columns["icon_url"], columns["prestige_tier"],
         columns["enabled"], columns["featured"], columns["xp_boost_multiplier"]))


class ShopPublisherApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Phase One", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="phase1-csrf")

    def preview(self, payload, csrf=True):
        headers = {"X-CSRF-Token": "phase1-csrf"} if csrf else {}
        return self.client.post("/api/shop-publisher/preview", json=payload, headers=headers)

    def test_catalog_groups_products_by_existing_type_only(self):
        seed_template("banner", '{"content": "hi", "embeds": []}')
        seed_product("Zulu", kind="title", price=10)
        seed_product("Alpha", kind="role", price=20)
        seed_product("Midnight", kind="prestige", price=2500, prestige_tier=3)
        response = self.client.get("/api/shop-publisher/catalog")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["templates"], ["banner"])
        self.assertEqual([p["name"] for p in data["products"]],
                         ["Alpha", "Midnight", "Zulu"])
        self.assertEqual([p["type"] for p in data["products"]],
                         ["role", "prestige", "title"])
        for field in ("id", "name", "type", "price", "price_diamonds",
                      "enabled", "current_stock", "max_stock", "prestige_tier"):
            self.assertIn(field, data["products"][0], field)

    def test_catalog_carries_the_fixed_token_catalog(self):
        import utils.shop_publisher as SP
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        self.assertEqual(data["tokens"], SP.token_catalog_payload())
        keys = [t["key"] for t in data["tokens"]]
        self.assertEqual(keys, list(SP.TOKEN_CATALOG),
                         "the UI reference and the resolver cannot drift")

    def test_preview_resolves_tokens_and_returns_the_publishable_action(self):
        seed_template("sale", '{"content": "Buy {{product.name}} for {{product.price}}!",'
                              '"embeds": [{"title": "{{product.name}}",'
                              '"description": "{{product.description}}"}]}')
        item_id = seed_product()
        response = self.preview({"template": "sale", "product_id": item_id})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIs(data["success"], True)
        self.assertEqual(data["preview"]["content"], "Buy VIP Role for 1,500 🪙 Coins!")
        self.assertEqual(data["preview"]["embeds"][0]["title"], "VIP Role")
        self.assertEqual(data["preview"]["embeds"][0]["description"], "The VIP role.")
        action = data["preview"]["purchase_action"]
        self.assertEqual(action["custom_id"], f"shop_buy_{item_id}")
        self.assertEqual(action["label"], "Buy VIP Role")
        self.assertEqual(action["style"], "green")
        self.assertEqual(data["preview"]["warnings"], [])
        self.assertEqual(data["product"], {"id": item_id, "name": "VIP Role",
                                           "type": "role"})
        used = {u["token"]: u["value"] for u in data["preview"]["tokens"]["used"]}
        self.assertEqual(used["{{product.name}}"], "VIP Role")
        self.assertEqual(used["{{product.price}}"], "1,500 🪙 Coins")

    def test_preview_matches_the_shared_resolver_byte_for_byte(self):
        import json
        import utils.shop_publisher as SP
        from utils.currency import get_currency_config
        seed_template("sale", '{"content": "{{product.name}} {{product.stock}}", "embeds": []}')
        item_id = seed_product()
        data = self.preview({"template": "sale", "product_id": item_id}).get_json()
        row = rows("SELECT id, name, type, price, price_diamonds, enabled, "
                   "current_stock, max_stock, prestige_tier, featured, "
                   "description, duration_hours, required_level, rarity, icon_url "
                   "FROM shop_items WHERE id = ?", (item_id,))[0]
        product = dict(zip(("id", "name", "type", "price", "price_diamonds", "enabled",
                            "current_stock", "max_stock", "prestige_tier", "featured",
                            "description", "duration_hours", "required_level",
                            "rarity", "icon_url"), row))
        direct = SP.preview_message(json.loads(
            rows("SELECT data FROM embed_templates")[0][0]), product,
            asyncio.run(get_currency_config(GUILD)))
        self.assertEqual(json.dumps(data["preview"], sort_keys=True),
                         json.dumps(direct, sort_keys=True),
                         "the route must not resolve anything on its own")

    def test_preview_warnings_survive_the_api(self):
        seed_template("sale", '{"content": "{{nope}} {{product.description}}", "embeds": []}')
        item_id = seed_product(description=None, enabled=0, current_stock=0)
        data = self.preview({"template": "sale", "product_id": item_id}).get_json()
        self.assertEqual([w["code"] for w in data["preview"]["warnings"]],
                         ["product_disabled", "product_out_of_stock",
                          "unknown_token", "empty_value"])

    def test_template_lookup_is_case_insensitive_like_the_embed_builder(self):
        seed_template("sale", '{"content": "{{product.name}}", "embeds": []}')
        item_id = seed_product()
        response = self.preview({"template": "SaLe", "product_id": item_id})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.get_json()["success"], True)

    def test_legacy_bare_embed_rows_preview_as_one_embed(self):
        seed_template("legacy", '{"title": "Only an embed", "description": "old"}')
        item_id = seed_product()
        data = self.preview({"template": "legacy", "product_id": item_id}).get_json()
        self.assertEqual(len(data["preview"]["embeds"]), 1)
        self.assertEqual(data["preview"]["embeds"][0]["title"], "Only an embed")

    def test_missing_selection_is_a_400_and_never_a_500(self):
        item_id = seed_product()
        seed_template("sale", '{"content": "x", "embeds": []}')
        for payload in ({}, {"template": "sale"},
                        {"product_id": item_id},
                        {"template": "sale", "product_id": "3"},
                        {"template": "sale", "product_id": True},
                        {"template": "sale", "product_id": 1.5}):
            with self.subTest(payload=payload):
                response = self.preview(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIs(response.get_json()["success"], False)
                self.assertTrue(response.get_json()["error"])

    def test_unknown_template_or_product_is_a_404(self):
        item_id = seed_product()
        seed_template("sale", '{"content": "x", "embeds": []}')
        self.assertEqual(self.preview({"template": "nope", "product_id": item_id}).status_code, 404)
        self.assertEqual(self.preview({"template": "sale", "product_id": item_id + 5}).status_code, 404)
        self.assertIs(self.preview({"template": "nope", "product_id": item_id}).get_json()["success"], False)

    def test_preview_and_catalog_are_read_only(self):
        seed_template("sale", '{"content": "{{product.name}}", "embeds": []}')
        item_id = seed_product()
        before = mutation_snapshot()
        before_audit = rows("SELECT * FROM audit_log")
        self.assertEqual(self.preview({"template": "sale", "product_id": item_id}).status_code, 200)
        self.assertEqual(self.client.get("/api/shop-publisher/catalog").status_code, 200)
        self.assertEqual(mutation_snapshot(), before,
                         "previewing must never touch a store")
        self.assertEqual(rows("SELECT * FROM audit_log"), before_audit,
                         "previewing is not an action worth an audit row")

    def test_csrf_is_still_enforced_on_preview(self):
        seed_template("sale", '{"content": "x", "embeds": []}')
        item_id = seed_product()
        before = mutation_snapshot()
        response = self.preview({"template": "sale", "product_id": item_id}, csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(mutation_snapshot(), before)

    def test_owner_level_is_required_on_both_routes(self):
        seed_template("sale", '{"content": "x", "embeds": []}')
        item_id = seed_product()
        execute("UPDATE dashboard_users SET permission_level='admin'")
        self.assertEqual(self.client.get("/api/shop-publisher/catalog").status_code, 403)
        self.assertEqual(self.preview({"template": "sale", "product_id": item_id}).status_code, 403)
        execute("UPDATE dashboard_users SET permission_level='moderator'")
        self.assertEqual(self.client.get("/api/shop-publisher/catalog").status_code, 403)
        execute("DELETE FROM dashboard_users")
        self.assertEqual(self.preview({"template": "sale", "product_id": item_id}).status_code, 403)

    def test_products_from_another_guild_are_invisible(self):
        seed_template("sale", '{"content": "x", "embeds": []}')
        execute("INSERT INTO shop_items (guild_id, name, type, price) "
                "VALUES (9999, 'Elsewhere', 'role', 5)")
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        self.assertEqual(data["products"], [])
        response = self.preview({"template": "sale", "product_id": rows(
            "SELECT id FROM shop_items")[0][0]})
        self.assertEqual(response.status_code, 404)

    def test_publisher_page_renders_for_owner(self):
        response = self.client.get("/shop-publisher")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-page-module="shop-publisher"', html)
        self.assertIn("sp-preview-mount", html)
        self.assertIn("sp-purchase", html)

    def test_publisher_page_is_owner_gated(self):
        execute("UPDATE dashboard_users SET permission_level='admin'")
        self.assertEqual(self.client.get("/shop-publisher").status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
