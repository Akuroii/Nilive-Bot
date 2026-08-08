"""
Clone the production database into the staging database (or any two
explicit paths) using SQLite's online backup API — safe to run while
the production bot/dashboard are live and actively writing (WAL mode),
unlike a plain file copy which can capture the main file and WAL out
of sync and produce a corrupt copy. Same technique cogs/backup.py
already uses for its own automated backups.

Usage:
    python3 scripts/clone_to_staging.py
    python3 scripts/clone_to_staging.py --source /custom/prod.db --dest /custom/staging.db
    python3 scripts/clone_to_staging.py --force   # overwrite an existing staging file

Deliberately does NOT read NERO_ENVIRONMENT / DATABASE_PATH / import
database.py — this script always targets the two well-known default
paths (mirrored below, kept in sync with database.py's
PROD_DB_DEFAULT_PATH / STAGING_DB_DEFAULT_PATH) unless both are
overridden with --source/--dest. That means running this script can
never accidentally pick up whatever DB_PATH your current shell
happens to be pointed at.
"""
import argparse
import asyncio
import os
import sys

import aiosqlite

# Mirrors database.py's PROD_DB_DEFAULT_PATH / STAGING_DB_DEFAULT_PATH.
# Kept as plain literals here (not imported) so this script has zero
# dependency on database.py's environment-resolution/safety-exit logic
# — see module docstring.
PROD_DB_DEFAULT_PATH = "/app/data/nero.db"
STAGING_DB_DEFAULT_PATH = "/app/data/nero_staging.db"


async def clone(source: str, dest: str, force: bool) -> None:
    source = os.path.abspath(source)
    dest = os.path.abspath(dest)

    if not os.path.exists(source):
        print(f"FATAL: source database not found: {source}")
        sys.exit(1)

    if source == dest:
        print("FATAL: source and destination are the same path — refusing to clone onto itself.")
        sys.exit(1)

    if os.path.exists(dest) and not force:
        print(f"FATAL: destination already exists: {dest}")
        print("  -> Pass --force to overwrite it, or pick a different --dest.")
        sys.exit(1)

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    if os.path.exists(dest):
        os.remove(dest)
        for suffix in ("-wal", "-shm"):
            stale = dest + suffix
            if os.path.exists(stale):
                os.remove(stale)

    print(f"Cloning:\n  source: {source}\n  dest:   {dest}")
    async with aiosqlite.connect(source) as src:
        async with aiosqlite.connect(dest) as dst:
            await src.backup(dst)

    size = os.path.getsize(dest)
    print(f"Done — {size:,} bytes written to {dest}")
    print("This is a one-time snapshot, not a live link — re-run this script")
    print("whenever you want staging to reflect a fresher copy of production.")


def main():
    parser = argparse.ArgumentParser(
        description="Clone the production SQLite DB into a staging copy.")
    parser.add_argument("--source", default=PROD_DB_DEFAULT_PATH,
                         help=f"Path to clone FROM (default: {PROD_DB_DEFAULT_PATH})")
    parser.add_argument("--dest", default=STAGING_DB_DEFAULT_PATH,
                         help=f"Path to clone TO (default: {STAGING_DB_DEFAULT_PATH})")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite --dest if it already exists")
    args = parser.parse_args()
    asyncio.run(clone(args.source, args.dest, args.force))


if __name__ == "__main__":
    main()
