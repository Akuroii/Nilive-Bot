"""Regression harness: every background loop must survive the real boot order.

Reproduces main.py's actual order -- init_db() -> load_cogs() -> bot.start --
with the Discord network stubbed at the first real suspension point
(HTTPClient.static_login), then inspects every tasks.Loop.

Read-only. Uses a scratch SQLite DB outside the repo; never touches production.

    python tests/test_background_task_lifecycle.py     # exit 0 = all pass
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SCRATCH_DIR = tempfile.mkdtemp(prefix="nilive-tasklife-")
os.environ.update({
    "DATABASE_PATH": os.path.join(SCRATCH_DIR, "scratch.db"),
    "NERO_ENVIRONMENT": "production",
    "OWNER_ID": "704453350384730237",
    "SECRET_KEY": "t" * 48,
    "DISCORD_TOKEN": "M" * 24 + "." + "A" * 6 + "." + "B" * 27,
    "DISCORD_CLIENT_ID": "123456789012345678",
})

# main.py's exact cog list
COGS = [
    "cogs.activity_engine", "cogs.mvp", "cogs.moderation", "cogs.leveling",
    "cogs.economy", "cogs.wallet", "cogs.trade", "cogs.missions", "cogs.boost",
    "cogs.botprofile", "cogs.reactionroles", "cogs.tickets", "cogs.embedbuilder",
    "cogs.sticky", "cogs.youtube", "cogs.triggers", "cogs.customcommands",
    "cogs.welcome", "cogs.shop", "cogs.auditlog", "cogs.twitch", "cogs.events",
    "cogs.minigames", "cogs.report", "cogs.health", "cogs.backup",
    "cogs.guild_admin", "cogs.scheduler", "cogs.tagmissions", "cogs.tagpartners",
    "cogs.afk", "cogs.shop_publication_sync", "cogs.command_aliases",
]

# Baseline inventory, MEASURED at 36ac86f rather than inferred: with all 33
# extensions loaded and the client still pre-login, exactly 17 tasks.Loop objects
# exist and 13 of them were already dead. 12 awaited wait_until_ready() inside
# before_loop (Leveling contributed two); CommandAliases._alias_sync_check is the
# 13th -- its before_loop did too, and it starts from cog_load() rather than
# __init__(), which is why an earlier inventory missed it. Verified by probing
# its task at baseline: died with RuntimeError("Client has not been properly
# initialised") before the first tick.
PREVIOUSLY_DEAD = {
    "Backup.daily_backup",
    "CommandAliases._alias_sync_check",
    "Events.scheduled_events_task",
    "Leveling.blacklist_role_task",
    "Leveling.leaderboard_reset_task",
    "MVP.mvp_cycle_task",
    "Minigames.daily_check_loop",
    "Moderation.scheduled_unban_check",
    "Scheduler.check_scheduled",
    "Shop.temp_role_cleanup",
    "TagMissions.mission_poll",
    "Twitch.check_streams",
    "YouTube.check_videos",
}

# The 4 loops that were already healthy at baseline (they never awaited
# wait_until_ready() in before_loop, or had no blocking precondition).
PREVIOUSLY_HEALTHY = {
    "ActivityEngine.voice_tick_task",
    "Health.heartbeat",
    "Minigames.spawn_request_loop",
    "ReactionRoles.expiry_check",
}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return ok


async def main() -> int:
    import discord
    from discord.ext import commands
    from discord.http import HTTPClient
    from utils.task_lifecycle import iter_cog_loops, report_task_health

    # ---- 1. pre-login state: the sentinel that used to kill the loops ----
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    from discord.utils import MISSING
    check("client._ready is the MISSING sentinel before login",
          bot._ready is MISSING, repr(bot._ready))

    raised = False
    try:
        await asyncio.wait_for(bot.wait_until_ready(), timeout=0.2)
    except RuntimeError:
        raised = True
    except asyncio.TimeoutError:
        raised = False
    check("wait_until_ready() still raises at this point (root cause)",
          raised, "RuntimeError: Client has not been properly initialized")

    # ---- 2. real boot order ----
    from database import init_db
    await init_db()
    check("init_db() completed before any task executed", True, "scratch DB")

    failed_cogs = []
    for name in COGS:
        try:
            await bot.load_extension(name)
        except Exception as exc:
            failed_cogs.append(f"{name}: {type(exc).__name__}: {exc}")
    check("all cogs load", not failed_cogs, "; ".join(failed_cogs) or f"{len(bot.cogs)} cogs")

    # the suspension login() performs, where the old loops died
    async def fake_static_login(self, token, **kwargs):
        await asyncio.sleep(0.05)
        raise discord.LoginFailure("stubbed: no Discord network in tests")
    HTTPClient.static_login = fake_static_login
    try:
        await bot.start(os.environ["DISCORD_TOKEN"])
    except discord.LoginFailure:
        pass

    from discord.utils import MISSING as _M
    check("client._ready exists after login()", bot._ready is not MISSING)

    # ---- 3. every loop must survive ----
    await asyncio.sleep(1.5)

    snapshot = report_task_health(bot)
    loops = {f"{c}.{a}": l for c, a, l in iter_cog_loops(bot)}

    check("no loop died at startup", not snapshot["dead"],
          str([d["loop"] for d in snapshot["dead"]]))
    check("loop count is stable (17 expected)", len(loops) == 17, f"{len(loops)} loops")
    check("dead+healthy inventory accounts for every discovered loop",
          set(loops) == (PREVIOUSLY_DEAD | PREVIOUSLY_HEALTHY),
          f"missing={sorted(set(loops) - (PREVIOUSLY_DEAD | PREVIOUSLY_HEALTHY))} "
          f"extra={sorted((PREVIOUSLY_DEAD | PREVIOUSLY_HEALTHY) - set(loops))}")
    check("previously-dead and previously-healthy sets are disjoint",
          not (PREVIOUSLY_DEAD & PREVIOUSLY_HEALTHY),
          str(PREVIOUSLY_DEAD & PREVIOUSLY_HEALTHY))
    check("baseline inventory is 13 dead + 4 healthy = 17",
          len(PREVIOUSLY_DEAD) == 13 and len(PREVIOUSLY_HEALTHY) == 4,
          f"dead={len(PREVIOUSLY_DEAD)} healthy={len(PREVIOUSLY_HEALTHY)}")

    for label in sorted(PREVIOUSLY_DEAD):
        loop = loops.get(label)
        if loop is None:
            check(f"previously-dead {label}", False, "loop not found")
            continue
        task = loop.get_task()
        exc = None
        try:
            exc = task.exception() if task else None
        except Exception:
            exc = None
        check(f"previously-dead now alive: {label}",
              loop.is_running() and task is not None and not task.done() and exc is None,
              f"is_running={loop.is_running()} task_done={task.done() if task else None} exc={exc}")

    for label in sorted(PREVIOUSLY_HEALTHY):
        loop = loops.get(label)
        check(f"previously-healthy still alive (no regression): {label}",
              loop is not None and loop.is_running(),
              "loop not found" if loop is None else f"is_running={loop.is_running()}")

    # ---- 4. before_loop must actually unblock once ready ----
    # Flip readiness the way Discord does, then confirm a loop body runs.
    ran: dict[str, int] = {}

    from discord.ext import tasks

    async def body():
        ran["n"] = ran.get("n", 0) + 1

    probe = tasks.loop(seconds=0.05)(body)

    @probe.before_loop
    async def _before():           # mirrors the shipped helper
        from utils.task_lifecycle import wait_until_client_ready
        await wait_until_client_ready(bot, interval=0.02)

    probe.start()
    await asyncio.sleep(0.2)
    check("waiting loop has not run while the client is not ready",
          ran.get("n", 0) == 0, f"runs={ran.get('n', 0)}")

    bot._ready.set()               # == READY received
    await asyncio.sleep(0.4)
    check("loop starts running once the client reports ready",
          ran.get("n", 0) > 0, f"runs={ran.get('n', 0)}")
    probe.cancel()

    # ---- 5. idempotent start: a second READY must not raise ----
    from utils.task_lifecycle import ensure_started
    target = loops["Scheduler.check_scheduled"]
    second = ensure_started(target)
    check("ensure_started() on a running loop is a no-op, not a raise",
          second is False, "returns False")

    double_start_raised = False
    try:
        target.start()             # the raw call the old on_ready made
    except RuntimeError:
        double_start_raised = True
    check("raw Loop.start() would have raised on reconnect (why ensure_started exists)",
          double_start_raised, "RuntimeError: Task is already launched")

    # ---- 6. reconnect: on_ready again must not duplicate loops ----
    before = {label: id(l.get_task()) for label, l in loops.items() if l.is_running()}
    from utils.task_lifecycle import report_task_health as _r
    _r(bot)
    after = {label: id(l.get_task()) for label, l in loops.items() if l.is_running()}
    check("second READY re-report leaves every running loop instance unchanged",
          before == after, f"{len(before)} running")

    # ---- 7. cancellation / unload ----
    bot.dispatch("ready")  # no-op; ensure nothing explodes
    unloaded_ok = True
    for name in list(bot.extensions):
        try:
            await bot.unload_extension(name)
        except Exception as exc:
            unloaded_ok = False
            print(f"  unload {name} failed: {exc}")
    await asyncio.sleep(0.2)
    check("all cogs unload cleanly (cancellation is safe)", unloaded_ok)

    # ---- summary ----
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n===== {passed}/{total} checks passed =====")
    if passed != total:
        print("FAILURES:")
        for n, ok, d in results:
            if not ok:
                print(f"  - {n} :: {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
