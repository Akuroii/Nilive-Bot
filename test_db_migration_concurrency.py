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

    def _legacy_reaction_table(self):
        execute('DROP TABLE reaction_role_expiry')
        execute('CREATE TABLE reaction_role_expiry (guild_id INTEGER, user_id INTEGER, role_id INTEGER, expires_at TEXT, PRIMARY KEY(guild_id,user_id,role_id))')
        execute("INSERT INTO reaction_role_expiry VALUES (1,2,3,'2030-01-01')")
        execute('CREATE INDEX legacy_reaction_expiry_idx ON reaction_role_expiry(expires_at)')
        execute('CREATE TABLE legacy_reaction_audit (role_id INTEGER)')
        execute('CREATE TRIGGER legacy_reaction_insert AFTER INSERT ON reaction_role_expiry BEGIN INSERT INTO legacy_reaction_audit VALUES (NEW.role_id); END')

    async def test_rebuild_preserves_user_index_and_row(self):
        self._legacy_reaction_table()
        await database.init_db()
        self.assertEqual(rows('SELECT guild_id,user_id,role_id,message_id FROM reaction_role_expiry'), [(1,2,3,0)])
        self.assertEqual(rows("SELECT name FROM sqlite_master WHERE type='index' AND name='legacy_reaction_expiry_idx'"), [('legacy_reaction_expiry_idx',)])
        self.assertEqual(rows('SELECT * FROM legacy_reaction_audit'), [])  # copying isn't a new event
        execute("INSERT INTO reaction_role_expiry(guild_id,user_id,role_id,message_id) VALUES (1,2,4,8)")
        self.assertEqual(rows('SELECT role_id FROM legacy_reaction_audit'), [(4,)])

    async def test_rebuild_keeps_external_view_and_trigger_valid(self):
        """Objects on OTHER tables that reference the rebuilt table must keep
        referencing it, not a dropped `_old` table (SQLite rewrites references
        on RENAME, so the rebuild must never rename the live table away)."""
        self._legacy_reaction_table()
        execute('CREATE TABLE ext_events (id INTEGER)')
        execute('CREATE TABLE ext_log (n INTEGER)')
        execute('CREATE VIEW ext_expiry_view AS SELECT guild_id, user_id, role_id FROM reaction_role_expiry')
        execute('CREATE TRIGGER ext_touch AFTER INSERT ON ext_events BEGIN '
                'INSERT INTO ext_log SELECT COUNT(*) FROM reaction_role_expiry; END')
        await database.init_db()
        schema = ' '.join(r[0] or '' for r in rows(
            "SELECT sql FROM sqlite_master WHERE name IN ('ext_expiry_view','ext_touch')"))
        self.assertNotIn('_old', schema)
        self.assertNotIn('_new', schema)
        self.assertEqual(rows('SELECT * FROM ext_expiry_view'), [(1, 2, 3)])
        execute('INSERT INTO ext_events VALUES (1)')
        self.assertEqual(rows('SELECT n FROM ext_log'), [(1,)])
        # A dangling reference would also poison later, unrelated ALTERs.
        execute('ALTER TABLE ext_log ADD COLUMN extra INTEGER')
        self.assertEqual(rows('PRAGMA integrity_check'), [('ok',)])
        self.assertEqual(rows(
            "SELECT name FROM sqlite_master WHERE name LIKE 'reaction_role_expiry_%' "
            "AND type='table'"), [])

    async def test_rebuild_failure_rolls_back_rename(self):
        self._legacy_reaction_table()
        original = aiosqlite.Connection.execute
        def fail_rename(conn, sql, *args, **kwargs):
            # The worst moment: the original has already been dropped.
            if 'RENAME TO reaction_role_expiry' in sql and 'reaction_role_expiry_new' in sql:
                raise sqlite3.OperationalError('injected rebuild failure')
            return original(conn, sql, *args, **kwargs)
        with patch.object(aiosqlite.Connection, 'execute', fail_rename):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'injected rebuild failure'):
                await database.init_db()
        self.assertEqual(rows('SELECT guild_id,user_id,role_id FROM reaction_role_expiry'), [(1,2,3)])
        self.assertEqual(rows("SELECT name FROM sqlite_master WHERE name IN ('reaction_role_expiry_old','reaction_role_expiry_new')"), [])
        self.assertEqual(rows("SELECT name FROM sqlite_master WHERE type='index' AND name='legacy_reaction_expiry_idx'"), [('legacy_reaction_expiry_idx',)])

    async def test_all_additive_migrations_from_old_columns(self):
        """Exercise all helper call sites, including schema absent from main."""
        import ast
        import re
        tree = ast.parse(open(database.__file__, encoding='utf8').read())
        targets = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'add_column_if_missing' and len(node.args) == 4):
                continue
            table, column = node.args[1:3]
            if isinstance(table, ast.Constant) and isinstance(column, ast.Constant):
                targets.add((table.value, column.value))
            elif isinstance(table, ast.Name) and table.id == '_reward_table':
                targets.update((t, 'audience') for t in ('leveling_rewards', 'leveling_currency_rewards', 'leveling_shop_rewards'))
            else:
                self.fail(f'unrecognised migration call at line {node.lineno}')
        from utils.timed_ownership import _PURCHASE_EXTRA_COLUMNS
        targets.update(('shop_purchase_operations', name) for name, _ in _PURCHASE_EXTRA_COLUMNS)
        before = {}
        with closing(sqlite3.connect(DB_PATH)) as db:
            for table, column in sorted(targets):
                info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
                self.assertIn(column, {r[1] for r in info}, (table,column))
                before[(table,column)] = next(r[1:] for r in info if r[1] == column)
            # Index/trigger DDL that refers to a removed column must be removed
            # before SQLite will permit DROP; init_db recreates owned objects.
            for typ, name, table, sql in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL").fetchall():
                if any(table == t and re.search(r'\b'+re.escape(col)+r'\b', sql, re.I) for t,col in targets):
                    db.execute(f'DROP {typ.upper()} "{name}"')
            for table,column in sorted(targets):
                db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
            db.commit()
        code = "import asyncio,sys,database; sys.stdin.read(1); asyncio.run(database.init_db())"
        children = [subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
                    for _ in range(6)]
        for child in children:
            child.stdin.write(b'x')
            child.stdin.flush()
        results = [(child, *child.communicate(timeout=45)) for child in children]
        for child, out, err in results:
            self.assertEqual(child.returncode, 0, (out + err).decode())
        await database.init_db()
        for (table,column), expected in before.items():
            actual = next(r[1:] for r in rows(f'PRAGMA table_info("{table}")') if r[1] == column)
            self.assertEqual(actual, expected, (table,column))
        self.assertEqual(rows('PRAGMA integrity_check'), [('ok',)])
        self.assertEqual(rows('PRAGMA foreign_key_check'), [])
