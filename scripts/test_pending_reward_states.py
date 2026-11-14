#!/usr/bin/env python3
"""
Held rewards: the state machine, the member notices and the table itself.

A reward the Inventory refuses over an item-name clash is kept in
`pending_rewards` (utils/pending_rewards.py). This file drives the real code
on a scratch SQLite database (only Discord is stubbed) and locks down:

  STATES
    pending       stored, waiting, retried automatically
    delivered     delivery confirmed, never paid again
    needs_review  outcome unknown - NEVER paid again automatically; staff
                  decide (retry / mark delivered) from the dashboard
    ...across concurrent retries, scheduler sweeps, the member's own retry and
    a bot restart.

  NOTICES
    The member is told at once, by the Mission and by the Minigame, that the
    reward is held - never that it was delivered, never twice, and the
    completion itself is unchanged.

  DATABASE
    The table is added to an existing database without touching anything
    else, is safe to create repeatedly, and is indexed for its two readers.

Run:  python3 scripts/test_pending_reward_states.py
"""
import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import GUILD, USER, execute, rows, reset_database

from cogs import wallet  # noqa: E402
from cogs.tagmissions import TagMissions  # noqa: E402
from cogs.tagpartners import TagPartners  # noqa: E402
from database import init_db  # noqa: E402
from utils import (  # noqa: E402
    inventory, minigame_engine, minigame_store, mission_engine,
    pending_rewards, reward_engine,
)
import dashboard.app as dashboard  # noqa: E402
from test_inventory_actions import FLAME  # noqa: E402
from test_reward_name_conflicts import (  # noqa: E402
    RewardPreservationCase, legacy_mission, stack, POTION_META,
    OTHER_USER, OTHER_GUILD,
)

CSRF = "pending-csrf"
MISSION_NOTICE = ("Your mission is complete! Your reward is waiting for "
                  "delivery because of an item conflict.")
MINIGAME_NOTICE = ("You won! Your reward is temporarily held and will be "
                   "delivered once the issue is resolved.")


def held_rows(guild=GUILD):
    return rows("SELECT id, user_id, status, attempts FROM pending_rewards "
                "WHERE guild_id=? ORDER BY id", (guild,))


def not_granted(name="Tonic", user=USER):
    """The freed name holds nothing: no row, or the emptied potion row."""
    return stack(name, user) in (None, ("potion", 0))


def status_of(row_id):
    return rows("SELECT status FROM pending_rewards WHERE id=?", (row_id,))[0][0]


def age_claim(row_id, minutes):
    """Pretend the row was claimed `minutes` ago (a process that stopped)."""
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    execute("UPDATE pending_rewards SET status='delivering', updated_at=? "
            "WHERE id=?", (stamp, row_id))


class HeldRewardCase(RewardPreservationCase):
    async def hold_item(self, user=USER, guild=GUILD, name="Tonic",
                        source="mission"):
        """A member owns a potion `name`; a plain `name` reward is refused."""
        await inventory.give_item(guild, user, name, 1, item_type="potion",
                                  metadata=POTION_META)
        result = await reward_engine.give_reward(
            self.bot, guild, user, "item", item_name=name,
            reason="Mission complete: Chatter", source=source)
        self.assertTrue(result["pending"], result)
        return result["pending_id"]

    async def free_name(self, user=USER, guild=GUILD, name="Tonic"):
        await inventory.remove_item(guild, user, name, 1)


# ═══ 1. states ══════════════════════════════════════════════════════════

