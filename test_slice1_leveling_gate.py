"""Slice 1: leveling_config.enabled is the passive XP gate.

Run from the scripts directory:
    python test_slice1_leveling_gate.py
"""
import asyncio
import io
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, member,
)

OTHER = USER + 1
FRESH = USER + 2


def level_of(user=USER):
    found = rows(
        "SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
        (GUILD, user))
    return found[0] if found else None


def wallet_of(user=USER):
    return rows(
        "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
        (GUILD, user))[0]


def set_enabled(value):
    execute(
        "INSERT INTO leveling_config (guild_id, enabled) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
        (GUILD, value))


def capture(coro):
    buf = io.StringIO()

    async def run():
        with redirect_stdout(buf):
            return await coro

    return buf, run()


class Fail(Exception):
    pass


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(name)


def cog():
    from cogs.leveling import Leveling
    obj = Leveling.__new__(Leveling)
    obj.bot = SimpleNamespace(get_guild=lambda gid: None)
    obj._xp_cooldowns = {}
    obj._spam_tracker = {}
    return obj


def person(user=USER, name="Phase One"):
    who = member(user=user)
    who.bot = False
    who.display_name = name
    who.mention = f"<@{user}>"
    who.roles = []
    who.add_roles = AsyncMock()
    who.remove_roles = AsyncMock()
    who.send = AsyncMock()
    who.display_avatar = SimpleNamespace(url="")
    who.guild = SimpleNamespace(id=GUILD, get_role=lambda _id: None)
    return who


def guild_for(*people):
    by_id = {p.id: p for p in people}
    role = SimpleNamespace(id=55, name="VIP", position=1)
    top = SimpleNamespace(position=10)
    guild = SimpleNamespace(
        id=GUILD,
        me=SimpleNamespace(top_role=top),
        get_member=lambda uid: by_id.get(uid),
        get_role=lambda rid: role if int(rid) == 55 else None,
        fetch_member=AsyncMock(return_value=people[0] if people else None),
    )
    for p in people:
        p.guild = guild
    return guild


def bot_for(guild):
    return SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)


