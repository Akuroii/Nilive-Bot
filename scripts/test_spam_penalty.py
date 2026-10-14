"""Incident-based anti-spam runtime regression suite.

Covers detection signals, quiet-window incident grouping, the capped
proportional XP deduction, warning rate limits, level consistency, and claim
ledger isolation through the active Message XP listener.
Run with:
    python scripts/test_spam_penalty.py
"""
import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows

APPROVED_SPAM_WARNING_TEXT = (
    "> مع كل احتراماتي لا تسبام <:brick:1556981905218478162>\n"
    "> -# من لفلك نيهاهاها   XP تم خصم (≖⩊≖)"
)


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def xp_to(level: int) -> int:
    return sum(math.floor(100 * current ** 1.5)
               for current in range(1, level + 1))


def xp_state(user):
    found = rows("SELECT xp,level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else (0, 0)


def make_cog():
    from cogs.leveling import Leveling
    obj = Leveling.__new__(Leveling)
    obj.bot = SimpleNamespace(get_guild=lambda _gid: None)
    obj._xp_cooldowns = {}
    obj._spam_tracker = {}
    obj._spam_incidents = {}
    obj._spam_warn_times = {}
    return obj


def make_message(user, content):
    author = SimpleNamespace(
        id=user, bot=False, roles=[], mention=f"<@{user}>",
        display_name=f"user-{user}")
    return SimpleNamespace(
        author=author,
        guild=SimpleNamespace(id=GUILD, get_channel=lambda _id: None),
        channel=SimpleNamespace(send=AsyncMock()),
        reply=AsyncMock(), content=content)


def set_spam_config(*, enabled=1, message_xp=1, threshold=10,
                    window=20, divisor=1000, cooldown=10):
    execute("""
        INSERT INTO leveling_config
            (guild_id,message_xp_enabled,xp_cooldown_seconds,
             spam_detection_enabled,spam_threshold,spam_window_seconds,
             spam_xp_penalty_divisor,levelup_announce)
        VALUES (?,?,?,?,?,?,?,0)
        ON CONFLICT(guild_id) DO UPDATE SET
            message_xp_enabled=excluded.message_xp_enabled,
            xp_cooldown_seconds=excluded.xp_cooldown_seconds,
            spam_detection_enabled=excluded.spam_detection_enabled,
            spam_threshold=excluded.spam_threshold,
            spam_window_seconds=excluded.spam_window_seconds,
            spam_xp_penalty_divisor=excluded.spam_xp_penalty_divisor,
            levelup_announce=0
    """, (GUILD, message_xp, cooldown, enabled, threshold, window, divisor))


def warning_embeds(messages):
    return [call.kwargs["embed"] for message in messages
            for call in message.reply.await_args_list
            if call.kwargs.get("embed") is not None]


async def main():
    import cogs.leveling as leveling_module
    from cogs.leveling import Leveling, SPAM_WARNING_TEXT
    from utils.level_claims import ensure_tables, record_crossing
    from utils.xp_calculator import (
        LEVELING_CONFIG_DEFAULTS, apply_spam_penalty, get_leveling_config,
        xp_progress,
    )
    import aiosqlite

    await reset_database()
    await ensure_tables()
    check("10s message cooldown and separate 20s spam window defaults",
          LEVELING_CONFIG_DEFAULTS["xp_cooldown_seconds"] == 10
          and LEVELING_CONFIG_DEFAULTS["spam_window_seconds"] == 20
          and LEVELING_CONFIG_DEFAULTS["spam_threshold"] == 10)
    check("incident divisor defaults to conservative 1/1000",
          LEVELING_CONFIG_DEFAULTS["spam_xp_penalty_divisor"] == 1000)
    check("spam detection remains enabled for an unconfigured guild",
          LEVELING_CONFIG_DEFAULTS["spam_detection_enabled"] == 1
          and (await get_leveling_config(GUILD + 90))["spam_detection_enabled"] == 1)

    # Proportional formula, per-incident cap, low-balance rounding and floor.
    for user, xp, divisor, expected in (
            (USER, 100_000, 1000, 100),
            (USER + 1, 100_000, 100, 1000),
            (USER + 2, 100_000, 1, 1000),  # corrupted divisor is clamped
            (USER + 3, 50, 1000, 0)):
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
                (GUILD, user, xp, xp_progress(xp)[0]))
        result = await apply_spam_penalty(
            GUILD, user, divisor, penalty_cap=1_000_000, now=1000.0)
        check(f"proportional deduction at xp={xp}, divisor={divisor}",
              result["deducted"] == expected
              and result["new_xp"] == xp - expected
              and result["applied"] == (expected > 0), str(result))
        check("stored level is recomputed from post-penalty XP",
              xp_state(user)[1] == xp_progress(xp_state(user)[0])[0])
        check("deduction never exceeds 1% or creates negative XP",
              0 <= result["new_xp"] <= xp
              and result["deducted"] <= xp // 100)

    # Restore the old boundary sweep with the current incident formula: every
    # live write must still persist xp_progress(new_xp), including exact edges.
    boundary_states = []
    for index, fixture_xp in enumerate((0, 3, 99, 100, 101, 384, 385,
                                        2819, 2820, 100_000)):
        boundary_user = USER + 30 + index
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
                (GUILD, boundary_user, fixture_xp,
                 xp_progress(fixture_xp)[0]))
        boundary_result = await apply_spam_penalty(
            GUILD, boundary_user, 100, penalty_cap=1_000_000,
            now=11_000.0 + index * 3600)
        expected_xp = fixture_xp - fixture_xp // 100
        stored = xp_state(boundary_user)
        boundary_states.append(
            stored == (expected_xp, xp_progress(expected_xp)[0])
            and boundary_result["new_level"] == xp_progress(expected_xp)[0]
            and 0 <= stored[0] <= fixture_xp)
    check("post-penalty XP/Level stay consistent across curve boundaries",
          all(boundary_states), str(boundary_states))

    zero_user = USER + 5
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
            (GUILD, zero_user))
    zero_penalty = await apply_spam_penalty(
        GUILD, zero_user, 1000, penalty_cap=50, now=1500.0)
    check("zero-XP incident remains at zero without debt or fake crossing",
          zero_penalty["deducted"] == 0 and zero_penalty["new_xp"] == 0
          and zero_penalty["new_level"] == 0 and xp_state(zero_user) == (0, 0))

    # The cumulative cap is one max production Message XP award for the hour.
    # It survives rebuilding every in-memory cog/state object because the
    # budget events are committed to SQLite with the XP deduction.
    budget_user = USER + 4
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, budget_user, 200_000, xp_progress(200_000)[0]))
    first_budget = await apply_spam_penalty(
        GUILD, budget_user, 100, penalty_cap=50, now=10_000.0)
    fresh_process_cog = make_cog()
    fresh_process_cog._spam_incidents.clear()
    reset_budget_attempts = []
    for incident in range(1, 8):
        reset_budget_attempts.append(await apply_spam_penalty(
            GUILD, budget_user, 100, penalty_cap=50,
            now=10_000.0 + incident * 300))
    used_in_hour = rows(
        "SELECT COALESCE(SUM(deducted),0) FROM leveling_spam_penalty_events "
        "WHERE guild_id=? AND user_id=? AND created_at>?",
        (GUILD, budget_user, 8_500.0))
    check("one incident cannot exceed the one-message rolling cap",
          first_budget["deducted"] == 50 and first_budget["rolling_cap"] == 50,
          str(first_budget))
    check("repeated incidents after a cog/state reset cannot exceed the hour budget",
          used_in_hour == [(50,)]
          and all(result["deducted"] == 0 for result in reset_budget_attempts)
          and xp_state(budget_user)[0] == 199_950,
          f"sum={used_in_hour}, xp={xp_state(budget_user)}")
    after_rollover = await apply_spam_penalty(
        GUILD, budget_user, 100, penalty_cap=50, now=13_601.0)
    rolling_after_expiry = rows(
        "SELECT COALESCE(SUM(deducted),0) FROM leveling_spam_penalty_events "
        "WHERE guild_id=? AND user_id=? AND created_at>?",
        (GUILD, budget_user, 10_001.0))
    check("a deduction ages out after the rolling hour and budget refills",
          after_rollover["deducted"] == 50
          and rolling_after_expiry == [(50,)]
          and xp_state(budget_user)[0] == 199_900,
          f"result={after_rollover}, sum={rolling_after_expiry}")

    # Create real role + currency entitlements, deliver them through Claim All,
    # then prove a down-level penalty neither mutates the fulfilled ledger nor
    # removes/re-pays the already-delivered rewards when XP is earned again.
    penalty_user = USER + 10
    start_xp = xp_to(3) + 5
    role_id = 3333
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id) VALUES (?,?,?)",
            (GUILD, 3, role_id))
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount) "
            "VALUES (?,3,'balance',777)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, penalty_user, start_xp, 3))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, penalty_user))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await record_crossing(db, GUILD, penalty_user, xp_to(2), start_xp, {},
                              source="test")
        await db.commit()

    role = SimpleNamespace(id=role_id, name="Level Three", position=1)
    guild = SimpleNamespace(
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_role=lambda requested: role if int(requested) == role_id else None)

    class ClaimMember:
        def __init__(self):
            self.guild = guild
            self.roles = []

        async def add_roles(self, given, reason=None):
            if given not in self.roles:
                self.roles.append(given)

        async def remove_roles(self, given, reason=None):
            self.roles = [held for held in self.roles if held != given]

    member_with_reward = ClaimMember()
    from utils.level_claims import claim_available
    paid = await claim_available(GUILD, penalty_user,
                                 member=member_with_reward)
    check("fixture: real Claim All delivered one role and one currency reward",
          paid["delivered_roles"] == 1 and paid["delivered_currency"] == 1
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, penalty_user)) == [(777,)]
          and [held.id for held in member_with_reward.roles] == [role_id])
    claims_before = rows("SELECT id,reward_level,track,reward_ref,payload_json,status "
                         "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                         "ORDER BY id", (GUILD, penalty_user))
    roles_before = [held.id for held in member_with_reward.roles]
    currency_ledger_before = rows(
        "SELECT currency,amount,source FROM transaction_ledger "
        "WHERE guild_id=? AND user_id=? AND currency!='xp' ORDER BY id",
        (GUILD, penalty_user))
    definitions_before = (
        rows("SELECT id,guild_id,level,role_id FROM leveling_rewards "
             "WHERE guild_id=? ORDER BY id", (GUILD,)),
        rows("SELECT id,guild_id,level,currency,amount FROM leveling_currency_rewards "
             "WHERE guild_id=? ORDER BY id", (GUILD,)),
    )
    penalty = await apply_spam_penalty(
        GUILD, penalty_user, 100, penalty_cap=1_000_000, now=2000.0)
    claims_after_penalty = rows(
        "SELECT id,reward_level,track,reward_ref,payload_json,status "
        "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY id", (GUILD, penalty_user))
    check("a penalty can demote while XP and Level stay consistent",
          penalty["new_level"] == 2 and xp_state(penalty_user)[1] == 2
          and xp_state(penalty_user)[0] == penalty["new_xp"], str(penalty))
    check("penalty leaves fulfilled claims, paid currency, roles and definitions unchanged",
          claims_after_penalty == claims_before
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, penalty_user)) == [(777,)]
          and [held.id for held in member_with_reward.roles] == roles_before
          and rows("SELECT currency,amount,source FROM transaction_ledger "
                   "WHERE guild_id=? AND user_id=? AND currency!='xp' ORDER BY id",
                   (GUILD, penalty_user)) == currency_ledger_before
          and definitions_before == (
              rows("SELECT id,guild_id,level,role_id FROM leveling_rewards "
                   "WHERE guild_id=? ORDER BY id", (GUILD,)),
              rows("SELECT id,guild_id,level,currency,amount "
                   "FROM leveling_currency_rewards WHERE guild_id=? ORDER BY id",
                   (GUILD,))),
          f"claims={claims_after_penalty}, wallet={rows('SELECT balance FROM economy WHERE guild_id=? AND user_id=?', (GUILD, penalty_user))}")

    from utils.reward_engine import give_reward
    before_reearn = xp_state(penalty_user)[0]
    recross = await give_reward(
        None, GUILD, penalty_user, "xp", amount=start_xp - before_reearn,
        reason="re-earn after penalty", source="test-reearn")
    retry = await claim_available(GUILD, penalty_user,
                                  member=member_with_reward)
    claims_after_reearn = rows(
        "SELECT id,reward_level,track,reward_ref,payload_json,status "
        "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY id", (GUILD, penalty_user))
    check("re-earning a crossed Level does not create a duplicate or pay again",
          recross.get("success") is True and recross.get("leveled_up") is True
          and retry["owned"] == 0 and claims_after_reearn == claims_before
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, penalty_user)) == [(777,)]
          and [held.id for held in member_with_reward.roles] == roles_before,
          f"grant={recross}, retry={retry}, claims={claims_after_reearn}")
    followup = await apply_spam_penalty(
        GUILD, penalty_user, 1000, penalty_cap=1_000_000, now=2001.0)
    check("a subsequent penalty still derives Level from XP",
          followup["new_level"] == xp_progress(followup["new_xp"])[0])

    # Replace wall clock only for the active cog's in-memory tracker/warning
    # window. DB penalty and shared XP paths remain real.
    clock = [1000.0]
    leveling_module.time = SimpleNamespace(time=lambda: clock[0])
    user = USER + 20
    initial_xp = 10_000
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, user, initial_xp, xp_progress(initial_xp)[0]))
    set_spam_config()
    cog = make_cog()
    seen = []

    async def send_at(when, content, target=user):
        clock[0] = float(when)
        msg = make_message(target, content)
        seen.append(msg)
        await Leveling.on_activity_message(cog, msg, 3)
        return msg

    # Five identical messages is one repeated-content incident. Messages two
    # through four are inside the ordinary XP cooldown, and message five opens
    # the incident: one penalty, one warning, then no repeated deductions.
    first_incident = [await send_at(1000 + 2 * i, "same short message")
                      for i in range(5)]
    after_first = xp_state(user)[0]
    first_deduction = (initial_xp + 5) // 1000
    check("five identical messages trigger one incident penalty",
          after_first == initial_xp + 5 - first_deduction,
          f"xp={after_first}, expected={initial_xp + 5 - first_deduction}")
    check("production warning constant retains the approved copy byte-for-byte",
          SPAM_WARNING_TEXT == APPROVED_SPAM_WARNING_TEXT,
          repr(SPAM_WARNING_TEXT))
    check("first incident replies to the triggering message with approved copy and actual deduction",
          len(warning_embeds(first_incident)) == 1
          and first_incident[-1].reply.await_count == 1
          and all(message.reply.await_count == 0 for message in first_incident[:-1])
          and warning_embeds(first_incident)[0].description == APPROVED_SPAM_WARNING_TEXT
          and warning_embeds(first_incident)[0].footer.text == f"-{first_deduction} XP")
    check("spam warning is reply-only; it does not send a standalone channel message",
          all(message.channel.send.await_count == 0 for message in first_incident))

    await send_at(1009, "same short message")
    await send_at(1011, "same short message")
    check("repeated detections inside an incident do not drain XP or re-warn",
          xp_state(user)[0] == after_first
          and sum(m.reply.await_count for m in seen) == 1,
          f"xp={xp_state(user)[0]}, replies={sum(m.reply.await_count for m in seen)}")

    # A second member has an independent detector, incident and warning clock
    # while the first member's incident is still active.
    peer_user = USER + 27
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,20000,4)",
            (GUILD, peer_user))
    peer_messages = []
    for i in range(5):
        clock[0] = 1020 + i
        peer_message = make_message(peer_user, "peer repeated content")
        peer_messages.append(peer_message)
        await Leveling.on_activity_message(cog, peer_message, 3)
        if i < 4:
            check(f"a different member does not inherit the first member's spam samples ({i + 1}/4)",
                  sum(item.reply.await_count for item in peer_messages) == 0)
    peer_incident_ids = rows(
        "SELECT user_id,incident_id FROM leveling_spam_incidents "
        "WHERE guild_id=? AND user_id IN (?,?) ORDER BY user_id",
        (GUILD, user, peer_user))
    check("both members open distinct incidents and each can receive its own warning",
          len(peer_incident_ids) == 2
          and peer_incident_ids[0][1] != peer_incident_ids[1][1]
          and peer_messages[-1].reply.await_count == 1
          and sum(item.reply.await_count for item in peer_messages) == 1,
          f"incidents={peer_incident_ids}, replies={sum(item.reply.await_count for item in peer_messages)}")

    # After a full 20s quiet interval the old message history and active incident
    # have expired; the next five identical messages are a new incident and may
    # receive exactly one new penalty/warning.
    await send_at(1040, "different normal content")
    second_incident = [await send_at(1041 + i, "second repeated line")
                       for i in range(5)]
    after_second = xp_state(user)[0]
    check("a new post-quiet incident gets one new penalty",
          after_second < after_first
          and sum(m.reply.await_count for m in seen) == 2,
          f"xp={after_second}, replies={sum(m.reply.await_count for m in seen)}")
    check("warning is a reply to the incident message and remains rate-limited",
          len(warning_embeds(second_incident)) == 1)
    await send_at(1046, "second repeated line")
    check("ongoing second incident still has exactly one penalty/warning",
          sum(m.reply.await_count for m in seen) == 2)

    # Six unique short-interval messages are the independent burst signal.
    rapid_user = USER + 21
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, rapid_user, 30_000, xp_progress(30_000)[0]))
    set_spam_config()
    rapid_cog = make_cog()
    rapid_msgs = []
    for i in range(6):
        clock[0] = 2000 + i * 0.5
        msg = make_message(rapid_user, f"unique burst {i}")
        rapid_msgs.append(msg)
        await Leveling.on_activity_message(rapid_cog, msg, 3)
        if i < 5:
            check(f"burst signal waits through message {i + 1}",
                  sum(item.reply.await_count for item in rapid_msgs) == 0)
    check("six messages in three seconds trigger one capped incident",
          sum(item.reply.await_count for item in rapid_msgs) == 1)

    # The configurable sustained-frequency threshold remains independent of
    # repeated content and the short-burst signal.
    sustained_user = USER + 22
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, sustained_user, 40_000, xp_progress(40_000)[0]))
    set_spam_config()
    sustained_cog = make_cog()
    sustained_msgs = []
    for i in range(10):
        clock[0] = 3000 + i * 2.1
        msg = make_message(sustained_user, f"different {i}")
        sustained_msgs.append(msg)
        await Leveling.on_activity_message(sustained_cog, msg, 3)
    check("10 distinct messages within the 20s window trigger once",
          sum(item.reply.await_count for item in sustained_msgs) == 1)

    # Ordinary chatter is not spam; low balances round to zero without a bogus
    # warning, and disabling spam detection stops detection independently.
    normal_user = USER + 23
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, normal_user, 0, 0))
    set_spam_config()
    normal_cog = make_cog()
    normal_msgs = []
    for i in range(4):
        clock[0] = 4000 + i * 1.0
        msg = make_message(normal_user, f"ordinary thought {i}")
        normal_msgs.append(msg)
        await Leveling.on_activity_message(normal_cog, msg, 3)
    check("four distinct ordinary messages do not open an incident",
          xp_state(normal_user)[0] == 5
          and sum(item.reply.await_count for item in normal_msgs) == 0)

    low_user = USER + 24
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,50,0)",
            (GUILD, low_user))
    set_spam_config(message_xp=0)
    low_cog = make_cog()
    low_msgs = []
    for i in range(6):
        clock[0] = 5000 + i
        msg = make_message(low_user, "tiny balance repeat")
        low_msgs.append(msg)
        await Leveling.on_activity_message(low_cog, msg, 3)
    check("zero-rounded low XP penalty is floored, no debt, no '-0 XP' warning",
          xp_state(low_user) == (50, 0)
          and sum(item.reply.await_count for item in low_msgs) == 0)

    disabled_user = USER + 25
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, disabled_user, 20_000, xp_progress(20_000)[0]))
    set_spam_config(enabled=0)
    disabled_cog = make_cog()
    disabled_msgs = []
    before_disabled = xp_state(disabled_user)[0]
    for i in range(6):
        clock[0] = 6000 + i * 0.2
        msg = make_message(disabled_user, "same content")
        disabled_msgs.append(msg)
        await Leveling.on_activity_message(disabled_cog, msg, 3)
    check("spam detection OFF prevents incident penalties and warnings",
          xp_state(disabled_user)[0] == before_disabled + 5
          and sum(item.reply.await_count for item in disabled_msgs) == 0)

    # A Discord reply failure is best-effort and cannot undo the committed XP
    # deduction; only one failed reply is attempted for that incident.
    failed_user = USER + 26
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
            (GUILD, failed_user, 70_000, xp_progress(70_000)[0]))
    set_spam_config()
    failed_cog = make_cog()
    failed_msgs = []
    for i in range(5):
        clock[0] = 7000 + i * 1.0
        msg = make_message(failed_user, "reply failure repeated")
        if i == 4:
            msg.reply.side_effect = RuntimeError("missing send permission")
        failed_msgs.append(msg)
        await Leveling.on_activity_message(failed_cog, msg, 3)
    after_failure = xp_state(failed_user)[0]
    clock[0] = 7006
    await Leveling.on_activity_message(
        failed_cog, make_message(failed_user, "reply failure repeated"), 3)
    check("failed warning does not rollback penalty or repeat within incident",
          after_failure < 70_005 and xp_state(failed_user)[0] == after_failure
          and sum(item.reply.await_count for item in failed_msgs) == 1)

    # Bot messages must be rejected before either ordinary XP or spam tracking.
    bot_user = USER + 28
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,20000,4)",
            (GUILD, bot_user))
    set_spam_config(cooldown=0)
    bot_cog = make_cog()
    bot_messages = []
    for i in range(5):
        clock[0] = 8000 + i
        bot_message = make_message(bot_user, "bot repeated content")
        bot_message.author.bot = True
        bot_messages.append(bot_message)
        await Leveling.on_activity_message(bot_cog, bot_message, 3)
    check("bot messages earn no XP and cannot open or warn on a spam incident",
          xp_state(bot_user) == (20_000, 4)
          and all(message.reply.await_count == 0 for message in bot_messages)
          and all(message.channel.send.await_count == 0 for message in bot_messages)
          and rows("SELECT COUNT(*) FROM leveling_spam_incidents "
                   "WHERE guild_id=? AND user_id=?", (GUILD, bot_user)) == [(0,)])

    # Finally exercise the actual listener with no stored config row: fallback
    # defaults must leave spam detection ON, not merely expose a correct dict.
    execute("DELETE FROM leveling_config WHERE guild_id=?", (GUILD,))
    default_user = USER + 29
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,10000,6)",
            (GUILD, default_user))
    effective_default = await get_leveling_config(GUILD)
    default_cog = make_cog()
    default_messages = []
    for i in range(5):
        clock[0] = 9000 + 2 * i
        default_message = make_message(default_user, "default-config repeated")
        default_messages.append(default_message)
        await Leveling.on_activity_message(default_cog, default_message, 3)
    check("unconfigured runtime detects spam and emits one incident warning by default",
          effective_default["spam_detection_enabled"] == 1
          and xp_state(default_user)[0] == 9_995
          and sum(message.reply.await_count for message in default_messages) == 1
          and default_messages[-1].reply.await_count == 1)

    print("ALL INCIDENT-BASED SPAM CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
