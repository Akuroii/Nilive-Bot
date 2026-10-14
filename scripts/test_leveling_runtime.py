"""Active Leveling runtime, Dashboard, voice, cooldown and UI regressions.

Runs the actual cogs.leveling message/voice handlers, Dashboard config API and
ActivityEngine voice tick against a scratch database with fake Discord objects.
Run with:
    python scripts/test_leveling_runtime.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from flask import Flask

from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def person(user=USER, *, bot=False, self_mute=False, mute=False,
           deaf=False, self_deaf=False):
    return SimpleNamespace(
        id=user, bot=bot, roles=[], mention=f"<@{user}>",
        display_name=f"person-{user}",
        voice=SimpleNamespace(self_mute=self_mute, mute=mute,
                              deaf=deaf, self_deaf=self_deaf),
    )


def make_message(user=USER, content="hello world", channel=None):
    return SimpleNamespace(
        author=person(user),
        guild=SimpleNamespace(id=GUILD, get_channel=lambda _cid: None),
        channel=channel or SimpleNamespace(send=AsyncMock()),
        reply=AsyncMock(),
        content=content,
    )


def make_leveling(bot=None):
    from cogs.leveling import Leveling
    obj = Leveling.__new__(Leveling)
    obj.bot = bot or SimpleNamespace(get_guild=lambda _gid: None)
    obj._xp_cooldowns = {}
    obj._spam_tracker = {}
    obj._spam_incidents = {}
    obj._spam_warn_times = {}
    return obj


def set_config(**values):
    defaults = {
        "message_xp_enabled": 1,
        "xp_cooldown_seconds": 10,
        "voice_xp_enabled": 1,
        "voice_xp_per_minute": 3,
        "voice_require_unmuted": 1,
        "spam_detection_enabled": 0,
        "spam_threshold": 10,
        "spam_window_seconds": 20,
        "spam_xp_penalty_divisor": 1000,
        "levelup_announce": 1,
    }
    defaults.update(values)
    execute("""
        INSERT INTO leveling_config
            (guild_id,message_xp_enabled,xp_cooldown_seconds,voice_xp_enabled,
             voice_xp_per_minute,voice_require_unmuted,spam_detection_enabled,
             spam_threshold,spam_window_seconds,spam_xp_penalty_divisor,
             levelup_announce)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(guild_id) DO UPDATE SET
            message_xp_enabled=excluded.message_xp_enabled,
            xp_cooldown_seconds=excluded.xp_cooldown_seconds,
            voice_xp_enabled=excluded.voice_xp_enabled,
            voice_xp_per_minute=excluded.voice_xp_per_minute,
            voice_require_unmuted=excluded.voice_require_unmuted,
            spam_detection_enabled=excluded.spam_detection_enabled,
            spam_threshold=excluded.spam_threshold,
            spam_window_seconds=excluded.spam_window_seconds,
            spam_xp_penalty_divisor=excluded.spam_xp_penalty_divisor,
            levelup_announce=excluded.levelup_announce
    """, (GUILD, defaults["message_xp_enabled"],
          defaults["xp_cooldown_seconds"], defaults["voice_xp_enabled"],
          defaults["voice_xp_per_minute"], defaults["voice_require_unmuted"],
          defaults["spam_detection_enabled"], defaults["spam_threshold"],
          defaults["spam_window_seconds"], defaults["spam_xp_penalty_divisor"],
          defaults["levelup_announce"]))


def xp(user=USER):
    found = rows("SELECT xp,level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else (0, 0)


async def test_dashboard_config_api():
    from dashboard.api import api_bp
    import dashboard.api.leveling as leveling_api
    import dashboard.auth as auth
    import dashboard.permissions as permissions
    from utils.xp_calculator import get_leveling_config

    app = Flask("leveling-runtime-test")
    app.secret_key = "test-only-leveling-secret"
    app.register_blueprint(api_bp, url_prefix="/api")
    client = app.test_client()

    async def admin_permission(_guild, _user):
        return "admin"

    with patch.object(auth, "is_session_valid", return_value=True), \
         patch.object(auth, "refresh_session_if_needed", return_value=None), \
         patch.object(permissions, "_get_permission_level", new=admin_permission), \
         patch.object(leveling_api, "log_action", return_value=None):
        csrf = "leveling-runtime-csrf-test"
        with client.session_transaction() as sess:
            sess["user"] = {"id": USER, "username": "runtime-test"}
            sess["guild_id"] = GUILD
            sess["csrf_token"] = csrf
        headers = {"X-CSRF-Token": csrf}

        initial = client.get("/api/leveling/config")
        initial_config = initial.get_json()["config"]
        check("Dashboard GET renders effective 10s cooldown / 20s spam defaults",
              initial.status_code == 200
              and initial_config["xp_cooldown_seconds"] == 10
              and initial_config["spam_window_seconds"] == 20
              and initial_config["spam_threshold"] == 10)

        payload = {
            "message_xp_enabled": 0,
            "xp_cooldown_seconds": 10,
            "voice_xp_enabled": 1,
            "voice_xp_per_minute": 3,
            "voice_require_unmuted": 1,
            "spam_detection_enabled": 0,
            "spam_threshold": 3,
            "spam_window_seconds": 20,
            "spam_xp_penalty_divisor": 1000,
            "levelup_announce": 0,
            "remove_old_reward_role": 0,
        }
        saved = client.post("/api/leveling/config", json=payload, headers=headers)
        stored = await get_leveling_config(GUILD)
        check("Dashboard save persists the independent Message/Voice controls",
              saved.status_code == 200 and saved.get_json().get("success")
              and stored["message_xp_enabled"] == 0
              and stored["voice_xp_enabled"] == 1
              and stored["voice_require_unmuted"] == 1,
              str(saved.get_json()))
        check("Dashboard saves the incident-based spam fields, not fixed XP",
              stored["spam_window_seconds"] == 20
              and stored["spam_threshold"] == 3
              and stored["spam_xp_penalty_divisor"] == 1000
              and "spam_xp_penalty" not in stored)
        persisted = rows(
            "SELECT message_xp_enabled,xp_cooldown_seconds,spam_window_seconds "
            "FROM leveling_config WHERE guild_id=?", (GUILD,))
        check("the Dashboard API values are present in the persisted guild row",
              persisted == [(0, 10, 20)], str(persisted))

        # Message toggle + cooldown: save OFF through the API, prove the real
        # handler reads it; then save ON and exercise 9.999s versus 10.000s.
        message_user = USER + 60
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
                (GUILD, message_user))
        message_cog = make_leveling()
        await type(message_cog).on_activity_message(
            message_cog, make_message(message_user, "message xp is off"), 5)
        check("Dashboard Message XP OFF blocks only the real message handler",
              xp(message_user) == (0, 0))
        payload["message_xp_enabled"] = 1
        saved_message_on = client.post(
            "/api/leveling/config", json=payload, headers=headers)
        message_clock = [1000.0]
        import cogs.leveling as leveling_module
        with patch.object(leveling_module, "time",
                          SimpleNamespace(time=lambda: message_clock[0])):
            await type(message_cog).on_activity_message(
                message_cog, make_message(message_user, "first eligible message"), 5)
            message_clock[0] = 1009.999
            await type(message_cog).on_activity_message(
                message_cog, make_message(message_user, "too soon"), 5)
            before_cooldown_edge = xp(message_user)[0]
            message_clock[0] = 1010.0
            await type(message_cog).on_activity_message(
                message_cog, make_message(message_user, "at the edge"), 5)
        message_config = await get_leveling_config(GUILD)
        check("Dashboard/API Message XP ON reads 10s and enforces the exact boundary",
              saved_message_on.status_code == 200
              and message_config["message_xp_enabled"] == 1
              and message_config["xp_cooldown_seconds"] == 10
              and before_cooldown_edge == 5 and xp(message_user)[0] == 10,
              f"before={before_cooldown_edge}, after={xp(message_user)}")

        # The Dashboard's existing checkbox is the one actually controlling
        # muted Voice XP, with both ON and OFF exercised end-to-end.
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
                (GUILD, USER))
        leveling = make_leveling()
        muted = {"self_mute": True, "mute": False,
                 "self_deaf": False, "deaf": False}
        await type(leveling).on_activity_voice_xp_tick(
            leveling, SimpleNamespace(id=GUILD), person(), muted)
        check("Dashboard Require-unmuted ON blocks muted Voice XP", xp()[0] == 0)

        payload["voice_require_unmuted"] = 0
        saved_off = client.post("/api/leveling/config", json=payload, headers=headers)
        stored_off = await get_leveling_config(GUILD)
        await type(leveling).on_activity_voice_xp_tick(
            leveling, SimpleNamespace(id=GUILD), person(), muted)
        check("Dashboard Require-unmuted OFF permits muted Voice XP",
              saved_off.status_code == 200 and stored_off["voice_require_unmuted"] == 0
              and xp()[0] == 3)

        payload["voice_xp_enabled"] = 0
        client.post("/api/leveling/config", json=payload, headers=headers)
        await type(leveling).on_activity_voice_xp_tick(
            leveling, SimpleNamespace(id=GUILD), person(),
            {"self_mute": False, "mute": False,
             "self_deaf": False, "deaf": False})
        check("Dashboard Voice XP OFF blocks only Voice XP", xp()[0] == 3)

        # Save the spam window/threshold through the API and prove that a real
        # incident, warning reply, and persistent penalty row follow that config.
        spam_user = USER + 61
        from utils.xp_calculator import xp_progress
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
                (GUILD, spam_user, 10_000, xp_progress(10_000)[0]))
        payload.update({
            "message_xp_enabled": 0,
            "spam_detection_enabled": 1,
            "spam_threshold": 3,
            "spam_window_seconds": 20,
            "spam_xp_penalty_divisor": 1000,
        })
        saved_spam = client.post("/api/leveling/config", json=payload, headers=headers)
        spam_config = await get_leveling_config(GUILD)
        spam_cog = make_leveling()
        spam_clock = [2000.0]
        spam_messages = []
        with patch.object(leveling_module, "time",
                          SimpleNamespace(time=lambda: spam_clock[0])):
            for offset, content in enumerate(("one", "two", "three", "four")):
                spam_clock[0] = 2000.0 + offset
                msg = make_message(spam_user, content)
                spam_messages.append(msg)
                await type(spam_cog).on_activity_message(spam_cog, msg, 2)
        penalty_rows = rows(
            "SELECT deducted,rolling_cap FROM leveling_spam_penalty_events "
            "WHERE guild_id=? AND user_id=?", (GUILD, spam_user))
        check("Dashboard/API spam settings drive the persisted runtime incident",
              saved_spam.status_code == 200
              and spam_config["spam_threshold"] == 3
              and spam_config["spam_window_seconds"] == 20
              and xp(spam_user)[0] == 9_990
              and penalty_rows == [(10, 50)],
              f"config={spam_config}, xp={xp(spam_user)}, penalties={penalty_rows}")
        check("incident warning is a fake-Discord reply once; flagged follow-up has no deduction",
              sum(msg.reply.await_count for msg in spam_messages) == 1
              and spam_messages[2].reply.await_count == 1
              and spam_messages[2].reply.await_args.kwargs["embed"].footer.text == "-10 XP"
              and xp(spam_user)[0] == 9_990)

        # Announcement OFF/ON is saved through Dashboard/API and observed at
        # the real level-up callback, not merely in a config read.
        announce_user = USER + 62
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,95,0)",
                (GUILD, announce_user))
        payload.update({
            "message_xp_enabled": 1,
            "xp_cooldown_seconds": 0,
            "spam_detection_enabled": 0,
            "levelup_announce": 0,
        })
        announce_off = client.post("/api/leveling/config", json=payload, headers=headers)
        announce_channel = SimpleNamespace(send=AsyncMock())
        announce_cog = make_leveling()
        await type(announce_cog).on_activity_message(
            announce_cog,
            make_message(announce_user, "announcement disabled", announce_channel), 5)
        announce_off_config = await get_leveling_config(GUILD)
        check("Dashboard announcement OFF preserves level-up XP and suppresses output",
              announce_off.status_code == 200
              and announce_off_config["levelup_announce"] == 0
              and xp(announce_user) == (100, 1)
              and announce_channel.send.await_count == 0)
        execute("UPDATE levels SET xp=95,level=0 WHERE guild_id=? AND user_id=?",
                (GUILD, announce_user))
        payload["levelup_announce"] = 1
        announce_on = client.post("/api/leveling/config", json=payload, headers=headers)
        announce_channel = SimpleNamespace(send=AsyncMock())
        announce_cog = make_leveling()
        await type(announce_cog).on_activity_message(
            announce_cog,
            make_message(announce_user, "announcement enabled", announce_channel), 5)
        check("Dashboard announcement ON emits one real-handler announcement",
              announce_on.status_code == 200 and xp(announce_user) == (100, 1)
              and announce_channel.send.await_count == 1)


async def test_message_cooldown_and_announcements():
    import cogs.leveling as leveling_module
    from cogs.leveling import Leveling
    from utils.xp_calculator import get_leveling_config

    execute("DELETE FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER + 1))
    user = USER + 1
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
            (GUILD, user))
    execute("DELETE FROM leveling_config WHERE guild_id=?", (GUILD,))
    defaults = await get_leveling_config(GUILD)
    check("a fresh guild receives a 10-second active Message XP cooldown",
          defaults["xp_cooldown_seconds"] == 10)
    set_config(message_xp_enabled=1, xp_cooldown_seconds=10,
               spam_detection_enabled=0)

    clock = [1000.0]
    leveling_module.time = SimpleNamespace(time=lambda: clock[0])
    cog = make_leveling()
    await Leveling.on_activity_message(
        cog, make_message(user, "first distinct message"), 5)
    after_first = xp(user)[0]
    clock[0] = 1009.999
    await Leveling.on_activity_message(
        cog, make_message(user, "second distinct message"), 5)
    before_boundary = xp(user)[0]
    clock[0] = 1010.0
    await Leveling.on_activity_message(
        cog, make_message(user, "third distinct message"), 5)
    check("10s cooldown blocks at 9.999s and allows exactly at 10s",
          after_first == 5 and before_boundary == 5 and xp(user)[0] == 10,
          f"{after_first} -> {before_boundary} -> {xp(user)[0]}")

    # Announcement setting suppresses only its own output, never the XP grant.
    announce_user = USER + 2
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,95,0)",
            (GUILD, announce_user))
    set_config(message_xp_enabled=1, xp_cooldown_seconds=0,
               spam_detection_enabled=0, levelup_announce=0)
    clock[0] = 2000.0
    channel = SimpleNamespace(send=AsyncMock())
    message = make_message(announce_user, "announcement test", channel)
    cog = make_leveling()
    await Leveling.on_activity_message(cog, message, 5)
    check("level-up announcement OFF does not suppress XP",
          xp(announce_user) == (100, 1) and channel.send.await_count == 0)

    execute("UPDATE levels SET xp=95,level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, announce_user))
    execute("UPDATE leveling_config SET levelup_announce=1 WHERE guild_id=?", (GUILD,))
    clock[0] = 2010.0
    channel = SimpleNamespace(send=AsyncMock())
    message = make_message(announce_user, "announcement enabled", channel)
    cog = make_leveling()
    await Leveling.on_activity_message(cog, message, 5)
    check("level-up announcement ON announces without changing the XP source",
          xp(announce_user) == (100, 1) and channel.send.await_count == 1)


def legacy_ticks(events):
    """The `activity_voice_tick` dispatches only (what missions/mvp consume)."""
    return [event for event in events if event[0] == "activity_voice_tick"]


async def test_real_voice_participant_gate():
    from cogs.activity_engine import ActivityEngine
    import cogs.leveling as leveling_module
    from cogs.leveling import Leveling

    # One real human plus a bot is still only one participant and dispatches
    # no activity tick. A bot may never satisfy the two-person voice gate.
    human = person(USER + 10)
    bot_member = person(USER + 11, bot=True)
    one_channel = SimpleNamespace(id=7001, members=[human, bot_member])
    one_guild = SimpleNamespace(id=GUILD, afk_channel=None,
                                voice_channels=[one_channel])
    one_events = []
    engine = ActivityEngine.__new__(ActivityEngine)
    engine.bot = SimpleNamespace(guilds=[one_guild],
                                 dispatch=lambda *args: one_events.append(args))
    await ActivityEngine.voice_tick_task.coro(engine)
    check("one real human plus a bot is below the voice participant threshold",
          legacy_ticks(one_events) == [])

    # Two actual people pass the ActivityEngine filter; bots still never appear
    # in its dispatch list. Feed those real dispatch events into Leveling's
    # actual runtime listener to confirm each human receives the voice tick.
    human1 = person(USER + 12)
    human2 = person(USER + 13)
    bot2 = person(USER + 14, bot=True)
    channel = SimpleNamespace(id=7002, members=[human1, human2, bot2])
    guild = SimpleNamespace(id=GUILD, afk_channel=None,
                            voice_channels=[channel])
    events = []
    engine.bot = SimpleNamespace(guilds=[guild],
                                 dispatch=lambda *args: events.append(args))
    await ActivityEngine.voice_tick_task.coro(engine)
    check("two real humans pass; bot does not count or receive a tick",
          len(legacy_ticks(events)) == 2
          and {event[2].id for event in legacy_ticks(events)}
          == {human1.id, human2.id})

    execute("DELETE FROM levels WHERE guild_id=? AND user_id IN (?,?)",
            (GUILD, human1.id, human2.id))
    for member in (human1, human2):
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
                (GUILD, member.id))
    set_config(message_xp_enabled=0, voice_xp_enabled=1,
               voice_require_unmuted=1, spam_detection_enabled=0)
    leveling = make_leveling()
    for event, event_guild, member, flags in events:
        if event == "activity_voice_xp_tick":
            await Leveling.on_activity_voice_xp_tick(
                leveling, event_guild, member, flags)
    check("Message XP OFF leaves the two-human Voice XP path active",
          xp(human1.id)[0] == 3 and xp(human2.id)[0] == 3)

    # Deafening either participant prevents that member's tick in the activity
    # engine, but a remaining solo human still cannot satisfy the two-real gate.
    human2.voice.self_deaf = True
    events.clear()
    await ActivityEngine.voice_tick_task.coro(engine)
    check("deafened member receives no activity tick",
          len(legacy_ticks(events)) == 1
          and legacy_ticks(events)[0][2].id == human1.id)
    solo_channel = SimpleNamespace(id=7003, members=[human1, bot2])
    guild.voice_channels = [solo_channel]
    events.clear()
    await ActivityEngine.voice_tick_task.coro(engine)
    check("after the second human leaves, human plus bot still earns no tick",
          legacy_ticks(events) == [])


async def test_level_stats_button_styles():
    import cogs.leveling as leveling_module
    from cogs.leveling import LevelRewardView

    view = LevelRewardView(SimpleNamespace(), GUILD, USER, claimable=False)
    check("Level page initially blue and Stats initially gray",
          view.show_level.style == discord.ButtonStyle.primary
          and view.show_stats.style == discord.ButtonStyle.secondary)
    guild = SimpleNamespace(id=GUILD, get_role=lambda _rid: None)
    member_obj = SimpleNamespace(id=USER)
    interaction = SimpleNamespace(
        user=member_obj, guild=guild,
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
    with patch.object(leveling_module, "_level_embed",
                      new=AsyncMock(return_value=discord.Embed(title="test"))):
        await view._refresh(interaction, "stats")
        check("Stats page makes Stats blue and Level gray",
              view.show_stats.style == discord.ButtonStyle.primary
              and view.show_level.style == discord.ButtonStyle.secondary)
        await view._refresh(interaction, "level")
        check("returning to Level restores blue/gray active-page styling",
              view.show_level.style == discord.ButtonStyle.primary
              and view.show_stats.style == discord.ButtonStyle.secondary)


async def main():
    await reset_database()
    await test_dashboard_config_api()
    await test_message_cooldown_and_announcements()
    await test_real_voice_participant_gate()
    await test_level_stats_button_styles()
    print("ALL LEVELING RUNTIME CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
