"""Durable provenance, operation journal and cross-process coordination.

Everything in this module exists to make level/reward corrections *reversible
without guessing* and *repeatable without double-spending*. It is the storage and
state-machine layer only - no Discord objects, no command logic.

Why a new module instead of more queries in the reset tool
----------------------------------------------------------
Four defects were reproduced against the previous design, and all four come from
the same root cause: the reset inferred what a reward had delivered from
*aggregate state* (a wallet balance, an inventory quantity, a claim row) instead
of from a record of the grant itself, and it performed side effects with no
durable marker of having done so.

  A. Retrying a reset removed the same inventory twice. `remove_item()` commits
     its own transaction; the claim-delete transaction ran afterwards, so a
     failure between them left the items gone, the claim row present, and nothing
     recording that the reversal had already happened.
  B. Unrelated inventory could be removed. "held >= granted" is not provenance:
     a member who used two reward potions and later earned five from a quest
     holds five, and the reset took two of the quest's.
  C. Two rewards granting the same item were each judged against the whole stack,
     so 2x + 2x was called reversible with 3 held.
  D. `utils.ledger.reverse_transaction()` debits the wallet in one connection,
     marks the original row `reversed=1` in a second and appends a compensating
     row in a third. A crash between them leaves a debited wallet with an
     unmarked ledger row, so a retry debits again. It also writes TWO rows for
     ONE wallet movement (safe_deduct's `deduct` plus its own `reversal`), which
     double-counts the event for anyone summing the ledger (defect E).

The shape of the fix follows standard ledger practice rather than inventing one:

* **Append-only grants.** Every level-reward delivery writes a `reward_grants`
  row inside the claim engine's own transaction, carrying the claim id, the exact
  quantity and a `remaining_quantity` counter. Consumption anywhere decrements
  that counter FIFO, so "how many of the units this reward granted are still
  attributable" is a recorded fact, not an inference from a shared stack.
* **Reversal journal with a uniqueness constraint.** `reward_reversals` has
  `UNIQUE(guild_id, user_id, claim_id, kind)`. A reversal is written in the SAME
  transaction as the movement it reverses, so the journal and the data cannot
  disagree, and a second attempt hits the constraint instead of moving value
  again. This is the idempotency key that makes retries safe (A, C, D).
* **One compensating row per movement.** `reverse_currency()` debits the wallet,
  marks the original ledger row and appends exactly one `reversal` row in one
  `BEGIN IMMEDIATE` transaction (D, E).
* **Refuse, don't invent.** When provenance does not exist (every grant made
  before this module, or any item whose provenance an absolute `set_quantity`
  write destroyed) the reversal is recorded as `refused` with the reason and the
  claim row is kept. Nothing is removed on a guess.
* **A database-backed lock, not a Python one.** The bot and the dashboard are
  separate processes over one SQLite file, so `level_op_locks` is a lease table
  keyed on (guild, user) with an expiry sampled *inside* the write transaction.
  A crashed holder's lease expires; a live one blocks a concurrent claim pass.

Environment verdicts live here too, because "is this the production database?"
must be answered from the file actually open, not from one string comparison -
see `environment_verdict()`.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import DB_PATH

# One row per administrative operation, so a partial failure is traceable and a
# retry can be tied to the run that started it.
OP_RESET = "level_test_reset"
OP_ROLLBACK = "xp_rollback"
OP_RECONCILE = "level_reconcile"
OP_SETXP = "setxp"
OP_SETLEVEL = "setlevel"
OP_DASHBOARD = "dashboard_xp"

# Reversal outcomes. `applied` is the only one that means value moved.
STATUS_APPLIED = "applied"
STATUS_ALREADY = "already_reversed"
STATUS_OUTSTANDING = "outstanding"     # proven, but the value is gone
STATUS_REFUSED = "refused"             # not provable, nothing attempted

KIND_CURRENCY = "currency"
KIND_INVENTORY = "inventory"
KIND_ROLE = "role"
# An XP-boost reward's effect is one row in `leveling_active_boosts` keyed by
# `level_claim:<claim id>`, so it can be named and removed exactly. It is a
# journal kind rather than a wallet movement: the value is elapsed time.
KIND_BOOST = "boost"

# How long an operation may hold a member's lock. Long enough to cover several
# Discord API round-trips, short enough that a crashed process does not wedge the
# member forever. Sampled inside the write transaction, never before it.
LOCK_TTL_SECONDS = 90

_TABLES_READY = False


class LevelOpsError(Exception):
    """Raised when an operation must not proceed. Always carries the reason."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expiry(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds")


