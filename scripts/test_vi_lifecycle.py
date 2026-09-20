"""Locked free/unmetered VI policy: real SQLite/services/listeners; no Discord login."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import aiosqlite
from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, seed_listing,
    member, bot_for, interaction, mutation_snapshot,
)
from utils import prestige
from cogs.boost import Boost
from cogs.shop import process_purchase, Shop


class FreeVITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        self.item = seed_listing(price=0, stock=0)
        self.person = member(True)
        self.bot = bot_for(self.person)
        self.person.guild = self.bot.get_guild(GUILD)
        self.person.guild.name = "Test guild"

    async def activate(self, user=USER, guild=GUILD, bot=None, item=None):
        try:
            return await prestige.activate_booster_prestige(
                guild, user, 999999, item_id=self.item if item is None else item,
                bot=bot or self.bot)
        except prestige.PrestigeError as exc:
            self.fail(f"eligible free VI activation rejected: {exc}")

    def seed_activation(self, user=USER, guild=GUILD, when=None):
        execute("INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) VALUES (?,?,?)",
                (guild, user, (when or datetime.now(timezone.utc)).isoformat()))

    def assert_nonvi_stores_unchanged(self, before):
        after = mutation_snapshot()
        for table in before.keys() - {"prestige_vi_activations", "purchase_history"}:
            self.assertEqual(after[table], before[table], table)

    async def test_free_activation_changes_only_vi_state_and_one_zero_receipt(self):
        before = mutation_snapshot()
        self.assertTrue((await self.activate())["success"])
        self.assert_nonvi_stores_unchanged(before)
        self.assertEqual(rows("SELECT item_id,price_paid FROM purchase_history"), [(self.item, 0)])
        self.assertTrue(await prestige.has_vi_activation(GUILD, USER))
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 6)

    async def test_zero_coin_member_can_activate(self):
        execute("UPDATE economy SET balance=0")
        await self.activate()
        self.assertEqual(rows("SELECT balance,diamonds FROM economy"), [(0, 138)])

    async def test_no_economy_stock_or_ledger_write_even_attempted(self):
        for table, operation in (("economy", "UPDATE"), ("shop_items", "UPDATE"),
                                 ("transaction_ledger", "INSERT")):
            execute(f"CREATE TRIGGER forbid_{table} BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'VI touched a forbidden store'); END")
        await self.activate()
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])

    async def test_direct_and_shop_paths_do_not_select_stock_columns(self):
        queries = []
        original = aiosqlite.Connection.execute
        async def record(connection, sql, *args, **kwargs):
            queries.append(sql.lower())
            return await original(connection, sql, *args, **kwargs)
        with patch.object(aiosqlite.Connection, "execute", new=record):
            await process_purchase(interaction(True), self.item)
        self.assertTrue(await prestige.has_vi_activation(GUILD, USER))
        shop_queries = [q for q in queries if "from shop_items" in q]
        self.assertTrue(shop_queries)
        self.assertTrue(all("current_stock" not in q and "max_stock" not in q and "select *" not in q
                            for q in shop_queries), shop_queries)

    async def test_stock_level_and_role_fields_are_completely_irrelevant_to_vi(self):
        for stock in (0, -1, 99, "invalid", None):
            with self.subTest(stock=stock):
                execute("DELETE FROM prestige_vi_activations")
                execute("UPDATE shop_items SET max_stock=?,current_stock=?,required_level='invalid',"
                        "required_role_id='invalid'", (stock, stock))
                before = mutation_snapshot()
                itx = interaction(True)
                await process_purchase(itx, self.item)
                self.assertTrue(await prestige.has_vi_activation(GUILD, USER))
                self.assert_nonvi_stores_unchanged(before)

    async def test_concurrent_same_user_produces_one_active_record_and_receipt(self):
        results = await asyncio.gather(*[
            prestige.activate_booster_prestige(GUILD, USER, 0, item_id=self.item, bot=self.bot)
            for _ in range(2)], return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, prestige.PrestigeError) for result in results), 1)
        self.assertEqual(rows("SELECT COUNT(*) FROM prestige_vi_activations"), [(1,)])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,)])
        self.assertEqual(rows("SELECT balance FROM economy"), [(9000,)])

    async def test_two_users_have_no_global_stock_contention(self):
        seed_member(user=USER + 1)
        await asyncio.gather(self.activate(), self.activate(user=USER + 1, bot=bot_for(member(True, USER + 1))))
        self.assertEqual(rows("SELECT COUNT(*) FROM prestige_vi_activations"), [(2,)])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(0,)])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,), (0,)])

    async def test_nonbooster_and_boolean_spoof_cannot_activate_zero_listing(self):
        before = mutation_snapshot()
        for bot in (None, bot_for(member(False))):
            with self.assertRaises(prestige.PrestigeError):
                await prestige.activate_booster_prestige(GUILD, USER, 0, item_id=self.item,
                                                         bot=bot, is_booster=True)
        self.assertEqual(mutation_snapshot(), before)

    async def test_booster_without_activation_stays_permanent(self):
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)

    async def test_verified_expiry_deletes_activation_not_permanent_or_receipts(self):
        self.seed_activation()
        before = mutation_snapshot()
        self.person.premium_since = None
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
        self.assertFalse(await prestige.has_vi_activation(GUILD, USER))
        self.assert_nonvi_stores_unchanged(before)
        self.assertEqual(mutation_snapshot()["purchase_history"], before["purchase_history"])

    async def test_expiry_then_reboost_requires_new_free_activation(self):
        await self.activate()
        self.person.premium_since = None
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
        self.person.premium_since = datetime.now(timezone.utc)
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
        await self.activate()
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 6)
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(0,), (0,)])
        self.assertEqual(rows("SELECT balance FROM economy"), [(9000,)])

    async def test_missed_expiry_then_new_boost_timestamp_does_not_restore_vi(self):
        self.seed_activation(when=datetime.now(timezone.utc) - timedelta(days=2))
        self.person.premium_since = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
        self.assertFalse(await prestige.has_vi_activation(GUILD, USER))

    async def test_unknown_context_does_not_delete_or_grant_vi(self):
        self.seed_activation()
        before = mutation_snapshot()
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER), 3)
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, is_booster=True), 3)
        self.assertEqual(mutation_snapshot(), before)

    async def test_expiry_is_scoped_to_guild_and_user(self):
        for gid, uid in ((GUILD, USER), (GUILD + 1, USER), (GUILD, USER + 1)):
            self.seed_activation(guild=gid, user=uid)
        self.person.premium_since = None
        await prestige.get_effective_prestige(GUILD, USER, bot=self.bot)
        self.assertEqual(set(rows("SELECT guild_id,user_id FROM prestige_vi_activations")),
                         {(GUILD + 1, USER), (GUILD, USER + 1)})

    async def test_boost_listener_revokes_with_automation_disabled(self):
        for mode in ("missing", "disabled", "no_auto_remove"):
            with self.subTest(mode=mode):
                execute("DELETE FROM boost_config")
                execute("DELETE FROM prestige_vi_activations")
                if mode != "missing":
                    execute("INSERT INTO boost_config (guild_id,enabled,auto_remove_on_unboost) VALUES (?,?,0)",
                            (GUILD, int(mode != "disabled")))
                self.seed_activation()
                before = member(True)
                self.person.premium_since = None
                with patch("utils.prestige.sync_prestige_roles", new=AsyncMock()) as sync:
                    await Boost(self.bot).on_member_update(before, self.person)
                self.assertFalse(await prestige.has_vi_activation(GUILD, USER))
                sync.assert_awaited_once()
                self.assertEqual(await prestige.get_permanent_prestige(GUILD, USER), 3)

    async def test_reconnect_reconciles_expired_rows(self):
        self.seed_activation()
        self.person.premium_since = None
        cog = Boost(self.bot)
        callback = getattr(cog, "on_ready", None)
        self.assertTrue(callable(callback), "Boost needs reconnect reconciliation")
        with patch("utils.prestige.sync_prestige_roles", new=AsyncMock()):
            await callback()
        self.assertFalse(await prestige.has_vi_activation(GUILD, USER))

    async def test_shop_displays_free_and_keeps_button_despite_zero_stock(self):
        itx = interaction(True)
        itx.guild.name = "Test guild"
        await Shop.shop.callback(object.__new__(Shop), itx)
        kwargs = itx.response.send_message.call_args.kwargs
        content = str(kwargs["embed"].to_dict()).lower()
        self.assertIn("free", content)
        self.assertNotIn("out of stock", content)
        self.assertNotIn("coins", content)
        self.assertEqual(len(kwargs["view"].children), 1)
        self.assertIn("free", kwargs["view"].children[0].label.lower())

    async def test_activation_and_receipt_failures_are_atomic(self):
        for table in ("prestige_vi_activations", "purchase_history"):
            with self.subTest(table=table):
                execute(f"CREATE TRIGGER fail_insert BEFORE INSERT ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
                before = mutation_snapshot()
                with self.assertRaises(prestige.PrestigeError) as caught:
                    await prestige.activate_booster_prestige(GUILD, USER, 0, item_id=self.item, bot=self.bot)
                self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)
                self.assertIn("injected failure", str(caught.exception.__cause__))
                self.assertEqual(mutation_snapshot(), before)
                execute("DROP TRIGGER fail_insert")

    async def test_actual_role_sync_removes_vi_and_restores_permanent_role(self):
        self.seed_activation()
        vi_role, permanent_role = SimpleNamespace(id=606), SimpleNamespace(id=303)
        self.person.roles = [vi_role]
        self.person.guild.get_role = lambda rid: {606: vi_role, 303: permanent_role}.get(rid)
        execute("INSERT INTO prestige_roles (guild_id,tier,role_id) VALUES (?,6,606)", (GUILD,))
        execute("INSERT INTO prestige_roles (guild_id,tier,role_id) VALUES (?,3,303)", (GUILD,))
        async def remove(role, **kwargs):
            self.person.roles.remove(role)
        async def add(role, **kwargs):
            self.person.roles.append(role)
        self.person.remove_roles = AsyncMock(side_effect=remove)
        self.person.add_roles = AsyncMock(side_effect=add)
        self.person.premium_since = None
        with patch("utils.permissions.check_bot_role_position", return_value=(True, "")):
            await Boost(self.bot).on_member_update(member(True), self.person)
        self.assertEqual(self.person.roles, [permanent_role])
        self.person.remove_roles.assert_awaited_once()
        self.person.add_roles.assert_awaited_once()
        self.assertFalse(await prestige.has_vi_activation(GUILD, USER))

    async def test_stale_expiry_event_does_not_delete_new_current_activation(self):
        self.seed_activation()
        stale_after = member(False)
        stale_after.guild = self.person.guild
        # Cache contains the current boosted member, not the stale event snapshot.
        with patch("utils.prestige.sync_prestige_roles", new=AsyncMock()):
            await Boost(self.bot).on_member_update(member(True), stale_after)
        self.assertTrue(await prestige.has_vi_activation(GUILD, USER))
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 6)

    async def test_departure_deletes_only_that_guilds_activation(self):
        self.seed_activation()
        self.seed_activation(guild=GUILD + 1)
        import discord
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), {"code": 10007, "message": "Unknown Member"}))
        await Boost(self.bot).on_member_remove(self.person)
        self.assertEqual(rows("SELECT guild_id FROM prestige_vi_activations"), [(GUILD + 1,)])

    async def test_new_boost_event_does_not_grant_vi(self):
        with patch("utils.prestige.sync_prestige_roles", new=AsyncMock()):
            await Boost(self.bot).on_member_update(member(False), self.person)
        self.assertFalse(await prestige.has_vi_activation(GUILD, USER))
        self.assertEqual(rows("SELECT * FROM purchase_history"), [])

    async def test_reconnect_lookup_failure_preserves_records_but_grants_nothing(self):
        self.seed_activation()
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(side_effect=RuntimeError("offline"))
        before = mutation_snapshot()
        await Boost(self.bot).on_ready()
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
        self.assertEqual(mutation_snapshot(), before)

    async def test_invalid_or_future_activation_timestamp_cannot_restore_vi(self):
        for timestamp in ("garbage", None, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()):
            with self.subTest(timestamp=timestamp):
                execute("INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) VALUES (?,?,?)",
                        (GUILD, USER, timestamp))
                self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
                self.assertFalse(await prestige.has_vi_activation(GUILD, USER))

    async def test_wrong_guild_member_cannot_change_or_grant_this_guilds_vi(self):
        self.seed_activation()
        wrong = member(False)
        wrong.guild = SimpleNamespace(id=GUILD + 1)
        before = mutation_snapshot()
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, member=wrong), 3)
        self.assertEqual(mutation_snapshot(), before)

    async def test_i_to_v_shop_price_reset_stock_and_requirement_behavior_unchanged(self):
        item = seed_listing(price=2500, tier=4, stock=2, name="Prestige IV")
        execute("UPDATE shop_items SET required_level=85 WHERE id=?", (item,))
        before = mutation_snapshot()
        await process_purchase(interaction(True), item)
        self.assertEqual(mutation_snapshot(), before)
        execute("UPDATE shop_items SET required_level=0,required_role_id=77 WHERE id=?", (item,))
        itx = interaction(True)
        itx.guild.get_role = lambda rid: SimpleNamespace(id=77, mention="role")
        before = mutation_snapshot()
        await process_purchase(itx, item)
        self.assertEqual(mutation_snapshot(), before)
        execute("UPDATE shop_items SET required_role_id=NULL WHERE id=?", (item,))
        await process_purchase(interaction(True), item)
        self.assertEqual(rows("SELECT balance,diamonds FROM economy"), [(0, 138)])
        self.assertEqual(rows("SELECT xp,level,prestige FROM levels"), [(2700055, 84, 4)])
        self.assertEqual(rows("SELECT price_paid FROM purchase_history"), [(9000,)])
        self.assertEqual(rows("SELECT amount FROM transaction_ledger"), [(-9000,)])
        # The pre-correction I–V path does not decrement stock; do NOT change it here.
        self.assertEqual(rows("SELECT current_stock FROM shop_items WHERE id=?", (item,)), [(2,)])

    async def test_direct_listing_identifiers_and_member_context_cannot_be_spoofed(self):
        before = mutation_snapshot()
        for kwargs in (dict(item_id=True), dict(item_id=-1), dict(item_id="bad"),
                       dict(item_id=self.item, bot=bot_for(member(True, USER + 1)))):
            params = dict(item_id=self.item, bot=self.bot)
            params.update(kwargs)
            with self.assertRaises(prestige.PrestigeError):
                await prestige.activate_booster_prestige(GUILD, USER, 0, **params)
        self.assertEqual(mutation_snapshot(), before)



    async def test_reconnect_confirmed_missing_member_revokes(self):
        import discord
        self.seed_activation()
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), {"code": 10007, "message": "Unknown Member"}))
        await Boost(self.bot).on_ready()
        self.assertFalse(await prestige.has_vi_activation(GUILD, USER))
        self.assertEqual(rows("SELECT prestige FROM levels"), [(3,)])

    async def test_compare_and_delete_preserves_activation_written_after_observation(self):
        self.seed_activation(when=datetime.now(timezone.utc) - timedelta(days=1))
        self.person.premium_since = None
        original = aiosqlite.Connection.execute
        replaced = []
        async def interleave(connection, sql, *args, **kwargs):
            if sql.startswith("DELETE FROM prestige_vi_activations") and not replaced:
                stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                execute("UPDATE prestige_vi_activations SET activated_at=?", (stamp,))
                replaced.append(stamp)
                self.person.premium_since = datetime.now(timezone.utc) - timedelta(minutes=1)
            return await original(connection, sql, *args, **kwargs)
        with patch.object(aiosqlite.Connection, "execute", new=interleave):
            self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 3)
        self.assertEqual(rows("SELECT activated_at FROM prestige_vi_activations"), [(replaced[0],)])
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 6)

    async def test_still_boosting_after_losing_one_of_two_boosts_keeps_vi(self):
        self.seed_activation()
        before = member(True)
        before.premium_subscription_count = 2
        self.person.premium_subscription_count = 1
        await Boost(self.bot).on_member_update(before, self.person)
        self.assertTrue(await prestige.has_vi_activation(GUILD, USER))
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, bot=self.bot), 6)

    async def test_missed_expiry_does_not_restore_vi_earn_multiplier(self):
        self.seed_activation(when=datetime.now(timezone.utc) - timedelta(days=2))
        self.person.premium_since = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(await prestige.get_prestige_earn_multiplier(
            GUILD, USER, "diamonds", bot=self.bot), 1.0)
        await self.activate()
        self.assertEqual(await prestige.get_prestige_earn_multiplier(
            GUILD, USER, "diamonds", bot=self.bot), 1.2)

    async def test_stale_uncached_observation_cannot_erase_activation_created_before_its_select(self):
        self.seed_activation()
        self.person.premium_since = None
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(return_value=self.person)
        original = aiosqlite.Connection.execute
        replaced = []
        async def interleave(connection, sql, *args, **kwargs):
            if sql.startswith("SELECT activated_at") and not replaced:
                stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                execute("UPDATE prestige_vi_activations SET activated_at=?", (stamp,))
                replaced.append(stamp)
            return await original(connection, sql, *args, **kwargs)
        with patch.object(aiosqlite.Connection, "execute", new=interleave):
            self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, member=self.person), 3)
        self.assertEqual(rows("SELECT activated_at FROM prestige_vi_activations"), [(replaced[0],)])

    async def test_cancellation_after_both_inserts_rolls_back_entire_cycle(self):
        before = mutation_snapshot()
        reached_commit = asyncio.Event()
        staged = []
        async def pause_commit(connection):
            for table in ("prestige_vi_activations", "purchase_history"):
                staged.append((await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0])
            reached_commit.set()
            await asyncio.Future()
        with patch.object(aiosqlite.Connection, "commit", new=pause_commit):
            task = asyncio.create_task(prestige.activate_booster_prestige(
                GUILD, USER, 0, item_id=self.item, bot=self.bot))
            try:
                await asyncio.wait_for(reached_commit.wait(), timeout=3)
                self.assertEqual(staged, [1, 1])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(mutation_snapshot(), before)

    async def test_unknown_member_shape_or_malformed_boost_context_preserves_activation(self):
        self.seed_activation()
        before = mutation_snapshot()
        for context in (SimpleNamespace(id=USER), SimpleNamespace(id=USER, premium_since=True),
                        SimpleNamespace(id=USER, premium_since="invalid"),
                        SimpleNamespace(id=USER, premium_since=datetime.now(timezone.utc) + timedelta(days=1))):
            with self.subTest(context=context):
                self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, member=context), 3)
                self.assertEqual(mutation_snapshot(), before)

    async def test_stale_departure_with_empty_cache_fetches_current_member_before_revoking(self):
        self.seed_activation()
        before = mutation_snapshot()
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(return_value=self.person)
        with patch("utils.prestige.sync_prestige_roles", new=AsyncMock()):
            await Boost(self.bot).on_member_remove(self.person)
        self.person.guild.fetch_member.assert_awaited_with(USER)
        self.assertEqual(mutation_snapshot(), before)

    async def test_departure_lookup_failure_does_not_destroy_a_possible_new_cycle(self):
        self.seed_activation()
        before = mutation_snapshot()
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(side_effect=RuntimeError("offline"))
        await Boost(self.bot).on_member_remove(self.person)
        self.assertEqual(mutation_snapshot(), before)

    async def test_uncached_expired_snapshot_is_refreshed_before_reconciling(self):
        self.seed_activation()
        fresh = member(True)
        fresh.guild = self.person.guild
        self.person.premium_since = None
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(return_value=fresh)
        before = mutation_snapshot()
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, member=self.person), 6)
        self.assertEqual(mutation_snapshot(), before)

    async def test_uncached_member_snapshot_with_failed_refresh_cannot_grant_or_revoke(self):
        self.seed_activation()
        self.person.guild.get_member = lambda uid: None
        self.person.guild.fetch_member = AsyncMock(side_effect=RuntimeError("offline"))
        before = mutation_snapshot()
        self.assertEqual(await prestige.get_effective_prestige(GUILD, USER, member=self.person), 3)
        self.assertEqual(mutation_snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
