#!/usr/bin/env python3
"""Real Flask owner/CSRF tests for explicit Shop emoji operation routes."""

import asyncio
import time
import unittest
from unittest.mock import patch

from phase1_support import GUILD, USER, execute, reset_database
import dashboard.app as dashboard
from dashboard.api import shop_emoji as routes
from utils.shop_emoji import DiscordAPIError


class ShopEmojiApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Owner", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="emoji-csrf")
        self.headers = {"X-CSRF-Token": "emoji-csrf"}

    def post(self, path, body, csrf=True):
        return self.client.post(path, json=body,
                                headers=self.headers if csrf else {})

    def test_prepare_uses_session_guild_ids_and_explicit_flags(self):
        captured = {}

        async def fake(guild_id, product_ids, **kwargs):
            captured.update(guild_id=guild_id, product_ids=product_ids, **kwargs)
            return {"success": True, "results": []}

        with patch.object(routes, "prepare_product_emojis", fake):
            response = self.post("/api/shop-publisher/emojis/prepare", {
                "guild_id": GUILD + 999, "product_ids": [1, 2],
                "retry_uncertain": True, "acknowledge_duplicate_risk": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, {
            "guild_id": GUILD, "product_ids": [1, 2],
            "retry_uncertain": True, "acknowledge_duplicate_risk": True,
        })

    def test_non_boolean_retry_flags_do_not_bypass_acknowledgement(self):
        captured = {}

        async def fake(guild_id, product_ids, **kwargs):
            captured.update(kwargs)
            return {"success": True, "results": []}

        with patch.object(routes, "prepare_product_emojis", fake):
            self.post("/api/shop-publisher/emojis/prepare", {
                "product_ids": [1], "retry_uncertain": "true",
                "acknowledge_duplicate_risk": 1,
            })
        self.assertEqual(captured, {"retry_uncertain": False,
                                    "acknowledge_duplicate_risk": False})

    def test_url_targets_are_rejected_before_backend_invocation(self):
        async def fake(*args, **kwargs):
            raise AssertionError("backend must not receive browser URL targets")

        for path in ("/api/shop-publisher/emojis/prepare",
                     "/api/shop-publisher/emojis/reconcile"):
            with self.subTest(path=path), patch.object(routes, "prepare_product_emojis", fake), \
                    patch.object(routes, "reconcile_product_emojis", fake):
                response = self.post(path, {"product_ids": [1], "icon_url": "https://127.0.0.1/x"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["code"], "product_ids_only")

    def test_reconcile_is_a_separate_read_only_operation(self):
        captured = {}

        async def fake(guild_id, product_ids):
            captured.update(guild_id=guild_id, product_ids=product_ids)
            return {"success": True, "results": [], "uncertain": False}

        with patch.object(routes, "reconcile_product_emojis", fake):
            response = self.post("/api/shop-publisher/emojis/reconcile", {"product_ids": [3]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, {"guild_id": GUILD, "product_ids": [3]})

    def test_duplicate_delete_requires_exact_id_confirmation(self):
        async def fake(*args, **kwargs):
            raise AssertionError("unconfirmed delete must not run")

        body = {"content_hash": "a" * 64, "emoji_id": "5002", "confirmation": "DELETE 5001"}
        with patch.object(routes, "delete_confirmed_duplicate", fake):
            response = self.post("/api/shop-publisher/emojis/delete-duplicate", body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "delete_confirmation_required")

    def test_confirmed_duplicate_delete_uses_session_guild(self):
        captured = {}

        async def fake(guild_id, content_hash, emoji_id, *, confirm):
            captured.update(guild_id=guild_id, content_hash=content_hash,
                            emoji_id=emoji_id, confirm=confirm)
            return {"success": True, "deleted_emoji_id": emoji_id}

        with patch.object(routes, "delete_confirmed_duplicate", fake):
            response = self.post("/api/shop-publisher/emojis/delete-duplicate", {
                "guild_id": GUILD + 999, "content_hash": "a" * 64,
                "emoji_id": "5002", "confirmation": "DELETE 5002",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, {"guild_id": GUILD, "content_hash": "a" * 64,
                                    "emoji_id": "5002", "confirm": True})

    def test_definitive_discord_error_status_is_not_collapsed_to_502(self):
        async def fake(*args, **kwargs):
            raise DiscordAPIError("forbidden", status=403, http_status=403, uncertain=False)

        with patch.object(routes, "delete_confirmed_duplicate", fake):
            response = self.post("/api/shop-publisher/emojis/delete-duplicate", {
                "content_hash": "a" * 64, "emoji_id": "5002",
                "confirmation": "DELETE 5002",
            })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "discord_api_error")

    def test_owner_permission_and_csrf_apply_to_every_mutation(self):
        execute("UPDATE dashboard_users SET permission_level='admin'")
        for path, body in (
            ("/api/shop-publisher/emojis/prepare", {"product_ids": [1]}),
            ("/api/shop-publisher/emojis/reconcile", {"product_ids": [1]}),
            ("/api/shop-publisher/emojis/delete-duplicate", {
                "content_hash": "a" * 64, "emoji_id": "5002", "confirmation": "DELETE 5002"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, body).status_code, 403)
        execute("UPDATE dashboard_users SET permission_level='owner'")
        self.assertEqual(self.post("/api/shop-publisher/emojis/prepare",
                                   {"product_ids": [1]}, csrf=False).status_code, 403)


if __name__ == "__main__":
    unittest.main()
