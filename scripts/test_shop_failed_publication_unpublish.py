"""Failed, message-less Publications can be cleared; everything else stays protected.

Real Flask routes + real SQLite. Only the Discord REST layer is faked, and every
fake records calls so the failed-row path is proven to make none.
"""
import asyncio, os, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase1_support import GUILD, USER, execute, rows, reset_database  # noqa: E402
import dashboard.app as dash  # noqa: E402
import dashboard.api.shop_publications as api_pub  # noqa: E402
import utils.shop_publication as pub  # noqa: E402

H = {"X-CSRF-Token": "t"}


class FakeRest(pub.DiscordREST):
    """create_message behaviour is chosen per test; all other calls are recorded."""
    send = "ok"          # "ok" | "reject" (definitive) | "uncertain"
    calls = []

    def __init__(self): pass

    def validate_channel_and_permissions(self, *a, **k):
        FakeRest.calls.append("validate"); return True

    def create_message(self, *a, **k):
        FakeRest.calls.append("create")
        if FakeRest.send == "reject":
            raise pub.DiscordFailure("Missing Permissions", status=403, discord_code=50013)
        if FakeRest.send == "uncertain":
            raise pub.DiscordFailure("timeout", uncertain=True)
        return 4242

    def fetch_message(self, *a, **k): FakeRest.calls.append("fetch"); return {"id": "4242"}
    def delete_message(self, *a, **k): FakeRest.calls.append("delete"); return True


