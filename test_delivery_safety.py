"""Real SQLite regressions; phase1_support creates scratch databases only."""
import asyncio
import ast
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from phase1_support import GUILD, USER, DB_PATH, execute, rows, reset_database
import aiosqlite
import database
from utils import inventory, potion_engine, xp_calculator
from utils import timed_ownership as TO


class PotionTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        await inventory.give_item(GUILD, USER, "Potion", item_type="potion",
                                  metadata=potion_engine.build_metadata("xp_boost", 2, 1))
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,track,kind,item_name,"
                "quantity,remaining_quantity,created_at) "
                "VALUES ('potion-grant',?,?,'shop','inventory','Potion',1,1,'scratch')",
                (GUILD,USER))

    def state(self):
        return (rows("SELECT quantity FROM inventory_items WHERE item_name='Potion'"),
                rows("SELECT multiplier FROM leveling_active_boosts"))

    async def test_consumption_and_effect_commit_together(self):
        result = await potion_engine.use_potion(GUILD, USER, "Potion")
        self.assertTrue(result["success"], result)
        self.assertEqual(self.state(), ([(0,)], [(2.0,)]))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(0,)])

    async def test_failure_after_effect_insert_rolls_back_both(self):
        original = xp_calculator.grant_xp_boost
        async def fail(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("failure after INSERT, before commit")
        with patch.object(xp_calculator, "grant_xp_boost", fail):
            result = await potion_engine.use_potion(GUILD, USER, "Potion")
        self.assertFalse(result["success"])
        self.assertEqual(self.state(), ([(1,)], []))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(1,)])

    async def test_cancellation_after_effect_insert_rolls_back_both(self):
        original = xp_calculator.grant_xp_boost
        async def cancel(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()
        with patch.object(xp_calculator, "grant_xp_boost", cancel):
            with self.assertRaises(asyncio.CancelledError):
                await potion_engine.use_potion(GUILD, USER, "Potion")
        self.assertEqual(self.state(), ([(1,)], []))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(1,)])

    async def test_provenance_failure_preserves_inventory_and_effects(self):
        async def fail(*args, **kwargs):
            raise RuntimeError("provenance update failed")
        with patch.object(inventory, "_consume_provenance", fail):
            result = await potion_engine.use_potion(GUILD, USER, "Potion")
        self.assertFalse(result["success"])
        self.assertEqual(self.state(), ([(1,)], []))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(1,)])

    async def test_concurrent_activation_of_last_copy_grants_once(self):
        results = await asyncio.gather(*(potion_engine.use_potion(GUILD, USER, "Potion")
                                         for _ in range(2)))
        self.assertEqual(sum(bool(r["success"]) for r in results), 1)
        self.assertEqual(self.state(), ([(0,)], [(2.0,)]))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(0,)])

    async def test_process_exit_before_or_after_effect_insert_does_not_commit(self):
        import sys
        for after_insert in (False, True):
            with self.subTest(after_insert=after_insert):
                code = f'''
import asyncio, os
from unittest.mock import patch
from utils.potion_engine import use_potion
from utils.xp_calculator import grant_xp_boost
async def crash(*args, **kwargs):
    if {after_insert!r}:
        await grant_xp_boost(*args, **kwargs)
    os._exit(17)
async def main():
    with patch("utils.xp_calculator.grant_xp_boost", crash):
        await use_potion({GUILD}, {USER}, "Potion")
asyncio.run(main())
'''
                result = await asyncio.to_thread(
                    subprocess.run, [sys.executable, "-c", code],
                    env=dict(os.environ), capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 17, result.stderr.decode())
                self.assertEqual(self.state(), ([(1,)], []))
                self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(1,)])

    async def test_commit_failure_rolls_back_all_three_stores(self):
        async def fail(connection):
            raise RuntimeError("commit failed before SQLite commit")
        with patch.object(aiosqlite.Connection, "commit", fail):
            result = await potion_engine.use_potion(GUILD, USER, "Potion")
        self.assertFalse(result["success"])
        self.assertEqual(self.state(), ([(1,)], []))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(1,)])

    async def test_remove_item_does_not_commit_caller_transaction(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            await inventory.remove_item(GUILD, USER, "Potion", db=db)
            await db.rollback()
        self.assertEqual(self.state(), ([(1,)], []))
        self.assertEqual(rows("SELECT remaining_quantity FROM reward_grants"), [(1,)])


class EntryKeyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    def legacy_table(self):
        execute("DROP TABLE equipped_roles")
        execute("CREATE TABLE equipped_roles (guild_id INTEGER, user_id INTEGER, "
                "item_name TEXT, role_id INTEGER, equipped_at TIMESTAMP, "
                "PRIMARY KEY(guild_id,user_id))")
        execute("INSERT INTO equipped_roles VALUES (?,?, 'Legacy', 55, 'old')", (GUILD, USER))

    async def test_fresh_and_repeat_startup_has_required_column(self):
        await database.init_db()
        await database.init_db()
        self.assertIn("entry_key", {r[1] for r in rows("PRAGMA table_info(equipped_roles)")})

    async def test_legacy_migration_preserves_rows_and_null_identity(self):
        self.legacy_table()
        await database.init_db()
        await database.init_db()
        self.assertEqual(rows("SELECT item_name,role_id,entry_key FROM equipped_roles"),
                         [("Legacy", 55, None)])

    async def test_required_alter_failure_aborts_startup(self):
        self.legacy_table()
        original = aiosqlite.Connection.execute
        def failing(connection, sql, *args, **kwargs):
            if "ALTER TABLE equipped_roles ADD COLUMN entry_key" in sql:
                raise RuntimeError("simulated read-only disk")
            return original(connection, sql, *args, **kwargs)
        with patch.object(aiosqlite.Connection, "execute", failing):
            with self.assertRaisesRegex(RuntimeError, "Required migration equipped_roles.entry_key"):
                await database.init_db()
        self.assertNotIn("entry_key", {r[1] for r in rows("PRAGMA table_info(equipped_roles)")})
        self.assertEqual(rows("SELECT item_name,role_id FROM equipped_roles"), [("Legacy", 55)])


class WalletDeletionSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_wallet_deletion_classes_imports_or_routes(self):
        for path in (Path("cogs/wallet.py"), Path("wallet.py")):
            with self.subTest(path=str(path)):
                tree = ast.parse(path.read_text())
                names = {n.name for n in ast.walk(tree)
                         if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertTrue(names.isdisjoint({"DropButton", "DropConfirmView",
                                                 "open_drop_confirm", "build_drop_confirm_embed"}))
                self.assertFalse(any(isinstance(n, ast.ImportFrom) and
                                     any(a.name == "drop_item" for a in n.names)
                                     for n in ast.walk(tree)))

    async def test_no_drop_for_any_item_panel_category(self):
        from cogs.wallet import ItemPanelView, TAB_ITEMS, TAB_COLORS, TAB_TITLES, TAB_POTIONS
        for tab, kind in ((TAB_ITEMS, "custom"), (TAB_COLORS, "color"),
                          (TAB_TITLES, "title"), (TAB_POTIONS, "potion")):
            for timed in (False, True):
                with self.subTest(tab=tab, timed=timed):
                    view = ItemPanelView(GUILD, USER, tab, {
                        "item_name": "Thing", "item_type": kind, "entry_key": "i:1",
                        "timed": timed, "metadata": {}}, False)
                    labels = [getattr(c, "label", None) for c in view.children]
                    self.assertNotIn("Drop", labels)
                    self.assertIn("Back to Inventory", labels)
                    view.stop()


class DescriptorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    async def test_foreign_guild_or_member_cannot_mutate_victim_state(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        for key in ("guild_id", "user_id"):
            with self.subTest(key=key):
                guild, user = (GUILD+1, USER) if key == "guild_id" else (GUILD, USER+1)
                await inventory.set_quantity(guild, user, "Victim", 3, item_type="role",
                                             metadata={"role_id": 55})
                victim = rows("SELECT id,quantity,item_type,source,metadata,created_at,updated_at "
                              "FROM inventory_items WHERE guild_id=? AND user_id=?", (guild,user))[0]
                execute("INSERT OR REPLACE INTO equipped_roles "
                        "(guild_id,user_id,item_name,role_id,entry_key) VALUES (?,?,?,55,?)",
                        (guild,user,"Victim",f"i:{victim[0]}"))
                foreign_op = await TO.open_purchase(guild,user,item_id=2,item_name="Victim",
                                                     currency="balance",amount=10)
                op = await TO.open_purchase(GUILD,USER,item_id=1,item_name="Own",
                                            currency="balance",amount=10)
                spec = {"guild_id":guild,"user_id":user,"item_id":1,"item_name":"Victim",
                        "internal_type":"role","role_id":55,"role_added":True,
                        "entry_key":f"i:{victim[0]}","delivered_inventory":victim}
                await TO.record_pending_delivery_rollback(op,spec,bot=None)
                snapshot = {t:rows(f"SELECT * FROM {t} ORDER BY rowid") for t in
                            ("inventory_items","equipped_roles","leveling_active_boosts",
                             "timed_holdings")}
                foreign_before = await TO.get_purchase(foreign_op)
                person = SimpleNamespace(add_roles=AsyncMock(),remove_roles=AsyncMock())
                calls = []
                def get_guild(gid):
                    calls.append(gid)
                    return SimpleNamespace(get_member=lambda _: person)
                bot = SimpleNamespace(get_guild=get_guild)
                result = await TO.compensate_late_delivery(op,spec,bot=bot)
                self.assertEqual(calls, [])
                self.assertFalse(result["complete"], result)
                self.assertTrue((await TO.get_purchase(op))["_raw_delivery_rollback"])
                for table,before in snapshot.items():
                    self.assertEqual(rows(f"SELECT * FROM {table} ORDER BY rowid"),before,table)
                self.assertEqual(await TO.get_purchase(foreign_op),foreign_before)
                person.add_roles.assert_not_awaited()
                person.remove_roles.assert_not_awaited()


class LevelRemovalSynchronizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()

    async def test_level_removal_cannot_strip_concurrent_new_equip(self):
        from unittest.mock import AsyncMock
        from test_timed_ownership import purchase_ctx
        from utils import equip_engine, level_claims
        role = type("Role", (), {"id":551, "name":"Shared",
                                 "position":1, "mention":"<@&551>"})()
        _, person, bot, guild = purchase_ctx(granted_role=role)
        person.guild = guild
        person.roles = [role]
        entered, release = asyncio.Event(), asyncio.Event()

        async def remove(target, **kwargs):
            entered.set()
            await release.wait()
            person.roles = [r for r in person.roles if r.id != target.id]

        async def add(target, **kwargs):
            if target not in person.roles:
                person.roles.append(target)

        person.remove_roles = AsyncMock(side_effect=remove)
        person.add_roles = AsyncMock(side_effect=add)
        with patch.object(level_claims, "superseded_role_ids", AsyncMock(return_value=[551])):
            removal = asyncio.create_task(level_claims.enforce_role_progression(
                person, GUILD, USER, 10, 552))
            equip = None
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await inventory.give_item(GUILD, USER, "New purchase", item_type="role",
                                          metadata={"role_id":551})
                equip = asyncio.create_task(equip_engine.equip_role(
                    bot, GUILD, USER, "New purchase"))
                try:
                    await asyncio.wait_for(asyncio.shield(equip), .2)
                    finished_during_removal = True
                except asyncio.TimeoutError:
                    finished_during_removal = False
                release.set()
                await asyncio.wait_for(removal, 3)
                result = await asyncio.wait_for(equip, 3)
                self.assertTrue(result["success"], result)
                self.assertFalse(finished_during_removal,
                                 "equip bypassed the in-flight Level removal")
                self.assertIn(role, person.roles)
                self.assertEqual(rows("SELECT role_id FROM equipped_roles"), [(551,)])
            finally:
                release.set()
                for task in (removal,equip):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(t for t in (removal,equip) if t is not None),
                                     return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
