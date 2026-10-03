#!/usr/bin/env python3
"""
Shop Publisher — Step 1 Slice 1: Category CRUD API, on the real Flask app,
real permissions, real CSRF and a scratch SQLite database (no HTTP server).

What this locks down
  1.  POST /api/shop-publisher/categories CREATES when `id` is absent/null
      (server-assigned id, default emoji 🎫 / enabled 1 / sort_order 0) and
      UPDATES only when `id` exists in the SESSION guild.
  2.  Unknown ids and ids from ANOTHER guild return the same indistinguishable
      404 {"code": "unknown_category"} bodies — cross-guild existence is never
      disclosed, and no request can move or mutate a category across guilds
      (guild_id comes only from the session; body guild_id is ignored).
  3.  Exactly name/emoji/enabled/sort_order are mutable; id, guild_id and
      created_at are immutable; updated_at is refreshed on successful
      update and reorder.
  4.  DELETE NULLs the matching shop_items.category_id links (products become
      uncategorized — never stale) and writes NOTHING else.
  5.  Product↔category assignment requires both rows in the session guild;
      the ONLY shop_items column any Category operation writes is the
      nullable category_id link — every commerce field is byte-identical
      before/after every operation.
  6.  Reorder is all-or-nothing (unknown/cross-guild/duplicate rejects the
      whole request before any write) and sets sort_order = index with a
      refreshed updated_at.
  7.  Access is LEVEL_OWNER and CSRF still applies to every write.

Run:  python3 scripts/test_shop_categories_api.py
"""

import time
import unittest

from phase1_support import (
    GUILD, USER, execute, rows, mutation_snapshot, reset_database,
)
import dashboard.app as dashboard
import asyncio

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

OTHER = 9101  # a second guild — used to prove cross-guild isolation

_COMMERCE_FIELDS = (
    "name", "description", "price", "type", "role_id", "duration_hours",
    "show_button", "limited", "limited_until", "max_stock", "current_stock",
    "featured", "required_level", "required_role_id", "enabled",
    "price_diamonds", "xp_boost_multiplier", "prestige_tier", "rarity",
    "icon_url",
)


def seed_category(name="Boosts", guild=GUILD, emoji="⚡", enabled=1,
                  sort_order=0):
    return execute(
        "INSERT INTO shop_categories (guild_id, name, emoji, enabled, "
        "sort_order) VALUES (?, ?, ?, ?, ?)",
        (guild, name, emoji, enabled, sort_order))


def seed_item(name="VIP Role", guild=GUILD, category_id=None, **over):
    columns = {
        "type": "role", "price": 1500, "description": "The VIP role.",
        "price_diamonds": None, "role_id": 99, "duration_hours": None,
        "max_stock": 20, "current_stock": 12, "required_level": 5,
        "rarity": "epic", "icon_url": "https://x/i.png",
        "prestige_tier": None, "enabled": 1, "featured": 0,
        "xp_boost_multiplier": None,
    }
    columns.update(over)
    return execute(
        "INSERT INTO shop_items (guild_id, name, type, price, description, "
        "price_diamonds, role_id, duration_hours, max_stock, current_stock, "
        "required_level, rarity, icon_url, prestige_tier, enabled, featured, "
        "xp_boost_multiplier, category_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (guild, name, columns["type"], columns["price"],
         columns["description"], columns["price_diamonds"],
         columns["role_id"], columns["duration_hours"], columns["max_stock"],
         columns["current_stock"], columns["required_level"],
         columns["rarity"], columns["icon_url"], columns["prestige_tier"],
         columns["enabled"], columns["featured"],
         columns["xp_boost_multiplier"], category_id))


def item_row(item_id):
    keys = ("id", "guild_id", "category_id") + _COMMERCE_FIELDS
    row = rows(f"SELECT {', '.join(keys)} FROM shop_items WHERE id=?",
               (item_id,))[0]
    return dict(zip(keys, row))


def commerce_of(item_id):
    row = item_row(item_id)
    return {k: row[k] for k in _COMMERCE_FIELDS}


def category_row(cat_id):
    keys = ("id", "guild_id", "name", "emoji", "enabled", "sort_order",
            "created_at", "updated_at")
    row = rows(f"SELECT {', '.join(keys)} FROM shop_categories WHERE id=?",
               (cat_id,))[0]
    return dict(zip(keys, row))


class ShopCategoriesApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Step One", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="step1-csrf")

    def post(self, path, payload, csrf=True):
        headers = {"X-CSRF-Token": "step1-csrf"} if csrf else {}
        return self.client.post(path, json=payload, headers=headers)

    def delete(self, path, csrf=True):
        headers = {"X-CSRF-Token": "step1-csrf"} if csrf else {}
        return self.client.delete(path, headers=headers)

    def test_json_non_object_category_bodies_return_validation_400(self):
        for body in ([], "text", 7, None):
            for path in ("/api/shop-publisher/categories",
                         "/api/shop-publisher/categories/reorder",
                         "/api/shop-publisher/products/category"):
                response = self.post(path, body)
                self.assertEqual(response.status_code, 400, (path, body))
                self.assertEqual(response.get_json()["problems"][0]["code"],
                                 "invalid_body")

    # ── 1. create semantics ────────────────────────────────────────────
    def test_create_when_id_absent_or_null(self):
        response = self.post("/api/shop-publisher/categories",
                             {"name": "Boosts"})
        self.assertEqual(response.status_code, 200)
        category = response.get_json()["category"]
        self.assertIs(response.get_json()["success"], True)
        self.assertEqual(category["name"], "Boosts")
        self.assertEqual(category["emoji"], "🎫")
        self.assertEqual(category["enabled"], 1)
        self.assertEqual(category["sort_order"], 0)
        self.assertEqual(category["guild_id"] if "guild_id" in category else GUILD, GUILD)
        self.assertTrue(category["id"] >= 1)
        self.assertTrue(category["created_at"])
        self.assertTrue(category["updated_at"])
        self.assertEqual(category_row(category["id"])["guild_id"], GUILD)

        response = self.post("/api/shop-publisher/categories",
                             {"id": None, "name": "Roles"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["category"]["name"], "Roles")
        self.assertEqual(len(rows("SELECT id FROM shop_categories")), 2)

    def test_create_validation(self):
        cases = [
            ({}, "invalid_name"),
            ({"name": "   "}, "invalid_name"),
            ({"name": "x" * 101}, "invalid_name"),
            ({"name": "Ok", "emoji": "x" * 101}, "invalid_emoji"),
            ({"name": "Ok", "emoji": 5}, "invalid_emoji"),
            ({"name": "Ok", "enabled": 2}, "invalid_enabled"),
            ({"name": "Ok", "enabled": "1"}, "invalid_enabled"),
            ({"name": "Ok", "sort_order": -1}, "invalid_sort_order"),
            ({"name": "Ok", "sort_order": True}, "invalid_sort_order"),
            ({"id": 0, "name": "Ok"}, "invalid_id"),
            ({"id": "x", "name": "Ok"}, "invalid_id"),
        ]
        for payload, code in cases:
            response = self.post("/api/shop-publisher/categories", payload)
            self.assertEqual(response.status_code, 400, payload)
            data = response.get_json()
            self.assertIs(data["success"], False)
            self.assertIn(code, [p["code"] for p in data["problems"]], payload)
        self.assertEqual(rows("SELECT id FROM shop_categories"), [])

    # ── 2. update semantics ────────────────────────────────────────────
    def test_update_existing_in_guild(self):
        cat_id = seed_category("Old", emoji="A", enabled=1, sort_order=3)
        response = self.post("/api/shop-publisher/categories",
                             {"id": cat_id, "name": "New", "emoji": "B",
                              "enabled": 0, "sort_order": 7})
        self.assertEqual(response.status_code, 200)
        category = response.get_json()["category"]
        self.assertEqual((category["name"], category["emoji"],
                          category["enabled"], category["sort_order"]),
                         ("New", "B", 0, 7))
        self.assertEqual(category["id"], cat_id, "id is immutable")

        # Omitted optional fields are kept.
        response = self.post("/api/shop-publisher/categories",
                             {"id": cat_id, "name": "Kept"})
        category = response.get_json()["category"]
        self.assertEqual((category["emoji"], category["enabled"],
                          category["sort_order"]), ("B", 0, 7))

    def test_created_at_immutable_and_updated_at_refreshed(self):
        cat_id = seed_category()
        execute("UPDATE shop_categories SET created_at='2000-01-01 00:00:00',"
                " updated_at='2000-01-01 00:00:00' WHERE id=?", (cat_id,))
        self.post("/api/shop-publisher/categories",
                  {"id": cat_id, "name": "Refreshed"})
        row = category_row(cat_id)
        self.assertEqual(row["created_at"], "2000-01-01 00:00:00",
                         "created_at is immutable")
        self.assertNotEqual(row["updated_at"], "2000-01-01 00:00:00",
                            "updated_at is refreshed on update")

    def test_unknown_and_cross_guild_ids_are_indistinguishable(self):
        foreign = seed_category("Foreign", guild=OTHER)
        unknown = self.post("/api/shop-publisher/categories",
                            {"id": 424242, "name": "X"})
        cross = self.post("/api/shop-publisher/categories",
                          {"id": foreign, "name": "X"})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(unknown.get_json(), cross.get_json(),
                         "unknown and cross-guild must be identical")
        problems = unknown.get_json()["problems"]
        self.assertEqual([p["code"] for p in problems], ["unknown_category"])

        self.assertEqual(self.delete(
            f"/api/shop-publisher/categories/{foreign}").status_code, 404)
        self.assertEqual(self.delete(
            "/api/shop-publisher/categories/424242").status_code, 404)
        self.assertEqual(len(rows("SELECT id FROM shop_categories")), 1,
                         "the foreign row is untouched")
        self.assertEqual(category_row(foreign)["name"], "Foreign")

    def test_no_client_controlled_guild_id(self):
        response = self.post("/api/shop-publisher/categories",
                             {"name": "Mine", "guild_id": OTHER})
        cat_id = response.get_json()["category"]["id"]
        self.assertEqual(category_row(cat_id)["guild_id"], GUILD,
                         "guild_id comes only from the session")

        foreign = seed_category("Twin", guild=OTHER)
        self.post("/api/shop-publisher/categories",
                  {"id": cat_id, "name": "Mine v2", "guild_id": OTHER})
        self.assertEqual(category_row(cat_id)["name"], "Mine v2")
        self.assertEqual(category_row(foreign)["name"], "Twin",
                         "a body guild_id can never retarget the write")

    # ── 3. deletion semantics ──────────────────────────────────────────
    def test_delete_nulls_links_and_touches_nothing_else(self):
        cat_id = seed_category()
        other_cat = seed_category("Other cat")
        mine_a = seed_item("A", category_id=cat_id)
        mine_b = seed_item("B", category_id=cat_id)
        unassigned = seed_item("C")
        foreign_cat = seed_category("Foreign cat", guild=OTHER)
        foreign_item = seed_item("F", guild=OTHER, category_id=foreign_cat)
        before_a, before_b = commerce_of(mine_a), commerce_of(mine_b)
        before_f = commerce_of(foreign_item)

        response = self.delete(f"/api/shop-publisher/categories/{cat_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["unlinked_products"], 2)

        for item_id, before in ((mine_a, before_a), (mine_b, before_b)):
            after = commerce_of(item_id)
            self.assertEqual(after, before,
                             "commerce fields are byte-identical")
            self.assertIsNone(item_row(item_id)["category_id"],
                              "products become uncategorized — never stale")
        self.assertIsNone(item_row(unassigned)["category_id"])
        self.assertEqual(item_row(foreign_item)["category_id"], foreign_cat,
                         "other guilds' links are untouched")
        self.assertEqual(commerce_of(foreign_item), before_f)
        self.assertEqual(rows("SELECT id FROM shop_categories WHERE id=?",
                              (cat_id,)), [])
        self.assertEqual(len(rows("SELECT id FROM shop_categories")), 2)

    # ── 4. reorder semantics ───────────────────────────────────────────
    def test_reorder_sets_sort_order_by_index(self):
        first, second, third = (seed_category("One"), seed_category("Two"),
                                seed_category("Three"))
        for cat_id in (first, second, third):
            execute("UPDATE shop_categories SET updated_at='2000-01-01 00:00:00' "
                    "WHERE id=?", (cat_id,))
        response = self.post("/api/shop-publisher/categories/reorder",
                             {"order": [third, first, second]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([category_row(c)["sort_order"]
                          for c in (first, second, third)], [1, 2, 0])
        self.assertNotEqual(category_row(third)["updated_at"],
                            "2000-01-01 00:00:00",
                            "updated_at is refreshed on reorder")

    def test_reorder_is_all_or_nothing(self):
        first, second = seed_category("One"), seed_category("Two")
        foreign = seed_category("Foreign", guild=OTHER)
        for payload, code in (
                ({"order": [first, 424242]}, "unknown_category"),
                ({"order": [first, foreign]}, "unknown_category"),
                ({"order": [first, first]}, "duplicate_entry"),
                ({"order": "first"}, "invalid_reorder"),
                ({"order": [True]}, "invalid_reorder"),
        ):
            response = self.post("/api/shop-publisher/categories/reorder",
                                 payload)
            self.assertIn(response.status_code, (400, 404), payload)
            self.assertIn(code, [p["code"] for p in
                                 response.get_json()["problems"]], payload)
            self.assertEqual(category_row(first)["sort_order"], 0,
                             "a rejected reorder writes nothing")
            self.assertEqual(category_row(second)["sort_order"], 0)

    # ── 5. assignment semantics ────────────────────────────────────────
    def test_assign_and_unassign_write_only_the_link(self):
        cat_id = seed_category()
        item_id = seed_item()
        before = commerce_of(item_id)

        response = self.post("/api/shop-publisher/products/category",
                             {"product_id": item_id, "category_id": cat_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(item_row(item_id)["category_id"], cat_id)
        self.assertEqual(commerce_of(item_id), before,
                         "the ONLY written column is category_id")

        response = self.post("/api/shop-publisher/products/category",
                             {"product_id": item_id, "category_id": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(item_row(item_id)["category_id"])
        self.assertEqual(commerce_of(item_id), before)

    def test_assignment_requires_same_guild_and_discloses_nothing(self):
        cat_id = seed_category()
        item_id = seed_item()
        foreign_cat = seed_category("Foreign", guild=OTHER)
        foreign_item = seed_item("F", guild=OTHER)

        response = self.post("/api/shop-publisher/products/category",
                             {"product_id": 424242, "category_id": cat_id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["problems"][0]["code"],
                         "unknown_product")

        response = self.post("/api/shop-publisher/products/category",
                             {"product_id": foreign_item,
                              "category_id": cat_id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["problems"][0]["code"],
                         "unknown_product")

        for bad in (424242, foreign_cat):
            response = self.post("/api/shop-publisher/products/category",
                                 {"product_id": item_id, "category_id": bad})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json()["problems"][0]["code"],
                             "unknown_category")
            self.assertIsNone(item_row(item_id)["category_id"],
                              "a rejected assignment writes nothing")

        for payload, code in (
                ({"product_id": "x", "category_id": cat_id},
                 "invalid_product_id"),
                ({"product_id": item_id, "category_id": "x"},
                 "invalid_category_id"),
        ):
            response = self.post("/api/shop-publisher/products/category",
                                 payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["problems"][0]["code"], code)

    # ── 6. listing ─────────────────────────────────────────────────────
    def test_list_returns_counts_and_uncategorized(self):
        boosts, roles = seed_category("Boosts", sort_order=1), \
            seed_category("Roles", sort_order=0)
        seed_item("A", category_id=boosts)
        seed_item("B", category_id=boosts)
        seed_item("C", category_id=roles)
        seed_item("D")
        data = self.client.get("/api/shop-publisher/categories").get_json()
        self.assertIs(data["success"], True)
        self.assertEqual(data["uncategorized"], 1)
        by_name = {c["name"]: c for c in data["categories"]}
        self.assertEqual(by_name["Boosts"]["product_count"], 2)
        self.assertEqual(by_name["Roles"]["product_count"], 1)
        self.assertEqual([c["name"] for c in data["categories"]],
                         ["Roles", "Boosts"], "sort_order, then name")

    # ── 7. the hard boundary: never write commerce fields ──────────────
    def test_category_operations_write_only_the_link_column(self):
        cat_id = seed_category()
        item_id = seed_item()
        baseline = commerce_of(item_id)
        self.post("/api/shop-publisher/products/category",
                  {"product_id": item_id, "category_id": cat_id})
        self.post("/api/shop-publisher/categories",
                  {"id": cat_id, "name": "Renamed", "sort_order": 5})
        self.post("/api/shop-publisher/categories/reorder", {"order": [cat_id]})
        self.delete(f"/api/shop-publisher/categories/{cat_id}")
        self.assertEqual(commerce_of(item_id), baseline,
                         "every commerce field survived every operation")
        self.assertIsNone(item_row(item_id)["category_id"])

    # ── 8. security: CSRF + LEVEL_OWNER ────────────────────────────────
    def test_csrf_is_still_enforced_on_writes(self):
        cat_id = seed_category()
        item_id = seed_item()
        before = mutation_snapshot()
        self.assertEqual(self.post("/api/shop-publisher/categories",
                                   {"name": "X"}, csrf=False).status_code, 403)
        self.assertEqual(self.post("/api/shop-publisher/categories/reorder",
                                   {"order": []}, csrf=False).status_code, 403)
        self.assertEqual(self.post("/api/shop-publisher/products/category",
                                   {"product_id": item_id}, csrf=False
                                   ).status_code, 403)
        self.assertEqual(self.delete(
            f"/api/shop-publisher/categories/{cat_id}", csrf=False
        ).status_code, 403)
        self.assertEqual(mutation_snapshot(), before)

    def test_owner_level_is_required_on_all_routes(self):
        cat_id = seed_category()
        item_id = seed_item()
        payload = {"name": "X"}
        execute("UPDATE dashboard_users SET permission_level='admin'")
        self.assertEqual(self.client.get(
            "/api/shop-publisher/categories").status_code, 403)
        self.assertEqual(self.post(
            "/api/shop-publisher/categories", payload).status_code, 403)
        self.assertEqual(self.delete(
            f"/api/shop-publisher/categories/{cat_id}").status_code, 403)
        self.assertEqual(self.post(
            "/api/shop-publisher/categories/reorder",
            {"order": [cat_id]}).status_code, 403)
        self.assertEqual(self.post(
            "/api/shop-publisher/products/category",
            {"product_id": item_id}).status_code, 403)
        execute("DELETE FROM dashboard_users")
        self.assertEqual(self.post(
            "/api/shop-publisher/categories", payload).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