class PendingStateTests(HeldRewardCase):
    async def test_pending_row_is_complete_and_correctly_associated(self):
        row_id = await self.hold_item(source="minigame")
        other = await self.hold_item(user=OTHER_USER, guild=OTHER_GUILD)
        stored = rows("SELECT guild_id, user_id, reward_type, source, reason, "
                      "status, attempts, delivered_at, payload FROM "
                      "pending_rewards WHERE id=?", (row_id,))[0]
        self.assertEqual(stored[:8], (GUILD, USER, "item", "minigame",
                                      "Mission complete: Chatter", "pending",
                                      1, None))
        payload = json.loads(stored[8])
        self.assertEqual(payload["item_name"], "Tonic")
        self.assertEqual(payload["source"], "minigame")
        # Each guild and member sees only its own.
        self.assertEqual([r["id"] for r in await pending_rewards.list_pending(GUILD)],
                         [row_id])
        self.assertEqual(
            [r["id"] for r in await pending_rewards.list_pending(OTHER_GUILD, OTHER_USER)],
            [other])
        self.assertEqual(await pending_rewards.list_pending(GUILD, OTHER_USER), [])
        # Freeing the name for one member delivers only that member's reward.
        await self.free_name()
        await pending_rewards.retry(self.bot)
        self.assertEqual((status_of(row_id), status_of(other)),
                         ("delivered", "pending"))
        self.assertEqual(stack("Tonic", OTHER_USER, OTHER_GUILD), ("potion", 1))

    async def test_delivered_is_final(self):
        row_id = await self.hold_item()
        await self.free_name()
        self.assertEqual((await pending_rewards.retry(self.bot))["delivered"], 1)
        delivered_at = rows("SELECT status, delivered_at FROM pending_rewards")[0]
        self.assertEqual(delivered_at[0], "delivered")
        self.assertIsNotNone(delivered_at[1])
        for _ in range(3):                           # sweeps, member retry
            await pending_rewards.retry(self.bot)
            await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(stack("Tonic"), ("custom", 1))
        # Staff cannot reopen a delivered reward either.
        self.assertFalse(await pending_rewards.resolve_review(GUILD, row_id, "retry"))
        self.assertEqual(status_of(row_id), "delivered")

    async def test_scheduler_member_and_manual_retries_at_once_deliver_once(self):
        row_id = await self.hold_item()
        await self.free_name()
        outcomes = await asyncio.gather(
            pending_rewards.retry(self.bot),                 # scheduler sweep
            pending_rewards.retry(self.bot, GUILD, USER),    # member opens Inventory
            pending_rewards.retry(self.bot, GUILD),
            pending_rewards.retry(self.bot),
            pending_rewards.retry(self.bot, GUILD, USER))
        self.assertEqual(sum(o["delivered"] for o in outcomes), 1)
        self.assertEqual(stack("Tonic"), ("custom", 1))
        self.assertEqual(status_of(row_id), "delivered")
        self.assertEqual(rows("SELECT COUNT(*) FROM inventory_activity WHERE "
                              "item_name='Tonic' AND source='mission'"), [(1,)])


