"""reaction_roles: allow the same role on several Studio buttons without losing data.

SQLite cannot drop a PRIMARY KEY, so an existing database needs a table rebuild.
These tests pin what that rebuild must guarantee. They only ever touch temporary
database files, never a real project database.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import phase1_support  # noqa: E402,F401  (sets the required env)
import aiosqlite  # noqa: E402
import database  # noqa: E402

# The exact table definition production databases have today (main @ 701f15d).
LEGACY_DDL = """
CREATE TABLE reaction_roles (
    guild_id         INTEGER,
    channel_id       INTEGER,
    message_id       INTEGER,
    button_label     TEXT,
    button_emoji     TEXT,
    button_color     TEXT DEFAULT 'blurple',
    role_id          INTEGER,
    booster_only     INTEGER DEFAULT 0,
    required_role_id INTEGER DEFAULT NULL,
    PRIMARY KEY (message_id, role_id)
)"""
LEGACY_ROWS = [
    (1, 10, 100, "Red", "🔴", "red", 501, 0, None),
    (1, 10, 100, "Blue", None, "blurple", 502, 1, 900),
    (1, 11, 200, "Green", "🟢", "green", 501, 0, None),
    (2, 20, 300, None, None, None, 777, 0, None),
]
DATA_COLS = ("guild_id, channel_id, message_id, button_label, button_emoji, "
             "button_color, role_id, booster_only, required_role_id")


def make_legacy_db(path, with_new_columns=False):
    c = sqlite3.connect(path)
    c.execute(LEGACY_DDL)
    if with_new_columns:  # databases the ZIP's cog already ALTERed
        for ddl in ("row_index INTEGER DEFAULT 0", "button_index INTEGER DEFAULT 0",
                    "custom_id TEXT DEFAULT NULL"):
            c.execute(f"ALTER TABLE reaction_roles ADD COLUMN {ddl}")
    c.executemany(f"INSERT INTO reaction_roles ({DATA_COLS}) VALUES (?,?,?,?,?,?,?,?,?)", LEGACY_ROWS)
    c.execute("CREATE TABLE reaction_role_panels (message_id INTEGER PRIMARY KEY, guild_id INTEGER, "
              "exclusive INTEGER DEFAULT 0, max_roles INTEGER DEFAULT 0, require_confirmation INTEGER DEFAULT 0)")
    c.execute("INSERT INTO reaction_role_panels VALUES (100, 1, 1, 2, 0)")
    c.commit()
    c.close()


def run(coro):
    return asyncio.run(coro)


class TestReactionRolesMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "legacy.db")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, path=None):
        c = sqlite3.connect(path or self.path)
        try:
            return c.execute(f"SELECT {DATA_COLS} FROM reaction_roles ORDER BY id").fetchall()
        finally:
            c.close()

    def test_01_every_row_and_value_is_preserved(self):
        make_legacy_db(self.path)
        run(database.migrate_reaction_roles(self.path))
        self.assertEqual(self.rows(), LEGACY_ROWS)
        c = sqlite3.connect(self.path)
        self.assertEqual([r[0] for r in c.execute("SELECT id FROM reaction_roles ORDER BY id")], [1, 2, 3, 4])
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        c.close()

    def test_02_same_role_may_repeat_when_custom_ids_differ(self):
        make_legacy_db(self.path)
        run(database.migrate_reaction_roles(self.path))
        c = sqlite3.connect(self.path)
        for idx in (0, 1):
            c.execute("INSERT INTO reaction_roles (guild_id, channel_id, message_id, role_id, custom_id, button_index) "
                      "VALUES (1, 10, 400, 501, ?, ?)", (f"rr_501_400_0_{idx}", idx))
        c.commit()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM reaction_roles WHERE message_id=400").fetchone()[0], 2)
        c.close()

    def test_03_legacy_insert_or_replace_still_replaces(self):
        """The /rr command writes rows without a custom_id and relies on REPLACE."""
        make_legacy_db(self.path)
        run(database.migrate_reaction_roles(self.path))
        c = sqlite3.connect(self.path)
        c.execute(f"INSERT OR REPLACE INTO reaction_roles ({DATA_COLS}) VALUES (1, 10, 100, 'Crimson', NULL, 'red', 501, 0, NULL)")
        c.commit()
        n = c.execute("SELECT COUNT(*) FROM reaction_roles WHERE message_id=100 AND role_id=501").fetchone()[0]
        label = c.execute("SELECT button_label FROM reaction_roles WHERE message_id=100 AND role_id=501").fetchone()[0]
        c.close()
        self.assertEqual((n, label), (1, "Crimson"))

    def test_04_second_run_changes_nothing(self):
        make_legacy_db(self.path)
        self.assertTrue(run(database.migrate_reaction_roles(self.path)))
        before = self.rows()
        self.assertFalse(run(database.migrate_reaction_roles(self.path)))
        self.assertEqual(self.rows(), before)

    def test_05_database_already_altered_by_the_cog_also_migrates(self):
        make_legacy_db(self.path, with_new_columns=True)
        run(database.migrate_reaction_roles(self.path))
        self.assertEqual(self.rows(), LEGACY_ROWS)

    def test_06_other_tables_are_untouched(self):
        make_legacy_db(self.path)
        run(database.migrate_reaction_roles(self.path))
        c = sqlite3.connect(self.path)
        self.assertEqual(c.execute("SELECT * FROM reaction_role_panels").fetchall(), [(100, 1, 1, 2, 0)])
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        self.assertNotIn("reaction_roles_rebuild", names)

    def test_07_fresh_database_has_the_same_shape_as_a_migrated_one(self):
        make_legacy_db(self.path)
        run(database.migrate_reaction_roles(self.path))
        fresh = os.path.join(self.tmp.name, "fresh.db")
        with mock.patch.object(database, "DB_PATH", fresh):
            run(database.init_db())

        def shape(p):
            c = sqlite3.connect(p)
            cols = [(r[1], r[2], r[4], r[5]) for r in c.execute("PRAGMA table_info(reaction_roles)")]
            idx = sorted((r[1], r[2]) for r in c.execute("PRAGMA index_list(reaction_roles)")
                         if not r[1].startswith("sqlite_autoindex"))
            c.close()
            return cols, idx
        self.assertEqual(shape(self.path), shape(fresh))

    def test_08_failure_midway_leaves_the_original_table_intact(self):
        make_legacy_db(self.path)
        real_execute = aiosqlite.Connection.execute

        async def failing(self_, sql, *a, **k):
            if str(sql).strip().upper() == "DROP TABLE REACTION_ROLES":
                raise sqlite3.OperationalError("injected failure")
            return await real_execute(self_, sql, *a, **k)

        with mock.patch.object(aiosqlite.Connection, "execute", failing):
            with self.assertRaises(sqlite3.OperationalError):
                run(database.migrate_reaction_roles(self.path))
        c = sqlite3.connect(self.path)
        cols = [r[1] for r in c.execute("PRAGMA table_info(reaction_roles)")]
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        data = c.execute(f"SELECT {DATA_COLS} FROM reaction_roles ORDER BY rowid").fetchall()
        c.close()
        self.assertNotIn("id", cols)
        self.assertNotIn("reaction_roles_rebuild", names)
        self.assertEqual(data, LEGACY_ROWS)

    def test_09_two_processes_racing_migrate_exactly_once(self):
        make_legacy_db(self.path)

        async def both():
            return await asyncio.gather(database.migrate_reaction_roles(self.path),
                                        database.migrate_reaction_roles(self.path))
        results = run(both())
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.rows(), LEGACY_ROWS)

    def test_10_missing_table_is_a_no_op(self):
        sqlite3.connect(self.path).close()
        self.assertFalse(run(database.migrate_reaction_roles(self.path)))


if __name__ == "__main__":
    unittest.main()
