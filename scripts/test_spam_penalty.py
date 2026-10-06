"""Anti-spam penalty: real deduction, floor at zero, consistent level (D1 = B).

Locks down the semantics decided for the penalty:

* a spam message deducts XP from the member — it is not just a "zero gain";
* XP is floored at zero and **never** goes negative: a penalty is not a debt,
  and it never has to be worked off;
* the stored `level` is recomputed from the resulting XP in the same
  transaction, so `levels.xp` and `levels.level` can never disagree (the old
  write floored XP but left `level` stale, and the *next* legitimate XP grant
  would then silently demote the member);
* a demotion caused by a penalty is legitimate, but it is **not a crossing**:
  it creates no entitlement, revokes no fulfilled claim and removes no role —
  and re-earning the level afterwards cannot pay a reward twice;
* the existing `spam_threshold` / `spam_xp_penalty` / `spam_window_seconds`
  values and the detection ordering are unchanged;
* the warning is the approved embed, sent as a REPLY to the offending message,
  at most once per member per spam window (the penalty still lands on every
  spamming message), it reports the XP that was actually deducted, and a
  failure to send it can never roll the penalty back.

Run from the scripts directory:
    python test_spam_penalty.py
"""
import asyncio
import math
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from phase1_support import DB_PATH, GUILD, USER, execute, reset_database, rows


def xp_to(level: int) -> int:
    return sum(math.floor(100 * l ** 1.5) for l in range(1, level + 1))


def level_state(user=USER):
    found = rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else (0, 0)


def claims():
    return rows("SELECT reward_level, track, status FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=? ORDER BY reward_level, track",
                (GUILD, USER))


def ledger_rows():
    return rows("SELECT COUNT(*) FROM transaction_ledger")[0][0]


def person(user=USER):
    who = SimpleNamespace(id=user, bot=False, mention=f"<@{user}>")
    who.roles = []
    return who


def warnings_sent(channel):
    """Only the cat warnings — the same channel also carries level-up embeds."""
    return [call.args[0] for call in channel.send.await_args_list
            if call.args and isinstance(call.args[0], str) and "Meow" in call.args[0]]


def message_for(user=USER, channel=None, reply=None):
    return SimpleNamespace(
        author=person(user),
        guild=SimpleNamespace(id=GUILD),
        channel=channel or SimpleNamespace(send=AsyncMock()),
        reply=reply or AsyncMock(),
        content="spam spam spam",
    )


def already_spamming(leveling, user=USER):
    """Pre-fill the tracker so the very next message is the penalised one."""
    import time as _time
    leveling._spam_tracker[(GUILD, user)] = [_time.time() - 2, _time.time() - 1]
    leveling._spam_warn_times.clear()


def warning_embeds(message):
    """The embeds the anti-spam warning replied with (kwargs form)."""
    return [call.kwargs.get("embed") for call in message.reply.await_args_list
            if call.kwargs.get("embed") is not None]


def cog():
    from cogs.leveling import Leveling
    obj = Leveling.__new__(Leveling)
    obj.bot = SimpleNamespace(get_guild=lambda gid: None)
    obj._xp_cooldowns = {}
    obj._spam_tracker = {}
    obj._spam_warn_times = {}
    return obj


