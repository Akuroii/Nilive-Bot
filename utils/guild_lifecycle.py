import asyncio

from utils.db_config import DB_PATH
from database import (
    sync_bot_guilds,
    invalidate_bot_sync_state,
    restore_bot_sync_state,
    touch_bot_sync_lease,
)


def _guild_row(guild) -> dict:
    """bot.guilds entry -> sync_bot_guilds() row. Unavailable guilds carry no metadata."""
    if getattr(guild, "unavailable", False):
        return {"guild_id": guild.id, "name": None, "icon": None, "owner_id": 0, "member_count": 0}
    icon = guild.icon
    return {
        "guild_id": guild.id,
        "name": guild.name,
        "icon": (getattr(icon, "key", None) if hasattr(icon, "key") else str(icon)) if icon else None,
        "owner_id": guild.owner_id or 0,
        "member_count": getattr(guild, "member_count", 0) or 0,
    }


class GuildLifecycleCoordinator:
    """
    Coordinates bot Gateway lifecycle events, membership mutations,
    authoritative state reconciliation, and persisted dashboard authorization.

    Invariants enforced:
    1. Authorization readiness (guilds_synced = '1') is written ONLY by this class, and only
       when can_restore_authorization() holds: Gateway transport verified (READY/RESUMED with
       an open socket), membership reconciled, zero pending mutations, no unreconciled failure.
    2. A failed or cancelled mutation, or a failed reconcile, flags has_unreconciled_failure
       and leaves authorization invalid until heartbeat() completes a clean reconciliation.
    3. pending_mutations is decremented exactly once per mutation, on every exit path.
    4. A disconnect invalidates in-memory state immediately and the database state under the
       lock, so it is ordered after any in-flight restore. A reconcile that overlaps a
       disconnect (connection_epoch changed) never marks membership reconciled.
    """

    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.pending_mutations: int = 0
        self.connection_epoch: int = 0
        self.has_unreconciled_failure: bool = False
        self.gateway_transport_verified: bool = False
        self.guild_membership_reconciled: bool = False

    def can_restore_authorization(self) -> bool:
        return (
            self.gateway_transport_verified
            and self.guild_membership_reconciled
            and not self.has_unreconciled_failure
            and self.pending_mutations == 0
            and bool(getattr(getattr(self.bot, "ws", None), "open", False))
            and self.bot.is_ready()
            and not self.bot.is_closed()
        )

    def can_touch_lease(self) -> bool:
        """Heartbeat eligibility: fully verified, reconciled, and not mutating."""
        return self.can_restore_authorization() and not self.lock.locked()

    def handle_disconnect(self):
        """Immediately invalidates in-memory transport and reconciliation state."""
        self.gateway_transport_verified = False
        self.guild_membership_reconciled = False
        self.connection_epoch += 1

    async def on_disconnect_async(self, db_path: str = DB_PATH):
        self.handle_disconnect()
        async with self.lock:
            await invalidate_bot_sync_state(db_path)

    async def execute_mutation(self, mutation_coro, db_path: str = DB_PATH):
        self.pending_mutations += 1
        counted = True
        try:
            async with self.lock:
                await invalidate_bot_sync_state(db_path)
                await mutation_coro()

                self.pending_mutations -= 1
                counted = False
                if self.can_restore_authorization():
                    await restore_bot_sync_state(db_path)
        except BaseException:
            self.has_unreconciled_failure = True
            self.guild_membership_reconciled = False
            raise
        finally:
            if counted:
                self.pending_mutations -= 1

    async def reconcile_guilds(self, db_path: str = DB_PATH) -> bool:
        """
        Authoritative full synchronization of bot.guilds into SQLite, then restoration of
        authorization if every invariant holds. Returns True when membership was reconciled
        for the current connection; False on failure or when the connection changed during
        the sync (a later READY/RESUMED reconciles again). Never raises except cancellation.
        """
        epoch = self.connection_epoch
        async with self.lock:
            try:
                await sync_bot_guilds(
                    [_guild_row(g) for g in self.bot.guilds],
                    authoritative=True, db_path=db_path,
                )
                if epoch != self.connection_epoch:
                    return False
                self.guild_membership_reconciled = True
                self.has_unreconciled_failure = False
                if self.can_restore_authorization():
                    await restore_bot_sync_state(db_path)
                return True
            except BaseException as exc:
                self.guild_membership_reconciled = False
                self.has_unreconciled_failure = True
                try:
                    await invalidate_bot_sync_state(db_path)
                except Exception:
                    pass  # state is already fail-closed: flags above, and the lease expires
                if not isinstance(exc, Exception):
                    raise
                print(f"[GATEWAY] Guild reconciliation failed: {type(exc).__name__}: {exc}")
                return False

    async def heartbeat(self, db_path: str = DB_PATH):
        """
        One heartbeat tick: recover from an unreconciled failure with a fresh authoritative
        reconcile, otherwise refresh the lease. Lease refresh alone never grants access
        (guilds_synced must also be '1').
        """
        if (self.has_unreconciled_failure and self.gateway_transport_verified
                and self.pending_mutations == 0):
            await self.reconcile_guilds(db_path)
        elif self.can_touch_lease():
            await touch_bot_sync_lease(db_path)
