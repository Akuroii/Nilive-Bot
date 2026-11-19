"""MVP acceptance tests: isolated SQLite, real settle/payout/reward paths.

Discord objects are small fakes; every payout, ledger, history and cycle
assertion is read back from the database. Live role assignment and the real
announcement are NOT proven here.
Run: PYTHONPATH=$PWD python3 -m pytest -q tests/test_mvp_phase3.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["OWNER_ID"] = "1"
os.environ.setdefault("SECRET_KEY", "isolated-test-only")
os.environ["DATABASE_PATH"] = f"/tmp/nilive-mvp-import-{os.getpid()}.sqlite"

import aiosqlite
import database
import cogs.mvp  # noqa: F401
import utils.currency, utils.economy_safe, utils.equip_engine, utils.ledger  # noqa: E401,F401
import utils.mvp_engine, utils.prestige, utils.reward_engine, utils.timed_ownership  # noqa: E401,F401
import utils.daily_engine, utils.inventory, utils.level_claims, utils.level_ops  # noqa: E401,F401
import utils.pending_rewards, utils.permissions, utils.streak_notifications  # noqa: E401,F401
import utils.streak_settings, utils.xp_calculator, utils.shop_validation  # noqa: E401,F401
import dashboard.api.mvp  # noqa: F401
from utils import mvp_engine, prestige

G1, G2 = 1280983591148584971, 1405993421167919336
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, guild, user_id, events, boosting=False):
        self.id, self.guild, self.roles = user_id, guild, []
        self.display_name, self.display_avatar = f"user{user_id}", None
        self.premium_since = None
        self._events = events

    async def add_roles(self, role, reason=None):
        self._events.append(("role", self.id))
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


class FakeChannel:
    def __init__(self, events):
        self.sent, self._events = [], events

    async def send(self, **kw):
        self._events.append(("announce",))
        self.sent.append(kw)


class FakeGuild:
    def __init__(self, guild_id, events, role_id=None, channel=True):
        self.id, self.members_by_id = guild_id, {}
        self.events = events
        self.role = FakeRole(role_id) if role_id else None
        self.channel = FakeChannel(events) if channel else None
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(
            administrator=False, manage_roles=True))

    @property
    def members(self):
        return list(self.members_by_id.values())

    def add(self, user_id):
        self.members_by_id[user_id] = FakeMember(self, user_id, self.events)
        return self.members_by_id[user_id]

    def get_member(self, user_id):
        return self.members_by_id.get(user_id)

    def get_role(self, role_id):
        return self.role if self.role and self.role.id == role_id else None

    def get_channel(self, channel_id):
        return self.channel


class FakeBot:
    def __init__(self, *guilds):
        self._g = {g.id: g for g in guilds}
        self.guilds = list(guilds)

    def get_guild(self, guild_id):
        return self._g.get(guild_id)


class MvpPhase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory(prefix="nilive-mvp-")
        self.db_path = str(Path(self.tmp.name) / "iso.sqlite")
        original = database.DB_PATH
        self._patches = []
        for mod in list(sys.modules.values()):
            if mod is not None and getattr(mod, "DB_PATH", None) == original:
                p = patch.object(mod, "DB_PATH", self.db_path)
                p.start()
                self._patches.append(p)
        await database.init_db()
        self.events = []

    async def asyncTearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    async def q(self, sql, args=()):
        async with aiosqlite.connect(self.db_path) as db:
            return await (await db.execute(sql, args)).fetchall()

    async def x(self, sql, args=()):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, args)
            await db.commit()

    async def configure(self, guild, *, ranks=3, rewards=(300, 200, 100), hours=6,
                        min_words=0, chat_w=1.0, voice_w=2.0, role=777, channel=888):
        await self.x(
            "INSERT OR REPLACE INTO mvp_config (guild_id, mvp_role_id, announce_channel_id, "
            "cycle_hours, chat_word_weight, voice_minute_weight, enabled, winner_ranks, "
            "rank_rewards, min_activity_words) VALUES (?,?,?,?,?,?,1,?,?,?)",
            (guild, role, channel, hours, chat_w, voice_w, ranks, json.dumps(list(rewards)), min_words))

    async def score(self, guild, user, points, *, at=T0, hours=6):
        await mvp_engine.record_activity(guild, user, message_score=points, now=at, cycle_hours=hours)

    async def balance(self, guild, user):
        r = await self.q("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (guild, user))
        return r[0][0] if r else 0

    def make_guild(self, gid, users, **kw):
        g = FakeGuild(gid, self.events, **kw)
        for u in users:
            g.add(u)
        return g

    async def settle(self, guild, when, **kw):
        bot = FakeBot(guild)
        with contextlib.redirect_stdout(io.StringIO()):
            return await mvp_engine.settle_cycle(bot, guild, now=when, **kw)

    # ── ranking ──
    def test_rank_winners_ties_share_rank_and_n_counts_ranks(self):
        rows = [(1, 200, "a"), (2, 150, "b"), (3, 150, "c"), (4, 120, "d"), (5, 90, "e")]
        w = mvp_engine.rank_winners(rows, 3, 0, lambda u: True)
        self.assertEqual([(x["rank"], x["user_id"]) for x in w],
                         [(1, 1), (2, 2), (2, 3), (3, 4)])       # 4 winners for N=3

    def test_left_member_skipped_and_threshold_applied_before_ranking(self):
        rows = [(1, 500, ""), (2, 400, ""), (3, 300, ""), (4, 20, "")]
        w = mvp_engine.rank_winners(rows, 2, 0, lambda u: u != 1)   # top scorer left
        self.assertEqual([(x["rank"], x["user_id"]) for x in w], [(1, 2), (2, 3)])
        w = mvp_engine.rank_winners(rows, 3, 100, lambda u: True)   # 20 < 100 filtered
        self.assertEqual([x["user_id"] for x in w], [1, 2, 3])
        self.assertEqual(mvp_engine.rank_winners([(9, 2, "")], 3, 50, lambda u: True), [])

    async def test_threshold_uses_each_guilds_chat_weight(self):
        await self.configure(G1, min_words=100, chat_w=1.0, voice_w=2.0)
        await self.configure(G2, min_words=100, chat_w=3.0, voice_w=1.5)
        c1, c2 = await mvp_engine.get_config(G1), await mvp_engine.get_config(G2)
        self.assertEqual((mvp_engine.threshold_points(c1), mvp_engine.threshold_points(c2)), (100, 300))
        self.assertEqual(mvp_engine.threshold_voice_minutes(c1), 50)      # 100 / 2.0
        self.assertEqual(mvp_engine.threshold_voice_minutes(c2), 200)     # 300 / 1.5

    # ── cycle boundaries ──
    async def test_cycle_uses_configured_hours_not_cairo_midnight(self):
        await self.configure(G1, hours=3)
        g = self.make_guild(G1, [1, 2])
        await self.score(G1, 1, 10, hours=3)
        self.assertIsNone(await self.settle(g, T0 + timedelta(hours=2, minutes=59)))
        done = await self.settle(g, T0 + timedelta(hours=3))
        self.assertEqual(done["cycle_end"], (T0 + timedelta(hours=3)).isoformat())
        self.assertEqual([w["user_id"] for w in done["winners"]], [1])
        # the next cycle began at the old end and is 3h long again
        start = (await self.q("SELECT cycle_start FROM mvp_cycle_state WHERE guild_id=?", (G1,)))[0][0]
        self.assertEqual(start, (T0 + timedelta(hours=3)).isoformat())

    # ── payout: amounts follow config, ties, roles, announcement ──
    async def test_ranked_payout_ties_roles_and_announcement(self):
        await self.configure(G1, ranks=3, rewards=(300, 200, 100))
        g = self.make_guild(G1, [1, 2, 3, 4, 5], role_id=777)
        for u, pts in ((1, 200), (2, 150), (3, 150), (4, 120), (5, 90)):
            await self.score(G1, u, pts)
        done = await self.settle(g, T0 + timedelta(hours=6))
        self.assertEqual([(w["rank"], w["user_id"]) for w in done["winners"]],
                         [(1, 1), (2, 2), (2, 3), (3, 4)])
        self.assertEqual([await self.balance(G1, u) for u in (1, 2, 3, 4, 5)], [300, 200, 200, 100, 0])
        pay = await self.q("SELECT rank,user_id,coins_base,coins_credited FROM mvp_payouts "
                           "WHERE guild_id=? ORDER BY rank,user_id", (G1,))
        self.assertEqual(pay, [(1, 1, 300, 300), (2, 2, 200, 200), (2, 3, 200, 200), (3, 4, 100, 100)])
        hist = await self.q("SELECT user_id,rank,score FROM mvp_history WHERE guild_id=? "
                            "ORDER BY rank,user_id", (G1,))
        self.assertEqual(hist, [(1, 1, 200), (2, 2, 150), (3, 2, 150), (4, 3, 120)])
        self.assertEqual(len(await self.q("SELECT 1 FROM transaction_ledger WHERE guild_id=? "
                                          "AND source='mvp'", (G1,))), 4)
        # roles for every winner, none for the rest, all BEFORE the announcement
        for u in (1, 2, 3, 4):
            self.assertIn(g.role, g.members_by_id[u].roles)
        self.assertEqual(g.members_by_id[5].roles, [])
        kinds = [e[0] for e in self.events]
        self.assertEqual(kinds, ["role"] * 4 + ["announce"])
        # exactly one announcement: role mention above the embed, role-only mentions
        self.assertEqual(len(g.channel.sent), 1)
        msg = g.channel.sent[0]
        self.assertEqual(msg["content"], "<@&777>")
        self.assertEqual(msg["embed"].title, "🏆 MVP Winners")
        self.assertEqual(msg["embed"].description.split("\n"),
                         ["1- <@1>", "2- <@2>", "2- <@3>", "3- <@4>"])
        self.assertEqual(msg["embed"].footer.text, "Next cycle in 6 hours")
        am = msg["allowed_mentions"]
        self.assertFalse(am.everyone)
        self.assertFalse(am.users)
        self.assertEqual([r.id for r in am.roles], [777])

    async def test_payout_follows_dashboard_config_rank_count_and_amounts(self):
        await self.configure(G1, ranks=1, rewards=(50,))
        g = self.make_guild(G1, [1, 2])
        await self.score(G1, 1, 10)
        await self.score(G1, 2, 9)
        await self.settle(g, T0 + timedelta(hours=6))
        self.assertEqual((await self.balance(G1, 1), await self.balance(G1, 2)), (50, 0))

    async def test_left_member_is_skipped_for_next_eligible(self):
        await self.configure(G1, ranks=1, rewards=(40,))
        g = self.make_guild(G1, [2, 3])                    # user 1 left the guild
        for u, pts in ((1, 900), (2, 80), (3, 70)):
            await self.score(G1, u, pts)
        done = await self.settle(g, T0 + timedelta(hours=6))
        self.assertEqual([w["user_id"] for w in done["winners"]], [2])
        self.assertEqual(await self.balance(G1, 1), 0)
        self.assertEqual(await self.balance(G1, 2), 40)

    async def test_minimum_activity_can_leave_cycle_without_winner(self):
        await self.configure(G1, min_words=100, chat_w=2.0)         # 200 points needed
        g = self.make_guild(G1, [1])
        await self.score(G1, 1, 199)
        done = await self.settle(g, T0 + timedelta(hours=6))
        self.assertEqual(done["winners"], [])
        self.assertEqual(await self.q("SELECT 1 FROM mvp_payouts"), [])
        self.assertEqual(g.channel.sent, [])                         # nothing announced
        self.assertEqual(await self.balance(G1, 1), 0)

    # ── guild isolation ──
    async def test_two_guilds_have_independent_config_and_cycles(self):
        await self.configure(G1, ranks=1, rewards=(475,), hours=6, role=777)
        await self.configure(G2, ranks=2, rewards=(1000, 500), hours=12, role=778)
        g1 = self.make_guild(G1, [1, 2], role_id=777)
        g2 = self.make_guild(G2, [1, 2], role_id=778)
        for g in (G1, G2):
            await self.score(g, 1, 50, hours=6 if g == G1 else 12)
            await self.score(g, 2, 40, hours=6 if g == G1 else 12)
        d1 = await self.settle(g1, T0 + timedelta(hours=6))
        self.assertIsNone(await self.settle(g2, T0 + timedelta(hours=6)))   # G2 not due (12h)
        self.assertEqual((await self.balance(G1, 1), await self.balance(G1, 2)), (475, 0))
        self.assertEqual((await self.balance(G2, 1), await self.balance(G2, 2)), (0, 0))
        d2 = await self.settle(g2, T0 + timedelta(hours=12))
        self.assertEqual((await self.balance(G2, 1), await self.balance(G2, 2)), (1000, 500))
        self.assertEqual(d1["cycle_hours"], 6)
        self.assertEqual(d2["cycle_hours"], 12)

    # ── idempotency, atomicity, no catch-up ──
    async def test_repeated_scheduler_execution_never_pays_twice(self):
        await self.configure(G1, ranks=1, rewards=(100,))
        g = self.make_guild(G1, [1])
        await self.score(G1, 1, 10)
        when = T0 + timedelta(hours=6)
        first = await self.settle(g, when)
        second = await self.settle(g, when)                   # same tick again
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(await self.balance(G1, 1), 100)
        self.assertEqual(len(await self.q("SELECT 1 FROM mvp_payouts")), 1)
        self.assertEqual(len(await self.q("SELECT 1 FROM mvp_history")), 1)

    async def test_payout_key_blocks_a_second_payment_for_the_same_rank_and_cycle(self):
        await self.configure(G1, ranks=1, rewards=(100,))
        await self.x("INSERT INTO mvp_payouts (guild_id,cycle_end,rank,user_id,coins_base) VALUES (?,?,?,?,?)",
                     (G1, "e", 1, 1, 100))
        with self.assertRaises(aiosqlite.IntegrityError):
            await self.x("INSERT INTO mvp_payouts (guild_id,cycle_end,rank,user_id,coins_base) VALUES (?,?,?,?,?)",
                         (G1, "e", 1, 1, 100))

    async def test_failure_mid_payout_rolls_everything_back_then_retry_pays_once(self):
        await self.configure(G1, ranks=2, rewards=(100, 50))
        g = self.make_guild(G1, [1, 2])
        await self.score(G1, 1, 10)
        await self.score(G1, 2, 9)
        import utils.reward_engine as re_mod
        real = re_mod.give_reward
        calls = {"n": 0}

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return await real(*a, **kw)
        when = T0 + timedelta(hours=6)
        with patch("utils.reward_engine.give_reward", flaky):
            with self.assertRaises(RuntimeError):
                await self.settle(g, when)
        self.assertEqual((await self.balance(G1, 1), await self.balance(G1, 2)), (0, 0))
        self.assertEqual(await self.q("SELECT 1 FROM mvp_payouts"), [])
        self.assertEqual(await self.q("SELECT 1 FROM mvp_history"), [])
        self.assertEqual(await self.q("SELECT 1 FROM transaction_ledger WHERE source='mvp'"), [])
        self.assertEqual(g.channel.sent, [])
        done = await self.settle(g, when)                      # retry succeeds, once
        self.assertEqual((await self.balance(G1, 1), await self.balance(G1, 2)), (100, 50))
        self.assertEqual(len(done["winners"]), 2)

    async def test_missed_cycles_are_not_paid_retroactively(self):
        await self.configure(G1, ranks=1, rewards=(100,), hours=6)
        g = self.make_guild(G1, [1])
        await self.score(G1, 1, 10)
        late = T0 + timedelta(hours=40)                       # bot was down ~6 cycles
        done = await self.settle(g, late)
        self.assertEqual(await self.balance(G1, 1), 100)      # ONE payout, for the open cycle
        start = (await self.q("SELECT cycle_start FROM mvp_cycle_state WHERE guild_id=?", (G1,)))[0][0]
        self.assertEqual(start, late.isoformat())             # next cycle starts now, no replay
        self.assertIsNone(await self.settle(g, late + timedelta(hours=1)))
        self.assertEqual(len(await self.q("SELECT 1 FROM mvp_payouts")), 1)

    # ── Prestige exactly once via reward_engine ──
    async def test_prestige_multiplier_applies_once_through_reward_engine(self):
        await self.configure(G1, ranks=1, rewards=(100,))
        await prestige.set_prestige_multipliers(G1, tiers={5: {"coins": 1.2, "diamonds": 1.1}})
        await self.x("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,?)",
                     (G1, 1, 0, 0, 5))
        g = self.make_guild(G1, [1])
        await self.score(G1, 1, 10)
        done = await self.settle(g, T0 + timedelta(hours=6))
        self.assertEqual(done["winners"][0]["coins_credited"], 120)
        self.assertEqual(await self.balance(G1, 1), 120)
        self.assertEqual(await self.q("SELECT coins_base,coins_credited FROM mvp_payouts"), [(100, 120)])
        self.assertEqual(await self.q("SELECT amount FROM transaction_ledger WHERE source='mvp'"), [(120,)])
        # a member in a guild without Prestige gets the raw amount
        await self.configure(G2, ranks=1, rewards=(100,))
        g2 = self.make_guild(G2, [1])
        await self.score(G2, 1, 10)
        await self.settle(g2, T0 + timedelta(hours=6))
        self.assertEqual(await self.balance(G2, 1), 100)

    # ── existing data untouched / role entitlement ──
    async def test_mvp_scores_table_is_not_modified_and_entitlement_covers_all_winners(self):
        from utils.timed_ownership import mvp_role_entitled
        await self.x("INSERT INTO mvp_scores (guild_id,user_id,date,message_score,total_score) "
                     "VALUES (?,?,?,?,?)", (G1, 1, "2026-11-18", 5, 5))
        before = await self.q("SELECT * FROM mvp_scores")
        await self.configure(G1, ranks=1, rewards=(0,))       # tie for rank 1; 3rd is outside
        g = self.make_guild(G1, [1, 2, 3])
        for u, pts in ((1, 9), (2, 9), (3, 1)):
            await self.score(G1, u, pts)
        await self.settle(g, T0 + timedelta(hours=6))
        self.assertEqual(await self.q("SELECT * FROM mvp_scores"), before)
        self.assertTrue(await mvp_role_entitled(G1, 1, 777))
        self.assertTrue(await mvp_role_entitled(G1, 2, 777))     # tied winner also entitled
        self.assertFalse(await mvp_role_entitled(G1, 3, 777))

    # ── dashboard API validation ──
    def test_dashboard_validation_caps_ranks_and_rewards(self):
        from dashboard.api.mvp import _validated_mvp_numbers as v
        ok, err = v({"cycle_hours": 6, "winner_ranks": 10, "rank_rewards": [5] * 10,
                     "min_activity_words": 0})
        self.assertIsNone(err)
        self.assertEqual(len(ok["rewards"]), 10)
        for bad in ({"winner_ranks": 11}, {"winner_ranks": 0}, {"rank_rewards": [-1]},
                    {"min_activity_words": -3}, {"cycle_hours": 0}, {"chat_word_weight": -1}):
            payload = {"cycle_hours": 6, "winner_ranks": 3, "rank_rewards": [1, 2, 3]}
            payload.update(bad)
            self.assertIsNotNone(v(payload)[1], bad)

    async def test_default_config_preserves_single_winner_no_coin_behaviour(self):
        cfg = await mvp_engine.get_config(G1)                  # no row at all
        self.assertEqual((cfg["winner_ranks"], cfg["rank_rewards"], cfg["min_activity_words"]),
                         (1, [0], 0.0))


if __name__ == "__main__":
    unittest.main()
