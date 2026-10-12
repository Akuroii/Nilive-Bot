"""Deleted channels/messages, event reconciliation, stale-operation recovery, re-check and the
record-only escape hatch. Real Flask routes + real SQLite; only the Discord layer is faked.
"""
import asyncio, os, sys, time, types, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase1_support import GUILD, USER, execute, rows, reset_database  # noqa: E402
import dashboard.app as dash  # noqa: E402
import dashboard.api.shop_publications as api_pub  # noqa: E402
import utils.shop_publication as pub  # noqa: E402
from cogs.shop_publication_sync import ShopPublicationSync  # noqa: E402

H = {"X-CSRF-Token": "t"}
CH = 777001


class FakeRest(pub.DiscordREST):
    channel = "ok"        # ok | deleted | forbidden | transient   (validate_channel_and_permissions)
    message = "present"   # present | missing | transient          (fetch_message)
    chan_get = "404"      # 404 | 403 | transport                  (GET /channels/<id>)
    calls = []

    def __init__(self): pass

    def validate_channel_and_permissions(self, *a, **k):
        FakeRest.calls.append("validate")
        if FakeRest.channel == "deleted":
            raise pub.PublicationError("The selected channel is not available in this server.", code="unknown_channel", status=404)
        if FakeRest.channel == "forbidden":
            raise pub.PublicationError("The bot lacks required channel permissions: View Channel.", code="bot_missing_permissions", status=403)
        if FakeRest.channel == "transient":
            raise pub.PublicationError("Discord channel verification failed.", code="channel_verification_failed", status=502)
        return True

    def request(self, method, path, **k):
        FakeRest.calls.append("GET " + path)
        if FakeRest.chan_get == "404":
            raise pub.DiscordFailure("Unknown Channel", status=404, discord_code=10003)
        if FakeRest.chan_get == "403":
            raise pub.DiscordFailure("Missing Access", status=403, discord_code=50001)
        raise pub.DiscordFailure("transport error", uncertain=False)

    def fetch_message(self, *a, **k):
        FakeRest.calls.append("fetch")
        if FakeRest.message == "missing":
            raise pub.DiscordFailure("Unknown Message", status=404, discord_code=10008)
        if FakeRest.message == "transient":
            raise pub.DiscordFailure("server error", status=500)
        return {"id": "1"}

    def delete_message(self, *a, **k):
        FakeRest.calls.append("delete"); return True


class Base(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) VALUES (?,?,'owner',1)", (GUILD, USER))
        api_pub.DiscordREST = FakeRest
        FakeRest.channel, FakeRest.message, FakeRest.chan_get, FakeRest.calls = "ok", "present", "404", []
        self.c = dash.app.test_client()
        with self.c.session_transaction() as s:
            s.update({"user": {"id": USER, "username": "t", "avatar": None}, "guild_id": GUILD,
                      "csrf_token": "t", "expires_at": time.time() + 7200})
        pid = execute("INSERT INTO shop_items (guild_id,name,type,price,description,role_id,max_stock,current_stock,required_level,rarity,enabled,featured) VALUES (?,?,?,?,?,?,?,?,?,?,1,0)", (GUILD, "VIP", "role", 1500, "d", 99, 20, 12, 5, "epic"))
        body = {"presentation": {"mode": "per_product", "content": "Buy {{product.name}}!", "embeds": [{"title": "{{product.name}}"}]},
                "products": [pid], "action": {"kind": "buttons", "entries": [{"product_id": pid}]}}
        r = self.c.post("/api/shop-publisher/designs", headers=H, json={"name": "d", "source_template_name": "banner", "design": body})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.did = r.get_json()["design"]["id"]

    def row(self, status, message_id=None, channel=CH, guild=GUILD, design=None):
        return execute("INSERT INTO shop_publications (guild_id,design_id,channel_id,message_id,status) VALUES (?,?,?,?,?)",
                       (guild, design or self.did, channel, message_id, status))

    def status(self, pid):
        r = rows("SELECT status FROM shop_publications WHERE id=?", (pid,))
        return r[0][0] if r else None

    def delete_design(self):
        return self.c.delete(f"/api/shop-publisher/designs/{self.did}", headers=H)

    def unpublish(self, pid):
        return self.c.delete(f"/api/shop-publisher/publications/{pid}", headers=H)


