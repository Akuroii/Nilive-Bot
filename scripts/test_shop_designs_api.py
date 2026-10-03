#!/usr/bin/env python3
"""
Shop Publisher — saved Design drafts API (Design Draft Persistence).

What this locks down
  1.  GET    /api/shop-publisher/designs — full records, session-guild only.
  2.  POST   /api/shop-publisher/designs — create (id absent/null) OR
      full-overwrite update (id present); immutable id/guild_id/created_at;
      updated_at refreshed on every successful update.
  3.  DELETE /api/shop-publisher/designs/<id> — hard delete; the second
      delete is the same indistinguishable 404 as an unknown/cross-guild id.
  4.  The persisted presentation is the design's OWN snapshot: normalized at
      save; source_template_name is provenance only and later source-template
      edits/deletion never mutate the saved design or its preview.
  5.  Stale product references surface the EXISTING validate_design() codes
      (unknown_product, ...) at preview time; `unknown_design` is only for the
      design id in this CRUD. No new product-reference error code exists.
  6.  Validation at save reuses the Step 0 design contract (empty_roster,
      invalid_product_ref, unknown_product, entry_not_in_roster, ...).
  7.  LEVEL_OWNER + session-guild isolation (body guild_id ignored) + the
      shared blueprint CSRF enforcement on POST/DELETE.
  8.  The routes write ONLY shop_designs — no audit rows, no other table.

Run:  python3 scripts/test_shop_designs_api.py
"""

import asyncio
import json
import time
import unittest

from phase1_support import (
    GUILD, USER, execute, rows, mutation_snapshot, reset_database,
)
import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

VALID_DRAFT = {
    "presentation": {"mode": "per_product", "content": "Buy {{product.name}}!",
                     "embeds": [{"title": "{{product.name}}"}]},
    "products": [],
    "action": {"kind": "buttons", "entries": []},
}


def draft_for(item_id, **over):
    draft = json.loads(json.dumps(VALID_DRAFT))
    draft["products"] = [item_id]
    draft["action"] = {"kind": "buttons", "entries": [{"product_id": item_id}]}
    draft.update(over)
    return draft


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


class ShopDesignsApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Step Zero", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="step0-csrf")

    # ── helpers ──────────────────────────────────────────────────────
    def get(self):
        return self.client.get("/api/shop-publisher/designs")

    def post(self, payload, csrf=True):
        headers = {"X-CSRF-Token": "step0-csrf"} if csrf else {}
        return self.client.post("/api/shop-publisher/designs",
                                json=payload, headers=headers)

    def delete(self, design_id, csrf=True):
        headers = {"X-CSRF-Token": "step0-csrf"} if csrf else {}
        return self.client.delete(f"/api/shop-publisher/designs/{design_id}",
                                  headers=headers)

    def save(self, name="Main", item_id=None, **over):
        payload = {"name": name,
                   "source_template_name": over.pop("source_template_name", "banner"),
                   "design": draft_for(item_id if item_id is not None else seed_product())}
        payload.update(over)
        return self.post(payload)

    # ── CRUD semantics ───────────────────────────────────────────────
    def test_create_assigns_id_and_returns_the_record(self):
        item_id = seed_product()
        response = self.save(item_id=item_id)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIs(body["success"], True)
        design = body["design"]
        self.assertIsInstance(design["id"], int)
        self.assertEqual(design["name"], "Main")
        self.assertEqual(design["source_template_name"], "banner")
        self.assertEqual(design["design"]["products"], [item_id])
        self.assertTrue(design["created_at"] and design["updated_at"])

    def test_save_normalizes_the_presentation_snapshot_and_stores_config_only(self):
        item_id = seed_product()
        # A legacy bare-embed document + no mode normalizes on save into the
        # design's own snapshot ({mode: per_product, content: '', embeds: [..]}).
        draft = draft_for(item_id)
        draft["presentation"] = {"title": "Only an embed", "description": "old"}
        draft["presentation"]["mode"] = "per_product"
        draft["action"]["custom_id"] = "shop_buy_999"
        draft["action"]["warnings"] = ["resolved warning"]
        draft["rendered"] = {"content": "already rendered"}
        body = self.post({"name": "Bare", "source_template_name": None,
                          "design": draft, "price": 12345,
                          "tokens": {"product.name": "snapshot"}}).get_json()
        saved = body["design"]["design"]
        presentation = saved["presentation"]
        self.assertEqual(presentation["mode"], "per_product")
        self.assertEqual(presentation["content"], "")
        self.assertEqual([e["title"] for e in presentation["embeds"]],
                         ["Only an embed"])
        self.assertEqual(set(saved), {"presentation", "products", "action"})
        self.assertEqual(set(saved["action"]), {"kind", "entries"})
        self.assertNotIn("custom_id", saved["action"])
        self.assertNotIn("warnings", saved["action"])
        self.assertIsNone(body["design"]["source_template_name"])

    def test_product_select_action_round_trips_config_only(self):
        first = seed_product("Potion", "custom", 100)
        second = seed_product("Sword", "custom", 200)
        draft = draft_for(first, products=[first, second])
        draft["action"] = {
            "kind": "product_select",
            "placeholder": "Choose a product",
            "entries": [
                {"product_id": second, "label": "Sword label", "description": "Sword desc",
                 "emoji": "⚔️", "custom_id": "must-not-persist", "price": 999999},
                {"product_id": first, "label": "Potion label", "description": "Potion desc",
                 "emoji": "🧪"},
            ],
        }
        response = self.post({"name": "Select", "design": draft})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        record = response.get_json()["design"]
        expected_action = {
            "kind": "product_select",
            "placeholder": "Choose a product",
            "entries": [
                {"product_id": second, "label": "Sword label", "description": "Sword desc",
                 "emoji": "⚔️"},
                {"product_id": first, "label": "Potion label", "description": "Potion desc",
                 "emoji": "🧪"},
            ],
        }
        self.assertEqual(record["design"]["action"], expected_action)
        loaded = self.get().get_json()["designs"][0]
        self.assertEqual(loaded["design"]["action"], expected_action)
        self.assertEqual(set(record["design"]), {"presentation", "products", "action"})

    def test_option_select_action_round_trips_direct_options_only(self):
        root = seed_product("Health Potion", "potion", 100)
        option7 = seed_product("7 Days", "potion", 25)
        option30 = seed_product("30 Days", "potion", 45)
        execute("UPDATE shop_items SET option_of_id=? WHERE id IN (?,?)",
                (root, option7, option30))
        draft = draft_for(root, products=[root])
        draft["action"] = {
            "kind": "option_select",
            "placeholder": "Choose duration",
            "entries": [
                {"product_id": option7, "label": "Seven days", "description": "A week",
                 "emoji": "⏳", "custom_id": "discard", "price": 999999},
                {"product_id": option30, "label": "Thirty days", "description": "A month",
                 "emoji": "📅"},
            ],
        }
        response = self.post({"name": "Potion options", "design": draft})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        expected = {
            "kind": "option_select", "placeholder": "Choose duration",
            "entries": [
                {"product_id": option7, "label": "Seven days", "description": "A week", "emoji": "⏳"},
                {"product_id": option30, "label": "Thirty days", "description": "A month", "emoji": "📅"},
            ],
        }
        saved = response.get_json()["design"]["design"]["action"]
        self.assertEqual(saved, expected)
        loaded = self.get().get_json()["designs"][0]["design"]["action"]
        self.assertEqual(loaded, expected)
        self.assertEqual(rows("SELECT option_of_id FROM shop_items WHERE id IN (?,?) ORDER BY id",
                              (option7, option30)), [(root,), (root,)])

    def test_update_is_a_full_overwrite(self):
        item_id = seed_product()
        created = self.save(name="Old", item_id=item_id,
                            source_template_name="banner").get_json()["design"]
        # Backdate updated_at so the refresh is observable (1s clock resolution).
        execute("UPDATE shop_designs SET updated_at = '2000-01-01 00:00:00' "
                "WHERE id = ?", (created["id"],))
        other = seed_product("Sword", "custom", 250)
        response = self.post({"id": created["id"], "name": "New",
                              "source_template_name": None,
                              "design": draft_for(other)})
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()["design"]
        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["name"], "New")
        self.assertIsNone(updated["source_template_name"])
        self.assertEqual(updated["design"]["products"], [other],
                         "full overwrite replaces the whole stored draft")
        self.assertEqual(updated["created_at"], created["created_at"],
                         "created_at is immutable")
        self.assertNotEqual(updated["updated_at"], "2000-01-01 00:00:00",
                            "updated_at is refreshed on every successful update")

    def test_unknown_and_cross_guild_ids_are_indistinguishable(self):
        item_id = seed_product()
        created = self.save(item_id=item_id).get_json()["design"]
        execute("INSERT INTO shop_designs (guild_id, name, design_json) "
                "VALUES (9999, 'Elsewhere', ?)",
                (json.dumps(draft_for(item_id)),))
        elsewhere = rows("SELECT id FROM shop_designs WHERE guild_id = 9999")[0][0]
        for payload in ({"id": 999999, "name": "", "design": {}},
                        {"id": elsewhere, "name": "X",
                         "design": draft_for(item_id)}):
            response = self.post(payload)
            self.assertEqual(response.status_code, 404)
            problems = response.get_json()["problems"]
            self.assertEqual([p["code"] for p in problems], ["unknown_design"])
        for design_id in (999999, elsewhere):
            response = self.delete(design_id)
            self.assertEqual(response.status_code, 404)
            self.assertEqual([p["code"] for p in response.get_json()["problems"]],
                             ["unknown_design"])
        self.assertEqual(len(self.get().get_json()["designs"]), 1,
                         "cross-guild rows stay invisible and untouched")

    def test_delete_is_hard_and_repeats_404(self):
        item_id = seed_product()
        created = self.save(item_id=item_id).get_json()["design"]
        self.assertIs(self.delete(created["id"]).get_json()["success"], True)
        self.assertEqual(rows("SELECT id FROM shop_designs WHERE id = ?",
                              (created["id"],)), [])
        self.assertEqual(self.delete(created["id"]).status_code, 404)

    def test_list_returns_full_records_for_the_session_guild_only(self):
        item_id = seed_product()
        self.save(name="Alpha", item_id=item_id)
        self.save(name="Beta", item_id=item_id)
        execute("INSERT INTO shop_designs (guild_id, name, design_json) "
                "VALUES (9999, 'Elsewhere', ?)", (json.dumps(draft_for(item_id)),))
        designs = self.get().get_json()["designs"]
        self.assertEqual([d["name"] for d in designs], ["Alpha", "Beta"])
        self.assertEqual(set(designs[0]),
                         {"id", "name", "source_template_name", "design",
                          "created_at", "updated_at"})
        self.assertEqual(designs[0]["design"]["presentation"]["content"],
                         "Buy {{product.name}}!")

    # ── validation (existing codes only) ─────────────────────────────
    def test_bad_name_and_bool_id_are_rejected(self):
        item_id = seed_product()
        response = self.post({"id": True, "name": "", "design": draft_for(item_id)})
        self.assertEqual(response.status_code, 400)
        codes = [p["code"] for p in response.get_json()["problems"]]
        self.assertIn("invalid_id", codes)
        self.assertIn("invalid_name", codes)
        response = self.post({"name": "x" * 101, "design": draft_for(item_id)})
        self.assertEqual([p["code"] for p in response.get_json()["problems"]],
                         ["invalid_name"])
        response = self.post({"name": "Ok", "source_template_name": "x" * 101,
                              "design": draft_for(item_id)})
        self.assertEqual([p["code"] for p in response.get_json()["problems"]],
                         ["invalid_source_template"])

    def test_save_reuses_the_design_problem_codes(self):
        item_id = seed_product()
        cases = (
            (draft_for(item_id, products=[]), "empty_roster"),
            (draft_for(item_id, products=["101"]), "invalid_product_ref"),
            (draft_for(item_id, products=[item_id, 999]), "unknown_product"),
            (draft_for(item_id, products=[seed_product("Other", "role", 5)],
                       action={"kind": "buttons",
                               "entries": [{"product_id": item_id}]}),
             "entry_not_in_roster"),
        )
        for draft, expected in cases:
            with self.subTest(expected_code=expected):
                response = self.post({"name": "Bad", "design": draft})
                self.assertEqual(response.status_code, 400)
                codes = [p["code"] for p in response.get_json()["problems"]]
                self.assertIn(expected, codes)
                self.assertNotIn("unknown_design", codes,
                                 "unknown_design is only for the design id")

    def test_cross_guild_products_are_unknown_products(self):
        execute("INSERT INTO shop_items (guild_id, name, type, price) "
                "VALUES (9999, 'Elsewhere', 'role', 5)")
        elsewhere = rows("SELECT id FROM shop_items")[0][0]
        response = self.post({"name": "Bad", "design": draft_for(elsewhere)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown_product",
                      [p["code"] for p in response.get_json()["problems"]])

    # ── enforcement ──────────────────────────────────────────────────
    def test_body_guild_id_is_ignored(self):
        item_id = seed_product()
        response = self.post({"guild_id": 9999, "name": "Mine",
                              "design": draft_for(item_id)})
        self.assertIs(response.get_json()["success"], True)
        row = rows("SELECT guild_id FROM shop_designs")[0]
        self.assertEqual(row[0], GUILD)

    def test_owner_level_is_required_on_all_three_routes(self):
        item_id = seed_product()
        created = self.save(item_id=item_id).get_json()["design"]
        for level in ("admin", "moderator"):
            execute("UPDATE dashboard_users SET permission_level=?", (level,))
            self.assertEqual(self.get().status_code, 403)
            self.assertEqual(self.post({"name": "X", "design": draft_for(item_id)}).status_code, 403)
            self.assertEqual(self.delete(created["id"]).status_code, 403)
        execute("DELETE FROM dashboard_users")
        self.assertEqual(self.get().status_code, 403)
        self.assertEqual(self.post({"name": "X", "design": draft_for(item_id)}).status_code, 403)
        self.assertEqual(self.delete(created["id"]).status_code, 403)

    def test_csrf_is_enforced_on_post_and_delete(self):
        item_id = seed_product()
        created = self.save(item_id=item_id).get_json()["design"]
        before = mutation_snapshot()
        self.assertEqual(self.get().status_code, 200, "GET is CSRF-exempt")
        self.assertEqual(self.post({"name": "X", "design": draft_for(item_id)},
                                   csrf=False).status_code, 403)
        self.assertEqual(self.delete(created["id"], csrf=False).status_code, 403)
        self.assertEqual(mutation_snapshot(), before)

    def test_writes_only_shop_designs_and_no_audit_rows(self):
        item_id = seed_product()
        created = self.save(item_id=item_id).get_json()["design"]
        before = mutation_snapshot()
        before_designs = rows("SELECT * FROM shop_designs ORDER BY id")
        before_audit = rows("SELECT * FROM audit_log")
        self.assertIs(self.post({"id": created["id"], "name": "Renamed",
                                 "design": draft_for(item_id)}).get_json()["success"], True)
        updated = rows("SELECT * FROM shop_designs ORDER BY id")
        self.assertNotEqual(updated, before_designs,
                            "the save mutates the shop_designs row")
        self.assertEqual(mutation_snapshot(), before,
                         "commerce, inventory and purchase stores stay untouched")
        self.assertIs(self.delete(created["id"]).get_json()["success"], True)
        self.assertEqual(rows("SELECT * FROM audit_log"), before_audit,
                         "saving/deleting a draft is not an action worth an audit row")

    # ── snapshot + staleness semantics ───────────────────────────────
    def test_snapshot_is_immune_to_source_template_edits_and_deletion(self):
        execute("INSERT INTO embed_templates (guild_id, name, data) "
                "VALUES (?, 'banner', ?)",
                (GUILD, json.dumps({"content": "Original {{product.name}}",
                                    "embeds": [{"title": "T"}]})))
        item_id = seed_product()
        # The page loaded the template document into the draft; Save persists
        # that presentation as the design's OWN snapshot.
        draft = draft_for(item_id, presentation={
            "mode": "per_product", "content": "Original {{product.name}}",
            "embeds": [{"title": "T"}]})
        created = self.post({"name": "Main", "source_template_name": "banner",
                             "design": draft}).get_json()["design"]
        execute("UPDATE embed_templates SET data = ? WHERE name = 'banner'",
                (json.dumps({"content": "EDITED", "embeds": []}),))
        execute("DELETE FROM embed_templates WHERE name = 'banner'")
        # The saved design still owns its presentation snapshot.
        record = self.get().get_json()["designs"][0]
        self.assertEqual(record["design"]["presentation"]["content"],
                         "Original {{product.name}}")
        preview = self.client.post("/api/shop-publisher/preview",
                                   json=record["design"],
                                   headers={"X-CSRF-Token": "step0-csrf"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.get_json()["preview"]["content"],
                         "Original VIP Role")
        self.assertEqual(record["source_template_name"], "banner",
                         "provenance is stored but never dereferenced")

    def test_stale_product_surfaces_the_existing_unknown_product_code(self):
        item_id = seed_product()
        created = self.save(item_id=item_id).get_json()["design"]
        execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        record = self.get().get_json()["designs"][0]
        self.assertEqual(record["design"]["products"], [item_id],
                         "GET returns the stored draft untouched")
        preview = self.client.post("/api/shop-publisher/preview",
                                   json=record["design"],
                                   headers={"X-CSRF-Token": "step0-csrf"})
        self.assertEqual(preview.status_code, 400)
        codes = [p["code"] for p in preview.get_json()["problems"]]
        self.assertIn("unknown_product", codes,
                      "the existing validate_design code — no unknown_design family")
        self.assertNotIn("unknown_design", codes)

    def test_save_rejects_unsupported_presentation_mode_but_accepts_supported_modes(self):
        item_id = seed_product()
        for mode in ("per_product", "frame"):
            draft = draft_for(item_id)
            draft["presentation"]["mode"] = mode
            response = self.post({"name": f"Design {mode}", "design": draft})
            self.assertEqual(response.status_code, 200, response.get_json())
        draft = draft_for(item_id)
        draft["presentation"]["mode"] = "execute-code"
        response = self.post({"name": "Bad mode", "design": draft})
        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid_presentation_mode",
                      [p["code"] for p in response.get_json()["problems"]])
        self.assertEqual(rows("SELECT name FROM shop_designs"),
                         [("Design per_product",), ("Design frame",)])

    def test_json_non_object_save_body_returns_validation_400(self):
        headers = {"X-CSRF-Token": "step0-csrf"}
        for body in ([], "text", 7, None):
            response = self.client.post("/api/shop-publisher/designs", json=body,
                                        headers=headers)
            self.assertEqual(response.status_code, 400, repr(body))
            self.assertEqual(response.get_json()["problems"][0]["code"], "invalid_body")

    def test_deleted_category_never_touches_a_saved_design(self):
        item_id = seed_product()
        execute("INSERT INTO shop_categories (guild_id, name) VALUES (?, 'Roles')",
                (GUILD,))
        cat_id = rows("SELECT id FROM shop_categories")[0][0]
        execute("UPDATE shop_items SET category_id = ? WHERE id = ?",
                (cat_id, item_id))
        created = self.save(item_id=item_id).get_json()["design"]
        execute("DELETE FROM shop_categories WHERE id = ?", (cat_id,))
        record = self.get().get_json()["designs"][0]
        self.assertEqual(record["design"], created["design"],
                         "designs hold no category data — deletion cannot touch them")
        preview = self.client.post("/api/shop-publisher/preview",
                                   json=record["design"],
                                   headers={"X-CSRF-Token": "step0-csrf"})
        self.assertEqual(preview.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
