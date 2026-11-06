"""Schema acceptance for the Shop icon automation (#10).

Run exactly:
    PYTHONPATH=. python scripts/test_shop_emoji_migration.py

Proves, against the ACTUAL pre-feature schema (reconstructed from commit
e0ca738's DDL, i.e. before `managed_name` and before `shop_emoji_queue`):

  * the additive migration lands: `managed_name` and `shop_emoji_queue` exist
    exactly once, and existing asset IDs / names / hashes / aliases are intact;
  * repeated initialisation is idempotent (no second column, no error);
  * concurrent initialisation from separate processes is safe;
  * a queue table created by the *previous* revision of this module (without
    `state`/`generation`) is upgraded without losing rows.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OWNER = "999999999999"
SCRATCH = tempfile.TemporaryDirectory(prefix="shop-emoji-migration-", dir=tempfile.gettempdir())

# The asset table exactly as commit e0ca738 created it: no managed_name.
PRE_FEATURE_ASSETS = """
CREATE TABLE shop_emoji_assets (
    application_id       TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    emoji_name           TEXT NOT NULL,
    emoji_id             TEXT,
    animated             INTEGER NOT NULL DEFAULT 0,
    creator_guild_id     INTEGER,
    pipeline_version     TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (
        status IN ('preparing', 'ready', 'uncertain', 'missing', 'failed')
    ),
    attempt_id           TEXT,
    attempt_started_at   TEXT,
    quarantine_until     TEXT,
    last_error           TEXT,
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (application_id, content_hash)
)
"""
PRE_FEATURE_ALIASES = """
CREATE TABLE shop_emoji_url_aliases (
    application_id   TEXT NOT NULL,
    guild_id         INTEGER NOT NULL,
    source_url       TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    recovery_ack_required INTEGER NOT NULL DEFAULT 0,
    recovery_not_before TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (application_id, guild_id, source_url),
    FOREIGN KEY (application_id, content_hash)
        REFERENCES shop_emoji_assets(application_id, content_hash)
        ON UPDATE CASCADE ON DELETE RESTRICT
)
"""

APP = "123456789012345678"
GUILD = 8101
HASH_A = "a" * 64
HASH_B = "b" * 64
URL_A = "https://assets.example.com/a.png"
URL_B = "https://assets.example.com/b.png"


def _seed(path):
    """Write a database in the exact pre-feature shape, with real rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(PRE_FEATURE_ASSETS)
        conn.execute(PRE_FEATURE_ALIASES)
        conn.execute(
            "INSERT INTO shop_emoji_assets "
            "(application_id,content_hash,emoji_name,emoji_id,animated,creator_guild_id,"
            " pipeline_version,status) VALUES (?,?,?,?,0,?,?, 'ready')",
            (APP, HASH_A, "shop_" + HASH_A[:24], "1500000000000000001", GUILD, "rgba-128-png-v1"))
        conn.execute(
            "INSERT INTO shop_emoji_assets "
            "(application_id,content_hash,emoji_name,emoji_id,animated,creator_guild_id,"
            " pipeline_version,status,last_error) VALUES (?,?,?,NULL,0,?,?, 'failed','boom')",
            (APP, HASH_B, "shop_" + HASH_B[:24], GUILD, "rgba-128-png-v1"))
        conn.executemany(
            "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash) "
            "VALUES (?,?,?,?)",
            [(APP, GUILD, URL_A, HASH_A), (APP, GUILD, URL_B, HASH_B)])
        conn.commit()
    finally:
        conn.close()