def _new_fence_epoch() -> str:
    """A clock value that can only move forward, written inside a transaction.

    Milliseconds, unlike the second-resolution lease timestamps: two fences in
    the same second must still be totally ordered against a deferral sampled
    between them, or `xp_write_barrier` would accept a stale attempt.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = (
    # One row per administrative operation (reset, rollback, reconcile, XP set).
    """
    CREATE TABLE IF NOT EXISTS level_ops (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        kind         TEXT NOT NULL,
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        actor_id     INTEGER,
        reason       TEXT,
        status       TEXT NOT NULL DEFAULT 'running',
        detail_json  TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_level_ops_member ON level_ops(guild_id, user_id, id)",
    # Every administrative XP change, with the reward levels it crossed. This is
    # what makes an erroneous grant identifiable and its consequences reversible.
    """
    CREATE TABLE IF NOT EXISTS xp_mutations (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id          TEXT NOT NULL UNIQUE,
        guild_id              INTEGER NOT NULL,
        user_id               INTEGER NOT NULL,
        source                TEXT NOT NULL,
        actor_id              INTEGER,
        reason                TEXT,
        xp_before             INTEGER NOT NULL,
        xp_after              INTEGER NOT NULL,
        level_before          INTEGER,
        level_after           INTEGER,
        delta                 INTEGER NOT NULL,
        crossings_json        TEXT,
        reverses_operation_id TEXT,
        rolled_back           INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_xp_mut_member ON xp_mutations(guild_id, user_id, id)",
    # What each level reward actually granted, and how much of it is still
    # attributable to that grant. Written by the claim engine, decremented FIFO
    # by consumption, zeroed by a reversal.
    """
    CREATE TABLE IF NOT EXISTS reward_grants (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        grant_id           TEXT NOT NULL UNIQUE,
        guild_id           INTEGER NOT NULL,
        user_id            INTEGER NOT NULL,
        claim_id           INTEGER,
        reward_level       INTEGER,
        track              TEXT NOT NULL,
        kind               TEXT NOT NULL,
        item_name          TEXT,
        currency           TEXT,
        ledger_id          INTEGER,
        quantity           INTEGER NOT NULL,
        remaining_quantity INTEGER NOT NULL,
        unattributed       INTEGER NOT NULL DEFAULT 0,
        unattributed_reason TEXT,
        operation_id       TEXT,
        created_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rg_member ON reward_grants(guild_id, user_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_rg_item ON reward_grants(guild_id, user_id, item_name)",
    # The reversal journal. UNIQUE(guild, user, claim, kind) is the idempotency
    # key: one claim's currency can be reversed once, its inventory once.
    """
    CREATE TABLE IF NOT EXISTS reward_reversals (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        reversal_id  TEXT NOT NULL UNIQUE,
        operation_id TEXT NOT NULL,
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        claim_id     INTEGER,
        grant_id     TEXT,
        kind         TEXT NOT NULL,
        currency     TEXT,
        item_name    TEXT,
        quantity     INTEGER NOT NULL,
        ledger_id    INTEGER,
        status       TEXT NOT NULL,
        reason       TEXT,
        created_at   TEXT NOT NULL,
        UNIQUE (guild_id, user_id, claim_id, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rr_member ON reward_reversals(guild_id, user_id, id)",
    # Per-member advisory lease. The bot and the dashboard are separate processes
    # over one SQLite file, so an in-process lock cannot coordinate them.
    """
    CREATE TABLE IF NOT EXISTS level_op_locks (
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        holder       TEXT NOT NULL,
        acquired_at  TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        fence_epoch  TEXT,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    # One row per ordinary-XP award attempted while a correction lease was live.
    # It is written inside the award's own write transaction, and its
    # `deferral_epoch` is the wall clock at that moment. Because the fence and
    # the award need the same SQLite write lock, the comparison with the lease's
    # `fence_epoch` is total: a row older than the fence raced it (counted, and
    # the correction re-runs), a row not older than the fence is refused. Rows
    # exist only while a lease does - `acquire_lock` clears them.
    """
    CREATE TABLE IF NOT EXISTS level_xp_deferrals (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id       INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        deferral_epoch TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
        created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_xp_deferrals_member "
    "ON level_xp_deferrals(guild_id, user_id, id)",
    # A ONE-SHOT XP award that could not be written yet.
    #
    # Message and voice XP are recurring: when the fence refuses them the XP was
    # never written, so the next message or voice minute simply earns it again
    # and nothing is lost. A mission, tag mission, minigame, event or tag-partner
    # award is different - the caller has already committed the work that earned
    # it (a mission row marked `completed`, a minigame round consumed), so a
    # refused award is a reward that can never be re-earned. Dropping it on a
    # timeout silently deletes a benefit the member earned.
    #
    # Such an award is therefore RECORDED before it is attempted and retried
    # until it lands. `idem_key` is the identity of the completed unit of work,
    # so re-delivering the same completion (a listener firing twice, a restart
    # replaying an event) can never pay twice. Rows survive process restarts -
    # that is the point - and are cleared by `drain_xp_grants`.
    """
    CREATE TABLE IF NOT EXISTS level_xp_pending_grants (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        idem_key   TEXT NOT NULL UNIQUE,
        amount     INTEGER NOT NULL,
        source     TEXT,
        reason     TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',
        attempts   INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        applied_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_xp_pending_member "
    "ON level_xp_pending_grants(guild_id, user_id, status)",
)


async def ensure_tables(db: aiosqlite.Connection | None = None) -> None:
    """Create the tables if they are missing. Safe to call often.

    Called from `database.init_db()` for a normal boot and lazily by every entry
    point here, so a database created before this module existed is migrated the
    first time anything touches it rather than failing with "no such table".
    """
    global _TABLES_READY

    async def create(connection):
        for statement in SCHEMA:
            await connection.execute(statement)
        # `fence_epoch` was added after the lease table shipped. The CREATE above
        # is a no-op on an existing table, so the column is added explicitly; a
        # database that already has it fails with "duplicate column" and that is
        # the only error tolerated here.
        cursor = await connection.execute("PRAGMA table_info(level_op_locks)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "fence_epoch" not in columns:
            try:
                await connection.execute(
                    "ALTER TABLE level_op_locks ADD COLUMN fence_epoch TEXT")
            except aiosqlite.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    if db is not None:
        await create(db)
        return
    if _TABLES_READY:
        return
    async with aiosqlite.connect(DB_PATH, timeout=10) as own:
        await create(own)
        await own.commit()
    _TABLES_READY = True


def invalidate_tables_cache() -> None:
    """Forget that the tables were created, so the next call re-checks.

    `ensure_tables()` memoises its success to keep DDL off every hot path. That is
    right for a database file that stays put, and wrong the moment one does not:
    a restore, a staging copy or a migration that drops and recreates a table
    leaves the memo claiming tables that are no longer there. Callers that hit
    "no such table" invalidate it and retry once, which is what a fresh process
    against the same file would do anyway.
    """
    global _TABLES_READY
    _TABLES_READY = False


def new_operation_id(kind: str) -> str:
    """An immutable, unique id for one administrative operation."""
    return f"{kind}-{uuid.uuid4().hex[:16]}"


# ───────────────────────── environment / target safety ─────────────────────────

# database.py resolves the database file like this:
#     NERO_ENVIRONMENT = getenv("NERO_ENVIRONMENT", "production").strip().lower()
#     DB_PATH = STAGING_DATABASE_PATH if NERO_ENVIRONMENT == "staging" else DATABASE_PATH
# Only the exact string "staging" selects the staging file. EVERY other value -
# "prod", "dev", "development", "test", "stagin", "" - selects the production
# file. A guard written as `if NERO_ENVIRONMENT == "production": refuse` therefore
# lets `NERO_ENVIRONMENT=dev` run a destructive reset against production.
STAGING_WORDS = frozenset({"staging", "stage", "stg"})
PRODUCTION_WORDS = frozenset({"production", "prod", "live"})


def environment_verdict() -> dict:
    """Which database is actually open, decided from the paths - not the label.

    Returns a dict with `target` of "staging", "production" or "ambiguous", plus
    the evidence. Destructive operations must require `target == "staging"`, or
    an explicit production authorization that matches the resolved path. Anything
    ambiguous fails closed.
    """
    env_raw = os.getenv("NERO_ENVIRONMENT", "")
    env = (env_raw or "").strip().lower()
    from database import DB_PATH as live_path
    staging_path = os.getenv("STAGING_DATABASE_PATH", "/app/data/nero_staging.db")
    production_path = os.getenv("DATABASE_PATH", "/app/data/nero.db")
    live = os.path.abspath(live_path)

    if env in STAGING_WORDS:
        label = "staging"
    elif env in PRODUCTION_WORDS:
        label = "production"
    elif env == "":
        # database.py defaults an unset key to production.
        label = "production (NERO_ENVIRONMENT unset)"
    else:
        label = f"unrecognised ({env_raw!r})"

    # Two independent facts have to AGREE before anything destructive is allowed:
    # the label the operator set, and the file the process actually has open. One
    # of them alone is not evidence - `NERO_ENVIRONMENT` is bypassable (only the
    # exact string "staging" selects the staging file, so `dev`, `test`,
    # `development` or a typo all silently select the production one), and a path
    # on its own cannot say what the operator believed they were pointed at.
    #
    #   label staging  + live == staging path (and the two paths differ) -> staging
    #   label prod/unset + live == production path                     -> production
    #   anything else - a contradictory pair, a path matching neither
    #   configured file, or an unrecognised label                      -> ambiguous
    #
    # Ambiguous is refused even with the production authorization key: the key
    # says "yes, do this on production", and if the code cannot prove which file
    # that is, honouring the key would be a guess about what it authorizes.
    staging_abs = os.path.abspath(staging_path)
    production_abs = os.path.abspath(production_path)
    same_file = staging_abs == production_abs
    if env in STAGING_WORDS:
        if same_file:
            # database.py refuses to BOOT this combination, so a process running
            # with it is not in a configuration any deploy can vouch for.
            target = "ambiguous"
        elif live == staging_abs:
            target = "staging"
        else:
            # Labelled staging, but the open file is the production one or neither
            # configured path. The label is not evidence that the data is safe.
            target = "ambiguous"
    elif env in PRODUCTION_WORDS or env == "":
        # An unset variable is not a contradiction: database.py documents it as
        # the production default, and the file confirms it.
        target = "production" if live == production_abs else "ambiguous"
    else:
        # "dev", "test", "development", "local", "stagin": no supported deploy
        # sets these, and database.py resolves every one of them to the
        # production file. Refusing here is what closes the bypass.
        target = "ambiguous"

    return {
        "target": target,
        "label": label,
        "env_raw": env_raw,
        "db_path": live,
        "staging_path": os.path.abspath(staging_path),
        "production_path": os.path.abspath(production_path),
    }


def require_destructive_allowed(*, production_authorization: bool = False,
                                operation: str = "this operation") -> dict:
    """Fail closed unless the target database is provably the staging one.

    `production_authorization` is the caller's explicit, validated second key. It
    is honoured only when the open file really is the production database - an
    authorization cannot turn an *ambiguous* target into a permitted one, because
    ambiguous means the code cannot tell which data it is about to destroy.
    """
    verdict = environment_verdict()
    target = verdict["target"]
    if target == "staging":
        return verdict
    if target == "production":
        if production_authorization:
            return verdict
        raise LevelOpsError(
            f"{operation} refuses to run against the production database "
            f"({verdict['db_path']}) without explicit authorization. Environment: "
            f"{verdict['label']}. Run the preview first, or point the deploy at a "
            f"staging database with NERO_ENVIRONMENT=staging and a separate "
            f"STAGING_DATABASE_PATH.")
    raise LevelOpsError(
        f"{operation} refuses to run: the environment is ambiguous, so the code "
        f"cannot prove which database it would destroy. NERO_ENVIRONMENT="
        f"{verdict['env_raw']!r} resolves to {verdict['db_path']}, which is "
        f"neither the configured staging path nor the configured production path. "
        f"Set NERO_ENVIRONMENT explicitly (database.py treats only 'staging' as "
        f"staging; every other value selects the production file).")


# ─────────────────────────────── operation log ────────────────────────────────

async def open_operation(operation_id: str, kind: str, guild_id: int,
                         user_id: int, *, actor_id: int | None = None,
                         reason: str | None = None) -> None:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "INSERT INTO level_ops (operation_id, kind, guild_id, user_id, "
            "actor_id, reason, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'running',?,?)",
            (operation_id, kind, guild_id, user_id, actor_id, reason,
             _now(), _now()))
        await db.commit()


async def finish_operation(operation_id: str, status: str,
                           detail: dict | None = None) -> None:
    """Record the outcome. `status` is 'completed', 'partial' or 'failed'."""
    import json
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE level_ops SET status=?, detail_json=?, updated_at=? "
            "WHERE operation_id=?",
            (status, json.dumps(detail) if detail else None, _now(),
             operation_id))
        await db.commit()


async def list_operations(guild_id: int, user_id: int | None = None,
                          limit: int = 20) -> list[dict]:
    await ensure_tables()
    sql = ("SELECT operation_id, kind, guild_id, user_id, actor_id, reason, "
           "status, created_at, updated_at FROM level_ops WHERE guild_id=?")
    params: list = [guild_id]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        rows = await (await db.execute(sql, params)).fetchall()
    return [{"operation_id": r[0], "kind": r[1], "guild_id": r[2],
             "user_id": r[3], "actor_id": r[4], "reason": r[5], "status": r[6],
             "created_at": r[7], "updated_at": r[8]} for r in rows]


# ───────────────────────────────── locks ──────────────────────────────────────

async def acquire_lock(guild_id: int, user_id: int, *, holder: str,
                       operation_id: str,
                       ttl_seconds: int = LOCK_TTL_SECONDS,
                       db: aiosqlite.Connection | None = None) -> dict:
    """Take the per-member lease, or report who holds it.

    One `BEGIN IMMEDIATE` transaction: an expired lease is cleared and the new one
    written together, so two processes cannot both believe they hold it. The clock
    is sampled INSIDE the transaction - sampling it before `BEGIN IMMEDIATE` and
    then blocking on another writer is how a lease gets granted after it expired.

    ``db`` joins the caller's transaction instead of opening its own. That is what
    lets an administrative XP change write the new row and take the lease in ONE
    commit: after that commit every claim pass sees the fence, so there is no
    window in which a reward above the corrected level can be delivered before
    the correction reconciles it. The caller owns commit/rollback in that mode.
    """
    if db is None:
        await ensure_tables()

    async def run(connection) -> dict:
        now = _now()
        await connection.execute(
            "DELETE FROM level_op_locks WHERE guild_id=? AND user_id=? "
            "AND expires_at <= ?", (guild_id, user_id, now))
        cursor = await connection.execute(
            "SELECT operation_id, holder, expires_at FROM level_op_locks "
            "WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        existing = await cursor.fetchone()
        if existing:
            return {"ok": False, "holder": existing[1],
                    "operation_id": existing[0], "expires_at": existing[2],
                    "reason": (f"{existing[1]} operation {existing[0]} is "
                               f"already running for this member "
                               f"(lease until {existing[2]})")}
        # A fresh lease starts a fresh accounting window: deferral rows only
        # exist while some lease is live, and `fence_live_writers()` reports
        # "how many ordinary awards collided with THIS operation" by counting
        # them. Without this, every earlier correction for the member would be
        # counted again and force pointless re-runs.
        await connection.execute(
            "DELETE FROM level_xp_deferrals WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        await connection.execute(
            "INSERT INTO level_op_locks (guild_id, user_id, operation_id, "
            "holder, acquired_at, expires_at) VALUES (?,?,?,?,?,?)",
            (guild_id, user_id, operation_id, holder, now,
             _expiry(ttl_seconds)))
        return {"ok": True, "holder": holder, "operation_id": operation_id}

    if db is not None:
        return await run(db)
    async with aiosqlite.connect(DB_PATH, timeout=10) as own:
        await own.execute("BEGIN IMMEDIATE")
        try:
            result = await run(own)
            await own.commit()
            return result
        except Exception:
            await own.execute("ROLLBACK")
            raise


async def renew_lock(guild_id: int, user_id: int, operation_id: str,
                     ttl_seconds: int = LOCK_TTL_SECONDS) -> bool:
    """Extend a lease we still own. False if it expired and was taken."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            now = _now()
            cursor = await db.execute(
                "UPDATE level_op_locks SET expires_at=? "
                "WHERE guild_id=? AND user_id=? AND operation_id=? "
                "AND expires_at > ?",
                (_expiry(ttl_seconds), guild_id, user_id, operation_id, now))
            await db.commit()
            return cursor.rowcount == 1
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def release_lock(guild_id: int, user_id: int,
                       operation_id: str) -> bool:
    """Release only our own lease - a stolen/expired one is left alone."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "DELETE FROM level_op_locks WHERE guild_id=? AND user_id=? "
            "AND operation_id=?", (guild_id, user_id, operation_id))
        await db.commit()
        return cursor.rowcount == 1


async def lock_held(guild_id: int, user_id: int) -> dict | None:
    """The live lease on this member, if any. Expired leases do not count."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT operation_id, holder, expires_at FROM level_op_locks "
            "WHERE guild_id=? AND user_id=? AND expires_at > ?",
            (guild_id, user_id, _now()))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"operation_id": row[0], "holder": row[1], "expires_at": row[2]}


async def fence_live_writers(guild_id: int, user_id: int,
                             operation_id: str) -> dict:
    """Revoke every XP writer's permission to proceed on this member.

    A correction holds the member's lease while it talks to Discord and then
    writes SQLite. The lease alone does not stop a *non-administrative* XP
    writer: message XP, voice XP, a mission/event/minigame award and the
    dashboard's member edit all used to write `levels` without ever looking at
    it. Such an award can legitimately put the member back above a level the
    correction is busy reversing, after the correction read the level it is
    correcting TO (B1). The correction then deletes a reward the member is once
    again entitled to, and no claim row survives to restore it.

    The fence is the lease's clock, not a second mechanism: an XP writer samples
    the lease inside its own write transaction, and this call moves the lease's
    start ("fence epoch") and expiry forward in ONE commit. A writer that took
    its sample before this commit can no longer be mid-write afterwards - its
    transaction is serialized against this one - and a writer that starts after
    it sees the new epoch and defers. So after this returns, every XP writer has
    either finished its award before the correction's reads or is refusing.

    `deferred` is the count of ordinary awards that collided with this
    operation's lease - awards that either were refused (written after an earlier
    fence) or got in just before this one. Any of them may have re-earned a
    reward level, so a non-zero count means the caller must re-run its sweep
    against the state that includes them. The caller must hold `operation_id`'s
    lease, and must call this BEFORE the reads its correction decisions rest on.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            now = _now()
            cursor = await db.execute(
                "UPDATE level_op_locks SET fence_epoch=?, expires_at=? "
                "WHERE guild_id=? AND user_id=? AND operation_id=? "
                "AND expires_at > ?",
                (_new_fence_epoch(), _expiry(LOCK_TTL_SECONDS), guild_id,
                 user_id, operation_id, now))
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return {"ok": False, "deferred": None,
                        "reason": ("this operation's lease is no longer held "
                                   "for this member, so it must not continue")}
            # Deferral rows are deleted when the lease is taken (see
            # acquire_lock), so "rows present" is exactly "ordinary awards that
            # collided with THIS operation". Any of them may have re-earned a
            # reward level, which is why the caller re-runs its sweep.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM level_xp_deferrals "
                "WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            deferred = int((await cursor.fetchone())[0] or 0)
            await db.commit()
            return {"ok": True, "deferred": deferred}
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def xp_write_barrier(guild_id: int, user_id: int,
                           db: aiosqlite.Connection) -> str | None:
    """Record one ordinary-XP attempt, or say why it must not write.

    Called INSIDE the writer's own ``BEGIN IMMEDIATE`` transaction, BEFORE it
    reads `levels`. That placement is the whole correctness argument: the
    transaction already holds SQLite's single write lock, and
    ``fence_live_writers()`` needs that same lock before it can move the epoch.
    So the lease this function reads cannot change until the award commits:
    either the fence committed first (this read sees the new epoch and refuses),
    or this award commits first (the fence moves the epoch afterwards, counts
    the deferral, and the correction re-runs against state that includes the
    award). There is no interleaving in which an award is authorised by a lease
    the correction has already superseded.

    Returns None when the award may proceed, otherwise the human-readable
    reason to defer it. A deferral is not a lost award: the XP was never
    written, so the next message/voice tick earns it normally.
    """
    if not db.in_transaction:
        raise LevelOpsError(
            "xp_write_barrier must run inside the writer's own BEGIN IMMEDIATE "
            "transaction, otherwise its view of the lease can go stale between "
            "the read and the write it authorises")
    cursor = await db.execute(
        "SELECT operation_id, holder, fence_epoch, expires_at "
        "FROM level_op_locks WHERE guild_id=? AND user_id=? AND expires_at > ?",
        (guild_id, user_id, _now()))
    live = await cursor.fetchone()
    if not live:
        # The overwhelmingly common case: nobody is correcting this member, so
        # an ordinary award costs one indexed SELECT and writes no bookkeeping.
        return None
    operation_id, holder, epoch, expires_at = live
    # `lastrowid` rather than RETURNING: RETURNING needs SQLite 3.35+, and this
    # table is written on every ordinary award, so it must work on the oldest
    # sqlite the bot may boot against.
    cursor = await db.execute(
        "INSERT INTO level_xp_deferrals (guild_id, user_id) VALUES (?,?)",
        (guild_id, user_id))
    deferral_id = cursor.lastrowid
    cursor = await db.execute(
        "SELECT deferral_epoch FROM level_xp_deferrals WHERE id=?",
        (deferral_id,))
    own = await cursor.fetchone()
    sampled_before_fence = (epoch is not None and own is not None
                            and own[0] is not None and own[0] < epoch)
    if epoch is None or sampled_before_fence:
        # Either the correction has not fenced yet (so its reads are still ahead
        # of this write and will include it), or this attempt sampled STRICTLY
        # before the fence - in which case it is one of the awards that raced the
        # fence, it is counted in `fence_live_writers()["deferred"]`, and the
        # correction re-runs against the state that includes it.
        #
        # Anything sampled AT or AFTER the fence is after the correction's
        # snapshot and must not land: the sweep may already have deleted the
        # claim this award would re-earn.
        return None
    # Deliberately says nothing about what happens next: whether a deferred
    # award is re-earned depends on the caller, not on the fence. A recurring
    # award (message/voice XP) IS re-earned on the next tick; a one-shot award
    # would not be, and promising otherwise is how a lost reward looks like a
    # handled one. `give_reward` attaches the source-accurate sentence.
    return (f"A {holder} correction ({operation_id}) is in progress for this "
            f"member until {expires_at}; the XP award was deferred and nothing "
            f"was written")


# ──────────────────── durable one-shot XP awards (never lose a reward) ────────

async def enqueue_xp_grant(guild_id: int, user_id: int, amount: int, *,
                           idem_key: str, source: str | None = None,
                           reason: str | None = None) -> bool:
    """Record a one-shot XP award that must not be lost. Returns True if new.

    Call this BEFORE trying to write the XP, so the award is durable from the
    moment the work that earned it is committed. `INSERT OR IGNORE` on the
    UNIQUE `idem_key` is what makes the pair idempotent: a second caller naming
    the same completed mission / minigame round / event is a retry, not a second
    reward, so it must not enqueue (and therefore not pay) twice.

    Returns False when the key was already known - already granted, or already
    queued and waiting for the fence to lift.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO level_xp_pending_grants "
                "(guild_id, user_id, idem_key, amount, source, reason, status, "
                " attempts, created_at) VALUES (?,?,?,?,?,?,'pending',0,?)",
                (int(guild_id), int(user_id), str(idem_key), int(amount),
                 source, reason, _now()))
            created = cursor.rowcount == 1
            await db.commit()
            return created
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def grant_row(idem_key: str) -> dict | None:
    """One queued award by idempotency key, whatever its state."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT id, amount, source, reason, status, attempts, applied_at "
            "FROM level_xp_pending_grants WHERE idem_key=?", (str(idem_key),))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "amount": int(row[1]), "source": row[2],
            "reason": row[3], "status": row[4], "attempts": int(row[5]),
            "applied_at": row[6]}