async def main():
    from utils.reward_engine import RewardError, give_reward, xp_grant_skipped
    from utils.mission_engine import create_definition, ensure_tables, record_activity
    from utils import minigame_store
    from utils.minigame_engine import MinigameEngine
    from cogs.events import ButtonRaceView
    from cogs.leveling import Leveling
    from cogs.tagmissions import TagMissions
    from cogs.tagpartners import TagPartners

    await reset_database()
    await ensure_tables()
    await minigame_store.ensure_tables()
    seed_member()
    set_enabled(0)
    before = level_of()
    wallet_before = wallet_of()
    ledger_before = rows("SELECT COUNT(*) FROM transaction_ledger")[0][0]

    # 1. Direct XP grant is a quiet skip. No levels write, no level-up reward.
    result = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=50,
        reason="slice1", source="test")
    check(
        "1 direct XP skip contract",
        result == {
            "success": False, "skipped": True, "reason": "leveling_disabled",
        } and xp_grant_skipped(result),
        str(result))
    fresh = await give_reward(
        SimpleNamespace(), GUILD, FRESH, "xp", amount=10,
        reason="slice1", source="test")
    check(
        "1 no levels row created for a new member",
        xp_grant_skipped(fresh) and level_of(FRESH) is None)
    execute(
        "INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount) "
        "VALUES (?, 1, 'balance', 777)", (GUILD,))
    execute(
        "INSERT INTO levels (guild_id, user_id, xp, level, prestige) "
        "VALUES (?, ?, 90, 0, 0)", (GUILD, OTHER))
    execute(
        "INSERT INTO economy (guild_id, user_id, balance, diamonds) "
        "VALUES (?, ?, 0, 0)", (GUILD, OTHER))
    other = person(OTHER, "Other")
    guild = guild_for(person(), other)
    crossed = await give_reward(
        bot_for(guild), GUILD, OTHER, "xp", amount=20,
        reason="would level", source="test")
    check(
        "1 level-up rewards do not run",
        xp_grant_skipped(crossed)
        and level_of(OTHER) == (90, 0, 0)
        and wallet_of(OTHER) == (0, 0),
        f"levels={level_of(OTHER)} wallet={wallet_of(OTHER)}")
    raised = False
    try:
        await give_reward(SimpleNamespace(), GUILD, USER, "xp", amount=None)
    except RewardError:
        raised = True
    check("1 missing XP amount still raises", raised)
    check(
        "1 seeded progress untouched",
        level_of() == before and wallet_of() == wallet_before
        and rows("SELECT COUNT(*) FROM transaction_ledger")[0][0] == ledger_before)

    # 2. Chat path returns before XP math.
    leveling = cog()
    message = SimpleNamespace(
        author=person(), guild=SimpleNamespace(id=GUILD), content="hello there")
    buf, ran = capture(Leveling.on_activity_message(leveling, message, 3))
    await ran
    check(
        "2 chat XP does not change levels",
        level_of() == before and "failed" not in buf.getvalue().lower())

    # 3. Voice tick returns before XP math even if voice XP itself is on.
    execute(
        "UPDATE leveling_config SET voice_xp_enabled=1 WHERE guild_id=?",
        (GUILD,))
    buf, ran = capture(Leveling.on_activity_voice_tick(
        leveling, SimpleNamespace(id=GUILD), person(),
        {"self_mute": False, "mute": False, "deaf": False, "self_deaf": False}))
    await ran
    check(
        "3 voice XP does not change levels",
        level_of() == before and "failed" not in buf.getvalue().lower())

    # 4. Mission stays completed. Skip is not a grant failure.
    await create_definition(
        GUILD, name="Say one thing", mtype="messages", target=1,
        reward_type="xp", reward_value="25")
    buf, ran = capture(record_activity(
        SimpleNamespace(), GUILD, USER, "messages", 1))
    completed = await ran
    progress = rows(
        "SELECT completed FROM mission_progress WHERE guild_id=? AND user_id=?",
        (GUILD, USER))
    log = buf.getvalue()
    check(
        "4 mission stays completed without a failure log",
        bool(completed) and progress == [(1,)] and level_of() == before
        and "failed" not in log.lower() and "error" not in log.lower(),
        log.strip())

    # 5. XP skip must not erase the normal winner record. Real finish_run
    # writes winner_id only when the grant status stays "won".
    log_id = await minigame_store.start_run(
        GUILD, None, "Slice", None, "Slice", "quick_click", "test", None)
    engine = MinigameEngine(
        {"guild_id": GUILD, "name": "Slice",
         "rewards": [{"reward_type": "xp", "reward_value": "40", "weight": 1}]},
        "test", log_id, bot=SimpleNamespace())
    buf, ran = capture(engine.resolve([{"id": USER, "name": "Phase One"}]))
    await ran
    run_row = rows(
        "SELECT status, winner_id, winner_display_name, winners_json "
        "FROM minigames_log WHERE id=?", (log_id,))[0]
    import json
    winners = json.loads(run_row[3])
    from cogs.minigames import get_user_win_count
    wins = await get_user_win_count(GUILD, USER)
    check(
        "5 XP skip keeps the normal winner outcome",
        run_row[0] == "completed" and run_row[1] == USER
        and run_row[2] == "Phase One"
        and winners and winners[0]["status"] == "won"
        and winners[0].get("error") is None
        and "failed" not in winners[0].get("status", "")
        and wins == 1 and level_of() == before
        and "failed" not in buf.getvalue().lower(),
        f"run={run_row[:3]} winners={winners}")

    coin_log = await minigame_store.start_run(
        GUILD, None, "Slice Coins", None, "Slice", "quick_click", "test", None)
    coin_engine = MinigameEngine(
        {"guild_id": GUILD, "name": "Slice Coins",
         "rewards": [{"reward_type": "coins", "reward_value": "8", "weight": 1}]},
        "test", coin_log, bot=bot_for(guild_for(person())))
    wallet_mid = wallet_of()
    await coin_engine.resolve([{"id": USER, "name": "Phase One"}])
    coin_row = rows(
        "SELECT status, winner_id FROM minigames_log WHERE id=?", (coin_log,))[0]
    check(
        "5 non-XP minigame still grants and keeps the winner",
        coin_row == ("completed", USER) and wallet_of()[0] > wallet_mid[0]
        and level_of() == before and await get_user_win_count(GUILD, USER) == 2)

    # 6. Event winner line must not say XP was received.
    view = ButtonRaceView(1, 1, "xp", "60")
    interaction = SimpleNamespace(
        user=person(), guild=SimpleNamespace(id=GUILD),
        client=SimpleNamespace(),
        response=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
        channel=SimpleNamespace(send=AsyncMock()),
    )
    buf, ran = capture(view.children[0].callback(interaction))
    await ran
    text = interaction.response.send_message.call_args.args[0]
    channel_text = interaction.channel.send.call_args.args[0]
    check(
        "6 event line does not say XP was received",
        "no XP was added" in text and "You won **" not in text
        and "XP" not in channel_text and "reward" not in channel_text.lower()
        and level_of() == before and "failed" not in buf.getvalue().lower(),
        text)

    # 7. Tag mission does not send the tag-removed failure DM.
    mission_id = execute(
        "INSERT INTO tag_missions (guild_id, title, reward_type, reward_amount, "
        "starts_at, ends_at, status, success_message, failure_message) "
        "VALUES (?, 'Wear it', 'xp', '30', '2020-01-01', '2020-01-02', "
        "'active', 'REWARD SENT', 'TAG REMOVED')", (GUILD,))
    execute(
        "INSERT INTO tag_mission_participants (mission_id, guild_id, user_id) "
        "VALUES (?, ?, ?)", (mission_id, GUILD, USER))
    wearer = person()
    wearer.primary_guild = SimpleNamespace(
        identity_enabled=True, identity_guild_id=GUILD)
    guild = guild_for(wearer)
    tag_cog = TagMissions.__new__(TagMissions)
    tag_cog.bot = bot_for(guild)
    buf, ran = capture(TagMissions._resolve_mission(
        tag_cog, mission_id, GUILD, "xp", "30", None, None,
        "REWARD SENT", "TAG REMOVED"))
    await ran
    outcome = rows(
        "SELECT outcome FROM tag_mission_participants WHERE mission_id=?",
        (mission_id,))[0][0]
    counted = rows(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN outcome='rewarded' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN outcome='removed_tag' THEN 1 ELSE 0 END) "
        "FROM tag_mission_participants WHERE mission_id=?", (mission_id,))[0]
    mission_status = rows(
        "SELECT status FROM tag_missions WHERE id=?", (mission_id,))[0][0]
    sent = [call.args[0] for call in wearer.send.await_args_list]
    check(
        "7 tag wearer stays in the rewarded count without an XP DM",
        outcome == "rewarded" and counted == (1, 1, 0)
        and mission_status == "completed" and sent == []
        and level_of() == before and "failed" not in buf.getvalue().lower()
        and "REWARD SENT" not in buf.getvalue(),
        f"outcome={outcome} counts={counted} sent={sent}")

    removed_id = execute(
        "INSERT INTO tag_missions (guild_id, title, reward_type, reward_amount, "
        "starts_at, ends_at, status, success_message, failure_message) "
        "VALUES (?, 'Dropped', 'xp', '30', '2020-01-01', '2020-01-02', "
        "'active', 'REWARD SENT', 'TAG REMOVED')", (GUILD,))
    execute(
        "INSERT INTO tag_mission_participants (mission_id, guild_id, user_id) "
        "VALUES (?, ?, ?)", (removed_id, GUILD, USER))
    dropped = person()
    dropped.primary_guild = SimpleNamespace(
        identity_enabled=False, identity_guild_id=None)
    drop_guild = guild_for(dropped)
    drop_cog = TagMissions.__new__(TagMissions)
    drop_cog.bot = bot_for(drop_guild)
    await TagMissions._resolve_mission(
        drop_cog, removed_id, GUILD, "xp", "30", None, None,
        "REWARD SENT", "TAG REMOVED")
    dropped_outcome = rows(
        "SELECT outcome FROM tag_mission_participants WHERE mission_id=?",
        (removed_id,))[0][0]
    check(
        "7 tag removal still uses the removed-tag outcome and DM",
        dropped_outcome == "removed_tag"
        and dropped.send.await_args.args[0] == "TAG REMOVED"
        and level_of() == before)

    coin_mission = execute(
        "INSERT INTO tag_missions (guild_id, title, reward_type, reward_amount, "
        "starts_at, ends_at, status, success_message, failure_message) "
        "VALUES (?, 'Coins', 'coins', '6', '2020-01-01', '2020-01-02', "
        "'active', 'COIN SENT', 'TAG REMOVED')", (GUILD,))
    execute(
        "INSERT INTO tag_mission_participants (mission_id, guild_id, user_id) "
        "VALUES (?, ?, ?)", (coin_mission, GUILD, USER))
    coin_wearer = person()
    coin_wearer.primary_guild = SimpleNamespace(
        identity_enabled=True, identity_guild_id=GUILD)
    coin_guild = guild_for(coin_wearer)
    coin_tag = TagMissions.__new__(TagMissions)
    coin_tag.bot = bot_for(coin_guild)
    wallet_tag = wallet_of()
    await TagMissions._resolve_mission(
        coin_tag, coin_mission, GUILD, "coins", "6", None, None,
        "COIN SENT", "TAG REMOVED")
    coin_outcome = rows(
        "SELECT outcome FROM tag_mission_participants WHERE mission_id=?",
        (coin_mission,))[0][0]
    check(
        "7 non-XP tag mission still rewards and sends the success DM",
        coin_outcome == "rewarded"
        and coin_wearer.send.await_args.args[0] == "COIN SENT"
        and wallet_of()[0] > wallet_tag[0] and level_of() == before)

    # 8. Tag partner does not consume the one-time XP reward.
    execute(
        "INSERT INTO tag_partner_rewards (guild_id, partner_guild_id, "
        "reward_type, reward_amount, welcome_message, enabled) "
        "VALUES (?, 777, 'xp', '15', 'WELCOME XP', 1)", (GUILD,))
    joiner = person()
    joiner.primary_guild = SimpleNamespace(
        identity_enabled=True, identity_guild_id=777)
    joiner.guild = SimpleNamespace(id=GUILD)
    partner_cog = TagPartners.__new__(TagPartners)
    partner_cog.bot = SimpleNamespace()
    buf, ran = capture(TagPartners.on_member_join(partner_cog, joiner))
    await ran
    logged = rows(
        "SELECT COUNT(*) FROM tag_join_reward_log WHERE guild_id=? AND user_id=?",
        (GUILD, USER))[0][0]
    check(
        "8 tag partner does not log ungiven XP",
        logged == 0 and joiner.send.await_count == 0 and level_of() == before
        and "failed" not in buf.getvalue().lower(),
        buf.getvalue().strip())

    # 9. Non-XP rewards and admin XP tools still work. Toggle itself is inert.
    coins = await give_reward(
        bot_for(guild_for(person())), GUILD, USER, "coins", amount=10,
        reason="slice1", source="test")
    diamonds = await give_reward(
        bot_for(guild_for(person())), GUILD, USER, "diamonds", amount=2,
        reason="slice1", source="test")
    item = await give_reward(
        bot_for(guild_for(person())), GUILD, USER, "item", amount=1,
        item_name="Slice Token", reason="slice1", source="test")
    role_person = person()
    role_guild = guild_for(role_person)
    role = await give_reward(
        bot_for(role_guild), GUILD, USER, "role", role_id=55,
        reason="slice1", source="test")
    owned = rows(
        "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
        "AND item_name='Slice Token'", (GUILD, USER))
    check(
        "9 coins, diamonds, items, and roles still grant",
        coins.get("success") and diamonds.get("success") and item.get("success")
        and role.get("success") and wallet_of()[0] > wallet_before[0]
        and wallet_of()[1] == wallet_before[1] + 2 and owned == [(1,)]
        and role_person.add_roles.await_count == 1
        and level_of() == before,
        f"coins={coins.get('success')} diamonds={diamonds.get('success')} "
        f"item={item.get('error')} role={role.get('error')} wallet={wallet_of()}")

    await create_definition(
        GUILD, name="Coin mission", mtype="words", target=1,
        reward_type="coins", reward_value="5")
    wallet_mid = wallet_of()
    buf, ran = capture(record_activity(
        bot_for(guild_for(person())), GUILD, USER, "words", 1))
    await ran
    check(
        "9 mission coins still grant while leveling is off",
        wallet_of()[0] > wallet_mid[0] and level_of() == before
        and "failed" not in buf.getvalue().lower())

    execute(
        "INSERT INTO tag_partner_rewards (guild_id, partner_guild_id, "
        "reward_type, reward_amount, welcome_message, enabled) "
        "VALUES (?, 778, 'coins', '4', 'WELCOME COINS', 1)", (GUILD,))
    coin_joiner = person()
    coin_joiner.primary_guild = SimpleNamespace(
        identity_enabled=True, identity_guild_id=778)
    coin_guild = guild_for(coin_joiner)
    coin_joiner.guild = coin_guild
    partner_cog.bot = bot_for(coin_guild)
    buf, ran = capture(TagPartners.on_member_join(partner_cog, coin_joiner))
    await ran
    partner_log = rows(
        "SELECT COUNT(*) FROM tag_join_reward_log WHERE partner_guild_id=778")[0][0]
    check(
        "9 tag partner coins still log and welcome",
        partner_log == 1 and coin_joiner.send.await_count == 1
        and coin_joiner.send.await_args.args[0] == "WELCOME COINS"
        and "failed" not in buf.getvalue().lower())

    admin = person(OTHER)
    admin_ix = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await Leveling.setxp.callback(cog(), admin_ix, admin, 1234)
    check("9 admin setxp still writes while leveling is off",
          level_of(OTHER)[0] == 1234 and level_of(OTHER)[1] >= 1)
    await Leveling.resetxp.callback(cog(), admin_ix, admin)
    check("9 admin resetxp still writes while leveling is off",
          level_of(OTHER)[0] == 0 and level_of(OTHER)[1] == 0)

    snapshot = level_of()
    set_enabled(1)
    set_enabled(0)
    check("9 toggling the setting does not change levels", level_of() == snapshot)

    rank_person = person()
    rank_guild = guild_for(rank_person)
    rank_ix = SimpleNamespace(
        guild=rank_guild,
        user=rank_person,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    import inspect
    rank_source = inspect.getsource(Leveling.rank.callback)
    check(
        "9 /rank source does not contain the passive XP gate",
        "leveling_disabled" not in rank_source and "xp_grant_skipped" not in rank_source
        and "enabled" not in rank_source)
    await Leveling.rank.callback(cog(), rank_ix)
    sent = rank_ix.followup.send.call_args
    rendered = sent is not None and (
        (sent.kwargs.get("file") is not None and sent.kwargs["file"].filename == "rank.png")
        or (sent.kwargs.get("embed") is not None
            and f"{before[0]:,}" in (sent.kwargs["embed"].description or "")
            + "".join(f.value for f in sent.kwargs["embed"].fields))
        or (sent.args and "no XP" not in sent.args[0]))
    check(
        "9 /rank still answers from stored XP while leveling is off",
        rank_ix.response.defer.await_count == 1 and rendered and level_of() == before,
        f"kwargs={None if sent is None else sorted(sent.kwargs)}")

    board_person = person()
    board_guild = guild_for(board_person)
    board_ix = SimpleNamespace(
        guild=board_guild,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    board_cog = cog()
    board_cog.bot = bot_for(board_guild)
    await Leveling.leaderboard.callback(board_cog, board_ix)
    embed = board_ix.response.send_message.call_args.kwargs["embed"]
    board_text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    check(
        "9 /leaderboard still shows stored XP while leveling is off",
        f"{before[0]:,} XP" in board_text and level_of() == before,
        board_text)

    # 10. ON, and a missing config row, keep the existing XP behavior.
    execute("DELETE FROM leveling_config WHERE guild_id=?", (GUILD,))
    missing = await give_reward(
        SimpleNamespace(), GUILD, USER, "xp", amount=1,
        reason="missing config", source="test")
    check(
        "10 missing config still grants XP",
        missing.get("success") is True and level_of()[0] == before[0] + 1,
        str(missing))

    set_enabled(1)
    on_before = level_of()
    await Leveling.on_activity_message(
        leveling, SimpleNamespace(
            author=person(), guild=SimpleNamespace(id=GUILD), content="hello"),
        3)
    after_chat = level_of()
    await Leveling.on_activity_voice_tick(
        leveling, SimpleNamespace(id=GUILD), person(),
        {"self_mute": False, "mute": False})
    after_voice = level_of()
    direct = await give_reward(
        bot_for(guild_for(person())), GUILD, USER, "xp", amount=7,
        reason="on", source="test")
    check(
        "10 chat, voice, and direct XP still grant when enabled",
        after_chat[0] > on_before[0] and after_voice[0] > after_chat[0]
        and direct.get("success") is True
        and level_of()[0] == after_voice[0] + 7,
        f"chat={after_chat[0] - on_before[0]} "
        f"voice={after_voice[0] - after_chat[0]} direct={direct.get('amount')}")

    # resetxp cleared the OFF fixture. Put it back just below level 1.
    execute(
        "UPDATE levels SET xp=90, level=0, prestige=0 WHERE guild_id=? AND user_id=?",
        (GUILD, OTHER))
    execute(
        "UPDATE economy SET balance=0, diamonds=0 WHERE guild_id=? AND user_id=?",
        (GUILD, OTHER))
    paid = await give_reward(
        bot_for(guild_for(person(OTHER))), GUILD, OTHER, "xp", amount=20,
        reason="now levels", source="test")
    pending = rows(
        "SELECT status, payload_json FROM level_reward_claims "
        "WHERE guild_id=? AND user_id=?", (GUILD, OTHER))
    check(
        "10 level-up creates a pending entitlement and does not auto-pay",
        paid.get("success") is True and paid.get("leveled_up") is True
        and level_of(OTHER) == (110, 1, 0) and wallet_of(OTHER)[0] == 0
        and pending and pending[0][0] == "pending" and "777" in pending[0][1],
        f"result={paid} levels={level_of(OTHER)} wallet={wallet_of(OTHER)} "
        f"claims={pending}")

    print("ALL SLICE 1 CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        sys.exit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