def _snapshot(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assets = [dict(r) for r in conn.execute(
            "SELECT application_id,content_hash,emoji_name,emoji_id,status "
            "FROM shop_emoji_assets ORDER BY content_hash")]
        aliases = [tuple(r) for r in conn.execute(
            "SELECT application_id,guild_id,source_url,content_hash "
            "FROM shop_emoji_url_aliases ORDER BY content_hash")]
        return assets, aliases
    finally:
        conn.close()


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _count_objects(path, name):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name=?", (name,)).fetchone()
        return row[0]
    finally:
        conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(SCRATCH.name) / f"case-{self._testMethodName}.db")
        _seed(self.db)

    def _init(self, db=None):
        env = dict(os.environ)
        env.update(DATABASE_PATH=db or self.db, OWNER_ID=OWNER,
                   NERO_ENVIRONMENT="production", DISCORD_CLIENT_ID=APP,
                   DISCORD_TOKEN="")     # no startup network work
        proc = subprocess.run([sys.executable, "-c",
                               "import asyncio, database; asyncio.run(database.init_db())"],
                              cwd=str(ROOT), env=env, capture_output=True, text=True)
        return proc

    # ── the migration itself ────────────────────────────────────────────
    def test_migrates_pre_feature_schema_without_losing_rows(self):
        before_assets, before_aliases = _snapshot(self.db)
        proc = self._init()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertIn("managed_name", _columns(self.db, "shop_emoji_assets"))
        self.assertIn("generation", _columns(self.db, "shop_emoji_queue"))
        self.assertIn("state", _columns(self.db, "shop_emoji_queue"))
        self.assertIsNotNone(_columns(self.db, "shop_icon_backfill"))

        # Exactly once — no duplicated table/column artifacts.
        self.assertEqual(_count_objects(self.db, "shop_emoji_queue"), 1)
        self.assertEqual(_count_objects(self.db, "shop_icon_backfill"), 1)
        self.assertEqual(_columns(self.db, "shop_emoji_assets").count("managed_name"), 1)

        after_assets, after_aliases = _snapshot(self.db)
        self.assertEqual(before_assets, after_assets,
                         "asset ids, names, hashes and statuses must be unchanged")
        self.assertEqual(before_aliases, after_aliases,
                         "aliases must be unchanged")

    def test_existing_assets_are_marked_unmanaged(self):
        self._init()
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT managed_name FROM shop_emoji_assets ORDER BY content_hash").fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], [0, 0],
                         "a pre-existing name is never assumed to be policy-managed")

    def test_repeated_initialization_is_idempotent(self):
        for _ in range(3):
            proc = self._init()
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_columns(self.db, "shop_emoji_assets").count("managed_name"), 1)
        self.assertEqual(_count_objects(self.db, "shop_emoji_queue"), 1)
        before_assets, before_aliases = _snapshot(self.db)
        self.assertEqual(len(before_assets), 2)
        self.assertEqual(len(before_aliases), 2)

    def test_concurrent_initialization_from_separate_processes(self):
        env = dict(os.environ)
        env.update(DATABASE_PATH=self.db, OWNER_ID=OWNER, NERO_ENVIRONMENT="production",
                   DISCORD_CLIENT_ID=APP, DISCORD_TOKEN="")
        procs = [subprocess.Popen(
            [sys.executable, "-c",
             "import asyncio, database; asyncio.run(database.init_db())"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            results.append((proc.returncode, err))
        for code, err in results:
            self.assertEqual(code, 0, err)
        self.assertEqual(_columns(self.db, "shop_emoji_assets").count("managed_name"), 1)
        self.assertEqual(_count_objects(self.db, "shop_emoji_queue"), 1)

    def test_upgrades_the_previous_revision_queue_table(self):
        """A queue table without state/generation must be upgraded in place."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                """
                CREATE TABLE shop_emoji_queue (
                    guild_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL, source_url TEXT NOT NULL,
                    content_hash TEXT, reason TEXT NOT NULL DEFAULT 'backfill',
                    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    next_attempt_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, PRIMARY KEY (guild_id, product_id))
                """)
            conn.execute(
                "INSERT INTO shop_emoji_queue (guild_id,product_id,item_name,source_url,attempts) "
                "VALUES (?,?,?,?,2)", (GUILD, 7, "legacy queued", URL_A))
            conn.commit()
        finally:
            conn.close()

        proc = self._init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT item_name,attempts,state,generation FROM shop_emoji_queue "
                "WHERE guild_id=? AND product_id=?", (GUILD, 7)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "legacy queued", "the pending intent survives")
        self.assertEqual(row[1], 2, "progress is preserved")
        self.assertEqual(row[2], "queued", "a NULL next_attempt_at was immediately eligible")
        self.assertEqual(row[3], 1, "a migrated row starts at generation 1")


class DashboardInitTests(unittest.TestCase):
    def test_dashboard_process_initializes_the_same_way(self):
        db = str(Path(SCRATCH.name) / "dashboard-init.db")
        env = dict(os.environ)
        env.update(DATABASE_PATH=db, OWNER_ID=OWNER, NERO_ENVIRONMENT="production",
                   DISCORD_CLIENT_ID=APP, DISCORD_TOKEN="",
                   SECRET_KEY="x" * 40, DASHBOARD_DEMO_LOGIN="0")
        proc = subprocess.run([sys.executable, "-c", "import dashboard.app"],
                              cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("managed_name", _columns(db, "shop_emoji_assets"))
        self.assertEqual(_count_objects(db, "shop_emoji_queue"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
