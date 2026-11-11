"""SQLite WAL behaviour for the Shop emoji queue, at two different scopes.

What is actually exercised, so the evidence is not inflated:

``MultiprocessQueueTests`` - 6 tests, each starting ONE OR MORE SEPARATE
``python`` processes (``subprocess.Popen``) against a temp database:
  * a stale queue generation cannot complete newer intent written by another
    process (marker-file handshake, no sleeps);
  * two processes racing to finish one generation: exactly one wins;
  * two processes writing the same row concurrently: no "database is locked",
    no lost write;
  * ``_insert_asset_or_claim`` (``BEGIN IMMEDIATE``) from two processes on one
    content hash: exactly one owner, one row;
  * a SECOND process takes over an asset attempt; the stale worker's create
    cannot be adopted into the new attempt;
  * ``ensure_shop_emoji_schema`` from two processes on a legacy schema.

``WalTransactionSemanticsTests`` - 2 tests, same process, deliberately NOT
multi-process: two SQLite connections and, in one case, two threads, to pin the
library-level facts (a deferred read-then-write fails fast with
SQLITE_BUSY/BUSY_SNAPSHOT even with ``busy_timeout=5000``; ``BEGIN IMMEDIATE``
waits for the holder and then observes its committed value).

What this file does NOT prove:
  * that Railway's runtime behaves the same - the deployed ``libsqlite3``
    version is not measured here (see SHOP_EMOJI_AUTOMATION.md);
  * anything about production data volumes, WAL file growth, or checkpoint
    latency under real load;
  * that the SQLite WAL-RESET corruption bug (fixed in 3.51.3) is absent: these
    tests never reproduce that race, and a clean run says nothing about it.

No Discord call, no production database, no emoji is created or deleted; every
database is a temp file owned by the test.

Run:  python3 scripts/test_shop_emoji_multiprocess.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import asyncio
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OWNER_ID", "999999999")
os.environ.setdefault("SECRET_KEY", "multiprocess-scratch-0123456789abcdef")
os.environ.setdefault("NERO_ENVIRONMENT", "test")

GUILD, PRODUCT = 9101, 7
APP = "123456789012345678"
BOT_ID = "999999999999999999"
STORM_ITERATIONS = 60
CHILD_TIMEOUT = 120


# ── child process source ────────────────────────────────────────────────────
# Deliberately a single script driven by a role argument: the child imports the
# real queue module and touches only the temp database it is told about.
CHILD_SOURCE = r'''
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, {root!r})
os.environ["DATABASE_PATH"] = {path!r}

from utils import shop_emoji_queue as Q
from utils import shop_emoji as SE
import aiosqlite

PATH = {path!r}
READY = {ready!r}
GO = {go!r}
GUILD = {guild!r}
PRODUCT = {product!r}
ITERATIONS = {iterations!r}
CONTENT_HASH = {content_hash!r}


def touch(marker):
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("1")


def wait(marker, timeout=60.0):
    deadline = time.time() + timeout
    while not os.path.exists(marker):
        if time.time() > deadline:
            raise SystemExit("timed out waiting for " + marker)
        time.sleep(0.005)


async def main(role):
    if role == "fenced_done":
        # Read the intent under the OLD generation, then let the parent write a
        # NEWER generation, then try to complete the stale snapshot.
        async with aiosqlite.connect(PATH, timeout=5) as db:
            row = await Q.descriptor(db, GUILD, PRODUCT)
        touch(READY)
        wait(GO)
        async with aiosqlite.connect(PATH, timeout=5) as db:
            done = await Q.mark_done(db, row)
        print(json.dumps({{"role": role, "mark_done": done,
                          "generation": row["generation"]}}))
        return

    if role == "contended_done":
        # Two processes try to finish the SAME generation at the same instant.
        async with aiosqlite.connect(PATH, timeout=5) as db:
            row = await Q.descriptor(db, GUILD, PRODUCT)
        touch(READY)
        wait(GO)
        async with aiosqlite.connect(PATH, timeout=5) as db:
            done = await Q.mark_done(db, row) if row else False
        print(json.dumps({{"role": role, "mark_done": done}}))
        return

    touch(READY) if role in ("storm", "claim") else None

    if role == "storm":
        # Concurrent writers on the same row: enqueue (generation bump),
        # claim (attempt count) and a backed-off retry must never raise
        # "database is locked" and must never lose a write.
        ok = 0
        errors = []
        async with aiosqlite.connect(PATH, timeout=5) as db:
            await Q.ensure_schema(db)
            for index in range(ITERATIONS):
                try:
                    await Q.enqueue_product(
                        db, GUILD, PRODUCT, "storm product",
                        "https://cdn.example.com/storm-%d.png" % index)
                    row = await Q.descriptor(db, GUILD, PRODUCT)
                    if row and await Q.record_attempt(db, row):
                        await Q.defer_without_attempt(db, row, "rate_limited", 1)
                    ok += 1
                except Exception as exc:               # noqa: BLE001
                    errors.append(type(exc).__name__ + ": " + str(exc))
        print(json.dumps({{"role": role, "ok": ok, "errors": errors}}))
        return

    if role == "claim":
        # The real BEGIN IMMEDIATE claim path of the asset pipeline, from a
        # second process, on the same file and the same content hash.
        created = 0
        errors = []
        async with aiosqlite.connect(PATH, timeout=5) as db:
            for index in range(ITERATIONS):
                try:
                    asset, claimed, _duplicates = await SE._insert_asset_or_claim(
                        db, {app!r}, GUILD, {app!r} and "c" * 64, [], None,
                        emoji_name="shop_c" + str(index % 3))
                    created += 1 if claimed else 0
                except Exception as exc:               # noqa: BLE001
                    errors.append(type(exc).__name__ + ": " + str(exc))
        print(json.dumps({{"role": role, "claimed": created, "errors": errors}}))
        return

    if role == "takeover_claim":
        # A SECOND process performs the takeover of a stale attempt: it ages the
        # claim (as the read-only reconcile does) and then re-claims the same
        # content under a brand-new attempt id.
        async with aiosqlite.connect(PATH, timeout=5) as db:
            stale = await SE._get_asset(db, {app!r}, CONTENT_HASH)
            aged = await SE._mark_uncertain(db, stale, "claim aged by another process")
            claimed = await SE._claim_existing(db, aged, GUILD, allow_uncertain=True)
        print(json.dumps({{"role": role, "claimed": bool(claimed),
                          "attempt": (claimed or {{}}).get("attempt_id"),
                          "status": (claimed or {{}}).get("status")}}))
        return

    if role == "ensure_schema":
        # Two processes initialising ONE file at the same time: the Dashboard and
        # the Bot really do start together on a deploy, and these additive
        # migrations run from init_db() in BOTH of them.
        async with aiosqlite.connect(PATH, timeout=5) as db:
            await SE.ensure_shop_emoji_schema(db)
            await db.commit()
        print(json.dumps({{"role": role, "ok": True}}))
        return

    raise SystemExit("unknown role " + role)


asyncio.run(main(sys.argv[1]))
'''


def _wait_for(markers, timeout=CHILD_TIMEOUT):
    """Block until every marker file exists (deterministic, no sleeps)."""
    deadline = time.time() + timeout
    for marker in markers:
        while not Path(marker).exists():
            assert time.time() < deadline, "child never reached its marker: " + str(marker)
            time.sleep(0.005)


def _run_children(path, roles, ready=None, go=None, app=None, handshake=True,
                  content_hash=""):
    """Start every role as an INDEPENDENT process and return parsed results."""
    folder = Path(path).parent
    ready_paths = [str(folder / f"ready-{index}") for index in range(len(roles))]
    go = str(go) if go else ""
    env = dict(os.environ, DATABASE_PATH=str(path), PYTHONPATH=str(ROOT))
    children = [subprocess.Popen(
        [sys.executable, "-c", CHILD_SOURCE.format(
            root=str(ROOT), path=str(path), ready=ready_paths[index], go=go,
            guild=GUILD, product=PRODUCT, iterations=STORM_ITERATIONS,
            app=app or "123456789012345678", content_hash=content_hash), role],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for index, role in enumerate(roles)]
    if go and handshake:
        # Every child holds its snapshot (or is in place) before one write
        # releases them together: the contention is created, not hoped for.
        _wait_for(ready_paths)
        Path(go).touch()
    results = []
    for child in children:
        out, err = child.communicate(timeout=CHILD_TIMEOUT)
        assert child.returncode == 0, f"child failed: {err}"
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


class _CreateOnlyFake:
    """The two Discord calls `_create_claimed` makes, nothing else."""

    def __init__(self, application_id, name):
        self.application_id = application_id
        self.remote = []
        self.created = []
        self._name = name

    async def list_emojis(self):
        return list(self.remote)

    async def create_emoji(self, name, final_png, bot_user_id):
        emoji = {"id": "7001", "name": name, "animated": False,
                 "application_id": self.application_id, "user": {"id": bot_user_id}}
        self.created.append(emoji)
        self.remote.append(emoji)
        return emoji


class MultiprocessQueueTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="shop-emoji-mp-")
        self.path = Path(self._folder.name) / "queue.db"
        self.ready = Path(self._folder.name) / "ready"
        self.go = Path(self._folder.name) / "go"
        self._create_queue()

    def tearDown(self):
        self._folder.cleanup()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_queue(self):
        # The queue schema exactly as ``Q.SCHEMA`` defines it, in WAL mode, the
        # way production gets it from ``init_db``.
        sys.path.insert(0, str(ROOT))
        from utils import shop_emoji_queue as Q  # local import: read-only use
        with sqlite3.connect(str(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            for statement in Q.SCHEMA:
                db.execute(statement)
            db.execute(
                "INSERT INTO shop_emoji_queue (guild_id,product_id,item_name,source_url,"
                "reason,state,generation) VALUES (?,?,?,?,?,'queued',1)",
                (GUILD, PRODUCT, "fenced product", "https://cdn.example.com/a.png",
                 "test"))
            db.commit()

    def _row(self):
        with self._connect() as db:
            row = db.execute("SELECT generation,state,attempts FROM shop_emoji_queue "
                             "WHERE guild_id=? AND product_id=?", (GUILD, PRODUCT)).fetchone()
        return dict(row) if row else None

    def _spawn(self, role, ready=None):
        source = CHILD_SOURCE.format(
            root=str(ROOT), path=str(self.path), ready=str(ready or self.ready),
            go=str(self.go), guild=GUILD, product=PRODUCT,
            iterations=STORM_ITERATIONS, app=APP, content_hash="")
        return subprocess.Popen(
            [sys.executable, "-c", source, role],
            env=dict(os.environ, DATABASE_PATH=str(self.path), PYTHONPATH=str(ROOT)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _integrity(self):
        with self._connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    # ── 1. a stale snapshot cannot complete newer intent (fencing) ─────────
    def test_a_second_process_cannot_complete_a_stale_generation(self):
        from utils import shop_emoji_queue as Q

        child = self._spawn("fenced_done")
        # Deterministic handshake: the child has READ the row (generation 1) and
        # is waiting. Nothing here is a sleep-based guess.
        deadline = time.time() + CHILD_TIMEOUT
        while not self.ready.exists():
            self.assertLess(time.time(), deadline, "the child never read its snapshot")
            time.sleep(0.005)

        # A newer intent (generation 2) is committed by ANOTHER process.
        async def newer_intent():
            import aiosqlite
            async with aiosqlite.connect(str(self.path)) as db:
                await Q.enqueue_product(db, GUILD, PRODUCT, "fenced product",
                                        "https://cdn.example.com/b.png")
        asyncio.run(newer_intent())
        self.go.touch()

        out, err = child.communicate(timeout=CHILD_TIMEOUT)
        self.assertEqual(child.returncode, 0, err)
        result = json.loads(out.strip().splitlines()[-1])

        self.assertEqual(result["generation"], 1)
        self.assertFalse(result["mark_done"],
                         "a worker holding generation 1 deleted generation 2's intent")
        row = self._row()
        self.assertIsNotNone(row, "the newer intent was destroyed by a stale worker")
        self.assertEqual(row["generation"], 2)
        self.assertEqual(row["state"], "queued")
        self._integrity()

    # ── 2. two processes racing to finish the same generation ──────────────
    def test_only_one_process_can_finish_one_generation(self):
        results = _run_children(self.path, ["contended_done", "contended_done"],
                                go=self.go)
        self.assertEqual(sorted(r["mark_done"] for r in results), [False, True],
                         "both processes completed the same generation")
        self.assertIsNone(self._row())
        self._integrity()

    # ── 3. concurrent writers never lose a write or raise BUSY ─────────────
    def test_concurrent_writers_neither_block_nor_lose_rows(self):
        results = _run_children(self.path, ["storm", "storm"])
        for result in results:
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["ok"], STORM_ITERATIONS)
        row = self._row()
        self.assertIsNotNone(row, "the contended row vanished")
        self.assertGreater(row["generation"], 1)
        self.assertEqual(self._row()["state"], "retry")
        self._integrity()

    # ── 4. the real BEGIN IMMEDIATE claim, from two processes ──────────────
    def test_begin_immediate_claim_has_exactly_one_winner_per_content(self):
        app = "123456789012345678"
        with sqlite3.connect(str(self.path)) as db:
            for statement in (
                """
                CREATE TABLE IF NOT EXISTS shop_emoji_assets (
                    application_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    emoji_name TEXT NOT NULL, emoji_id TEXT,
                    animated INTEGER NOT NULL DEFAULT 0, creator_guild_id INTEGER,
                    pipeline_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('preparing','ready','uncertain','missing','failed')),
                    attempt_id TEXT, attempt_started_at TEXT, quarantine_until TEXT,
                    last_error TEXT, managed_name INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (application_id, content_hash))
                """,
                """
                CREATE TABLE IF NOT EXISTS shop_emoji_url_aliases (
                    application_id TEXT NOT NULL, guild_id INTEGER NOT NULL,
                    source_url TEXT NOT NULL, content_hash TEXT NOT NULL,
                    recovery_ack_required INTEGER NOT NULL DEFAULT 0,
                    recovery_not_before TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (application_id, guild_id, source_url))
                """):
                db.execute(statement)
            db.commit()

        results = _run_children(self.path, ["claim", "claim"], app=app)
        for result in results:
            self.assertEqual(result["errors"], [])
        self.assertEqual(sum(r["claimed"] for r in results), 1,
                         "two processes claimed the same content hash")

        with self._connect() as db:
            assets = db.execute("SELECT COUNT(*) FROM shop_emoji_assets").fetchone()[0]
        self.assertEqual(assets, 1, "one content hash produced more than one asset row")
        self._integrity()


    # ── 5. a SECOND process takes over; the stale worker cannot write ──────
    def _create_asset_tables(self):
        with sqlite3.connect(str(self.path)) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS shop_emoji_assets (
                    application_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    emoji_name TEXT NOT NULL, emoji_id TEXT,
                    animated INTEGER NOT NULL DEFAULT 0, creator_guild_id INTEGER,
                    pipeline_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('preparing','ready','uncertain','missing','failed')),
                    attempt_id TEXT, attempt_started_at TEXT, quarantine_until TEXT,
                    last_error TEXT, managed_name INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (application_id, content_hash))
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS shop_emoji_url_aliases (
                    application_id TEXT NOT NULL, guild_id INTEGER NOT NULL,
                    source_url TEXT NOT NULL, content_hash TEXT NOT NULL,
                    recovery_ack_required INTEGER NOT NULL DEFAULT 0,
                    recovery_not_before TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (application_id, guild_id, source_url))
            """)
            db.commit()

    def _asset_row(self, content_hash):
        from utils import shop_emoji as SE
        with self._connect() as db:
            row = db.execute(
                f"SELECT {', '.join(SE._ASSET_FIELDS)} FROM shop_emoji_assets "
                "WHERE application_id=? AND content_hash=?", (APP, content_hash)).fetchone()
        return dict(row) if row else None

    def test_a_second_process_takes_over_and_the_stale_worker_cannot_write(self):
        """The takeover really happens in ANOTHER PROCESS.

        Process A claimed the content (attempt-A) and stalls. Process B ages the
        claim and re-claims it (new attempt id) - `_claim_existing`, the same
        path an explicit owner retry takes. Only then does A finish its create.
        A must not bind its result into B's attempt.
        """
        from utils import shop_emoji as SE

        self._create_asset_tables()
        content_hash = "d" * 64
        with sqlite3.connect(str(self.path)) as db:
            db.execute(
                "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,"
                "animated,creator_guild_id,pipeline_version,status,attempt_id,attempt_started_at) "
                "VALUES (?,?,?,0,?,?,'preparing','attempt-A',?)",
                (APP, content_hash, SE.deterministic_emoji_name(content_hash), GUILD,
                 SE.PIPELINE_VERSION, SE._iso()))
            db.commit()
        stale_asset = dict(self._asset_row(content_hash), claim_origin="new")

        results = _run_children(self.path, ["takeover_claim"], app=APP,
                                handshake=False, content_hash=content_hash)
        takeover = results[0]
        self.assertTrue(takeover["claimed"], takeover)
        self.assertEqual(takeover["status"], "preparing")
        self.assertNotEqual(takeover["attempt"], "attempt-A")
        self.assertEqual(self._asset_row(content_hash)["attempt_id"], takeover["attempt"],
                         "the child process did not own the row after the takeover")

        # Process A finally completes its POST, holding the pre-takeover snapshot.
        result = asyncio.run(self._stale_create(stale_asset, content_hash))
        self.assertEqual(result["code"], "late_completion", result)

        row = self._asset_row(content_hash)
        self.assertEqual(row["attempt_id"], takeover["attempt"],
                         "the stale worker overwrote the new owner's attempt")
        self.assertEqual(row["status"], "preparing")
        self.assertIsNone(row["emoji_id"],
                          "the stale worker bound its emoji into the new attempt")
        self._integrity()

    async def _stale_create(self, stale_asset, content_hash):
        from utils import shop_emoji as SE
        import aiosqlite
        fake = _CreateOnlyFake(APP, SE.deterministic_emoji_name(content_hash))
        async with aiosqlite.connect(str(self.path)) as db:
            return await SE._create_claimed(db, fake, stale_asset, b"final-normalized-png",
                                            BOT_ID)

    # ── 6. both processes may initialise the same file at once ────────────
    def test_two_processes_can_run_the_additive_migrations_together(self):
        """The emoji migrations specifically (``ensure_shop_emoji_schema``).

        Scope note: this exercises the tables THIS feature owns. Running the
        whole ``init_db()`` from two processes is a broader pre-existing concern
        (other migration modules use bare ALTER TABLE); it is recorded
        separately rather than fixed here.
        """
        legacy = Path(self._folder.name) / "legacy.db"
        with sqlite3.connect(str(legacy)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            # The shape this feature shipped BEFORE the queue/generation columns
            # and the managed_name/creator_guild_id attribution existed.
            db.execute("""
                CREATE TABLE shop_emoji_assets (
                    application_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    emoji_name TEXT NOT NULL, emoji_id TEXT,
                    animated INTEGER NOT NULL DEFAULT 0,
                    pipeline_version TEXT NOT NULL, status TEXT NOT NULL,
                    attempt_id TEXT, attempt_started_at TEXT, quarantine_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (application_id, content_hash))
            """)
            db.execute("""
                CREATE TABLE shop_emoji_queue (
                    guild_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL, source_url TEXT NOT NULL,
                    content_hash TEXT, reason TEXT NOT NULL DEFAULT 'backfill',
                    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    next_attempt_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    PRIMARY KEY (guild_id, product_id))
            """)
            db.execute("INSERT INTO shop_emoji_assets "
                       "(application_id,content_hash,emoji_name,pipeline_version,status) "
                       "VALUES ('123456789012345678',?, 'shop_legacy','v1','failed')",
                       ("b" * 64,))
            db.commit()

        for _attempt in range(3):
            results = _run_children(legacy, ["ensure_schema", "ensure_schema"],
                                    handshake=False)
            self.assertEqual([r["ok"] for r in results], [True, True])

        with sqlite3.connect(str(legacy)) as db:
            asset_columns = {row[1] for row in db.execute("PRAGMA table_info(shop_emoji_assets)")}
            queue_columns = {row[1] for row in db.execute("PRAGMA table_info(shop_emoji_queue)")}
            self.assertTrue({"creator_guild_id", "managed_name"} <= asset_columns)
            self.assertTrue({"state", "generation"} <= queue_columns, queue_columns)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM shop_emoji_assets").fetchone()[0], 1,
                "an existing asset row was lost by a concurrent migration")


