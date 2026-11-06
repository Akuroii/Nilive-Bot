"""Automatic Shop icon preparation — behavioural tests over the REAL pipeline.

Run exactly:
    PYTHONPATH=. python scripts/test_shop_emoji_automation.py

What is real here: the Flask save/edit/delete endpoints, ``init_db`` and its
schema, the durable queue (``utils/shop_emoji_queue``), the worker
(``utils/shop_emoji_worker``), and the whole preparation core in
``utils/shop_emoji`` (hashing, aliasing, claiming, create fencing, quota,
reconciliation, ownership, duplicate detection, renaming).

What is faked: only the two boundaries a hermetic test cannot own — the Discord
HTTP API and the source-image download. Both fakes are strict: an unexpected
call fails the test instead of silently succeeding.

Covered scenarios (one class per area):
  * 100 existing products, bounded batches, resume after a simulated restart
  * duplicate images / shared content hashes
  * expired (404) and missing source URLs
  * Discord timeout and ambiguous create (no blind retry, no deletion)
  * new product creation and icon_url changes; unrelated edits do nothing
  * readable names on create + rename in place via PATCH
  * Inventory and Rank end-to-end resolution stays read-only
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_CACHE = ROOT / ".cache"
_CACHE.mkdir(exist_ok=True)
_SCRATCH = tempfile.TemporaryDirectory(prefix="shop-emoji-automation-", dir=_CACHE)
DB_FILE = Path(_SCRATCH.name) / "scratch.db"

APP = "123456789012345678"
OTHER_APP = "123456789012345999"
GUILD, OTHER_GUILD, USER = 8101, 8102, 8201
BOT_ID = "999999999999999999"
CSRF = "automation-csrf"

os.environ.update(
    DATABASE_PATH=str(DB_FILE), NERO_ENVIRONMENT="test", OWNER_ID="999999999999",
    SECRET_KEY="isolated-automation-secret-key-0123456789abcdef",
    DISCORD_CLIENT_ID=APP, DASHBOARD_DEMO_LOGIN="0", DISCORD_TOKEN="test-token-not-real",
)

import aiosqlite  # noqa: E402
from PIL import Image  # noqa: E402

from database import DB_PATH, init_db  # noqa: E402

assert Path(DB_PATH) == DB_FILE, "Refusing to run outside the scratch DB"

import dashboard.app as dashboard  # noqa: E402
from cogs import wallet  # noqa: E402
from utils import rank_card_data, rank_card_renderer as renderer  # noqa: E402
from utils import shop_emoji as SE  # noqa: E402
from utils import shop_emoji_queue as Q  # noqa: E402
from utils import shop_emoji_worker as W  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────
def png_bytes(color):
    buf = io.BytesIO()
    Image.new("RGBA", (128, 128), color).save(buf, format="PNG")
    return buf.getvalue()


URL_PREFIX = "https://assets.example.com"


def url_for(index):
    return f"{URL_PREFIX}/item-{index}.png?v=1"


class FakeDiscord:
    """Strict stand-in for DiscordAPI. Only documented operations are allowed."""

    application_id = APP
    next_id = 1_562_903_222_086_279_178

    def __init__(self, *, create_failures=None, create_timeout_indexes=(),
                 rename_failures=None):
        self.remote = []
        self.creates = 0
        self.renames = 0
        self.deletes = 0
        self.lists = 0
        self.create_failures = set(create_failures or ())
        self.create_timeout_indexes = set(create_timeout_indexes or ())
        self.rename_failures = dict(rename_failures or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def verify_application(self):
        return None

    async def bot_user_id(self):
        return BOT_ID

    async def list_emojis(self):
        self.lists += 1
        return [dict(e) for e in self.remote]

    async def create_emoji(self, name, final_png, bot_user_id):
        self.creates += 1
        if self.creates in self.create_failures:
            # A definitive rejection: Discord answered, and said no.
            raise SE.DiscordAPIError("Discord API 400: invalid emoji",
                                     code="discord_api_error", status=400, http_status=400)
        FakeDiscord.next_id += 1
        emoji = {"id": str(FakeDiscord.next_id), "name": name, "animated": False,
                 "application_id": APP, "user": {"id": bot_user_id}}
        if self.creates in self.create_timeout_indexes:
            # AMBIGUOUS: Discord created the emoji but the response was lost.
            self.remote.append(dict(emoji))
            raise SE.DiscordAPIError("Discord transport did not confirm the operation.",
                                     code="create_uncertain", status=504, uncertain=True)
        self.remote.append(dict(emoji))
        return emoji

    async def rename_emoji(self, emoji_id, name):
        self.renames += 1
        if self.rename_failures.get(str(emoji_id)) == "uncertain":
            raise SE.DiscordAPIError("rename unconfirmed", code="rename_uncertain",
                                     uncertain=True)
        if str(self.rename_failures.get(str(emoji_id))) == "rejected":
            raise SE.DiscordAPIError("Discord API 400: invalid name",
                                     code="discord_api_error", status=400, http_status=400)
        for emoji in self.remote:
            if str(emoji["id"]) == str(emoji_id):
                emoji["name"] = name
                return dict(emoji)
        raise SE.DiscordAPIError("Discord API 404: Unknown Emoji",
                                 code="discord_api_error", status=404, http_status=404)

    async def delete_emoji(self, emoji_id):   # pragma: no cover - must never run
        self.deletes += 1
        raise AssertionError(f"delete_emoji must never be called (tried {emoji_id})")


class ImageServer:
    """Deterministic source images, with optional 'dead' URLs."""

    def __init__(self):
        self.contents = {}
        self.dead = set()
        self.fetches = []

    def put(self, url, color):
        self.contents[url] = png_bytes(color)

    def kill(self, url):
        self.dead.add(url)

    async def download(self, url):
        self.fetches.append(url)
        if url in self.dead:
            raise SE.SourceImageError("The image server returned HTTP 404.",
                                      code="source_http_error", status=502)
        if url not in self.contents:
            raise SE.SourceImageError("The image server returned HTTP 404.",
                                      code="source_http_error", status=502)
        return self.contents[url]


def _hash_of(png):
    return SE.content_identity(png)


# ── base case ───────────────────────────────────────────────────────────────
class AutomationTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_FILE) + suffix).unlink(missing_ok=True)
        # init_db schedules a real Discord reconciliation task; tests must not
        # inherit background network work from a previous case.
        with patch.object(SE, "schedule_startup_reconciliation", lambda: None):
            await init_db()
        from utils.minigame_store import ensure_tables
        await ensure_tables()
        SE._preparation_locks.clear()
        W._pass_lock = None
        os.environ["DISCORD_CLIENT_ID"] = APP
        os.environ["DISCORD_TOKEN"] = "test-token-not-real"

        self.images = ImageServer()
        self.fake = FakeDiscord()

        # Boundaries: no real network, and Discord is always our fake.
        patches = [
            patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake),
            patch.object(SE, "_download_and_normalize",
                         AsyncMock(side_effect=self.images.download)),
            patch("aiohttp.ClientSession._request",
                  side_effect=AssertionError("real network blocked")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
            await db.execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
                             "VALUES (?,?,?,?,0)", (GUILD, USER, 1000, 5))
            await db.execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
                             "VALUES (?,?,1000,0)", (GUILD, USER))
            await db.execute("INSERT INTO dashboard_users "
                             "(guild_id,user_id,permission_level,enabled) "
                             "VALUES (?,?,'owner',1)", (GUILD, USER))
            await db.commit()

        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Automation", "avatar": None},
                           guild_id=GUILD, expires_at=4102444800, csrf_token=CSRF)

    # ── helpers ─────────────────────────────────────────────────────────────
    async def _one(self, sql, args=()):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(sql, args)
            return await cur.fetchone()

    async def _all(self, sql, args=()):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(sql, args)
            return await cur.fetchall()

    async def _exec(self, sql, args=()):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(sql, args)
            await db.commit()

    def _add_product(self, name, *, icon_url=None, price=100):
        r = self.client.post("/api/shop/item",
                             json={"name": name, "type": "custom", "price": price},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    def _set_icon(self, product_id, url):
        r = self.client.put(f"/api/shop/item/{product_id}", json={"icon_url": url},
                            headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    async def _product_id(self, name):
        row = await self._one("SELECT id FROM shop_items WHERE guild_id=? AND name=?",
                              (GUILD, name))
        self.assertIsNotNone(row, f"product {name!r} not found")
        return row[0]

    async def _seed_products(self, count, *, prefix="Product", colors=None):
        """Create `count` products through the REAL dashboard add + edit path."""
        ids = []
        for index in range(count):
            name = f"{prefix} {index:03d}"
            self._add_product(name)
            pid = await self._product_id(name)
            url = url_for(index)
            color = (colors(index) if colors else
                     ((index * 7) % 256, (index * 13) % 256, (index * 29) % 256, 255))
            self.images.put(url, color)
            self._set_icon(pid, url)
            ids.append(pid)
        return ids

    async def _run_pass(self, **kwargs):
        kwargs.setdefault("limit", Q.DEFAULT_BATCH_LIMIT)
        return await W.run_pass(**kwargs)

    async def _drain(self, max_passes=40, **kwargs):
        """Repeated bounded passes, exactly as the scheduler would run them."""
        reports = []
        for _ in range(max_passes):
            report = await self._run_pass(**kwargs)
            reports.append(report)
            if not report["processed"]:
                break
        return reports


# ── 1. bulk backfill ────────────────────────────────────────────────────────
class BulkBackfillTests(AutomationTestBase):
    async def test_100_products_bounded_batches_and_full_coverage(self):
        ids = await self._seed_products(100)
        self.assertEqual(len(ids), 100)

        # The queue was populated automatically by the save path — no roster.
        queued = await self._one("SELECT COUNT(*) FROM shop_emoji_queue WHERE guild_id=?",
                                 (GUILD,))
        self.assertEqual(queued[0], 100)

        reports = await self._drain()
        self.assertGreater(len(reports), 1, "work must be split across several passes")
        # Every pass respected the batch bound.
        for report in reports:
            self.assertLessEqual(report["processed"], Q.DEFAULT_BATCH_LIMIT)

        self.assertEqual(self.fake.creates, 100)
        self.assertEqual(len(self.fake.remote), 100)
        self.assertEqual(len({e["id"] for e in self.fake.remote}), 100, "no duplicate IDs")

        # Every product resolves to a ready asset with a readable name.
        rows = await self._all(
            "SELECT s.name, e.status, e.emoji_name FROM shop_items s "
            "JOIN shop_emoji_url_aliases a ON a.guild_id=s.guild_id "
            "  AND a.source_url=s.icon_url "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash WHERE s.guild_id=?", (GUILD,))
        self.assertEqual(len(rows), 100)
        for name, status, emoji_name in rows:
            self.assertEqual(status, "ready")
            self.assertRegex(emoji_name, r"^product_\d{3}_[0-9a-f]{6}$")
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)

    async def test_restart_mid_backfill_resumes_without_duplicates(self):
        await self._seed_products(100)
        first = await self._run_pass(limit=8)
        self.assertEqual(first["processed"], 8)
        self.assertEqual(self.fake.creates, 8)
        created_before = len(self.fake.remote)

        # ── simulate a Railway restart: fresh process state, same DB/volume ──
        SE._preparation_locks.clear()
        W._pass_lock = None
        rebooted = FakeDiscord()
        rebooted.remote = [dict(e) for e in self.fake.remote]   # Discord remembers
        self.fake = rebooted
        patch.object(SE, "DiscordAPI", lambda *a, **k: rebooted).start()

        # Startup discovery re-runs; finished work must not be re-queued.
        async with aiosqlite.connect(DB_PATH) as db:
            requeued = await Q.enqueue_guild_products(db, GUILD, reason="restart")
        self.assertEqual(requeued, 0)

        reports = await self._drain()
        self.assertEqual(rebooted.creates, 92, "only unfinished work is created")
        self.assertEqual(len(rebooted.remote), 100)
        self.assertEqual(len({e["id"] for e in rebooted.remote}), 100)
        self.assertGreater(created_before, 0)

    async def test_backfill_from_database_requires_no_roster_entry(self):
        # Products exist with URLs but were never queued (pre-feature data).
        await self._seed_products(12)
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            queued = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(queued, 12)
        await self._drain()
        self.assertEqual(len(self.fake.remote), 12)

    async def test_disabled_and_url_less_products_are_not_queued(self):
        self._add_product("No Image")
        self._add_product("Disabled")
        pid = await self._product_id("Disabled")
        self.images.put(url_for(0), (10, 20, 30, 255))
        self._set_icon(pid, url_for(0))
        await self._exec("UPDATE shop_items SET enabled=0 WHERE id=?", (pid,))
        async with aiosqlite.connect(DB_PATH) as db:
            queued = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(queued, 0)

    async def test_guild_isolation(self):
        await self._seed_products(3)
        self.images.put(url_for(50), (5, 250, 5, 255))
        await self._exec("INSERT INTO shop_items (guild_id,name,price,type,icon_url) "
                         "VALUES (?,?,?,?,?)",
                         (OTHER_GUILD, "Other Guild Item", 10, "custom", url_for(50)))
        async with aiosqlite.connect(DB_PATH) as db:
            queued = await Q.enqueue_guild_products(db, OTHER_GUILD)
        self.assertEqual(queued, 1)
        reports = await self._drain()
        self.assertTrue(any(r["created"] for r in reports))
        # This guild's products were not touched by another guild's backfill.
        self.assertEqual(len(self.fake.remote), 4)


# ── 2. duplicates and shared hashes ─────────────────────────────────────────
class DuplicateContentTests(AutomationTestBase):
    async def test_three_products_sharing_one_image_create_one_emoji(self):
        same = (200, 50, 50, 255)
        ids = []
        for index in range(3):
            name = f"Shared {index}"
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(index), same)     # identical bytes, distinct URLs
            self._set_icon(pid, url_for(index))
            ids.append(pid)
        await self._drain()

        self.assertEqual(self.fake.creates, 1, "one upload for one content identity")
        self.assertEqual(len(self.fake.remote), 1)
        hashes = await self._all(
            "SELECT DISTINCT content_hash FROM shop_emoji_assets WHERE application_id=?",
            (APP,))
        self.assertEqual(len(hashes), 1)
        # …and all three products resolve to that single ready asset.
        for pid in ids:
            row = await self._one(
                "SELECT e.status FROM shop_items s "
                "JOIN shop_emoji_url_aliases a ON a.guild_id=s.guild_id AND a.source_url=s.icon_url "
                "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
                "  AND e.content_hash=a.content_hash WHERE s.id=?", (pid,))
            self.assertEqual(row[0], "ready")

    async def test_identical_urls_create_one_emoji(self):
        for index in range(2):
            name = f"Same URL {index}"
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(9), (90, 90, 90, 255))
            self._set_icon(pid, url_for(9))
        await self._drain()
        self.assertEqual(self.fake.creates, 1)

    async def test_shared_hashes_are_not_duplicated_across_a_restart(self):
        same = (60, 60, 220, 255)
        for index in range(3):
            name = f"Batch Shared {index}"
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(index), same)
            self._set_icon(pid, url_for(index))
        await self._run_pass(limit=2)          # partial pass
        SE._preparation_locks.clear()
        W._pass_lock = None
        rebooted = FakeDiscord()
        rebooted.remote = [dict(e) for e in self.fake.remote]
        self.fake = rebooted
        patch.object(SE, "DiscordAPI", lambda *a, **k: rebooted).start()
        await self._drain()
        # The remaining product shares the content hash of the emoji created
        # before the restart, so it must REUSE it: zero new uploads.
        self.assertEqual(rebooted.creates, 0, "shared content is reused, never re-uploaded")
        self.assertEqual(len(rebooted.remote), 1, "exactly one emoji for one content identity")
        rows = await self._all(
            "SELECT e.status, COUNT(*) FROM shop_items s "
            "JOIN shop_emoji_url_aliases a ON a.guild_id=s.guild_id AND a.source_url=s.icon_url "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash WHERE s.guild_id=? GROUP BY e.status", (GUILD,))
        self.assertEqual(rows, [("ready", 3)], "all three products resolve to that one emoji")

    async def test_reprocessing_is_a_reuse_not_a_duplicate(self):
        await self._seed_products(4)
        await self._drain()
        creates_after_first = self.fake.creates
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD, only_unprepared=False)
        await self._drain()
        self.assertEqual(self.fake.creates, creates_after_first)


# ── 3. dead / missing source images ─────────────────────────────────────────
class SourceFailureTests(AutomationTestBase):
    async def test_expired_url_is_recorded_and_retried_with_backoff(self):
        self._add_product("Expired")
        pid = await self._product_id("Expired")
        self.images.put(url_for(0), (1, 2, 3, 255))
        self.images.kill(url_for(0))
        self._set_icon(pid, url_for(0))

        report = await self._run_pass()
        self.assertEqual(report["failed"], 1)
        self.assertEqual(self.fake.creates, 0)
        queued = await self._one(
            "SELECT attempts, last_error, next_attempt_at FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(queued[0], 1)
        self.assertIn("source_http_error", queued[1])
        self.assertIsNotNone(queued[2], "a retry must be scheduled, not spun on")

        # Backoff means the next pass does not immediately re-fetch.
        report2 = await self._run_pass()
        self.assertEqual(report2["processed"], 0)

        # The image comes back: the queued intent survives and then succeeds.
        self.images.put(url_for(0), (1, 2, 3, 255))
        self.images.dead.clear()
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
        await self._run_pass()
        self.assertEqual(self.fake.creates, 1)

    async def test_permanently_dead_url_stops_after_max_attempts(self):
        self._add_product("Dead")
        pid = await self._product_id("Dead")
        self.images.put(url_for(0), (4, 5, 6, 255))
        self.images.kill(url_for(0))
        self._set_icon(pid, url_for(0))
        for _ in range(Q.MAX_AUTO_ATTEMPTS):
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
            await self._run_pass()
        row = await self._one(
            "SELECT attempts, next_attempt_at FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(row[0], Q.MAX_AUTO_ATTEMPTS)
        self.assertIsNone(row[1], "parks for a human instead of hammering the API")

    async def test_deleted_listing_drops_queued_work(self):
        await self._seed_products(2)
        pid = (await self._all("SELECT id FROM shop_items WHERE guild_id=? ORDER BY id",
                               (GUILD,)))[0][0]
        r = self.client.delete(f"/api/shop/item/{pid}", headers={"X-CSRF-Token": CSRF})
        self.assertIn(r.status_code, (200, 204))
        remaining = await self._all(
            "SELECT product_id FROM shop_emoji_queue WHERE guild_id=?", (GUILD,))
        self.assertNotIn(pid, [row[0] for row in remaining])

# ── 4. Discord failures and ambiguous creates ───────────────────────────────
class DiscordFailureTests(AutomationTestBase):
    async def test_ambiguous_create_is_never_retried_blindly(self):
        await self._seed_products(1)
        self.fake = FakeDiscord(create_timeout_indexes={1})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()

        report = await self._run_pass()
        self.assertEqual(report["deferred"], 1)
        queued = await self._one("SELECT attempts, last_error FROM shop_emoji_queue")
        self.assertIsNotNone(queued[1])
        self.assertEqual(len(self.fake.remote), 1, "Discord did create it")

        # The asset must be uncertain — not ready, not failed.
        status = await self._one("SELECT status FROM shop_emoji_assets")
        self.assertIn(status[0], {"uncertain", "preparing"})

        # Next pass: reconciliation MUST adopt it instead of POSTing again.
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
        report2 = await self._run_pass()
        self.assertEqual(self.fake.creates, 1, "no second create for an ambiguous one")
        self.assertEqual(report2["reused"], 1)
        status2 = await self._one("SELECT status, emoji_id FROM shop_emoji_assets")
        self.assertEqual(status2[0], "ready")
        self.assertEqual(str(status2[1]), self.fake.remote[0]["id"])
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)

    async def test_transport_failure_marks_failed_without_creating(self):
        await self._seed_products(1)
        self.fake = FakeDiscord(create_failures={1})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()
        report = await self._run_pass()
        self.assertEqual(report["failed"], 1)
        status = await self._one("SELECT status FROM shop_emoji_assets")
        self.assertEqual(status[0], "failed")
        self.assertEqual(self.fake.deletes, 0)

    async def test_worker_never_deletes(self):
        await self._seed_products(3)
        self.fake = FakeDiscord(create_timeout_indexes={1, 2})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()
        await self._drain()
        self.assertEqual(self.fake.deletes, 0)

    async def test_time_budget_bounds_a_single_pass(self):
        await self._seed_products(20)
        report = await self._run_pass(limit=20, time_budget=0.0)
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual(self.fake.creates, 0)
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 20)

    async def test_missing_credentials_leaves_queue_untouched(self):
        await self._seed_products(2)
        with patch.dict(os.environ, {"DISCORD_TOKEN": ""}):
            report = await self._run_pass()
        self.assertTrue(report.get("skipped_no_credentials"))
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 2)
        self.assertEqual(self.fake.creates, 0)


# ── 5. future changes ───────────────────────────────────────────────────────
class FutureChangeTests(AutomationTestBase):
    async def test_new_product_is_queued_automatically(self):
        self._add_product("Brand New")
        pid = await self._product_id("Brand New")
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0,
                         "no image yet, so nothing to prepare")
        self.images.put(url_for(0), (10, 200, 10, 255))
        self._set_icon(pid, url_for(0))
        row = await self._one("SELECT source_url, reason FROM shop_emoji_queue "
                              "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(row[0], url_for(0))
        self.assertEqual(row[1], "icon_url_changed")
        await self._drain()
        self.assertEqual(self.fake.creates, 1)

    async def test_unrelated_edit_does_not_requeue(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        await self._drain()
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)
        creates = self.fake.creates

        r = self.client.put(f"/api/shop/item/{pid}",
                            json={"price": 999, "description": "new text",
                                  "rarity": "epic"},
                            headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0,
                         "editing non-icon fields must not queue icon work")
        await self._drain()
        self.assertEqual(self.fake.creates, creates)

    async def test_icon_url_change_queues_the_new_image(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        await self._drain()
        old_id = await self._one("SELECT emoji_id FROM shop_emoji_assets")
        self.images.put(url_for(999), (7, 7, 200, 255))
        self._set_icon(pid, url_for(999))
        await self._drain()

        self.assertEqual(self.fake.creates, 2)
        # New alias resolves to the NEW image; the old asset is untouched.
        new = await self._one(
            "SELECT e.emoji_id, e.status FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash "
            "WHERE a.guild_id=? AND a.source_url=?", (GUILD, url_for(999)))
        self.assertEqual(new[1], "ready")
        self.assertNotEqual(str(new[0]), str(old_id[0]))
        old = await self._one("SELECT status, emoji_id FROM shop_emoji_assets WHERE emoji_id=?",
                              (old_id[0],))
        self.assertEqual(old[0], "ready", "the previous emoji is never destroyed")

    async def test_clearing_the_url_drops_queued_work(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        self._set_icon(pid, "")
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)

    async def test_refreshing_the_same_url_does_not_reset_failure_backoff(self):
        self._add_product("Retry Me")
        pid = await self._product_id("Retry Me")
        self.images.put(url_for(0), (1, 1, 1, 255))
        self.images.kill(url_for(0))
        self._set_icon(pid, url_for(0))
        await self._run_pass()
        before = await self._one("SELECT attempts FROM shop_emoji_queue WHERE product_id=?",
                                 (pid,))
        self._set_icon(pid, url_for(0))   # same URL again
        after = await self._one("SELECT attempts FROM shop_emoji_queue WHERE product_id=?",
                               (pid,))
        self.assertEqual(before[0], after[0])


# ── 6. readable names ───────────────────────────────────────────────────────
class NamingTests(AutomationTestBase):
    async def test_new_emoji_is_created_with_a_readable_name(self):
        await self._seed_products(1)
        await self._drain()
        name = self.fake.remote[0]["name"]
        self.assertRegex(name, r"^product_000_[0-9a-f]{6}$")
        stored = await self._one("SELECT emoji_name, emoji_id FROM shop_emoji_assets")
        self.assertEqual(stored[1], self.fake.remote[0]["id"])
        self.assertEqual(stored[0], name)

    async def test_arabic_product_names_transliterate(self):
        cases = {
            "عصير المجرات": r"^asir_almjrat_[0-9a-f]{6}$",
            "سهران السيرفر": r"^shran_alsirfr_[0-9a-f]{6}$",
        }
        for name, pattern in cases.items():
            emoji_name = SE.readable_emoji_name(name, "3ef701f9213ab1742536266a" + "ab" * 20)
            self.assertRegex(emoji_name, pattern)
            self.assertLessEqual(len(emoji_name), 32)
            self.assertTrue(emoji_name.isascii())

    async def test_name_falls_back_when_no_ascii_slug_survives(self):
        h = "ab" * 32
        self.assertEqual(SE.readable_emoji_name("🎮🎮", h), SE.deterministic_emoji_name(h))
        self.assertEqual(SE.readable_emoji_name("", h), SE.deterministic_emoji_name(h))

    async def test_existing_emoji_is_renamed_in_place_not_recreated(self):
        await self._seed_products(1)
        await self._drain()
        # Simulate a legacy asset carrying the old hash-only name.
        await self._exec("UPDATE shop_emoji_assets SET emoji_name='shop_' || substr(content_hash,1,24), "
                         "managed_name=0")
        legacy = await self._one("SELECT emoji_id, emoji_name FROM shop_emoji_assets")
        self.fake.remote[0]["name"] = legacy[1]

        result = await W.rename_pass(guild_id=GUILD, apply=True)
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(self.fake.renames, 1)
        self.assertEqual(self.fake.creates, 1, "renaming must not create a new emoji")
        now = await self._one("SELECT emoji_id, emoji_name, managed_name FROM shop_emoji_assets")
        self.assertEqual(now[0], legacy[0], "the emoji ID is preserved")
        self.assertRegex(now[1], r"^product_000_[0-9a-f]{6}$")
        self.assertEqual(now[2], 1)
        # Renaming again is a no-op.
        again = await W.rename_pass(guild_id=GUILD, apply=True)
        self.assertEqual(again["renamed"], 0)
        self.assertEqual(self.fake.renames, 1)

    async def test_rename_preview_does_not_touch_discord(self):
        await self._seed_products(2)
        await self._drain()
        await self._exec("UPDATE shop_emoji_assets SET emoji_name='shop_legacy'")
        preview = await W.rename_pass(guild_id=GUILD, apply=False)
        self.assertEqual(len(preview["preview"]), 2)
        self.assertEqual(self.fake.renames, 0)
        self.assertEqual(preview["renamed"], 0)

    async def test_uncertain_rename_does_not_break_resolution(self):
        await self._seed_products(1)
        await self._drain()
        emoji_id = self.fake.remote[0]["id"]
        await self._exec("UPDATE shop_emoji_assets SET emoji_name='shop_legacy'")
        self.fake.rename_failures = {str(emoji_id): "uncertain"}

        await W.rename_pass(guild_id=GUILD, apply=True)
        # A managed asset tolerates a name difference on refresh…
        async with aiosqlite.connect(DB_PATH) as db:
            asset = await SE._get_asset(db, APP, (await self._one(
                "SELECT content_hash FROM shop_emoji_assets"))[0])
        # …and Inventory still resolves it.
        entries = await wallet._decorate(GUILD, [{
            "item_name": "Product 000", "item_type": "custom", "quantity": 1,
            "entry_key": "i:1", "icon_url": None}], application_id=APP)
        self.assertTrue(entries[0]["app_emoji"].startswith("<:"))

    async def test_rejected_rename_keeps_the_working_name(self):
        await self._seed_products(1)
        await self._drain()
        emoji_id = self.fake.remote[0]["id"]
        await self._exec("UPDATE shop_emoji_assets SET emoji_name='shop_legacy'")
        self.fake.rename_failures = {str(emoji_id): "rejected"}
        await W.rename_pass(guild_id=GUILD, apply=True)
        now = await self._one("SELECT emoji_id, emoji_name, managed_name FROM shop_emoji_assets")
        self.assertEqual(now[1], "shop_legacy")
        self.assertEqual(now[2], 0, "a rejected name is not treated as managed")


# ── 7. read-only consumers ──────────────────────────────────────────────────
class ReadOnlyConsumerTests(AutomationTestBase):
    async def test_inventory_and_rank_resolve_prepared_icons(self):
        names = ["عصير المجرات", "سهران السيرفر", "عنتيل السيرفر", "شوكولاته الجلاكسي"]
        for index, name in enumerate(names):
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(index), (index * 40 % 256, 20, 200, 255))
            self._set_icon(pid, url_for(index))
            await self._exec(
                "INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,quantity) "
                "VALUES (?,?,?,'custom',1)", (GUILD, USER, name))
        await self._drain()
        self.assertEqual(len(self.fake.remote), 4)

        # Inventory: the prepared application emoji reaches the row.
        entries = await wallet._decorate(GUILD, [
            {"item_name": n, "item_type": "custom", "quantity": 1,
             "entry_key": f"i:{i}", "icon_url": None} for i, n in enumerate(names)
        ], application_id=APP)
        for entry in entries:
            self.assertTrue(entry["app_emoji"].startswith("<:"), entry)
            self.assertIn(entry["app_emoji"], wallet._item_display(entry, "⚪"))

        # Rank: the same token reaches the grid payload and the renderer's
        # candidate list.
        data = await rank_card_data.get_rank_card_data(GUILD, USER, application_id=APP)
        self.assertEqual(len(data["inventory_grid"]), 4)
        for item in data["inventory_grid"]:
            self.assertTrue(item["app_emoji"].startswith("<:"), item)
            self.assertIn(renderer._fetch_item_icon.__name__, "_fetch_item_icon")

    async def test_opening_inventory_or_rank_never_creates_or_downloads(self):
        await self._seed_products(4)
        await self._drain()
        creates = self.fake.creates
        fetches = len(self.images.fetches)
        remote_before = [dict(e) for e in self.fake.remote]
        await self._exec("DELETE FROM shop_emoji_queue")

        for _ in range(3):
            await wallet._decorate(GUILD, [
                {"item_name": "Product 000", "item_type": "custom",
                 "quantity": 1, "entry_key": "i:1", "icon_url": None}],
                application_id=APP)
            await rank_card_data.get_rank_card_data(GUILD, USER, application_id=APP)

        self.assertEqual(self.fake.creates, creates)
        self.assertEqual(len(self.images.fetches), fetches)
        self.assertEqual(self.fake.remote, remote_before)
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)

    async def test_idle_pass_makes_zero_discord_calls(self):
        await self._seed_products(2)
        await self._drain()
        lists, creates, fetches = self.fake.lists, self.fake.creates, len(self.images.fetches)
        for _ in range(5):
            report = await self._run_pass()
            self.assertTrue(report.get("skipped_idle"), report)
        self.assertEqual(self.fake.lists, lists, "an idle pass must not list emojis")
        self.assertEqual(self.fake.creates, creates)
        self.assertEqual(len(self.images.fetches), fetches)

    async def test_rank_output_is_unchanged_for_unprepared_products(self):
        self._add_product("Unprepared")
        await self._exec(
            "INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,quantity) "
            "VALUES (?,?,?,'custom',1)", (GUILD, USER, "Unprepared"))
        data = await rank_card_data.get_rank_card_data(GUILD, USER, application_id=APP)
        self.assertEqual(data["inventory_grid"][0]["app_emoji"], "")
        self.assertEqual(self.fake.creates, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
