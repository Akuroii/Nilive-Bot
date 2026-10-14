#!/usr/bin/env python3
"""
Missions v2 — verification suite.

Runs the REAL engine, cog display code, and dashboard API (Flask test
client) against a scratch SQLite DB created by the project's own
init_db(), so schema drift between this test and the code is
impossible. Rewards are counted via a patched give_reward so no
economy side effects run (the grant path itself is unchanged v1 code
already covered by the wallet/economy suites).

Covers the agreed v2 validation list:
   1.  Existing missions (no channel) still work / unrestricted.
   2.  v1 DB migration: channel_id added, legacy rows intact,
       idempotent across repeated ensure_tables() runs.
   3.  Restricted mission only counts its configured channel.
   4.  Thread behavior: a thread counts as its parent channel
       (_effective_channel_id), incl. orphaned threads.
   5/6. Multiple Daily/Weekly missions → (completed / total) headings.
   7.  Progress bar reflects the real percentage.
   8.  Progress reaches 100% exactly once (idempotent re-completion).
   9.  Reward granted automatically exactly once.
  10.  Completed missions render "⨽ `reward claimed` <check emoji>
       <configured currency amount>" — resolved from the Economy currency
       config.
  11/12. Daily / weekly reset countdown math (UTC).
  13.  Refresh edits the existing message, never sends a new one.
  14.  Deleted / disabled missions stop counting; honest not-found.
  15.  Recent Completions formatting ('2026-09-14 · 12:51').
  16.  API malformed input → 400/404, never a 500.
  17.  Dashboard channel selection round-trips into the definition.
  18/19. Migration survival + idempotency (see 2).
  20.  daily_completions chains from daily completions exactly once.
  Plus: validation matrix for create_definition, guild isolation,
  CSRF and permission gating on the API.

Run:
  python scripts/test_missions_v2.py
(needs the bot's own deps — discord.py, aiosqlite, flask — same as
requirements.txt; no network, no Discord token: everything runs
against a scratch SQLite DB and a patched reward grant.)
"""
import os
import re
import sys
import asyncio
import tempfile
import sqlite3
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="missions_v2_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "missions.db")
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import aiosqlite  # noqa: E402
from database import DB_PATH, init_db  # noqa: E402
import utils.mission_engine as me  # noqa: E402
import utils.reward_engine  # noqa: E402
from utils.emoji import CHECK_EMOJI  # noqa: E402

GUILD = 1100

USER = 2202
CH_ALLOWED = 111111
CH_OTHER = 222222