async def drain_xp_grants(guild_id: int, user_id: int, *,
                          idem_key: str | None = None,
                          limit: int = 100) -> dict:
    """Apply queued one-shot XP awards, each exactly once.

    Every grant gets its own `BEGIN IMMEDIATE` transaction that opens with
    `xp_write_barrier`, so a grant can never land inside a correction's fence:
    it stays queued and is retried later. That is the whole reason this exists -
    a correction can last longer than any caller-side wait, so "try for N
    seconds" is not a guarantee; "stay queued until it is safe" is.

    The XP write, the crossing rows and the `applied` mark commit together, so a
    crash mid-award leaves the row pending rather than half-paid.
    """
    await ensure_tables()
    applied: list[dict] = []
    deferred: list[dict] = []
    failed: list[dict] = []

    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT id, idem_key, amount, source, reason "
            "FROM level_xp_pending_grants WHERE guild_id=? AND user_id=? "
            "AND status='pending' ORDER BY id LIMIT ?",
            (int(guild_id), int(user_id), max(1, int(limit))))
        pending = await cursor.fetchall()

    for (row_id, key, amount, source, reason) in pending:
        if idem_key is not None and str(key) != str(idem_key):
            continue
        new_xp = None
        try:
            async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    blocked = await xp_write_barrier(guild_id, user_id, db)
                    if blocked is not None:
                        # Not now. The row stays pending and is retried - this
                        # is the designed behaviour, not an error.
                        await db.execute("ROLLBACK")
                        deferred.append({"idem_key": key, "reason": blocked})
                        continue
                    from utils.xp_calculator import xp_progress
                    from utils.level_claims import (record_crossing,
                                                    snapshot_multipliers)
                    cursor = await db.execute(
                        "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id))
                    row = await cursor.fetchone()
                    old_xp = int(row[0]) if row else 0
                    new_xp = max(0, old_xp + int(amount))
                    new_level, _, _ = xp_progress(new_xp)
                    await db.execute(
                        "INSERT INTO levels (guild_id, user_id, xp, level) "
                        "VALUES (?,?,?,?) ON CONFLICT(guild_id, user_id) "
                        "DO UPDATE SET xp=?, level=?",
                        (guild_id, user_id, new_xp, new_level, new_xp,
                         new_level))
                    multipliers = await snapshot_multipliers(
                        guild_id, user_id)
                    await record_crossing(
                        db, guild_id, user_id, old_xp, new_xp, multipliers,
                        source=source or "", is_booster=False)
                    cursor = await db.execute(
                        "UPDATE level_xp_pending_grants SET status='applied', "
                        "applied_at=?, attempts=attempts+1, last_error=NULL "
                        "WHERE id=? AND status='pending'", (_now(), row_id))
                    if cursor.rowcount != 1:
                        # Someone applied it between the SELECT and here.
                        await db.execute("ROLLBACK")
                        continue
                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise
        except Exception as exc:
            async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                await db.execute(
                    "UPDATE level_xp_pending_grants SET attempts=attempts+1, "
                    "last_error=? WHERE id=?",
                    (f"{type(exc).__name__}: {exc}", row_id))
                await db.commit()
            failed.append({"idem_key": key,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            from utils.reward_engine import _log_xp_ledger
            await _log_xp_ledger(guild_id, user_id, int(amount), int(new_xp),
                                 reason or "", source or "")
        except Exception:
            pass
        applied.append({"idem_key": key, "amount": int(amount),
                        "new_xp": new_xp})

    return {"applied": applied, "deferred": deferred, "failed": failed}


async def drain_xp_grants_sweep(member_limit: int = 200,
                                 per_member: int = 50) -> dict:
    """Periodic sweep over every member with a queued award.

    The in-line drains in `give_reward` and `record_activities` settle a queued
    award as soon as the member does anything. This covers the rest: a member who
    goes quiet, or a process that restarted while awards were pending. Bounded on
    both axes so a backlog after an outage is drained gradually, not in one
    blocking pass.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT DISTINCT guild_id, user_id FROM level_xp_pending_grants "
            "WHERE status='pending' ORDER BY id LIMIT ?",
            (max(1, int(member_limit)),))
        members = await cursor.fetchall()
    applied = deferred = failed = 0
    for guild_id, user_id in members:
        try:
            out = await drain_xp_grants(guild_id, user_id, limit=per_member)
        except Exception:
            failed += 1
            continue
        applied += len(out["applied"])
        deferred += len(out["deferred"])
        failed += len(out["failed"])
    return {"members": len(members), "applied": applied,
            "deferred": deferred, "failed": failed}


# ─────────────────────────── XP mutation provenance ───────────────────────────

async def record_xp_mutation(*, guild_id: int, user_id: int, source: str,
                             actor_id: int | None, reason: str | None,
                             xp_before: int, xp_after: int,
                             level_before: int | None = None,
                             level_after: int | None = None,
                             crossings: list | None = None,
                             reverses_operation_id: str | None = None,
                             operation_id: str | None = None,
                             db: aiosqlite.Connection | None = None) -> str:
    """One immutable row per administrative XP change.

    `crossings` is the list of reward levels this mutation moved the member
    across, which is what lets a later rollback tell the rewards it caused from
    the ones the member had already earned.
    """
    import json
    await ensure_tables(db)
    op_id = operation_id or new_operation_id(source)

    async def insert(connection):
        await connection.execute(
            "INSERT INTO xp_mutations (operation_id, guild_id, user_id, source, "
            "actor_id, reason, xp_before, xp_after, level_before, level_after, "
            "delta, crossings_json, reverses_operation_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, guild_id, user_id, source, actor_id, reason,
             int(xp_before), int(xp_after), level_before, level_after,
             int(xp_after) - int(xp_before),
             json.dumps(crossings) if crossings else None,
             reverses_operation_id, _now()))

    if db is not None:
        await insert(db)
        return op_id
    async with aiosqlite.connect(DB_PATH, timeout=10) as own:
        await insert(own)
        await own.commit()
    return op_id


async def mark_rolled_back(operation_id: str,
                           db: aiosqlite.Connection | None = None) -> None:
    """Flag the original change as undone.

    Takes the caller's transaction when there is one: the flag has to commit with
    the XP change and the claim deletions it describes, because a rollback that
    committed without the flag can be run again, and one that flagged without
    committing would report an undo that never happened.
    """
    await ensure_tables(db)

    async def update(connection):
        await connection.execute(
            "UPDATE xp_mutations SET rolled_back=1 WHERE operation_id=?",
            (operation_id,))

    if db is not None:
        await update(db)
        return
    async with aiosqlite.connect(DB_PATH, timeout=10) as own:
        await update(own)
        await own.commit()


async def get_xp_mutation(operation_id: str) -> dict | None:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT operation_id, guild_id, user_id, source, actor_id, reason, "
            "xp_before, xp_after, level_before, level_after, delta, "
            "crossings_json, reverses_operation_id, rolled_back, created_at "
            "FROM xp_mutations WHERE operation_id=?", (operation_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    import json
    try:
        crossings = json.loads(row[11]) if row[11] else []
    except (TypeError, ValueError):
        crossings = []
    return {"operation_id": row[0], "guild_id": row[1], "user_id": row[2],
            "source": row[3], "actor_id": row[4], "reason": row[5],
            "xp_before": row[6], "xp_after": row[7], "level_before": row[8],
            "level_after": row[9], "delta": row[10], "crossings": crossings,
            "reverses_operation_id": row[12], "rolled_back": bool(row[13]),
            "created_at": row[14]}


async def list_xp_mutations(guild_id: int, user_id: int,
                            limit: int = 15) -> list[dict]:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT operation_id, source, actor_id, reason, xp_before, xp_after, "
            "level_before, level_after, delta, crossings_json, rolled_back, "
            "created_at FROM xp_mutations WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT ?", (guild_id, user_id, max(1, int(limit))))
        rows = await cursor.fetchall()
    import json
    out = []
    for r in rows:
        try:
            crossings = json.loads(r[9]) if r[9] else []
        except (TypeError, ValueError):
            crossings = []
        out.append({"operation_id": r[0], "source": r[1], "actor_id": r[2],
                    "reason": r[3], "xp_before": r[4], "xp_after": r[5],
                    "level_before": r[6], "level_after": r[7], "delta": r[8],
                    "crossings": crossings, "rolled_back": bool(r[10]),
                    "created_at": r[11]})
    return out


# ───────────────────────────── grant provenance ───────────────────────────────

async def record_grant(db: aiosqlite.Connection, *, guild_id: int, user_id: int,
                       claim_id: int, reward_level: int, track: str, kind: str,
                       quantity: int, item_name: str | None = None,
                       currency: str | None = None,
                       ledger_id: int | None = None,
                       operation_id: str | None = None) -> str:
    """Record what one level reward granted, inside the caller's transaction.

    Called by the claim engine while it delivers, so the grant row commits with
    the delivery it describes. A grant recorded without its delivery - or a
    delivery without its grant - would be exactly the ambiguity this table
    exists to remove.
    """
    await ensure_tables(db)
    grant_id = f"grant-{uuid.uuid4().hex[:16]}"
    await db.execute(
        "INSERT INTO reward_grants (grant_id, guild_id, user_id, claim_id, "
        "reward_level, track, kind, item_name, currency, ledger_id, quantity, "
        "remaining_quantity, operation_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (grant_id, guild_id, user_id, claim_id, reward_level, track, kind,
         item_name, currency, ledger_id, int(quantity), int(quantity),
         operation_id, _now()))
    return grant_id


async def consume_grants(db: aiosqlite.Connection | None, guild_id: int,
                         user_id: int, item_name: str, quantity: int) -> int:
    """Take `quantity` units off the oldest attributable grants, FIFO.

    Called by every path that removes units from an inventory stack, so
    `remaining_quantity` keeps meaning "units of this reward that are still
    there". Returns the number of units attributed to level-reward grants; the
    rest came from elsewhere and no grant is touched for them.
    """
    if quantity <= 0:
        return 0
    await ensure_tables(db)
    left = int(quantity)
    consumed = 0

    async def run(connection):
        nonlocal left, consumed
        cursor = await connection.execute(
            "SELECT grant_id, remaining_quantity FROM reward_grants "
            "WHERE guild_id=? AND user_id=? AND item_name=? AND kind=? "
            "AND remaining_quantity > 0 AND unattributed=0 ORDER BY id",
            (guild_id, user_id, item_name, KIND_INVENTORY))
        for grant_id, remaining in await cursor.fetchall():
            if left <= 0:
                break
            take = min(int(remaining), left)
            await connection.execute(
                "UPDATE reward_grants SET remaining_quantity = "
                "remaining_quantity - ? WHERE grant_id=?", (take, grant_id))
            left -= take
            consumed += take

    if db is not None:
        await run(db)
        return consumed
    async with aiosqlite.connect(DB_PATH, timeout=10) as own:
        await own.execute("BEGIN IMMEDIATE")
        try:
            await run(own)
            await own.commit()
        except Exception:
            await own.execute("ROLLBACK")
            raise
    return consumed


async def invalidate_grants(db: aiosqlite.Connection | None, guild_id: int,
                            user_id: int, item_name: str, reason: str) -> int:
    """Give up on provenance for one item stack instead of guessing at it.

    An absolute quantity write (`set_quantity`) does not say which units it
    replaced, so after one runs no grant for that item can be trusted. Marking
    them `unattributed` makes every later automatic reversal refuse, which is the
    safe direction: an unresolved case an admin can reconcile, never a silent
    deduction from someone else's items.
    """
    await ensure_tables(db)

    async def run(connection):
        cursor = await connection.execute(
            "UPDATE reward_grants SET unattributed=1, unattributed_reason=?, "
            "remaining_quantity=0 WHERE guild_id=? AND user_id=? AND item_name=? "
            "AND kind=? AND unattributed=0 AND remaining_quantity > 0",
            (reason, guild_id, user_id, item_name, KIND_INVENTORY))
        return cursor.rowcount

    if db is not None:
        return await run(db)
    async with aiosqlite.connect(DB_PATH, timeout=10) as own:
        await own.execute("BEGIN IMMEDIATE")
        try:
            affected = await run(own)
            await own.commit()
        except Exception:
            await own.execute("ROLLBACK")
            raise
    return affected


async def grant_for_claim(guild_id: int, user_id: int, claim_id: int,
                          kind: str) -> dict | None:
    """The provenance row for one claim's delivered value, if it exists."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT grant_id, quantity, remaining_quantity, unattributed, "
            "unattributed_reason, item_name, currency, ledger_id, reward_level, "
            "track, operation_id FROM reward_grants "
            "WHERE guild_id=? AND user_id=? AND claim_id=? AND kind=?",
            (guild_id, user_id, claim_id, kind))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"grant_id": row[0], "quantity": row[1], "remaining_quantity": row[2],
            "unattributed": bool(row[3]), "unattributed_reason": row[4],
            "item_name": row[5], "currency": row[6], "ledger_id": row[7],
            "reward_level": row[8], "track": row[9], "operation_id": row[10]}


async def outstanding_reversals(guild_id: int,
                                user_id: int | None = None) -> list[dict]:
    """Every reversal that is proven but unresolved - the admin's work queue."""
    await ensure_tables()
    # An open case is a reversal that is neither applied nor closed by an
    # administrator. `resolve_outstanding` writes `resolved:<decision>` rather
    # than `applied` (the value did not move), so filtering only `applied`
    # kept every resolved case in the queue forever and let /level_reconcile
    # resolve the same case again. The row itself is never deleted, so the
    # audit history is retained.
    sql = ("SELECT reversal_id, operation_id, claim_id, kind, currency, "
           "item_name, quantity, ledger_id, status, reason, created_at "
           "FROM reward_reversals WHERE guild_id=? "
           "AND status NOT IN (?, ?) "
           "AND status NOT LIKE 'resolved:%'")
    # `already_reversed` is terminal exactly like `applied`: the effect was
    # proven gone. Leaving it in this queue offered an administrator a case that
    # needed no decision, and `_resolve_case_in` would then overwrite the
    # terminal marker with `resolved:...` (audit finding N1).
    params: list = [guild_id, STATUS_APPLIED, STATUS_ALREADY]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    sql += " ORDER BY id DESC"
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        rows = await (await db.execute(sql, params)).fetchall()
    return [{"reversal_id": r[0], "operation_id": r[1], "claim_id": r[2],
             "kind": r[3], "currency": r[4], "item_name": r[5],
             "quantity": r[6], "ledger_id": r[7], "status": r[8],
             "reason": r[9], "created_at": r[10]} for r in rows]


async def existing_reversal(guild_id: int, user_id: int, claim_id: int,
                            kind: str) -> dict | None:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT reversal_id, status, quantity, reason, operation_id, "
            "created_at FROM reward_reversals "
            "WHERE guild_id=? AND user_id=? AND claim_id=? AND kind=?",
            (guild_id, user_id, claim_id, kind))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"reversal_id": row[0], "status": row[1], "quantity": row[2],
            "reason": row[3], "operation_id": row[4], "created_at": row[5]}


