"""Dashboard authorization must survive disconnects/resumes without weakening.

Focus: the disconnect/resume ordering race in utils/guild_lifecycle.py that
revoked a session which had already been re-verified, and the recovery paths
(reconnect, supervisor restart, heartbeat interruption).

Read-only. Scratch SQLite DB outside the repo; Discord is a local double.

    python tests/test_gateway_authorization_recovery.py    # exit 0 = all pass
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SCRATCH_DIR = tempfile.mkdtemp(prefix="nilive-gw-")
DB = os.path.join(SCRATCH_DIR, "scratch.db")
os.environ.update({
    "DATABASE_PATH": DB,
    "NERO_ENVIRONMENT": "production",
    "OWNER_ID": "704453350384730237",
    "SECRET_KEY": "g" * 48,
    "DISCORD_TOKEN": "M" * 24 + "." + "A" * 6 + "." + "B" * 27,
})

GUILD = 1413528046785101865
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return ok


class FakeWS:
    def __init__(self): self.open = True


class FakeGuild:
    def __init__(self, gid=GUILD):
        self.id = gid; self.name = "Scratch"; self.icon = None
        self.owner_id = 704453350384730237; self.member_count = 3
        self.unavailable = False


class FakeBot:
    """Minimal Client double: is_ready / is_closed / ws.open / guilds."""
    def __init__(self, guilds):
        self.guilds = guilds
        self.ws = FakeWS()
        self._open = True
        self._ready_flag = True

    def is_ready(self): return self._ready_flag
    def is_closed(self): return not self._open


async def authorize(guild_id=GUILD):
    """Exactly what dashboard/permissions.py asks for the trusted tier."""
    from database import check_bot_guild_access_atomic, GuildAccessState
    state = await check_bot_guild_access_atomic(guild_id)
    return state == GuildAccessState.INSTALLED, state


async def main():
    import aiosqlite
    import database
    from utils.guild_lifecycle import GuildLifecycleCoordinator

    await database.init_db()

    async def seed_guilds(guilds):
        async with aiosqlite.connect(DB) as db:
            for g in guilds:
                await db.execute(
                    "INSERT OR REPLACE INTO bot_guilds (guild_id,name,icon,owner_id,"
                    "member_count,synced_at) VALUES (?,?,?,?,?,?)",
                    (g.id, g.name, None, g.owner_id, g.member_count,
                     datetime.now(timezone.utc).isoformat()))
            await db.commit()

    guilds = [FakeGuild()]
    await seed_guilds(guilds)
    bot = FakeBot(guilds)
    coord = GuildLifecycleCoordinator(bot)

    # ---- baseline: a healthy READY grants authorization ----
    bot.gateway_coordinator = coord
    coord.gateway_transport_verified = True
    ok = await coord.reconcile_guilds()
    allowed, state = await authorize()
    check("healthy READY authorizes the operator", ok and allowed, f"state={state}")

    # ---- 1. plain disconnect re-invalidates (fail-closed still works) ----
    await coord.on_disconnect_async()
    allowed, state = await authorize()
    check("disconnect re-invalidates authorization (fail-closed preserved)",
          not allowed, f"state={state}")

    # ---- 2. recovery after disconnect via RESUME path ----
    coord.gateway_transport_verified = True       # main.py on_resumed
    await coord.reconcile_guilds()
    allowed, state = await authorize()
    check("reconnect (reconcile) restores authorization", allowed, f"state={state}")

    # ---- 3. THE RACE: on_disconnect's pre-await half runs, then a resumed
    # reconcile re-verifies the SAME connection, then the disconnect coroutine
    # finally reaches its locked half. Before the fix that last step stamped
    # guilds_synced='0' over a live, re-verified session and nothing restored it.
    coord.handle_disconnect()                     # pre-await half (epoch bumps)
    # a subsequent call inside on_disconnect_async must not double-bump, so we
    # exercise exactly the ordering the shipped code can produce.
    _real_handle_disconnect = type(coord).handle_disconnect
    coord.handle_disconnect = lambda: None
    coord.gateway_transport_verified = True       # main.py on_resumed
    resumed_ok = await coord.reconcile_guilds()   # resume wins the lock
    allowed_after_resume, _ = await authorize()
    check("resume re-authorizes before the delayed disconnect lands",
          resumed_ok and allowed_after_resume,
          f"resumed={resumed_ok} authorized={allowed_after_resume}")

    await coord.on_disconnect_async()             # stale locked half runs now
    allowed, state = await authorize()
    check("stale disconnect does not revoke a re-verified session (race fixed)",
          allowed, f"state={state}")
    # restore the real pre-await half for the fail-closed checks below
    del coord.handle_disconnect

    # ---- 4. genuine disconnect still invalidates after the fix ----
    await coord.on_disconnect_async()
    allowed, _ = await authorize()
    check("genuine disconnect still invalidates (no weakening)",
          not allowed, "guilds_synced reset")

    # ---- 4b. verified-but-NOT-reconciled must still invalidate (no weakening).
    # main.py sets gateway_transport_verified at the top of on_ready, before
    # reconcile_guilds() has proven anything, so that flag alone must not be
    # allowed to suppress the invalidation.
    bot._open = True                            # the client is live again
    coord.gateway_transport_verified = True
    await coord.reconcile_guilds()
    allowed, _ = await authorize()
    check("precondition: authorized before the transport-only disconnect", allowed)
    coord.gateway_transport_verified = True     # on_ready top, no reconcile yet
    coord.guild_membership_reconciled = False   # nothing proven this epoch
    await coord.on_disconnect_async()
    allowed, state = await authorize()
    check("transport-verified alone does NOT suppress invalidation",
          not allowed, f"state={state}")

    # ---- 5. heartbeat refreshes the lease but never resurrects authorization ----
    coord.gateway_transport_verified = True
    await coord.reconcile_guilds()
    await coord.heartbeat()
    allowed, _ = await authorize()
    check("heartbeat alone keeps a verified session alive", allowed)
    await database.invalidate_bot_sync_state()
    await coord.heartbeat()          # lease refreshed, guilds_synced still 0
    allowed, state = await authorize()
    check("heartbeat alone cannot resurrect a revoked authorization",
          not allowed, f"state={state} (must require a reconcile)")

    # ---- 6. lease expiry is still enforced (TTL not raised) ----
    await coord.reconcile_guilds()
    allowed, _ = await authorize()
    check("baseline authorized before simulated lease staleness", allowed)
    async with aiosqlite.connect(DB) as db:
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        await db.execute("UPDATE bot_sync_state SET updated_at=? WHERE key=?",
                         (old, "bot_sync_lease"))
        await db.commit()
    allowed, state = await authorize()
    check("a stale lease still denies (45 s TTL unchanged)", not allowed, f"state={state}")

    # ---- 7. a fresh heartbeat recovers from staleness ----
    await coord.heartbeat()
    allowed, state = await authorize()
    check("next heartbeat restores access after lease staleness", allowed, f"state={state}")

    # ---- 8. bot genuinely gone (closed / not ready) -> must deny ----
    bot._open = False
    await database.invalidate_bot_sync_state()
    await coord.heartbeat()
    allowed, state = await authorize()
    check("a closed client cannot be authorized", not allowed, f"state={state}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} checks passed =====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} :: {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
