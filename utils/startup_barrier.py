import os
import sys
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from utils.db_config import DB_PATH


def run_barrier(db_path: str = DB_PATH) -> bool:
    """
    Startup Barrier: ensures database connectivity and fail-closed synchronization
    readiness state before service launch and immediately upon process termination.

    Invariants guaranteed upon clean exit:
    1. Database schema exists and is accessible.
    2. guilds_synced = '0' (persisted authorization disabled).
    3. bot_sync_lease is purged, ensuring no stale lease can be honored.
    """
    try:
        # Ensure parent directory exists
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        now = datetime.now(timezone.utc).isoformat()
        # sqlite3's own context manager only commits; closing() actually releases the connection.
        with closing(sqlite3.connect(db_path, timeout=10.0)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_guilds (
                    guild_id        INTEGER PRIMARY KEY,
                    name            TEXT NOT NULL DEFAULT 'Unknown Server',
                    icon            TEXT,
                    owner_id        INTEGER DEFAULT 0,
                    member_count    INTEGER DEFAULT 0,
                    synced_at       TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_sync_state (
                    key             TEXT PRIMARY KEY,
                    value           TEXT,
                    updated_at      TEXT NOT NULL
                )
            """)

            # Invalidate readiness
            conn.execute("""
                INSERT INTO bot_sync_state (key, value, updated_at)
                VALUES ('guilds_synced', '0', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = '0',
                    updated_at = excluded.updated_at
            """, (now,))

            # Purge any existing lease timestamp
            conn.execute("DELETE FROM bot_sync_state WHERE key = 'bot_sync_lease'")
            conn.commit()

        print(f"[BARRIER] Successfully initialized barrier at {db_path} (guilds_synced='0', lease purged)")
        return True
    except Exception as e:
        print(f"[BARRIER] FATAL: Startup barrier failed on {db_path}: {e}", file=sys.stderr)
        return False


def main():
    success = run_barrier()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