# ─────────────────────── the two atomic reversals ─────────────────────────────

async def reverse_currency(*, guild_id: int, user_id: int, ledger_id: int,
                           amount: int, currency: str, claim_id: int,
                           operation_id: str, reversed_by: int | None = None,
                           reason: str) -> dict:
    """Undo one level-reward credit: wallet, marker and journal in ONE commit.

    Replaces `utils.ledger.reverse_transaction()` for this path, which splits the
    same work across three connections and two commits: a crash between the debit
    and the `reversed=1` mark leaves a wallet that was charged and a ledger row
    that still looks reversible, so a retry charges it again. It also writes two
    rows for one movement (safe_deduct's `deduct` plus its own `reversal`), so
    anyone summing the ledger counts the event twice.

    Here the debit, the marker, exactly one compensating `reversal` row and the
    journal entry commit together, and the journal's UNIQUE(guild, user, claim,
    kind) makes a second attempt a no-op instead of a second debit. The wallet is
    never forced negative: if the money has been spent the outcome is
    `outstanding`, recorded for an administrator, and nothing moves.
    """
    await ensure_tables()
    amount = int(amount)
    if currency not in ("balance", "diamonds"):
        return {"status": STATUS_REFUSED, "quantity": amount,
                "reason": f"currency {currency!r} is not a wallet column"}
    prior = await existing_reversal(guild_id, user_id, claim_id, KIND_CURRENCY)
    if prior and prior["status"] in (STATUS_APPLIED, STATUS_ALREADY):
        # Value already moved for this claim, once. This is the idempotency key:
        # a retry, a second process or a re-run of the whole reset all land here.
        return {"status": STATUS_ALREADY, "quantity": prior["quantity"],
                "reversal_id": prior["reversal_id"],
                "reason": (f"already recorded {prior['status']} by operation "
                           f"{prior['operation_id']} at {prior['created_at']}")}
    # A prior `refused`/`outstanding` row does NOT block this attempt - it means
    # an earlier try could not prove the reversal (money spent, row missing). If
    # it can be proven now, the row is updated in place; the history is kept.

    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT reversed, amount, currency FROM transaction_ledger "
                "WHERE id=? AND guild_id=? AND user_id=?",
                (ledger_id, guild_id, user_id))
            row = await cursor.fetchone()
            if not row:
                return await _record(
                    db, operation_id, guild_id, user_id, claim_id, KIND_CURRENCY,
                    None, amount, STATUS_REFUSED,
                    f"ledger row {ledger_id} does not exist for this member",
                    prior=prior, currency=currency, ledger_id=ledger_id)
            if row[0]:
                # Someone already reversed it (the dashboard, a previous run).
                # The effect is gone; record that and move no money.
                return await _record(
                    db, operation_id, guild_id, user_id, claim_id, KIND_CURRENCY,
                    None, amount, STATUS_ALREADY,
                    "the ledger row was already marked reversed",
                    prior=prior, currency=currency, ledger_id=ledger_id)

            cursor = await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            wallet = await cursor.fetchone()
            held = int(wallet[0] or 0) if wallet else 0
            if held < amount:
                recorded = await _record(
                    db, operation_id, guild_id, user_id, claim_id, KIND_CURRENCY,
                    None, amount, STATUS_OUTSTANDING,
                    f"the wallet holds {held} {currency}, less than the "
                    f"{amount} this reward paid - it has been spent, so taking "
                    f"it back would remove money from another source",
                    prior=prior, currency=currency, ledger_id=ledger_id)
                return dict(recorded, status=STATUS_OUTSTANDING, held=held,
                            reason=f"the wallet holds {held} {currency} of the "
                                   f"{amount} paid")

            cursor = await db.execute(
                f"UPDATE economy SET {currency} = {currency} - ? "
                f"WHERE guild_id=? AND user_id=? AND {currency} >= ?",
                (amount, guild_id, user_id, amount))
            if cursor.rowcount != 1:
                raise LevelOpsError(
                    f"wallet guard rejected the reversal of {amount} {currency}")
            new_balance = held - amount
            await db.execute(
                "UPDATE transaction_ledger SET reversed=1, "
                "reversed_at=CURRENT_TIMESTAMP, reversed_by=? "
                "WHERE id=? AND reversed=0", (reversed_by, ledger_id))
            # Exactly ONE compensating row for this one movement.
            await db.execute(
                "INSERT INTO transaction_ledger (guild_id, user_id, currency, "
                "amount, balance_after, type, reason, source) "
                "VALUES (?,?,?,?,?,'reversal',?,?)",
                (guild_id, user_id, currency, -amount, new_balance, reason,
                 "level_reward_reversal"))
            if prior:
                await _record(db, operation_id, guild_id, user_id, claim_id,
                              KIND_CURRENCY, None, amount, STATUS_APPLIED,
                              "applied on a later attempt", prior=prior,
                              currency=currency, ledger_id=ledger_id)
            else:
                await db.execute(
                    "INSERT INTO reward_reversals (reversal_id, operation_id, "
                    "guild_id, user_id, claim_id, kind, currency, quantity, "
                    "ledger_id, status, reason, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"rev-{uuid.uuid4().hex[:16]}", operation_id, guild_id,
                     user_id, claim_id, KIND_CURRENCY, currency, amount,
                     ledger_id, STATUS_APPLIED, None, _now()))
            await db.commit()
            return {"status": STATUS_APPLIED, "quantity": amount,
                    "new_balance": new_balance}
        except aiosqlite.IntegrityError:
            # The UNIQUE(guild, user, claim, kind) key: a concurrent or repeated
            # attempt already recorded this reversal. Losing the race is the
            # correct outcome - the winner moved the value once.
            await db.execute("ROLLBACK")
            return {"status": STATUS_ALREADY, "quantity": amount,
                    "reason": "another operation reversed this claim first"}
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def reverse_inventory(*, guild_id: int, user_id: int, claim_id: int,
                            item_name: str, quantity: int, operation_id: str,
                            reason: str) -> dict:
    """Take back exactly the units one level reward granted - at most once.

    Three writes, one transaction: the stack quantity, the grant's
    `remaining_quantity`, and the journal row whose UNIQUE(guild, user, claim,
    kind) key is what makes a retry a no-op. Provenance is required: with no
    grant row (anything delivered before this module existed) or with a grant
    whose remaining units were consumed or invalidated, the outcome is `refused`
    and NOTHING is removed, because the units in the stack cannot be shown to be
    this reward's.
    """
    await ensure_tables()
    quantity = int(quantity)
    prior = await existing_reversal(guild_id, user_id, claim_id, KIND_INVENTORY)
    if prior and prior["status"] in (STATUS_APPLIED, STATUS_ALREADY):
        # Value already moved for this claim, once. This is the idempotency key.
        return {"status": STATUS_ALREADY, "quantity": prior["quantity"],
                "reversal_id": prior["reversal_id"],
                "reason": (f"already recorded {prior['status']} by operation "
                           f"{prior['operation_id']} at {prior['created_at']}")}

    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT grant_id, quantity, remaining_quantity, unattributed, "
                "unattributed_reason FROM reward_grants "
                "WHERE guild_id=? AND user_id=? AND claim_id=? AND kind=?",
                (guild_id, user_id, claim_id, KIND_INVENTORY))
            grant = await cursor.fetchone()
            if not grant:
                return await _record(db, operation_id, guild_id, user_id,
                                     claim_id, KIND_INVENTORY, item_name,
                                     quantity, STATUS_REFUSED,
                                     "no provenance record: this reward was "
                                     "delivered before reward_grants existed, "
                                     "and inventory_items merges units from "
                                     "every source into one quantity, so the "
                                     "units in the stack cannot be shown to be "
                                     "this reward's", prior=prior)
            grant_id, granted, remaining, unattributed, why = grant
            if unattributed:
                return await _record(db, operation_id, guild_id, user_id,
                                     claim_id, KIND_INVENTORY, item_name,
                                     quantity, STATUS_REFUSED,
                                     f"provenance was invalidated: {why}",
                                     prior=prior)
            if int(remaining) < quantity:
                # Name BOTH numbers: what the reward granted, how much of it is
                # still attributable, and how much was asked for. Saying only
                # "2 of the 2 units are attributable" while refusing a request for
                # 5 reads like a contradiction to the administrator who has to
                # decide the case.
                return await _record(
                    db, operation_id, guild_id, user_id, claim_id,
                    KIND_INVENTORY, item_name, quantity, STATUS_OUTSTANDING,
                    f"this reward granted {int(granted)} unit(s) and "
                    f"{int(remaining)} are still attributable, fewer than the "
                    f"{quantity} asked for - the rest were used, traded or "
                    f"dropped, so they cannot be taken back without removing "
                    f"units from another source", prior=prior)
            cursor = await db.execute(
                "SELECT quantity FROM inventory_items WHERE guild_id=? "
                "AND user_id=? AND item_name=?", (guild_id, user_id, item_name))
            row = await cursor.fetchone()
            held = int(row[0] or 0) if row else 0
            if held < quantity:
                return await _record(
                    db, operation_id, guild_id, user_id, claim_id,
                    KIND_INVENTORY, item_name, quantity, STATUS_OUTSTANDING,
                    f"the stack holds {held}x {item_name}, fewer than the "
                    f"{quantity} this reward granted", prior=prior)

            cursor = await db.execute(
                "UPDATE inventory_items SET quantity = quantity - ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE guild_id=? AND user_id=? "
                "AND item_name=? AND quantity >= ?",
                (quantity, guild_id, user_id, item_name, quantity))
            if cursor.rowcount != 1:
                raise LevelOpsError("inventory guard rejected the reversal")
            cursor = await db.execute(
                "UPDATE reward_grants SET remaining_quantity = "
                "remaining_quantity - ? WHERE grant_id=? "
                "AND remaining_quantity >= ?", (quantity, grant_id, quantity))
            if cursor.rowcount != 1:
                raise LevelOpsError("provenance guard rejected the reversal")
            # Journal row and movement commit together: a reversal that is not
            # recorded would be repeated by the next attempt.
            if prior:
                await _record(db, operation_id, guild_id, user_id, claim_id,
                              KIND_INVENTORY, item_name, quantity, STATUS_APPLIED,
                              "applied on a later attempt", prior=prior,
                              grant_id=grant_id)
            else:
                await db.execute(
                    "INSERT INTO reward_reversals (reversal_id, operation_id, "
                    "guild_id, user_id, claim_id, grant_id, kind, item_name, "
                    "quantity, status, reason, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"rev-{uuid.uuid4().hex[:16]}", operation_id, guild_id,
                     user_id, claim_id, grant_id, KIND_INVENTORY, item_name,
                     quantity, STATUS_APPLIED, None, _now()))
            await db.commit()
            return {"status": STATUS_APPLIED, "quantity": quantity,
                    "grant_id": grant_id, "remaining": held - quantity}
        except aiosqlite.IntegrityError:
            await db.execute("ROLLBACK")
            return {"status": STATUS_ALREADY, "quantity": quantity,
                    "reason": "another operation reversed this claim first"}
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def _record(db, operation_id, guild_id, user_id, claim_id, kind,
                  item_name, quantity, status, reason, *, prior: dict = None,
                  currency: str = None, ledger_id: int = None,
                  grant_id: str = None, role_id: int = None) -> dict:
    """Write one journal outcome and commit, so the case stays on the record.

    A refusal that is not recorded is indistinguishable from a reversal that never
    ran, and the claim row would then be deleted on the strength of a report
    nobody can read afterwards.

    `prior` is an existing row for this (guild, user, claim, kind). The UNIQUE key
    allows only one, so a later attempt UPDATES it instead of inserting a second
    row - and the earlier reason is kept, appended to, so the case reads as a
    history rather than being overwritten. This matters in the direction that is
    easy to get wrong: a first attempt that was refused (provenance missing, money
    already spent, quantity not attributable) must not permanently block a later
    attempt that CAN be proven. Only an `applied`/`already_reversed` row does
    that, because only those say value actually moved.
    """
    if prior:
        history = str(prior.get("reason") or "").strip()
        merged = (f"{history} | {operation_id} at {_now()}: {reason or status}"
                  if history else f"{operation_id} at {_now()}: {reason or status}")
        await db.execute(
            "UPDATE reward_reversals SET operation_id=?, quantity=?, status=?, "
            "reason=?, currency=COALESCE(?, currency), "
            "ledger_id=COALESCE(?, ledger_id), grant_id=COALESCE(?, grant_id), "
            "item_name=COALESCE(?, item_name) WHERE reversal_id=?",
            (operation_id, int(quantity), status, merged, currency, ledger_id,
             grant_id, item_name, prior["reversal_id"]))
        await db.commit()
        return {"status": status, "quantity": int(quantity), "reason": merged,
                "reversal_id": prior["reversal_id"]}
    reversal_id = f"rev-{uuid.uuid4().hex[:16]}"
    await db.execute(
        "INSERT INTO reward_reversals (reversal_id, operation_id, guild_id, "
        "user_id, claim_id, kind, item_name, currency, ledger_id, grant_id, "
        "quantity, status, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (reversal_id, operation_id, guild_id, user_id, claim_id, kind, item_name,
         currency, ledger_id, grant_id, int(quantity), status, reason, _now()))
    await db.commit()
    return {"status": status, "quantity": int(quantity), "reason": reason,
            "reversal_id": reversal_id}