class UnpublishDeletedChannel(Base):
    def test_confirmed_deleted_channel_clears_row_then_design_deletes(self):
        pid = self.row("published", 555)
        FakeRest.channel = "deleted"
        r = self.unpublish(pid)
        self.assertEqual((r.status_code, r.get_json()["success"]), (200, True), r.get_json())
        self.assertTrue(r.get_json()["channel_deleted"] and r.get_json()["already_missing"])
        self.assertNotIn("delete", FakeRest.calls)               # no message delete on a dead channel
        self.assertIsNone(self.status(pid))
        self.assertEqual(self.delete_design().status_code, 200)

    def test_attention_row_with_deleted_channel_also_clears(self):
        pid = self.row("attention", 555); FakeRest.channel = "deleted"
        self.assertEqual(self.unpublish(pid).status_code, 200)
        self.assertIsNone(self.status(pid))

    def test_unknown_channel_without_discord_confirmation_stays_protected_403(self):
        pid = self.row("published", 555); FakeRest.channel = "deleted"; FakeRest.chan_get = "403"
        r = self.unpublish(pid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.status(pid), "attention")
        self.assertEqual(self.delete_design().status_code, 409)

    def test_unknown_channel_with_transport_failure_stays_protected(self):
        pid = self.row("published", 555); FakeRest.channel = "deleted"; FakeRest.chan_get = "transport"
        self.assertEqual(self.unpublish(pid).status_code, 409)
        self.assertEqual(self.status(pid), "attention")

    def test_permission_error_is_never_treated_as_deleted(self):
        pid = self.row("published", 555); FakeRest.channel = "forbidden"
        self.assertEqual(self.unpublish(pid).status_code, 409)
        self.assertEqual(self.status(pid), "attention")
        self.assertNotIn("GET /channels/%d" % CH, FakeRest.calls)   # not even asked: only unknown_channel is verified

    def test_transient_verification_error_is_never_treated_as_deleted(self):
        pid = self.row("published", 555); FakeRest.channel = "transient"
        self.assertEqual(self.unpublish(pid).status_code, 409)
        self.assertEqual(self.status(pid), "attention")


class EventReconcilers(Base):
    def test_message_delete_removes_only_the_exact_stable_row(self):
        hit = self.row("published", 111)
        attn = self.row("attention", 112)
        other_msg = self.row("published", 113)
        other_chan = self.row("published", 111, channel=CH + 1)
        other_guild = self.row("published", 111, guild=GUILD + 1)
        inflight = self.row("unpublishing", 111)
        pending = self.row("pending", None)
        n = pub.reconcile_message_deleted(GUILD, CH, [111, 112])
        self.assertEqual(n, 2, "exactly the published + attention rows for messages 111/112 in this channel")
        self.assertIsNone(self.status(hit)); self.assertIsNone(self.status(attn))
        for keep in (other_msg, other_chan, other_guild, inflight, pending):
            self.assertIsNotNone(self.status(keep), "unrelated / in-flight / pending rows must be untouched")

    def test_message_delete_is_idempotent_and_ignores_unknown_ids(self):
        pid = self.row("published", 111)
        self.assertEqual(pub.reconcile_message_deleted(GUILD, CH, [111]), 1)
        self.assertEqual(pub.reconcile_message_deleted(GUILD, CH, [111]), 0)
        self.assertEqual(pub.reconcile_message_deleted(GUILD, CH, [999999]), 0)
        self.assertEqual(pub.reconcile_message_deleted(GUILD, CH, []), 0)

    def test_bulk_delete_removes_all_listed_and_only_those(self):
        a, b, c = self.row("published", 1), self.row("published", 2), self.row("published", 3)
        self.assertEqual(pub.reconcile_message_deleted(GUILD, CH, {1, 2}), 2)
        self.assertIsNone(self.status(a)); self.assertIsNone(self.status(b)); self.assertEqual(self.status(c), "published")

    def test_channel_delete_removes_every_non_inflight_row_of_that_channel_only(self):
        gone = [self.row(s, 50 + i) for i, s in enumerate(("published", "attention", "replacement_send_uncertain", "replacement_retry_authorized"))]
        gone += [self.row("pending", None), self.row("failed", None)]
        keep_inflight = [self.row(s, 90) for s in ("updating", "unpublishing", "replacement_sending")]
        keep_other = [self.row("published", 60, channel=CH + 1), self.row("published", 61, guild=GUILD + 1)]
        self.assertEqual(pub.reconcile_channel_deleted(GUILD, CH), len(gone))
        for pid in gone: self.assertIsNone(self.status(pid))
        for pid in keep_inflight + keep_other: self.assertIsNotNone(self.status(pid))
        self.assertEqual(pub.reconcile_channel_deleted(GUILD, CH), 0)       # idempotent

    def test_deleting_a_channel_unblocks_the_design(self):
        self.row("published", 1); self.row("pending", None)
        self.assertEqual(self.delete_design().status_code, 409)
        pub.reconcile_channel_deleted(GUILD, CH)
        self.assertEqual(self.delete_design().status_code, 200)