class NeedsReviewTests(HeldRewardCase):
    async def _uncertain_delivery(self, row_id):
        await self.free_name()
        uncertain = AsyncMock(return_value={
            "success": False, "uncertain": True,
            "error": "Discord did not confirm the role change"})
        with patch.object(reward_engine, "give_reward", uncertain):
            outcome = await pending_rewards.retry(self.bot)
        self.assertEqual(outcome, {"delivered": 0, "still_pending": 0, "uncertain": 1})
        self.assertEqual(uncertain.await_count, 1)
        self.assertEqual(status_of(row_id), "needs_review")

    async def assert_never_paid_automatically(self, row_id):
        attempts = held_rows()[0][3]
        for _ in range(3):
            self.assertEqual(await pending_rewards.retry(self.bot),
                             {"delivered": 0, "still_pending": 0, "uncertain": 0})
            await pending_rewards.retry(self.bot, GUILD, USER)
        await self.open_tab(wallet.TAB_ITEMS)        # the member's own retry
        self.assertEqual(status_of(row_id), "needs_review")
        self.assertEqual(held_rows()[0][3], attempts)        # not even tried
        self.assertTrue(not_granted(), stack("Tonic"))       # nothing granted

    async def test_uncertain_outcome_goes_to_review_and_is_never_auto_paid(self):
        row_id = await self.hold_item()
        await self._uncertain_delivery(row_id)
        await self.assert_never_paid_automatically(row_id)
        row = (await pending_rewards.list_pending(GUILD, USER))[0]
        self.assertTrue(row["needs_review"])
        self.assertIn("did not confirm", row["last_error"])
        # The member is told it is being looked at, not that it arrived.
        _, embed = await self.open_tab(wallet.TAB_ITEMS)
        self.assertIn("1× Tonic — being checked by staff", embed.description)

    async def test_unexpected_error_during_delivery_is_uncertain_too(self):
        row_id = await self.hold_item()
        await self.free_name()
        with patch.object(reward_engine, "give_reward",
                          AsyncMock(side_effect=RuntimeError("connection reset"))):
            outcome = await pending_rewards.retry(self.bot)
        self.assertEqual(outcome["uncertain"], 1)
        self.assertEqual(status_of(row_id), "needs_review")
        await self.assert_never_paid_automatically(row_id)

    async def test_restart_in_the_middle_of_a_delivery_goes_to_review(self):
        row_id = await self.hold_item()
        await self.free_name()
        age_claim(row_id, pending_rewards.STALE_CLAIM_MINUTES + 1)
        await init_db()                                   # the bot starts again
        outcome = await pending_rewards.retry(self.bot)   # first sweep after it
        self.assertEqual(outcome, {"delivered": 0, "still_pending": 0, "uncertain": 0})
        self.assertEqual(status_of(row_id), "needs_review")
        self.assertIn("interrupted", (await pending_rewards.list_pending(GUILD))[0]["last_error"])
        await self.assert_never_paid_automatically(row_id)

    async def test_a_delivery_in_progress_is_left_to_its_owner(self):
        row_id = await self.hold_item()
        await self.free_name()
        age_claim(row_id, 1)                              # claimed a minute ago
        await pending_rewards.retry(self.bot)
        self.assertEqual(status_of(row_id), "delivering")
        self.assertTrue(not_granted())
        # Not decidable by staff while a delivery may still be running.
        self.assertFalse(await pending_rewards.resolve_review(GUILD, row_id, "retry"))
        self.assertEqual(status_of(row_id), "delivering")

    async def test_restart_keeps_pending_rewards_and_delivers_them_later(self):
        row_id = await self.hold_item()
        before = rows("SELECT * FROM pending_rewards")
        await init_db()                                   # restart
        await init_db()
        self.assertEqual(rows("SELECT * FROM pending_rewards"), before)
        await self.free_name()
        self.assertEqual((await pending_rewards.retry(self.bot))["delivered"], 1)
        self.assertEqual(status_of(row_id), "delivered")
        self.assertEqual(stack("Tonic"), ("custom", 1))

    async def test_staff_retry_delivers_exactly_once(self):
        row_id = await self.hold_item()
        await self._uncertain_delivery(row_id)
        # Wrong guild, a second decision, an unknown action: all refused.
        self.assertFalse(await pending_rewards.resolve_review(OTHER_GUILD, row_id, "retry"))
        with self.assertRaises(ValueError):
            await pending_rewards.resolve_review(GUILD, row_id, "pay twice")
        decisions = await asyncio.gather(
            pending_rewards.resolve_review(GUILD, row_id, "retry", actor="staff 1"),
            pending_rewards.resolve_review(GUILD, row_id, "retry", actor="staff 2"),
            pending_rewards.resolve_review(GUILD, row_id, "delivered", actor="staff 3"))
        self.assertEqual(sorted(decisions), [False, False, True])
        if status_of(row_id) == "pending":
            outcomes = await asyncio.gather(pending_rewards.retry(self.bot),
                                            pending_rewards.retry(self.bot, GUILD, USER))
            self.assertEqual(sum(o["delivered"] for o in outcomes), 1)
            self.assertEqual(stack("Tonic"), ("custom", 1))
        else:
            self.assertTrue(not_granted())
        self.assertEqual(status_of(row_id), "delivered")
        before = stack("Tonic")
        await pending_rewards.retry(self.bot)
        self.assertEqual(stack("Tonic"), before)

    async def test_staff_retry_then_delivery(self):
        row_id = await self.hold_item()
        await self._uncertain_delivery(row_id)
        self.assertTrue(await pending_rewards.resolve_review(GUILD, row_id, "retry"))
        self.assertEqual(status_of(row_id), "pending")
        self.assertEqual((await pending_rewards.retry(self.bot))["delivered"], 1)
        self.assertEqual(stack("Tonic"), ("custom", 1))
        await pending_rewards.retry(self.bot)
        self.assertEqual(stack("Tonic"), ("custom", 1))

    async def test_staff_mark_delivered_grants_nothing(self):
        row_id = await self.hold_item()
        await self._uncertain_delivery(row_id)
        self.assertTrue(await pending_rewards.resolve_review(
            GUILD, row_id, "delivered", actor="staff 77"))
        closed = rows("SELECT status, last_error, delivered_at FROM pending_rewards")[0]
        self.assertEqual(closed[:2], ("delivered", "Marked as delivered by staff 77"))
        self.assertIsNotNone(closed[2])
        await pending_rewards.retry(self.bot)
        self.assertTrue(not_granted())
        self.assertEqual(await pending_rewards.list_pending(GUILD), [])

    async def test_a_waiting_reward_is_not_a_staff_decision(self):
        row_id = await self.hold_item()
        for action in ("retry", "delivered"):
            self.assertFalse(await pending_rewards.resolve_review(GUILD, row_id, action))
        self.assertEqual(status_of(row_id), "pending")