async def resolve_outstanding(reversal_id: str, *, actor_id: int,
                              resolution: str, note: str,
                              db: aiosqlite.Connection | None = None) -> bool:
    """Close an outstanding/refused case by an administrator's explicit decision.

    The journal row is never deleted and its `status` is never rewritten to
    `applied` - that would claim value moved when it did not. The resolution is
    appended to the reason, so the audit trail shows the case, the decision and
    who made it.

    The decision is a ONE-WAY transition. Only a case that is still open - not
    `applied` and not already `resolved:<decision>` - can be closed, so a retry,
    a stale listing or a second administrator can never overwrite a decision or
    close the same case twice. Returns False when the case was not open; the
    caller must then change nothing else.

    Pass `db` to run the check-and-update inside the caller's transaction, so a
    decision and the rows it justifies (such as the claim deleted behind
    `drop_claim`) commit together or not at all.
    """
    if db is not None:
        return await _resolve_case_in(db, reversal_id, actor_id=actor_id,
                                      resolution=resolution, note=note)
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as owned:
        await owned.execute("BEGIN IMMEDIATE")
        try:
            closed = await _resolve_case_in(owned, reversal_id, actor_id=actor_id,
                                            resolution=resolution, note=note)
            await owned.commit()
        except Exception:
            await owned.execute("ROLLBACK")
            raise
        return closed


