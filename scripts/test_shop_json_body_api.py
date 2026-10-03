"""Focused non-object JSON-body validation for Shop Publisher APIs."""
import asyncio
import time
import unittest

from phase1_support import GUILD, USER, execute, reset_database
import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)


class ShopJsonBodyApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "JSON test", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="json-test-csrf")
        self.headers = {"X-CSRF-Token": "json-test-csrf"}

    def test_publication_and_preview_non_object_bodies_are_controlled_400(self):
        execute("INSERT INTO shop_designs (guild_id,name,design_json) VALUES (?,?,?)",
                (GUILD, "Test", '{}'))
        publication_id = execute(
            "INSERT INTO shop_publications (guild_id,design_id,channel_id,status) "
            "VALUES (?,?,?,'published')", (GUILD, 1, 2))
        paths = (
            "/api/shop-publisher/publications/publish",
            f"/api/shop-publisher/publications/{publication_id}/update",
            "/api/shop-publisher/preview",
        )
        for body in ([], "text", 7, None):
            for path in paths:
                response = self.client.post(path, json=body, headers=self.headers)
                self.assertEqual(response.status_code, 400, (path, body))
                self.assertIsInstance(response.get_json(), dict)
                self.assertFalse(response.get_json()["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
