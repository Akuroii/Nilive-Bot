"""Offline (no-network) tests for the health heartbeat pipeline.

Reproduces the exact boot order of main.py (load extensions BEFORE login)
against a throwaway SQLite file, and asserts:

  1. the heartbeat task SURVIVES cog load pre-login — the old loop died on
     arrival because wait_until_ready() raises RuntimeError before login
     and tasks.Loop awaits before_loop outside its own try/except;
  2. started_at is refreshed at every startup (it used to be frozen at
     first-ever row creation by INSERT OR IGNORE);
  3. once "ready", last_heartbeat actually updates and is fresh — the
     end-to-end "does the running bot keep bot_status alive" check;
  4. non-finite gateway latency (inf/nan sentinels) no longer skips the
     whole heartbeat write (round() used to raise on them);
  5. overview.html renders Bot Status from the real is_online state and
     no longer hardcodes "Online".

Run:  python3 scripts/test_heartbeat.py
"""

import asyncio
import os
import sys
import tempfile

# Path the repo root so `cogs.health` / `database` import cleanly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


RESET = "\x1b[0m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
BOLD = "\x1b[1m"


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DATABASE_PATH"] = tmp.name
    # database.py hard-requires OWNER_ID at import time (security fix).
    if not os.getenv("OWNER_ID"):
        os.environ["OWNER_ID"] = "123456789012345678"

    asyncio.run(run(tmp.name))
    os.unlink(tmp.name)
    print()
    color = GREEN if FAIL == 0 else RED
    print(f"{BOLD}{color}{PASS}/{PASS + FAIL} passed.{RESET}")
    sys.exit(1 if FAIL else 0)


OLD_STAMP = "2020-01-01T00:00:00+00:00"


async def read_row(db_path):
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT * FROM bot_status WHERE id = 1")
        row = await cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))


async def run(db_path):
    import datetime as dt

    import discord
    from discord.ext import commands

    from database import init_db
    from cogs import health as H

    print(f"{BOLD}Health heartbeat tests (DB: {db_path}){RESET}")

    await init_db()

    # Pre-seed a stale row so "refresh on startup" is observable.
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO bot_status (id, started_at) VALUES (1, ?)",
            (OLD_STAMP,))
        await db.commit()

    # ── 1. pre-login cog load: the task must survive (old code died here) ──
    print(f"{BOLD}[1] pre-login load & task survival{RESET}")
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    check("Client._ready is the pre-login sentinel",
          not bot.is_ready())
    try:
        await bot.load_extension("cogs.health")
        check("load_extension('cogs.health') succeeds pre-login", True)
    except Exception as e:
        check("load_extension('cogs.health') succeeds pre-login", False,
              f"{type(e).__name__}: {e}")
        return

    cog = bot.get_cog("Health")
    task = getattr(cog.heartbeat, "_task", None)
    check("heartbeat task was started", task is not None)
    await asyncio.sleep(0.3)
    alive = task is not None and not task.done()
    if task is not None and task.done():
        try:
            await task
        except Exception as e:
            extra = f"task died: {type(e).__name__}: {e}"
    else:
        extra = ""
    check("heartbeat task still alive 0.3s after pre-login load", alive, extra)

    # ── 2. started_at refreshed at startup; frozen within the run ─────────
    print(f"{BOLD}[2] started_at refreshed on startup{RESET}")
    row = await read_row(db_path)
    now = dt.datetime.now(dt.timezone.utc)
    started = dt.datetime.fromisoformat(row["started_at"])
    check("started_at no longer the stale seed",
          row["started_at"] != OLD_STAMP)
    check("started_at is this startup's timestamp (<5s old)",
          abs((now - started).total_seconds()) < 5,
          f"started_at={row['started_at']}")
    check("last_heartbeat not written before ready",
          row["last_heartbeat"] is None)

    # record_error (main.py error handlers) must NOT re-stamp started_at.
    await H.record_error("test-source", "test error")
    row2 = await read_row(db_path)
    check("record_error does not re-stamp started_at",
          row2["started_at"] == row["started_at"])
    check("record_error still records the error",
          (row2["last_error"] or "").startswith("[test-source]"))

    # ── 3. real running behavior: last_heartbeat updates after startup ────
    print(f"{BOLD}[3] last_heartbeat updates after startup{RESET}")
    # Simulate login(): _async_setup_hook creates Client._ready, then the
    # READY handler sets it — exactly the two steps login() performs.
    await bot._async_setup_hook()
    bot._handle_ready()
    check("bot is_ready() after simulated READY", bot.is_ready())

    # First tick runs immediately after before_loop resolves (relative
    # tasks.loop fires the body first, then sleeps between iterations).
    await asyncio.sleep(1.2)
    row = await read_row(db_path)
    check("last_heartbeat is now written",
          row["last_heartbeat"] is not None)
    if row["last_heartbeat"]:
        hb = dt.datetime.fromisoformat(row["last_heartbeat"])
        age = (dt.datetime.now(dt.timezone.utc) - hb).total_seconds()
        check("last_heartbeat is fresh (<5s old)", age < 5, f"age={age:.1f}s")
    check("started_at unchanged by heartbeat ticks",
          row["started_at"] == row2["started_at"])

    # ── 4. non-finite latency guard (bot.latency is nan with no ws) ───────
    print(f"{BOLD}[4] non-finite latency never skips the write{RESET}")
    check("no-ws bot.latency is a non-finite sentinel",
          not (bot.latency is None) and
          not __import__("math").isfinite(bot.latency))
    check("write succeeded despite sentinel latency",
          row["latency_ms"] == 0, f"latency_ms={row['latency_ms']}")
    check("_latency_ms(None) == 0", H._latency_ms(None) == 0)
    check("_latency_ms(inf) == 0", H._latency_ms(float("inf")) == 0)
    check("_latency_ms(nan) == 0", H._latency_ms(float("nan")) == 0)
    check("_latency_ms(0.042) == 42", H._latency_ms(0.042) == 42)
    check("_latency_ms('junk') == 0", H._latency_ms("junk") == 0)

    # ── 5. overview.html uses the real is_online state ────────────────────
    print(f"{BOLD}[5] overview.html bot-status card{RESET}")
    tpl_path = os.path.join(
        ROOT, "dashboard", "templates", "general", "overview.html")
    tpl = open(tpl_path, encoding="utf-8").read()
    check("card branches on is_online",
          "{% if is_online %}" in tpl)
    check("no hardcoded 'Online' value",
          'color:var(--success)">Online' not in tpl
          and ">Online</div>" not in tpl)
    check("card keeps the Bot Status label", "Bot Status" in tpl)

    # Stop the loop cleanly so the script exits without pending tasks.
    cog.heartbeat.cancel()


if __name__ == "__main__":
    main()
