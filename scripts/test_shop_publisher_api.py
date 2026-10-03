#!/usr/bin/env python3
"""
Shop Publisher — Step 0: the catalog + design-draft preview API, on the real
Flask app, real permissions, real CSRF and a scratch SQLite database (no HTTP
server).

What this locks down
  1.  GET  /api/shop-publisher/catalog serves BOTH pickers plus the fixed
      token catalog; templates arrive as names ONLY (the explicit final Step 0
      contract — Q2#12 closed 2026-09-30: light payload at ~100-template
      scale; the selected template's document is fetched on selection from
      the Embed Builder's existing GET /api/embedbuilder/template/<name> —
      the intentional Step 0 transitional presentation source); products
      arrive in the preserved deterministic sort (the existing `type`-grouped
      order — never reordered client-side) and carry the additive
      `category_id` link (the Slice 1 Shop Category mapping; presentation
      metadata, NEVER publication).
  2.  POST /api/shop-publisher/preview takes a Design draft
      ({presentation, products, action}) and runs the ONE server-side resolver
      — the same module the future publish path must reuse — returning the
      resolved message + the purchase action that will be published
      (shop_buy_<id> entries) + the deterministic warnings.
  3.  The roster contract: products[] is the ROOT-PRODUCT roster; drafts that
      violate the locked rules (option rows in products[], option entries
      without their root in products[], option rows in product_select,
      multi-family option_select) are rejected with 400 + pathed problems.
  4.  The routes are READ-ONLY: no table changes, no audit-log rows.
  5.  Access is LEVEL_OWNER (like the Embed Builder routes) and CSRF still
      applies to the POST.
  6.  Legacy embed_templates rows (bare embed dict) preview correctly.

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


def draft_for(item_id, template='{"content": "Buy {{product.name}} for {{product.price_display}}!",'
                               '"embeds": [{"title": "{{product.name}}",'
                               '"description": "{{product.description}}"}]}', **over):
    import json
    doc = json.loads(template) if isinstance(template, str) else template
    draft = {
        # The draft presentation carries the template document as-is plus the
        # mode — the shared resolver normalizes (legacy bare embeds included).
        "presentation": {"mode": "per_product", **doc},
        "products": [item_id],
        "action": {"kind": "buttons", "entries": [{"product_id": item_id}]},
    }
    draft.update(over)
    return draft


class ShopPublisherApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Step Zero", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="step0-csrf")

    def preview(self, payload, csrf=True):
        headers = {"X-CSRF-Token": "step0-csrf"} if csrf else {}
        return self.client.post("/api/shop-publisher/preview", json=payload, headers=headers)

    def test_catalog_groups_products_by_existing_type_only(self):
        seed_template("banner", '{"content": "hi", "embeds": []}')
        seed_product("Zulu", kind="title", price=10)
        seed_product("Alpha", kind="role", price=20)
        seed_product("Midnight", kind="prestige", price=2500, prestige_tier=3)
        response = self.client.get("/api/shop-publisher/catalog")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["templates"], ["banner"],
                         "names-only catalog contract (explicit final Step 0 decision)")
        self.assertEqual([p["name"] for p in data["products"]],
                         ["Alpha", "Midnight", "Zulu"])
        self.assertEqual([p["type"] for p in data["products"]],
                         ["role", "prestige", "title"])
        for field in ("id", "name", "type", "price", "price_diamonds",
                      "enabled", "current_stock", "max_stock",
                      "prestige_tier", "category_id", "option_of_id"):
            self.assertIn(field, data["products"][0], field)

    def test_catalog_products_carry_the_category_link(self):
        """The catalog product rows carry the additive category_id link
        (the Slice 1 mapping; presentation metadata, never publication)."""
        first = seed_product("Wand", kind="item", price=50)
        second = seed_product("Cape", kind="item", price=60)
        execute("UPDATE shop_items SET category_id = 42 WHERE id = ?", (first,))
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        by_id = {p["id"]: p for p in data["products"]}
        self.assertEqual(by_id[first]["category_id"], 42)
        self.assertIsNone(by_id[second]["category_id"],
                          "unassigned products stay uncategorized")
        self.assertEqual(
            set(by_id[first]),
            {"id", "name", "type", "price", "price_diamonds", "enabled",
             "current_stock", "max_stock", "prestige_tier", "featured",
             "category_id", "option_of_id"},
            "category_id and root/option relationship are additive catalog metadata")
        self.assertIsNone(by_id[first]["option_of_id"])
        execute("UPDATE shop_items SET category_id = NULL WHERE id = ?", (first,))
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        by_id = {p["id"]: p for p in data["products"]}
        self.assertIsNone(by_id[first]["category_id"],
                          "the link is nullable — unassign reads back as NULL")

    def test_catalog_adds_option_parent_metadata_without_filtering_rows(self):
        root = seed_product("Potion", kind="potion", price=100)
        option = seed_product("7 Days", kind="potion", price=25)
        execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (root, option))
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        by_id = {product["id"]: product for product in data["products"]}
        self.assertIn(root, by_id)
        self.assertIn(option, by_id, "the existing catalog row set remains unchanged")
        self.assertIsNone(by_id[root]["option_of_id"])
        self.assertEqual(by_id[option]["option_of_id"], root)

    def test_catalog_carries_the_fixed_token_catalog(self):
        import utils.shop_publisher as SP
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        self.assertEqual(data["tokens"], SP.token_catalog_payload())
        keys = [t["key"] for t in data["tokens"]]
        self.assertEqual(keys, list(SP.TOKEN_CATALOG),
                         "the UI reference and the resolver cannot drift")

    def test_preview_resolves_a_design_draft_and_returns_the_action(self):
        item_id = seed_product()
        response = self.preview(draft_for(item_id))
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIs(data["success"], True)
        preview = data["preview"]
        self.assertEqual(preview["mode"], "per_product")
        self.assertEqual(preview["content"], "Buy VIP Role for 1,500 🪙 Coins!")
        self.assertEqual(preview["embeds"][0]["title"], "VIP Role")
        self.assertEqual(preview["embeds"][0]["description"], "The VIP role.")
        action = preview["action"]
        self.assertEqual(action["kind"], "buttons")
        entry = action["entries"][0]
        self.assertEqual(entry["custom_id"], f"shop_buy_{item_id}")
        self.assertEqual(entry["label"], "Buy VIP Role")
        self.assertEqual(entry["style"], "green")
        self.assertIs(entry["free"], False)
        self.assertEqual(preview["warnings"], [])
        used = {u["token"]: u["value"] for u in preview["tokens"]["used"]}
        self.assertEqual(used["{{product.name}}"], "VIP Role")
        self.assertEqual(used["{{product.price_display}}"], "1,500 🪙 Coins")

    def test_preview_matches_the_shared_resolver_byte_for_byte(self):
        import json
        import utils.shop_publisher as SP
        from utils.currency import get_currency_config
        item_id = seed_product()
        draft = draft_for(item_id, '{"content": "{{product.name}} {{product.stock}}", "embeds": []}')
        data = self.preview(draft).get_json()
        row = rows("SELECT id, name, type, price, price_diamonds, enabled, "
                   "current_stock, max_stock, prestige_tier, featured, "
                   "description, duration_hours, required_level, rarity, icon_url, "
                   "option_of_id FROM shop_items WHERE id = ?", (item_id,))[0]
        product = dict(zip(("id", "name", "type", "price", "price_diamonds", "enabled",
                            "current_stock", "max_stock", "prestige_tier", "featured",
                            "description", "duration_hours", "required_level",
                            "rarity", "icon_url", "option_of_id"), row))
        direct = SP.preview_design(draft, {item_id: product},
                                   asyncio.run(get_currency_config(GUILD)))
        self.assertEqual(json.dumps(data["preview"], sort_keys=True),
                         json.dumps(direct, sort_keys=True),
                         "the route must not resolve anything on its own")

    def test_preview_warnings_survive_the_api(self):
        item_id = seed_product(description=None, enabled=0, current_stock=0)
        draft = draft_for(item_id, '{"content": "{{nope}} {{product.description}}", "embeds": []}')
        data = self.preview(draft).get_json()
        self.assertEqual([w["code"] for w in data["preview"]["warnings"]],
                         ["product_disabled", "product_out_of_stock",
                          "unknown_token", "empty_value"])

    def test_product_select_root_roster_and_entry_count_contract(self):
        roots = [seed_product(f"Root {i}", kind="custom", price=100 + i)
                 for i in range(26)]

        def select_payload(ids, roster=None):
            chosen = ids[0] if ids else roots[0]
            return draft_for(
                chosen,
                products=roots if roster is None else roster,
                action={"kind": "product_select",
                        "placeholder": "Choose a product",
                        "entries": [{"product_id": pid} for pid in ids]})

        self.assertEqual(self.preview(select_payload(roots[:2])).status_code, 200)
        self.assertEqual(self.preview(select_payload(roots[:25])).status_code, 200,
                         "the full 25-choice boundary is accepted")
        too_few = self.preview(select_payload(roots[:1]))
        self.assertEqual(too_few.status_code, 400)
        self.assertIn("action_entry_count", [p["code"] for p in too_few.get_json()["problems"]])
        too_many = self.preview(select_payload(roots))
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("action_entry_count", [p["code"] for p in too_many.get_json()["problems"]])
        duplicate = self.preview(select_payload([roots[0], roots[0]]))
        self.assertIn("duplicate_entry", [p["code"] for p in duplicate.get_json()["problems"]])
        unrostered = self.preview(select_payload([roots[0], roots[1]], roster=[roots[0]]))
        self.assertIn("entry_not_in_roster", [p["code"] for p in unrostered.get_json()["problems"]])

        option = seed_product("Root option", kind="custom", price=50)
        execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (roots[0], option))
        option_select = self.preview(select_payload([option, roots[1]], roster=[roots[0], roots[1]]))
        self.assertIn("option_in_product_select",
                      [p["code"] for p in option_select.get_json()["problems"]])

    def test_option_select_is_one_rostered_root_and_direct_options_only(self):
        root = seed_product("Health Potion", kind="potion", price=100)
        other_root = seed_product("Mana Potion", kind="potion", price=120)
        options = [seed_product(f"Health option {i}", kind="potion", price=10 + i)
                   for i in range(26)]
        for option in options:
            execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (root, option))
        other_option = seed_product("Mana option", kind="potion", price=15)
        execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (other_root, other_option))

        def option_payload(entries, roster):
            return {
                "presentation": {"mode": "frame", "content": "Pick a duration", "embeds": []},
                "products": roster,
                "action": {"kind": "option_select", "placeholder": "Choose duration",
                           "entries": entries},
            }

        entries25 = [{"product_id": pid, "label": f"Choice {i}",
                      "description": f"Description {i}", "emoji": "⏳"}
                     for i, pid in enumerate(options[:25])]
        valid = self.preview(option_payload(entries25, [root]))
        self.assertEqual(valid.status_code, 200, valid.get_data(as_text=True))
        descriptor = valid.get_json()["preview"]["action"]
        self.assertEqual(descriptor["kind"], "option_select")
        self.assertEqual(descriptor["placeholder"], "Choose duration")
        self.assertEqual([entry["product_id"] for entry in descriptor["entries"]], options[:25])
        self.assertEqual([entry["custom_id"] for entry in descriptor["entries"]],
                         [f"shop_buy_{pid}" for pid in options[:25]])

        too_few = self.preview(option_payload(entries25[:1], [root]))
        self.assertIn("action_entry_count", [p["code"] for p in too_few.get_json()["problems"]])
        too_many = self.preview(option_payload(entries25 + [
            {"product_id": options[25]}], [root]))
        self.assertIn("action_entry_count", [p["code"] for p in too_many.get_json()["problems"]])
        duplicate = self.preview(option_payload(
            [{"product_id": options[0]}, {"product_id": options[0]}], [root]))
        self.assertIn("duplicate_entry", [p["code"] for p in duplicate.get_json()["problems"]])
        root_entry = self.preview(option_payload(
            [{"product_id": root}, {"product_id": options[0]}], [root]))
        self.assertIn("root_in_option_select",
                      [p["code"] for p in root_entry.get_json()["problems"]])
        other_family = self.preview(option_payload(
            [{"product_id": options[0]}, {"product_id": other_option}],
            [root, other_root]))
        self.assertIn("option_select_multi_family",
                      [p["code"] for p in other_family.get_json()["problems"]])
        unrostered = self.preview(option_payload(
            [{"product_id": options[0]}, {"product_id": options[1]}], [other_root]))
        self.assertIn("option_outside_roster",
                      [p["code"] for p in unrostered.get_json()["problems"]])

    def test_roster_rejects_option_rows_and_rogue_entries(self):
        """The locked roster contract at the route: option rows cannot be
        roster products, and an option entry needs its root in products[].
        the persisted option_of_id field is loaded into preview rows, so
        this pins the route's 400 + problems shape alongside the pure suite."""
        item_id = seed_product()
        other_id = seed_product("Sword", kind="custom", price=250)

        # Malformed roster/action shapes are 400s with pathed problems.
        for payload, expected_code in (
            (draft_for(item_id, products=[]), "empty_roster"),
            (draft_for(item_id, products=["101"]), "invalid_product_ref"),
            (draft_for(item_id, products=[item_id, 999]), "unknown_product"),
            (draft_for(item_id, products=[item_id],
                       action={"kind": "menus", "entries": []}), "unknown_action_kind"),
            (draft_for(item_id, products=[other_id],
                       action={"kind": "buttons",
                               "entries": [{"product_id": item_id}]}), "entry_not_in_roster"),
        ):
            with self.subTest(expected_code=expected_code):
                response = self.preview(payload)
                self.assertEqual(response.status_code, 400)
                body = response.get_json()
                self.assertIs(body["success"], False)
                self.assertIn(expected_code, [p["code"] for p in body["problems"]])
                self.assertTrue(body["error"])

    def test_missing_or_malformed_draft_parts_are_400_never_500(self):
        item_id = seed_product()
        for payload in ({}, {"presentation": "x"}, {"presentation": {}},
                        {"presentation": {}, "products": "x"},
                        {"presentation": {}, "products": []},
                        {"presentation": {}, "products": [True]},
                        {"presentation": {}, "products": ["3"]},
                        {"presentation": {}, "products": [1.5]},
                        {"presentation": {}, "products": [item_id]},
                        {"presentation": {}, "products": [item_id], "action": []}):
            with self.subTest(payload=payload):
                response = self.preview(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIs(response.get_json()["success"], False)
                self.assertTrue(response.get_json()["error"])

    def test_legacy_bare_embed_rows_preview_as_one_embed(self):
        item_id = seed_product()
        draft = draft_for(item_id, {"title": "Only an embed", "description": "old"})
        data = self.preview(draft).get_json()
        self.assertEqual(len(data["preview"]["embeds"]), 1)
        self.assertEqual(data["preview"]["embeds"][0]["title"], "Only an embed")

    def test_preview_and_catalog_are_read_only(self):
        item_id = seed_product()
        before = mutation_snapshot()
        before_audit = rows("SELECT * FROM audit_log")
        self.assertEqual(self.preview(draft_for(item_id)).status_code, 200)
        self.assertEqual(self.client.get("/api/shop-publisher/catalog").status_code, 200)
        self.assertEqual(mutation_snapshot(), before,
                         "previewing must never touch a store")
        self.assertEqual(rows("SELECT * FROM audit_log"), before_audit,
                         "previewing is not an action worth an audit row")

    def test_csrf_is_still_enforced_on_preview(self):
        item_id = seed_product()
        before = mutation_snapshot()
        response = self.preview(draft_for(item_id), csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(mutation_snapshot(), before)

    def test_owner_level_is_required_on_both_routes(self):
        item_id = seed_product()
        execute("UPDATE dashboard_users SET permission_level='admin'")
        self.assertEqual(self.client.get("/api/shop-publisher/catalog").status_code, 403)
        self.assertEqual(self.preview(draft_for(item_id)).status_code, 403)
        execute("UPDATE dashboard_users SET permission_level='moderator'")
        self.assertEqual(self.client.get("/api/shop-publisher/catalog").status_code, 403)
        execute("DELETE FROM dashboard_users")
        self.assertEqual(self.preview(draft_for(item_id)).status_code, 403)

    def test_products_from_another_guild_are_invisible(self):
        execute("INSERT INTO shop_items (guild_id, name, type, price) "
                "VALUES (9999, 'Elsewhere', 'role', 5)")
        elsewhere = rows("SELECT id FROM shop_items")[0][0]
        data = self.client.get("/api/shop-publisher/catalog").get_json()
        self.assertEqual(data["products"], [])
        response = self.preview(draft_for(elsewhere))
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown_product",
                      [p["code"] for p in response.get_json()["problems"]],
                      "cross-guild ids are invisible, not leaking")

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
