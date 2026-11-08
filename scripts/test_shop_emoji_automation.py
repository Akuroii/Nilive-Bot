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
        self.patch_requests = 0
        self.patched_emoji_ids = []
        self.deletes = 0
        self.lists = 0
        self.list_calls = []
        self.list_gates = {}
        self.create_failures = set(create_failures or ())
        self.create_timeout_indexes = set(create_timeout_indexes or ())
        self.rename_failures = dict(rename_failures or {})
        # Indexes of creates whose 2xx response omits application_id/user — the
        # "ambiguous reply" hypothesis. Not a claim about what Discord really
        # sent in production; the historical body was never captured.
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
        self.list_calls.append(self.lists)
        gate = self.list_gates.get(self.lists)
        if gate is not None:
            # Block INSIDE the Nth list call. Used to prove the validation that
            # precedes a create is still re-checked at the actual POST boundary:
            # by the time this returns, the product may have changed.
            await gate.wait()
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
        self.patch_requests += 1
        self.patched_emoji_ids.append(str(emoji_id))
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
            requeued = (await Q.enqueue_guild_products(db, GUILD, reason="restart"))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD))["queued"]
        self.assertEqual(queued, 0)

    async def test_guild_isolation(self):
        await self._seed_products(3)
        self.images.put(url_for(50), (5, 250, 5, 255))
        await self._exec("INSERT INTO shop_items (guild_id,name,price,type,icon_url) "
                         "VALUES (?,?,?,?,?)",
                         (OTHER_GUILD, "Other Guild Item", 10, "custom", url_for(50)))
        async with aiosqlite.connect(DB_PATH) as db:
            queued = (await Q.enqueue_guild_products(db, OTHER_GUILD))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD, application_id=APP))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD, application_id=APP))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD, application_id=APP))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD, application_id=APP))["queued"]
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
            queued = (await Q.enqueue_guild_products(db, GUILD, application_id=APP))["queued"]
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


# ── 4a2. audit: a stale download must never CREATE (external side effect) ──
class StaleCreationTests(AutomationTestBase):
    """An obsolete worker must not POST an emoji for an image that is no longer
    the product's current image.

    Queue fencing alone only protects the ROW. The emoji is an external, durable
    side effect that is never deleted and keeps consuming quota, so the fence
    has to reach the point immediately before the POST.
    """

    async def _start_blocked_pass(self, url):
        gate = self.images.gate(url)
        task = asyncio.create_task(self._run_pass())
        for _ in range(400):
            if url in self.images.fetches:
                return gate, task
            await asyncio.sleep(0.01)
        self.fail(f"the download of {url} never started")

    async def test_url_changed_mid_download_never_creates_the_old_image(self):
        self._add_product("Swapped Product")
        pid = await self._product_id("Swapped Product")
        self.images.put(url_for(0), (10, 10, 10, 255))
        self._set_icon(pid, url_for(0))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)

        gate, task = await self._start_blocked_pass(url_for(0))
        # Real Dashboard save while the download is blocked.
        self.images.put(url_for(1), (200, 20, 20, 255))
        self._set_icon(pid, url_for(1))
        gate.set()
        report = await asyncio.wait_for(task, timeout=90)

        self.assertEqual(self.fake.creates, 0,
                         "URL A must never reach Discord once B is the current image")
        self.assertEqual(len(self.fake.remote), 0, "no obsolete emoji exists")
        stale = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_url_aliases WHERE guild_id=? AND source_url=?",
            (GUILD, url_for(0)))
        self.assertEqual(stale[0], 0, "no ready alias may be created for the stale URL")
        # B survives and is genuinely prepared afterwards.
        row = await self._one(
            "SELECT state,source_url FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (GUILD, pid))
        self.assertIsNotNone(row, "B intent must survive")
        self.assertEqual(row[1], url_for(1))
        await self._drain()
        self.assertEqual(self.fake.creates, 1, "only the current image is uploaded")
        ready = await self._one(
            "SELECT e.status FROM shop_emoji_url_aliases a JOIN shop_emoji_assets e "
            "ON e.application_id=a.application_id AND e.content_hash=a.content_hash "
            "WHERE a.guild_id=? AND a.source_url=?", (GUILD, url_for(1)))
        self.assertIsNotNone(ready)
        self.assertEqual(ready[0], "ready")

    async def test_url_cleared_mid_download_never_creates_the_old_image(self):
        self._add_product("Cleared Product")
        pid = await self._product_id("Cleared Product")
        self.images.put(url_for(2), (11, 22, 33, 255))
        self._set_icon(pid, url_for(2))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)

        gate, task = await self._start_blocked_pass(url_for(2))
        self._set_icon(pid, "")        # real save with the image removed
        gate.set()
        await asyncio.wait_for(task, timeout=90)

        self.assertEqual(self.fake.creates, 0,
                         "a cleared image must not be uploaded")
        self.assertEqual(len(self.fake.remote), 0)
        self.assertEqual(self.fake.deletes, 0)
        self.assertIsNone(await self._queue_state(pid),
                          "the intent is void, not resurrected")

    async def test_stale_work_consumes_no_quota(self):
        self._add_product("Quota Product")
        pid = await self._product_id("Quota Product")
        self.images.put(url_for(3), (40, 50, 60, 255))
        self._set_icon(pid, url_for(3))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)

        gate, task = await self._start_blocked_pass(url_for(3))
        self.images.put(url_for(4), (61, 71, 81, 255))
        self._set_icon(pid, url_for(4))
        gate.set()
        await asyncio.wait_for(task, timeout=90)

        # Not one asset row may exist for the abandoned image: those rows are
        # exactly what consume the 100-per-guild allowance.
        assets = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_assets WHERE status IN "
            "('ready','preparing','uncertain')")
        self.assertEqual(assets[0], 0, "stale work must not occupy quota")
        await self._drain()
        self.assertEqual((await self._one(
            "SELECT COUNT(*) FROM shop_emoji_assets WHERE status IN "
            "('ready','preparing','uncertain')"))[0], 1, "only the current image")


# ── 4a3. audit: stale queue deletions must not destroy newer intent ────────
class StaleQueueDeletionTests(AutomationTestBase):
    """`clear_product()` deletes by (guild, product) with no generation fence.

    A worker holding an OLDER listing snapshot would then destroy a newer queue
    row the Dashboard has just written, losing that intent entirely.
    """

    async def _row_dict(self, pid):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"SELECT {','.join(Q.QUEUE_FIELDS)} FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, pid))
            fetched = await cur.fetchone()
            return Q._row(fetched) if fetched else None

    async def _resume_stale_worker(self, old_row):
        """Run `_process_row` with the snapshot a previous pass would hold."""
        async with aiosqlite.connect(DB_PATH) as db:
            return await W._process_row(
                db, self.fake, old_row, list(self.fake.remote), BOT_ID,
                W._empty_report())

    async def test_disabled_listing_does_not_destroy_a_newer_generation(self):
        self._add_product("Disabling Product")
        pid = await self._product_id("Disabling Product")
        self.images.put(url_for(5), (5, 5, 5, 255))
        self._set_icon(pid, url_for(5))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)
        old_generation = old_row["generation"]

        # A newer intent arrives while the old worker is mid-flight.
        self.images.put(url_for(6), (6, 6, 6, 255))
        self._set_icon(pid, url_for(6))
        newer = await self._row_dict(pid)
        self.assertEqual(newer["source_url"], url_for(6))
        self.assertGreater(newer["generation"], old_generation)

        # The stale worker now observes a DISABLED listing (its old snapshot).
        await self._exec("UPDATE shop_items SET enabled=0 WHERE id=?", (pid,))
        await self._resume_stale_worker(old_row)

        survivor = await self._row_dict(pid)
        self.assertIsNotNone(
            survivor,
            "a stale worker must never delete the queue row for a NEWER generation")
        self.assertEqual(survivor["generation"], newer["generation"],
                         "the newer generation must be byte-identical")
        self.assertEqual(survivor["source_url"], url_for(6))

    async def test_url_less_listing_does_not_destroy_a_newer_generation(self):
        self._add_product("Blank Product")
        pid = await self._product_id("Blank Product")
        self.images.put(url_for(7), (7, 7, 7, 255))
        self._set_icon(pid, url_for(7))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)

        self.images.put(url_for(8), (8, 8, 8, 255))
        self._set_icon(pid, url_for(8))
        newer = await self._row_dict(pid)

        # The stale worker sees the image as absent (its own older snapshot).
        await self._exec("UPDATE shop_items SET icon_url='' WHERE id=?", (pid,))
        await self._resume_stale_worker(old_row)

        survivor = await self._row_dict(pid)
        self.assertIsNotNone(survivor, "the newer intent must survive")
        self.assertEqual(survivor["generation"], newer["generation"])
        self.assertEqual(survivor["source_url"], url_for(8))

    async def test_deleted_listing_completes_only_its_own_generation(self):
        """The listing is REALLY deleted here, so the branch is actually entered.

        The queue row is left in place (a deleted listing does not delete it),
        which is what lets the worker reach the `listing_gone` branch. The
        scenario the branch must handle is: this generation is still current, so
        completing it is correct — and the completion must be the CAS, so a row
        written afterwards survives.
        """
        self._add_product("Vanishing Product")
        pid = await self._product_id("Vanishing Product")
        self.images.put(url_for(9), (9, 9, 9, 255))
        self._set_icon(pid, url_for(9))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)
        self.assertEqual(old_row["source_url"], url_for(9))

        # Delete the listing. There is deliberately no FK cascade from
        # shop_emoji_queue to shop_items, so the intent row survives — which is
        # precisely the state the branch exists to clean up.
        await self._exec("DELETE FROM shop_items WHERE id=?", (pid,))
        self.assertIsNone(await self._one(
            "SELECT id FROM shop_items WHERE id=?", (pid,)),
            "the listing must genuinely be gone for this test to mean anything")
        self.assertIsNotNone(await self._row_dict(pid),
                             "the queue row survives the listing deletion")

        status = await self._resume_stale_worker(old_row)
        self.assertEqual(status, "listing_gone",
                         "the deleted-listing branch must be the one that ran")
        self.assertIsNone(await self._row_dict(pid),
                          "a current generation of a deleted listing is void")

    async def test_deleted_listing_cannot_destroy_a_newer_generation(self):
        """Same branch, but a newer intent exists: the CAS must refuse."""
        self._add_product("Vanishing Newer")
        pid = await self._product_id("Vanishing Newer")
        self.images.put(url_for(19), (19, 19, 19, 255))
        self._set_icon(pid, url_for(19))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)

        # A newer generation is written directly, as the Dashboard would.
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_product(db, GUILD, pid, "Vanishing Newer",
                                    url_for(20), reason="product_saved")
        newer = await self._row_dict(pid)
        self.assertGreater(newer["generation"], old_row["generation"])

        await self._exec("DELETE FROM shop_items WHERE id=?", (pid,))
        status = await self._resume_stale_worker(old_row)
        self.assertEqual(status, "stale_listing_gone",
                         "the stale generation must be reported, not acted on")
        survivor = await self._row_dict(pid)
        self.assertIsNotNone(survivor,
                             "a stale worker must never delete a newer generation")
        self.assertEqual(survivor["generation"], newer["generation"])
        self.assertEqual(survivor["source_url"], url_for(20))

    async def test_reactivate_cannot_reset_a_newer_intent(self):
        """An old Backfill snapshot must not revive/reset a newer row."""
        self._add_product("Reactivate Product")
        pid = await self._product_id("Reactivate Product")
        self.images.put(url_for(10), (10, 10, 10, 255))
        self._set_icon(pid, url_for(10))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        stale = await self._row_dict(pid)

        # A newer URL arrives after the Backfill snapshot was read.
        self.images.put(url_for(11), (11, 11, 11, 255))
        self._set_icon(pid, url_for(11))
        newer = await self._row_dict(pid)

        # The stale Backfill's explicit reactivation must not touch it.
        async with aiosqlite.connect(DB_PATH) as db:
            changed = await Q.reactivate(db, GUILD, pid,
                                         expect_generation=stale["generation"],
                                         expect_source_url=stale["source_url"])
        self.assertFalse(changed, "a stale reactivation must be refused")
        after = await self._row_dict(pid)
        self.assertEqual(after["generation"], newer["generation"])
        self.assertEqual(after["source_url"], url_for(11))
        self.assertEqual(after["attempts"], newer["attempts"])


