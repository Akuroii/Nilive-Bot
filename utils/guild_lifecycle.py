import asyncio
from datetime import datetime, timezone
import discord
from utils.db_config import DB_PATH
from database import (
    sync_bot_guilds,
    invalidate_bot_sync_state,
    restore_bot_sync_state,
)


class GuildLifecycleCoordinator:
    """
    Coordinates bot Gateway lifecycle events, membership mutations,
    authoritative state reconciliation, and persisted dashboard authorization.

    Invariants enforced:
    1. Authorization readiness (guilds_synced = '1') is restored ONLY when:
       - Gateway transport is verified (completed READY or RESUMED handshake with active socket)
       - Guild membership database is authoritatively reconciled
       - Zero mutations are pending (_pending_mutations == 0)
       - No prior unhandled failure remains unreconciled (_has_unreconciled_failure == False)
    2. Any failed or cancelled mutation flags _has_unreconciled_failure = True and keeps
       authorization invalid until a clean authoritative reconciliation succeeds.
    3. Multi-stage cancellation (lock wait, invalidation, mutation, restoration) is explicitly
       trapped and re-raised without leaking lock state or restoring invalid authorization.
    4. Generation fencing prevents an older completing restoration from overwriting a newer invalidation.
    """

    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.pending_mutations: int = 0
        self.sync_generation: int = 0
        self.has_unreconciled_failure: bool = False
        self.gateway_transport_verified: bool = False
        self.guild_membership_reconciled: bool = False

    def can_restore_authorization(self) -> bool:
        ws_open = getattr(getattr(self.bot, "ws", None), "open", False) if self.bot else True
        is_ready = self.bot.is_ready() if self.bot else True
        not_closed = not self.bot.is_closed() if self.bot else True
        return (
            self.gateway_transport_verified
            and self.guild_membership_reconciled
            and not self.has_unreconciled_failure
            and self.pending_mutations == 0
            and ws_open
            and is_ready
            and not_closed
        )

    def can_touch_lease(self) -> bool:
        """Heartbeat eligibility check: true only when fully verified, reconciled, and not mutating."""
        return self.can_restore_authorization() and not self.lock.locked()

    def handle_disconnect(self):
        """Immediately invalidates in-memory transport and reconciliation state."""
        self.gateway_transport_verified = False
        self.guild_membership_reconciled = False

    async def on_disconnect_async(self, db_path: str = DB_PATH):
        self.handle_disconnect()
        await invalidate_bot_sync_state(db_path)

    async def execute_mutation(self, mutation_coro, db_path: str = DB_PATH):
        self.pending_mutations += 1
        self.sync_generation += 1
        gen = self.sync_generation
        lock_acquired = False

        try:
            # Stage 1: Lock acquisition (cancellation-safe)
            await self.lock.acquire()
            lock_acquired = True

            # Stage 2: Invalidation inside lock
            await invalidate_bot_sync_state(db_path)

            # Stage 3: Mutation execution
            await mutation_coro()

            # Mutation body succeeded: decrement pending count
            self.pending_mutations -= 1

            # Stage 4: Restoration inside lock with generation fence
            if (
                self.pending_mutations == 0
                and self.sync_generation == gen
                and self.can_restore_authorization()
            ):
                await restore_bot_sync_state(db_path)

        except (asyncio.CancelledError, BaseException) as exc:
            self.has_unreconciled_failure = True
            self.guild_membership_reconciled = False
            if self.pending_mutations > 0:
                self.pending_mutations -= 1
            raise exc
        finally:
            if lock_acquired:
                self.lock.release()

    async def reconcile_guilds(self, db_path: str = DB_PATH, simulate_failure: bool = False) -> bool:
        """
        Executes an authoritative full synchronization of bot.guilds into SQLite.
        Clears any prior unreconciled failure flag if the transaction commits cleanly.
        """
        lock_acquired = False
        try:
            await self.lock.acquire()
            lock_acquired = True

            if simulate_failure:
                self.guild_membership_reconciled = False
                self.has_unreconciled_failure = True
                await invalidate_bot_sync_state(db_path)
                return False

            if not self.bot:
                guilds = []
            else:
                guilds = [
                    {
                        "guild_id": guild.id,
                        "name": guild.name if not getattr(guild, "unavailable", False) else None,
                        "icon": (getattr(guild.icon, "key", None) if hasattr(guild.icon, "key") else (str(guild.icon) if guild.icon else None)) if not getattr(guild, "unavailable", False) else None,
                        "owner_id": (guild.owner_id or 0) if not getattr(guild, "unavailable", False) else 0,
                        "member_count": (getattr(guild, "member_count", 0) or 0) if not getattr(guild, "unavailable", False) else 0,
                    }
                    for guild in self.bot.guilds
                ]

            await sync_bot_guilds(guilds, authoritative=True, db_path=db_path)
            self.guild_membership_reconciled = True
            self.has_unreconciled_failure = False

            if self.can_restore_authorization():
                await restore_bot_sync_state(db_path)
            return True
        except (asyncio.CancelledError, BaseException) as exc:
            self.guild_membership_reconciled = False
            self.has_unreconciled_failure = True
            await invalidate_bot_sync_state(db_path)
            raise exc
        finally:
            if lock_acquired:
                self.lock.release()
