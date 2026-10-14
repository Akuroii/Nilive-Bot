"""Production caller integrations for shared XP rewards.

These tests execute the real Events button, Missions completion handler,
MinigameEngine resolution, Tag Mission resolution and Tag Partner join listener
against a disposable database. Discord is represented by explicit async fakes;
these are runtime integrations, not live-Discord verification.

Run with:
    python scripts/test_leveling_caller_integrations.py
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from phase1_support import (
    DB_PATH, GUILD, USER, execute, reset_database, rows, seed_member,
)


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def xp(user=USER):
    found = rows("SELECT xp,level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else None


def person(user=USER, name=None):
    member = SimpleNamespace(
        id=user, bot=False, name=name or f"member-{user}",
        display_name=name or f"Member {user}", mention=f"<@{user}>",
        roles=[], display_avatar=SimpleNamespace(url=""),
        add_roles=AsyncMock(), remove_roles=AsyncMock(), send=AsyncMock(),
        primary_guild=None)
    return member


def make_guild(*members):
    by_id = {member.id: member for member in members}
    server = SimpleNamespace(
        id=GUILD, owner_id=USER + 99,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_member=lambda user_id: by_id.get(user_id),
        get_role=lambda _role_id: None,
        fetch_member=AsyncMock(side_effect=lambda user_id: by_id.get(user_id)),
        get_channel=lambda _channel_id: None,
    )
    for member in members:
        member.guild = server
    return server


def make_bot(guild):
    return SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None)


async def main():
    await reset_database()
    seed_member()

    # 1. Event button: real click → event winner row → shared XP grant →
    # response and the original max-winner completion behavior.
    from cogs.events import ButtonRaceView

    winner_one = person(USER)
    winner_two = person(USER + 1)
    event_guild = make_guild(winner_one, winner_two)
    event_bot = make_bot(event_guild)
    start_one = xp(USER)[0]
    start_two = xp(USER + 1)
    # The second winner needs the ordinary economy/levels fixture because a
    # successful XP reward goes through the same shared engine as production.
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,1000,2)",
            (GUILD, USER + 1))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, USER + 1))
    start_two = xp(USER + 1)[0]

    view = ButtonRaceView(501, 2, "xp", "60")

    def event_interaction(member):
        return SimpleNamespace(
            user=member, guild=event_guild, client=event_bot,
            response=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
            channel=SimpleNamespace(send=AsyncMock()),
        )

    first_ix = event_interaction(winner_one)
    await view.children[0].callback(first_ix)
    first_text = first_ix.response.send_message.await_args.args[0]
    check("Event click grants real XP and records the first winner",
          xp(USER)[0] == start_one + 60
          and rows("SELECT user_id FROM event_winners WHERE event_id=? ORDER BY id",
                   (501,)) == [(USER,)]
          and "**60** XP" in first_text,
          f"xp={xp(USER)}, reply={first_text!r}")
    check("Event does not finish before max_winners is reached",
          not view.finished and first_ix.message.edit.await_count == 0
          and first_ix.channel.send.await_count == 0)

    second_ix = event_interaction(winner_two)
    await view.children[0].callback(second_ix)
    second_text = second_ix.response.send_message.await_args.args[0]
    check("second Event click grants XP and completes at the winner limit",
          xp(USER + 1)[0] == start_two + 60 and view.finished
          and len(view.winners) == 2
          and rows("SELECT user_id FROM event_winners WHERE event_id=? ORDER BY id",
                   (501,)) == [(USER,), (USER + 1,)]
          and "**60** XP" in second_text,
          f"xp={xp(USER + 1)}, winners={view.winners}, reply={second_text!r}")
    check("max-winner completion disables the view and announces once",
          all(child.disabled for child in view.children)
          and second_ix.message.edit.await_count == 1
          and second_ix.channel.send.await_count == 1
          and "Event ended" in second_ix.channel.send.await_args.args[0])
    closed_ix = event_interaction(person(USER + 2))
    await view.children[0].callback(closed_ix)
    check("a click after max-winner completion cannot create a third winner",
          len(view.winners) == 2
          and closed_ix.response.send_message.await_args.args[0] == "This event has ended.")

    # A real failure result still owns a winner slot and runs the same finalizer;
    # this protects the regression branch without substituting the successful
    # grant integration above.
    failed_view = ButtonRaceView(502, 1, "xp", "20")
    failed_member = person(USER + 3)
    failed_guild = make_guild(failed_member)
    failed_ix = SimpleNamespace(
        user=failed_member, guild=failed_guild, client=make_bot(failed_guild),
        response=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
        channel=SimpleNamespace(send=AsyncMock()),
    )
    with patch("cogs.events.give_reward", new=AsyncMock(return_value={
            "success": False, "error": "simulated delivery failure"})):
        await failed_view.children[0].callback(failed_ix)
    check("Event delivery failure still finalizes the last winner slot",
          failed_view.finished and len(failed_view.winners) == 1
          and failed_ix.message.edit.await_count == 1
          and failed_ix.channel.send.await_count == 1
          and "could not be delivered" in failed_ix.response.send_message.await_args.args[0])

    # 2. Mission completion: real record_activity/record_activities logic,
    # persisted progress, completed status, and shared XP ledger source.
    from utils.mission_engine import create_definition, ensure_tables, record_activity
    await ensure_tables()
    mission_id = await create_definition(
        GUILD, name="Caller integration mission", mtype="words", target=1,
        reward_type="xp", reward_value="25", period="once")
    mission_xp_before = xp(USER)[0]
    completed = await record_activity(
        event_bot, GUILD, USER, "words", 1, channel_id=8001)
    progress = rows(
        "SELECT progress,completed FROM mission_progress "
        "WHERE guild_id=? AND user_id=? AND mission_id=?",
        (GUILD, USER, mission_id))
    ledger = rows(
        "SELECT amount,source,reason FROM transaction_ledger "
        "WHERE guild_id=? AND user_id=? AND currency='xp' ORDER BY id DESC LIMIT 1",
        (GUILD, USER))
    check("Mission caller completes and grants XP through production flow",
          any(item["id"] == mission_id for item in completed)
          and progress == [(1, 1)] and xp(USER)[0] == mission_xp_before + 25
          and ledger and ledger[0][0] == 25 and ledger[0][1] == "mission",
          f"progress={progress}, xp={xp(USER)}, ledger={ledger}")

    # 3. MinigameEngine.resolve: resolve a real configured winner/reward pool,
    # then assert final run/winner status and XP, not merely the source label.
    from utils import minigame_store
    from utils.minigame_engine import MinigameEngine
    await minigame_store.ensure_tables()
    run_id = await minigame_store.start_run(
        GUILD, None, "Caller Integration", None, "Caller Integration",
        "quick_click", "test", None)
    engine = MinigameEngine(
        {"guild_id": GUILD, "name": "Caller Integration",
         "rewards": [{"reward_type": "xp", "reward_value": "40", "weight": 1}]},
        "test", run_id, bot=event_bot)
    game_xp_before = xp(USER)[0]
    resolved = await engine.resolve([{"id": USER, "name": "Member"}])
    run = rows("SELECT status,winner_id,winners_json FROM minigames_log WHERE id=?",
               (run_id,))[0]
    import json
    game_winners = json.loads(run[2])
    check("Minigame resolution persists winner and actually awards XP",
          resolved is True and run[0] == "completed" and run[1] == USER
          and game_winners[0]["status"] == "won"
          and game_winners[0].get("error") is None
          and xp(USER)[0] == game_xp_before + 40,
          f"run={run}, winners={game_winners}, xp={xp(USER)}")

    # 4. Tag Mission: the actual end-of-mission callback fetches the participant,
    # verifies the live tag, marks rewarded, sends success text, and grants XP.
    from cogs.tagmissions import TagMissions
    mission_member = person(USER)
    mission_member.primary_guild = SimpleNamespace(
        identity_enabled=True, identity_guild_id=GUILD)
    tag_guild = make_guild(mission_member)
    tag_cog = TagMissions.__new__(TagMissions)
    tag_cog.bot = make_bot(tag_guild)
    tag_mission_id = execute(
        "INSERT INTO tag_missions (guild_id,title,reward_type,reward_amount,"
        "starts_at,ends_at,status,success_message,failure_message) "
        "VALUES (?, 'Tag caller', 'xp', '30', '2020-01-01', '2020-01-02',"
        "'active', 'TAG REWARD DELIVERED', 'TAG REMOVED')", (GUILD,))
    execute("INSERT INTO tag_mission_participants (mission_id,guild_id,user_id) "
            "VALUES (?,?,?)", (tag_mission_id, GUILD, USER))
    tag_mission_xp_before = xp(USER)[0]
    await TagMissions._resolve_mission(
        tag_cog, tag_mission_id, GUILD, "xp", "30", None, None,
        "TAG REWARD DELIVERED", "TAG REMOVED")
    participant = rows("SELECT outcome FROM tag_mission_participants "
                       "WHERE mission_id=? AND user_id=?", (tag_mission_id, USER))
    check("Tag Mission actual end callback records reward and grants XP",
          participant == [("rewarded",)]
          and xp(USER)[0] == tag_mission_xp_before + 30
          and mission_member.send.await_args.args[0] == "TAG REWARD DELIVERED",
          f"participant={participant}, xp={xp(USER)}")

    # 5. Tag Partner: actual member-join callback grants once, persists the
    # anti-repeat row and sends its configured welcome message.
    from cogs.tagpartners import TagPartners
    partner_id = 99001
    execute("INSERT INTO tag_partner_rewards "
            "(guild_id,partner_guild_id,reward_type,reward_amount,welcome_message,enabled) "
            "VALUES (?,?, 'xp','15','PARTNER WELCOME',1)", (GUILD, partner_id))
    partner_member = person(USER + 4)
    partner_member.primary_guild = SimpleNamespace(
        identity_enabled=True, identity_guild_id=partner_id)
    partner_guild = make_guild(partner_member)
    partner_cog = TagPartners.__new__(TagPartners)
    partner_cog.bot = make_bot(partner_guild)
    partner_xp_before = xp(partner_member.id)
    if partner_xp_before is None:
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,500,1)",
                (GUILD, partner_member.id))
        partner_xp_before = xp(partner_member.id)
    await TagPartners.on_member_join(partner_cog, partner_member)
    after_first_join = xp(partner_member.id)[0]
    join_log = rows("SELECT COUNT(*) FROM tag_join_reward_log "
                    "WHERE guild_id=? AND partner_guild_id=? AND user_id=?",
                    (GUILD, partner_id, partner_member.id))[0][0]
    check("Tag Partner actual join grants XP, logs once, and welcomes",
          after_first_join == partner_xp_before[0] + 15
          and join_log == 1 and partner_member.send.await_count == 1
          and partner_member.send.await_args.args[0] == "PARTNER WELCOME",
          f"xp={partner_xp_before[0]}->{after_first_join}, log={join_log}")
    await TagPartners.on_member_join(partner_cog, partner_member)
    check("Tag Partner repeat join does not repay or resend",
          xp(partner_member.id)[0] == after_first_join
          and rows("SELECT COUNT(*) FROM tag_join_reward_log "
                   "WHERE guild_id=? AND partner_guild_id=? AND user_id=?",
                   (GUILD, partner_id, partner_member.id))[0][0] == 1
          and partner_member.send.await_count == 1)

    print("ALL FIVE XP CALLER INTEGRATIONS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