# ── 4a4. audit: the fence cannot be bypassed via the uncertain/remap paths ─
class FenceCoverageTests(AutomationTestBase):
    """Every path that can reach Discord must pass through the fence."""

    async def test_all_creation_sites_are_fenced(self):
        """Structural proof that no creation call can bypass validate_source."""
        source = (ROOT / "utils/shop_emoji.py").read_text(encoding="utf-8")
        fences = source.count("await _guard_source(validate_source)")
        self.assertGreaterEqual(
            fences, 3,
            "the fence must guard the fresh-create, uncertain-recovery and "
            "changed-source remap paths")
        # The fresh-create site specifically: a fence must appear between the
        # download and _create_claimed in the main path.
        main = source[source.index("async def _prepare_one"):]
        download_at = main.index("final_png = await _download_and_normalize(source_url)")
        create_at = main.index("return await _create_claimed(db, discord, claimed")
        fence_at = main.index("await _guard_source(validate_source)")
        self.assertLess(download_at, fence_at,
                        "the fence must run AFTER the download")
        self.assertLess(fence_at, create_at,
                        "the fence must run BEFORE the Discord creation call")

    async def test_worker_supplies_a_fence_to_prepare_one(self):
        source = (ROOT / "utils/shop_emoji_worker.py").read_text(encoding="utf-8")
        self.assertIn("validate_source=lambda: _source_still_current(db, row)", source,
                      "the worker must pass an authoritative staleness fence")
        self.assertIn("async def _source_still_current", source)

    async def test_fence_rejects_a_disabled_listing(self):
        """The fence is authoritative about the LISTING, not only the generation."""
        self._add_product("Fence Disabled")
        pid = await self._product_id("Fence Disabled")
        self.images.put(url_for(150), (1, 1, 9, 255))
        self._set_icon(pid, url_for(150))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
            row = Q._row(await (await db.execute(
                f"SELECT {','.join(Q.QUEUE_FIELDS)} FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, pid))).fetchone())
            await self._exec("UPDATE shop_items SET enabled=0 WHERE id=?", (pid,))
            self.assertFalse(await W._source_still_current(db, row),
                             "a disabled listing must never be created")

    async def test_fence_rejects_a_listing_edit_that_has_not_yet_queued(self):
        """An edit that has not produced a queue row is still a change."""
        self._add_product("Fence Pending Edit")
        pid = await self._product_id("Fence Pending Edit")
        self.images.put(url_for(151), (2, 3, 4, 255))
        self._set_icon(pid, url_for(151))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
            row = Q._row(await (await db.execute(
                f"SELECT {','.join(Q.QUEUE_FIELDS)} FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, pid))).fetchone())
            # Change the listing WITHOUT going through the enqueue hook.
            await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?",
                             (url_for(152), pid))
            self.assertFalse(await W._source_still_current(db, row),
                             "the listing must be re-read, not trusted")

    async def test_fence_accepts_an_unchanged_listing(self):
        self._add_product("Fence Unchanged")
        pid = await self._product_id("Fence Unchanged")
        self.images.put(url_for(153), (5, 6, 7, 255))
        self._set_icon(pid, url_for(153))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
            row = Q._row(await (await db.execute(
                f"SELECT {','.join(Q.QUEUE_FIELDS)} FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, pid))).fetchone())
            self.assertTrue(await W._source_still_current(db, row),
                            "normal work must still be allowed")


# ── 4a5. audit round 4: the fence must be at the ACTUAL POST boundary ──────
class PostBoundaryFenceTests(AutomationTestBase):
    """A fence that runs before `_create_claimed()` is not at the POST.

    `_create_claimed()` awaits `list_emojis()` and `_reconcile_asset()` before
    calling `create_emoji()`. A product can change inside that window, after the
    pre-create validation has already passed. These tests block the LIST call
    that happens inside `_create_claimed()` — i.e. after the download fence has
    succeeded — to prove the boundary itself is guarded.
    """

    async def _run_to_blocked_list(self, url, *, pass_index=1):
        """Start a pass and block the Nth list_emojis call of that pass."""
        gate = asyncio.Event()
        self.fake.list_gates[pass_index] = gate
        task = asyncio.create_task(self._run_pass())
        for _ in range(600):
            if self.fake.lists >= pass_index:
                return gate, task
            await asyncio.sleep(0.01)
        self.fail(f"list_emojis call #{pass_index} never happened")

    async def test_url_changed_during_the_final_list_never_posts_the_old_image(self):
        self._add_product("Boundary Product")
        pid = await self._product_id("Boundary Product")
        self.images.put(url_for(0), (12, 24, 36, 255))
        self._set_icon(pid, url_for(0))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)

        # Block the LAST list call before the create. In this flow the pass
        # performs: verify/list for reconciliation, then _create_claimed's own
        # list. Blocking by index keeps the test deterministic.
        gate, task = await self._run_to_blocked_list(url_for(0), pass_index=self.fake.lists + 1)
        # While the list is in flight, the product really changes.
        self.images.put(url_for(1), (200, 10, 10, 255))
        self._set_icon(pid, url_for(1))
        gate.set()
        await asyncio.wait_for(task, timeout=90)

        self.assertEqual(self.fake.creates, 0,
                         "the POST boundary must revalidate: URL A must not be created")
        stale_asset = await self._one(
            "SELECT COUNT(*) FROM shop_emoji_url_aliases WHERE guild_id=? AND source_url=?",
            (GUILD, url_for(0)))
        self.assertEqual(stale_asset[0], 0, "no alias may exist for the abandoned image")

    async def test_cleared_image_during_the_final_list_never_posts(self):
        self._add_product("Boundary Cleared")
        pid = await self._product_id("Boundary Cleared")
        self.images.put(url_for(2), (3, 6, 9, 255))
        self._set_icon(pid, url_for(2))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)

        gate, task = await self._run_to_blocked_list(url_for(2), pass_index=self.fake.lists + 1)
        self._set_icon(pid, "")
        gate.set()
        await asyncio.wait_for(task, timeout=90)
        self.assertEqual(self.fake.creates, 0,
                         "a cleared image must not be POSTed at the boundary")

    async def test_the_validator_is_invoked_immediately_before_create_emoji(self):
        """Behavioural proof, not a source-string count.

        The fake records the ORDER of list/create/validate events, so we can
        assert that a validation happened after the final list and before the
        POST rather than trusting a grep.
        """
        self._add_product("Boundary Order")
        pid = await self._product_id("Boundary Order")
        self.images.put(url_for(3), (7, 14, 21, 255))
        self._set_icon(pid, url_for(3))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)

        events = []
        real_create = self.fake.create_emoji
        real_list = self.fake.list_emojis

        async def create_spy(name, png, bot_user_id):
            events.append("create")
            return await real_create(name, png, bot_user_id)

        async def list_spy():
            events.append("list")
            return await real_list()

        self.fake.create_emoji = create_spy
        self.fake.list_emojis = list_spy
        # Count validations by wrapping the worker's fence.
        real_fence = W._source_still_current
        validations = []

        async def fence_spy(db, row):
            validations.append(list(events))
            return await real_fence(db, row)

        with patch.object(W, "_source_still_current", fence_spy):
            await self._run_pass()

        self.assertEqual(self.fake.creates, 1, "the normal path still creates")
        self.assertIn("create", events)
        create_at = events.index("create")
        last_list_before = max(i for i, e in enumerate(events[:create_at]) if e == "list")
        # Some validation must be recorded after that final list.
        self.assertTrue(
            any(len(snapshot) > last_list_before and "create" not in snapshot
                for snapshot in validations),
            f"no validation ran after the final list and before the POST: "
            f"events={events} validations={validations}")


# ── 4a6. audit round 4: stale requeue must not overwrite newer intent ──────
class StaleRequeueTests(AutomationTestBase):
    """The requeue branch wrote from an old listing snapshot, unconditionally."""

    async def _row_dict(self, pid):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"SELECT {','.join(Q.QUEUE_FIELDS)} FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, pid))
            fetched = await cur.fetchone()
            return Q._row(fetched) if fetched else None

    async def test_requeue_from_a_stale_snapshot_does_not_overwrite_a_newer_url(self):
        self._add_product("Requeue Product")
        pid = await self._product_id("Requeue Product")
        self.images.put(url_for(10), (10, 1, 1, 255))
        self._set_icon(pid, url_for(10))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)          # generation for URL 10

        # The listing moves to URL 11, then to URL 12 (newest intent).
        self.images.put(url_for(11), (11, 1, 1, 255))
        self.images.put(url_for(12), (12, 1, 1, 255))
        self._set_icon(pid, url_for(11))
        self._set_icon(pid, url_for(12))
        newest = await self._row_dict(pid)
        self.assertEqual(newest["source_url"], url_for(12))

        # A stale worker resumes believing the listing is still URL 11.
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (url_for(11), pid))
        async with aiosqlite.connect(DB_PATH) as db:
            await W._process_row(db, self.fake, old_row, list(self.fake.remote),
                                 BOT_ID, W._empty_report())

        survivor = await self._row_dict(pid)
        self.assertIsNotNone(survivor, "newer intent must not be destroyed")
        self.assertEqual(survivor["generation"], newest["generation"],
                         "a stale requeue must never bump or replace a newer generation")
        self.assertEqual(survivor["source_url"], newest["source_url"],
                         "the newest URL must survive, not be overwritten by a stale one")

    async def test_requeue_is_refused_when_the_listing_moved_on_again(self):
        """The listing itself changed after the snapshot was taken."""
        self._add_product("Requeue Moved")
        pid = await self._product_id("Requeue Moved")
        self.images.put(url_for(13), (13, 2, 2, 255))
        self._set_icon(pid, url_for(13))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)

        self.images.put(url_for(14), (14, 2, 2, 255))
        self._set_icon(pid, url_for(14))
        newest = await self._row_dict(pid)

        # Between the worker's read and its write, the listing changes again.
        self.images.put(url_for(15), (15, 2, 2, 255))
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (url_for(15), pid))
        async with aiosqlite.connect(DB_PATH) as db:
            listing = await W._load_listing(db, GUILD, pid)
            _i, _g, name, icon, enabled = listing
            wrote = await Q.requeue_if_current(
                db, old_row, name, Q.queue_source_url(icon))
        self.assertFalse(wrote, "a requeue must verify the listing still matches")

    async def test_the_current_url_is_still_prepared_after_a_stale_requeue(self):
        """Requirement 2, third half: refusing the write must not lose the work.

        A lost requeue is only correct if the surviving intent still gets
        prepared. This drains the real worker and asserts that exactly the
        CURRENT image was uploaded — never the URL the stale worker proposed.
        """
        self._add_product("Requeue Converges")
        pid = await self._product_id("Requeue Converges")
        self.images.put(url_for(20), (20, 5, 5, 255))
        self._set_icon(pid, url_for(20))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        old_row = await self._row_dict(pid)          # generation for URL 20

        # The listing really moves to URL 21, which queues its own generation.
        self.images.put(url_for(21), (21, 5, 5, 255))
        self._set_icon(pid, url_for(21))
        newest = await self._row_dict(pid)

        # The stale worker proposes URL 21 from a snapshot it read too early:
        # its write must be refused because its own generation is gone.
        async with aiosqlite.connect(DB_PATH) as db:
            wrote = await Q.requeue_if_current(db, old_row, "Requeue Converges",
                                               url_for(21))
        self.assertFalse(wrote, "a superseded generation must not write")

        await self._drain()
        survivor = await self._row_dict(pid)
        self.assertIsNone(survivor, "the current intent must be completed, not stranded")
        alias = await self._all(
            "SELECT source_url FROM shop_emoji_url_aliases WHERE guild_id=?", (GUILD,))
        self.assertIn((url_for(21),), alias,
                      "the current URL must end up prepared")
        self.assertNotIn((url_for(20),), alias,
                         "the superseded URL must never be prepared")
        self.assertEqual(self.fake.creates, 1,
                         "exactly one emoji may be created, for the current image")

    async def test_backfill_snapshot_cannot_overwrite_newer_intent(self):
        self._add_product("Backfill Snapshot")
        pid = await self._product_id("Backfill Snapshot")
        self.images.put(url_for(16), (16, 3, 3, 255))
        self._set_icon(pid, url_for(16))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        # A newer URL arrives before Backfill writes its snapshot.
        self.images.put(url_for(17), (17, 3, 3, 255))
        self._set_icon(pid, url_for(17))
        newest = await self._row_dict(pid)
        before = dict(newest)
        # Backfill re-runs: it must not clobber the newer intent.
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        after = await self._row_dict(pid)
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(after["source_url"], before["source_url"])


# ── 4a7. audit round 4: reactivation must match its documented contract ───
class RetryStateContractTests(AutomationTestBase):
    """/emojis/retry is documented as reactivating PARKED rows.

    It used to reset any existing row: a `queued` row was bumped to a new
    generation (invalidating a worker mid-flight) and a `retry` row lost its
    attempts, error and backoff timer. Each state is exercised through the real
    API, not by calling reactivate() directly.
    """

    async def _queue(self, name, index):
        self._add_product(name)
        pid = await self._product_id(name)
        self.images.put(url_for(index), (index, 9, 9, 255))
        self._set_icon(pid, url_for(index))
        return pid

    async def _row(self, pid):
        return await self._one(
            "SELECT state,generation,attempts,next_attempt_at,last_error "
            "FROM shop_emoji_queue WHERE guild_id=? AND product_id=?", (GUILD, pid))

    async def _retry(self, pid):
        r = self.client.post("/api/shop-publisher/emojis/retry",
                             json={"product_ids": [pid]},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    async def test_a_parked_row_is_reactivated(self):
        pid = await self._queue("Retry Parked", 0)
        await self._exec(
            "UPDATE shop_emoji_queue SET state='parked', attempts=4, "
            "last_error='dead', next_attempt_at=NULL WHERE guild_id=? AND product_id=?",
            (GUILD, pid))
        body = await self._retry(pid)
        self.assertEqual(body["reactivated"], 1)
        row = await self._row(pid)
        self.assertEqual(row[0], "queued", "parked -> queued")
        self.assertEqual(row[1], 2, "a new generation fences stale work")
        self.assertEqual(row[2], 0, "the retry budget is reset")
        self.assertIsNone(row[4])

    async def test_a_queued_row_is_left_alone(self):
        pid = await self._queue("Retry Queued", 1)
        before = await self._row(pid)
        self.assertEqual(before[0], "queued")
        body = await self._retry(pid)
        self.assertEqual(body["reactivated"], 0,
                         "an already-queued row is not a parked row")
        after = await self._row(pid)
        self.assertEqual(tuple(after), tuple(before),
                         "a queued row must stay byte-identical: its generation "
                         "must not be bumped under a worker that is processing it")

    async def test_a_retry_row_keeps_its_backoff(self):
        pid = await self._queue("Retry Backoff", 2)
        await self._exec(
            "UPDATE shop_emoji_queue SET state='retry', attempts=2, "
            "last_error='source_http_error: 404', "
            "next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+900 seconds') "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        before = await self._row(pid)
        body = await self._retry(pid)
        self.assertEqual(body["reactivated"], 0,
                         "a row in backoff is not a parked row")
        after = await self._row(pid)
        self.assertEqual(tuple(after), tuple(before),
                         "attempts, error and next_attempt_at must all survive")
        self.assertEqual(after[0], "retry")

    async def test_a_missing_row_is_queued_fresh(self):
        self._add_product("Retry Missing")
        pid = await self._product_id("Retry Missing")
        self.images.put(url_for(3), (3, 9, 9, 255))
        self._set_icon(pid, url_for(3))
        await self._exec("DELETE FROM shop_emoji_queue")
        body = await self._retry(pid)
        self.assertEqual(body["reactivated"], 1, "a never-queued product is queued")
        row = await self._row(pid)
        self.assertEqual(row[0], "queued")

    async def test_parked_path_in_backfill_still_reactivates(self):
        """The Backfill `retry_parked` switch must keep working."""
        pid = await self._queue("Retry Backfill", 4)
        await self._exec(
            "UPDATE shop_emoji_queue SET state='parked', attempts=4 "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        r = self.client.post("/api/shop-publisher/emojis/backfill",
                             json={"retry_parked": True},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["reactivated"], 1)


# ── 4b. generation fencing ──────────────────────────────────────────────────# ── 4b. generation fencing ──────────────────────────────────────────────────
class ProductionUncertainCreateTests(AutomationTestBase):
    """The historical production outcome: ``عصير المجرات: uncertain`` /
    "Discord's create response did not confirm the expected emoji."

    WHAT IS KNOWN (observed, not inferred):
      * production stored the asset as ``uncertain`` with no ``emoji_id``;
      * the recorded message was "Discord's create response did not confirm the
        expected emoji.";
      * the product was in Arabic, and no readable name was produced.

    WHAT THIS DOUBLE SIMULATES (a hypothesis about the cause, deliberately
    narrower than the observed outcome):
      * Discord's POST reply was a 2xx body whose ``application_id``/``user``
        keys were absent, so ``_ownership_supported()`` could not accept it.

    WHAT DISCORD DOCUMENTS: Create Guild/Application Emoji returns the emoji
    object (id, name, animated, and — for application emojis — the owning
    ``user``/``application_id`` metadata). List Application Emojis returns
    ``{items: [...]}`` of the same object shape. Both the create body and the
    list entry are therefore expected to carry ownership metadata, which is why
    a MISSING field is treated as "ambiguous", never as "not ours".

    WHAT IS NOT CLAIMED: that the fake reproduces the exact historical response.
    The real historical body was never captured. The test proves the SAFETY
    BEHAVIOUR for that class of response — reconcile read-only, adopt only with
    ownership proof, and never POST a second time.
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


# ── 4g. review round 2, item 2: retry_parked must actually reactivate ─────
class BackfillReactivationTests(AutomationTestBase):
    async def _park(self, product_id, url):
        """Drive a product to state='parked' the way the worker really does."""
        self.images.kill(url)
        for _ in range(Q.MAX_AUTO_ATTEMPTS + 3):
            await self._run_pass()
            row = await self._one(
                "SELECT state FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
                (GUILD, product_id))
            if row and row[0] == Q.STATE_PARKED:
                break
            # Clear the backoff so the next attempt actually happens.
            await self._exec(
                "UPDATE shop_emoji_queue SET next_attempt_at=NULL "
                "WHERE guild_id=? AND product_id=?", (GUILD, product_id))
        row = await self._one(
            "SELECT state, attempts, last_error FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, product_id))
        self.assertEqual(row[0], Q.STATE_PARKED, f"expected parked, got {row}")
        return row

    async def test_same_url_stays_parked_during_ordinary_discovery(self):
        self._add_product("Parked Same")
        pid = await self._product_id("Parked Same")
        self.images.put(url_for(70), (70, 70, 70, 255))
        self._set_icon(pid, url_for(70))
        await self._park(pid, url_for(70))
        before = await self._one(
            "SELECT state, attempts, generation FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))

        # Ordinary discovery (no retry_parked) must not resurrect it.
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.authorize_backfill(db, GUILD, actor="owner")
            result = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(result["queued"], 0, "a parked product is not new work")
        self.assertEqual(result["reactivated"], 0, "discovery revives nothing")
        after = await self._one(
            "SELECT state, attempts, generation FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(tuple(after), tuple(before),
                         "discovery must leave a parked row byte-identical")

    async def test_owner_backfill_with_retry_parked_reactivates_and_completes(self):
        self._add_product("Parked Retry")
        pid = await self._product_id("Parked Retry")
        self.images.put(url_for(71), (71, 71, 71, 255))
        self._set_icon(pid, url_for(71))
        parked = await self._park(pid, url_for(71))
        attempts_before = parked[1]
        self.assertGreater(attempts_before, 0)
        gen_before = (await self._one(
            "SELECT generation FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (GUILD, pid)))[0]

        # The image is replaced at the SAME canonical URL (bytes change, URL does
        # not) — the hardest case: only an explicit owner retry may revive it.
        self.images.dead.discard(url_for(71))
        self.images.put(url_for(71), (11, 22, 33, 255))
        r = self.client.post("/api/shop-publisher/emojis/backfill",
                             json={"retry_parked": True},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        payload = r.get_json()
        self.assertEqual(payload["reactivated"], 1,
                         "the API must report the product it revived")
        self.assertEqual(payload["queued"], 0,
                         "a revived row is not NEW intent and must not be double-counted")
        self.assertEqual(payload["queue"]["parked"], 0)

        row = await self._one(
            "SELECT state, attempts, generation, next_attempt_at, last_error "
            "FROM shop_emoji_queue WHERE guild_id=? AND product_id=?", (GUILD, pid))
        self.assertEqual(row[0], Q.STATE_QUEUED, "parked -> queued")
        self.assertEqual(row[1], 0, "retry budget reset")
        self.assertGreater(row[2], gen_before, "new generation invalidates stale work")
        self.assertIsNone(row[3], "immediately eligible")
        self.assertIsNone(row[4], "the old failure is cleared")

        # And the worker actually processes it.
        report = await self._run_pass()
        self.assertEqual(report["created"], 1, report)
        ready = await self._one(
            "SELECT e.status FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
            "  AND e.content_hash=a.content_hash WHERE a.source_url=? AND a.guild_id=?",
            (url_for(71), GUILD))
        self.assertIsNotNone(ready)
        self.assertEqual(ready[0], "ready")
        self.assertEqual(self.fake.deletes, 0)

    async def test_retry_endpoint_still_reactivates_a_parked_product(self):
        """#2 must not break the existing /emojis/retry behaviour."""
        self._add_product("Parked Endpoint")
        pid = await self._product_id("Parked Endpoint")
        self.images.put(url_for(72), (72, 72, 72, 255))
        self._set_icon(pid, url_for(72))
        await self._park(pid, url_for(72))
        r = self.client.post("/api/shop-publisher/emojis/retry",
                             json={"product_ids": [pid]},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        row = await self._one(
            "SELECT state, attempts FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (GUILD, pid))
        self.assertEqual(row[0], Q.STATE_QUEUED)
        self.assertEqual(row[1], 0)
        self.images.dead.discard(url_for(72))
        self.images.put(url_for(72), (7, 7, 99, 255))
        report = await self._run_pass()
        self.assertEqual(report["created"], 1)


# ── 4h. review round 2, item 3: same-URL discovery must be a true no-op ────
class BackfillIdempotenceTests(AutomationTestBase):
    async def _row(self, pid):
        return await self._one(
            "SELECT state,generation,attempts,next_attempt_at,last_error,source_url,"
            "updated_at FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (GUILD, pid))

    async def test_repeated_backfill_leaves_a_queued_row_untouched(self):
        self._add_product("Idem Queued")
        pid = await self._product_id("Idem Queued")
        self.images.put(url_for(73), (73, 73, 73, 255))
        self._set_icon(pid, url_for(73))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        before = await self._row(pid)
        self.assertEqual(before[1], 1, "first enqueue is generation 1")

        for _ in range(3):
            async with aiosqlite.connect(DB_PATH) as db:
                result = await Q.enqueue_guild_products(db, GUILD)
            self.assertEqual(result["queued"], 0, "no new intent")
            self.assertEqual(result["reactivated"], 0)
        after = await self._row(pid)
        self.assertEqual(tuple(after), tuple(before),
                         "same URL + queued intent must be a byte-identical no-op")
        self.assertEqual(after[1], 1, "generation must NOT be incremented")

    async def test_repeated_backfill_leaves_a_retry_row_in_backoff(self):
        self._add_product("Idem Retry")
        pid = await self._product_id("Idem Retry")
        self.images.put(url_for(74), (74, 74, 74, 255))
        self._set_icon(pid, url_for(74))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        # Make it a retry row in the future, as mark_failed would.
        await self._exec(
            "UPDATE shop_emoji_queue SET state=?, attempts=2, last_error='boom', "
            "next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+900 seconds') "
            "WHERE guild_id=? AND product_id=?", (Q.STATE_RETRY, GUILD, pid))
        before = await self._row(pid)
        self.assertEqual(before[0], Q.STATE_RETRY)

        for _ in range(3):
            async with aiosqlite.connect(DB_PATH) as db:
                await Q.enqueue_guild_products(db, GUILD)
        after = await self._row(pid)
        self.assertEqual(tuple(after), tuple(before),
                         "state, attempts, error and retry time must all survive")
        self.assertEqual(after[0], Q.STATE_RETRY, "must not silently become 'queued'")
        self.assertEqual(after[2], 2)
        self.assertEqual(after[3], before[3], "backoff timestamp preserved")

    async def test_same_url_discovery_cannot_invalidate_an_in_flight_pass(self):
        self._add_product("Idem Inflight")
        pid = await self._product_id("Idem Inflight")
        self.images.put(url_for(75), (75, 75, 75, 255))
        self._set_icon(pid, url_for(75))
        gate = self.images.gate(url_for(75))
        task = asyncio.create_task(self._run_pass())
        for _ in range(300):
            if url_for(75) in self.images.fetches:
                break
            await asyncio.sleep(0.01)
        self.assertIn(url_for(75), self.images.fetches, "download never started")

        # Periodic discovery runs WHILE the worker is mid-flight.
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.authorize_backfill(db, GUILD, actor="owner")
            await Q.enqueue_guild_products(db, GUILD)      # same URL
            # The row the worker claimed is still the current generation.
            still = await Q.still_current(db, {
                "guild_id": GUILD, "product_id": pid,
                "generation": (await self._row(pid))[1]})
        self.assertTrue(still, "discovery must not invalidate the claimed generation")

        gate.set()
        report = await asyncio.wait_for(task, timeout=60)
        self.assertEqual(report.get("stale", 0), 0, f"work was wrongly invalidated: {report}")
        self.assertEqual(report["created"], 1, report)
        self.assertIsNone(await self._queue_state(pid),
                          "mark_done removes the finished row")
        self.assertEqual(self.fake.creates, 1, "exactly one create, no duplicate")

    async def test_changed_url_still_bumps_the_generation(self):
        self._add_product("Idem Change")
        pid = await self._product_id("Idem Change")
        self.images.put(url_for(76), (76, 76, 76, 255))
        self._set_icon(pid, url_for(76))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        before = await self._row(pid)
        self.images.put(url_for(77), (77, 77, 77, 255))
        self._set_icon(pid, url_for(77))
        after = await self._row(pid)
        self.assertGreater(after[1], before[1], "a REAL change must bump generations")
        self.assertEqual(after[5], url_for(77))
        self.assertEqual(after[0], Q.STATE_QUEUED)


# ── 4i. review round 2, item 4: one definition of "ready" ──────────────────
class ReadyDefinitionTests(AutomationTestBase):
    async def _plant(self, canon, emoji_id, *, status="ready", guild=GUILD):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO shop_emoji_assets (application_id,content_hash,"
                "emoji_name,emoji_id,animated,creator_guild_id,pipeline_version,status) "
                "VALUES (?,?,?,?,0,?,?,?)",
                (APP, "f" * 64, "shop_planted12345678", emoji_id, guild,
                 "rgba-128-png-v1", status))
            await db.execute(
                "INSERT OR REPLACE INTO shop_emoji_url_aliases "
                "(application_id,guild_id,source_url,content_hash) VALUES (?,?,?,?)",
                (APP, guild, canon, "f" * 64))
            await db.commit()

    async def _assert_unusable_id_does_not_suppress(self, label, value, index):
        name = f"Ready {label}"
        self._add_product(name)
        pid = await self._product_id(name)
        canon = url_for(80 + index)
        self.images.put(canon, (80 + index, 80, 80, 255))
        self._set_icon(pid, canon)
        await self._exec("DELETE FROM shop_emoji_queue")
        await self._plant(canon, value)
        async with aiosqlite.connect(DB_PATH) as db:
            result = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(
            result["queued"], 1,
            f"a {label} emoji_id cannot render in Inventory/Rank, so it must not "
            "suppress backfill")

    async def test_invalid_emoji_id_empty_string(self):
        await self._assert_unusable_id_does_not_suppress("empty string", "", 0)

    async def test_invalid_emoji_id_non_numeric(self):
        await self._assert_unusable_id_does_not_suppress("non-numeric", "not-an-id", 1)

    async def test_invalid_emoji_id_zero(self):
        await self._assert_unusable_id_does_not_suppress("zero", 0, 2)

    async def test_invalid_emoji_id_null(self):
        await self._assert_unusable_id_does_not_suppress("null", None, 3)

    async def test_valid_positive_id_is_recognised_as_ready(self):
        self._add_product("Ready Valid")
        pid = await self._product_id("Ready Valid")
        canon = url_for(81)
        self.images.put(canon, (81, 81, 81, 255))
        self._set_icon(pid, canon)
        await self._exec("DELETE FROM shop_emoji_queue")
        await self._plant(canon, "950000000000000001")
        async with aiosqlite.connect(DB_PATH) as db:
            result = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(result["queued"], 0,
                         "a genuinely ready asset suppresses duplicate work")

    async def test_not_ready_status_is_never_prepared_even_with_a_valid_id(self):
        self._add_product("Ready Status")
        pid = await self._product_id("Ready Status")
        canon = url_for(82)
        self.images.put(canon, (82, 82, 82, 255))
        self._set_icon(pid, canon)
        await self._exec("DELETE FROM shop_emoji_queue")
        await self._plant(canon, "950000000000000002", status="uncertain")
        async with aiosqlite.connect(DB_PATH) as db:
            result = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(result["queued"], 1, "only status='ready' counts")


# ── 4j. review round 2, item 5: canonicalisation in the NAME lookup ────────
class CanonicalNameLookupTests(AutomationTestBase):
    """A stored URL and its canonical alias must agree for naming.

    ``shop_emoji_url_aliases.source_url`` is canonicalized; ``shop_items.icon_url``
    is stored as the owner typed it. Raw string equality between the two is
    therefore wrong, and it silently made the readable name depend on which
    product happened to be processed first.
    """

    def _upper_host(self, index):
        return f"https://ASSETS.Example.COM/item-{index}.png?v=1"

    def _explicit_port(self, index):
        return f"https://assets.example.com:443/item-{index}.png?v=1"

    async def test_hostname_case_does_not_hide_the_label(self):
        colour = (101, 102, 103, 255)
        self._add_product("Zebra Item")          # lexicographically LAST
        self._add_product("Alpha Item")          # should win, but is stored oddly
        alpha = await self._product_id("Alpha Item")
        zebra = await self._product_id("Zebra Item")
        self.images.put(url_for(90), colour)
        # Alpha and Zebra share identical bytes; Alpha's URL is written with an
        # uppercase host so raw equality with the canonical alias fails.
        self._set_icon(alpha, self._upper_host(90))
        self._set_icon(zebra, url_for(90))
        self.assertEqual(SE.canonicalize_url(self._upper_host(90)),
                         SE.canonicalize_url(url_for(90)),
                         "the two strings must be the same URL to this pipeline")

        await self._drain()
        self.assertEqual(len(self.fake.remote), 1, "identical bytes -> one emoji")
        self.assertTrue(self.fake.remote[0]["name"].startswith("alpha_item_"),
                        f"label lookup lost the canonical name: {self.fake.remote[0]['name']}")

    async def test_explicit_default_port_does_not_hide_the_label(self):
        colour = (201, 202, 203, 255)
        self._add_product("Beta Item")
        self._add_product("Zeta Item")
        beta = await self._product_id("Beta Item")
        zeta = await self._product_id("Zeta Item")
        self.images.put(url_for(91), colour)
        self._set_icon(beta, self._explicit_port(91))
        self._set_icon(zeta, url_for(91))
        await self._drain()
        self.assertEqual(len(self.fake.remote), 1)
        self.assertTrue(self.fake.remote[0]["name"].startswith("beta_item_"),
                        self.fake.remote[0]["name"])

    async def test_order_and_restart_do_not_change_the_name(self):
        colour = (31, 32, 33, 255)
        names = ["Mike Item", "Alpha Item", "Charlie Item"]
        variants = [url_for(92), self._upper_host(92), self._explicit_port(92)]
        ids = []
        for name, url in zip(names, variants):
            self._add_product(name)
            pid = await self._product_id(name)
            self.images.put(url_for(92), colour)
            self._set_icon(pid, url)
            ids.append(pid)
        await self._drain()
        first = [e["name"] for e in self.fake.remote]
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0].startswith("alpha_item_"), first)

        # Restart and process again in the opposite order.
        SE._preparation_locks.clear()
        W._pass_lock = None
        await self._exec("DELETE FROM shop_emoji_queue")
        await self._exec("UPDATE shop_items SET icon_url=icon_url")   # keep URLs
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD, only_unprepared=False)
        await self._drain()
        self.assertEqual([e["name"] for e in self.fake.remote], first,
                         "the readable name must not depend on processing order")

    async def test_repeated_passes_issue_no_extra_patches(self):
        colour = (51, 52, 53, 255)
        self._add_product("Nova Item")
        self._add_product("Atlas Item")
        nova = await self._product_id("Nova Item")
        atlas = await self._product_id("Atlas Item")
        self.images.put(url_for(93), colour)
        self._set_icon(nova, self._upper_host(93))
        self._set_icon(atlas, url_for(93))
        await self._drain()
        renames_before = self.fake.renames
        for _ in range(3):
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL")
            await self._drain(max_passes=3)
            r = await self._names_pass()
            self.assertEqual(r["renamed"], 0, f"unnecessary rename: {r['preview']}")
        self.assertEqual(self.fake.renames, renames_before)

    async def _names_pass(self):
        return await W.rename_all(apply=True)


# ── 4k. review round 2, item 6: guild-scoped rename cursor ─────────────────
class RenameCursorScopeTests(AutomationTestBase):
    """`rename_all(guild_id=…)` must not let another guild's hashes advance it.

    Assets are created through the real pipeline, so each one has a bound
    listing and therefore a real readable label. Their names are then rewound to
    the legacy hash-only form, and every other asset (in content_hash order) is
    reassigned to the other guild. The two guilds' hashes therefore INTERLEAVE,
    which is exactly the case where an unscoped MAX(content_hash) cursor skips
    the target guild's rows.
    """

    async def _seed_interleaved(self, count=8):
        await self._seed_products(count)
        await self._drain()
        self.assertEqual(len(self.fake.remote), count)
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                "SELECT content_hash FROM shop_emoji_assets ORDER BY content_hash")).fetchall()
            target, other = [], []
            legacy_names = {}
            for index, (chash,) in enumerate(rows):
                legacy = SE.deterministic_emoji_name(chash)
                legacy_names[chash] = legacy
                await db.execute(
                    "UPDATE shop_emoji_assets SET emoji_name=?,managed_name=0 "
                    "WHERE content_hash=?", (legacy, chash))
                guild = OTHER_GUILD if index % 2 else GUILD
                await db.execute(
                    "UPDATE shop_emoji_assets SET creator_guild_id=? "
                    "WHERE content_hash=?", (guild, chash))
                (target if guild == GUILD else other).append(chash)
            await db.commit()
        for emoji in self.fake.remote:
            emoji["name"] = "shop_legacy_" + emoji["id"][-6:]
        ids_before = {e["id"]: e["name"] for e in self.fake.remote}
        self.assertTrue(target and other, "both guilds must have assets")
        self.legacy_names = legacy_names
        # REAL before-state, captured so the assertions below compare actual
        # values rather than an expression with itself.
        self.ids_before = {
            row[0]: row[1] for row in await self._all(
                "SELECT content_hash, emoji_id FROM shop_emoji_assets")}
        self.aliases_before = sorted(await self._all(
            "SELECT guild_id, source_url, content_hash FROM shop_emoji_url_aliases"))
        # Prove the interleaving the reviewer described.
        ordered = sorted(target + other)
        self.assertNotEqual(ordered[:2], sorted(target)[:2],
                            "hashes must be interleaved for this test to mean anything")
        return target, other, ids_before

    async def test_interleaved_guilds_do_not_skip_the_target_guild(self):
        target, other, ids_before = await self._seed_interleaved()
        report = await W.rename_all(guild_id=GUILD, apply=True, page_size=2)
        self.assertFalse(report["skipped_no_credentials"], report)
        renamed = {r["content_hash"] for r in report["results"] if r.get("renamed")}
        self.assertEqual(renamed, set(target),
                         "every eligible target-guild asset must be renamed, and "
                         "no other-guild asset")
        self.assertEqual(report["renamed"], len(target))
        for row in report["results"]:
            if row.get("renamed"):
                self.assertEqual(row["guild_id"], GUILD)

        rows = await self._all(
            "SELECT content_hash,emoji_id,creator_guild_id,emoji_name,managed_name "
            "FROM shop_emoji_assets ORDER BY content_hash")
        # GENUINE identity check: every content_hash -> emoji_id mapping must be
        # unchanged, i.e. no emoji was recreated, rebound or deleted.
        self.assertEqual({r[0]: r[1] for r in rows}, self.ids_before,
                         "renaming must preserve every content_hash -> emoji_id pair")
        self.assertEqual(sorted(await self._all(
            "SELECT guild_id, source_url, content_hash FROM shop_emoji_url_aliases")),
            self.aliases_before,
            "renaming must not touch a single alias row")
        for chash, _eid, guild, name, managed in rows:
            if chash in other:
                self.assertEqual(guild, OTHER_GUILD)
                self.assertEqual(name, self.legacy_names[chash],
                                 "another guild's asset must NOT be renamed")
                self.assertEqual(managed, 0)
        self.assertEqual({e["id"] for e in self.fake.remote}, set(ids_before),
                         "no emoji was recreated or deleted")
        self.assertEqual(self.fake.deletes, 0)

    async def test_second_scoped_run_issues_zero_patches(self):
        target, other, ids_before = await self._seed_interleaved(6)
        first = await W.rename_all(guild_id=GUILD, apply=True, page_size=2)
        self.assertEqual(first["renamed"], len(target), first)
        before = self.fake.patch_requests
        second = await W.rename_all(guild_id=GUILD, apply=True, page_size=2)
        self.assertEqual(second["renamed"], 0, second)
        self.assertEqual(self.fake.patch_requests, before,
                         "a second scoped run must issue no PATCH")
        # IDs, hashes and aliases are all preserved, compared against a REAL
        # snapshot taken before the first run.
        rows = await self._all(
            "SELECT content_hash, emoji_id FROM shop_emoji_assets")
        self.assertEqual({r[0]: r[1] for r in rows}, self.ids_before,
                         "a second run must not rebind any emoji")
        self.assertEqual(sorted(await self._all(
            "SELECT guild_id, source_url, content_hash FROM shop_emoji_url_aliases")),
            self.aliases_before)
        self.assertEqual({e["id"] for e in self.fake.remote}, set(ids_before))


# ── 4l. review round 2, item 1: the Dashboard bulk action ──────────────────
class DashboardBulkActionTests(AutomationTestBase):
    """The owner button must cover the whole `shop_items` catalog.

    None of these products are ever added to a Shop Designer roster: they are
    created straight against the Shop database, which is exactly the case the
    reviewer called out (``draft.products.slice()`` would have sent none).
    """

    async def _catalog_product(self, name, index, guild=GUILD):
        url = url_for(index)
        self.images.put(url, ((index * 17) % 256, (index * 31) % 256, 90, 255))
        await self._exec(
            "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
            "VALUES (?,?,?,?,?,1)", (guild, name, 100, "custom", url))
        return await self._one(
            "SELECT id FROM shop_items WHERE guild_id=? AND name=?", (guild, name))

    async def test_one_action_queues_the_whole_catalog_without_a_roster(self):
        rows = [await self._catalog_product(f"Catalog {i:02d}", i) for i in range(5)]
        ids = [r[0] for r in rows]
        # Nothing was added to the Shop Designer; the queue starts empty because
        # these rows were inserted directly rather than through the save path.
        await self._exec("DELETE FROM shop_emoji_queue")
        self.assertEqual((await self._one("SELECT COUNT(*) FROM shop_emoji_queue"))[0], 0)

        r = self.client.post("/api/shop-publisher/emojis/backfill", json={},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        payload = r.get_json()
        self.assertEqual(payload["queued"], 5, "one action, the entire catalog")
        queued = await self._all(
            "SELECT product_id, guild_id FROM shop_emoji_queue ORDER BY product_id")
        self.assertEqual([q[0] for q in queued], sorted(ids),
                         "every eligible product entered the queue")
        self.assertEqual({q[1] for q in queued}, {GUILD})
        self.assertTrue(payload["queue"]["authorized"])

        await self._drain()
        self.assertEqual(len(self.fake.remote), 5)
        self.assertEqual(self.fake.deletes, 0)

    async def test_action_ignores_the_designer_roster_entirely(self):
        await self._catalog_product("Not Rostered A", 10)
        await self._catalog_product("Not Rostered B", 11)
        await self._exec("DELETE FROM shop_emoji_queue")
        # The roster is irrelevant to this endpoint: it is never consulted.
        r = self.client.post("/api/shop-publisher/emojis/backfill", json={},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.get_json()["queued"], 2)

    async def test_another_guild_is_untouched_and_unauthorised(self):
        await self._catalog_product("Mine", 20)
        rows = await self._catalog_product("Theirs", 21, guild=OTHER_GUILD)
        await self._exec("DELETE FROM shop_emoji_queue")
        r = self.client.post("/api/shop-publisher/emojis/backfill", json={},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["queued"], 1)
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertFalse(await Q.is_authorized(db, OTHER_GUILD),
                             "advertising one guild must not authorise another")
        await self._drain()
        self.assertEqual(len(self.fake.remote), 1)

    async def test_excluded_products_are_not_queued(self):
        await self._catalog_product("Enabled Product", 30)
        await self._exec(
            "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
            "VALUES (?,?,?,?,?,0)", (GUILD, "Disabled Product", 5, "custom", url_for(31)))
        await self._exec(
            "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
            "VALUES (?,?,?,?,'',1)", (GUILD, "No Image Product", 5, "custom"))
        await self._exec("DELETE FROM shop_emoji_queue")
        r = self.client.post("/api/shop-publisher/emojis/backfill", json={},
                             headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.get_json()["queued"], 1,
                         "disabled and image-less products are not eligible")

    async def test_the_action_reports_meaningful_progress_numbers(self):
        for index in range(3):
            await self._catalog_product(f"Progress {index}", 40 + index)
        await self._exec("DELETE FROM shop_emoji_queue")
        self.client.post("/api/shop-publisher/emojis/backfill", json={},
                         headers={"X-CSRF-Token": CSRF})
        await self._drain()
        report = self.client.get("/api/shop-publisher/emojis/report",
                                 headers={"X-CSRF-Token": CSRF}).get_json()
        self.assertEqual(report["eligible_products"], 3)
        self.assertEqual(report["already_ready"], 3, "the same fields the UI renders")
        self.assertEqual(report["unprepared_products"], 0)
        self.assertEqual(report["quota"]["used"], 3)
        self.assertEqual(report["queue"]["total"], 0)
        self.assertEqual(report["parked"], [])


# ── 4m. review round 2, item 1: the control actually exists in the page ────
class DashboardControlPresenceTests(unittest.TestCase):
    """SOURCE-LEVEL checks only — the rendered element and the wiring strings.

    These are deliberately NOT proof that a user can click the control. A
    source-string assertion cannot tell whether the page mounts, whether the
    button is enabled, or whether a click reaches fetch(). The runtime proof
    lives in ``scripts/test_shop_designer_ui.js``, which renders the real
    template in a DOM, mounts the real module through the real page lifecycle,
    dispatches a real MouseEvent and asserts on the outgoing request. Both run
    in this PR's battery; neither substitutes for the other.
    """

    def setUp(self):
        self.template = (ROOT / "dashboard/templates/manage/shopdesigner.html").read_text(encoding="utf-8")
        self.script = (ROOT / "dashboard/static/js/shop-designer.js").read_text(encoding="utf-8")

    def test_the_owner_action_is_present_in_the_page(self):
        self.assertIn('id="sd-catalog-emoji-backfill"', self.template)
        self.assertIn("Prepare all Shop product icons", self.template)
        for element in ("sd-catalog-emoji-refresh", "sd-catalog-emoji-retry-parked",
                        "sd-catalog-emoji-status", "sd-catalog-emoji-report"):
            self.assertIn(f'id="{element}"', self.template, element)

    def test_the_control_calls_the_backfill_endpoint(self):
        self.assertIn("/api/shop-publisher/emojis/backfill", self.script)
        self.assertIn("EMOJI_BACKFILL_URL", self.script)
        self.assertIn("runCatalogBackfill", self.script)
        self.assertIn("ctx.on(catalogEmojiBackfill, 'click', runCatalogBackfill)", self.script)

    def test_the_control_does_not_depend_on_the_roster(self):
        # The new path must not read draft.products at all.
        start = self.script.index("function runCatalogBackfill(")
        end = self.script.index("function runEmojiOperation(")
        body = self.script[start:end]
        self.assertNotIn("draft.products", body,
                         "the catalog action must work from shop_items, not the roster")

    def test_progress_is_rendered_from_the_read_only_report(self):
        self.assertIn("/api/shop-publisher/emojis/report", self.script)
        for field in ("eligible_products", "already_ready", "unprepared_products",
                      "deferred", "parked", "quota_limited"):
            self.assertIn(field, self.script, field)


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


# ── 6f. audit: the Dashboard report must agree with Backfill ───────────────
class ReportAgreementTests(AutomationTestBase):
    """One readiness predicate for Backfill and the report.

    If they disagree, the Dashboard says "everything is prepared" while the
    worker keeps queueing the same product — or the reverse, which is worse:
    a product that can never render is reported as complete.
    """

    async def _plant(self, canon, emoji_id, *, status="ready", guild=None):
        guild = GUILD if guild is None else guild
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO shop_emoji_assets (application_id,content_hash,"
                "emoji_name,emoji_id,animated,creator_guild_id,pipeline_version,status) "
                "VALUES (?,?,?,?,0,?,?,?)",
                (APP, "e" * 64, "shop_planted12345678", emoji_id, guild,
                 "rgba-128-png-v1", status))
            await db.execute(
                "INSERT OR REPLACE INTO shop_emoji_url_aliases "
                "(application_id,guild_id,source_url,content_hash) VALUES (?,?,?,?)",
                (APP, guild, canon, "e" * 64))
            await db.commit()

    async def _report(self):
        r = self.client.get("/api/shop-publisher/emojis/report",
                            headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    async def _backfill_says_queued(self):
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            return (await Q.enqueue_guild_products(db, GUILD))["queued"]

    async def _ready_agreement(self, label, value, index):
        """Report and Backfill must agree that this asset is NOT prepared."""
        name = f"Agree {label}"
        self._add_product(name)
        pid = await self._product_id(name)
        canon = url_for(120 + index)
        self.images.put(canon, (120 + index, 40, 40, 255))
        self._set_icon(pid, canon)
        await self._plant(canon, value)

        report = await self._report()
        self.assertEqual(report["unprepared_products"], 1,
                         f"the report must not call a {label} emoji_id prepared")
        self.assertEqual(report["already_ready"], 0)
        self.assertEqual(await self._backfill_says_queued(), 1,
                         "and Backfill must agree it is queueable")

    async def test_report_agrees_empty_string_id_is_not_prepared(self):
        await self._ready_agreement("empty", "", 0)

    async def test_report_agrees_non_numeric_id_is_not_prepared(self):
        await self._ready_agreement("text", "abc", 1)

    async def test_report_agrees_zero_id_is_not_prepared(self):
        await self._ready_agreement("zero", 0, 2)

    async def test_report_agrees_null_id_is_not_prepared(self):
        await self._ready_agreement("null", None, 3)

    async def test_report_agrees_a_valid_id_is_prepared(self):
        self._add_product("Agree Valid")
        pid = await self._product_id("Agree Valid")
        canon = url_for(125)
        self.images.put(canon, (9, 99, 9, 255))
        self._set_icon(pid, canon)
        await self._plant(canon, "960000000000000001")
        report = await self._report()
        self.assertEqual(report["already_ready"], 1)
        self.assertEqual(report["unprepared_products"], 0)
        self.assertEqual(await self._backfill_says_queued(), 0)

    async def test_invalid_source_urls_are_reported_not_silently_dropped(self):
        self._add_product("Valid Product")
        pid = await self._product_id("Valid Product")
        self.images.put(url_for(130), (1, 2, 3, 255))
        self._set_icon(pid, url_for(130))
        # Two products whose stored URL cannot be canonicalized at all.
        for name, bad in (("Http Product", "http://insecure.example.com/x.png"),
                          ("Malformed Product", "https://")):
            await self._exec(
                "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) "
                "VALUES (?,?,?,?,?,1)", (GUILD, name, 10, "custom", bad))
        await self._exec("DELETE FROM shop_emoji_queue")

        report = await self._report()
        self.assertEqual(report["eligible_products"], 3,
                         "all three non-blank enabled listings are eligible")
        self.assertEqual(len(report["invalid_source_products"]), 2,
                         "unsupported URLs must be reported, not dropped")
        self.assertEqual(
            report["already_ready"] + report["unprepared_products"]
            + len(report["invalid_source_products"]), report["eligible_products"],
            "ready + unprepared + invalid must ACCOUNT FOR every eligible product")
        self.assertFalse(report["fully_prepared"],
                         "cannot claim completion while products were excluded")

    async def test_fully_prepared_is_only_true_when_nothing_is_outstanding(self):
        self._add_product("Done Product")
        pid = await self._product_id("Done Product")
        self.images.put(url_for(140), (4, 5, 6, 255))
        self._set_icon(pid, url_for(140))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        await self._drain()
        report = await self._report()
        self.assertEqual(report["unprepared_products"], 0)
        self.assertEqual(report["invalid_source_products"], [])
        self.assertTrue(report["fully_prepared"])

    async def test_deferred_count_is_not_labelled_uncertain(self):
        """`stats()` counts all future retry rows, not just uncertain creates."""
        self._add_product("Deferred Product")
        pid = await self._product_id("Deferred Product")
        self.images.put(url_for(141), (7, 8, 9, 255))
        self._set_icon(pid, url_for(141))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.enqueue_guild_products(db, GUILD)
        # A plain source failure in backoff: NOT an uncertain Discord create.
        await self._exec(
            "UPDATE shop_emoji_queue SET state=?, attempts=1, last_error='source_http_error: 404', "
            "next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+600 seconds') "
            "WHERE guild_id=? AND product_id=?", (Q.STATE_RETRY, GUILD, pid))
        report = await self._report()
        self.assertGreaterEqual(report["queue"]["deferred"], 1)
        self.assertEqual(report["uncertain"], 0,
                         "a source failure is not an uncertain Discord create")
        self.assertTrue(report["queue"]["retrying_backoff"] >= 1)


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


# ── 4b. audit round 5: real interleavings and the true POST boundary ───────
class _InterleavingDB:
    """aiosqlite proxy that lets a test act BETWEEN the code-under-test's statements.

    ``hook(n)`` runs, awaited, immediately before the n-th statement issued
    while this connection is NOT inside a write transaction. Those are exactly
    the windows in which another process (the Dashboard) can commit: while a
    write transaction is open SQLite already serializes the writers. The tests
    sweep every such point, so they hold for any implementation and do not
    depend on which helper function the write happens to go through.
    """

    def __init__(self, db, hook):
        self._db, self._hook, self.points = db, hook, 0

    async def execute(self, sql, params=()):
        if not self._db.in_transaction:
            self.points += 1
            await self._hook(self.points)
        return await self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


class StaleWriteInterleavingTests(AutomationTestBase):
    """A stale snapshot may never replace newer intent, at ANY interleaving point,
    and after every race the newest listing URL must still be what gets prepared."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.created_hashes = []
        real_create = self.fake.create_emoji

        async def spy(name, png, bot_user_id):
            self.created_hashes.append(SE.content_identity(png))
            return await real_create(name, png, bot_user_id)
        self.fake.create_emoji = spy

    async def _queue_row(self, pid):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"SELECT {','.join(Q.QUEUE_FIELDS)} FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, pid))
            fetched = await cur.fetchone()
            return Q._row(fetched) if fetched else None

    async def _fresh_product(self, label, url):
        await self._exec("DELETE FROM shop_items WHERE guild_id=?", (GUILD,))
        await self._exec("DELETE FROM shop_emoji_queue")
        self._add_product(label)
        pid = await self._product_id(label)
        self.images.put(url, (5, 5, 5, 255))
        self._set_icon(pid, url)
        return pid

    async def _dashboard_save_sql(self, pid, url):
        """What a Dashboard save does at the DB level: commit the listing, then queue it."""
        async with aiosqlite.connect(DB_PATH) as d:
            await d.execute("UPDATE shop_items SET icon_url=? WHERE id=?", (url, pid))
            await d.commit()
            await Q.enqueue_product(d, GUILD, pid, "n", url, reason="product_saved")

    async def _assert_converges(self, pid, newest_url, label):
        """Drain the real worker: newest URL prepared, nothing superseded uploaded, not stuck."""
        await self._drain()
        icon = (await self._one("SELECT icon_url FROM shop_items WHERE id=?", (pid,)))[0]
        self.assertEqual(Q.queue_source_url(icon), Q.queue_source_url(newest_url),
                         f"{label}: the listing is the authoritative intent")
        self.assertIsNone(await self._queue_row(pid), f"{label}: queue row stuck after drain")
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertTrue(await Q._resolves_ready(db, APP, GUILD, Q.queue_source_url(newest_url)),
                            f"{label}: the newest image was never prepared")
        newest_hash = SE.content_identity(self.images.contents[newest_url])
        self.assertTrue(set(self.created_hashes) <= {newest_hash},
                        f"{label}: a superseded image was uploaded to Discord")
        stuck = await self._all("SELECT status FROM shop_emoji_assets "
                                "WHERE status IN ('preparing','uncertain')")
        self.assertEqual(stuck, [], f"{label}: asset left recovering")

    async def _sweep(self, label, scenario):
        """Run ``scenario(db, fired, k, label)`` once per interleaving point.

        ``scenario`` builds the starting state, calls the code under test with the
        proxied connection (the Dashboard save fires before statement ``k``) and
        returns ``(pid, newest_url)``.
        """
        counted = {"n": 0}

        async def count_only(n):
            counted["n"] = n
        async with aiosqlite.connect(DB_PATH) as raw:
            await scenario(_InterleavingDB(raw, count_only), None, 0, label + "-dry")
        total = counted["n"]
        # At minimum the snapshot read and the write must both be interleavable.
        self.assertGreaterEqual(total, 2, "sweep would not cover both the read and the write")
        for k in range(1, total + 1):
            fired = {}
            self.created_hashes.clear()

            async def act(n, k=k, fired=fired):
                if n == k and not fired:
                    fired["go"] = True
            async with aiosqlite.connect(DB_PATH) as raw:
                pid, newest_url = await scenario(_InterleavingDB(raw, act), fired, k, f"{label}-{k}")
            self.assertTrue(fired.get("saved"), f"point {k}: the Dashboard save never ran")
            row = await self._queue_row(pid)
            self.assertIsNotNone(row, f"point {k}: newer intent was destroyed")
            self.assertEqual(row["source_url"], Q.queue_source_url(newest_url),
                             f"point {k}: a stale write replaced the newer URL")
            self.assertEqual(row["generation"], fired["gen"],
                             f"point {k}: a stale write bumped/replaced the newer generation")
            await self._assert_converges(pid, newest_url, f"{label} point {k}")

    def _arm(self, db, fired, save):
        """Make the Dashboard save fire at the chosen statement."""
        inner = db._hook

        async def hook(n):
            await inner(n)
            if fired.get("go") and not fired.get("saved"):
                fired["saved"] = True
                await save()
        db._hook = hook

    async def test_requeue_cannot_replace_a_save_made_between_its_check_and_its_write(self):
        async def scenario(db, fired, k, label):
            a, b, newest = url_for(1000 + 10 * k), url_for(1001 + 10 * k), url_for(1002 + 10 * k)
            pid = await self._fresh_product(label, a)
            self.images.put(b, (1, 2, 3, 255))
            self.images.put(newest, (7, k % 256, 7, 255))
            old_row = await self._queue_row(pid)              # generation held by the worker
            # The Dashboard has committed URL B to shop_items but has not queued it yet.
            await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (b, pid))
            if fired is not None:
                async def save():
                    self._set_icon(pid, newest)               # real Dashboard save
                    fired["gen"] = (await self._queue_row(pid))["generation"]
                self._arm(db, fired, save)
            await Q.requeue_if_current(db, old_row, "n", Q.queue_source_url(b))
            return pid, newest

        await self._sweep("requeue", scenario)

    async def test_backfill_cannot_replace_a_save_made_after_its_catalog_snapshot(self):
        async def scenario(db, fired, k, label):
            a, newest = url_for(2000 + 10 * k), url_for(2001 + 10 * k)
            pid = await self._fresh_product(label, a)
            await self._exec("DELETE FROM shop_emoji_queue")   # Backfill sees an unqueued product
            self.images.put(newest, (9, k % 256, 9, 255))
            if fired is not None:
                async def save():
                    self._set_icon(pid, newest)               # Dashboard saves URL B mid-Backfill
                    fired["gen"] = (await self._queue_row(pid))["generation"]
                self._arm(db, fired, save)
            await Q.enqueue_guild_products(db, GUILD)
            return pid, newest

        await self._sweep("backfill", scenario)

    async def test_owner_retry_without_a_queue_row_cannot_replace_a_concurrent_save(self):
        """/emojis/retry with no descriptor reads the listing, then writes a queue row."""
        import dashboard.api.shop_emoji as endpoint

        class _Connect:
            """The endpoint's own connection, proxied so every statement is an interleave point."""
            def __init__(self, hook):
                self.hook = hook

            async def __aenter__(self):
                self.raw = await aiosqlite.connect(DB_PATH)
                return _InterleavingDB(self.raw, self.hook)

            async def __aexit__(self, *exc):
                await self.raw.close()

        async def scenario(db, fired, k, label):
            a, newest = url_for(3000 + 10 * k), url_for(3001 + 10 * k)
            pid = await self._fresh_product(label, a)
            await self._exec("DELETE FROM shop_emoji_queue")   # never queued / already finished
            self.images.put(newest, (3, k % 256, 3, 255))

            async def hook(n):
                await db._hook(n)          # the sweep's counter (dry run) or trigger (point k)
                if fired and fired.get("go") and not fired.get("saved"):
                    fired["saved"] = True
                    await self._dashboard_save_sql(pid, newest)
                    fired["gen"] = (await self._queue_row(pid))["generation"]
            ns = SimpleNamespace(connect=lambda path, *a, **kw: _Connect(hook))
            with patch.object(endpoint, "aiosqlite", ns):
                r = self.client.post("/api/shop-publisher/emojis/retry",
                                     json={"product_ids": [pid]},
                                     headers={"X-CSRF-Token": CSRF})
            self.assertEqual(r.status_code, 200, r.get_json())
            return pid, newest

        await self._sweep("owner-retry", scenario)

    async def test_two_dashboard_saves_finishing_out_of_order_converge_on_the_final_listing(self):
        """Save 1 commits the listing, save 2 commits AND enqueues, then save 1 enqueues.

        Both saves go through the real endpoint on the Dashboard's own loop; save 1's
        enqueue is held until save 2 has fully returned. That leaves the queue holding
        the OLDER url while the listing holds the NEWER one (the unfavourable order).
        """
        import threading
        a, x, y = url_for(4000), url_for(4001), url_for(4002)
        pid = await self._fresh_product("Two Saves", a)
        self.images.put(x, (200, 20, 20, 255))
        self.images.put(y, (20, 200, 20, 255))
        reached, save2_done = threading.Event(), threading.Event()
        real_enqueue = Q.enqueue_product

        async def held_for_x(db, guild_id, product_id, item_name, icon_url, **kw):
            if icon_url == x:
                reached.set()
                while not save2_done.is_set():          # never block the shared loop
                    await asyncio.sleep(0.01)
            return await real_enqueue(db, guild_id, product_id, item_name, icon_url, **kw)

        with patch.object(Q, "enqueue_product", held_for_x):
            save1 = asyncio.create_task(asyncio.to_thread(self._set_icon, pid, x))
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(reached.wait, 30), 35),
                            "save 1 never reached its enqueue")
            await asyncio.to_thread(self._set_icon, pid, y)
            save2_done.set()
            await asyncio.wait_for(save1, 30)

        icon = (await self._one("SELECT icon_url FROM shop_items WHERE id=?", (pid,)))[0]
        self.assertEqual(icon, y, "save 2 wrote the listing last")
        row = await self._queue_row(pid)
        self.assertEqual(row["source_url"], Q.queue_source_url(x),
                         "precondition: the older save enqueued last, so the queue is stale")
        await self._assert_converges(pid, y, "out-of-order saves")

    async def test_fenced_write_refuses_to_resurrect_a_queue_row_that_disappeared(self):
        """Caller read generation G; another worker finished and deleted the row; stale write arrives."""
        pid = await self._fresh_product("Resurrect", url_for(5000))
        row = await self._queue_row(pid)
        icon = (await self._one("SELECT icon_url FROM shop_items WHERE id=?", (pid,)))[0]
        await self._exec("DELETE FROM shop_emoji_queue")            # consumed by someone else
        async with aiosqlite.connect(DB_PATH) as db:
            refused = await Q.enqueue_product(
                db, GUILD, pid, "n", icon, expect_icon_url=icon, expect_generation=row["generation"])
        self.assertIsNone(refused, "a numeric generation names a row that no longer exists")
        self.assertIsNone(await self._queue_row(pid), "stale work was resurrected")
        # A caller that genuinely observed NO row keeps its behaviour: it may insert.
        async with aiosqlite.connect(DB_PATH) as db:
            written = await Q.enqueue_product(
                db, GUILD, pid, "n", icon, expect_icon_url=icon, expect_generation=None)
        self.assertEqual(written, Q.queue_source_url(icon))
        self.assertEqual((await self._queue_row(pid))["source_url"], Q.queue_source_url(icon))

    async def test_requeue_does_not_resurrect_a_consumed_row_and_discovery_still_converges(self):
        a, b = url_for(5010), url_for(5011)
        pid = await self._fresh_product("Requeue Gone", a)
        self.images.put(b, (30, 60, 90, 255))
        old_row = await self._queue_row(pid)
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (b, pid))   # listing moved on
        await self._exec("DELETE FROM shop_emoji_queue")            # row consumed after the worker read it
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertFalse(await Q.requeue_if_current(db, old_row, "n", Q.queue_source_url(b)))
        self.assertIsNone(await self._queue_row(pid), "a consumed row was resurrected by requeue")
        # Not stranded: the next discovery tick sees no row and queues the listing's URL.
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertEqual((await Q.enqueue_guild_products(db, GUILD))["queued"], 1)
        await self._assert_converges(pid, b, "requeue then discovery")

    async def test_backfill_does_not_resurrect_a_row_consumed_after_its_snapshot(self):
        """Two products: while product 1 is being written, product 2's row (already in
        Backfill's queue snapshot) is consumed by a worker. The write for product 2 must
        refuse, and the next discovery tick must still queue it."""
        a1, b1, a2, b2 = url_for(5020), url_for(5021), url_for(5022), url_for(5023)
        p1 = await self._fresh_product("Backfill One", a1)
        self._add_product("Backfill Two")
        p2 = await self._product_id("Backfill Two")
        self._set_icon(p2, a2)
        for pid, newer, color in ((p1, b1, (60, 30, 90, 255)), (p2, b2, (90, 30, 60, 255))):
            self.images.put(newer, color)
            await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (newer, pid))
        self.assertEqual({(await self._queue_row(p1))["source_url"], (await self._queue_row(p2))["source_url"]},
                         {Q.queue_source_url(a1), Q.queue_source_url(a2)}, "both rows are in the snapshot")
        fired, real = {}, Q.enqueue_product

        async def consume_p2_during_p1(db, guild_id, product_id, *args, **kwargs):
            if not fired:
                fired["yes"] = True                       # after the snapshot, before p2's write
                await self._exec("DELETE FROM shop_emoji_queue WHERE product_id=?", (p2,))
            return await real(db, guild_id, product_id, *args, **kwargs)
        with patch.object(Q, "enqueue_product", consume_p2_during_p1):
            async with aiosqlite.connect(DB_PATH) as db:
                result = await Q.enqueue_guild_products(db, GUILD)
        self.assertTrue(fired, "the interleaving never happened; test would be vacuous")
        self.assertEqual(result["queued"], 1, "only product 1 may be written; product 2's row was gone")
        self.assertEqual((await self._queue_row(p1))["source_url"], Q.queue_source_url(b1))
        self.assertIsNone(await self._queue_row(p2), "a consumed snapshot row was resurrected")
        async with aiosqlite.connect(DB_PATH) as db:      # next tick: fresh snapshot, no row for p2
            self.assertEqual((await Q.enqueue_guild_products(db, GUILD))["queued"], 1)
        await self._drain()
        async with aiosqlite.connect(DB_PATH) as db:
            for newer in (b1, b2):
                self.assertTrue(await Q._resolves_ready(db, APP, GUILD, Q.queue_source_url(newer)))
        self.assertTrue(set(self.created_hashes) <= {SE.content_identity(self.images.contents[b1]),
                                                     SE.content_identity(self.images.contents[b2])},
                        "a superseded image was uploaded")
        self.assertEqual(await self._all("SELECT 1 FROM shop_emoji_queue"), [])

    async def test_fenced_write_refuses_a_stale_generation_but_dashboard_saves_stay_authoritative(self):
        pid = await self._fresh_product("Authority", url_for(60))
        row = await self._queue_row(pid)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE shop_items SET icon_url=? WHERE id=?", (url_for(61), pid))
            await db.commit()
            # Fenced to a generation that is not current: refused, nothing written.
            refused = await Q.enqueue_product(
                db, GUILD, pid, "n", url_for(61), expect_icon_url=url_for(61),
                expect_generation=row["generation"] + 5)
            self.assertIsNone(refused)
            self.assertEqual((await self._queue_row(pid))["generation"], row["generation"])
            # Fenced with "no row seen" must never replace an existing row.
            self.assertIsNone(await Q.enqueue_product(
                db, GUILD, pid, "n", url_for(61), expect_icon_url=url_for(61),
                expect_generation=None))
            # Fenced to a listing URL that is not what shop_items holds: refused.
            self.assertIsNone(await Q.enqueue_product(
                db, GUILD, pid, "n", url_for(61), expect_icon_url=url_for(99),
                expect_generation=row["generation"]))
            self.assertEqual((await self._queue_row(pid))["source_url"], row["source_url"])
            # The Dashboard's own (unfenced) save always wins, whatever generation was seen.
            self.assertEqual(await Q.enqueue_product(db, GUILD, pid, "n", url_for(61)),
                             Q.queue_source_url(url_for(61)))
        after = await self._queue_row(pid)
        self.assertEqual((after["source_url"], after["generation"]),
                         (Q.queue_source_url(url_for(61)), row["generation"] + 1))

    async def test_fenced_retraction_of_an_unusable_url_only_removes_the_row_it_saw(self):
        pid = await self._fresh_product("Retract", url_for(70))
        row = await self._queue_row(pid)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE shop_items SET icon_url=? WHERE id=?", ("not a url", pid))
            await db.commit()
            await Q.enqueue_product(db, GUILD, pid, "n", "not a url",
                                    expect_icon_url="not a url",
                                    expect_generation=row["generation"] + 3)
            self.assertIsNotNone(await self._queue_row(pid), "a stale generation must not delete")
            await Q.enqueue_product(db, GUILD, pid, "n", "not a url",
                                    expect_icon_url="not a url",
                                    expect_generation=row["generation"])
        self.assertIsNone(await self._queue_row(pid))

    async def test_fenced_writes_still_write_when_nothing_changed(self):
        pid = await self._fresh_product("Positive", url_for(50))
        old_row = await self._queue_row(pid)
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (url_for(51), pid))
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertTrue(await Q.requeue_if_current(db, old_row, "Positive", url_for(51)))
        row = await self._queue_row(pid)
        self.assertEqual((row["source_url"], row["generation"]),
                         (url_for(51), old_row["generation"] + 1))
        await self._exec("DELETE FROM shop_emoji_queue")
        async with aiosqlite.connect(DB_PATH) as db:
            result = await Q.enqueue_guild_products(db, GUILD)
        self.assertEqual(result["queued"], 1)
        self.assertEqual((await self._queue_row(pid))["source_url"], url_for(51))


