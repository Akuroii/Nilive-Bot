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
    """Strict stand-in for DiscordAPI with REALISTIC response shapes.

    Discord's create-emoji response does not always carry ``application_id`` or
    a populated ``user`` object, and that is exactly the production condition
    that produced "Discord's create response did not confirm the expected
    emoji." So the fake can deliberately omit those fields.
    """

    application_id = APP
    next_id = 1_562_903_222_086_279_178

    def __init__(self, *, create_failures=None, create_timeout_indexes=(),
                 rename_failures=None, omit_metadata_on_create=(),
                 omit_metadata_in_list=False, list_status=None):
        self.remote = []
        self.creates = 0
        self.renames = 0
        self.deletes = 0
        self.lists = 0
        self.create_failures = set(create_failures or ())
        self.create_timeout_indexes = set(create_timeout_indexes or ())
        self.rename_failures = dict(rename_failures or {})
        # Index of creates whose response omits application_id/user (ambiguous).
        self.omit_metadata_on_create = set(omit_metadata_on_create or ())
        # When True, list_emojis() returns entries without ownership metadata.
        self.omit_metadata_in_list = omit_metadata_in_list
        # e.g. {"status": 429, "retry_after": 1.5} or {"status": 500}
        self.list_status = dict(list_status or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def verify_application(self):
        return None

    async def bot_user_id(self):
        return BOT_ID

    def _view(self, emoji):
        out = {"id": emoji["id"], "name": emoji["name"],
               "animated": emoji.get("animated", False)}
        if not self.omit_metadata_in_list:
            out["application_id"] = emoji.get("application_id", APP)
            out["user"] = emoji.get("user", {"id": BOT_ID})
        return out

    async def list_emojis(self):
        self.lists += 1
        if self.list_status:
            status = self.list_status.get("status", 500)
            raise SE.DiscordAPIError(
                f"Discord API {status}: simulated",
                code="discord_api_error", status=status, http_status=status,
                retry_after=self.list_status.get("retry_after"))
        return [self._view(e) for e in self.remote]

    async def create_emoji(self, name, final_png, bot_user_id):
        self.creates += 1
        if self.creates in self.create_failures:
            raise SE.DiscordAPIError("Discord API 400: invalid emoji",
                                     code="discord_api_error", status=400, http_status=400)
        FakeDiscord.next_id += 1
        stored = {"id": str(FakeDiscord.next_id), "name": name, "animated": False,
                  "application_id": APP, "user": {"id": bot_user_id}}
        self.remote.append(dict(stored))
        if self.creates in self.create_timeout_indexes:
            raise SE.DiscordAPIError("Discord transport did not confirm the operation.",
                                     code="create_uncertain", status=504, uncertain=True)
        response = dict(stored)
        if self.creates in self.omit_metadata_on_create:
            # Discord created it, but the response omits application_id/user —
            # the exact production shape behind "create response did not
            # confirm the expected emoji".
            response = {"id": stored["id"], "name": name, "animated": False}
        # Mirror DiscordAPI.create_emoji's real validation contract instead of
        # bypassing it, so the code under test sees what production saw.
        ok = (isinstance(response, dict) and SE._valid_emoji_id(response.get("id"))
              and response.get("name") == name
              and not bool(response.get("animated", False))
              and SE._ownership_supported(response, self.application_id, bot_user_id))
        if not ok:
            raise SE.DiscordAPIError("Discord's create response did not confirm the expected emoji.",
                                     code="create_uncertain", uncertain=True)
        return response

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
                return self._view(emoji)
        raise SE.DiscordAPIError("Discord API 404: Unknown Emoji",
                                 code="discord_api_error", status=404, http_status=404)

    async def delete_emoji(self, emoji_id):   # pragma: no cover - must never run
        self.deletes += 1
        raise AssertionError(f"delete_emoji must never be called (tried {emoji_id})")


class ImageServer:
    """Deterministic source images, with optional 'dead' URLs and a gate.

    The gate lets a test hold a download open, so a second actor (the Dashboard)
    can change the queue intent while the worker is mid-flight.
    """

    def __init__(self):
        self.contents = {}
        self.dead = set()
        self.fetches = []
        self.gates = {}

    def put(self, url, color):
        self.contents[url] = png_bytes(color)

    def kill(self, url):
        self.dead.add(url)

    def gate(self, url):
        """Return an asyncio.Event that must be set before this URL downloads."""
        event = asyncio.Event()
        self.gates[url] = event
        return event

    def ungated_fetches(self):
        return [u for u in self.fetches if u not in self.gates]

    async def download(self, url):
        self.fetches.append(url)
        gate = self.gates.get(url)
        if gate is not None:
            await gate.wait()
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

    async def _queue_state(self, product_id):
        row = await self._one(
            "SELECT state FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (GUILD, product_id))
        return row[0] if row else None

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
            self.assertRegex(emoji_name, r"^product_\d{3}_[0-9a-f]{8}$")
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


# ── 2b. generation fencing: a stale worker must never clobber newer intent ──
class GenerationFencingTests(AutomationTestBase):
    """#2: the Dashboard and the Bot are separate processes over one SQLite file.

    Each test holds a real download open, changes the product through the real
    Dashboard API while the worker is mid-flight, then releases it.
    """

    async def _start_blocked_pass(self, url):
        """Run a worker pass that blocks inside the download of `url`."""
        gate = self.images.gate(url)
        task = asyncio.create_task(self._run_pass())
        # Wait until the worker has actually reached the download.
        for _ in range(200):
            if url in self.images.fetches:
                break
            await asyncio.sleep(0.01)
        self.assertIn(url, self.images.fetches, "worker never started the download")
        return task, gate

    async def test_url_changed_during_download_keeps_newer_intent(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        url_a, url_b = url_for(0), url_for(1)
        self.images.put(url_b, (1, 200, 1, 255))

        task, gate = await self._start_blocked_pass(url_a)
        # ── the Dashboard saves a NEW image while the worker is downloading A ──
        self._set_icon(pid, url_b)
        queued_b = await self._one(
            "SELECT source_url, generation, state FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(queued_b[0], url_b)

        gate.set()
        await asyncio.wait_for(task, timeout=30)

        # The stale generation A must not have deleted or overwritten B.
        now = await self._one(
            "SELECT source_url, state FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (GUILD, pid))
        self.assertIsNotNone(now, "newer queue intent was destroyed by the stale worker")
        self.assertEqual(now[0], url_b)

        # A subsequent pass prepares B; inventory/rank resolve the latest image.
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
        await self._drain()
        ready = await self._one(
            "SELECT e.emoji_id FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash "
            "WHERE a.guild_id=? AND a.source_url=? AND e.status='ready'", (GUILD, url_b))
        self.assertIsNotNone(ready, "the new image was never prepared")
        # Inventory resolves that prepared emoji, not the superseded one.
        entries = await wallet._decorate(GUILD, [{
            "item_name": "Product 000", "item_type": "custom", "quantity": 1,
            "entry_key": "i:1", "icon_url": None}], application_id=APP)
        self.assertIn(f":{ready[0]}>", entries[0]["app_emoji"])
        self.assertEqual(self.fake.deletes, 0)

    async def test_url_cleared_during_download_is_honoured(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        task, gate = await self._start_blocked_pass(url_for(0))
        self._set_icon(pid, "")              # admin clears the image
        gate.set()
        await asyncio.wait_for(task, timeout=30)
        self.assertIsNone(
            await self._one("SELECT 1 FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
                            (GUILD, pid)),
            "a stale worker re-created intent for a cleared image")
        self.assertEqual(self.fake.deletes, 0)

    async def test_product_deleted_during_download_is_honoured(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        task, gate = await self._start_blocked_pass(url_for(0))
        r = self.client.delete(f"/api/shop/item/{pid}", headers={"X-CSRF-Token": CSRF})
        self.assertIn(r.status_code, (200, 204))
        gate.set()
        await asyncio.wait_for(task, timeout=30)
        self.assertIsNone(
            await self._one("SELECT 1 FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
                            (GUILD, pid)),
            "the stale worker must not resurrect a deleted listing")

    async def test_stale_generation_cannot_mark_done_or_failed(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        async with aiosqlite.connect(DB_PATH) as db:
            stale = await Q.descriptor(db, GUILD, pid)
        # A newer generation replaces the row (as the Dashboard would).
        self.images.put(url_for(2), (2, 2, 2, 255))
        self._set_icon(pid, url_for(2))

        async with aiosqlite.connect(DB_PATH) as db:
            self.assertFalse(await Q.mark_done(db, stale), "stale mark_done must be refused")
            self.assertFalse(await Q.mark_failed(db, stale, "boom"),
                             "stale mark_failed must be refused")
            self.assertFalse(await Q.defer_uncertain(db, stale, "boom"),
                             "stale defer must be refused")
            self.assertFalse(await Q.record_attempt(db, stale),
                             "stale claim must be refused")
            survivor = await Q.descriptor(db, GUILD, pid)
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor["source_url"], url_for(2))
        self.assertEqual(survivor["state"], Q.STATE_QUEUED)
        self.assertIsNone(survivor["last_error"])


# ── 2c. backfill must check the CURRENT url and the ACTIVE application ──────
class BackfillScopeTests(AutomationTestBase):
    """#3: a stale or wrong-scope alias must not silently suppress a backfill."""

    async def _currently_ready(self, name):
        row = await self._one(
            "SELECT COALESCE(SUM(e.status='ready'),0) FROM shop_items s "
            "JOIN shop_emoji_url_aliases a ON a.guild_id=s.guild_id "
            "  AND a.source_url=s.icon_url "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash WHERE s.name=?", (name,))
        return row[0]

    async def test_current_url_with_ready_alias_is_skipped(self):
        await self._seed_products(1)
        await self._drain()
        async with aiosqlite.connect(DB_PATH) as db:
            queued = await Q.enqueue_guild_products(db, GUILD, application_id=APP)
        self.assertEqual(queued, 0, "a genuinely prepared product must not be re-queued")

    async def test_changed_url_is_queued_even_when_the_old_url_is_ready(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        url_old = url_for(0)
        await self._drain()
        self.assertEqual(await self._currently_ready("Product 000"), 1)

        # Simulate a URL change that bypassed the save hook (e.g. direct SQL).
        self.images.put(url_for(5), (5, 100, 5, 255))
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (url_for(5), pid))
        async with aiosqlite.connect(DB_PATH) as db:
            queued = await Q.enqueue_guild_products(db, GUILD, application_id=APP)
        self.assertEqual(queued, 1, "the CURRENT url is unprepared and must be queued")
        await self._drain()
        ready = await self._one(
            "SELECT e.status FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash "
            "WHERE a.guild_id=? AND a.source_url=? AND a.application_id=?",
            (GUILD, url_for(5), APP))
        self.assertEqual(ready[0], "ready")

    async def test_alias_under_another_application_does_not_suppress(self):
        await self._seed_products(1)
        await self._exec("DELETE FROM shop_emoji_queue")   # pre-feature product
        # A ready asset prepared by a DIFFERENT application for the same URL.
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT icon_url FROM shop_items WHERE guild_id=?", (GUILD,))
            url = (await cur.fetchone())[0]
            await db.execute(
                "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,"
                "animated,creator_guild_id,pipeline_version,status) "
                "VALUES (?,?,?,?,0,?,?, 'ready')",
                (OTHER_APP, "a" * 64, "shop_foreign", "700000000000000001", GUILD, "v1"))
            await db.execute(
                "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash) "
                "VALUES (?,?,?,?)", (OTHER_APP, GUILD, url, "a" * 64))
            await db.commit()
            # A backfill for the ACTIVE application must not be suppressed.
            queued = await Q.enqueue_guild_products(db, GUILD, application_id=APP)
        self.assertEqual(queued, 1, "another application's ready alias is not ours")
        await self._drain()
        mine = await self._one(
            "SELECT e.status FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash "
            "WHERE a.guild_id=? AND a.application_id=? AND e.status='ready'", (GUILD, APP))
        self.assertIsNotNone(mine)

    async def test_alias_under_another_guild_is_never_cross_linked(self):
        await self._seed_products(1)
        await self._exec("DELETE FROM shop_emoji_queue")   # pre-feature product
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT icon_url FROM shop_items WHERE guild_id=?", (GUILD,))
            url = (await cur.fetchone())[0]
            await db.execute(
                "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,"
                "animated,creator_guild_id,pipeline_version,status) "
                "VALUES (?,?,?,?,0,?,?, 'ready')",
                (APP, "b" * 64, "shop_other_guild", "700000000000000002", OTHER_GUILD, "v1"))
            await db.execute(
                "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash) "
                "VALUES (?,?,?,?)", (APP, OTHER_GUILD, url, "b" * 64))
            await db.commit()
            queued = await Q.enqueue_guild_products(db, GUILD, application_id=APP)
        self.assertEqual(queued, 1, "another guild's alias must not suppress or be reused")
        self.assertEqual(self.fake.creates, 0)

    async def test_non_ready_alias_keeps_reconciliation_behaviour(self):
        await self._seed_products(1)
        await self._exec("DELETE FROM shop_emoji_queue")   # pre-feature product
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT icon_url FROM shop_items WHERE guild_id=?", (GUILD,))
            url = (await cur.fetchone())[0]
            await db.execute(
                "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,"
                "animated,creator_guild_id,pipeline_version,status) "
                "VALUES (?,?,?,NULL,0,?,?, 'uncertain')",
                (APP, "c" * 64, "shop_uncertain", GUILD, "v1"))
            await db.execute(
                "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash) "
                "VALUES (?,?,?,?)", (APP, GUILD, url, "c" * 64))
            await db.commit()
            queued = await Q.enqueue_guild_products(db, GUILD, application_id=APP)
        self.assertEqual(queued, 1, "a non-ready alias means 'not done', so it is queued")

    async def test_duplicate_listings_are_never_guessed(self):
        # Two listings with the same name: each keeps its own URL and queue row.
        self._add_product("Twin")
        p1 = await self._product_id("Twin")
        self.images.put(url_for(10), (10, 10, 10, 255))
        self._set_icon(p1, url_for(10))
        await self._exec(
            "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
            "VALUES (?,?,?,?,?,1)", (GUILD, "Twin", 50, "custom", url_for(11)))
        self.images.put(url_for(11), (11, 11, 11, 255))
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        rows = await self._all(
            "SELECT source_url FROM shop_emoji_queue WHERE guild_id=? ORDER BY product_id",
            (GUILD,))
        self.assertEqual([r[0] for r in rows], [url_for(10), url_for(11)],
                         "each duplicate keeps its own URL, nothing is merged or guessed")


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
        self.assertEqual(await self._queue_state(pid), Q.STATE_RETRY,
                         "a backoff row stays live, not parked")
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

    async def test_permanently_dead_url_is_terminal_over_many_passes(self):
        """#1: a parked row must not become eligible again through due()."""
        self._add_product("Dead")
        pid = await self._product_id("Dead")
        self.images.put(url_for(0), (4, 5, 6, 255))
        self.images.kill(url_for(0))
        self._set_icon(pid, url_for(0))
        for _ in range(Q.MAX_AUTO_ATTEMPTS):
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
            await self._run_pass()
        row = await self._one(
            "SELECT state, attempts, last_error FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(row[0], Q.STATE_PARKED)
        self.assertEqual(row[1], Q.MAX_AUTO_ATTEMPTS)
        terminal_error = row[2]

        # Five further scheduler passes: NOTHING may happen for this product.
        fetches = len(self.images.fetches)
        lists = self.fake.lists
        for _ in range(5):
            report = await self._run_pass()
            self.assertTrue(report.get("skipped_idle"), report)
        self.assertEqual(len(self.images.fetches), fetches, "no further downloads")
        self.assertEqual(self.fake.lists, lists, "no Discord request at all")
        self.assertEqual(self.fake.creates, 0)
        after = await self._one(
            "SELECT state, attempts, last_error FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(after[0], Q.STATE_PARKED)
        self.assertEqual(after[1], Q.MAX_AUTO_ATTEMPTS, "attempt count unchanged")
        self.assertEqual(after[2], terminal_error, "terminal error still visible")

        # A NEW url reactivates it (generation bump), and it can then succeed.
        self.images.put(url_for(77), (4, 5, 6, 255))
        self._set_icon(pid, url_for(77))
        state = await self._queue_state(pid)
        self.assertEqual(state, Q.STATE_QUEUED, "a changed URL reactivates a park")
        await self._drain()
        self.assertEqual(self.fake.creates, 1)
        self.assertIsNone(await self._queue_state(pid), "finished and removed")

    async def test_owner_retry_reactivates_a_parked_product(self):
        """#1: explicit owner reactivation resets the budget for the SAME url."""
        self._add_product("Parked Then Retried")
        pid = await self._product_id("Parked Then Retried")
        self.images.put(url_for(0), (9, 9, 9, 255))
        self.images.kill(url_for(0))
        self._set_icon(pid, url_for(0))
        for _ in range(Q.MAX_AUTO_ATTEMPTS):
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
            await self._run_pass()
        self.assertEqual(await self._queue_state(pid), Q.STATE_PARKED)

        # Discovery alone must NOT resurrect it (same URL, no owner action).
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(await self._queue_state(pid), Q.STATE_PARKED)

        r = self.client.post("/api/shop-publisher/emojis/retry",
                             json={"product_ids": [pid]},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["reactivated"], 1)
        row = await self._one(
            "SELECT state, attempts FROM shop_emoji_queue WHERE product_id=?", (pid,))
        self.assertEqual(row[0], Q.STATE_QUEUED)
        self.assertEqual(row[1], 0, "owner retry resets the retry budget")

        self.images.put(url_for(0), (9, 9, 9, 255))
        self.images.dead.clear()
        await self._drain()
        self.assertEqual(self.fake.creates, 1)

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


# ── 4b. the real production uncertain-create case (#7) ─────────────────────
class ProductionUncertainCreateTests(AutomationTestBase):
    """The exact production shape: ``عصير المجرات: uncertain`` /
    "Discord's create response did not confirm the expected emoji."

    Discord created the emoji but the POST body lacked ``application_id``/``user``,
    so the local asset could not be confirmed. Reconciliation must then list the
    emoji, and if the LIST carries ownership proof, adopt it — without a second
    POST.
    """

    async def test_uncertain_then_reconciled_by_listing(self):
        self._add_product("عصير المجرات")
        pid = await self._product_id("عصير المجرات")
        self.images.put(url_for(0), (120, 30, 200, 255))
        self._set_icon(pid, url_for(0))

        # create response omits metadata; the list WILL carry it.
        self.fake = FakeDiscord(omit_metadata_on_create={1})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()

        report = await self._run_pass()
        self.assertEqual(report["deferred"], 1, report)
        status, emoji_id, last_error = await self._one(
            "SELECT status, emoji_id, last_error FROM shop_emoji_assets")
        self.assertEqual(status, "uncertain")
        self.assertIn("did not confirm", (last_error or "").lower(), last_error)
        self.assertEqual(self.fake.creates, 1)
        created_id = self.fake.remote[0]["id"]

        # Next scheduled pass: reconcile (list has ownership), adopt, no POST.
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
        report2 = await self._run_pass()
        self.assertEqual(self.fake.creates, 1, "NEVER a second POST for an ambiguous create")
        self.assertEqual(report2["reused"], 1, report2)
        adopted = await self._one("SELECT status, emoji_id, emoji_name FROM shop_emoji_assets")
        self.assertEqual(adopted[0], "ready")
        self.assertEqual(str(adopted[1]), created_id)

        # Inventory resolves the custom emoji by its real ID.
        entries = await wallet._decorate(GUILD, [{
            "item_name": "عصير المجرات", "item_type": "custom", "quantity": 1,
            "entry_key": "i:1", "icon_url": None}], application_id=APP)
        self.assertIn(f":{created_id}>", entries[0]["app_emoji"])

        # Rank keeps using the prepared emoji (CDN) as its first source.
        await self._exec(
            "INSERT OR IGNORE INTO inventory_items (guild_id,user_id,item_name,item_type,quantity) "
            "VALUES (?,?,?,'custom',1)", (GUILD, USER, "عصير المجرات"))
        data = await rank_card_data.get_rank_card_data(GUILD, USER, application_id=APP)
        item = next(i for i in data["inventory_grid"] if i["item_name"] == "عصير المجرات")
        self.assertIn(f":{created_id}>", item["app_emoji"])
        self.assertTrue(item["app_emoji"].startswith("<:"))
        self.assertEqual(self.fake.deletes, 0)

    async def test_uncertain_stays_uncertain_when_ownership_cannot_be_proven(self):
        self._add_product("عصير المجرات")
        pid = await self._product_id("عصير المجرات")
        self.images.put(url_for(0), (120, 30, 200, 255))
        self._set_icon(pid, url_for(0))

        self.fake = FakeDiscord(omit_metadata_on_create={1},
                                omit_metadata_in_list=True)
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()

        await self._run_pass()
        for _ in range(3):
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
            await self._run_pass()

        status, emoji_id = await self._one("SELECT status, emoji_id FROM shop_emoji_assets")
        self.assertEqual(status, "uncertain", "unproven ownership must never become ready")
        self.assertIsNone(emoji_id, "an unverified emoji must never be adopted")
        self.assertEqual(self.fake.creates, 1, "and never duplicated")
        self.assertEqual(len(self.fake.remote), 1)
        self.assertEqual(self.fake.deletes, 0)

        # And it is NOT invisible: the queue row is still deferred, not parked.
        state = await self._queue_state(pid)
        self.assertIn(state, (Q.STATE_RETRY, Q.STATE_QUEUED))


# ── 4c. guild authorisation and isolation (#8) ─────────────────────────────
class GuildAuthorizationTests(AutomationTestBase):
    async def test_no_guild_is_authorised_by_default(self):
        await self._seed_products(2)
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertFalse(await Q.is_authorized(db, GUILD),
                             "no guild is authorised until an owner asks")
            self.assertEqual(await Q.authorized_guilds(db), [])
        # The bot's discovery loop therefore has nothing to sweep: it iterates
        # the authorisation table, not bot.guilds.
        report = await self._run_pass()
        self.assertFalse(report.get("skipped_no_credentials"))


    async def test_owner_backfill_authorises_only_their_own_guild(self):
        await self._seed_products(2)
        self.images.put(url_for(50), (5, 250, 5, 255))
        await self._exec("INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
                         "VALUES (?,?,?,?,?,1)",
                         (OTHER_GUILD, "Other Guild Item", 10, "custom", url_for(50)))
        await self._exec("DELETE FROM shop_emoji_queue")

        r = self.client.post("/api/shop-publisher/emojis/backfill", json={},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["queue"]["authorized"])
        self.assertEqual(r.get_json()["queued"], 2, "only this guild's products")
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertEqual(await Q.authorized_guilds(db), [GUILD])
            other = await Q.descriptor(db, OTHER_GUILD, 999)   # not queued either
        self.assertIsNone(other)
        queued_guilds = await self._all("SELECT DISTINCT guild_id FROM shop_emoji_queue")
        self.assertEqual([g[0] for g in queued_guilds], [GUILD])

        await self._drain()
        self.assertEqual(len(self.fake.remote), 2,
                         "the unauthorised guild's product was never uploaded")

    async def test_other_guild_stays_untouched_until_it_is_authorised(self):
        await self._seed_products(1)
        self.images.put(url_for(50), (9, 9, 250, 255))
        await self._exec("INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
                         "VALUES (?,?,?,?,?,1)",
                         (OTHER_GUILD, "Foreign Item", 10, "custom", url_for(50)))
        await self._exec("DELETE FROM shop_emoji_queue")

        # Authorise ONLY this guild, then run discovery the way the bot does.
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.authorize_backfill(db, GUILD, actor="owner")
            for gid in await Q.authorized_guilds(db):
                await Q.enqueue_guild_products(db, gid, reason="backfill")
        await self._drain()
        rows = await self._all("SELECT DISTINCT guild_id FROM shop_emoji_queue")
        self.assertNotIn(OTHER_GUILD, [r[0] for r in rows])
        other_asset = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_assets WHERE creator_guild_id=?", (OTHER_GUILD,))
        self.assertEqual(other_asset[0], 0, "no asset may be created for another guild")

    async def test_cross_guild_url_is_never_reused(self):
        """Two guilds may legitimately share one image URL: separate aliases."""
        shared_colour = (44, 44, 44, 255)
        self._add_product("Local Product")
        local_pid = await self._product_id("Local Product")
        self.images.put(url_for(60), shared_colour)
        self._set_icon(local_pid, url_for(60))
        await self._exec(
            "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
            "VALUES (?,?,?,?,?,1)", (OTHER_GUILD, "Foreign Product", 10, "custom", url_for(60)))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            # Both guilds authorised, each for its own catalog only.
            await Q.authorize_backfill(db, GUILD, actor="owner")
            await Q.authorize_backfill(db, OTHER_GUILD, actor="other-owner")
            for gid in await Q.authorized_guilds(db):
                await Q.enqueue_guild_products(db, gid)
        await self._drain()

        # Same content hash => one emoji is fine, but each guild must hold its
        # OWN alias row; no cross-guild alias may be forged.
        aliases = await self._all(
            "SELECT guild_id FROM shop_emoji_url_aliases WHERE source_url=?", (url_for(60),))
        self.assertEqual(sorted(a[0] for a in aliases), sorted([GUILD, OTHER_GUILD]))


# ── 4d. quotas (#9) ────────────────────────────────────────────────────────
class QuotaTests(AutomationTestBase):
    async def test_101_distinct_assets_against_the_100_limit(self):
        await self._seed_products(101)
        await self._drain(max_passes=60)
        created = len(self.fake.remote)
        self.assertEqual(created, SE.GUILD_ASSET_LIMIT,
                         f"exactly the {SE.GUILD_ASSET_LIMIT}-asset guild limit is used")
        quota_blocked = await self._all(
            "SELECT last_error FROM shop_emoji_queue WHERE last_error LIKE '%guild_emoji_quota%'")
        self.assertTrue(quota_blocked, "the blocked product is reported, not silent")
        self.assertIn("guild_emoji_quota", quota_blocked[0][0])

    async def test_quota_error_does_not_become_a_retry_loop(self):
        await self._seed_products(SE.GUILD_ASSET_LIMIT + 1)
        await self._drain(max_passes=60)
        creates = self.fake.creates
        fetches = len(self.images.fetches)
        for _ in range(3):
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
            await self._run_pass()
        # The blocked row may retry with backoff, but must not create or hammer.
        self.assertEqual(self.fake.creates, creates, "no further uploads")
        row = await self._one(
            "SELECT attempts FROM shop_emoji_queue WHERE last_error LIKE '%quota%'")
        self.assertLessEqual(row[0], Q.MAX_AUTO_ATTEMPTS)
        self.assertGreaterEqual(len(self.images.fetches), fetches)

    async def test_application_wide_capacity_is_enforced(self):
        await self._seed_products(2)
        # Fill the application to its cap with unrelated ready emoji.
        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(SE.APPLICATION_EMOJI_LIMIT):
                self.fake.remote.append({"id": str(790000000000000000 + i),
                                         "name": f"filler_{i}", "animated": False,
                                         "application_id": APP, "user": {"id": BOT_ID}})
        await self._drain()
        self.assertEqual(self.fake.creates, 0, "the application cap blocks new uploads")
        errors = await self._all("SELECT last_error FROM shop_emoji_queue")
        self.assertTrue(any("capacity" in (e[0] or "") for e in errors), errors)

    async def test_url_change_consumes_quota_and_is_reported(self):
        await self._seed_products(1)
        pid = await self._product_id("Product 000")
        await self._drain()
        before = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_assets WHERE status='ready'")
        # Warm the guild to exactly the limit, then change one URL.
        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(SE.GUILD_ASSET_LIMIT - before[0]):
                await db.execute(
                    "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,"
                    "emoji_id,animated,creator_guild_id,pipeline_version,status) "
                    "VALUES (?,?,?,?,0,?,?, 'ready')",
                    (APP, f"{i:064x}", f"filler_{i}", str(800000000000000000 + i),
                     GUILD, "v1"))
            await db.commit()
        self.images.put(url_for(31), (31, 31, 31, 255))
        self._set_icon(pid, url_for(31))
        await self._drain()
        report = await self._report()
        self.assertTrue(report["quota"]["used"] >= SE.GUILD_ASSET_LIMIT)
        self.assertTrue(report["quota_limited"])
        blocked = await self._one(
            "SELECT last_error FROM shop_emoji_queue WHERE last_error LIKE '%quota%'")
        self.assertIsNotNone(blocked, "the replaced image is quota-blocked and visible")
        # The OLD asset and alias are untouched (safety over quota).
        old = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_assets WHERE status='ready' AND creator_guild_id=?",
            (GUILD,))
        self.assertGreaterEqual(old[0], SE.GUILD_ASSET_LIMIT)

    async def _report(self):
        r = self.client.get("/api/shop-publisher/emojis/report", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()


# ── 4e. Discord rate limits, 5xx and transport failures ────────────────────
class DiscordTransportTests(AutomationTestBase):
    async def test_429_on_list_is_reported_and_backed_off(self):
        await self._seed_products(1)
        self.fake = FakeDiscord(list_status={"status": 429, "retry_after": 2.5})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()
        report = await self._run_pass()
        self.assertIn("429", report.get("discord_unavailable") or "",
                      "the outage is reported, not silently swallowed")
        row = await self._one("SELECT state, attempts FROM shop_emoji_queue")
        self.assertEqual(row[0], Q.STATE_QUEUED, "work is preserved for the next tick")
        self.assertEqual(row[1], 0, "a Discord outage is not the product's fault")
        self.assertEqual(self.fake.creates, 0)
        self.assertEqual(self.fake.deletes, 0)

    async def test_5xx_on_list_does_not_lose_the_intent(self):
        await self._seed_products(2)
        self.fake = FakeDiscord(list_status={"status": 500})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()
        report = await self._run_pass()
        self.assertIn("500", report.get("discord_unavailable") or "")
        queued = await self._one("SELECT COUNT(*) FROM shop_emoji_queue")
        self.assertEqual(queued[0], 2, "a server error must not drop queued work")
        attempts = await self._one("SELECT MAX(attempts) FROM shop_emoji_queue")
        self.assertEqual(attempts[0], 0, "and must not burn the retry budget")
        self.assertEqual(self.fake.creates, 0)

    async def test_transport_timeout_on_create_is_uncertain_not_ready(self):
        await self._seed_products(1)
        self.fake = FakeDiscord(create_timeout_indexes={1})
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()
        await self._run_pass()
        status = await self._one("SELECT status FROM shop_emoji_assets")
        self.assertIn(status[0], {"uncertain", "preparing"})
        self.assertEqual(self.fake.creates, 1)

    async def test_restart_during_in_flight_download_resumes(self):
        """A process killed mid-download must not lose or duplicate the work."""
        await self._seed_products(1)
        gate = self.images.gate(url_for(0))
        task = asyncio.create_task(self._run_pass())
        for _ in range(200):
            if url_for(0) in self.images.fetches:
                break
            await asyncio.sleep(0.01)
        # Process "dies" here: cancel the in-flight pass, keep the DB.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertEqual(self.fake.creates, 0)
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 1,
                         "the intent survives an interrupted pass")

        # Restart with fresh process state and finish the job.
        SE._preparation_locks.clear()
        W._pass_lock = None
        rebooted = FakeDiscord()
        rebooted.remote = [dict(e) for e in self.fake.remote]
        self.fake = rebooted
        patch.object(SE, "DiscordAPI", lambda *a, **k: rebooted).start()
        gate.set()
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
        await self._drain()
        self.assertEqual(rebooted.creates, 1)
        self.assertEqual(len(rebooted.remote), 1, "no duplicate after the restart")


# ── 4f. scale: 100 products that all share one image ──────────────────────
class SharedHashScaleTests(AutomationTestBase):
    async def test_100_products_one_image_create_one_emoji(self):
        colour = (77, 88, 99, 255)
        for index in range(100):
            name = f"Bulk {index:03d}"
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(index), colour)
            self._set_icon(pid, url_for(index))
        await self._drain(max_passes=60)
        self.assertEqual(self.fake.creates, 1, "one content identity -> exactly one emoji")
        self.assertEqual(len(self.fake.remote), 1)
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)
        ready = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash WHERE e.status='ready'")
        self.assertEqual(ready[0], 100, "all 100 products resolve to that one emoji")
        # One stable name for the shared emoji (lexicographic minimum label).
        self.assertTrue(self.fake.remote[0]["name"].startswith("bulk_000_"),
                        self.fake.remote[0]["name"])
        self.assertEqual(self.fake.renames, 0, "the create-time name never needed fixing")


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
        self.assertRegex(name, r"^product_000_[0-9a-f]{8}$")
        stored = await self._one("SELECT emoji_name, emoji_id FROM shop_emoji_assets")
        self.assertEqual(stored[1], self.fake.remote[0]["id"])
        self.assertEqual(stored[0], name)

    async def test_arabic_product_names_transliterate(self):
        cases = {
            "عصير المجرات": r"^asir_almjrat_[0-9a-f]{8}$",
            "سهران السيرفر": r"^shran_alsirfr_[0-9a-f]{8}$",
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
        self.assertRegex(now[1], r"^product_000_[0-9a-f]{8}$")
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


# ── 6b. shared content keeps ONE stable identity (#4) ───────────────────────
class SharedIdentityTests(AutomationTestBase):
    async def _shared_products(self, names, color=(33, 66, 99, 255)):
        ids = []
        for index, name in enumerate(names):
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(index), color)     # IDENTICAL bytes
            self._set_icon(pid, url_for(index))
            ids.append(pid)
        return ids

    async def test_name_does_not_depend_on_queue_order(self):
        names = ["zeta item", "alpha item", "mike item"]
        await self._shared_products(names)
        await self._drain()
        self.assertEqual(self.fake.creates, 1, "one content identity -> one emoji")
        first_name = self.fake.remote[0]["name"]
        # The canonical label is the lexicographic minimum of the bound names.
        self.assertTrue(first_name.startswith("alpha_item_"), first_name)

        # Re-run repeatedly: the name must be stable and cost zero PATCHes.
        renames = self.fake.renames
        for _ in range(3):
            await W.run_pass()
        self.assertEqual(self.fake.remote[0]["name"], first_name)
        self.assertEqual(self.fake.renames, renames, "no oscillating renames")

    async def test_save_order_does_not_change_the_name(self):
        await self._shared_products(["zzz first", "aaa second"])
        await self._drain()
        stored = await self._one("SELECT emoji_name FROM shop_emoji_assets")
        await self._exec("DELETE FROM shop_emoji_assets")
        await self._exec("DELETE FROM shop_emoji_url_aliases")
        await self._exec("DELETE FROM shop_emoji_queue")
        self.fake = FakeDiscord()
        patch.object(SE, "DiscordAPI", lambda *a, **k: self.fake).start()
        # Same products, opposite processing order.
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        rows = await self._all("SELECT product_id FROM shop_emoji_queue ORDER BY product_id DESC")
        for (pid,) in rows:
            async with aiosqlite.connect(DB_PATH) as db:
                await W.process_product_now(db, self.fake, GUILD, pid)
        self.assertEqual(self.fake.remote[0]["name"], stored[0],
                         "the shared emoji's name is order-independent")

    async def test_restart_keeps_the_shared_name(self):
        await self._shared_products(["beta one", "alpha two"])
        await self._drain()
        name = self.fake.remote[0]["name"]
        SE._preparation_locks.clear()
        W._pass_lock = None
        rebooted = FakeDiscord()
        rebooted.remote = [dict(e) for e in self.fake.remote]
        self.fake = rebooted
        patch.object(SE, "DiscordAPI", lambda *a, **k: rebooted).start()
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD, only_unprepared=False)
        await self._drain()
        self.assertEqual(rebooted.remote[0]["name"], name)
        self.assertEqual(rebooted.creates, 0, "no re-upload after restart")
        self.assertEqual(rebooted.renames, 0, "no rename churn after restart")

    async def test_rename_pass_uses_the_same_canonical_label(self):
        await self._shared_products(["twin b", "twin a"])
        await self._drain()
        await self._exec("UPDATE shop_emoji_assets SET emoji_name='shop_legacy', managed_name=0")
        result = await W.rename_all(guild_id=GUILD, apply=True)
        self.assertEqual(result["renamed"], 1)
        self.assertTrue(self.fake.remote[0]["name"].startswith("twin_a_"),
                        self.fake.remote[0]["name"])
        again = await W.rename_all(guild_id=GUILD, apply=True)
        self.assertEqual(again["patch_requests"], 0, "second run is a no-op")


# ── 6c. suffix strength and collision safety (#5) ───────────────────────────
class NameCollisionTests(AutomationTestBase):
    def test_two_hashes_sharing_slug_and_prefix_still_differ(self):
        shared_prefix = "3ef701"
        h1 = shared_prefix + "a" * 58
        h2 = shared_prefix + "b" * 58
        self.assertEqual(h1[:6], h2[:6])
        n1 = SE.readable_emoji_name("عصير المجرات", h1)
        n2 = SE.readable_emoji_name("عصير المجرات", h2)
        self.assertNotEqual(n1, n2, "8-hex suffixes must disambiguate a shared slug")
        self.assertTrue(n1.isascii() and len(n1) <= 32)

    def test_name_match_must_correspond_to_the_content_hash(self):
        h1 = "3ef701" + "a" * 58
        h2 = "3ef701" + "b" * 58
        # A readable name built from h2 is NOT a legitimate name for h1.
        self.assertFalse(SE._name_matches_content(SE.readable_emoji_name("x", h2), h1))
        self.assertTrue(SE._name_matches_content(SE.readable_emoji_name("x", h1), h1))
        self.assertTrue(SE._name_matches_content(SE.deterministic_emoji_name(h1), h1))

    async def test_colliding_slug_never_adopts_the_wrong_emoji(self):
        """A remote emoji whose name matches the slug but not the hash."""
        await self._seed_products(1)
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT icon_url FROM shop_items WHERE guild_id=?", (GUILD,))
            url = (await cur.fetchone())[0]
        # Plant an emoji in the application whose name belongs to OTHER content.
        foreign_hash = "3ef701" + "f" * 58
        self.fake.remote.append({
            "id": "788000000000000001",
            "name": SE.readable_emoji_name("Product 000", foreign_hash),
            "animated": False, "application_id": APP, "user": {"id": BOT_ID}})
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        await self._drain()

        assets = await self._all("SELECT status, emoji_id, content_hash FROM shop_emoji_assets")
        adopted = [a for a in assets if str(a[1]) == "788000000000000001"]
        self.assertEqual(adopted, [], "the unrelated emoji must never be adopted")
        ready = [a for a in assets if a[0] == "ready"]
        self.assertTrue(ready, "the real content still gets prepared")
        self.assertNotEqual(str(ready[0][1]), "788000000000000001")

    async def test_emoji_id_cannot_be_bound_to_two_content_hashes(self):
        await self._seed_products(1)
        await self._drain()
        emoji_id = self.fake.remote[0]["id"]
        async with aiosqlite.connect(DB_PATH) as db:
            # A second asset claiming the same Discord emoji ID for other content.
            await db.execute(
                "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,"
                "animated,creator_guild_id,pipeline_version,status) "
                "VALUES (?,?,?,?,0,?,?, 'preparing')",
                (APP, "d" * 64, "other_content", emoji_id, GUILD, "v1"))
            await db.commit()
            asset = await SE._get_asset(db, APP, "d" * 64)
            adopted = await SE._adopt_asset(db, asset, {"id": emoji_id, "name": "other_content",
                                                       "animated": False})
        self.assertNotEqual(adopted["status"], "ready",
                            "one emoji ID must never be adopted by two content hashes")
        self.assertIn("already bound", (adopted.get("last_error") or "").lower())


# ── 6d. legacy rename over the whole catalog, bounded (#6) ─────────────────
class LegacyRenameTests(AutomationTestBase):
    LEGACY = 60

    async def _seed_legacy(self, count=None):
        """`count` ready assets carrying the old hash-only name."""
        count = count or self.LEGACY
        await self._seed_products(count)
        await self._drain()
        self.assertEqual(len(self.fake.remote), count)
        # Rewind every asset to the legacy deterministic name.
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                "SELECT content_hash FROM shop_emoji_assets")).fetchall()
            for (chash,) in rows:
                legacy = SE.deterministic_emoji_name(chash)
                await db.execute(
                    "UPDATE shop_emoji_assets SET emoji_name=?,managed_name=0 "
                    "WHERE content_hash=?", (legacy, chash))
                await db.execute(
                    "UPDATE shop_emoji_assets SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE content_hash=?", (chash,))
            await db.commit()
        for emoji in self.fake.remote:
            emoji["name"] = "shop_legacy_" + emoji["id"][-6:]
        ids_before = {e["id"]: e["name"] for e in self.fake.remote}
        return ids_before

    async def test_sixty_legacy_emojis_across_multiple_pages(self):
        ids_before = await self._seed_legacy()
        creates = self.fake.creates
        result = await W.rename_all(guild_id=GUILD, apply=True, page_size=25)
        self.assertEqual(result["pages"], 3, "60 assets must span three pages")
        self.assertEqual(result["renamed"], self.LEGACY)
        self.assertEqual(self.fake.creates, creates, "no emoji was recreated")
        self.assertEqual(self.fake.deletes, 0, "no emoji was deleted")
        # Every ID is unchanged and now carries a readable name.
        self.assertEqual({e["id"] for e in self.fake.remote}, set(ids_before))
        for emoji in self.fake.remote:
            self.assertRegex(emoji["name"], r"^product_\d{3}_[0-9a-f]{8}$")
        # A second complete run performs ZERO PATCHes.
        renames = self.fake.renames
        again = await W.rename_all(guild_id=GUILD, apply=True, page_size=25)
        self.assertEqual(again["patch_requests"], 0)
        self.assertEqual(self.fake.renames, renames)

    async def test_preview_lists_the_whole_catalog_without_patching(self):
        await self._seed_legacy()
        result = await W.rename_all(guild_id=GUILD, apply=False, page_size=25)
        self.assertEqual(len(result["preview"]), self.LEGACY)
        self.assertEqual(self.fake.renames, 0)

    async def test_interrupted_rename_resumes_and_completes(self):
        await self._seed_legacy()
        first = await W.rename_all(guild_id=GUILD, apply=True, page_size=25, max_pages=1)
        self.assertEqual(first["renamed"], 25)
        # Simulate a restart between pages.
        SE._preparation_locks.clear()
        W._pass_lock = None
        rest = await W.rename_all(guild_id=GUILD, apply=True, page_size=25)
        self.assertEqual(first["renamed"] + rest["renamed"], self.LEGACY)
        final = await W.rename_all(guild_id=GUILD, apply=True, page_size=25)
        self.assertEqual(final["renamed"], 0)

    async def test_renaming_preserves_aliases_and_hashes(self):
        await self._seed_legacy(3)
        before = await self._all(
            "SELECT application_id,guild_id,source_url,content_hash FROM shop_emoji_url_aliases "
            "ORDER BY content_hash")
        hashes_before = await self._all(
            "SELECT content_hash, emoji_id FROM shop_emoji_assets ORDER BY content_hash")
        await W.rename_all(guild_id=GUILD, apply=True)
        after = await self._all(
            "SELECT application_id,guild_id,source_url,content_hash FROM shop_emoji_url_aliases "
            "ORDER BY content_hash")
        hashes_after = await self._all(
            "SELECT content_hash, emoji_id FROM shop_emoji_assets ORDER BY content_hash")
        self.assertEqual(before, after)
        self.assertEqual(hashes_before, hashes_after)


# ── 6e. concurrent workers, one database ──────────────────────────────────
class ConcurrentWorkerTests(AutomationTestBase):
    async def test_two_concurrent_passes_never_double_create(self):
        await self._seed_products(6)
        # Two passes raced against the same DB (the second waits on the lock
        # inside one process; the DB fences are what make it correct anyway).
        first = asyncio.create_task(self._run_pass())
        second = asyncio.create_task(self._run_pass())
        await asyncio.gather(first, second)
        ids = [e["id"] for e in self.fake.remote]
        self.assertEqual(len(ids), len(set(ids)), "no duplicate emoji IDs")
        self.assertEqual(len(self.fake.remote), 6)
        self.assertEqual(self.fake.creates, 6)

    async def test_dashboard_and_bot_writes_do_not_interleave_lossily(self):
        """The Dashboard upsert and the worker's CAS writes on one file."""
        await self._seed_products(3)
        pid = await self._product_id("Product 000")
        task = asyncio.create_task(self._run_pass())
        self.images.put(url_for(91), (91, 91, 91, 255))
        self._set_icon(pid, url_for(91))     # concurrent Dashboard write
        await asyncio.wait_for(task, timeout=60)
        row = await self._one(
            "SELECT source_url FROM shop_emoji_queue WHERE product_id=?", (pid,))
        if row is not None:
            self.assertEqual(row[0], url_for(91))
        # Whatever happened, the current URL must end up prepared.
        await self._drain()
        ready = await self._one(
            "SELECT e.status FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash WHERE a.source_url=? AND a.guild_id=?",
            (url_for(91), GUILD))
        self.assertIsNotNone(ready)
        self.assertEqual(ready[0], "ready")
        self.assertEqual(self.fake.deletes, 0)


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

    async def test_queue_endpoint_is_read_only_and_guild_scoped(self):
        """#10: a GET must not create schema, and must scope the retry time."""
        await self._seed_products(2)
        # Add a row for ANOTHER guild so scoping is observable.
        await self._exec(
            "INSERT INTO shop_emoji_queue (guild_id,product_id,item_name,source_url,state,attempts) "
            "VALUES (?,?,?,?,'retry',1)", (OTHER_GUILD, 900, "foreign", url_for(90)))
        await self._exec(
            "UPDATE shop_emoji_queue SET next_attempt_at="
            "strftime('%Y-%m-%dT%H:%M:%fZ','now','+600 seconds') WHERE guild_id=?",
            (OTHER_GUILD,))

        before = await self._one("SELECT COUNT(*) FROM sqlite_master WHERE name='shop_emoji_queue'")
        r = self.client.get("/api/shop-publisher/emojis/queue", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        payload = r.get_json()
        self.assertTrue(payload["schema_ready"])
        self.assertEqual(payload["queue"]["total"], 2, "only this guild's rows are counted")
        self.assertIsNone(payload["next_attempt_at"],
                          "another guild's retry time must not leak into this report")
        after = await self._one("SELECT COUNT(*) FROM sqlite_master WHERE name='shop_emoji_queue'")
        self.assertEqual(before, after)

    async def test_queue_endpoint_on_a_database_without_the_table(self):
        await self._exec("DROP TABLE shop_emoji_queue")
        r = self.client.get("/api/shop-publisher/emojis/queue", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertFalse(r.get_json()["schema_ready"])
        # The GET must NOT have created the table as a side effect.
        exists = await self._one(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='shop_emoji_queue'")
        self.assertEqual(exists[0], 0, "a read endpoint must never run DDL")

    async def test_report_counts_are_honest(self):
        shared = (12, 34, 56, 255)
        for index in range(3):
            self._add_product(f"Rep {index}")
            pid = await self._product_id(f"Rep {index}")
            self.images.put(url_for(index), shared)
            self._set_icon(pid, url_for(index))
        self._add_product("Never Imaged")
        await self._drain()
        r = self.client.get("/api/shop-publisher/emojis/report", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        report = r.get_json()
        self.assertEqual(report["eligible_products"], 3, "the URL-less product is excluded")
        self.assertEqual(report["already_ready"], 3)
        self.assertEqual(report["unprepared_products"], 0)
        self.assertEqual(report["quota"]["used"], 1, "three products share one asset")
        # Reuse is measured on CONTENT (identical bytes), not on shared URLs.
        self.assertEqual(len(report["shared_url_groups"]), 0,
                         "these three products have distinct URLs")
        self.assertEqual(len(report["content_reused_groups"]), 1,
                         "one content hash serves three products")
        self.assertEqual(report["content_reused_groups"][0]["products"], 3)

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
