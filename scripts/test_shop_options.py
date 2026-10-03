#!/usr/bin/env python3
"""Focused Shop Designer Step 1 one-level option relationship tests."""
import asyncio
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

from phase1_support import DB_PATH, GUILD, USER, execute, rows, reset_database
from database import init_db
import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
OTHER_GUILD = 9102


def seed_item(name, guild=GUILD, option_of_id=None, **over):
    return execute(
        "INSERT INTO shop_items (guild_id, name, description, price, type, "
        "enabled, option_of_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild, name, "test commerce row", 100, "custom", 1, option_of_id))


class ShopOptionTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Options", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="options-csrf")

    def post(self, path, payload):
        return self.client.post(path, json=payload,
                                headers={"X-CSRF-Token": "options-csrf"})

    def test_existing_database_migration_and_idempotence(self):
        item = seed_item("Existing")
        before = rows("SELECT * FROM shop_items WHERE id=?", (item,))[0]
        # Recreate the pre-Step-1 shape while retaining every existing value.
        with sqlite3.connect(DB_PATH) as db:
            db.execute("DROP TRIGGER IF EXISTS shop_item_option_insert_guard")
            db.execute("DROP TRIGGER IF EXISTS shop_item_option_update_guard")
            db.execute("DROP TRIGGER IF EXISTS shop_item_option_delete_guard")
            db.execute("DROP INDEX IF EXISTS idx_si_guild_option")
            db.execute("ALTER TABLE shop_items DROP COLUMN option_of_id")
        asyncio.run(init_db())
        cols = [r[1] for r in rows("PRAGMA table_info(shop_items)")]
        after_first = rows("SELECT * FROM shop_items WHERE id=?", (item,))[0]
        self.assertIn("option_of_id", cols)
        self.assertIsNone(rows("SELECT option_of_id FROM shop_items WHERE id=?", (item,))[0][0])
        self.assertEqual(after_first[:-1], before[:-1])
        asyncio.run(init_db())
        cols_again = [r[1] for r in rows("PRAGMA table_info(shop_items)")]
        self.assertEqual(cols_again.count("option_of_id"), 1)
        self.assertEqual(rows("SELECT * FROM shop_items WHERE id=?", (item,))[0], after_first)
        self.assertIn("idx_si_guild_option", [r[0] for r in rows(
            "SELECT name FROM sqlite_master WHERE type='index'")])

    def test_integrity_triggers_accept_only_direct_same_guild_root_children(self):
        root = seed_item("Potion")
        child = seed_item("7 Days", option_of_id=root)
        other_root = seed_item("Other guild", guild=OTHER_GUILD)
        self.assertEqual(rows("SELECT option_of_id FROM shop_items WHERE id=?", (child,)), [(root,)])

        with self.assertRaises(sqlite3.IntegrityError):
            seed_item("Missing", option_of_id=987654)
        with self.assertRaises(sqlite3.IntegrityError):
            seed_item("Cross guild", guild=GUILD, option_of_id=other_root)
        with self.assertRaises(sqlite3.IntegrityError):
            execute("UPDATE shop_items SET option_of_id=id WHERE id=?", (other_root,))
        with self.assertRaises(sqlite3.IntegrityError):
            execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (child, root))
        with self.assertRaises(sqlite3.IntegrityError):
            execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (child, child))
        with self.assertRaises(sqlite3.IntegrityError):
            execute("UPDATE shop_items SET id=? WHERE id=?", (root + 1000000, root))
        sibling = seed_item("30 Days", option_of_id=root)
        with self.assertRaises(sqlite3.IntegrityError):
            execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (child, sibling))
        with self.assertRaises(sqlite3.IntegrityError):
            execute("DELETE FROM shop_items WHERE id=?", (root,))
        self.assertEqual(rows("SELECT option_of_id FROM shop_items WHERE id=?", (child,)), [(root,)])

    def test_management_write_is_session_guild_scoped_and_validates_parent(self):
        root = seed_item("Potion")
        foreign_root = seed_item("Foreign", guild=OTHER_GUILD)
        payload = {"name": "30 Days", "type": "custom", "price": 300,
                   "guild_id": OTHER_GUILD, "option_of_id": root}
        response = self.post("/api/shop/item", payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        option = rows("SELECT guild_id,option_of_id FROM shop_items WHERE name='30 Days'")[0]
        self.assertEqual(option, (GUILD, root), "request guild_id is ignored")

        option_id = rows("SELECT id FROM shop_items WHERE name='30 Days'")[0][0]
        for parent in (999999, foreign_root, option_id):
            bad = dict(payload, name=f"Bad {parent}", option_of_id=parent)
            self.assertEqual(self.post("/api/shop/item", bad).status_code, 400)
        self.assertEqual(rows("SELECT COUNT(*) FROM shop_items WHERE name LIKE 'Bad %'")[0][0], 0)
        refused = self.client.delete(
            f"/api/shop/item/{root}",
            headers={"X-CSRF-Token": "options-csrf"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(rows("SELECT COUNT(*) FROM shop_items WHERE id=?", (root,))[0][0], 1)

    def test_family_read_is_guild_scoped_and_returns_only_direct_children(self):
        root = seed_item("Health Potion")
        direct = seed_item("7 Days", option_of_id=root)
        seed_item("Other root")
        seed_item("Foreign option", guild=OTHER_GUILD, option_of_id=seed_item(
            "Foreign root", guild=OTHER_GUILD))
        response = self.client.get(f"/api/shop-publisher/products/{root}/options")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["root"]["id"], root)
        self.assertIsNone(body["root"]["option_of_id"])
        self.assertEqual([row["id"] for row in body["options"]], [direct])
        self.assertEqual(body["options"][0]["option_of_id"], root)
        self.assertEqual(self.client.get(
            f"/api/shop-publisher/products/{direct}/options").status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/shop-publisher/products/{999999}/options").status_code, 404)

    def test_shop_query_and_root_only_preview_contract_remain_compatible(self):
        root = seed_item("Sword")
        option = seed_item("Sword option", option_of_id=root)
        disabled = seed_item("Disabled root")
        execute("UPDATE shop_items SET enabled=0 WHERE id=?", (disabled,))
        source = Path(__file__).resolve().parents[1] / "cogs/shop.py"
        self.assertIn("AND option_of_id IS NULL", source.read_text())
        # Exercise the actual command callback, including both the listing
        # fields and its direct-buy button view.
        from cogs.shop import Shop
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=GUILD, name="Test Guild"),
            user=SimpleNamespace(id=USER, roles=[]), response=response)
        asyncio.run(Shop.shop.callback(None, interaction))
        kwargs = response.send_message.await_args.kwargs
        listed_names = [field.name for field in kwargs["embed"].fields]
        button_ids = [item.custom_id for item in kwargs["view"].children]
        self.assertTrue(any("Sword" in name for name in listed_names))
        self.assertFalse(any("Sword option" in name for name in listed_names))
        self.assertFalse(any("Disabled root" in name for name in listed_names))
        self.assertEqual(button_ids, [f"shop_buy_{root}"])
        listed = rows("SELECT id FROM shop_items WHERE guild_id=? AND enabled=1 "
                      "AND option_of_id IS NULL ORDER BY featured DESC, price ASC", (GUILD,))
        self.assertIn((root,), listed)
        self.assertNotIn((option,), listed)
        self.assertNotIn((disabled,), listed)
        # The existing catalog payload remains unchanged; root-only drafts
        # continue to resolve against the same root commerce row.
        from utils.shop_publisher import validate_design, preview_design
        row = rows("SELECT id,name,type,price,price_diamonds,enabled,current_stock,"
                   "max_stock,prestige_tier,featured,description,duration_hours,"
                   "required_level,rarity,icon_url,option_of_id FROM shop_items WHERE id=?",
                   (root,))[0]
        keys = ("id", "name", "type", "price", "price_diamonds", "enabled",
                "current_stock", "max_stock", "prestige_tier", "featured",
                "description", "duration_hours", "required_level", "rarity",
                "icon_url", "option_of_id")
        product = dict(zip(keys, row))
        draft = {"presentation": {"mode": "per_product", "content": "{{product.name}}",
                                  "embeds": []},
                 "products": [root], "action": {"kind": "buttons",
                 "entries": [{"product_id": root}]}}
        self.assertEqual(validate_design(draft, {root: product}), [])
        preview = preview_design(draft, {root: product}, {
            "coins": {"name": "Coins", "emoji": "🪙"},
            "diamonds": {"name": "Diamonds", "emoji": "💎"}})
        self.assertEqual(preview["content"], "Sword")
        self.assertEqual(preview["action"]["entries"][0]["custom_id"], f"shop_buy_{root}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
