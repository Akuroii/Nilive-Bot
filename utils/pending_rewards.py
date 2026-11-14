"""
Rewards that were earned but could not be delivered yet.

A Mission, Minigame or Event reward is granted once: the mission is already
marked completed, the round already resolved, the winner already recorded.
When the Inventory refuses the grant because the member owns a different
kind of item under the same name (utils.inventory.ItemTypeConflict), the
reward is parked here instead of being dropped. The completed objective is
untouched, and the reward is delivered by a later retry once the name is
free again - the member never has to earn it twice.

Not used for Shop purchases (a refused delivery is refunded) or Level reward
claims (a refused claim stays `failed` and is retried by its own claim flow).

A row is the keyword arguments of utils.reward_engine.give_reward(), replayed
unchanged by `retry()`:
    pending      -> safely stored and waiting; retried by the scheduler sweep
                    and whenever the member opens their Inventory
    delivering   -> one retry owns it right now (a claim, so two retries can
                    never both deliver it). Transient.
    delivered    -> delivery confirmed; kept as the record
    needs_review -> the outcome of a delivery is unknown (a Discord call that
                    may have landed, an unexpected error, or a process that
                    stopped mid-delivery). NEVER retried automatically: staff
                    check the member and either retry it or mark it delivered
                    (`resolve_review`, dashboard Inventory page).
"""
import json
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import DB_PATH

STATUS_PENDING = "pending"
STATUS_DELIVERING = "delivering"
STATUS_DELIVERED = "delivered"
STATUS_NEEDS_REVIEW = "needs_review"
OPEN_STATUSES = (STATUS_PENDING, STATUS_DELIVERING, STATUS_NEEDS_REVIEW)

# A `delivering` claim older than this was abandoned by a stopped process
# (a delivery takes seconds). The delivery may have landed, so it becomes
# `needs_review` instead of being retried.
STALE_CLAIM_MINUTES = 10

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS pending_rewards (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        reward_type  TEXT NOT NULL,
        payload      TEXT NOT NULL,
        source       TEXT,
        reason       TEXT,
        status       TEXT NOT NULL DEFAULT 'pending',
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        delivered_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pending_rewards_member "
    "ON pending_rewards(guild_id, user_id, status)",
    # The scheduler sweep reads `WHERE status='pending' ORDER BY id`.
    "CREATE INDEX IF NOT EXISTS idx_pending_rewards_status "
    "ON pending_rewards(status, id)",
)

_COLUMNS = ("id", "guild_id", "user_id", "reward_type", "payload", "source",
            "reason", "status", "attempts", "last_error", "created_at",
            "updated_at", "delivered_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(raw) -> dict:
    row = dict(zip(_COLUMNS, raw))
    try:
        row["payload"] = json.loads(row["payload"]) or {}
    except (TypeError, ValueError):
        row["payload"] = {}
    row["label"] = describe(row)
    row["needs_review"] = needs_review(row)
    return row


def describe(row: dict) -> str:
    """Short human label of what is waiting: `2× Tonic`, `Role <@&id>`."""
    payload = row.get("payload") or {}
    if row.get("reward_type") == "item":
        quantity = int(payload.get("amount") or 1)
        return f"{quantity}× {payload.get('item_name')}"
    if payload.get("role_id"):
        return f"Role <@&{payload['role_id']}>"
    return str(row.get("reward_type"))


async def park(guild_id: int, user_id: int, reward_type: str, payload: dict, *,
               source: str | None, reason: str | None, error: str) -> int:
    """Record one undelivered reward. Returns its id."""
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO pending_rewards (guild_id, user_id, reward_type, "
            "payload, source, reason, status, attempts, last_error, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)",
            (guild_id, user_id, reward_type, json.dumps(payload), source,
             reason, STATUS_PENDING, str(error)[:300], now, now))
        await db.commit()
        return cursor.lastrowid


async def list_pending(guild_id: int, user_id: int | None = None) -> list[dict]:
    """Everything not yet delivered: pending, being delivered, or in review."""
    sql = (f"SELECT {', '.join(_COLUMNS)} FROM pending_rewards "
           "WHERE guild_id=? AND status IN (?, ?, ?)")
    args = [guild_id, *OPEN_STATUSES]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql + " ORDER BY id", args)
        return [_row(raw) for raw in await cursor.fetchall()]


async def _claim(row_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE pending_rewards SET status=?, attempts=attempts+1, "
            "updated_at=? WHERE id=? AND status=?",
            (STATUS_DELIVERING, _now(), row_id, STATUS_PENDING))
        await db.commit()
        return cursor.rowcount == 1