# ═══ 2. notices ═════════════════════════════════════════════════════════

class MissionNoticeTests(HeldRewardCase):
    CHANNEL = 8001

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.member.send = AsyncMock()
        self.channel = SimpleNamespace(id=self.CHANNEL, send=AsyncMock())
        self.guild.get_channel = (
            lambda cid: self.channel if int(cid) == self.CHANNEL else None)

    async def complete(self):
        return await mission_engine.record_activities(
            self.bot, GUILD, USER, {"messages": 1}, channel_id=self.CHANNEL)

    async def test_member_is_told_once_that_the_reward_is_held(self):
        legacy_mission("Tonic")
        await self.give_potion("Tonic", 1)
        completed = await self.complete()

        self.assertEqual([d["name"] for d in completed], ["Chatter"])   # result unchanged
        self.assertEqual(self.progress(), [(1, 1)])
        self.assertEqual(self.member.send.await_count, 1)
        text = self.member.send.await_args.args[0]
        self.assertIn(MISSION_NOTICE, text)
        self.assertIn("Chatter", text)
        self.assertIn("do not need to do the mission again", text)
        self.assertNotIn("has been delivered", text)
        self.assertNotIn("you received", text.lower())
        self.channel.send.assert_not_awaited()            # the DM was enough

        # Retries - refused, then successful - and more activity say nothing more.
        await pending_rewards.retry(self.bot)
        await self.open_tab(wallet.TAB_POTIONS)
        await self.complete()
        await self.free_name()
        self.assertEqual((await pending_rewards.retry(self.bot))["delivered"], 1)
        await self.complete()
        self.assertEqual(self.member.send.await_count, 1)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.progress(), [(1, 1)])

    async def test_closed_dms_fall_back_to_the_activity_channel(self):
        legacy_mission("Tonic")
        await self.give_potion("Tonic", 1)
        self.member.send = AsyncMock(side_effect=RuntimeError("DMs closed"))
        await self.complete()
        self.assertEqual(self.channel.send.await_count, 1)
        text = self.channel.send.await_args.args[0]
        self.assertTrue(text.startswith(f"<@{USER}> "))
        self.assertIn(MISSION_NOTICE, text)

    async def test_a_notice_that_cannot_be_sent_changes_nothing(self):
        legacy_mission("Tonic")
        await self.give_potion("Tonic", 1)
        self.member.send = AsyncMock(side_effect=RuntimeError("DMs closed"))
        self.channel.send = AsyncMock(side_effect=RuntimeError("no permission"))
        completed = await self.complete()
        self.assertEqual([d["name"] for d in completed], ["Chatter"])
        self.assertEqual(self.progress(), [(1, 1)])
        self.assertEqual([r[2] for r in held_rows()], ["pending"])

    async def test_a_delivered_reward_sends_no_notice(self):
        legacy_mission("Ticket")
        await self.complete()
        self.assertEqual(stack("Ticket"), ("custom", 1))
        self.member.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()


