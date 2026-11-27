"""Background-task lifecycle helpers.

Why this module exists
----------------------
``main.py`` boots in this order::

    init_db()  ->  load_cogs()  ->  bot.start(token)

Cogs therefore construct their ``tasks.loop`` objects — and start them — *before*
``discord.Client.login()`` has run. ``Client._ready`` is only created inside
``Client._async_setup_hook()`` (called from ``login()``); before that it is the
``MISSING`` sentinel and ``Client.wait_until_ready()`` raises
``RuntimeError: Client has not been properly initialized``.

``tasks.Loop._loop`` awaits ``before_loop`` *outside* its own retry ``try``
block, so an exception there kills the task on arrival and it is never
retried. Every loop whose ``before_loop`` awaited ``wait_until_ready()`` was
therefore dead for the entire lifetime of every bot process: it ticked zero
times, forever, while ``loaded_cogs`` still reported the cog as loaded.

This module is the single implementation of the two primitives that fix that
class of failure, so it cannot drift between cogs:

``wait_until_client_ready``
    A drop-in replacement for ``wait_until_ready()`` inside ``before_loop``
    that polls ``is_ready()``. ``is_ready()`` is documented to return ``False``
    — never raise — before login and flips ``True`` at READY, so this keeps
    "wait until ready" semantics across boot-time loading, reconnects and
    post-login cog reloads without depending on the internal readiness event.

``ensure_started``
    Starts a loop at most once. ``Loop.start()`` raises
    ``RuntimeError('Task is already launched and is not completed.')`` on a
    second call, which would abort the caller — notably ``on_ready``, which
    Discord dispatches again after a reconnect that required a fresh IDENTIFY.

Both are async/sync-safe to call from ``__init__``, ``cog_load`` and ``on_ready``.
"""
from __future__ import annotations

import asyncio
from typing import Any

# Poll interval while waiting for the client to become ready. Matches the
# value used by the original in-cog fixes (cogs/health.py, cogs/activity_engine.py).
READY_POLL_SECONDS = 0.5


async def wait_until_client_ready(bot: Any,
                                  *,
                                  interval: float = READY_POLL_SECONDS) -> None:
    """Block until the Discord client is ready, without raising pre-login.

    Safe to await from ``before_loop``: it never touches the internal
    ``_ready`` event, so it cannot raise the pre-login ``RuntimeError`` that
    previously killed the task. Returns immediately once the client is ready.
    """
    while not bot.is_ready():
        await asyncio.sleep(interval)


def ensure_started(loop: Any) -> bool:
    """Start ``loop`` unless it is already running. Returns True if started.

    Never raises for the "already started" case, so a second ``on_ready``
    (Discord re-dispatches it after a reconnect needing a fresh IDENTIFY)
    cannot abort the rest of that handler.
    """
    if loop.is_running():
        return False
    loop.start()
    return True


def iter_cog_loops(bot: Any):
    """Yield ``(cog_name, attribute, loop)`` for every ``tasks.Loop`` on every cog.

    ``tasks.Loop`` is a descriptor: the per-cog instance is bound in the cog's
    ``__dict__``, so ``vars(cog)`` is the authoritative view.
    """
    for cog_name, cog in getattr(bot, "cogs", {}).items():
        for attribute, value in vars(cog).items():
            if hasattr(value, "is_running") and hasattr(value, "get_task"):
                yield cog_name, attribute, value


def task_health(bot: Any) -> dict:
    """Snapshot background-loop health. Read-only; never raises."""
    running, dead = [], []
    for cog_name, attribute, loop in iter_cog_loops(bot):
        label = f"{cog_name}.{attribute}"
        task = None
        try:
            task = loop.get_task()
        except Exception:
            task = None
        error = None
        try:
            if task is not None:
                error = task.exception()
        except Exception:
            error = None
        if loop.is_running():
            running.append(label)
        else:
            dead.append({
                "loop": label,
                "error": f"{type(error).__name__}: {error}" if error else None,
            })
    return {"running": running, "dead": dead,
            "total": len(running) + len(dead)}


def report_task_health(bot: Any) -> dict:
    """Log every dead background loop loudly. Returns the snapshot.

    A loop that died is a silent functional failure — its feature simply never
    runs — so it must be visible in the deployment logs rather than inferred.
    """
    snapshot = task_health(bot)
    if snapshot["dead"]:
        print(f"[TASKS] WARNING: {len(snapshot['dead'])} of {snapshot['total']} "
              f"background loop(s) are NOT running:")
        for entry in snapshot["dead"]:
            print(f"[TASKS]   DEAD {entry['loop']}"
                  + (f"  ({entry['error']})" if entry["error"] else ""))
    else:
        print(f"[TASKS] all {snapshot['total']} background loop(s) running")
    return snapshot