async def main():
    import cogs.leveling as leveling_mod
    from utils.xp_calculator import (
        LEVELING_CONFIG_DEFAULTS, apply_spam_penalty, xp_progress,
    )
    from utils.level_claims import ensure_tables
    from cogs.leveling import Leveling, SPAM_WARNING_TEXT

    await reset_database()
    await ensure_tables()

    checks = []

    def check(name, ok, extra=""):
        checks.append((name, ok, extra))
        print(f"  {'PASS' if ok else 'FAIL'} {name}"
              + (f"  [{extra}]" if extra and not ok else ""))

    # ── 1. the numbers the user asked to keep unchanged ────────────────────
    print("== 1. detection settings are untouched ==")
    check("spam_threshold is still 3",
          LEVELING_CONFIG_DEFAULTS["spam_threshold"] == 3)
    check("spam_xp_penalty is still 10",
          LEVELING_CONFIG_DEFAULTS["spam_xp_penalty"] == 10)
    check("spam_window_seconds is still 10",
          LEVELING_CONFIG_DEFAULTS["spam_window_seconds"] == 10)
    check("spam detection is still enabled by default",
          LEVELING_CONFIG_DEFAULTS["spam_detection_enabled"] == 1)

    # ── 2. the deduction, and the level that follows it ────────────────────
    print("== 2. a penalty deducts XP and recomputes the level ==")
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    at_level2 = xp_to(2) + 3                       # 3 XP into Level 2
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, USER, at_level2, 2))
    outcome = await apply_spam_penalty(GUILD, USER, 10)
    xp, level = level_state()
    check("the penalty subtracted from current XP",
          xp == at_level2 - 10 and outcome["applied"] is True,
          f"xp={xp} outcome={outcome}")
    check("the stored level matches the XP curve after the penalty",
          level == xp_progress(xp)[0] == 1,
          f"stored={level} derived={xp_progress(xp)[0]}")

    print("== 3. XP is floored at zero and never becomes a debt ==")
    execute("UPDATE levels SET xp=4, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    first = await apply_spam_penalty(GUILD, USER, 10)
    check("a penalty larger than the balance floors XP at 0",
          level_state() == (0, 0) and first["applied"] is True and
          first["new_xp"] == 0, f"state={level_state()} outcome={first}")
    seen = []
    for _ in range(5):
        outcome = await apply_spam_penalty(GUILD, USER, 10)
        seen.append(outcome["new_xp"])
    check("repeated penalties at zero stay at zero (no debt)",
          seen == [0] * 5 and level_state() == (0, 0), f"sequence={seen}")
    check("no negative XP anywhere in the sequence",
          all(value >= 0 for value in seen) and level_state()[0] >= 0)

    print("== 4. consistency across fixtures ==")
    consistent = True
    for fixture_xp in (0, 3, 99, 100, 101, 384, 385, 2819, 2820, 100000):
        execute("UPDATE levels SET xp=?, level=? WHERE guild_id=? AND user_id=?",
                (fixture_xp, xp_progress(fixture_xp)[0], GUILD, USER))
        await apply_spam_penalty(GUILD, USER, 10)
        stored_xp, stored_level = level_state()
        if stored_xp != max(0, fixture_xp - 10) or \
                stored_level != xp_progress(stored_xp)[0]:
            consistent = False
    check("xp and level agree after every penalty, at every boundary",
          consistent)

    # ── 5. a penalty is not a crossing ─────────────────────────────────────
    print("== 5. the demotion creates no entitlement and revokes no claim ==")
    execute("DELETE FROM level_reward_claims")
    execute("DELETE FROM leveling_rewards")
    execute("INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?,?,?)",
            (GUILD, 2, 2222))
    execute("INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?,?,?)",
            (GUILD, 5, 5555))
    xp_at_5 = xp_to(5)
    execute("UPDATE levels SET xp=?, level=? WHERE guild_id=? AND user_id=?",
            (xp_at_5, 5, GUILD, USER))
    from utils.level_claims import SOURCE_SETXP, record_crossing
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await record_crossing(db, GUILD, USER, 0, xp_at_5, {}, source="test")
        await db.commit()
    execute("UPDATE level_reward_claims SET status='fulfilled'")
    before_claims = claims()
    before_ledger = ledger_rows()
    check("fixture: both crossed levels hold a fulfilled role claim",
          before_claims == [(2, "role", "fulfilled"), (5, "role", "fulfilled")],
          str(before_claims))

    demoting = await apply_spam_penalty(GUILD, USER, xp_to(5))
    xp, level = level_state()
    check("the penalty demoted the member (legitimate)",
          xp == 0 and level == 0 and demoting["old_level"] == 5
          and demoting["new_level"] == 0, f"state={level_state()}")
    check("no entitlement row was created by the demotion",
          claims() == before_claims, str(claims()))
    check("the fulfilled claim was not revoked or re-opened",
          all(row[2] == "fulfilled" for row in claims()), str(claims()))
    check("the XP transaction ledger was not written by the penalty",
          ledger_rows() == before_ledger, f"{before_ledger} -> {ledger_rows()}")

    print("== 6. re-earning a demoted level cannot pay twice ==")
    from utils.reward_engine import give_reward
    bot = SimpleNamespace(get_guild=lambda gid: None)
    await give_reward(bot, GUILD, USER, "xp", amount=xp_at_5,
                      reason="re-earn after the penalty", source="test")
    after = claims()
    check("re-crossing the level adds no second claim row",
          len(after) == len(before_claims), str(after))
    check("the already-fulfilled claim is still fulfilled exactly once",
          after == before_claims, str(after))

    # ── 7. the approved warning: an embed REPLY to the offending message ────
    print("== 7. the anti-spam warning is the approved embed reply ==")
    APPROVED_TEXT = (
        "> مع كل احتراماتي لا تسبام <:brick:1556981905218478162>\n"
        "> -# من لفلك نيهاهاها   XP تم خصم (≖⩊≖)")
    check("the shipped text is the approved text, byte for byte",
          SPAM_WARNING_TEXT == APPROVED_TEXT, repr(SPAM_WARNING_TEXT))
    check("the custom emoji syntax is preserved literally",
          "<:brick:1556981905218478162>" in SPAM_WARNING_TEXT)
    check("both approved lines are quoted blockquote lines",
          SPAM_WARNING_TEXT.startswith("> ")
          and "\n> -# " in SPAM_WARNING_TEXT)
    check("the old standalone cat message is gone",
          not hasattr(leveling_mod, "SPAM_WARNING_MESSAGE")
          or "Meow" not in getattr(leveling_mod, "SPAM_WARNING_MESSAGE", ""))

    execute("DELETE FROM leveling_config")
    leveling = cog()
    channel = SimpleNamespace(send=AsyncMock())
    execute("DELETE FROM levels")
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, USER, xp_to(5) + 50, 5))

    message = message_for(channel=channel)
    for _ in range(2):                       # threshold 3: not spam yet
        await Leveling.on_activity_message(leveling, message_for(channel=channel), 3)
    check("messages below the threshold reply with nothing",
          message.reply.await_count == 0 and warning_embeds(message) == [],
          str(message.reply.await_args_list))

    message = message_for(channel=channel)
    await Leveling.on_activity_message(leveling, message, 3)
    embeds = warning_embeds(message)
    check("the spamming message gets exactly one embed reply",
          len(embeds) == 1 and message.reply.await_count == 1, str(embeds))
    check("the warning is a reply to the offending message, not a channel send",
          message.reply.await_count == 1
          and not any("brick" in str(call) for call in channel.send.await_args_list),
          f"channel_sends={channel.send.await_args_list}")
    check("the embed carries the approved description",
          embeds and embeds[0].description == APPROVED_TEXT,
          repr(embeds[0].description if embeds else None))
    check("the embed reports the XP that was actually deducted",
          embeds and embeds[0].footer is not None
          and embeds[0].footer.text == "-10 XP",
          repr(embeds[0].footer.text if embeds and embeds[0].footer else None))

    print("== 8. the warning rate-limit is one per spam window ==")
    before_xp = level_state()[0]
    burst = message_for(channel=channel)
    for _ in range(4):                       # same burst: penalised, not re-warned
        await Leveling.on_activity_message(leveling, burst, 3)
    check("every further spamming message still pays the penalty",
          level_state()[0] == before_xp - 4 * 10, str(level_state()))
    check("but the member is warned only once in the window",
          burst.reply.await_count == 0, str(burst.reply.await_count))

    import cogs.leveling as leveling_mod
    real_time = leveling_mod.time
    clock = {"t": 1000.0}
    leveling_mod.time = SimpleNamespace(time=lambda: clock["t"])
    try:
        leveling._spam_warn_times.clear()
        first = leveling._spam_warning_due(GUILD, USER, 10)
        same_window = leveling._spam_warning_due(GUILD, USER, 10)
        clock["t"] = 1005.0
        still_same = leveling._spam_warning_due(GUILD, USER, 10)
        clock["t"] = 1011.0
        next_window = leveling._spam_warning_due(GUILD, USER, 10)
        check("the first spam in a fresh window warns",
              first is True, str(first))
        check("further spam inside the same window stays quiet",
              same_window is False and still_same is False,
              f"{same_window}/{still_same}")
        check("the next window warns again", next_window is True)
        check("a different member has their own warning clock",
              leveling._spam_warning_due(GUILD, USER + 1, 10) is True)
    finally:
        leveling_mod.time = real_time

    print("== 9. the warning reports the real amount (XP floor) ==")
    leveling = cog()
    execute("UPDATE levels SET xp=4, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    already_spamming(leveling)               # the next message is the penalised one
    message = message_for(channel=channel)
    await Leveling.on_activity_message(leveling, message, 3)
    embeds = warning_embeds(message)
    check("a member with less XP than the penalty loses only what they had",
          level_state() == (0, 0), str(level_state()))
    check("the footer shows the deducted amount, not the configured one",
          embeds and embeds[0].footer.text == "-4 XP",
          repr(embeds[0].footer.text if embeds and embeds[0].footer else None))

    print("== 10. nothing to deduct means no warning (and never a debt) ==")
    leveling = cog()
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    already_spamming(leveling)
    message = message_for(channel=channel)
    await Leveling.on_activity_message(leveling, message, 3)
    check("a member at 0 XP is penalised in code but not warned about XP",
          level_state() == (0, 0) and message.reply.await_count == 0,
          f"state={level_state()} replies={message.reply.await_count}")

    print("== 11. a warning that cannot be sent never breaks the penalty ==")
    leveling = cog()
    execute("UPDATE levels SET xp=500, level=1 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    already_spamming(leveling)
    failing = AsyncMock(side_effect=RuntimeError("missing permissions"))
    message = message_for(channel=channel, reply=failing)
    await Leveling.on_activity_message(leveling, message, 3)     # must not raise
    check("a failed reply still applies the penalty",
          level_state()[0] == 490, str(level_state()))
    check("the failure was attempted once and swallowed",
          failing.await_count == 1, str(failing.await_count))

    print("== 12. penalty 0 / detection off / non-spam traffic ==")
    execute("INSERT INTO leveling_config (guild_id, spam_xp_penalty) VALUES (?,?)",
            (GUILD, 0))
    leveling = cog()
    message = message_for(channel=channel)
    for _ in range(3):
        await Leveling.on_activity_message(leveling, message, 3)
    # 490 + the first message's grant (1 XP per word raised to the
    # `xp_min_per_message` floor of 5); message 2 is inside the cooldown and
    # message 3 reaches the spam branch with the penalty set to 0, which writes
    # nothing and warns nobody.
    check("penalty 0 writes nothing and warns nobody",
          level_state()[0] == 495 and message.reply.await_count == 0,
          f"xp={level_state()} replies={message.reply.await_count}")

    execute("UPDATE leveling_config SET spam_detection_enabled=0 WHERE guild_id=?",
            (GUILD,))
    leveling = cog()
    message = message_for(channel=channel)
    for _ in range(3):
        await Leveling.on_activity_message(leveling, message, 3)
    check("detection off means no penalty and no warning",
          level_state()[0] == 500 and message.reply.await_count == 0,
          f"xp={level_state()} replies={message.reply.await_count}")

    execute("DELETE FROM leveling_config")
    leveling = cog()
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    message = message_for(channel=channel)
    await Leveling.on_activity_message(leveling, message, 3)
    check("a normal message grants XP and warns nobody",
          level_state()[0] == 5 and message.reply.await_count == 0,
          f"xp={level_state()} replies={message.reply.await_count}")
    bot_message = message_for(channel=channel)
    bot_message.author.bot = True
    await Leveling.on_activity_message(leveling, bot_message, 3)
    check("bot messages are ignored (no XP, no warning)",
          level_state()[0] == 5 and bot_message.reply.await_count == 0,
          f"xp={level_state()}")

    failed = [c for c in checks if not c[1]]
    print(f"\nSPAM PENALTY: {len(checks) - len(failed)} passed, {len(failed)} failed")
    for name, _, extra in failed:
        print("  FAILED:", name, extra)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
