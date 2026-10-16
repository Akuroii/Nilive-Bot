"""Offline voice-loop startup and persistence regressions.

Run: python scripts/test_activity_voice_startup.py
Uses phase1_support's temporary database, real discord.py Tasks/Bot dispatch,
and real leveling/mission listeners. Never logs in or uses production data.
Only the test scheduler interval is shortened; production stays at 60 seconds.
"""
import asyncio
from types import SimpleNamespace
import unittest

# Must precede all database/cog imports: forces a fresh scratch DB path.
import phase1_support as S

import discord
from discord.ext import commands
from cogs.leveling import Leveling
from cogs.missions import Missions
from utils.mission_engine import create_definition, ensure_tables, get_period_key
from utils.timezone import get_cairo_daily_key


class VoiceStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await S.reset_database()
        await ensure_tables()
        S.execute(
            "INSERT INTO leveling_config (guild_id, voice_xp_enabled, "
            "voice_xp_per_minute, voice_require_unmuted, voice_farming_guard) "
            "VALUES (?,1,10,0,0)", (S.GUILD,))
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
        self.member = SimpleNamespace(
            id=S.USER, bot=False, roles=[], premium_since=None,
            voice=SimpleNamespace(self_mute=True, mute=True,
                                  self_deaf=True, deaf=True))
        self.channel = SimpleNamespace(id=7001, members=[self.member])
        # Deliberately alone, AFK, muted and deafened: guard OFF / unmuted OFF
        # must still permit the member under the reported guild settings.
        self.guild = SimpleNamespace(
            id=S.GUILD, afk_channel=self.channel, voice_channels=[self.channel],
            get_member=lambda uid: self.member if uid == S.USER else None)
        self.bot._connection._guilds = {S.GUILD: self.guild}
        self.leveling = Leveling.__new__(Leveling)
        self.leveling.bot = self.bot
        self.missions = Missions.__new__(Missions)
        self.missions.bot = self.bot
        self.events = []
        self.committed_minutes = []
        self.two_xp_events = asyncio.Event()
        self.stop_after_two = False
        self.completed = {"activity_voice_tick": 0, "activity_voice_xp_tick": 0}
        original_dispatch = self.bot.dispatch

        def dispatch(name, *args):
            if name in self.completed:
                self.events.append((name, args[1].id, args[2]))
                if name == "activity_voice_tick":
                    # Separate SQLite connection at the actual dispatch boundary.
                    self.committed_minutes.append(self.minutes())
                if (self.stop_after_two and name == "activity_voice_xp_tick"
                        and self.event_count(name) == 2):
                    # Stop at the synchronous dispatch boundary, not after an
                    # arbitrarily slow SQLite listener finishes its work.
                    self.cog.voice_tick_task.stop()
            original_dispatch(name, *args)

        self.bot.dispatch = dispatch
        self.mission_id = await create_definition(
            S.GUILD, name="Voice startup test", mtype="voice_minutes",
            target=100, reward_type="xp", reward_value="1",
            channel_id=self.channel.id)
        self.other_mission_id = await create_definition(
            S.GUILD, name="Other channel", mtype="voice_minutes",
            target=100, reward_type="xp", reward_value="1", channel_id=9999)

        def listener_finished(name):
            self.completed[name] += 1
            if all(count == 2 for count in self.completed.values()):
                self.two_xp_events.set()

        async def voice_listener(guild, member, flags):
            await self.missions.on_activity_voice_tick(guild, member, flags)
            listener_finished("activity_voice_tick")

        async def xp_listener(guild, member, flags):
            await self.leveling.on_activity_voice_xp_tick(guild, member, flags)
            listener_finished("activity_voice_xp_tick")

        self.bot.add_listener(voice_listener, "on_activity_voice_tick")
        self.bot.add_listener(xp_listener, "on_activity_voice_xp_tick")

    async def asyncTearDown(self):
        cog = self.bot.get_cog("ActivityEngine")
        if cog is not None:
            task = cog.voice_tick_task.get_task()
            await self.bot.unload_extension("cogs.activity_engine")
            if task is not None:
                # Retrieve even a before_loop exception so failures are explicit,
                # rather than an unhandled-task warning during loop shutdown.
                await asyncio.gather(task, return_exceptions=True)
        await self.bot.close()

    async def load_engine(self):
        await self.bot.load_extension("cogs.activity_engine")
        self.cog = self.bot.get_cog("ActivityEngine")
        self.assertEqual(self.cog.voice_tick_task.seconds, 60)

    def minutes(self):
        return S.rows(
            "SELECT voice_minutes FROM activity_stats "
            "WHERE guild_id=? AND user_id=? AND date=?",
            (S.GUILD, S.USER, get_cairo_daily_key()))

    def xp(self):
        return S.rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                      (S.GUILD, S.USER))

    def progress(self, mission_id):
        return S.rows(
            "SELECT progress FROM mission_progress WHERE guild_id=? "
            "AND user_id=? AND mission_id=? AND period_key=?",
            (S.GUILD, S.USER, mission_id, get_period_key("daily")))

    def event_count(self, name):
        return sum(event[0] == name for event in self.events)

    async def test_prelogin_survives_then_ready_runs_and_persists(self):
        self.assertFalse(self.bot.is_ready())
        # Public API distinguishes an uninitialised client from an unset READY event.
        with self.assertRaisesRegex(RuntimeError, "not been properly initialised"):
            await asyncio.wait_for(self.bot.wait_until_ready(), timeout=1)
        await self.load_engine()  # Real main.py order: extension BEFORE login.
        task = self.cog.voice_tick_task.get_task()
        await asyncio.sleep(0.6)  # Exercise more than one readiness poll.
        self.assertFalse(task.done(), "voice loop died before client initialization")
        self.assertTrue(self.cog.voice_tick_task.is_running())
        self.assertEqual(self.events, [])
        self.assertEqual(self.minutes(), [])
        self.assertEqual(self.cog.voice_tick_task.current_loop, 0)

        # Same initialization login performs, without HTTP, credentials or READY.
        await self.bot._async_setup_hook()
        await asyncio.sleep(0.6)
        self.assertFalse(task.done())
        self.assertEqual(self.minutes(), [])
        self.assertEqual(self.events, [])

        self.cog.voice_tick_task.change_interval(seconds=0.05)
        self.stop_after_two = True
        self.bot._ready.set()  # Simulate the Discord READY event.
        await asyncio.wait_for(self.two_xp_events.wait(), timeout=3)
        await asyncio.wait_for(task, timeout=1)
        self.assertIsNone(task.exception())
        self.assertEqual(self.event_count("activity_voice_tick"), 2)
        self.assertEqual(self.event_count("activity_voice_xp_tick"), 2)
        self.assertEqual(self.minutes(), [(2.0,)])
        self.assertEqual(self.committed_minutes, [[(1.0,)], [(2.0,)]])
        self.assertEqual(self.progress(self.mission_id), [(2,)])
        self.assertEqual(self.progress(self.other_mission_id), [])
        self.assertEqual(self.xp(), [(20,)])
        self.assertEqual({event[1] for event in self.events}, {S.USER})
        for name, _, flags in self.events:
            self.assertEqual(flags["channel_id"], self.channel.id)
            self.assertTrue(flags["self_mute"] and flags["deaf"])
            if name == "activity_voice_xp_tick":
                self.assertTrue(flags["alone"] and flags["afk"])

    async def test_unload_before_ready_cancels_waiter(self):
        await self.load_engine()
        task = self.cog.voice_tick_task.get_task()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        await self.bot.unload_extension("cogs.activity_engine")
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events, [])
        self.assertEqual(self.minutes(), [])

    async def test_reload_after_ready_has_only_one_live_loop(self):
        await self.load_engine()
        old_task = self.cog.voice_tick_task.get_task()
        await asyncio.sleep(0.05)
        self.assertFalse(old_task.done())
        await self.bot._async_setup_hook()
        self.bot._ready.set()
        await self.bot.reload_extension("cogs.activity_engine")
        await asyncio.gather(old_task, return_exceptions=True)
        self.assertTrue(old_task.cancelled())
        self.cog = self.bot.get_cog("ActivityEngine")
        self.cog.voice_tick_task.change_interval(seconds=0.05)
        self.stop_after_two = True
        task = self.cog.voice_tick_task.get_task()
        self.assertIsNot(task, old_task)
        await asyncio.wait_for(self.two_xp_events.wait(), timeout=3)
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(self.event_count("activity_voice_tick"), 2)
        self.assertEqual(self.event_count("activity_voice_xp_tick"), 2)
        self.assertEqual(self.minutes(), [(2.0,)])
        self.assertEqual(self.xp(), [(20,)])

    async def test_policy_switches_remain_independent(self):
        # These exercise the real tick body and real listeners; lifecycle is
        # covered above. Fresh scratch rows per subcase, never production IDs.
        await self.bot._async_setup_hook()
        from cogs.activity_engine import ActivityEngine
        engine = ActivityEngine.__new__(ActivityEngine)

        for guard, unmuted, enabled, minutes, mission, xp in (
                (0, 0, 1, 1, 1, 10),
                (1, 0, 1, 0, 0, 0),
                (0, 1, 1, 1, 1, 0),
                (0, 0, 0, 1, 1, 0)):
            with self.subTest(guard=guard, unmuted=unmuted, enabled=enabled):
                for table in ("activity_stats", "levels", "mission_progress"):
                    S.execute(f"DELETE FROM {table}")
                S.execute(
                    "UPDATE leveling_config SET voice_farming_guard=?, "
                    "voice_require_unmuted=?, voice_xp_enabled=? WHERE guild_id=?",
                    (guard, unmuted, enabled, S.GUILD))
                events = []
                engine.bot = SimpleNamespace(
                    guilds=[self.guild], dispatch=lambda *args: events.append(args))
                await ActivityEngine.voice_tick_task.coro(engine)
                for name, guild, member, flags in events:
                    if name == "activity_voice_tick":
                        await self.missions.on_activity_voice_tick(guild, member, flags)
                    else:
                        await self.leveling.on_activity_voice_xp_tick(guild, member, flags)
                self.assertEqual([e[0] for e in events],
                                 (["activity_voice_tick"] if minutes else [])
                                 + ["activity_voice_xp_tick"])
                self.assertEqual(self.minutes(), [(float(minutes),)] if minutes else [])
                self.assertEqual(self.progress(self.mission_id), [(mission,)] if mission else [])
                self.assertEqual(self.xp(), [(xp,)] if xp else [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