class PostBoundaryReleaseTests(AutomationTestBase):
    """Block the list that really sits inside `_create_claimed()` — the POST boundary.

    Counting list calls is not enough (the first list of a pass belongs to
    `run_pass`, not to `_create_claimed`), so the gate keys on being inside
    `_create_claimed`. Each test asserts the gate engaged.
    """

    def _gate_inside_create_claimed(self, *, raise_once=None):
        state = {"inside": False, "hit": asyncio.Event(), "gate": asyncio.Event(), "raised": False}
        real_cc = SE._create_claimed

        async def wrapped(*args, **kwargs):
            state["inside"] = True
            try:
                return await real_cc(*args, **kwargs)
            finally:
                state["inside"] = False
        real_list = self.fake.list_emojis

        async def listing():
            if state["inside"]:
                if raise_once is not None and not state["raised"]:
                    state["raised"] = True
                    state["hit"].set()
                    raise raise_once
                if raise_once is None:
                    state["hit"].set()
                    await state["gate"].wait()
            return await real_list()
        patcher = patch.object(SE, "_create_claimed", wrapped)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake.list_emojis = listing
        return state

    async def _assets(self):
        return await self._all("SELECT status, emoji_id FROM shop_emoji_assets ORDER BY created_at")

    async def _usage(self):
        async with aiosqlite.connect(DB_PATH) as db:
            return await SE._guild_asset_usage(db, APP, GUILD)

    async def _setup(self, name, idx, color):
        self._add_product(name)
        pid = await self._product_id(name)
        self.images.put(url_for(idx), color)
        self._set_icon(pid, url_for(idx))
        return pid

    async def _stale_at_the_final_list(self, change):
        pid = await self._setup("Boundary Release", 60, (12, 24, 36, 255))
        self.images.put(url_for(61), (200, 10, 10, 255))
        state = self._gate_inside_create_claimed()
        task = asyncio.create_task(self._run_pass())
        await asyncio.wait_for(state["hit"].wait(), 30)
        change(pid)
        state["gate"].set()
        await asyncio.wait_for(task, 90)
        return pid

    async def test_stale_at_the_real_final_list_posts_nothing_and_releases_the_claim(self):
        pid = await self._stale_at_the_final_list(lambda pid: self._set_icon(pid, url_for(61)))
        self.assertEqual(self.fake.creates, 0, "URL A must never be POSTed")
        for status, _id in await self._assets():
            self.assertNotIn(status, {"preparing", "uncertain"},
                             "an abandoned, never-POSTed claim must not stay in a recovering state")
        self.assertEqual(await self._usage(), 0, "an unposted claim must not consume quota")

        # The newest URL converges, and returning to the abandoned URL needs no owner recovery.
        await self._drain()
        self.assertEqual(self.fake.creates, 1)
        self._set_icon(pid, url_for(60))
        reports = await self._drain()
        self.assertEqual(self.fake.creates, 2)
        self.assertEqual(sum(r["deferred"] for r in reports), 0,
                         "no duplicate-risk acknowledgement may be demanded for an emoji never POSTed")
        self.assertEqual(sorted(s for s, _ in await self._assets()), ["ready", "ready"])
        self.assertEqual(await self._usage(), len(self.fake.remote))

    async def test_cleared_image_at_the_real_final_list_releases_the_claim(self):
        await self._stale_at_the_final_list(lambda pid: self._set_icon(pid, ""))
        self.assertEqual(self.fake.creates, 0)
        self.assertEqual([s for s, _ in await self._assets() if s in {"preparing", "uncertain"}], [])
        self.assertEqual(await self._usage(), 0)

    async def test_transient_429_at_the_final_list_leaves_an_ordinarily_retryable_claim(self):
        pid = await self._setup("Boundary 429", 62, (1, 2, 3, 255))
        self._gate_inside_create_claimed(raise_once=SE.DiscordAPIError(
            "Discord API 429: rate limited", code="discord_api_error", status=429,
            http_status=429, retry_after=1.0))
        report = await self._run_pass()
        self.assertEqual((self.fake.creates, report["failed"]), (0, 1))
        self.assertEqual([s for s, _ in await self._assets() if s in {"preparing", "uncertain"}], [],
                         "a 429 before any POST must not look like an uncertain create")
        self.assertEqual(await self._usage(), 0)
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL, state='queued'")
        reports = await self._drain()
        self.assertEqual(self.fake.creates, 1)
        self.assertEqual(sum(r["deferred"] for r in reports), 0)
        self.assertEqual([s for s, _ in await self._assets()], ["ready"])

    async def test_owner_path_stale_at_the_real_final_list_posts_nothing(self):
        pid = await self._setup("Boundary Owner", 63, (4, 5, 6, 255))
        await self._exec("DELETE FROM shop_emoji_queue")
        self.images.put(url_for(64), (90, 90, 90, 255))
        state = self._gate_inside_create_claimed()
        task = asyncio.create_task(SE.prepare_product_emojis(GUILD, [pid]))
        await asyncio.wait_for(state["hit"].wait(), 30)
        self._set_icon(pid, url_for(64))
        state["gate"].set()
        result = (await asyncio.wait_for(task, 90))["results"][0]
        self.assertEqual(self.fake.creates, 0)
        self.assertNotEqual(result["status"], "ready")
        self.assertEqual([s for s, _ in await self._assets() if s in {"preparing", "uncertain"}], [])
        self.assertEqual(await self._usage(), 0)

    async def test_uncertain_recovery_stale_at_the_real_final_list_keeps_the_duplicate_guard(self):
        self.fake.create_timeout_indexes = {1}
        pid = await self._setup("Boundary Recovery", 65, (8, 9, 10, 255))
        await self._exec("DELETE FROM shop_emoji_queue")
        first = (await SE.prepare_product_emojis(GUILD, [pid]))["results"][0]
        self.assertEqual((first["status"], self.fake.creates), ("uncertain", 1))
        self.fake.remote.clear()          # Discord shows nothing: only a POST could recreate it
        await self._exec("UPDATE shop_emoji_assets SET quarantine_until=?",
                         (SE._iso(SE._utcnow() - SE.timedelta(seconds=1)),))
        self.images.put(url_for(66), (70, 70, 70, 255))
        state = self._gate_inside_create_claimed()
        task = asyncio.create_task(SE.prepare_product_emojis(
            GUILD, [pid], retry_uncertain=True, acknowledge_duplicate_risk=True))
        await asyncio.wait_for(state["hit"].wait(), 30)
        self._set_icon(pid, url_for(66))
        state["gate"].set()
        result = (await asyncio.wait_for(task, 90))["results"][0]
        self.assertEqual(self.fake.creates, 1, "recovery must not POST for a replaced image")
        self.assertNotEqual(result["status"], "ready")
        self.assertEqual([s for s, _ in await self._assets()], ["uncertain"],
                         "an asset with an earlier ambiguous POST must stay uncertain, never become failed")

    # ── release is only ever for a claim whose create request was never sent ──
    def _spy_release(self):
        calls = []
        real = SE._release_unposted_claim

        async def spy(db, asset, reason):
            calls.append({"creates_at_release": self.fake.creates, "origin": asset.get("claim_origin")})
            return await real(db, asset, reason)
        patcher = patch.object(SE, "_release_unposted_claim", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    async def _status_of(self, product_name):
        return (await self._one(
            "SELECT a.status FROM shop_emoji_assets a JOIN shop_emoji_url_aliases l "
            "ON l.application_id=a.application_id AND l.content_hash=a.content_hash "
            "JOIN shop_items i ON i.icon_url=l.source_url WHERE i.name=?", (product_name,)))[0]

    async def test_release_never_runs_once_a_create_request_was_dispatched(self):
        calls = self._spy_release()
        # 1. POST sent, response lost (timeout): may exist on Discord -> uncertain.
        self.fake.create_timeout_indexes = {1}
        await self._setup("Sent Timeout", 80, (1, 1, 1, 255))
        await self._run_pass()
        self.assertEqual((self.fake.creates, await self._status_of("Sent Timeout")), (1, "uncertain"))
        # 2. POST sent and rejected by Discord: the existing failure path, not a release.
        self.fake.create_failures = {2}
        await self._setup("Sent Rejected", 81, (2, 2, 2, 255))
        await self._run_pass()
        self.assertEqual(self.fake.creates, 2)
        self.assertEqual(await self._status_of("Sent Rejected"), "failed")
        # 3. POST sent and succeeded.
        await self._setup("Sent Ok", 82, (3, 3, 3, 255))
        await self._run_pass()
        self.assertEqual((self.fake.creates, await self._status_of("Sent Ok")), (3, "ready"))
        # 4. POST sent, then a local failure while recording it: the emoji exists, the
        #    claim must stay `preparing` (never released), and a later pass adopts it.
        real_adopt = SE._adopt_asset
        boom = {"armed": True}

        async def flaky(db, asset, candidate):
            if boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("local failure after the POST")
            return await real_adopt(db, asset, candidate)
        with patch.object(SE, "_adopt_asset", flaky):
            crash_pid = await self._setup("Sent Then Crash", 83, (4, 4, 4, 255))
            await self._run_pass()
        self.assertEqual((self.fake.creates, await self._status_of("Sent Then Crash")), (4, "preparing"))
        # Only THIS product's backoff is lifted (the rejected product above legitimately retries).
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL, state='queued' "
                         "WHERE product_id=?", (crash_pid,))
        before = len(self.fake.remote)
        await self._drain()
        self.assertEqual(self.fake.creates, 4, "recovery must adopt, never POST a duplicate")
        self.assertEqual(len(self.fake.remote), before)
        self.assertEqual(await self._status_of("Sent Then Crash"), "ready")
        self.assertEqual(calls, [], "a claim was released although a create request had been sent")

    async def test_release_happens_only_before_any_create_and_with_the_right_origin(self):
        calls = self._spy_release()
        await self._stale_at_the_final_list(lambda pid: self._set_icon(pid, url_for(61)))
        self.assertEqual([c["creates_at_release"] for c in calls], [0])
        self.assertEqual([c["origin"] for c in calls], ["new"])
        self.assertEqual(self.fake.creates, 0)

    async def test_release_cannot_touch_an_asset_another_process_completed_or_reclaimed(self):
        content = SE.content_identity(b"release-unit-probe")
        async with aiosqlite.connect(DB_PATH) as db:
            _, claimed, _ = await SE._insert_asset_or_claim(
                db, APP, GUILD, content, [], BOT_ID, emoji_name="unit_probe")
            self.assertEqual(claimed["claim_origin"], "new")
            # Another process finished it (Discord had the emoji): ready must stay ready.
            await SE._adopt_asset(db, claimed, {"id": "1562903222086279111"})
            after = await SE._release_unposted_claim(db, claimed, "late")
            self.assertEqual((after["status"], after["emoji_id"]), ("ready", "1562903222086279111"))

            # A claim another worker took over (new attempt id) must not be released by the old holder.
            other = SE.content_identity(b"release-unit-probe-2")
            _, first, _ = await SE._insert_asset_or_claim(
                db, APP, GUILD, other, [], BOT_ID, emoji_name="unit_probe_two")
            await SE._mark_failed(db, first, "first holder gave up")
            failed_row = await SE._get_asset(db, APP, other)
            second = await SE._claim_existing(db, failed_row, GUILD)
            self.assertEqual(second["claim_origin"], "recovered")
            self.assertNotEqual(second["attempt_id"], first["attempt_id"])
            still = await SE._release_unposted_claim(db, first, "stale holder")
            self.assertEqual((still["status"], still["attempt_id"]), ("preparing", second["attempt_id"]))

            # Release of a claim taken from `uncertain` returns to `uncertain` (duplicate guard kept).
            third = SE.content_identity(b"release-unit-probe-3")
            _, fresh, _ = await SE._insert_asset_or_claim(
                db, APP, GUILD, third, [], BOT_ID, emoji_name="unit_probe_three")
            await SE._mark_uncertain(db, fresh, "earlier POST may have reached Discord")
            await db.execute("UPDATE shop_emoji_assets SET quarantine_until=? WHERE content_hash=?",
                             (SE._iso(SE._utcnow() - SE.timedelta(seconds=1)), third))
            await db.commit()
            reclaimed = await SE._claim_existing(
                db, await SE._get_asset(db, APP, third), GUILD, allow_uncertain=True)
            self.assertEqual(reclaimed["claim_origin"], "uncertain")
            released = await SE._release_unposted_claim(db, reclaimed, "never sent this time")
            self.assertEqual(released["status"], "uncertain")
            self.assertIsNotNone(released["quarantine_until"])

            # A claim from a fresh row goes to `failed` with no attempt left behind.
            fourth = SE.content_identity(b"release-unit-probe-4")
            _, fresh4, _ = await SE._insert_asset_or_claim(
                db, APP, GUILD, fourth, [], BOT_ID, emoji_name="unit_probe_four")
            gone = await SE._release_unposted_claim(db, fresh4, "never sent")
            self.assertEqual((gone["status"], gone["attempt_id"], gone["emoji_id"]), ("failed", None, None))

    async def test_released_claim_leaves_alias_unresolvable_and_deletes_nothing(self):
        pid = await self._stale_at_the_final_list(lambda pid: self._set_icon(pid, url_for(61)))
        a_alias = await self._one("SELECT content_hash FROM shop_emoji_url_aliases WHERE source_url=?",
                                  (Q.queue_source_url(url_for(60)),))
        self.assertIsNotNone(a_alias, "the alias row is kept")
        self.assertEqual((await self._one(
            "SELECT status FROM shop_emoji_assets WHERE content_hash=?", (a_alias[0],)))[0], "failed")
        async with aiosqlite.connect(DB_PATH) as db:
            self.assertFalse(await Q._resolves_ready(db, APP, GUILD, Q.queue_source_url(url_for(60))),
                             "an alias to a non-ready asset must not resolve")
        self.assertEqual((self.fake.creates, self.fake.deletes, self.fake.patch_requests), (0, 0, 0))

    async def test_a_shared_image_released_by_one_product_is_prepared_once_for_the_other(self):
        first_pid = {}
        real_row = W._process_row

        async def tracking(*args, **kwargs):
            first_pid.setdefault("pid", args[2]["product_id"])   # _process_row(db, discord, row, ...)
            return await real_row(*args, **kwargs)
        p1 = await self._setup("Shared One", 90, (11, 22, 33, 255))
        self._add_product("Shared Two")
        p2 = await self._product_id("Shared Two")
        self._set_icon(p2, url_for(90))
        self.images.put(url_for(91), (99, 88, 77, 255))
        state = self._gate_inside_create_claimed()
        with patch.object(W, "_process_row", tracking):
            task = asyncio.create_task(self._run_pass())
            await asyncio.wait_for(state["hit"].wait(), 30)
            # Whichever product holds the claim moves to another image; the other keeps A.
            self._set_icon(first_pid["pid"], url_for(91))
            state["gate"].set()
            await asyncio.wait_for(task, 90)
        await self._drain()
        self.assertEqual(self.fake.creates, 2, "image A once (for the product still using it), image B once")
        self.assertEqual(sorted(s for s, _ in await self._assets()), ["ready", "ready"])
        self.assertEqual(await self._usage(), len(self.fake.remote))
        self.assertEqual(await self._all("SELECT 1 FROM shop_emoji_queue"), [])

    async def test_a_released_claim_cannot_be_retaken_past_the_guild_quota(self):
        pid = await self._stale_at_the_final_list(lambda pid: self._set_icon(pid, url_for(61)))
        await self._drain()                                   # newest image (B) converges
        creates_before = self.fake.creates
        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(SE.GUILD_ASSET_LIMIT):
                await db.execute(
                    "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,animated,"
                    "creator_guild_id,pipeline_version,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,0,?,?,'ready',?,?)",
                    (APP, SE.content_identity(f"quota-{i}".encode()), f"quota_{i}", str(2000000000000000000 + i),
                     GUILD, SE.PIPELINE_VERSION, SE._iso(), SE._iso()))
            await db.commit()
        self._set_icon(pid, url_for(60))                      # back to the released image
        await self._drain()
        self.assertEqual(self.fake.creates, creates_before, "the quota must still block the re-claim")