async def _settle(row_id: int, status: str, error: str | None) -> None:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_rewards SET status=?, last_error=?, updated_at=?, "
            "delivered_at=? WHERE id=? AND status=?",
            (status, (str(error)[:300] if error else None), now,
             now if status == STATUS_DELIVERED else None, row_id,
             STATUS_DELIVERING))
        await db.commit()


async def retry(bot, guild_id: int | None = None, user_id: int | None = None,
                *, limit: int = 50) -> dict:
    """Try to deliver waiting rewards, each at most once.

    A reward that is still refused goes back to `pending` with the reason.
    Only a definite failure does: if the delivery outcome is uncertain (a
    Discord role call that may have landed, or an unexpected error) the row
    becomes `needs_review`, so it is checked by staff rather than paid a
    second time. Only `pending` rows are ever selected here.
    """
    await flag_abandoned()
    sql = f"SELECT {', '.join(_COLUMNS)} FROM pending_rewards WHERE status=?"
    args: list = [STATUS_PENDING]
    if guild_id is not None:
        sql += " AND guild_id=?"
        args.append(guild_id)
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql + " ORDER BY id LIMIT ?", (*args, limit))
        waiting = [_row(raw) for raw in await cursor.fetchall()]

    outcome = {"delivered": 0, "still_pending": 0, "uncertain": 0}
    if not waiting:
        return outcome
    from utils.reward_engine import give_reward
    for row in waiting:
        if not await _claim(row["id"]):
            continue                      # another retry owns it
        try:
            result = await give_reward(
                bot, row["guild_id"], row["user_id"], row["reward_type"],
                park_on_conflict=False, **row["payload"])
        except Exception as exc:
            # give_reward reports refusals as results; a raise is unexpected
            # and says nothing about how far the delivery got.
            result = {"success": False, "uncertain": True,
                      "error": f"Unexpected error during delivery: {exc}"}
        if result.get("success"):
            await _settle(row["id"], STATUS_DELIVERED, None)
            outcome["delivered"] += 1
        elif result.get("uncertain"):
            await _settle(row["id"], STATUS_NEEDS_REVIEW,
                          result.get("error") or "Delivery outcome unknown")
            outcome["uncertain"] += 1
            print(f"[REWARDS] pending reward #{row['id']} needs review "
                  f"(guild={row['guild_id']} user={row['user_id']}): "
                  f"{result.get('error')}")
        else:
            await _settle(row["id"], STATUS_PENDING, result.get("error"))
            outcome["still_pending"] += 1
    return outcome


async def flag_abandoned() -> int:
    """Move `delivering` claims a stopped process left behind to review.

    Runs before every retry, so after a restart the next sweep surfaces them.
    Returns how many were flagged.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=STALE_CLAIM_MINUTES)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE pending_rewards SET status=?, last_error=?, updated_at=? "
            "WHERE status=? AND updated_at < ?",
            (STATUS_NEEDS_REVIEW,
             "Delivery was interrupted (the bot stopped while delivering); "
             "it may or may not have arrived.", _now(), STATUS_DELIVERING,
             cutoff))
        await db.commit()
        return cursor.rowcount


async def resolve_review(guild_id: int, row_id: int, action: str, *,
                         actor: str = "staff") -> bool:
    """Staff decision on a `needs_review` reward of this guild.

    action "retry"     -> back to `pending`; the next sweep delivers it. For
                          when the member did NOT receive the reward.
    action "delivered" -> closed as delivered. For when the member did.
    Only a `needs_review` row can be decided, and only once: the guarded
    UPDATE makes a second click (or a second staff member) a no-op.
    """
    if action not in ("retry", "delivered"):
        raise ValueError("action must be 'retry' or 'delivered'")
    await flag_abandoned()
    now = _now()
    delivered = action == "delivered"
    note = (f"Marked as delivered by {actor}" if delivered
            else f"Retry requested by {actor}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE pending_rewards SET status=?, last_error=?, updated_at=?, "
            "delivered_at=? WHERE id=? AND guild_id=? AND status=?",
            (STATUS_DELIVERED if delivered else STATUS_PENDING, note, now,
             now if delivered else None, row_id, guild_id,
             STATUS_NEEDS_REVIEW))
        await db.commit()
        return cursor.rowcount == 1


def needs_review(row: dict) -> bool:
    """Outcome unknown: staff check it by hand, it is never auto-paid."""
    if row.get("status") == STATUS_NEEDS_REVIEW:
        return True
    if row.get("status") != STATUS_DELIVERING:
        return False
    try:
        claimed = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - claimed > timedelta(
        minutes=STALE_CLAIM_MINUTES)