class MinigameNoticeTests(HeldRewardCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await minigame_store.ensure_tables()

    async def run_round(self, reward_name):
        run_id = await minigame_store.start_run(
            GUILD, None, "Quick", None, "Quick", "quick_click", "manual", None)
        engine = minigame_engine.MinigameEngine(
            {"guild_id": GUILD, "name": "Quick",
             "embed": {"description": "Hit the button"},
             "rewards": [{"reward_type": "item", "reward_value": reward_name,
                          "weight": 1}]},
            "manual", run_id, bot=self.bot)
        engine._message = SimpleNamespace(edit=AsyncMock())
        resolved = await engine.resolve(
            [{"id": USER, "name": "Member"}], result_text="🏆 **Member** won.")
        if engine._view is not None:
            engine._view.stop()
        run = rows("SELECT status, winner_id, winners_json FROM minigames_log "
                   "WHERE id=?", (run_id,))[0]
        return resolved, engine, run

    async def test_winner_is_told_once_that_the_reward_is_held(self):
        await self.give_potion("Tonic", 1)
        resolved, engine, run = await self.run_round("Tonic")

        # The round's result is unchanged: resolved, completed, same winner.
        self.assertTrue(resolved)
        self.assertEqual(run[:2], ("completed", USER))
        winner = json.loads(run[2])[0]
        self.assertEqual((winner["id"], winner["reward_value"], winner["status"]),
                         (USER, "Tonic", "won"))
        self.assertIn("held as pending reward", winner["error"])   # staff see why
        self.assertEqual([r[1:3] for r in held_rows()], [(USER, "pending")])
        self.assertEqual(stack("Tonic"), ("potion", 1))

        self.assertEqual(engine._message.edit.await_count, 1)
        text = engine._message.edit.await_args.kwargs["embed"].description
        self.assertIn(f"<@{USER}> {MINIGAME_NOTICE}", text)
        self.assertIn("🏆 **Member** won.", text)
        self.assertNotIn("has been delivered", text)

        # Retries deliver without announcing anything again.
        await pending_rewards.retry(self.bot)
        await self.free_name()
        self.assertEqual((await pending_rewards.retry(self.bot))["delivered"], 1)
        self.assertEqual(engine._message.edit.await_count, 1)
        self.assertEqual(stack("Tonic"), ("custom", 1))

    async def test_a_delivered_reward_shows_no_notice(self):
        resolved, engine, run = await self.run_round("Ticket")
        self.assertTrue(resolved)
        self.assertEqual(json.loads(run[2])[0]["status"], "won")
        self.assertEqual(stack("Ticket"), ("custom", 1))
        text = engine._message.edit.await_args.kwargs["embed"].description
        self.assertNotIn("held", text)
        self.assertEqual(held_rows(), [])


class TagRewardTests(HeldRewardCase):
    """Role rewards stored under the Discord role's name ("Flame")."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.member.send = AsyncMock()
        await self.give_potion("Flame", 1)               # the clashing item

    async def test_partner_reward_is_held_once_however_often_the_member_joins(self):
        partner = 99001
        execute("INSERT INTO tag_partner_rewards (guild_id, partner_guild_id, "
                "reward_type, reward_role_id, welcome_message, enabled) "
                "VALUES (?,?,'role',?, 'WELCOME - ROLE GRANTED', 1)",
                (GUILD, partner, FLAME))
        self.member.primary_guild = SimpleNamespace(
            identity_enabled=True, identity_guild_id=partner)
        cog = TagPartners.__new__(TagPartners)
        cog.bot = self.bot
        for _ in range(3):
            await TagPartners.on_member_join(cog, self.member)
        self.assertEqual([r[1:3] for r in held_rows()], [(USER, "pending")])
        self.member.send.assert_not_awaited()      # no "granted" welcome yet
        await inventory.remove_item(GUILD, USER, "Flame", 1)
        self.assertEqual((await pending_rewards.retry(self.bot))["delivered"], 1)
        self.assertEqual(stack("Flame"), ("role", 1))
        await TagPartners.on_member_join(cog, self.member)
        self.assertEqual(len(held_rows()), 1)
        self.assertEqual(stack("Flame"), ("role", 1))

    async def test_tag_mission_reports_the_held_reward_not_a_failure(self):
        self.member.primary_guild = SimpleNamespace(
            identity_enabled=True, identity_guild_id=GUILD)
        mission = execute(
            "INSERT INTO tag_missions (guild_id, title, reward_type, "
            "reward_role_id, starts_at, ends_at, status, success_message, "
            "failure_message) VALUES (?, 'Loyal', 'role', ?, '2020-01-01', "
            "'2020-01-02', 'active', 'ROLE DELIVERED', 'TAG REMOVED')",
            (GUILD, FLAME))
        execute("INSERT INTO tag_mission_participants (mission_id, guild_id, "
                "user_id) VALUES (?,?,?)", (mission, GUILD, USER))
        self.guild.fetch_member = AsyncMock(return_value=self.member)
        cog = TagMissions.__new__(TagMissions)
        cog.bot = self.bot
        await TagMissions._resolve_mission(
            cog, mission, GUILD, "role", None, FLAME, None,
            "ROLE DELIVERED", "TAG REMOVED")
        self.assertEqual(rows("SELECT outcome FROM tag_mission_participants"),
                         [("rewarded",)])
        self.assertEqual(self.member.send.await_count, 1)
        text = self.member.send.await_args.args[0]
        self.assertIn("waiting for delivery because of an item conflict", text)
        self.assertNotIn("ROLE DELIVERED", text)
        self.assertNotIn("TAG REMOVED", text)
        self.assertEqual([r[1:3] for r in held_rows()], [(USER, "pending")])


# ═══ 3. staff view ══════════════════════════════════════════════════════

class DashboardReviewTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Staff", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token=CSRF)

    def hold(self, guild=GUILD, status="needs_review"):
        async def seed():
            await inventory.give_item(guild, OTHER_USER, "Tonic", 1,
                                      item_type="potion", metadata=POTION_META)
            result = await reward_engine.give_reward(
                None, guild, OTHER_USER, "item", item_name="Tonic",
                reason="Mission complete: Chatter", source="mission")
            return result["pending_id"]
        row_id = asyncio.run(seed())
        if status != "pending":
            execute("UPDATE pending_rewards SET status=?, last_error="
                    "'Discord did not confirm the role change' WHERE id=?",
                    (status, row_id))
        return row_id

    def decide(self, row_id, action, csrf=CSRF):
        return self.client.post(
            f"/api/pending-rewards/{row_id}/{action}",
            headers={"X-CSRF-Token": csrf} if csrf else {})

    def test_page_shows_state_reason_and_the_two_decisions(self):
        waiting = self.hold(status="pending")
        html = self.client.get("/inventory").get_data(as_text=True)
        self.assertIn("<strong>Pending</strong>", html)
        self.assertIn("already uses this name", html)
        self.assertNotIn(f"resolvePendingReward({waiting},", html)   # nothing to decide

        execute("UPDATE pending_rewards SET status='needs_review', last_error="
                "'Discord did not confirm the role change' WHERE id=?", (waiting,))
        for path in ("/inventory", f"/inventory/{OTHER_USER}"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("<strong>Needs review</strong>", html, path)
            self.assertIn("Discord did not confirm the role change", html)
            self.assertIn("Mission complete: Chatter", html)
            self.assertIn(f"resolvePendingReward({waiting}, 'retry')", html)
            self.assertIn(f"resolvePendingReward({waiting}, 'delivered')", html)

    def test_retry_requeues_once_and_is_audited(self):
        row_id = self.hold()
        self.assertEqual(self.decide(row_id, "retry").status_code, 200)
        self.assertEqual(status_of(row_id), "pending")
        self.assertEqual(self.decide(row_id, "retry").status_code, 409)
        self.assertEqual(self.decide(row_id, "delivered").status_code, 409)
        self.assertEqual(status_of(row_id), "pending")
        note = rows("SELECT last_error FROM pending_rewards")[0][0]
        self.assertEqual(note, f"Retry requested by staff {USER}")
        audit = rows("SELECT * FROM audit_log")
        self.assertEqual(len(audit), 1)
        self.assertIn(f"Pending reward #{row_id}: retry requested", str(audit[0]))

    def test_mark_delivered_closes_without_granting(self):
        row_id = self.hold()
        self.assertEqual(self.decide(row_id, "delivered").status_code, 200)
        self.assertEqual(status_of(row_id), "delivered")
        self.assertEqual(stack("Tonic", OTHER_USER), ("potion", 1))
        self.assertNotIn("Rewards waiting for delivery",
                         self.client.get("/inventory").get_data(as_text=True))

    def test_only_this_guilds_reviewable_rewards_can_be_decided(self):
        foreign = self.hold(guild=OTHER_GUILD)
        waiting = self.hold(status="pending")
        delivering = self.hold(status="delivering")
        for row_id in (foreign, waiting, delivering, 999999):
            for action in ("retry", "delivered"):
                self.assertEqual(self.decide(row_id, action).status_code, 409,
                                 (row_id, action))
        self.assertEqual([status_of(i) for i in (foreign, waiting, delivering)],
                         ["needs_review", "pending", "delivering"])
        self.assertEqual(self.decide(foreign, "pay-again").status_code, 400)

    def test_decision_needs_the_csrf_token(self):
        row_id = self.hold()
        self.assertGreaterEqual(self.decide(row_id, "retry", csrf=None).status_code, 400)
        self.assertGreaterEqual(self.decide(row_id, "retry", csrf="wrong").status_code, 400)
        self.assertEqual(status_of(row_id), "needs_review")


# ═══ 4. the table ═══════════════════════════════════════════════════════

def schema_without_pending_rewards():
    return rows("SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE tbl_name != 'pending_rewards' ORDER BY type, name")


def contents(tables):
    return {t: rows(f'SELECT * FROM "{t}" ORDER BY rowid') for t in tables}


class TableSafetyTests(HeldRewardCase):
    async def test_upgrade_of_an_existing_database_only_adds_the_table(self):
        # A database from before this change, with member data in it.
        await self.give_potion("Tonic", 2)
        await inventory.give_item(GUILD, USER, "Ticket", 3, item_type="custom")
        legacy_mission("Ticket")
        execute("DROP TABLE pending_rewards")           # scratch DB: pre-upgrade
        self.assertEqual(rows("SELECT name FROM sqlite_master WHERE "
                              "tbl_name='pending_rewards'"), [])
        schema_before = schema_without_pending_rewards()
        tables = [r[1] for r in schema_before
                  if r[0] == "table" and not r[1].startswith("sqlite_")]
        data_before = contents(tables)
        self.assertGreater(len(data_before["inventory_items"]), 0)

        await init_db()                                  # the upgraded bot starts

        self.assertEqual(schema_without_pending_rewards(), schema_before)
        self.assertEqual(contents(tables), data_before)
        added = rows("SELECT type, name FROM sqlite_master WHERE "
                     "tbl_name='pending_rewards' AND name NOT LIKE 'sqlite_%' "
                     "ORDER BY name")
        self.assertEqual(added, [("index", "idx_pending_rewards_member"),
                                 ("index", "idx_pending_rewards_status"),
                                 ("table", "pending_rewards")])
        self.assertEqual(rows("SELECT COUNT(*) FROM pending_rewards"), [(0,)])
        self.assertEqual(rows("PRAGMA integrity_check"), [("ok",)])

    async def test_creating_it_again_keeps_every_row(self):
        await self.hold_item()
        before = rows("SELECT * FROM pending_rewards")
        schema = rows("SELECT sql FROM sqlite_master WHERE tbl_name='pending_rewards'")
        for _ in range(2):
            await init_db()
        self.assertEqual(rows("SELECT * FROM pending_rewards"), before)
        self.assertEqual(
            rows("SELECT sql FROM sqlite_master WHERE tbl_name='pending_rewards'"),
            schema)

    async def test_identity_and_required_columns(self):
        first = await self.hold_item()
        second = await self.hold_item(user=OTHER_USER)
        self.assertNotEqual(first, second)               # one row per held reward
        columns = {r[1]: (r[3], r[5]) for r in rows("PRAGMA table_info(pending_rewards)")}
        self.assertEqual(columns["id"], (0, 1))          # primary key
        for required in ("guild_id", "user_id", "reward_type", "payload",
                         "status", "attempts", "created_at", "updated_at"):
            self.assertEqual(columns[required][0], 1, required)   # NOT NULL
        with self.assertRaises(Exception):
            execute("INSERT INTO pending_rewards (user_id, reward_type, payload, "
                    "created_at, updated_at) VALUES (1,'item','{}','x','x')")
        # AUTOINCREMENT: an id is never reused after a row is gone, so a staff
        # link or log line can never point at a different reward.
        execute("DELETE FROM pending_rewards WHERE id=?", (second,))
        third = await self.hold_item(user=OTHER_USER, name="Elixir")
        self.assertGreater(third, second)

    async def test_both_readers_use_an_index(self):
        def plan(sql, args):
            return " ".join(str(r[-1]) for r in rows("EXPLAIN QUERY PLAN " + sql, args))
        sweep = plan("SELECT id FROM pending_rewards WHERE status=? "
                     "ORDER BY id LIMIT ?", ("pending", 50))
        self.assertIn("idx_pending_rewards_status", sweep)
        self.assertNotIn("TEMP B-TREE", sweep)           # no sort either
        member = plan("SELECT id FROM pending_rewards WHERE guild_id=? AND "
                      "status IN (?,?,?) AND user_id=?",
                      (GUILD, "pending", "delivering", "needs_review", USER))
        self.assertIn("idx_pending_rewards_member", member)


if __name__ == "__main__":
    unittest.main(verbosity=2)
