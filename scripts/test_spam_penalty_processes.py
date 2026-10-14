"""Independent-process regressions for durable anti-spam incidents.

The child interpreters each open the same persistent SQLite database, as two
bot processes do after a restart or during concurrent message delivery.
Run from the repository root with:
    .venv/bin/python scripts/test_spam_penalty_processes.py
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from phase1_support import DB_PATH, GUILD, USER, ROOT, execute, reset_database, rows


CHILD = r'''
import asyncio
import json
import os
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


def emit(value):
    print("SPAM_PROCESS_RESULT=" + json.dumps(value, sort_keys=True))


async def listener_main():
    from database import init_db
    import aiosqlite
    import cogs.leveling as leveling_module
    from cogs.leveling import Leveling

    # Each listener scenario is a freshly started interpreter and initializes
    # the same on-disk database before its first event.
    await init_db()
    guild_id = int(os.environ["SPAM_TEST_GUILD"])
    user_id = int(os.environ["SPAM_TEST_USER"])
    mode = os.environ["SPAM_TEST_MODE"]
    clock = [0.0]
    leveling_module.time = SimpleNamespace(time=lambda: clock[0])

    cog = Leveling.__new__(Leveling)
    cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}
    warnings = []

    if mode == "A":
        schedule = [(1000 + 2 * i, "same continuing spam") for i in range(5)]
    elif mode == "B":
        schedule = [(1010 + 2 * i, "same continuing spam") for i in range(5)]
    elif mode == "C":
        schedule = [(1040, "ordinary after old quiet expiry")]
        schedule += [(1041 + 2 * i, "new continuing spam") for i in range(5)]
    elif mode == "D":
        schedule = [(1070, "ordinary after second quiet expiry")]
        schedule += [(1071 + 2 * i, "third continuing spam") for i in range(5)]
    elif mode == "E":
        schedule = [(1100, "ordinary after third quiet expiry")]
        schedule += [(1101 + 2 * i, "fourth continuing spam") for i in range(5)]
    elif mode == "touch_open":
        schedule = [(3000 + 2 * i, "touch-window opener") for i in range(5)]
    elif mode == "touch_active":
        schedule = [(3010, "one ordinary message during active incident")]
    elif mode == "normal_open":
        schedule = [(2000 + 2 * i, "separate user's first spam")
                    for i in range(5)]
    elif mode == "normal_after_expiry":
        schedule = [(2030, "ordinary message after quiet expiry")]
    else:
        raise ValueError("unknown listener mode: " + mode)

    for timestamp, content in schedule:
        clock[0] = float(timestamp)
        author = SimpleNamespace(
            id=user_id, bot=False, roles=[], mention=f"<@{user_id}>",
            display_name=f"user-{user_id}")
        message = SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=guild_id, get_channel=lambda _id: None),
            channel=SimpleNamespace(send=AsyncMock()),
            reply=AsyncMock(), content=content)
        await Leveling.on_activity_message(cog, message, 3)
        warnings.append(message.reply.await_count)

    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        cursor = await db.execute(
            "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        xp_row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(deducted), 0), "
            "COUNT(DISTINCT incident_id) "
            "FROM leveling_spam_penalty_events WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        events = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT incident_id, active_until, last_warning_at "
            "FROM leveling_spam_incidents WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        incident = await cursor.fetchone()
    emit({
        "mode": mode,
        "xp": xp_row[0] if xp_row else None,
        "warnings": sum(warnings),
        "detector_sample_count": len(cog._spam_tracker.get((guild_id, user_id), [])),
        "event_count": events[0],
        "deducted_total": events[1],
        "distinct_penalty_incidents": events[2],
        "incident_id": incident[0] if incident else None,
        "active_until": incident[1] if incident else None,
        "last_warning_at": incident[2] if incident else None,
    })


async def concurrent_claim_main():
    from utils.xp_calculator import apply_spam_penalty

    barrier_dir = Path(os.environ["SPAM_TEST_BARRIER"])
    barrier_dir.mkdir(parents=True, exist_ok=True)
    worker = os.environ["SPAM_TEST_WORKER"]
    (barrier_dir / f"ready-{worker}").touch()
    deadline = real_time.monotonic() + 15
    while not all((barrier_dir / f"ready-{other}").exists()
                  for other in ("one", "two")):
        if real_time.monotonic() >= deadline:
            raise TimeoutError("the independent penalty workers did not rendezvous")
        real_time.sleep(0.01)

    result = await apply_spam_penalty(
        int(os.environ["SPAM_TEST_GUILD"]),
        int(os.environ["SPAM_TEST_USER"]),
        divisor=1000,
        penalty_cap=50,
        now=6000,
        incident_detected=True,
        incident_window_seconds=20,
        warning_window_seconds=20)
    emit({key: result.get(key) for key in (
        "incident_started", "incident_id", "applied", "deducted",
        "warning_due", "in_incident")})


if __name__ == "__main__":
    mode = os.environ["SPAM_TEST_MODE"]
    if mode == "concurrent_claim":
        asyncio.run(concurrent_claim_main())
    else:
        asyncio.run(listener_main())
'''


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        raise AssertionError(label)


def run_child(mode, user, **extra_env):
    env = os.environ.copy()
    env.update({
        "SPAM_TEST_MODE": mode,
        "SPAM_TEST_GUILD": str(GUILD),
        "SPAM_TEST_USER": str(user),
        **{key: str(value) for key, value in extra_env.items()},
    })
    result = subprocess.run(
        [sys.executable, "-c", CHILD], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise AssertionError(
            f"child {mode} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    records = [line.split("=", 1)[1] for line in result.stdout.splitlines()
               if line.startswith("SPAM_PROCESS_RESULT=")]
    if len(records) != 1:
        raise AssertionError(
            f"child {mode} returned no unique result\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return json.loads(records[0])


def set_config(*, message_xp=0, cooldown=10):
    execute("""
        INSERT INTO leveling_config
            (guild_id,message_xp_enabled,xp_cooldown_seconds,
             spam_detection_enabled,spam_threshold,spam_window_seconds,
             spam_xp_penalty_divisor,levelup_announce)
        VALUES (?,?,?,1,10,20,1000,0)
        ON CONFLICT(guild_id) DO UPDATE SET
            message_xp_enabled=excluded.message_xp_enabled,
            xp_cooldown_seconds=excluded.xp_cooldown_seconds,
            spam_detection_enabled=1,
            spam_threshold=10,
            spam_window_seconds=20,
            spam_xp_penalty_divisor=1000,
            levelup_announce=0
    """, (GUILD, message_xp, cooldown))


def seed(user, xp):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,0)",
            (GUILD, user, xp))


async def main():
    await reset_database()

    # Simulate a pre-incident-id release: a budget row exists in the old
    # ledger, but that table has no incident_id column yet. Startup migration
    # must add the column without discarding the existing rolling budget.
    legacy_user = USER + 103
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO leveling_spam_penalty_events "
            "(guild_id,user_id,created_at,deducted,rolling_cap) "
            "VALUES (?,?,?,?,?)",
            (GUILD, legacy_user, 500, 3, 50))
        db.execute("DROP INDEX IF EXISTS idx_leveling_spam_penalties_incident")
        db.execute(
            "ALTER TABLE leveling_spam_penalty_events DROP COLUMN incident_id")
    from database import init_db
    await init_db()
    migrated_columns = {
        row[1] for row in rows("PRAGMA table_info(leveling_spam_penalty_events)")
    }
    check("legacy penalty ledger migrates incident_id without losing budget rows",
          "incident_id" in migrated_columns
          and rows("SELECT deducted FROM leveling_spam_penalty_events "
                   "WHERE guild_id=? AND user_id=?", (GUILD, legacy_user))
          == [(3,)])

    set_config()
    incident_user = USER + 100
    normal_user = USER + 101
    concurrent_user = USER + 102
    touch_user = USER + 104
    for user, xp in ((incident_user, 20_000),
                     (normal_user, 10_000),
                     (concurrent_user, 20_000),
                     (touch_user, 20_000)):
        seed(user, xp)

    # Process A opens and penalizes an incident. A separately-started Process B
    # gets only the shared DB; it must see the same active incident and warning
    # claim even though its detector/cog state starts empty.
    process_a = run_child("A", incident_user)
    process_b = run_child("B", incident_user)
    check("independent process A creates exactly one 20 XP penalty and warning",
          process_a["xp"] == 19_980 and process_a["event_count"] == 1
          and process_a["deducted_total"] == 20 and process_a["warnings"] == 1,
          str(process_a))
    check("process B restart continues the same incident without repeat penalty/warning",
          process_b["xp"] == 19_980 and process_b["event_count"] == 1
          and process_b["incident_id"] == process_a["incident_id"]
          and process_b["warnings"] == 0
          and process_b["detector_sample_count"] == 0,
          str(process_b))

    touch_open = run_child("touch_open", touch_user)
    touch_active = run_child("touch_active", touch_user)
    check("active incident persistence/expiry does not depend on post-open detector samples",
          touch_open["xp"] == 19_980 and touch_active["xp"] == 19_980
          and touch_active["incident_id"] == touch_open["incident_id"]
          and touch_active["active_until"] == 3030
          and touch_active["detector_sample_count"] == 0
          and touch_active["event_count"] == 1
          and touch_active["warnings"] == 0,
          f"open={touch_open}; active_message={touch_active}")

    process_c = run_child("C", incident_user)
    process_d = run_child("D", incident_user)
    process_e = run_child("E", incident_user)
    check("a new incident starts after quiet expiry and is penalized once",
          process_c["xp"] == 19_961 and process_c["event_count"] == 2
          and process_c["distinct_penalty_incidents"] == 2
          and process_c["incident_id"] != process_b["incident_id"]
          and process_c["warnings"] == 1,
          str(process_c))
    check("rolling-hour budget remains shared across more process restarts",
          process_d["xp"] == 19_950 and process_d["deducted_total"] == 50
          and process_d["event_count"] == 3 and process_d["warnings"] == 1,
          str(process_d))
    check("exhausted rolling budget prevents further deduction and warning",
          process_e["xp"] == 19_950 and process_e["deducted_total"] == 50
          and process_e["event_count"] == 3 and process_e["warnings"] == 0,
          str(process_e))

    # A second member proves ordinary Message XP resumes after the stored
    # deadline rather than treating post-expiry chatter as the old incident.
    opened = run_child("normal_open", normal_user)
    set_config(message_xp=1, cooldown=0)
    after_expiry = run_child("normal_after_expiry", normal_user)
    check("normal message after persisted expiry is not suppressed as old incident",
          opened["xp"] == 9_990 and after_expiry["xp"] == 9_995
          and after_expiry["event_count"] == 1
          and after_expiry["warnings"] == 0,
          f"open={opened}; after_expiry={after_expiry}")

    # Two distinct Python processes rendezvous, then concurrently try to claim
    # the same member's first incident using the production transactional API.
    # The shared SQLite DB must serialize claim + penalty as one operation.
    barrier = Path(tempfile.mkdtemp(prefix="nilive_spam_barrier_"))
    children = []
    for worker in ("one", "two"):
        env = os.environ.copy()
        env.update({
            "SPAM_TEST_MODE": "concurrent_claim",
            "SPAM_TEST_GUILD": str(GUILD),
            "SPAM_TEST_USER": str(concurrent_user),
            "SPAM_TEST_BARRIER": str(barrier),
            "SPAM_TEST_WORKER": worker,
        })
        children.append(subprocess.Popen(
            [sys.executable, "-c", CHILD], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    concurrent_results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        if child.returncode:
            raise AssertionError(
                f"concurrent child exited {child.returncode}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}")
        records = [line.split("=", 1)[1] for line in stdout.splitlines()
                   if line.startswith("SPAM_PROCESS_RESULT=")]
        if len(records) != 1:
            raise AssertionError(
                f"concurrent child returned no unique result\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}")
        concurrent_results.append(json.loads(records[0]))
    shutil.rmtree(barrier, ignore_errors=True)

    concurrent_events = rows(
        "SELECT COUNT(*), COALESCE(SUM(deducted),0) "
        "FROM leveling_spam_penalty_events WHERE guild_id=? AND user_id=?",
        (GUILD, concurrent_user))
    concurrent_state = rows(
        "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
        (GUILD, concurrent_user))
    check("two simultaneous independent claimants serialize to one penalty and warning claim",
          sorted(bool(item["incident_started"]) for item in concurrent_results)
          == [False, True]
          and sum(bool(item["warning_due"]) for item in concurrent_results) == 1
          and concurrent_events == [(1, 20)]
          and concurrent_state == [(19_980,)],
          f"workers={concurrent_results}, events={concurrent_events}, xp={concurrent_state}")

    print("ALL INDEPENDENT-PROCESS SPAM CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