async def _resolve_case_in(db, reversal_id: str, *, actor_id: int,
                           resolution: str, note: str) -> bool:
    """Guarded close, inside the caller's write transaction.

    SQLite evaluates the WHERE clause at write time under the write lock, so
    exactly ONE concurrent decision can match a still-open case. The second one
    updates zero rows, moves nothing, and leaves the first decision intact.
    """
    cursor = await db.execute(
        "UPDATE reward_reversals SET status=?, reason=reason || ? "
        "WHERE reversal_id=? AND status NOT IN (?, ?) "
        "AND status NOT LIKE 'resolved:%'",
        (f"resolved:{resolution}", f" | resolved by {actor_id} at {_now()}: "
         f"{note}", reversal_id, STATUS_APPLIED, STATUS_ALREADY))
    return cursor.rowcount == 1


async def note_reversal(*, guild_id: int, user_id: int, claim_id: int,
                        kind: str, quantity: int, operation_id: str,
                        status: str, reason: str | None = None,
                        item_name: str | None = None,
                        currency: str | None = None,
                        ledger_id: int | None = None,
                        role_id: int | None = None) -> bool:
    """Append a journal row for an outcome that moved no fungible value.

    Used for Discord role/boost outcomes, where the side effect is on the member
    rather than in a balance. Idempotent through the same UNIQUE(guild, user,
    claim, kind) key: there is never a second row for one claim's outcome of one
    kind.

    An `applied`/`already_reversed` row is terminal - a later attempt returns
    False and must not move anything again. A `refused`/`outstanding` row is NOT
    terminal: when a retry finally succeeds (the role was removed, the boost
    duration could be deleted), that same row is upgraded to `applied` with the
    earlier reason preserved. Without that, a first failure would permanently
    hide the fact that the effect was later reversed.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT reversal_id, status, reason FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? AND claim_id=? AND kind=?",
                (guild_id, user_id, claim_id, kind))
            existing = await cursor.fetchone()
            if existing:
                if existing[1] in (STATUS_APPLIED, STATUS_ALREADY):
                    await db.commit()
                    return False
                history = str(existing[2] or "").strip()
                merged = (f"{history} | {operation_id} at {_now()}: "
                          f"{reason or status}" if history
                          else f"{operation_id} at {_now()}: {reason or status}")
                await db.execute(
                    "UPDATE reward_reversals SET operation_id=?, quantity=?, "
                    "status=?, reason=?, currency=COALESCE(?, currency), "
                    "ledger_id=COALESCE(?, ledger_id), item_name=COALESCE(?, "
                    "item_name) WHERE reversal_id=?",
                    (operation_id, int(quantity), status, merged, currency,
                     ledger_id,
                     item_name if item_name is not None else (
                         str(role_id) if role_id is not None else None),
                     existing[0]))
                await db.commit()
                return True
            await db.execute(
                "INSERT INTO reward_reversals (reversal_id, operation_id, "
                "guild_id, user_id, claim_id, kind, currency, item_name, "
                "quantity, ledger_id, status, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"rev-{uuid.uuid4().hex[:16]}", operation_id, guild_id,
                 user_id, claim_id, kind, currency,
                 item_name if item_name is not None else (
                     str(role_id) if role_id is not None else None),
                 int(quantity), ledger_id, status, reason, _now()))
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def reversal_rows(guild_id: int, user_id: int) -> list[dict]:
    """Every journal row for one member - the reset's idempotency memory."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT claim_id, kind, status, quantity, reason, operation_id, "
            "created_at, reversal_id, item_name, currency, ledger_id "
            "FROM reward_reversals WHERE guild_id=? AND user_id=? ORDER BY id",
            (guild_id, user_id))
        rows = await cursor.fetchall()
    return [{"claim_id": r[0], "kind": r[1], "status": r[2], "quantity": r[3],
             "reason": r[4], "operation_id": r[5], "created_at": r[6],
             "reversal_id": r[7], "item_name": r[8], "currency": r[9],
             "ledger_id": r[10]} for r in rows]