class PreDispatchCancellationTests(AutomationTestBase):
    """Cancel (or raise) at every await between claim acquisition and the POST.

    A claim must be handed back only when no create request can have been
    dispatched, and must be left to the uncertain/recovery machinery the moment
    one may have been. Each point is entered by gating a real function the worker
    really calls; every test asserts the gate engaged.
    """

    POINTS = ("claim_acquisition", "alias_write", "validator_after_claim",
              "final_list", "validator_at_boundary")

    async def _setup(self, name, idx, color):
        self._add_product(name)
        pid = await self._product_id(name)
        self.images.put(url_for(idx), color)
        self._set_icon(pid, url_for(idx))
        return pid

    async def _statuses(self):
        return [r[0] for r in await self._all("SELECT status FROM shop_emoji_assets")]

    async def _usage(self):
        async with aiosqlite.connect(DB_PATH) as db:
            return await SE._guild_asset_usage(db, APP, GUILD)

    async def _claim_exists(self):
        """True once THIS point's attempt holds a claim (orphans from earlier points don't count)."""
        rows = await self._all("SELECT attempt_id FROM shop_emoji_assets WHERE status='preparing'")
        return any(r[0] not in self._known_attempts for r in rows)

    def _install(self, stack, point, mode):
        """Patch the real function for ``point``; it blocks forever (cancel) or raises."""
        hit, seen = asyncio.Event(), {"n": 0, "inside": False}

        async def trigger():
            hit.set()
            if mode == "raise":
                raise RuntimeError(f"injected failure at {point}")
            await asyncio.Event().wait()           # parked until the task is cancelled

        if point == "claim_acquisition":
            real = SE._get_asset

            async def get_asset(*a, **k):
                if not seen.get("done") and await self._claim_exists():
                    seen["done"] = True            # first read after the claim was written
                    await trigger()
                return await real(*a, **k)
            stack.enter_context(patch.object(SE, "_get_asset", get_asset))
        elif point == "alias_write":
            real = SE._upsert_alias

            async def upsert(*a, **k):
                if not seen.get("done") and await self._claim_exists():
                    seen["done"] = True
                    await trigger()
                return await real(*a, **k)
            stack.enter_context(patch.object(SE, "_upsert_alias", upsert))
        elif point in ("validator_after_claim", "validator_at_boundary"):
            real = SE._guard_source
            want = 1 if point == "validator_after_claim" else 2

            async def guard(validate):
                if await self._claim_exists():
                    seen["n"] += 1
                    if seen["n"] == want:
                        await trigger()
                return await real(validate)
            stack.enter_context(patch.object(SE, "_guard_source", guard))
        else:                                      # final_list: the list inside _create_claimed
            real_cc, real_list = SE._create_claimed, self.fake.list_emojis

            async def cc(*a, **k):
                seen["inside"] = True
                try:
                    return await real_cc(*a, **k)
                finally:
                    seen["inside"] = False

            async def listing():
                if seen["inside"] and not seen.get("done"):
                    seen["done"] = True
                    await trigger()
                return await real_list()
            stack.enter_context(patch.object(SE, "_create_claimed", cc))
            self.fake.list_emojis = listing
        return hit

    async def _run_point(self, point, mode, idx):
        import contextlib
        pid = await self._setup(f"Pre {point} {mode}", idx, (idx % 250, 40, 90, 255))
        base_creates, base_usage = self.fake.creates, await self._usage()
        self._known_attempts = {r[0] for r in await self._all(
            "SELECT attempt_id FROM shop_emoji_assets WHERE status='preparing'")}
        recovering = lambda statuses: len([x for x in statuses if x in {"preparing", "uncertain"}])
        base_recovering = recovering(await self._statuses())   # judge this point on its own
        with contextlib.ExitStack() as stack:
            hit = self._install(stack, point, mode)
            task = asyncio.create_task(self._run_pass())
            if mode == "cancel":
                await asyncio.wait_for(hit.wait(), 30)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                await asyncio.wait_for(task, 60)
                self.assertTrue(hit.is_set(), "the injected failure never ran")
        label = f"{point}/{mode}"
        self.assertEqual(self.fake.creates, base_creates, f"{label}: nothing may be POSTed")
        self.assertEqual(recovering(await self._statuses()), base_recovering,
                         f"{label}: an undispatched claim must not stay recovering")
        self.assertEqual(await self._usage(), base_usage, f"{label}: quota must be released")
        if mode == "raise":                         # the worker's own retry backoff
            await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL, state='queued' "
                             "WHERE product_id=?", (pid,))
        reports = await self._drain()
        self.assertEqual(self.fake.creates, base_creates + 1, f"{label}: must converge with one create")
        self.assertEqual(sum(r["deferred"] for r in reports), 0,
                         f"{label}: no acknowledgement may be demanded for a never-sent create")
        self.assertEqual(await self._all("SELECT 1 FROM shop_emoji_queue WHERE product_id=?", (pid,)), [],
                         f"{label}: queue row stuck")
        self.assertEqual(await self._usage(), base_usage + 1)

    async def test_cancellation_at_every_pre_dispatch_point_releases_the_claim(self):
        for n, point in enumerate(self.POINTS):
            with self.subTest(point=point, mode="cancel"):
                await self._run_point(point, "cancel", 110 + n)

    async def test_an_exception_at_every_pre_dispatch_point_releases_the_claim(self):
        for n, point in enumerate(self.POINTS):
            with self.subTest(point=point, mode="raise"):
                await self._run_point(point, "raise", 130 + n)

    async def _cancel_during_post(self, completes_remotely, idx):
        """Park inside create_emoji(), i.e. AFTER dispatch began, then cancel."""
        pid = await self._setup("Post Cancel", idx, (idx % 250, 90, 40, 255))
        hit = asyncio.Event()
        real_create = self.fake.create_emoji

        async def create(name, png, bot_user_id):
            if completes_remotely:
                await real_create(name, png, bot_user_id)   # Discord created it...
            hit.set()
            await asyncio.Event().wait()                     # ...but we never saw the response

        self.fake.create_emoji = create
        task = asyncio.create_task(self._run_pass())
        await asyncio.wait_for(hit.wait(), 30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.fake.create_emoji = real_create
        return pid

    async def test_guard_contract_a_dispatched_claim_is_never_released_and_release_happens_once(self):
        async with aiosqlite.connect(DB_PATH) as db:
            async def claim(tag):
                h = SE.content_identity(f"guard-contract-{tag}".encode())
                _, c, _ = await SE._insert_asset_or_claim(db, APP, GUILD, h, [], BOT_ID,
                                                          emoji_name=f"guard_{tag}")
                return h, c

            # Before dispatch: cancellation releases the claim.
            h1, c1 = await claim("a")
            with self.assertRaises(asyncio.CancelledError):
                async with SE._PreDispatch(db, c1):
                    raise asyncio.CancelledError()
            self.assertEqual((await SE._get_asset(db, APP, h1))["status"], "failed")

            # After dispatch began: the very same exit must leave the claim untouched.
            h2, c2 = await claim("b")
            c2["dispatch_started"] = True
            for exc in (asyncio.CancelledError(), RuntimeError("late"), SE.StalePreparation()):
                with self.assertRaises(type(exc)):
                    async with SE._PreDispatch(db, c2):
                        raise exc
            kept = await SE._get_asset(db, APP, h2)
            self.assertEqual((kept["status"], kept["attempt_id"]), ("preparing", c2["attempt_id"]))

            # Nested guards over one claim release it exactly once.
            h3, c3 = await claim("c")
            calls = []
            real = SE._release_unposted_claim

            async def counting(*a, **k):
                calls.append(1)
                return await real(*a, **k)
            with patch.object(SE, "_release_unposted_claim", counting):
                with self.assertRaises(RuntimeError):
                    async with SE._PreDispatch(db, c3):
                        async with SE._PreDispatch(db, c3):
                            raise RuntimeError("boom")
            self.assertEqual(len(calls), 1)

    async def test_a_second_cancellation_during_claim_release_cannot_abandon_the_cleanup(self):
        """Cancel once (release starts), cancel again while it runs: the release must still finish."""
        import contextlib
        pid = await self._setup("Double Cancel", 160, (160, 40, 40, 255))
        started, gate = asyncio.Event(), asyncio.Event()
        real_release = SE._release_unposted_claim

        async def slow_release(*a, **k):
            started.set()
            await gate.wait()                      # holds the release open between the two cancels
            return await real_release(*a, **k)
        task = None
        try:
            with contextlib.ExitStack() as stack:
                hit = self._install(stack, "final_list", "cancel")
                stack.enter_context(patch.object(SE, "_release_unposted_claim", slow_release))
                task = asyncio.create_task(self._run_pass())
                await asyncio.wait_for(hit.wait(), 30)
                task.cancel()                                          # 1st: release begins
                await asyncio.wait_for(started.wait(), 30)
                task.cancel()                                          # 2nd: parent is waiting on it
                await asyncio.sleep(0.05)
                self.assertFalse(task.done(), "the parent left while its cleanup was still running")
                gate.set()
                with self.assertRaises(asyncio.CancelledError):        # cancellation is not swallowed
                    await asyncio.wait_for(task, 30)
        finally:
            gate.set()
            await asyncio.sleep(0.05)
        self.assertEqual([s for s in await self._statuses() if s in {"preparing", "uncertain"}], [],
                         "the claim must be released before the connection is closed")
        self.assertEqual((self.fake.creates, await self._usage()), (0, 0))

    async def test_finish_cleanup_survives_repeated_cancellation_and_still_raises_it(self):
        done = []

        async def cleanup():
            await asyncio.sleep(0.05)
            done.append("finished")
            return "result"
        task = asyncio.create_task(SE._finish_cleanup(cleanup()))
        await asyncio.sleep(0)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.005)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(done, ["finished"])
        # Without cancellation it behaves like a plain await, result and exceptions included.
        self.assertEqual(await SE._finish_cleanup(cleanup()), "result")

        async def failing():
            raise ValueError("cleanup failed")
        with self.assertRaises(ValueError):
            await SE._finish_cleanup(failing())

        # A cancellation that arrived while the cleanup then failed is still not swallowed.
        async def fails_late():
            await asyncio.sleep(0.03)
            raise ValueError("late failure")
        task = asyncio.create_task(SE._finish_cleanup(fails_late()))
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_cancellation_after_dispatch_never_releases_an_unrecorded_request(self):
        pid = await self._cancel_during_post(False, 150)
        self.assertEqual(await self._statuses(), ["preparing"],
                         "a request may have been sent: the claim must NOT be released")
        # Recovery: absence from one listing is no proof; it ages into uncertain, never re-POSTs blindly.
        await self._exec("UPDATE shop_emoji_assets SET attempt_started_at='2000-01-01T00:00:00+00:00' "
                         "WHERE status='preparing'")
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL, state='queued' "
                         "WHERE product_id=?", (pid,))
        reports = await self._drain()
        self.assertEqual(self.fake.creates, 0, "no blind second POST after a possibly-sent one")
        self.assertEqual(await self._statuses(), ["uncertain"])
        self.assertGreaterEqual(sum(r["deferred"] for r in reports), 1)

    async def test_cancellation_after_a_completed_request_is_adopted_not_duplicated(self):
        pid = await self._cancel_during_post(True, 151)
        self.assertEqual(await self._statuses(), ["preparing"])
        self.assertEqual(len(self.fake.remote), 1, "Discord did create the emoji")
        await self._exec("UPDATE shop_emoji_queue SET next_attempt_at=NULL, state='queued' "
                         "WHERE product_id=?", (pid,))
        await self._drain()
        self.assertEqual(len(self.fake.remote), 1, "recovery must adopt, never POST a duplicate")
        self.assertEqual(await self._statuses(), ["ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
