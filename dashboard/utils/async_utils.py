import asyncio
import threading

# PERFORMANCE FIX (dark-fixes pass #2): the original implementation
# spun up a brand-new asyncio event loop, ran one coroutine, then
# tore the loop down — on every single call. There are 120+ call
# sites across dashboard/app.py and dashboard/api.py alone, so a
# dashboard under real traffic was creating and destroying an event
# loop per DB-touching request, sometimes several times within the
# same request. That's real overhead (loop setup/teardown isn't free)
# and a known source of subtle asyncio footguns (e.g. any library
# code that caches something against "the current loop" breaks across
# calls since the loop identity changes every time).
#
# Fix: one persistent event loop, running forever in a dedicated
# background thread, started lazily on first use. Flask's request
# threads submit coroutines to it via run_coroutine_threadsafe and
# block on the result — which is exactly what run_async's callers
# already expect (a synchronous call that returns the coroutine's
# result), so this is a drop-in replacement. No call site anywhere
# needed to change.
#
# This was chosen over the alternative of dropping aiosqlite for
# synchronous sqlite3 on the dashboard side (also viable, arguably
# more "correct" since Flask routes don't need to be async at all)
# because that alternative means touching every one of those 120+
# call sites individually — real scope, real regression risk, for a
# problem this fixes just as effectively as a single 40-line file.
# If the dashboard ever gets rewritten to be natively async (e.g.
# moving off Flask), revisit this decision.

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _run_background_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_loop_running() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    if _loop is not None and _loop.is_running():
        return _loop
    with _lock:
        # Re-check after acquiring the lock — another thread may have
        # already started it while we were waiting.
        if _loop is not None and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(
            target=_run_background_loop, args=(_loop,),
            name="dashboard-async-loop", daemon=True)
        _thread.start()
    return _loop


def run_async(coro):
    loop = _ensure_loop_running()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
