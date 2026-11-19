"""Isolated prototype tests for the NILIVE Streak V2 path.

Run with a throwaway DATABASE_PATH (the fixture redirects every DB-using
module to its own TemporaryDirectory database before initialization).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Force a harmless import-time DB path even if a caller's shell exports a
# production DATABASE_PATH; fixtures then redirect each module to private temp DBs.
os.environ["OWNER_ID"] = "1"
os.environ["DATABASE_PATH"] = f"/tmp/nilive-streak-import-{os.getpid()}.sqlite"

import aiosqlite
import database
import utils.streak_settings as streak_settings
import utils.streak_notifications as streak_notifications
import utils.daily_engine as daily_engine
import utils.economy_safe as economy_safe
import utils.ledger as ledger
import utils.level_claims as level_claims
from utils.timezone import CAIRO_TZ


class StreakV2PrototypeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = TemporaryDirectory(prefix="nilive-streak-test-")
        self.db_path = str(Path(self.tempdir.name) / "isolated.sqlite")
        self._db_patches = []
        for module in (database, streak_settings, streak_notifications,
                       daily_engine, economy_safe, ledger, level_claims):
            p = patch.object(module, "DB_PATH", self.db_path)
            p.start()
            self._db_patches.append(p)
        await database.init_db()

        self._prestige_patch = patch(
            "utils.prestige.get_prestige_earn_multiplier",
            new_callable=AsyncMock,
        )
        self.prestige = self._prestige_patch.start()
        self.prestige.return_value = 1.0
        self._booster_patch = patch("utils.prestige.is_booster", new=Mock(return_value=False))
        self._booster_patch.start()

    async def asyncTearDown(self):
        self._booster_patch.stop()
        self._prestige_patch.stop()
        for p in reversed(self._db_patches):
            p.stop()
        self.tempdir.cleanup()

    async def configure(self, guild_id=10, *, stages=(100, 100, 100, 100, 100),
                        low=100, high=100, visibility="private", channel=None):
        return await streak_settings.save_streak_settings(guild_id, {
            "stages": list(stages),
            "stage6_min": low,
            "stage6_max": high,
            "receipt_visibility": visibility,
            "receipt_channel_id": channel,
        })

    async def claim(self, guild=10, user=20, *, when=None, base=None):
        when = when or datetime(2026, 1, 1, 12, tzinfo=CAIRO_TZ)
        return await daily_engine.perform_streak_claim(
            None, guild, user, now=when, base_amount=base)

    async def scalar(self, query, args=()):
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(query, args)).fetchone()
        return row[0] if row else None

    async def test_01_progression_stages_and_unbounded_stored_count(self):
        # S1-style table: fixed 2/4/6/8/10, then random 13..18 every day.
        await self.configure(stages=(2, 4, 6, 8, 10), low=13, high=18)
        results = []
        start = date(2026, 1, 5)
        for offset in range(8):
            when = datetime.combine(start + timedelta(days=offset),
                                    datetime.min.time(), tzinfo=CAIRO_TZ) + timedelta(hours=12)
            results.append(await self.claim(when=when))
        self.assertEqual([r["stage"] for r in results], [1, 2, 3, 4, 5, 6, 6, 6])
        self.assertEqual([r["streak"] for r in results], list(range(1, 9)))
        self.assertEqual([r["amount"] for r in results[:5]], [2, 4, 6, 8, 10])
        self.assertTrue(all(13 <= r["amount"] <= 18 for r in results[5:]))
        self.assertTrue(all(r["streak_bonus"] == 0 and r["v2_enabled"] for r in results))
        self.assertEqual(await self.scalar(
            "SELECT streak_count FROM daily_claims WHERE guild_id=10 AND user_id=20"), 8)

    async def test_02_stage_six_range_never_cycles_for_day_seven_plus(self):
        table = {"stages": [2, 4, 6, 8, 10], "stage6_min": 13, "stage6_max": 18}
        for day in (6, 7, 30, 365):
            self.assertEqual(streak_settings.stage_reward_range(day, table), (13, 18))
        self.assertEqual([streak_settings.stage_reward_range(d, table) for d in range(1, 6)],
                         [(2, 2), (4, 4), (6, 6), (8, 8), (10, 10)])

    async def test_03_cairo_break_boundary_and_zero_state(self):
        await self.configure()
        monday = datetime(2026, 1, 5, 12, tzinfo=CAIRO_TZ)
        await self.claim(when=monday)
        tuesday = datetime(2026, 1, 6, 0, 0, tzinfo=CAIRO_TZ)
        wednesday = datetime(2026, 1, 7, 0, 0, tzinfo=CAIRO_TZ)
        state_tue = await daily_engine.get_streak_state(10, 20, now=tuesday)
        state_wed = await daily_engine.get_streak_state(10, 20, now=wednesday)
        self.assertEqual(state_tue["streak"], 1)
        self.assertFalse(state_tue["broken"])
        self.assertEqual(state_wed["streak"], 0)
        self.assertTrue(state_wed["broken"])
        self.assertEqual(state_wed["days_since_last_claim"], 2)
        self.assertEqual(state_wed["last_claim_date"], "2026-01-05")

    async def test_04_guild_specific_reward_tables_are_isolated(self):
        await self.configure(10, stages=(2, 4, 6, 8, 10), low=13, high=18)
        await self.configure(11, stages=(15, 30, 45, 60, 75), low=80, high=100)
        first = await streak_settings.get_streak_settings(10)
        second = await streak_settings.get_streak_settings(11)
        self.assertEqual(first["reward_table"],
                         {"stages": [2, 4, 6, 8, 10], "stage6_min": 13, "stage6_max": 18})
        self.assertEqual(second["reward_table"],
                         {"stages": [15, 30, 45, 60, 75], "stage6_min": 80, "stage6_max": 100})
        self.assertEqual(first["receipt_mode_source"], "guild")
        # Same member id in both guilds: independent streaks and rewards.
        a = await self.claim(guild=10, user=20)
        b = await self.claim(guild=11, user=20)
        self.assertEqual((a["amount"], b["amount"]), (2, 15))
        a2 = await self.claim(guild=10, user=20, when=datetime(2026, 1, 2, 12, tzinfo=CAIRO_TZ))
        self.assertEqual(a2["amount"], 4)
        self.assertEqual(await self.scalar(
            "SELECT streak_count FROM daily_claims WHERE guild_id=11 AND user_id=20"), 1)
        # A third, unconfigured guild is untouched by guilds 10 and 11.
        self.assertIsNone((await streak_settings.get_streak_settings(12))["reward_table"])
        # Public mode may intentionally have no configured channel; reward
        # execution still proceeds and receipt delivery reports unavailable.
        await self.configure(12, visibility="public", channel=None)
        optional_channel = await streak_settings.get_streak_settings(12)
        self.assertEqual(optional_channel["receipt_visibility"], "public")
        self.assertIsNone(optional_channel["receipt_channel_id"])

    async def test_05_unconfigured_guild_keeps_legacy_reward_behavior(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO bot_settings (key,value) VALUES (?,?)",
                [("daily_min", "100"), ("daily_max", "300"),
                 ("daily_streak_bonus_per_day", "17")],
            )
            await db.commit()
        config = await streak_settings.get_streak_settings(999)
        self.assertFalse(config["v2_enabled"])
        self.assertIsNone(config["reward_table"])
        self.assertEqual(config["receipt_mode_source"], "default")
        self.assertEqual((config["daily_min"], config["daily_max"]), (100, 300))
        result = await self.claim(guild=999, base=100)
        self.assertEqual(result["streak_bonus"], 17)
        self.assertEqual(result["amount"], 117)

    async def test_06_partial_or_corrupt_table_is_rejected_or_ignored(self):
        v = streak_settings.validate_streak_settings
        self.assertIsNone(v(["", "", "", "", ""], "", "")["table"])
        with self.assertRaises(ValueError):
            v(["1", "2", "", "4", "5"], "6", "7")          # partial
        with self.assertRaises(ValueError):
            v(["1", "2", "3", "4", "5"], "9", "8")         # max < min
        with self.assertRaises(ValueError):
            v(["0", "2", "3", "4", "5"], "6", "7")         # below 1
        with self.assertRaises(ValueError):
            v(["1", "2", "3", "4", "5"], "6", "1000000001")
        with self.assertRaises(ValueError):
            v(["1", "2", "3", "4"], "6", "7")              # wrong stage count
        # A table with a missing key in storage falls back as a whole.
        await self.configure(10, stages=(2, 4, 6, 8, 10), low=13, high=18)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM guild_settings_kv WHERE guild_id=10 AND key=?",
                             (streak_settings.STAGE6_MAX_KEY,))
            await db.commit()
        self.assertFalse((await streak_settings.get_streak_settings(10))["v2_enabled"])
        # Blank save removes the table: explicit path back to legacy.
        await self.configure(11, stages=(2, 4, 6, 8, 10), low=13, high=18)
        await streak_settings.save_streak_settings(11, {
            "stages": ["", "", "", "", ""], "stage6_min": "", "stage6_max": ""})
        self.assertFalse((await streak_settings.get_streak_settings(11))["v2_enabled"])

    async def test_07_prestige_applies_once_to_stage_reward_and_ledger(self):
        await self.configure(stages=(200, 200, 200, 200, 200), low=200, high=200)
        self.prestige.return_value = 1.1
        result = await self.claim()
        self.assertEqual(result["base"], 200)
        self.assertEqual(result["streak_bonus"], 0)
        self.assertEqual(result["pre_prestige_total"], 200)
        self.assertEqual(result["credited_total"], int(round(200 * 1.1)))
        self.assertEqual(result["amount"], result["credited_total"])
        self.assertEqual(await self.scalar(
            "SELECT balance FROM economy WHERE guild_id=10 AND user_id=20"), 220)
        self.assertEqual(await self.scalar(
            "SELECT amount FROM transaction_ledger WHERE guild_id=10 AND user_id=20"), 220)
        self.assertEqual(await self.scalar(
            "SELECT COUNT(*) FROM transaction_ledger WHERE guild_id=10 AND user_id=20"), 1)
        self.assertEqual(self.prestige.await_count, 1)

    async def test_08_concurrent_claims_award_and_roll_once(self):
        await self.configure()
        when = datetime(2026, 1, 5, 12, tzinfo=CAIRO_TZ)
        with patch("utils.daily_engine.random.randint", return_value=100) as roll:
            results = await asyncio.gather(
                daily_engine.perform_streak_claim(None, 10, 20, now=when),
                daily_engine.perform_streak_claim(None, 10, 20, now=when),
                return_exceptions=True,
            )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, daily_engine.DailyAlreadyClaimed) for r in results), 1)
        self.assertEqual(roll.call_count, 1)
        self.assertEqual(await self.scalar(
            "SELECT balance FROM economy WHERE guild_id=10 AND user_id=20"), 100)
        self.assertEqual(await self.scalar(
            "SELECT COUNT(*) FROM transaction_ledger WHERE guild_id=10 AND user_id=20"), 1)

    async def test_09_failed_credit_rolls_back_claim_and_retry_succeeds(self):
        await self.configure()
        when = datetime(2026, 2, 1, 12, tzinfo=CAIRO_TZ)
        with patch("utils.economy_safe.safe_credit", new=AsyncMock(side_effect=RuntimeError("injected credit failure"))):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await daily_engine.perform_streak_claim(None, 10, 20, now=when, base_amount=100)
        self.assertIsNone(await self.scalar(
            "SELECT last_claim_date FROM daily_claims WHERE guild_id=10 AND user_id=20"))
        self.assertIsNone(await self.scalar(
            "SELECT balance FROM economy WHERE guild_id=10 AND user_id=20"))
        result = await self.claim(when=when, base=100)
        self.assertEqual(result["amount"], 100)

    async def test_10_scheduler_catchup_enqueues_old_breaks(self):
        now = datetime.now(timezone.utc)
        today = now.astimezone(CAIRO_TZ).date()
        old = (today - timedelta(days=2)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO daily_claims(guild_id,user_id,last_claim_date,streak_count)
                VALUES(10,20,?,4)
            """, (old,))
            await db.commit()
        self.assertEqual(await streak_notifications.enqueue_due_streak_breaks(now), 1)
        self.assertEqual(await streak_notifications.enqueue_due_streak_breaks(now), 0)
        row = await self.scalar("SELECT status FROM streak_break_notifications")
        self.assertEqual(row, "pending")

        # A returning member may claim before the periodic sweep. The claim
        # transaction must persist the old episode's notice before replacing
        # the daily_claims row, so downtime cannot erase it.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO daily_claims(guild_id,user_id,last_claim_date,streak_count)
                VALUES(10,21,?,3)
            """, (old,))
            await db.commit()
        await daily_engine.perform_streak_claim(
            None, 10, 21, now=now, base_amount=100)
        pending_after_return = await self.scalar(
            "SELECT status FROM streak_break_notifications WHERE user_id=21")
        self.assertEqual(pending_after_return, "pending")

    async def test_11_break_dm_dispatch_is_private_and_idempotent(self):
        now = datetime.now(timezone.utc)
        old = (now.astimezone(CAIRO_TZ).date() - timedelta(days=2)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO daily_claims(guild_id,user_id,last_claim_date,streak_count)
                VALUES(10,20,?,5)
            """, (old,))
            await db.commit()
        channel = FakeDMChannel(bot_user_id=999)
        bot = FakeBot(channel)
        first = await streak_notifications.dispatch_streak_break_notifications(bot, now=now)
        second = await streak_notifications.dispatch_streak_break_notifications(bot, now=now)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(await self.scalar("SELECT status FROM streak_break_notifications"), "sent")
        self.assertIn("Stage 1", channel.sent[0]["embed"].description)

    async def test_12_ambiguous_dm_retry_checks_marker_before_resending(self):
        now = datetime.now(timezone.utc)
        old = (now.astimezone(CAIRO_TZ).date() - timedelta(days=2)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO daily_claims(guild_id,user_id,last_claim_date,streak_count)
                VALUES(10,20,?,2)
            """, (old,))
            await db.commit()
        channel = FakeDMChannel(bot_user_id=999, ambiguous_first_send=True)
        bot = FakeBot(channel)
        first = await streak_notifications.dispatch_streak_break_notifications(bot, now=now)
        second = await streak_notifications.dispatch_streak_break_notifications(
            bot, now=now + timedelta(minutes=5))
        self.assertEqual(first["retried"], 1)
        self.assertEqual(second["deduplicated"], 1)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(await self.scalar("SELECT status FROM streak_break_notifications"), "sent")

    async def test_13_gif_order_and_missing_asset_fallback(self):
        import cogs.wallet as wallet
        expected = {
            1: "streak_2.gif", 2: "streak_3.gif", 3: "streak_4.gif",
            4: "streak_5.gif", 5: "streak_6.gif", 6: "streak_7.gif",
        }
        self.assertEqual(wallet.STREAK_STAGE_FILENAMES, expected)
        for stage, name in expected.items():
            path = wallet.streak_stage_path(stage)
            self.assertIsNotNone(path)
            self.assertEqual(path.name, name)
        with TemporaryDirectory() as empty:
            self.assertIsNone(wallet.streak_stage_path(1, Path(empty)))
            embed = wallet.discord.Embed(title="No asset")
            with patch.object(wallet, "STREAK_ASSET_DIR", Path(empty)):
                wallet._apply_stage_image(embed, 1)
            self.assertIsNone(embed.image.url)

    async def test_14_unavailable_public_channel_does_not_raise_or_change_result(self):
        import cogs.wallet as wallet
        result = {
            "guild_id": 10, "user_id": 20, "receipt_channel_id": "123",
            "receipt_public": True, "stage": 1, "streak": 1,
            "base": 100, "streak_bonus": 0, "pre_prestige_total": 100,
            "multiplier": 1, "amount": 100, "credited_total": 100,
            "next_reset_seconds": 1, "member_name": "Member",
        }
        before = dict(result)
        bot = SimpleNamespace(get_channel=Mock(return_value=None),
                              fetch_channel=AsyncMock(side_effect=RuntimeError("missing")))
        sent = await wallet.send_public_claim_receipt(bot, result, {"coins": {"emoji": "🪙", "name": "Coin"}})
        self.assertFalse(sent)
        self.assertEqual(result, before)

    async def test_15_receipt_modes_share_calculated_result(self):
        import cogs.wallet as wallet
        self.assertEqual(streak_settings.receipt_destinations("private"), (True, False))
        self.assertEqual(streak_settings.receipt_destinations("public"), (False, True))
        self.assertEqual(streak_settings.receipt_destinations("both"), (True, True))
        result = {
            "guild_id": 10, "user_id": 20, "member_name": "Test Member",
            "streak": 3, "stage": 3, "base": 200, "streak_bonus": 33,
            "pre_prestige_total": 233, "multiplier": 1.1,
            "amount": 256, "credited_total": 256, "next_reset_seconds": 3600,
        }
        currency = {"coins": {"emoji": "🪙", "name": "Coins"}}
        private = wallet.build_claim_embed(result, currency)
        public = wallet.build_claim_embed(result, currency)
        self.assertEqual(private.to_dict(), public.to_dict())
        self.assertIn("256 Coins credited", private.fields[2].value)
        self.assertIn("<@20>", private.description)

    async def test_16_config_change_applies_next_claim_without_reset(self):
        await self.configure(stages=(10, 20, 30, 40, 50), low=60, high=70)
        day1 = datetime(2026, 3, 1, 12, tzinfo=CAIRO_TZ)
        first = await self.claim(when=day1)
        await self.configure(stages=(10, 99, 30, 40, 50), low=60, high=70)
        second = await self.claim(when=day1 + timedelta(days=1))
        self.assertEqual(first["streak"], 1)
        self.assertEqual(first["amount"], 10)
        self.assertEqual(second["streak"], 2)
        self.assertEqual(second["stage"], 2)
        self.assertEqual(second["amount"], 99)
        self.assertEqual(await self.scalar(
            "SELECT streak_count FROM daily_claims WHERE guild_id=10 AND user_id=20"), 2)

    async def test_17_slash_and_wallet_entry_open_the_same_claim_panel(self):
        import cogs.wallet as wallet
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10), user=SimpleNamespace(id=20),
        )
        with patch.object(wallet, "open_streak_panel", new=AsyncMock()) as opened:
            await wallet.Wallet.streak.callback(None, interaction)
            self.assertTrue(opened.await_args.kwargs["first"])
            opened.reset_mock()
            view = wallet.WalletHubView(10, 20, claimed_today=False)
            await view.streak_button.callback(interaction)
            self.assertEqual(opened.await_count, 1)
            self.assertNotIn("first", opened.await_args.kwargs)
        # Both entry points render StreakPanelView; its only claim callback
        # calls the shared perform_streak_claim engine.
        self.assertTrue(hasattr(wallet.StreakPanelView, "claim_button"))
        source = Path(wallet.__file__).read_text(encoding="utf-8")
        self.assertIn("result = await perform_streak_claim(", source)

    async def test_18_broken_streak_panel_explains_cairo_break_and_stage_one_restart(self):
        import cogs.wallet as wallet
        currency = {"coins": {"emoji": "🪙", "name": "Coins"}}
        embed = wallet.build_streak_embed({
            "broken": True,
            "stage": 1,
            "last_claim_date": "2026-11-03",
            "days_since_last_claim": 3,
        }, currency, member_name="Test Member")
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("full Cairo calendar day", embed.description)
        self.assertIn("Stage 1", embed.description)
        self.assertEqual(fields["Current Streak"], "**0 days**")
        self.assertEqual(fields["Last successful claim"], "2026-11-03")
        self.assertEqual(fields["Days since last claim"], "3 Cairo calendar days")
        self.assertEqual(embed.image.url, "attachment://streak_2.gif")

    async def test_19_claim_and_private_acknowledgements_reconcile_all_receipt_modes(self):
        import cogs.wallet as wallet
        currency = {"coins": {"emoji": "🪙", "name": "Coins"}}
        result = {
            "guild_id": 10, "user_id": 20, "member_name": "Test Member",
            "streak": 3, "stage": 3, "base": 200, "streak_bonus": 33,
            "pre_prestige_total": 233, "multiplier": 1.1,
            "amount": 256, "credited_total": 256, "next_reset_seconds": 3600,
        }
        receipt = wallet.build_claim_embed(result, currency, public_copy_sent=False)
        fields = {field.name: field.value for field in receipt.fields}
        self.assertIn("**200** Coins (base roll)", fields["Daily Rewards"])
        self.assertIn("+**33** Coins", fields["Streak Bonus · Stage 3"])
        self.assertIn("(200 + 33) = 233 before Prestige", fields["Reward calculation"])
        self.assertIn("Prestige ×1.1 → **256 Coins credited**", fields["Reward calculation"])
        self.assertIn("still credited", fields["Public receipt"])

        public_ack = wallet.build_public_only_ack_embed(result, True, currency)
        failed_ack = wallet.build_public_only_ack_embed(result, False, currency)
        self.assertIn("**256 Coins** credited", public_ack.description)
        self.assertIn("could not be reached", failed_ack.description)
        self.assertIn("No alternate channel was used", failed_ack.description)
        self.assertEqual(streak_settings.receipt_destinations("private"), (True, False))
        self.assertEqual(streak_settings.receipt_destinations("public"), (False, True))
        self.assertEqual(streak_settings.receipt_destinations("both"), (True, True))

    async def test_20_dashboard_validation_limits_and_optional_channel(self):
        five = ["1", "2", "3", "4", "5"]
        valid = streak_settings.validate_streak_settings(
            ["1", "1", "1", "1", "1000000000"], "1", "1000000000", "both", "123456789012345678")
        self.assertEqual(valid["table"]["stages"][-1], 1_000_000_000)
        self.assertEqual(valid["table"]["stage6_max"], 1_000_000_000)
        self.assertEqual(valid["receipt_channel_id"], "123456789012345678")
        no_channel = streak_settings.validate_streak_settings(five, "6", "7", "public", "")
        self.assertIsNone(no_channel["receipt_channel_id"])
        private_ignores_channel = streak_settings.validate_streak_settings(
            five, "6", "7", "private", "not-a-channel")
        self.assertIsNone(private_ignores_channel["receipt_channel_id"])
        with self.assertRaises(ValueError):
            streak_settings.validate_streak_settings(five, "6", "7", "public", "bad")
        with self.assertRaises(ValueError):
            streak_settings.validate_streak_settings(five, "6", "7", "unknown")
        with self.assertRaises(ValueError):
            streak_settings.validate_streak_settings(["1.5", "2", "3", "4", "5"], "6", "7")

    async def test_21_forbidden_break_dm_is_terminal_and_private(self):
        now = datetime.now(timezone.utc)
        old = (now.astimezone(CAIRO_TZ).date() - timedelta(days=2)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO daily_claims(guild_id,user_id,last_claim_date,streak_count)
                VALUES(10,20,?,4)
            """, (old,))
            await db.commit()

        class Forbidden(Exception):
            pass

        channel = FakeDMChannel(bot_user_id=999)
        channel.send = AsyncMock(side_effect=Forbidden("DM closed"))
        bot = FakeBot(channel)
        first = await streak_notifications.dispatch_streak_break_notifications(bot, now=now)
        second = await streak_notifications.dispatch_streak_break_notifications(
            bot, now=now + timedelta(minutes=5))
        self.assertEqual(first["blocked"], 1)
        self.assertEqual(second["blocked"], 0)
        self.assertEqual(channel.send.await_count, 1)
        self.assertEqual(await self.scalar("SELECT status FROM streak_break_notifications"), "blocked")

    async def test_22_public_receipt_success_uses_selected_guild_and_no_mentions(self):
        import cogs.wallet as wallet

        class ReceiptChannel:
            def __init__(self):
                self.guild = SimpleNamespace(id=10)
                self.sent = []

            async def send(self, **kwargs):
                self.sent.append(kwargs)

        channel = ReceiptChannel()
        bot = SimpleNamespace(get_channel=Mock(return_value=channel))
        result = {
            "guild_id": 10, "user_id": 20, "member_name": "Test Member",
            "receipt_public": True, "receipt_channel_id": "123",
            "streak": 1, "stage": 1, "base": 100, "streak_bonus": 0,
            "pre_prestige_total": 100, "multiplier": 1.0,
            "amount": 100, "credited_total": 100, "next_reset_seconds": 3600,
        }
        sent = await wallet.send_public_claim_receipt(
            bot, result, {"coins": {"emoji": "", "name": "Coins"}})
        self.assertTrue(sent)
        self.assertEqual(len(channel.sent), 1)
        payload = channel.sent[0]
        self.assertEqual(payload["allowed_mentions"].to_dict(), {"parse": []})
        self.assertIn("Test Member", payload["embed"].description)
        self.assertEqual(payload["file"].filename, "streak_2.gif")
        payload["file"].close()

    async def test_23_dashboard_api_get_post_validation_and_csrf(self):
        from flask import Flask
        import dashboard.auth as auth
        import dashboard.permissions as permissions
        import dashboard.api.economy_shop as economy_api
        from dashboard.api import api_bp

        app = Flask(__name__)
        app.secret_key = "isolated-test-only"
        app.register_blueprint(api_bp, url_prefix="/api")
        with (
            patch.object(permissions, "DB_PATH", self.db_path),
            patch.object(auth, "is_session_valid", return_value=True),
            patch.object(auth, "refresh_session_if_needed"),
            patch.object(permissions, "_get_permission_level",
                         new=AsyncMock(return_value=permissions.LEVEL_OWNER)),
            patch.object(economy_api, "log_action"),
        ):
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["user"] = {"id": "1", "username": "Test Admin"}
                sess["guild_id"] = 10
                sess["csrf_token"] = "test-token"

            initial = client.get("/api/economy/streak-settings")
            self.assertEqual(initial.status_code, 200)
            self.assertIsNone(initial.json["settings"]["reward_table"])
            self.assertEqual(initial.json["settings"]["receipt_visibility"], "private")

            no_csrf = client.post("/api/economy/streak-settings", json={})
            self.assertEqual(no_csrf.status_code, 403)

            saved = client.post(
                "/api/economy/streak-settings",
                json={"stages": ["2", "4", "6", "8", "10"],
                      "stage6_min": "13", "stage6_max": "18",
                      "receipt_visibility": "both",
                      "receipt_channel_id": "123"},
                headers={"X-CSRF-Token": "test-token"})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json["settings"]["reward_table"],
                             {"stages": [2, 4, 6, 8, 10], "stage6_min": 13, "stage6_max": 18})
            self.assertEqual(saved.json["settings"]["receipt_visibility"], "both")

            invalid = client.post(
                "/api/economy/streak-settings",
                json={"stages": ["2", "4", "6", "8", "10"],
                      "stage6_min": "18", "stage6_max": "13",
                      "receipt_visibility": "both"},
                headers={"X-CSRF-Token": "test-token"})
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("maximum", invalid.json["error"])


    async def _run_days(self, guild, days, *, roll):
        start = date(2026, 4, 1)
        out = []
        for offset in range(days):
            when = datetime.combine(start + timedelta(days=offset),
                                    datetime.min.time(), tzinfo=CAIRO_TZ) + timedelta(hours=12)
            with patch("utils.daily_engine.random.randint", side_effect=roll):
                out.append(await self.claim(guild=guild, when=when))
        return out

    async def test_24_seven_day_totals_match_plan_for_both_servers(self):
        await self.configure(10, stages=(2, 4, 6, 8, 10), low=13, high=18)
        await self.configure(11, stages=(15, 30, 45, 60, 75), low=80, high=100)
        lo = lambda a, b: a
        hi = lambda a, b: b
        s1_lo = sum(r["amount"] for r in await self._run_days(10, 7, roll=lo))
        self.assertEqual(s1_lo, 56)
        # Fresh member for the max run so streaks do not collide.
        async def run_max(guild, user):
            start = date(2026, 5, 1)
            total = 0
            for offset in range(7):
                when = datetime.combine(start + timedelta(days=offset),
                                        datetime.min.time(), tzinfo=CAIRO_TZ) + timedelta(hours=12)
                with patch("utils.daily_engine.random.randint", side_effect=hi):
                    total += (await self.claim(guild=guild, user=user, when=when))["amount"]
            return total
        self.assertEqual(await run_max(10, 21), 66)
        s2_lo = sum(r["amount"] for r in await self._run_days(11, 7, roll=lo))
        self.assertEqual(s2_lo, 385)
        self.assertEqual(await run_max(11, 21), 425)

    async def test_25_day_six_onward_stays_in_range_for_many_days(self):
        await self.configure(10, stages=(2, 4, 6, 8, 10), low=13, high=18)
        start = date(2026, 6, 1)
        seen = set()
        for offset in range(40):
            when = datetime.combine(start + timedelta(days=offset),
                                    datetime.min.time(), tzinfo=CAIRO_TZ) + timedelta(hours=12)
            r = await self.claim(when=when)
            if offset >= 5:
                self.assertEqual(r["stage"], 6)
                self.assertTrue(13 <= r["amount"] <= 18, r)
                seen.add(r["amount"])
        self.assertGreater(len(seen), 1)  # genuinely random, not a fixed value

    async def test_26_missed_day_resets_next_claim_to_day_one_amount(self):
        await self.configure(10, stages=(2, 4, 6, 8, 10), low=13, high=18)
        start = date(2026, 7, 1)
        for offset in range(4):
            when = datetime.combine(start + timedelta(days=offset),
                                    datetime.min.time(), tzinfo=CAIRO_TZ) + timedelta(hours=12)
            last = await self.claim(when=when)
        self.assertEqual((last["streak"], last["amount"]), (4, 8))
        skipped = datetime.combine(start + timedelta(days=6),
                                   datetime.min.time(), tzinfo=CAIRO_TZ) + timedelta(hours=12)
        after = await self.claim(when=skipped)
        self.assertEqual((after["streak"], after["stage"], after["amount"]), (1, 1, 2))
        self.assertEqual(after["previous_streak"], 4)

    async def test_27_v2_receipt_and_preview_show_stage_reward_then_prestige(self):
        import cogs.wallet as wallet
        currency = {"coins": {"emoji": "🪙", "name": "Coins"}}
        result = {
            "guild_id": 10, "user_id": 20, "member_name": "Test Member",
            "streak": 6, "stage": 6, "base": 15, "streak_bonus": 0,
            "pre_prestige_total": 15, "multiplier": 1.2, "amount": 18,
            "credited_total": 18, "next_reset_seconds": 3600, "v2_enabled": True,
        }
        embed = wallet.build_claim_embed(result, currency)
        fields = {f.name: f.value for f in embed.fields}
        self.assertIn("**15** Coins · before Prestige", fields["Stage 6 Reward"])
        self.assertIn("Prestige ×1.2", fields["Reward calculation"])
        self.assertIn("**18 Coins credited**", fields["Reward calculation"])
        self.assertNotIn("Streak Bonus", " ".join(fields))
        await self.configure(10, stages=(2, 4, 6, 8, 10), low=13, high=18)
        preview = await daily_engine.get_streak_preview(
            10, 20, now=datetime(2026, 8, 1, 12, tzinfo=CAIRO_TZ))
        self.assertEqual((preview["stage"], preview["daily_min"], preview["daily_max"]), (1, 2, 2))
        self.assertTrue(preview["v2_enabled"])


    async def test_28_both_guilds_save_and_reload_all_seven_settings_independently(self):
        from flask import Flask
        import dashboard.auth as auth
        import dashboard.permissions as permissions
        import dashboard.api.economy_shop as economy_api
        from dashboard.api import api_bp

        app = Flask(__name__)
        app.secret_key = "isolated-test-only"
        app.register_blueprint(api_bp, url_prefix="/api")
        tables = {
            1280983591148584971: ([2, 4, 6, 8, 10], 13, 18),
            1405993421167919336: ([15, 30, 45, 60, 75], 80, 100),
        }
        with (
            patch.object(permissions, "DB_PATH", self.db_path),
            patch.object(auth, "is_session_valid", return_value=True),
            patch.object(auth, "refresh_session_if_needed"),
            patch.object(permissions, "_get_permission_level",
                         new=AsyncMock(return_value=permissions.LEVEL_OWNER)),
            patch.object(economy_api, "log_action"),
        ):
            client = app.test_client()

            def as_guild(guild_id):
                with client.session_transaction() as sess:
                    sess["user"] = {"id": "1", "username": "Test Admin"}
                    sess["guild_id"] = guild_id
                    sess["csrf_token"] = "test-token"

            for guild_id, (stages, low, high) in tables.items():
                as_guild(guild_id)
                saved = client.post(
                    "/api/economy/streak-settings",
                    json={"stages": [str(v) for v in stages],
                          "stage6_min": str(low), "stage6_max": str(high),
                          "receipt_visibility": "private"},
                    headers={"X-CSRF-Token": "test-token"})
                self.assertEqual(saved.status_code, 200, saved.json)
            # Reload each guild after BOTH were saved: no cross-contamination.
            for guild_id, (stages, low, high) in tables.items():
                as_guild(guild_id)
                got = client.get("/api/economy/streak-settings").json["settings"]
                self.assertEqual(got["reward_table"],
                                 {"stages": stages, "stage6_min": low, "stage6_max": high})
                async with aiosqlite.connect(self.db_path) as db:
                    keys = {r[0]: r[1] for r in await (await db.execute(
                        "SELECT key, value FROM guild_settings_kv WHERE guild_id=? "
                        "AND key LIKE 'streak_v2_%'", (guild_id,))).fetchall()}
                self.assertEqual(sorted(keys), sorted(streak_settings.REWARD_KEYS))
                self.assertEqual(len(keys), 7)
        # No global/default reward values were written anywhere.
        async with aiosqlite.connect(self.db_path) as db:
            global_rows = await (await db.execute(
                "SELECT COUNT(*) FROM bot_settings WHERE key LIKE 'streak_v2_%'")).fetchone()
        self.assertEqual(global_rows[0], 0)
        # An unconfigured third guild gets no table (no hardcoded defaults).
        self.assertIsNone((await streak_settings.get_streak_settings(777))["reward_table"])


class FakeMessage:
    def __init__(self, bot_user_id, embed):
        self.author = SimpleNamespace(id=bot_user_id)
        self.embeds = [embed]


class FakeDMChannel:
    def __init__(self, bot_user_id, ambiguous_first_send=False):
        self.bot_user_id = bot_user_id
        self.ambiguous_first_send = ambiguous_first_send
        self.sent = []
        self.send_calls = 0

    async def history(self, limit=100):
        for item in reversed(self.sent[-limit:]):
            yield FakeMessage(self.bot_user_id, item["embed"])

    async def send(self, **kwargs):
        self.send_calls += 1
        self.sent.append(kwargs)
        if self.ambiguous_first_send and self.send_calls == 1:
            raise TimeoutError("Discord may have accepted the first DM")


class FakeUser:
    def __init__(self, channel):
        self.channel = channel

    async def create_dm(self):
        return self.channel


class FakeBot:
    def __init__(self, channel):
        self.user = SimpleNamespace(id=999)
        self.user_for_dm = FakeUser(channel)

    def get_user(self, user_id):
        return self.user_for_dm if int(user_id) == 20 else None

    async def fetch_user(self, user_id):
        return self.get_user(user_id)