class WalTransactionSemanticsTests(unittest.TestCase):
    """The deterministic two-connection case: why writes use BEGIN IMMEDIATE."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="shop-emoji-wal-")
        self.path = Path(self._folder.name) / "wal.db"

    def tearDown(self):
        self._folder.cleanup()

    def _setup(self):
        reader = sqlite3.connect(str(self.path), isolation_level=None, timeout=0)
        writer = sqlite3.connect(str(self.path), isolation_level=None, timeout=0)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        self.assertEqual(reader.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        reader.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value INT)")
        reader.execute("INSERT INTO t VALUES (1, 0)")
        return reader, writer

    def test_deferred_read_then_write_fails_fast_even_with_a_busy_timeout(self):
        reader, writer = self._setup()
        reader.execute("PRAGMA busy_timeout=5000")
        reader.execute("BEGIN")                       # DEFERRED: read snapshot now
        self.assertEqual(reader.execute("SELECT value FROM t").fetchone()[0], 0)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE t SET value=1")
        writer.execute("COMMIT")
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError) as error:
            reader.execute("UPDATE t SET value=2")
        self.assertLess(time.monotonic() - started, 1.0,
                        "the stale upgrade waited for the busy timeout instead of failing fast")
        self.assertIn(error.exception.sqlite_errorcode,
                      (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT))
        reader.execute("ROLLBACK")
        self.assertEqual(reader.execute("SELECT value FROM t").fetchone()[0], 1)

    def test_begin_immediate_waits_for_the_lock_instead_of_failing(self):
        """BEGIN IMMEDIATE queues on the write lock; it does not fail fast.

        This is why the claim paths use IMMEDIATE: the loser waits inside
        SQLite's busy handler and then sees the winner's committed row, instead
        of raising into the caller.
        """
        import threading

        reader, _unused = self._setup()
        reader.execute("PRAGMA busy_timeout=5000")
        holding, release, finished = threading.Event(), threading.Event(), threading.Event()

        def holder():
            # A connection belongs to the thread that created it (sqlite3's own
            # rule), so the writer opens its own here.
            connection = sqlite3.connect(str(self.path), isolation_level=None, timeout=5)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE t SET value=5")
                holding.set()
                release.wait(10)
                connection.execute("COMMIT")
            finally:
                connection.close()
                finished.set()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(holding.wait(10), "the writer never acquired the lock")
            timer = threading.Timer(0.2, release.set)   # hold it, then commit
            timer.start()                               # (a duration, not a race guess)
            started = time.monotonic()
            reader.execute("BEGIN IMMEDIATE")           # waits for the holder
            waited = time.monotonic() - started
            self.assertGreaterEqual(waited, 0.05,
                                    "the writer did not actually hold the lock")
            self.assertEqual(reader.execute("SELECT value FROM t").fetchone()[0], 5,
                             "the blocked writer did not observe the committed value")
            reader.execute("COMMIT")
            timer.cancel()
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertTrue(finished.is_set())

if __name__ == "__main__":
    unittest.main(verbosity=2)