class CogHandlers(Base):
    def run_cog(self, coro_fn, *a):
        cog = ShopPublicationSync(types.SimpleNamespace())
        return asyncio.run(coro_fn(cog, *a))

    def test_raw_message_delete_handler(self):
        pid = self.row("published", 111)
        payload = types.SimpleNamespace(guild_id=GUILD, channel_id=CH, message_id=111)
        self.run_cog(ShopPublicationSync.on_raw_message_delete, payload)
        self.assertIsNone(self.status(pid))
        self.run_cog(ShopPublicationSync.on_raw_message_delete, payload)    # duplicate event: no error

    def test_dm_and_guildless_events_are_ignored(self):
        pid = self.row("published", 111)
        self.run_cog(ShopPublicationSync.on_raw_message_delete, types.SimpleNamespace(guild_id=None, channel_id=CH, message_id=111))
        self.run_cog(ShopPublicationSync.on_raw_bulk_message_delete, types.SimpleNamespace(guild_id=None, channel_id=CH, message_ids={111}))
        self.run_cog(ShopPublicationSync.on_guild_channel_delete, types.SimpleNamespace(guild=None, id=CH))
        self.assertEqual(self.status(pid), "published")

    def test_bulk_and_channel_handlers(self):
        a, b = self.row("published", 1), self.row("published", 2)
        self.run_cog(ShopPublicationSync.on_raw_bulk_message_delete, types.SimpleNamespace(guild_id=GUILD, channel_id=CH, message_ids={1}))
        self.assertIsNone(self.status(a)); self.assertEqual(self.status(b), "published")
        self.run_cog(ShopPublicationSync.on_guild_channel_delete, types.SimpleNamespace(guild=types.SimpleNamespace(id=GUILD), id=CH))
        self.assertIsNone(self.status(b))

    def test_handler_failure_never_raises(self):
        orig = pub.reconcile_message_deleted
        import cogs.shop_publication_sync as mod
        def boom(*a, **k): raise RuntimeError("db locked")
        mod.reconcile_message_deleted = boom
        try:
            self.run_cog(ShopPublicationSync.on_raw_message_delete, types.SimpleNamespace(guild_id=GUILD, channel_id=CH, message_id=1))
        finally:
            mod.reconcile_message_deleted = orig

    def test_startup_recovery_runs_once(self):
        old = self.row("updating", 5)
        execute("UPDATE shop_publications SET updated_at=datetime('now','-1 hour') WHERE id=?", (old,))
        cog = ShopPublicationSync(types.SimpleNamespace())
        asyncio.run(cog.on_ready())
        self.assertEqual(self.status(old), "attention")
        execute("UPDATE shop_publications SET status='updating', updated_at=datetime('now','-1 hour') WHERE id=?", (old,))
        asyncio.run(cog.on_ready())                      # second ready event: guarded
        self.assertEqual(self.status(old), "updating")


class StaleRecovery(Base):
    def age(self, pid, seconds=3600):
        execute("UPDATE shop_publications SET updated_at=datetime('now', ?) WHERE id=?", (f"-{seconds} seconds", pid))

    def test_only_stale_inflight_rows_move_and_into_recoverable_states(self):
        u, un, rs = self.row("updating", 1), self.row("unpublishing", 2), self.row("replacement_sending", 3)
        fresh = self.row("updating", 4)
        published, pending = self.row("published", 5), self.row("pending", None)
        for pid in (u, un, rs, published, pending): self.age(pid)
        self.assertEqual(pub.recover_stale_operations(900), 3)
        self.assertEqual((self.status(u), self.status(un)), ("attention", "attention"))
        self.assertEqual(self.status(rs), "replacement_send_uncertain")
        self.assertEqual(self.status(fresh), "updating")                  # not stale
        self.assertEqual((self.status(published), self.status(pending)), ("published", "pending"))
        self.assertEqual(pub.recover_stale_operations(900), 0)           # idempotent

    def test_recovered_rows_can_be_unpublished(self):
        pid = self.row("unpublishing", 555); self.age(pid)
        pub.recover_stale_operations(900)
        r = self.unpublish(pid)                                           # message present -> real unpublish path
        self.assertEqual(r.status_code, 200, r.get_json())