_passed = 0
_failed = 0
_failures = []


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[92mPASS\033[0m  {label}")
    else:
        _failed += 1
        _failures.append(label)
        print(f"  \033[91mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# ── Reward counting (patched — grant path itself is unchanged v1 code) ──
reward_calls = []


async def fake_give_reward(bot, guild_id, user_id, reward_type, **kwargs):
    reward_calls.append((guild_id, user_id, reward_type, kwargs.get("amount")))
    return {"success": True, "reward_type": reward_type}


utils.reward_engine.give_reward = fake_give_reward


async def db_exec(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, args)
        await db.commit()


async def db_query(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, args)
        return await cursor.fetchall()


async def progress_row(mission_id, period_key=None, user_id=USER, guild_id=GUILD):
    rows = await db_query(
        "SELECT progress, completed FROM mission_progress "
        "WHERE guild_id=? AND user_id=? AND mission_id=? AND period_key=?",
        (guild_id, user_id, mission_id,
         period_key or me.get_period_key((await db_query(
             "SELECT period FROM missions_definitions WHERE id=?",
             (mission_id,)))[0][0])))
    return rows[0] if rows else None


# ═══════════════════════════════════════════════════════════════════
async def engine_tests():
    section("1/2. Migration — v1 schema upgraded, idempotent, data intact")

    # Rebuild the definitions table in its exact v1 shape (no channel_id),
    # with a legacy definition + legacy progress row, then let v2's
    # ensure_tables() migrate it — the real upgrade path a live DB takes.
    legacy_def_id = None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DROP TABLE missions_definitions")
        await db.execute("""
            CREATE TABLE missions_definitions (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id               INTEGER NOT NULL,
                name                   TEXT NOT NULL,
                description            TEXT,
                type                   TEXT NOT NULL,
                target                 INTEGER NOT NULL,
                period                 TEXT NOT NULL DEFAULT 'daily',
                reward_type            TEXT NOT NULL,
                reward_value           TEXT NOT NULL,
                reward_duration_hours  INTEGER,
                enabled                INTEGER DEFAULT 1,
                created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor = await db.execute("""
            INSERT INTO missions_definitions
                (guild_id, name, description, type, target, period,
                 reward_type, reward_value)
            VALUES (?, 'Legacy words', NULL, 'words', 100, 'daily',
                    'coins', '250')
        """, (GUILD,))
        legacy_def_id = cursor.lastrowid
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        await db.execute("""
            INSERT INTO mission_progress
                (guild_id, user_id, mission_id, period_key, progress, completed)
            VALUES (?, ?, ?, ?, 40, 1)
        """, (GUILD, USER, legacy_def_id, yesterday))
        await db.commit()

    await me.ensure_tables()
    await me.ensure_tables()   # idempotency — second run must be a no-op
    await me.ensure_tables()   # and a third, for good measure

    cols = [c[1] for c in await db_query("PRAGMA table_info(missions_definitions)")]
    check("channel_id column added by migration", "channel_id" in cols, str(cols))
    legacy = await db_query(
        "SELECT name, type, target, channel_id, enabled FROM missions_definitions "
        "WHERE id=?", (legacy_def_id,))
    check("legacy definition row survived",
          legacy and legacy[0][0] == "Legacy words" and legacy[0][4] == 1, str(legacy))
    check("legacy definition migrated to channel_id NULL (unrestricted)",
          legacy and legacy[0][3] is None, str(legacy))
    old = await db_query(
        "SELECT progress, completed FROM mission_progress WHERE mission_id=?",
        (legacy_def_id,))
    check("legacy progress row survived untouched",
          old and old[0][0] == 40 and old[0][1] == 1, str(old))

    # ═════════════════════════════════════════════════════════════
    section("3. Channel restriction — restricted counts only its channel")

    words_any = await me.create_definition(
        GUILD, name="Words anywhere", mtype="words", target=1000,
        reward_type="coins", reward_value="100")
    words_chan = await me.create_definition(
        GUILD, name="Words in allowed", mtype="words", target=10,
        reward_type="coins", reward_value="100", channel_id=CH_ALLOWED)

    check("definition stored with channel",
          (await db_query("SELECT channel_id FROM missions_definitions WHERE id=?",
                          (words_chan,)))[0][0] == CH_ALLOWED)
    check("definition stored without channel is NULL",
          (await db_query("SELECT channel_id FROM missions_definitions WHERE id=?",
                          (words_any,)))[0][0] is None)

    await me.record_activity(None, GUILD, USER, "words", 3, channel_id=CH_ALLOWED)
    await me.record_activity(None, GUILD, USER, "words", 4, channel_id=CH_OTHER)

    row_any = await progress_row(words_any)
    row_chan = await progress_row(words_chan)
    check("unrestricted mission counts every channel",
          row_any and row_any[0] == 7, str(row_any))
    check("restricted mission only counts its own channel",
          row_chan and row_chan[0] == 3, str(row_chan))

    # A thread whose parent is the restricted channel counts; a thread
    # under a different parent (and an orphan with no parent) doesn't.
    await me.record_activity(None, GUILD, USER, "words", 2,
                             channel_id=CH_ALLOWED)  # thread parent == allowed
    await me.record_activity(None, GUILD, USER, "words", 5,
                             channel_id=None)        # orphan thread / no channel
    row_chan = await progress_row(words_chan)
    check("thread of the restricted channel counts, orphaned/unknown does not",
          row_chan and row_chan[0] == 5, str(row_chan))

    # ═════════════════════════════════════════════════════════════
    section("Engine — batch counting, completion, reward idempotency")

    msgs = await me.create_definition(
        GUILD, name="Chatterbox", mtype="messages", target=3,
        reward_type="coins", reward_value="50")
    # One chat event carries messages + words together:
    # words so far: 3+4 (any channel) + 2 (thread of allowed) + 5
    # (orphan/unknown — unrestricted still counts it) = 14.
    await me.record_activities(None, GUILD, USER,
                               {"messages": 1, "words": 6}, channel_id=CH_OTHER)
    check("messages+words batched in one event both count",
          (await progress_row(msgs))[0] == 1
          and (await progress_row(words_any))[0] == 20)
    # words=0 is filtered, messages still counts (v1 behavior):
    await me.record_activities(None, GUILD, USER, {"messages": 1, "words": 0})
    check("zero word count doesn't create a phantom words tick",
          (await progress_row(msgs))[0] == 2)

    reward_calls.clear()
    await me.record_activity(None, GUILD, USER, "messages", 1)  # 3/3 → complete
    check("mission completes when target reached",
          (await progress_row(msgs))[1] == 1)
    check("reward granted automatically exactly once",
          len([c for c in reward_calls
               if c[0] == GUILD and c[2] == "coins"]) == 1, str(reward_calls))

    # Past-the-target overflow and repeats: no second completion/reward.
    await me.record_activity(None, GUILD, USER, "messages", 5)
    await me.record_activity(None, GUILD, USER, "messages", 1)
    row = await progress_row(msgs)
    check("no re-completion after target reached",
          row[1] == 1 and len([c for c in reward_calls if c[2] == "coins"]) == 1,
          str(row))

    # A single event that overshoots the target completes once.
    overshoot = await me.create_definition(
        GUILD, name="Overshoot", mtype="words", target=5,
        reward_type="xp", reward_value="10")
    reward_calls.clear()
    await me.record_activity(None, GUILD, USER, "words", 50)
    row = await progress_row(overshoot)
    check("overshooting event completes exactly once with one reward",
          row[1] == 1 and len(reward_calls) == 1, f"{row} {reward_calls}")

    # ═════════════════════════════════════════════════════════════
    section("20. daily_completions — weekly 'Complete N daily missions'")

    # Two daily missions that one event completes together, plus the
    # weekly meta-mission they should feed.
    daily_a = await me.create_definition(
        GUILD, name="Daily A", mtype="voice_minutes", target=1,
        reward_type="coins", reward_value="5")
    daily_b = await me.create_definition(
        GUILD, name="Daily B", mtype="words", target=1,
        reward_type="coins", reward_value="5")
    weekly_meta = await me.create_definition(
        GUILD, name="Complete 2 daily missions", mtype="daily_completions",
        target=2, period="weekly", reward_type="diamonds", reward_value="1",
        channel_id=CH_ALLOWED)  # channel must be ignored for this type

    check("daily_completions definition drops the channel (not channel-bound)",
          (await db_query("SELECT channel_id FROM missions_definitions WHERE id=?",
                          (weekly_meta,)))[0][0] is None)

    reward_calls.clear()
    # Voice tick (in a channel neither mission restricts) completes
    # daily A; the 1-word message completes daily B — together they
    # push the weekly meta to 2/2 in the same event chain.
    await me.record_activities(None, GUILD, USER,
                               {"voice_minutes": 1, "words": 1},
                               channel_id=CH_OTHER)
    meta_row = await progress_row(weekly_meta)
    check("two daily completions chain into the weekly meta mission",
          meta_row and meta_row[0] == 2 and meta_row[1] == 1, str(meta_row))
    check("meta mission reward granted once (diamonds)",
          len([c for c in reward_calls if c[2] == "diamonds"]) == 1,
          str(reward_calls))
    # Chained completions don't double-count the original daily missions.
    check("chain does not re-complete the source daily missions",
          (await progress_row(daily_a))[0] == 1
          and (await progress_row(daily_b))[0] == 1)

    # ═════════════════════════════════════════════════════════════
    section("14. Disabled / deleted missions behave correctly")

    disabled = await me.create_definition(
        GUILD, name="Disabled soon", mtype="words", target=5,
        reward_type="coins", reward_value="5")
    check("set_definition_enabled returns True for existing mission",
          await me.set_definition_enabled(GUILD, disabled, False))
    await me.record_activity(None, GUILD, USER, "words", 3)
    check("disabled mission records no progress",
          await progress_row(disabled) is None)

    doomed = await me.create_definition(
        GUILD, name="Doomed", mtype="words", target=5,
        reward_type="coins", reward_value="5")
    check("delete_definition returns True for existing mission",
          await me.delete_definition(GUILD, doomed))
    check("delete_definition returns False for missing mission",
          not await me.delete_definition(GUILD, 999999))
    check("set_definition_enabled returns False for missing mission",
          not await me.set_definition_enabled(GUILD, 999999, True))

    # ═════════════════════════════════════════════════════════════
    section("Validation — create_definition rejects bad input cleanly")

    bad_inputs = [
        ("empty name", dict(name="  ", mtype="words", target=5,
                            reward_type="coins", reward_value="10")),
        ("bad type", dict(name="X", mtype="hugs", target=5,
                          reward_type="coins", reward_value="10")),
        ("bad period", dict(name="X", mtype="words", target=5, period="hourly",
                            reward_type="coins", reward_value="10")),
        ("bad reward_type", dict(name="X", mtype="words", target=5,
                                 reward_type="hugs", reward_value="10")),
        ("zero target", dict(name="X", mtype="words", target=0,
                             reward_type="coins", reward_value="10")),
        ("non-numeric target", dict(name="X", mtype="words", target="many",
                                    reward_type="coins", reward_value="10")),
        ("empty reward_value", dict(name="X", mtype="words", target=5,
                                    reward_type="coins", reward_value="")),
        ("non-numeric coins amount", dict(name="X", mtype="words", target=5,
                                          reward_type="coins", reward_value="rich")),
        ("non-numeric role id", dict(name="X", mtype="words", target=5,
                                     reward_type="role", reward_value="not-an-id")),
        ("bad channel", dict(name="X", mtype="words", target=5,
                             reward_type="coins", reward_value="10",
                             channel_id="everywhere")),
        ("bad duration", dict(name="X", mtype="words", target=5,
                              reward_type="temp_role", reward_value="123",
                              reward_duration_hours="soon")),
    ]
    for label, kwargs in bad_inputs:
        try:
            await me.create_definition(GUILD, **kwargs)
            check(f"rejects {label}", False, "no ValueError raised")
        except ValueError:
            check(f"rejects {label}", True)

    ok_id = await me.create_definition(
        GUILD, name="Temp role ok", mtype="words", target=5,
        reward_type="temp_role", reward_value="777",
        reward_duration_hours="2", channel_id=str(CH_ALLOWED))
    row = await db_query(
        "SELECT reward_duration_hours, channel_id FROM missions_definitions "
        "WHERE id=?", (ok_id,))
    check("accepts valid input, normalizes str channel/duration to ints",
          row[0][0] == 2 and row[0][1] == CH_ALLOWED, str(row))

    # ═════════════════════════════════════════════════════════════
    section("v2.1 — display-safety length caps (slash-command input)")

    check("name at the 100-char cap is accepted",
          await me.create_definition(
              GUILD, name="N" * 100, mtype="words", target=5,
              reward_type="coins", reward_value="10") > 0)
    try:
        await me.create_definition(
            GUILD, name="N" * 101, mtype="words", target=5,
            reward_type="coins", reward_value="10")
        check("name over 100 chars is rejected", False)
    except ValueError:
        check("name over 100 chars is rejected", True)
    try:
        await me.create_definition(
            GUILD, name="D", mtype="words", target=5,
            reward_type="coins", reward_value="10",
            description="D" * 201)
        check("description over 200 chars is rejected", False)
    except ValueError:
        check("description over 200 chars is rejected", True)
    check("description at the 200-char cap is accepted",
          await me.create_definition(
              GUILD, name="D2", mtype="words", target=5,
              reward_type="coins", reward_value="10",
              description="D" * 200) > 0)

    # ═════════════════════════════════════════════════════════════
    section("v2.1 — concurrent events on the same mission")

    # Two events racing across the completion boundary: the BEGIN
    # IMMEDIATE per-mission transaction must let only one of them
    # perform the completing write, so the reward fires exactly once.
    race_mission = await me.create_definition(
        GUILD, name="Race", mtype="words", target=10,
        reward_type="coins", reward_value="10")
    await me.record_activity(None, GUILD, USER, "words", 9)
    reward_calls.clear()
    await asyncio.gather(
        me.record_activity(None, GUILD, USER, "words", 1),
        me.record_activity(None, GUILD, USER, "words", 1),
    )
    race_row = await db_query(
        "SELECT progress, completed FROM mission_progress WHERE mission_id=?",
        (race_mission,))
    race_rewards = [c for c in reward_calls if c[2] == "coins"]
    # Final progress is 10, not 11: the loser of the race reads
    # completed=1 after the winner commits and is skipped by the
    # idempotency guard — post-completion increments are deliberately
    # not recorded (progress is capped once the reward fired).
    check("concurrent double-cross: completed once, progress capped at target",
          race_row[0][1] == 1 and race_row[0][0] == 10, str(race_row))
    check("concurrent double-cross: exactly one reward",
          len(race_rewards) == 1, str(reward_calls))

    # ═════════════════════════════════════════════════════════════
    section("11/12. Reset countdowns (UTC) and formatting")

    utc = timezone.utc
    from utils.timezone import CAIRO_TZ
    # Cairo daily reset is 00:00 Africa/Cairo. In September Cairo is UTC+3 (EEST),
    # so 1 hour before Cairo midnight is 20:00 UTC (which is 23:00 Cairo).
    check("daily countdown to Cairo midnight (1h before)",
          me.seconds_until_daily_reset(datetime(2026, 9, 14, 20, 0, tzinfo=utc)) == 3600)
    # Weekly is Saturday 00:00 Cairo. Use a known Cairo week: Saturday 2024-01-06 is start of week.
    # Wednesday 2024-01-10 12:00 UTC = Wednesday 2024-01-10 14:00 Cairo (UTC+2 in Jan) -> next Saturday is 2024-01-13 00:00 Cairo = 2024-01-12 22:00 UTC -> delta 2d 10h = 212400s
    # Simpler: test Friday 23:59 Cairo -> 1 minute until Saturday midnight, and Saturday 00:00 -> 7 days
    from datetime import datetime as _dt
    # Friday 2024-01-12 23:59 Cairo = Friday 2024-01-12 21:59 UTC (UTC+2)
    fri_cairo = _dt(2024, 1, 12, 21, 59, tzinfo=utc)
    check("weekly countdown 1 min before Cairo Saturday",
          0 < me.seconds_until_weekly_reset(fri_cairo) <= 90)
    sat_midnight_utc = _dt(2024, 1, 5, 22, 0, tzinfo=utc)  # Sat 2024-01-06 00:00 Cairo = Fri 2024-01-05 22:00 UTC (Jan UTC+2)
    check("weekly countdown from Saturday 00:00 Cairo is a full week",
          me.seconds_until_weekly_reset(sat_midnight_utc) == 604800)
    check("countdown formatting H:MMH '7:00H'",
          me.format_reset_countdown(7 * 3600) == "7:00H")
    check("countdown formatting '72:00H' for 3 days",
          me.format_reset_countdown(3 * 86400) == "72:00H")
    check("countdown formatting '1:00H'",
          me.format_reset_countdown(3600) == "1:00H")
    check("countdown formatting '0:45H'",
          me.format_reset_countdown(45 * 60) == "0:45H")
    check("countdown never renders as 0:00H",
          me.format_reset_countdown(30) == "0:01H"
          and me.format_reset_countdown(0) == "0:00H")
    # Also test example from spec: 1:12H = 1h 12m = 4320s
    check("spec example 1:12H",
          me.format_reset_countdown(1*3600+12*60) == "1:12H")

    # Period rollover: yesterday's key is not today's key.
    today_key = me.get_period_key("daily")
    check("daily period key changes across Cairo midnight (rollover basis)",
          today_key != yesterday, f"{today_key} vs {yesterday}")

    # get_user_progress reads the CURRENT period only: the legacy
    # mission was completed YESTERDAY (row above), so today must read
    # as not-completed while yesterday's row is preserved as history.
    prog = {m["id"]: m for m in await me.get_user_progress(GUILD, USER)}
    check("a mission completed yesterday reads as incomplete today",
          prog[legacy_def_id]["completed"] is False,
          str(prog[legacy_def_id]))
    yrow = await db_query(
        "SELECT progress, completed FROM mission_progress "
        "WHERE guild_id=? AND user_id=? AND mission_id=? AND period_key=?",
        (GUILD, USER, legacy_def_id, yesterday))
    check("yesterday's completed row is preserved as history",
          yrow and yrow[0][0] == 40 and yrow[0][1] == 1, str(yrow))

    # ═════════════════════════════════════════════════════════════
    # LAST section of engine_tests on purpose: it drops and recreates
    # mission_progress, wiping every progress row seeded above.
    section("v2.1 — rollback error path doesn't mask the real error")

    # Force a failure INSIDE the per-mission transaction (table dropped
    # after the definition loads) and confirm the exception that surfaces
    # is the original one, not 'cannot rollback - no transaction is
    # active' (the bare-ROLLBACK-in-except bug this pass fixed).
    await me.create_definition(
        GUILD, name="Err", mtype="words", target=5,
        reward_type="coins", reward_value="10")
    await db_exec("DROP TABLE mission_progress")
    surfaced = None
    try:
        await me.record_activity(None, GUILD, USER, "words", 1)
    except Exception as e:
        surfaced = e
    await me.ensure_tables()  # restore the (empty) table for later suites
    check("mid-transaction failure surfaces the original error",
          surfaced is not None and "no such table" in str(surfaced),
          repr(surfaced))
    check("failure is not masked by a rollback error",
          surfaced is not None and "rollback" not in str(surfaced).lower(),
          repr(surfaced))


# ═══════════════════════════════════════════════════════════════════
def cog_tests():
    section("Refresh button — custom emoji (locked by Dark)")
    import discord
    from cogs import missions as cog

    check("REFRESH_EMOJI is the agreed custom server emoji",
          cog.REFRESH_EMOJI == "<:imagePhotoroom17:1549206183498481714>",
          cog.REFRESH_EMOJI)
    pe = discord.PartialEmoji.from_str(cog.REFRESH_EMOJI)
    check("custom emoji parses to a PartialEmoji (valid Discord format)",
          pe.id == 1549206183498481714 and pe.name == "imagePhotoroom17"
          and pe.animated is False, f"{pe!r}")

    # The decorated button must actually carry the custom emoji + the
    # agreed label + secondary style (this is the real assertion that
    # the constant flows into the rendered component).
    class _MiniBot:
        pass
    v = cog.MissionsView(_MiniBot(), 1, 2)
    b = v.refresh_button
    check("button label is 'ʀᴇꜰʀᴇꜱʜ'", b.label == "ʀᴇꜰʀᴇꜱʜ", repr(b.label))
    # Gray/neutral appearance, per the locked UI spec — not blurple.
    check("button style is Secondary/neutral gray",
          b.style is discord.ButtonStyle.secondary, str(b.style))
    check("button carries the custom emoji (name + id, static)",
          b.emoji is not None and b.emoji.name == "imagePhotoroom17"
          and b.emoji.id == 1549206183498481714 and not b.emoji.animated,
          f"{b.emoji!r}")

    # The per-currency reward emoji is no longer a Missions constant.
    # Missions must resolve currency display from the Economy config, and
    # the success glyph must come from utils/emoji.py — asserting either
    # one locally would re-create the hardcode this replaced.
    check("no DIAMOND_EMOJI currency constant in Missions",
          not hasattr(cog, "DIAMOND_EMOJI"))
    check("no CHECKMARK_EMOJI constant in Missions (moved to utils.emoji)",
          not hasattr(cog, "CHECKMARK_EMOJI"))
    check("check glyph comes from utils.emoji", CHECK_EMOJI.startswith("<a:"))

    section("4. Threads count as their parent channel")

    thread = discord.Thread.__new__(discord.Thread)
    thread.parent_id = CH_ALLOWED
    check("thread resolves to its parent channel",
          cog._effective_channel_id(thread) == CH_ALLOWED)
    orphan = discord.Thread.__new__(discord.Thread)
    orphan.parent_id = None
    check("orphaned thread resolves to None", cog._effective_channel_id(orphan) is None)
    check("plain channel resolves to its own id",
          cog._effective_channel_id(type("C", (), {"id": CH_OTHER})()) == CH_OTHER)

    section("7/10. Progress bar, percentage, completed rendering")
    def filled(pct):
        return cog.progress_bar(pct).count(cog.NODE_FILLED)
    check("62% lights 4 of 6 nodes (v3 6-node bar)", filled(62) == 4)
    check("20% lights 2 of 6 nodes", filled(20) == 2)
    check("100% lights all 6", filled(100) == 6)
    check("0% lights none", filled(0) == 0)
    check("28% lights 2 (ceil rule)", filled(28) == 2)
    check("bar joins nodes with ──",
          cog.progress_bar(100) == "⬤──⬤──⬤──⬤──⬤──⬤")
    check("tiny progress lights at least one node", filled(0.5) == 1)

    # v2.1 verification: the BAR (not just the number) must never claim
    # completion early — a full glyph bar reads as "done", and done
    # means the reward was granted. An incomplete mission at 95% shows
    # 6 of 7 nodes; only a completed mission shows 7.
    def block_filled(m):
        return cog._mission_block(m, FakeGuild()).count(cog.NODE_FILLED)
    m95 = {"name": "X", "description": None, "type": "words",
           "target": 1000, "progress": 950, "completed": False,
           "channel_id": None}
    check("incomplete mission at 95% shows 5/6 nodes, not a full bar (v3)",
          block_filled(m95) == 5, cog._mission_block(m95, FakeGuild()))
    m100 = {**m95, "progress": 1000, "completed": True, "reward_type": "coins", "reward_value": "10"}
    check("completed mission shows a full 6/6 bar",
          block_filled(m100) == 6)
    # Sweep: no incomplete progress value may ever render a full bar (6 nodes).
    sweep_full = [p for p in range(0, 1000)
                  if block_filled({**m95, "progress": p, "completed": False}) == 6]
    check("no incomplete progress value (0-99.9%) renders a full bar",
          not sweep_full, str(sweep_full[:5]))
    # And every complete one does (6/6).
    check("bar is full exactly when completed (sweep consistency)",
          block_filled({**m95, "progress": 999, "completed": True, "reward_type": "coins", "reward_value": "1"}) == 6)

    m = {"name": "Write 200 words", "description": None, "type": "words",
         "target": 200, "progress": 124, "completed": False,
         "channel_id": CH_ALLOWED}
    block = cog._mission_block(m, FakeGuild())
    check("block shows capped numeric progress",
          "`Progress: 124 / 200 words`" in block, block)
    check("block shows the real percentage (v3 bold code)",
          "**`62%`**ˎˊ˗" in block and "⬤──⬤──⬤──⬤──◯──◯" in block, block)
    check("restricted mission does NOT show channel text (v3: internal only)",
          "counts in" not in block and "#writing" not in block, block)
    m_free = {**m, "channel_id": None}
    check("unrestricted mission doesn't imply a channel",
          "channel" not in cog._mission_block(m_free, FakeGuild()))
    # Completed missions now show dynamic reward + checkmark, not hardcoded diamond
    m_done = {**m, "progress": 200, "completed": True, "reward_type": "coins", "reward_value": "100"}
    done_block = cog._mission_block(m_done, FakeGuild())
    check("completed mission shows `reward claimed` + dynamic reward + checkmark",
          "`reward claimed`" in done_block and CHECK_EMOJI in done_block
          and "Coins" in done_block, done_block)
    check("completed mission drops the Progress line",
          "Progress:" not in done_block, done_block)
    check("completed mission shows 100% (bold code)",
          "**`100%`**ˎˊ˗" in done_block, done_block)
    m_edge = {**m, "progress": 199, "completed": False}
    check("99.5% renders as 99%, never a false 100% (v3)",
          "**`99%`**ˎˊ˗" in cog._mission_block(m_edge, FakeGuild()))
    m_meta = {"name": "Complete 5 daily missions", "description": None,
              "type": "daily_completions", "target": 5, "progress": 1,
              "completed": False, "channel_id": None}
    check("daily_completions progress line uses 'missions' unit",
          "`Progress: 1 / 5 missions`" in cog._mission_block(m_meta, FakeGuild()))


class FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"#{name}"


class FakeGuild:
    def __init__(self, guild_id=GUILD, name="TestBot"):
        self.id = guild_id
        self._name = name
        self.me = type("Me", (), {"display_name": name})()
        self._channels = {CH_ALLOWED: FakeChannel(CH_ALLOWED, "writing")}

    def get_channel(self, cid):
        return self._channels.get(cid)


class FakeBot:
    def __init__(self, guild):
        self._guild = guild
        self.user = type("U", (), {"display_name": "GlobalBot"})()

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None


# ═══════════════════════════════════════════════════════════════════
async def display_tests():
    section("5/6/13. Two-embed display, dynamic counts, Refresh in place")
    from cogs import missions as cog

    # Dedicated guild so earlier engine-test missions can't bleed into
    # the section counts below.
    D = 4400
    OTHER_USER = 8888

    await me.create_definition(
        D, name="Write 200 words", mtype="words", target=200,
        reward_type="coins", reward_value="100", channel_id=CH_ALLOWED)
    await me.create_definition(
        D, name="voice 60 min", mtype="voice_minutes", target=60,
        reward_type="coins", reward_value="100")
    await me.create_definition(
        D, name="Complete 5 daily missions", mtype="daily_completions",
        target=5, period="weekly", reward_type="diamonds", reward_value="2")
    await me.create_definition(
        D, name="First steps", mtype="messages", target=1,
        period="once", reward_type="coins", reward_value="10")

    # A different member completes the words mission — same guild, so
    # the DENOMINATOR is shared, but the NUMERATOR must be per-user.
    await me.record_activity(None, D, OTHER_USER, "words", 999,
                             channel_id=CH_ALLOWED)
    # USER: words 124/200, voice 60/60 (completes → chains +1 weekly).
    await me.record_activity(None, D, USER, "words", 124,
                             channel_id=CH_ALLOWED)
    await me.record_activity(None, D, USER, "voice_minutes", 60,
                             channel_id=CH_OTHER)

    guild = FakeGuild(guild_id=D)
    bot = FakeBot(guild)
    display = await cog.build_mission_display(bot, guild, USER)
    check("display built (missions exist)", display is not None)
    content, embeds = display

    check("message title uses the bot's display name",
          content == "### TestBot Missions", content)
    daily = [e for e in embeds if e.title.startswith("Daily Mission Progress")]
    weekly = [e for e in embeds if e.title.startswith("Weekly Mission Progress")]
    once = [e for e in embeds if e.title.startswith("One-time Missions")]
    check("daily and weekly are separate embeds",
          len(daily) == 1 and len(weekly) == 1, [e.title for e in embeds])
    check("one-time missions get their own embed", len(once) == 1)
    check("daily heading counts this member's completions dynamically",
          daily[0].title == "Daily Mission Progress (1 / 2)", daily[0].title)
    check("weekly heading is dynamic too",
          weekly[0].title == "Weekly Mission Progress (0 / 1)", weekly[0].title)
    check("completed mission renders `reward claimed` + checkmark",
          "`reward claimed`" in daily[0].description
          and CHECK_EMOJI in daily[0].description)
    check("incomplete mission keeps its Progress line",
          "`Progress: 124 / 200 words`" in daily[0].description)
    check("daily embed ends with its next-rotation countdown",
          "**Next mission:** `" in daily[0].description
          and daily[0].description.count("Next mission:") == 1)
    check("weekly embed has its own countdown",
          "**Next mission:** `" in weekly[0].description)
    check("one-time embed has NO countdown (never resets)",
          "Next mission" not in once[0].description)
    check("weekly meta mission shows missions unit",
          "`Progress: 1 / 5 missions`" in weekly[0].description,
          weekly[0].description)
    check("all embed descriptions under Discord's 4096 cap",
          all(len(e.description) <= 4096 for e in embeds))
    check("channel restriction is internal only - no member-facing channel text",
          "counts in" not in daily[0].description and "#writing" not in daily[0].description)

    # Same guild, other member: shared denominator, own numerator —
    # the words mission is complete for THEM, not for USER.
    _, other_embeds = await cog.build_mission_display(bot, guild, OTHER_USER)
    other_daily = [e for e in other_embeds
                   if e.title.startswith("Daily Mission Progress")]
    check("numerator is per-member (other member completed the words "
          "mission in the same shared section)",
          other_daily[0].title == "Daily Mission Progress (1 / 2)"
          and "`reward claimed`" in other_daily[0].description
          and "`Progress: 124 / 200 words`" not in other_daily[0].description,
          other_daily[0].title)

    # Empty guild → no embeds at all.
    check("guild with no missions returns None (no empty embeds)",
          await cog.build_mission_display(bot, FakeGuild(guild_id=5500),
                                          USER) is None)

    # Only-weekly guild → no empty daily embed.
    await me.create_definition(
        6600, name="Only weekly", mtype="words", target=10,
        period="weekly", reward_type="coins", reward_value="1")
    guild_bw = FakeGuild(guild_id=6600)
    _, embeds_bw = await cog.build_mission_display(
        FakeBot(guild_bw), guild_bw, USER)
    check("period with no missions produces no embed",
          len(embeds_bw) == 1 and embeds_bw[0].title.startswith("Weekly"),
          [e.title for e in embeds_bw])

    # Overflow split: many long missions → continuation embeds, all valid.
    # (Names stay under the 100-char creation cap — the split is driven
    # by the description filler, not by invalid-length names.)
    for i in range(30):
        await me.create_definition(
            D, name=f"Long mission {i} " + "x" * 60,
            mtype="words", target=100, reward_type="coins",
            reward_value="1", description="Filler description " * 8)
    _, long_embeds = await cog.build_mission_display(bot, guild, USER)
    daily_parts = [e for e in long_embeds
                   if e.title.startswith("Daily Mission Progress")]
    check("oversized sections split into continuation embeds",
          len(daily_parts) > 1
          and all(len(e.description) <= 4096 for e in long_embeds),
          f"{len(daily_parts)} daily parts, {len(long_embeds)} embeds")
    check("split headings keep the same (done / total) count on every part",
          all(e.title.startswith("Daily Mission Progress (1 / 32)")
              for e in daily_parts),
          [e.title for e in daily_parts])

    # ── v2.1: pathological legacy row can't break the display ──────
    # A definition created before length validation (or by hand) with a
    # multi-K description: its single block exceeds the chunk limit, so
    # it must be hard-cut rather than producing an oversized embed that
    # Discord would reject.
    P = 7100
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO missions_definitions
                (guild_id, name, description, type, target, period,
                 reward_type, reward_value)
            VALUES (?, 'Pathological', ?, 'words', 10, 'daily',
                    'coins', '1')
        """, (P, "x" * 4500))
        await db.commit()
    guild_p = FakeGuild(guild_id=P)
    try:
        _, embeds_p = await cog.build_mission_display(
            FakeBot(guild_p), guild_p, USER)
        check("pathological legacy row still renders",
              bool(embeds_p) and all(len(e.description) <= 4096 for e in embeds_p),
              [len(e.description) for e in embeds_p])
        check("pathological block is truncated, not dropped",
              any("Pathological" in e.description for e in embeds_p))
    except Exception as e:
        check("pathological legacy row still renders", False, repr(e))

    # ── v2.1: /mission_list pagination (Discord's 25-field cap) ────
    L = 7200
    for i in range(30):
        await me.create_definition(
            L, name=f"List mission {i}", mtype="words", target=5,
            reward_type="coins", reward_value="10")

    class ListResponse:
        def __init__(self):
            self.sent = None
        async def send_message(self, *a, **k):
            self.sent = (a, k)

    class ListInteraction:
        def __init__(self, guild_id):
            self.guild = type("G", (), {"id": guild_id})()
            self.response = ListResponse()

    cog_instance = cog.Missions(bot)
    list_inter = ListInteraction(L)
    try:
        await cog.Missions.mission_list.callback(cog_instance, list_inter)
        kwargs = list_inter.response.sent[1]
        pages = kwargs.get("embeds") or [kwargs.get("embed")]
        field_counts = [len(p.fields) for p in pages]
        check("/mission_list paginates 30 missions into 2 embeds of 20+10",
              field_counts == [20, 10], str(field_counts))
        check("every list page stays under Discord's 25-field cap",
              all(n <= 25 for n in field_counts), str(field_counts))
        check("list field names fit Discord's 256-char title limit",
              all(len(f.name) <= 256 for p in pages for f in p.fields))
    except Exception as e:
        check("/mission_list paginates without raising", False, repr(e))

    # ── Refresh: edits the same message, never sends a new one ──────
    import discord

    class _FakeHTTPResp:
        status = 404
        reason = "Not Found"

    def _http_err():
        return discord.HTTPException(_FakeHTTPResp(), "404: Not Found")

    class FakeFollowup:
        """Interaction.followup stand-in — records sends, can be made
        to fail (fully expired interaction token)."""
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail
        async def send(self, *a, **k):
            if self.fail:
                raise _http_err()
            self.calls.append(k)
            return "replacement-message"

    class FakeResponse:
        def __init__(self, edit_error=None):
            self.calls = []
            self.edit_error = edit_error
        async def send_message(self, *a, **k):
            self.calls.append(("send", a, k))
        async def edit_message(self, *a, **k):
            self.calls.append(("edit", a, k))
            if self.edit_error is not None:
                raise self.edit_error

    class FakeInteraction:
        def __init__(self, user_id, edit_error=None, followup=None):
            self.user = type("U", (), {"id": user_id})()
            self.response = FakeResponse(edit_error)
            self.followup = followup if followup is not None else FakeFollowup()

    view = cog.MissionsView(bot, D, USER)
    inter = FakeInteraction(USER)
    await view.refresh_button.callback(inter)
    check("refresh EDITS the existing message",
          [c[0] for c in inter.response.calls] == ["edit"],
          inter.response.calls)
    check("refresh sends no duplicate message",
          not any(c[0] == "send" for c in inter.response.calls))
    edit_kwargs = inter.response.calls[0][2]
    check("refresh re-renders all embeds in place",
          len(edit_kwargs.get("embeds", [])) >= 3
          and edit_kwargs.get("view") is view, str(edit_kwargs)[:120])

    stranger = FakeInteraction(424242)
    allowed = await view.interaction_check(stranger)
    check("panel is owner-only (strangers are rejected)",
          allowed is False and stranger.response.calls[0][0] == "send")
    owner = FakeInteraction(USER)
    check("owner passes the panel check", await view.interaction_check(owner) is True)

    # ── Stale/deleted panel: recovery paths ─────────────────────────
    # 1. Original message deleted (edit 404s): exactly ONE followup
    #    replaces the unreachable panel — not a duplicate, a successor.
    inter_gone = FakeInteraction(USER, edit_error=_http_err())
    await view.refresh_button.callback(inter_gone)
    gone_edit = [c for c in inter_gone.response.calls if c[0] == "edit"]
    check("deleted panel: the in-place edit was attempted first",
          len(gone_edit) == 1)
    check("deleted panel: exactly one followup replacement",
          len(inter_gone.followup.calls) == 1,
          str(inter_gone.followup.calls))
    fu = inter_gone.followup.calls[0] if inter_gone.followup.calls else {}
    check("replacement is ephemeral and carries the live view",
          fu.get("ephemeral") is True and fu.get("view") is view)
    check("replacement carries the rendered content and embeds",
          isinstance(fu.get("content"), str)
          and len(fu.get("embeds") or []) >= 3)

    # 2. Fully expired interaction token (edit AND followup fail):
    #    swallowed — never raises into the component dispatch.
    inter_dead = FakeInteraction(USER, edit_error=_http_err(),
                                 followup=FakeFollowup(fail=True))
    try:
        await view.refresh_button.callback(inter_dead)
        check("expired interaction: failure swallowed, no raise", True)
    except Exception as e:
        check("expired interaction: failure swallowed, no raise", False, repr(e))

    # 3. Timeout: button disabled, and a failed disable-edit is cosmetic.
    view_t = cog.MissionsView(bot, D, USER)

    class _MsgEditFails:
        async def edit(self, **k):
            raise _http_err()
    view_t.message = _MsgEditFails()
    await view_t.on_timeout()
    check("timeout disables the Refresh button",
          view_t.refresh_button.disabled is True)
    check("timeout swallows the failed disable-edit (no raise)", True)

    # Refresh after every mission in the guild is deleted downgrades
    # gracefully instead of rendering empty embeds.
    for d in await me.get_definitions(D):
        await me.delete_definition(D, d["id"])
    inter2 = FakeInteraction(USER)
    await view.refresh_button.callback(inter2)
    check("refresh with zero missions edits to a notice, no embeds",
          [c[0] for c in inter2.response.calls] == ["edit"]
          and inter2.response.calls[0][2].get("embeds") == []
          and inter2.response.calls[0][2].get("view") is None,
          inter2.response.calls)


# ═══════════════════════════════════════════════════════════════════
def api_tests():
    section("15/16/17. Dashboard API — validation, formatting, round-trip")
    import time
    import dashboard.app as dapp
    from database import DB_PATH as CONFIRMED_DB
    assert CONFIRMED_DB == DB_PATH

    app = dapp.app
    app.config["TESTING"] = True

    GID, UID, MOD = 3300, 300, 301
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled)"
        " VALUES (?,?,?,1)", (GID, UID, "admin"))
    conn.execute(
        "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled)"
        " VALUES (?,?,?,1)", (GID, MOD, "moderator"))
    conn.commit()
    conn.close()

    CSRF = "testcsrf"

    def client(user_id):
        c = app.test_client()
        with c.session_transaction() as s:
            s["user"] = {"id": user_id, "username": "t", "avatar": None}
            s["guild_id"] = GID
            s["expires_at"] = time.time() + 7200
            s["csrf_token"] = CSRF
        return c

    A = client(UID)
    MODC = client(MOD)
    ANON = app.test_client()

    def call(c, method, path, payload=None, csrf=True):
        headers = {}
        if method != "GET":
            if csrf:
                headers["X-CSRF-Token"] = CSRF
            if payload is not None:
                headers["Content-Type"] = "application/json"
        return c.open(path, method=method, json=payload,
                      headers=headers or None)

    # 17: channel selection round-trips into the definition.
    # Realistic magnitude on purpose: channel snowflakes (~1.5e18)
    # exceed JS's 2^53 safe-integer range — this test proves the exact
    # ID survives the picker string → API → engine int() → storage →
    # list-as-string journey with no trailing-digit corruption.
    BIG_CH = 1549206183498481714
    r = call(A, "POST", "/api/missions/definition", {
        "name": "API words", "type": "words", "target": 50,
        "period": "daily", "reward_type": "coins", "reward_value": "250",
        "channel_id": str(BIG_CH)})
    j = r.get_json()
    check("create with channel (as string from the picker) succeeds",
          r.status_code == 200 and j.get("success"), f"{r.status_code} {j}")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT channel_id, target FROM missions_definitions "
        "WHERE guild_id=? AND name='API words'", (GID,)).fetchall()
    conn.close()
    check("big snowflake channel id stored EXACTLY (no 2^53 corruption)",
          rows and rows[0][0] == BIG_CH, str(rows))

    r = call(A, "POST", "/api/missions/definition", {
        "name": "API any", "type": "voice_minutes", "target": 15,
        "period": "weekly", "reward_type": "coins", "reward_value": "250",
        "channel_id": None})
    check("create without channel succeeds (unrestricted)",
          r.status_code == 200 and r.get_json().get("success"))

    # 16: malformed input → clean 400s, never 500s.
    malformed = [
        ("missing name", {"type": "words", "target": 5,
                          "reward_type": "coins", "reward_value": "1"}),
        ("non-numeric target", {"name": "x", "type": "words", "target": "abc",
                                "reward_type": "coins", "reward_value": "1"}),
        ("bad type", {"name": "x", "type": "hugs", "target": 5,
                      "reward_type": "coins", "reward_value": "1"}),
        ("bad channel", {"name": "x", "type": "words", "target": 5,
                         "reward_type": "coins", "reward_value": "1",
                         "channel_id": "not-a-channel"}),
        ("bad reward value", {"name": "x", "type": "words", "target": 5,
                              "reward_type": "coins", "reward_value": "lots"}),
    ]
    for label, payload in malformed:
        r = call(A, "POST", "/api/missions/definition", payload)
        check(f"API rejects {label} with 400",
              r.status_code == 400 and not r.get_json().get("success"),
              f"{r.status_code} {r.get_json()}")

    r = call(A, "DELETE", "/api/missions/definition/999999")
    check("delete unknown mission → 404 (not silent success)",
          r.status_code == 404, r.status_code)
    r = call(A, "POST", "/api/missions/definition/999999/toggle",
             {"enabled": False})
    check("toggle unknown mission → 404", r.status_code == 404, r.status_code)

    # Malformed JSON body (Content-Type json + garbage) must be a clean
    # 400 from Flask's parser, never a 500 — CSRF header included so the
    # request gets PAST the before_request hook and actually reaches the
    # handler's request.json parse.
    r = A.post("/api/missions/definition",
               data="this is { not json",
               content_type="application/json",
               headers={"X-CSRF-Token": CSRF})
    check("malformed JSON body → 400, not 500", r.status_code == 400,
          f"{r.status_code} {r.get_data(as_text=True)[:80]}")
    r = A.post("/api/missions/definition/1/toggle",
               data="{broken",
               content_type="application/json",
               headers={"X-CSRF-Token": CSRF})
    check("malformed JSON on toggle → 400, not 500", r.status_code == 400,
          r.status_code)

    # Length caps apply through the API too.
    r = call(A, "POST", "/api/missions/definition", {
        "name": "N" * 101, "type": "words", "target": 5,
        "reward_type": "coins", "reward_value": "1"})
    check("API rejects over-length name with 400",
          r.status_code == 400 and "100" in (r.get_json() or {}).get("error", ""),
          f"{r.status_code} {r.get_json()}")

    r = call(A, "GET", "/api/missions/completions?limit=abc")
    check("completions with non-numeric limit → 200, not 500",
          r.status_code == 200 and "completions" in (r.get_json() or {}),
          r.status_code)
    r = call(A, "GET", "/api/missions/completions?limit=999999")
    check("completions limit is capped, not an error",
          r.status_code == 200, r.status_code)

    # 15: Recent Completions presentation timestamp.
    conn = sqlite3.connect(DB_PATH)
    has_any = conn.execute(
        "SELECT COUNT(*) FROM mission_progress WHERE completed=1").fetchone()[0]
    conn.close()
    if has_any:
        r = call(A, "GET", "/api/missions/completions")
        rows = r.get_json()["completions"]
        check("completions include 'YYYY-MM-DD · HH:MM' display field",
              all(re.match(r"^\d{4}-\d{2}-\d{2} · \d{2}:\d{2}$",
                           c["completed_at_display"] or "") for c in rows),
              str([c.get("completed_at_display") for c in rows[:3]]))
        check("completions keep the raw stored timestamp too",
              all("completed_at" in c for c in rows))
        check("user ids travel as strings (snowflake safety)",
              all(isinstance(c["user_id"], str) for c in rows))
    else:
        check("completions include display field (skipped: none completed)", True)

    # List endpoint carries channel info for the table — as a STRING
    # (snowflake safety: JSON numbers past 2^53 corrupt trailing digits
    # in JS, breaking both the table lookup and the map key match).
    r = call(A, "GET", "/api/missions/list")
    missions = r.get_json()["missions"]
    api_words = [m for m in missions if m["name"] == "API words"]
    check("list endpoint exposes channel_id as an exact string",
          api_words and api_words[0]["channel_id"] == str(BIG_CH),
          str(api_words and api_words[0]["channel_id"]))

    # The dashboard page itself renders (template + base.html intact).
    r = A.get("/missions")
    check("missions dashboard page renders", r.status_code == 200, r.status_code)
    r = MODC.get("/missions")
    check("missions dashboard page blocked for moderator (admin page)",
          r.status_code == 403, r.status_code)

    # Security: CSRF, permissions, guild isolation.
    r = call(A, "POST", "/api/missions/definition", {
        "name": "no csrf", "type": "words", "target": 5,
        "reward_type": "coins", "reward_value": "1"}, csrf=False)
    check("POST without CSRF token → 403", r.status_code == 403, r.status_code)
    r = call(MODC, "POST", "/api/missions/definition", {
        "name": "mod attempt", "type": "words", "target": 5,
        "reward_type": "coins", "reward_value": "1"})
    check("moderator cannot create missions (admin-only) → 403",
          r.status_code == 403, r.status_code)
    r = ANON.get("/api/missions/list")
    check("anonymous request → 401", r.status_code == 401, r.status_code)
    other = [m for m in missions if m["guild_id"] != GID]
    check("list only returns this guild's missions", not other, str(other))


# ═══════════════════════════════════════════════════════════════════
async def main():
    await init_db()
    await me.ensure_tables()
    await engine_tests()
    cog_tests()
    await display_tests()
    api_tests()

    print(f"\n{'='*60}")
    if _failed:
        print(f"RESULT: {_passed} passed, {_failed} FAILED")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"RESULT: all {_passed} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