class FailedPublicationUnpublish(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        api_pub.DiscordREST = FakeRest
        FakeRest.send, FakeRest.calls = "ok", []
        self.c = dash.app.test_client()
        with self.c.session_transaction() as s:
            s.update({"user": {"id": USER, "username": "t", "avatar": None}, "guild_id": GUILD,
                      "csrf_token": "t", "expires_at": time.time() + 7200})

    # -- helpers
    def design(self, name="d"):
        pid = execute(
            "INSERT INTO shop_items (guild_id,name,type,price,description,role_id,max_stock,current_stock,"
            "required_level,rarity,enabled,featured) VALUES (?,?,?,?,?,?,?,?,?,?,1,0)",
            (GUILD, "VIP " + name, "role", 1500, "d", 99, 20, 12, 5, "epic"))
        body = {"presentation": {"mode": "per_product", "content": "Buy {{product.name}}!",
                                 "embeds": [{"title": "{{product.name}}"}]},
                "products": [pid], "action": {"kind": "buttons", "entries": [{"product_id": pid}]}}
        r = self.c.post("/api/shop-publisher/designs", headers=H,
                        json={"name": name, "source_template_name": "banner", "design": body})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["design"]["id"]

    def publish(self, did):
        r = self.c.post("/api/shop-publisher/publications/publish", headers=H,
                        json={"design_id": did, "channel_id": 777001})
        if r.get_json().get("code") == "ack_required" or r.get_json().get("requires_warning_ack"):
            tok = r.get_json().get("warning_token")
            r = self.c.post("/api/shop-publisher/publications/publish", headers=H,
                            json={"design_id": did, "channel_id": 777001, "warning_token": tok})
        return r

    def pubs(self, did):
        return rows("SELECT id,status,message_id FROM shop_publications WHERE design_id=?", (did,))

    def design_exists(self, did):
        return bool(rows("SELECT id FROM shop_designs WHERE id=?", (did,)))

    def delete_design(self, did):
        return self.c.delete(f"/api/shop-publisher/designs/{did}", headers=H)

    def unpublish(self, pid):
        return self.c.delete(f"/api/shop-publisher/publications/{pid}", headers=H)

    # -- 1. published rows keep blocking deletion
    def test_published_design_deletion_stays_blocked(self):
        did = self.design()
        self.assertEqual(self.publish(did).status_code, 201)
        r = self.delete_design(did)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "design_has_publications")
        self.assertEqual(r.get_json()["error"], "This Design has Publications. Unpublish them first.")
        self.assertTrue(self.design_exists(did))
        self.assertEqual(len(self.pubs(did)), 1)

    def test_published_row_still_unpublishes_via_discord_then_design_deletes(self):
        did = self.design()
        self.assertEqual(self.publish(did).status_code, 201)
        pid = self.pubs(did)[0][0]
        FakeRest.calls = []
        r = self.unpublish(pid)
        self.assertEqual((r.status_code, r.get_json()["success"]), (200, True))
        self.assertIn("delete", FakeRest.calls)           # normal path still deletes the message
        self.assertEqual(self.delete_design(did).status_code, 200)

    # -- 2. failed + message-less rows can be cleared, then the design deleted
    def test_failed_messageless_row_can_be_cleared_then_design_deleted(self):
        did = self.design()
        FakeRest.send = "reject"
        r = self.publish(did)
        self.assertEqual(r.status_code, 502)
        (pid, status, message_id), = self.pubs(did)
        self.assertEqual((status, message_id), ("failed", None))
        self.assertEqual(self.delete_design(did).status_code, 409)       # still guarded while the row exists
        FakeRest.calls = []
        r = self.unpublish(pid)
        self.assertEqual((r.status_code, r.get_json()["success"]), (200, True), r.get_json())
        self.assertEqual(FakeRest.calls, [], "clearing a never-sent row must make no Discord calls")
        self.assertEqual(self.pubs(did), [])
        r = self.delete_design(did)
        self.assertEqual((r.status_code, r.get_json()["success"]), (200, True))
        self.assertFalse(self.design_exists(did))

    def test_clearing_only_removes_the_targeted_failed_row(self):
        a, b = self.design("a"), self.design("b")
        FakeRest.send = "reject"
        self.publish(a); self.publish(b)
        pid_a = self.pubs(a)[0][0]
        self.assertEqual(self.unpublish(pid_a).status_code, 200)
        self.assertEqual(self.pubs(a), [])
        self.assertEqual(len(self.pubs(b)), 1)                            # sibling design's row untouched
        self.assertEqual(self.delete_design(b).status_code, 409)

    def test_unpublish_of_an_already_cleared_row_is_404(self):
        did = self.design()
        FakeRest.send = "reject"; self.publish(did)
        pid = self.pubs(did)[0][0]
        self.assertEqual(self.unpublish(pid).status_code, 200)
        self.assertEqual(self.unpublish(pid).status_code, 404)

    # -- 3. pending (uncertain send) rows stay protected
    def test_pending_row_stays_protected(self):
        did = self.design()
        FakeRest.send = "uncertain"
        self.assertEqual(self.publish(did).status_code, 202)
        (pid, status, message_id), = self.pubs(did)
        self.assertEqual((status, message_id), ("pending", None))
        FakeRest.calls = []
        r = self.unpublish(pid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "publication_attention")
        self.assertEqual(FakeRest.calls, [])
        self.assertEqual(len(self.pubs(did)), 1, "pending row must be retained")
        self.assertEqual(self.delete_design(did).status_code, 409)
        self.assertTrue(self.design_exists(did))

    def test_failed_row_that_has_a_message_id_is_not_cleared_by_the_new_path(self):
        did = self.design()
        pid = execute("INSERT INTO shop_publications (guild_id,design_id,channel_id,message_id,status) "
                      "VALUES (?,?,?,?,?)", (GUILD, did, 777001, 555001, "failed"))
        r = self.unpublish(pid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.pubs(did)), 1)

    def test_remove_publication_null_match_never_matches_a_row_with_a_message(self):
        did = self.design()
        pid = execute("INSERT INTO shop_publications (guild_id,design_id,channel_id,message_id,status) "
                      "VALUES (?,?,?,?,?)", (GUILD, did, 777001, 555001, "failed"))
        self.assertFalse(pub.remove_publication(GUILD, pid, expected_statuses=("failed",),
                                                expected_message_id=None, match_message=True))
        self.assertEqual(len(self.pubs(did)), 1)
        pid2 = execute("INSERT INTO shop_publications (guild_id,design_id,channel_id,status) "
                       "VALUES (?,?,?,?)", (GUILD, did, 777002, "pending"))
        self.assertFalse(pub.remove_publication(GUILD, pid2, expected_statuses=("failed",),
                                                expected_message_id=None, match_message=True))
        self.assertEqual(len(self.pubs(did)), 2, "status guard must keep pending rows")

    # -- 4. normal deletion still works
    def test_design_without_publications_still_deletes(self):
        did = self.design()
        r = self.delete_design(did)
        self.assertEqual((r.status_code, r.get_json()["success"]), (200, True))
        self.assertFalse(self.design_exists(did))
        self.assertEqual(self.delete_design(did).status_code, 404)


if __name__ == "__main__":
    unittest.main()
