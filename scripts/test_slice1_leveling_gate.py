"""Regression: XP sources are independent; no global Leveling master gate remains.

Run with:
    python scripts/test_slice1_leveling_gate.py
"""
import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import GUILD, USER, execute, rows, reset_database


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def xp_state(user=USER):
    found = rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else (0, 0)


def cog():
    from cogs.leveling import Leveling
    obj = Leveling.__new__(Leveling)
    obj.bot = SimpleNamespace(get_guild=lambda _gid: None)
    obj._xp_cooldowns = {}
    obj._spam_tracker = {}
    obj._spam_incidents = {}
    obj._spam_warn_times = {}
    return obj


def person(user=USER):
    return SimpleNamespace(
        id=user, bot=False, roles=[], mention=f"<@{user}>",
        display_name=f"user-{user}", display_avatar=SimpleNamespace(url=""),
        joined_at=None, premium_since=None)


async def main():
    await reset_database()
    from cogs.leveling import Leveling
    from utils.mission_engine import create_definition, ensure_tables, record_activity
    from utils.reward_engine import RewardError, give_reward
    from utils.xp_calculator import LEVELING_CONFIG_DEFAULTS, get_leveling_config

    check("fresh Message XP default is enabled",
          LEVELING_CONFIG_DEFAULTS["message_xp_enabled"] == 1)
    check("no global enabled column in the live schema",
          "enabled" not in {row[1] for row in rows("PRAGMA table_info(leveling_config)")})

    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
            (GUILD, USER))
    execute("INSERT INTO leveling_config (guild_id,message_xp_enabled,voice_xp_enabled,"
            "voice_xp_per_minute,voice_require_unmuted,spam_detection_enabled) "
            "VALUES (?,0,1,3,1,0)", (GUILD,))
    cfg = await get_leveling_config(GUILD)
    check("stored Dashboard Message XP OFF is what runtime reads",
          cfg["message_xp_enabled"] == 0)

    # Message XP OFF stops only the message listener. Shared XP grants from
    # mission/event/minigame/tag sources still use the generic reward path.
    message = SimpleNamespace(
        author=person(), guild=SimpleNamespace(id=GUILD), content="hello world",
        channel=SimpleNamespace(send=AsyncMock()))
    leveling = cog()
    await Leveling.on_activity_message(leveling, message, 2)
    check("Message XP toggle blocks only Message XP", xp_state() == (0, 0),
          str(xp_state()))

    for source in ("mission", "event", "minigame", "tag_partner", "tag_mission"):
        result = await give_reward(
            SimpleNamespace(get_guild=lambda _guild_id: None), GUILD, USER, "xp", amount=5,
            reason=f"independent {source}", source=source)
        check(f"{source} XP grant is independent of Message XP",
              result.get("success") is True)
    xp_after_other_sources = xp_state()[0]
    check("independent grants actually changed XP", xp_after_other_sources == 25,
          str(xp_after_other_sources))

    missing_amount_raises = False
    try:
        await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=None)
    except RewardError:
        missing_amount_raises = True
    check("shared XP grants still reject a missing amount",
          missing_amount_raises)

    # Mission completion still pays its XP reward while Message XP itself is off.
    await ensure_tables()
    await create_definition(GUILD, name="Independent mission", mtype="words",
                            target=1, reward_type="xp", reward_value="7")
    completed = await record_activity(SimpleNamespace(get_guild=lambda _guild_id: None), GUILD, USER, "words", 1)
    check("mission completes and grants XP with Message XP OFF",
          completed and xp_state()[0] == xp_after_other_sources + 7,
          f"completed={completed}, xp={xp_state()[0]}")

    # /leaderboard is a read path, not a passive-XP grant. Keep its real
    # callback covered while Message XP is disabled so gate refactors cannot
    # silently hide persisted XP from members.
    board_member = person()
    board_guild = SimpleNamespace(
        id=GUILD, get_member=lambda user_id: board_member if user_id == USER else None)
    board_ix = SimpleNamespace(
        guild=board_guild, response=SimpleNamespace(send_message=AsyncMock()))
    board_cog = cog()
    before_board = xp_state()
    await Leveling.leaderboard.callback(board_cog, board_ix)
    board_embed = board_ix.response.send_message.call_args.kwargs["embed"]
    board_text = "\n".join(
        f"{field.name}\n{field.value}" for field in board_embed.fields)
    check("/leaderboard shows persisted XP while Message XP is OFF",
          f"{before_board[0]:,} XP" in board_text and xp_state() == before_board,
          board_text)

    from utils.minigame_store import ensure_tables as ensure_minigame_tables
    await ensure_minigame_tables()
    rank_member = person()
    rank_ix = SimpleNamespace(
        guild=board_guild, user=rank_member,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    rank_cog = cog()
    # Keep the real rank-data/database path while faking only image rendering.
    with patch("utils.rank_card_renderer.render_rank_card", new_callable=AsyncMock) as render:
        render.return_value = io.BytesIO(b"rank-card")
        await Leveling.rank.callback(rank_cog, rank_ix)
    rank_data = render.await_args.args[0]
    rank_file = rank_ix.followup.send.await_args.kwargs["file"]
    check("/rank renders stored XP while Message XP is OFF",
          rank_ix.response.defer.await_count == 1
          and rank_data["xp_total"] == before_board[0]
          and rank_file.filename == "rank.png" and xp_state() == before_board,
          f"xp_total={rank_data['xp_total']} file={rank_file.filename}")

    # Voice XP has its own enable switch and the existing Require-unmuted toggle.
    muted_flags = {"self_mute": True, "mute": False,
                   "deaf": False, "self_deaf": False}
    await Leveling.on_activity_voice_xp_tick(leveling, SimpleNamespace(id=GUILD),
                                          person(), muted_flags)
    check("Require unmuted ON blocks a muted member", xp_state()[0] == 32)
    execute("UPDATE leveling_config SET voice_require_unmuted=0 WHERE guild_id=?",
            (GUILD,))
    await Leveling.on_activity_voice_xp_tick(leveling, SimpleNamespace(id=GUILD),
                                          person(), muted_flags)
    check("Require unmuted OFF permits muted-member Voice XP",
          xp_state()[0] == 35, str(xp_state()))
    execute("UPDATE leveling_config SET voice_xp_enabled=0 WHERE guild_id=?",
            (GUILD,))
    await Leveling.on_activity_voice_xp_tick(leveling, SimpleNamespace(id=GUILD),
                                          person(), {"self_mute": False, "mute": False})
    check("Voice XP OFF blocks only Voice XP", xp_state()[0] == 35)

    # Announcements are also independent of both switches: they follow the
    # Message XP award and only their own levelup_announce option.
    execute("UPDATE levels SET xp=95,level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE leveling_config SET message_xp_enabled=1, spam_detection_enabled=0, "
            "xp_cooldown_seconds=0, levelup_announce=0 WHERE guild_id=?", (GUILD,))
    announce_channel = SimpleNamespace(send=AsyncMock())
    message.guild.get_channel = lambda _channel_id: None
    message.channel = announce_channel
    leveling._xp_cooldowns.clear()
    await Leveling.on_activity_message(leveling, message, 3)
    check("levelup_announce OFF suppresses announcement only",
          xp_state() == (100, 1) and announce_channel.send.await_count == 0,
          f"state={xp_state()}, sends={announce_channel.send.await_count}")

    execute("UPDATE levels SET xp=95,level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE leveling_config SET levelup_announce=1 WHERE guild_id=?", (GUILD,))
    leveling._xp_cooldowns.clear()
    await Leveling.on_activity_message(leveling, message, 3)
    check("announcements ON announces a Message XP level-up",
          xp_state() == (100, 1) and announce_channel.send.await_count == 1)

    # The grant engine has no skip contract or persisted master-gate branch.
    import inspect
    from utils import reward_engine
    source = inspect.getsource(reward_engine.give_reward)
    check("shared XP grant path contains no global leveling gate",
          "leveling_disabled" not in source and "get_leveling_config" not in source)

    print("ALL INDEPENDENT XP SOURCE CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