class Recheck(Base):
    def recheck(self):
        r = self.c.post(f"/api/shop-publisher/designs/{self.did}/publications/recheck", headers=H)
        return r.status_code, r.get_json()

    def test_confirmed_missing_message_is_cleared(self):
        pid = self.row("published", 555); FakeRest.message = "missing"
        code, b = self.recheck()
        self.assertEqual((code, b["removed"]), (200, 1)); self.assertIsNone(self.status(pid))

    def test_confirmed_deleted_channel_is_cleared(self):
        pid = self.row("attention", 555); FakeRest.channel = "deleted"
        code, b = self.recheck()
        self.assertEqual(b["removed"], 1); self.assertIsNone(self.status(pid))

    def test_present_message_is_kept(self):
        pid = self.row("published", 555)
        code, b = self.recheck()
        self.assertEqual((b["removed"], b["still_present"]), (0, 1)); self.assertEqual(self.status(pid), "published")

    def test_ambiguous_results_never_remove(self):
        a, b_, c_ = self.row("published", 1), self.row("published", 2), self.row("published", 3)
        for setup in (("forbidden", "present", "404"), ("transient", "present", "404"), ("ok", "transient", "404"), ("deleted", "present", "403")):
            FakeRest.channel, FakeRest.message, FakeRest.chan_get = setup
            code, body = self.recheck()
            self.assertEqual(body["removed"], 0, setup)
            self.assertEqual(body["unverified"], 3, setup)
        for pid in (a, b_, c_): self.assertEqual(self.status(pid), "published")

    def test_non_stable_and_message_less_rows_are_skipped(self):
        ids = [self.row("pending", None), self.row("failed", None), self.row("updating", 9), self.row("replacement_send_uncertain", 9)]
        FakeRest.message = "missing"
        code, b = self.recheck()
        self.assertEqual((b["removed"], b["skipped"]), (0, 4))
        for pid in ids: self.assertIsNotNone(self.status(pid))

    def test_unknown_design_is_404(self):
        r = self.c.post("/api/shop-publisher/designs/99999/publications/recheck", headers=H)
        self.assertEqual(r.status_code, 404)

    def test_recheck_only_touches_its_own_design(self):
        mine = self.row("published", 1)
        theirs = self.row("published", 2, design=self.did + 500)
        FakeRest.message = "missing"
        self.recheck()
        self.assertIsNone(self.status(mine)); self.assertEqual(self.status(theirs), "published")


class RecordOnlyRemoval(Base):
    def forget(self, pid, body=None):
        r = self.c.post(f"/api/shop-publisher/publications/{pid}/forget", headers=H, json=body if body is not None else {"confirm": True})
        return r.status_code, r.get_json()

    def test_requires_explicit_confirmation(self):
        pid = self.row("pending", None)
        for bad in ({}, {"confirm": "yes"}, {"confirm": 1}, {"confirm": False}):
            code, b = self.forget(pid, bad)
            self.assertEqual((code, b["code"]), (400, "confirmation_required"), bad)
        self.assertEqual(self.status(pid), "pending")

    def test_removes_only_allowed_states_and_never_calls_discord(self):
        for st, mid in (("pending", None), ("attention", 5), ("replacement_send_uncertain", 6)):
            pid = self.row(st, mid)
            FakeRest.calls = []
            code, b = self.forget(pid)
            self.assertEqual((code, b["success"], b["discord_untouched"]), (200, True, True), st)
            self.assertIsNone(self.status(pid)); self.assertEqual(FakeRest.calls, [], st)

    def test_published_and_inflight_and_other_states_are_refused(self):
        for st, mid in (("published", 5), ("updating", 5), ("unpublishing", 5), ("replacement_sending", 5),
                        ("replacement_retry_authorized", 5), ("failed", None)):
            pid = self.row(st, mid)
            code, b = self.forget(pid)
            self.assertEqual((code, b["code"]), (409, "record_not_removable"), st)
            self.assertEqual(self.status(pid), st)

    def test_unknown_row_is_404_and_guard_still_blocks_published(self):
        self.assertEqual(self.forget(99999)[0], 404)
        self.row("published", 5)
        self.assertEqual(self.delete_design().status_code, 409)

    def test_stuck_pending_row_unblocks_design_after_explicit_removal(self):
        pid = self.row("pending", None)
        self.assertEqual(self.delete_design().status_code, 409)
        self.assertEqual(self.forget(pid)[0], 200)
        self.assertEqual(self.delete_design().status_code, 200)


if __name__ == "__main__":
    unittest.main()
