"""Phase 2 additional integration/rollback/runtime checks. No visual tests.

Run in a separate process: python scripts/test_phase2_backend.py
Only VI-policy expectations superseded by the free entitlement rule are revised.
"""
import ast
import asyncio
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import aiosqlite
from phase1_support import (
    ROOT, GUILD, USER, DB_PATH, execute, rows, reset_database, seed_member,
    seed_listing, member, bot_for, interaction, mutation_snapshot,
)
from utils.prestige import activate_booster_prestige, PrestigeError
from utils.ledger import log_transaction
from cogs.shop import process_purchase
import test_phase1_api as api_tests
import test_phase1_runtime as runtime_tests
from utils import command_gating
from cogs.command_aliases import AliasGateError


class AuthoritativePurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        self.item = seed_listing()

    async def buy(self, **overrides):
        args = dict(item_id=self.item, bot=bot_for(member(True)))
        args.update(overrides)
        return await activate_booster_prestige(GUILD, USER, 2500, **args)

    async def rejected(self):
        before = mutation_snapshot()
        with self.assertRaises(PrestigeError):
            await self.buy()
        self.assertEqual(mutation_snapshot(), before)

    async def test_price_and_receipt_name_come_from_listing_not_caller(self):
        result = await activate_booster_prestige(
            GUILD, USER, 999999, item_id=self.item, item_name="Spoofed name",
            bot=bot_for(member(True)))
        self.assertTrue(result["success"], "caller price must not override canonical Free")
        self.assertEqual(rows("SELECT item_name,price_paid FROM purchase_history"), [("Prestige VI", 0)])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(2,)])

    async def test_boolean_alone_without_member_context_does_not_authorize(self):
        before = mutation_snapshot()
        with self.assertRaises(PrestigeError):
            await self.buy(bot=None, is_booster=True)
        self.assertEqual(mutation_snapshot(), before)

    async def test_uncached_booster_uses_member_fetch(self):
        from unittest.mock import AsyncMock
        person = member(True)
        server = SimpleNamespace(id=GUILD, get_member=lambda uid: None,
                                 fetch_member=AsyncMock(return_value=person))
        result = await self.buy(bot=SimpleNamespace(get_guild=lambda gid: server))
        self.assertTrue(result["success"])
        server.fetch_member.assert_awaited_once_with(USER)

    async def test_failed_member_fetch_fails_closed_without_writes(self):
        from unittest.mock import AsyncMock
        server = SimpleNamespace(id=GUILD, get_member=lambda uid: None,
                                 fetch_member=AsyncMock(side_effect=RuntimeError("unavailable")))
        before = mutation_snapshot()
        with self.assertRaises(PrestigeError):
            await self.buy(bot=SimpleNamespace(get_guild=lambda gid: server), is_booster=True)
        self.assertEqual(mutation_snapshot(), before)

    async def test_shop_ignores_invalid_stock_and_preserves_nonvi_stores(self):
        for stock in (0, -1, "garbage", 0.5, None):
            with self.subTest(stock=stock):
                execute("UPDATE shop_items SET current_stock=?", (stock,))
                before = mutation_snapshot()
                itx = interaction(True)
                await process_purchase(itx, self.item)
                after = mutation_snapshot()
                for table in before.keys() - {"prestige_vi_activations", "purchase_history"}:
                    self.assertEqual(after[table], before[table])
                self.assertEqual(rows("SELECT COUNT(*) FROM prestige_vi_activations"), [(1,)])
                execute("DELETE FROM prestige_vi_activations")
                itx.response.send_message.assert_awaited_once()
                self.assertIn("Free", itx.response.send_message.call_args.kwargs["embed"].description)

    async def test_unlimited_stock_is_preserved(self):
        execute("UPDATE shop_items SET max_stock=NULL,current_stock=NULL")
        self.assertTrue((await self.buy())["success"])
        self.assertEqual(rows("SELECT current_stock,max_stock FROM shop_items"), [(None, None)])

    async def test_stock_one_remains_untouched(self):
        execute("UPDATE shop_items SET max_stock=1,current_stock=1")
        self.assertTrue((await self.buy())["success"])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(1,)])

    async def test_same_user_concurrent_attempts_have_one_free_activation_and_receipt(self):
        results = await asyncio.gather(self.buy(), self.buy(), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) and result["success"] for result in results), 1)
        self.assertEqual(sum(isinstance(result, PrestigeError) for result in results), 1)
        self.assertEqual(rows("SELECT COUNT(*) FROM prestige_vi_activations"), [(1,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM purchase_history"), [(1,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(2,)])
        self.assertEqual(rows("SELECT balance FROM economy"), [(9000,)])

    async def test_prestige_disabled_is_rejected(self):
        execute("INSERT INTO prestige_config (guild_id,enabled) VALUES (?,0)", (GUILD,))
        await self.rejected()

    async def test_cancelled_purchase_rolls_back_even_after_receipt(self):
        from unittest.mock import AsyncMock
        before = mutation_snapshot()
        with patch.object(aiosqlite.Connection, "commit", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.buy()
        self.assertEqual(mutation_snapshot(), before)

    async def test_commit_failure_rolls_back_every_store(self):
        from unittest.mock import AsyncMock
        before = mutation_snapshot()
        with patch.object(aiosqlite.Connection, "commit", new=AsyncMock(side_effect=aiosqlite.OperationalError("commit failed"))):
            with self.assertRaises(PrestigeError):
                await self.buy()
        self.assertEqual(mutation_snapshot(), before)

    async def test_ledger_helper_respects_caller_transaction_and_standalone_commit(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            await log_transaction(GUILD, USER, "balance", 1, 9001, type="credit", db=db)
            self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])
            await db.rollback()
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])
        await log_transaction(GUILD, USER, "balance", 1, 9001, type="credit")
        self.assertEqual(rows("SELECT amount,balance_after FROM transaction_ledger"), [(1, 9001)])


BAD_CANONICAL = {
    "negative_stock": ("current_stock", -1),
    "malformed_stock": ("current_stock", "garbage"),
    "fractional_stock": ("current_stock", 0.5),
    "missing_finite_stock": ("current_stock", None),
    "over_maximum_stock": ("current_stock", 3),
    "invalid_maximum_stock": ("max_stock", -1),
    "malformed_maximum_stock": ("max_stock", "garbage"),
    "required_level": ("required_level", 85),
    "malformed_required_level": ("required_level", "garbage"),
    "required_role": ("required_role_id", 777),
    "malformed_required_role": ("required_role_id", "garbage"),
    "empty_name": ("name", "  "),
    "diamond_price": ("price_diamonds", 10),
}
for label, (field, value) in BAD_CANONICAL.items():
    ignored = field in {"current_stock", "max_stock", "required_level", "required_role_id"}
    def make_case(field, value, ignored):
        async def test(self):
            execute(f"UPDATE shop_items SET {field}=?", (value,))
            if ignored:
                before = mutation_snapshot()
                self.assertTrue((await self.buy())["success"])
                after = mutation_snapshot()
                for table in before.keys() - {"prestige_vi_activations", "purchase_history"}:
                    self.assertEqual(after[table], before[table])
            else:
                await self.rejected()
        return test
    setattr(AuthoritativePurchaseTests, "test_canonical_" + ("ignores_" if ignored else "rejects_") + label,
            make_case(field, value, ignored))

for table, operation in (("shop_items", "UPDATE"), ("economy", "UPDATE"),
                         ("prestige_vi_activations", "INSERT"), ("purchase_history", "INSERT"),
                         ("transaction_ledger", "INSERT")):
    def make_rollback_case(table, operation):
        async def test(self):
            execute(f"CREATE TRIGGER injected_failure BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
            if table in {"shop_items", "economy", "transaction_ledger"}:
                before = mutation_snapshot()
                self.assertTrue((await self.buy())["success"])
                after = mutation_snapshot()
                for untouched in before.keys() - {"prestige_vi_activations", "purchase_history"}:
                    self.assertEqual(after[untouched], before[untouched])
            else:
                await self.rejected()
                before = mutation_snapshot()
                itx = interaction(True)
                await process_purchase(itx, self.item)
                self.assertEqual(mutation_snapshot(), before)
                itx.response.send_message.assert_awaited_once()
                self.assertIn("No coins were spent", itx.response.send_message.call_args.args[0])
        return test
    setattr(AuthoritativePurchaseTests, ("test_no_write_to_" if table in {"shop_items", "economy", "transaction_ledger"} else "test_rollback_at_") + table, make_rollback_case(table, operation))


class AdditionalApiTests(unittest.TestCase):
    setUp = api_tests.ListingApiTests.setUp
    post = api_tests.ListingApiTests.post

    def test_required_and_malformed_fields_are_http_400_and_write_nothing(self):
        missing = object()
        cases = [("name", missing), ("name", " "), ("name", []),
                 ("type", missing), ("type", "invalid"), ("type", []),
                 ("max_stock", -1), ("max_stock", "bad"), ("max_stock", 1.5),
                 ("max_stock", True), ("price", 2**70), ("price", "9" * 5000),
                 ("required_level", []), ("featured", {}), ("description", {}),
                 ("icon_url", []), ("rarity", []), ("role_id", {}),
                 ("required_role_id", -1), ("duration_hours", "bad"),
                 ("price_diamonds", []), ("xp_boost_multiplier", "nan")]
        for field, value in cases:
            with self.subTest(field=field, value=str(value)[:40]):
                payload = dict(name="Paid V", type="prestige", prestige_tier=5, price=2500)
                if value is missing:
                    payload.pop(field)
                else:
                    payload[field] = value
                before = mutation_snapshot()
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIs(response.get_json()["success"], False)
                self.assertTrue(response.get_json()["error"])
                self.assertEqual(mutation_snapshot(), before)

    def test_nonobject_or_malformed_json_is_400(self):
        for body in ('[]', 'null', '"VI"', '{invalid', '{}'):
            with self.subTest(body=body):
                before = mutation_snapshot()
                response = self.client.post("/api/shop/item", data=body, content_type="application/json",
                                            headers={"X-CSRF-Token": "phase1-csrf"})
                self.assertEqual(response.status_code, 400)
                self.assertIs(response.get_json()["success"], False)
                self.assertEqual(mutation_snapshot(), before)

    def test_form_numbers_supported_and_session_guild_is_authoritative(self):
        response = self.client.post("/api/shop/item", data=dict(name="VI", type="prestige",
            price="0", prestige_tier="6", max_stock="2", guild_id=str(GUILD + 1)),
            headers={"X-CSRF-Token": "phase1-csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.get_json()["success"], True)
        self.assertEqual(rows("SELECT guild_id,price,prestige_tier,current_stock FROM shop_items"),
                         [(GUILD, 0, 6, None)])


class AdditionalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runtime_tests.RuntimeTests.asyncSetUp
    cleanup_bots = runtime_tests.RuntimeTests.cleanup_bots
    slash = runtime_tests.RuntimeTests.slash

    async def test_cooldown_expiry_allows_next_path_at_same_boundary(self):
        with patch("time.time", return_value=1000000):
            self.assertTrue(await self.slash())
        with patch("time.time", return_value=1000059):
            with self.assertRaises(AliasGateError):
                await self.alias(self.ctx)
        with patch("time.time", return_value=1000060):
            self.assertTrue(await self.alias(self.ctx))
            self.assertFalse(await self.slash())
        with patch("time.time", return_value=1000120):
            self.assertTrue(await self.slash())

    async def test_other_user_and_guild_have_isolated_buckets(self):
        self.assertTrue(await self.slash())
        self.ctx.author = member(user=USER + 1)
        self.assertTrue(await self.alias(self.ctx))
        self.ctx.author = self.itx.user
        self.ctx.guild = SimpleNamespace(id=GUILD + 1, owner_id=99)
        execute("INSERT INTO command_toggles (guild_id,command_name,enabled,cooldown_seconds) "
                "VALUES (?,'rank',1,60)", (GUILD + 1,))
        self.assertTrue(await self.alias(self.ctx))
        self.assertFalse(await self.slash())
        self.assertEqual(set(command_gating._command_cooldowns),
                         {(GUILD, USER, "rank"), (GUILD, USER + 1, "rank"), (GUILD + 1, USER, "rank")})

    async def test_normal_import_and_script_entrypoint_share_the_gating_store(self):
        normal = importlib.import_module("main")  # explicit second startup-mode fixture only
        self.assertIs(normal._command_cooldowns, command_gating._command_cooldowns)
        self.assertIs(self.runtime._command_cooldowns, normal._command_cooldowns)
        self.assertIs(normal._prune_command_cooldowns, self.runtime._prune_command_cooldowns)
        self.assertTrue(await normal.bot.tree.interaction_check(self.itx))
        with self.assertRaises(AliasGateError):
            await self.alias(self.ctx)
        self.assertFalse(await self.slash())

    async def test_pruning_retains_existing_threshold_and_age_policy(self):
        state = command_gating._command_cooldowns
        state.update({(GUILD, uid, "rank"): 1 for uid in range(4999)})
        command_gating._prune_command_cooldowns(100000)
        self.assertEqual(len(state), 4999)
        state[(GUILD, 10000, "rank")] = 100000
        command_gating._prune_command_cooldowns(100000)
        self.assertEqual(state, {(GUILD, 10000, "rank"): 100000})

    async def test_permission_and_bypass_policy_stays_identical(self):
        for column, value, allowed in (("owner_only", 1, False),
                                      ("disabled_channels", '[8301]', False),
                                      ("enabled_roles", '[77]', False),
                                      ("disabled_roles", '[77]', True)):
            with self.subTest(column=column):
                execute("UPDATE command_toggles SET owner_only=0,disabled_channels=NULL,"
                        "enabled_roles=NULL,disabled_roles=NULL,cooldown_seconds=0")
                execute(f"UPDATE command_toggles SET {column}=?", (value,))
                self.assertEqual(await self.slash(), allowed)
                if allowed:
                    self.assertTrue(await self.alias(self.ctx))
                else:
                    with self.assertRaises(AliasGateError):
                        await self.alias(self.ctx)
        execute("UPDATE command_toggles SET disabled_roles=NULL,cooldown_seconds=60,"
                "bypass_cooldown_roles='[77]'")
        self.itx.user.roles = [SimpleNamespace(id=77)]
        self.assertTrue(await self.slash())
        self.assertTrue(await self.alias(self.ctx))
        self.assertTrue(await self.slash())
        self.assertEqual(command_gating._command_cooldowns, {})

    async def test_single_canonical_reset_loop_runs_once_after_ready_and_cancels(self):
        import cogs.leveling as leveling
        tree = ast.parse((ROOT / "cogs/leveling.py").read_text())
        cog_ast = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Leveling")
        for name in ("leaderboard_reset_task", "before_reset_task"):
            self.assertEqual(sum(getattr(node, "name", None) == name for node in cog_ast.body), 1)
        execute("INSERT INTO leveling_reset_config (guild_id,enabled,period) VALUES (?,1,'daily')", (GUILD,))
        await self.bot.load_extension("cogs.leveling")
        # load_extension creates a fresh module: instrument the loaded instance.
        leveling = importlib.import_module("cogs.leveling")
        finished = asyncio.Event()
        original_config = leveling.get_leveling_config
        async def record(*args, **kwargs):
            # Signal after the iteration's final DB context has closed, so the
            # fixture does not cancel aiosqlite 0.19 during connection startup.
            result = await original_config(*args, **kwargs)
            finished.set()
            return result
        with patch.object(leveling, "perform_leaderboard_reset", wraps=leveling.perform_leaderboard_reset) as reset, \
                patch.object(leveling, "get_leveling_config", side_effect=record):
            cog = self.bot.get_cog("Leveling")
            task = cog.leaderboard_reset_task.get_task()
            self.bot._ready.set()  # local event, not a Discord READY payload
            await asyncio.wait_for(finished.wait(), timeout=5)
            self.assertEqual(reset.await_count, 1)
            self.assertEqual(rows("SELECT COUNT(*) FROM leveling_leaderboard_history"), [(1,)])
            self.assertEqual(rows("SELECT xp,level,prestige FROM levels"), [(0, 0, 3)])
            await self.bot.unload_extension("cogs.leveling")
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main(verbosity=2)
