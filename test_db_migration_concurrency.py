"""Regression coverage for concurrent additive database migrations."""
import sqlite3
import os
import subprocess
import sys
import unittest
from contextlib import closing
from unittest.mock import patch

import aiosqlite
from phase1_support import DB_PATH, reset_database, execute, rows
import database


class InitMigrationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    async def _race_column(self, table, column, ddl):
        execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        original = aiosqlite.Connection.execute
        fired = False

        def race(connection, sql, *args, **kwargs):
            nonlocal fired
            if (not fired and 'ALTER TABLE' in sql.upper()
                    and table.lower() in sql.lower()
                    and column.lower() in sql.lower()):
                fired = True
                with closing(sqlite3.connect(DB_PATH, timeout=5)) as winner:
                    winner.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')
                    winner.commit()
            return original(connection, sql, *args, **kwargs)

        with patch.object(aiosqlite.Connection, 'execute', race):
            await database.init_db()
        self.assertTrue(fired, 'race was not exercised')
        self.assertIn(column, {r[1] for r in rows(f'PRAGMA table_info({table})')})
        self.assertEqual(rows('PRAGMA integrity_check'), [('ok',)])

    async def test_boost_origin_duplicate_column_race(self):
        await self._race_column('leveling_active_boosts', 'origin_op_id', 'TEXT')

    async def test_purchase_attempts_duplicate_column_race(self):
        await self._race_column('shop_purchase_operations', 'delivery_rollback_attempts', 'INTEGER NOT NULL DEFAULT 0')

    async def test_duplicate_claim_without_column_is_not_accepted(self):
        async with aiosqlite.connect(':memory:') as db:
            await db.execute('CREATE TABLE example (id INTEGER)')
            original = db.execute

            def fail_alter(sql, *args, **kwargs):
                if sql.startswith('ALTER TABLE'):
                    raise sqlite3.OperationalError('duplicate column name: missing')
                return original(sql, *args, **kwargs)

            with patch.object(db, 'execute', fail_alter):
                with self.assertRaises(sqlite3.OperationalError):
                    await database.add_column_if_missing(db, 'example', 'missing', 'TEXT')

    async def test_unrelated_error_is_not_accepted(self):
        async with aiosqlite.connect(':memory:') as db:
            await db.execute('CREATE TABLE example (id INTEGER)')
            original = db.execute

            def fail_alter(sql, *args, **kwargs):
                if sql.startswith('ALTER TABLE'):
                    raise sqlite3.OperationalError('disk I/O error')
                return original(sql, *args, **kwargs)

            with patch.object(db, 'execute', fail_alter):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O'):
                    await database.add_column_if_missing(db, 'example', 'missing', 'TEXT')

    async def test_independent_processes_preserve_legacy_rows(self):
        for workers in (2, 6):
            with self.subTest(workers=workers):
                await reset_database()
                execute("INSERT INTO economy(guild_id,user_id,balance) VALUES (1,2,123)")
                execute("CREATE INDEX sentinel_economy_balance ON economy(balance)")
                execute("ALTER TABLE leveling_active_boosts DROP COLUMN origin_op_id")
                execute("ALTER TABLE shop_purchase_operations DROP COLUMN delivery_rollback_attempts")
                execute("DROP TABLE reaction_role_expiry")
                execute("CREATE TABLE reaction_role_expiry (guild_id INTEGER, user_id INTEGER, role_id INTEGER, expires_at TEXT, PRIMARY KEY(guild_id,user_id,role_id))")
                execute("INSERT INTO reaction_role_expiry VALUES (1,2,3,'2030-01-01')")
                code = "import asyncio,sys,database; sys.stdin.read(1); asyncio.run(database.init_db())"
                children = [subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy()) for _ in range(workers)]
                for child in children:
                    child.stdin.write(b'x')
                    child.stdin.flush()
                for child in children:
                    out, err = child.communicate(timeout=45)
                    self.assertEqual(child.returncode, 0, (out + err).decode())
                self.assertEqual(rows("SELECT balance FROM economy WHERE guild_id=1 AND user_id=2"), [(123,)])
                self.assertEqual(rows("SELECT guild_id,user_id,role_id,message_id FROM reaction_role_expiry"), [(1,2,3,0)])
                self.assertTrue(rows("SELECT name FROM sqlite_master WHERE type='index' AND name='sentinel_economy_balance'"))
                self.assertEqual(rows('PRAGMA integrity_check'), [('ok',)])
                self.assertEqual(rows('PRAGMA foreign_key_check'), [])
