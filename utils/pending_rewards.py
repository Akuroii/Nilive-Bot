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
    if row.get("reward_type") in ("item", "level_claim"):
        quantity = int(payload.get("amount") or payload.get("quantity") or 1)
        return f"{quantity}× {payload.get('item_name')}"
    if payload.get("role_id"):
        return f"Role <@&{payload['role_id']}>"
    return str(row.get("reward_type"))


async def park(guild_id: int, user_id: int, reward_type: str, payload: dict, *,
               source: str | None, reason: str | None, error: str) -> int:
    """Record one undelivered reward. Returns its id.

    Re-running the same reward source must not stack a second copy: a payload
    carrying a stable ``idempotency_key`` (missions and tag missions supply one
    per reward and period) is one debt, so an OPEN row with the same guild,
    member, reward type, source and key is reused instead of inserting another.
    Sources with no such key keep the previous behaviour: there is nothing
    stable to match on, and merging two real rewards would lose one.
    """
    identity = str(payload.get("idempotency_key") or "").strip() or None
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        # One write transaction for the lookup AND the insert. A bare SELECT
        # followed by an INSERT lets two replays of the same source both find
        # nothing and both insert a copy; BEGIN IMMEDIATE serialises them, so
        # the second one sees the first one's row.
        await db.execute("BEGIN IMMEDIATE")
        try:
            if identity is not None:
                cursor = await db.execute(
                    "SELECT id, payload FROM pending_rewards WHERE guild_id=? "
                    "AND user_id=? AND reward_type=? AND source IS ? AND status "
                    "IN (?, ?, ?) ORDER BY id",
                    (guild_id, user_id, reward_type, source, *OPEN_STATUSES))
                for held_id, held_payload in await cursor.fetchall():
                    try:
                        held_identity = str(
                            (json.loads(held_payload or "{}") or {})
                            .get("idempotency_key") or "").strip()
                    except (TypeError, ValueError):
                        held_identity = ""
                    if held_identity and held_identity == identity:
                        await db.execute(
                            "UPDATE pending_rewards SET last_error=?, "
                            "updated_at=? WHERE id=?",
                            (str(error)[:300], now, held_id))
                        await db.commit()
                        print(f"[REWARDS] {reward_type} reward for "
                              f"guild={guild_id} user={user_id} is already held "
                              f"as pending reward #{held_id} (source identity "
                              f"{identity!r}); not parked twice.")
                        return int(held_id)
            cursor = await db.execute(
                "INSERT INTO pending_rewards (guild_id, user_id, reward_type, "
                "payload, source, reason, status, attempts, last_error, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                (guild_id, user_id, reward_type, json.dumps(payload), source,
                 reason, STATUS_PENDING, str(error)[:300], now, now))
            await db.commit()
            return cursor.lastrowid
        except BaseException:
            await db.rollback()
            raise


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
        items = [_row(raw) for raw in await cursor.fetchall()]
        stale_ids: list[int] = []
        kept: list[dict] = []
        for item in items:
            if item.get("reward_type") == "level_claim":
                payload = item.get("payload") or {}
                try:
                    cid = int(payload["claim_id"])
                except (KeyError, TypeError, ValueError):
                    cid = None
                if cid is not None:
                    try:
                        c_cur = await db.execute(
                            "SELECT status FROM level_reward_claims "
                            "WHERE id=? AND guild_id=? AND user_id=?",
                            (cid, int(item["guild_id"]), int(item["user_id"])),
                        )
                        c_row = await c_cur.fetchone()
                    except aiosqlite.Error as exc:
                        if "no such table" in str(exc).lower():
                            c_row = None
                        else:
                            raise
                    if c_row is None or c_row[0] == "fulfilled":
                        stale_ids.append(int(item["id"]))
                        continue
            kept.append(item)
        if stale_ids:
            now_iso = _now()
            marks = ",".join("?" for _ in stale_ids)
            await db.execute(
                f"UPDATE pending_rewards SET status=?, last_error=NULL, "
                f"updated_at=?, delivered_at=? "
                f"WHERE id IN ({marks}) AND status IN (?, ?, ?)",
                (STATUS_DELIVERED, now_iso, now_iso, *stale_ids, *OPEN_STATUSES),
            )
            await db.commit()
        return kept


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
        if row["reward_type"] == "level_claim":
            try:
                from utils.level_claims import claim_available
                payload = row.get("payload") or {}
                claim_id = int(payload["claim_id"])
                gid = int(row["guild_id"])
                uid = int(row["user_id"])
                async with aiosqlite.connect(DB_PATH) as check_db:
                    cur_c = await check_db.execute(
                        "SELECT status, last_error FROM level_reward_claims "
                        "WHERE id=? AND guild_id=? AND user_id=?",
                        (claim_id, gid, uid),
                    )
                    claim_row = await cur_c.fetchone()
                if claim_row is None or claim_row[0] == "fulfilled":
                    await _settle(row["id"], STATUS_DELIVERED, None)
                    continue
                guild = (bot.get_guild(gid)
                         if bot is not None and callable(getattr(bot, "get_guild", None))
                         else None)
                member = (guild.get_member(uid)
                          if guild is not None and callable(getattr(guild, "get_member", None))
                          else None)
                if member is None and guild is not None and callable(getattr(guild, "fetch_member", None)):
                    try:
                        member = await guild.fetch_member(uid)
                    except Exception:
                        member = None
                if payload.get("role_id") and member is None:
                    await _settle(row["id"], STATUS_PENDING,
                                  "Waiting for member availability on Discord")
                    outcome["still_pending"] += 1
                    continue
                claim_res = await claim_available(
                    gid, uid, member=member, bot=bot, claim_ids=[claim_id]
                )
                if claim_id in (claim_res.get("fulfilled") or []):
                    await _settle(row["id"], STATUS_DELIVERED, None)
                    outcome["delivered"] += 1
                else:
                    async with aiosqlite.connect(DB_PATH) as err_db:
                        cur_e = await err_db.execute(
                            "SELECT status, last_error FROM level_reward_claims "
                            "WHERE id=? AND guild_id=? AND user_id=?",
                            (claim_id, gid, uid),
                        )
                        err_row = await cur_e.fetchone()
                    if err_row is None or err_row[0] == "fulfilled":
                        await _settle(row["id"], STATUS_DELIVERED, None)
                    else:
                        err_msg = (
                            (err_row[1] if err_row[1] else None)
                            or claim_res.get("blocked")
                            or "Reward still held pending"
                        )
                        await _settle(row["id"], STATUS_PENDING, str(err_msg))
                        outcome["still_pending"] += 1
            except Exception as exc:
                await _settle(row["id"], STATUS_NEEDS_REVIEW,
                              f"Unexpected error during level claim retry: {exc}")
                outcome["uncertain"] += 1
            continue
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
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur_row = await (await db.execute(
                "SELECT user_id, reward_type, payload FROM pending_rewards "
                "WHERE id=? AND guild_id=? AND status=?",
                (row_id, guild_id, STATUS_NEEDS_REVIEW),
            )).fetchone()
            cursor = await db.execute(
                "UPDATE pending_rewards SET status=?, last_error=?, updated_at=?, "
                "delivered_at=? WHERE id=? AND guild_id=? AND status=?",
                (STATUS_DELIVERED if delivered else STATUS_PENDING, note, now,
                 now if delivered else None, row_id, guild_id,
                 STATUS_NEEDS_REVIEW))
            changed = cursor.rowcount == 1
            if changed and delivered and cur_row and cur_row[1] == "level_claim":
                try:
                    p = json.loads(cur_row[2] or "{}") or {}
                    cid = int(p["claim_id"])
                except (KeyError, TypeError, ValueError):
                    cid = None
                if cid is not None:
                    try:
                        await db.execute(
                            "UPDATE level_reward_claims "
                            "SET status='fulfilled', fulfilled_at=?, "
                            "owner_token=NULL, processing_started_at=NULL, "
                            "last_error=NULL "
                            "WHERE id=? AND guild_id=? AND user_id=? "
                            "AND status IN ('pending', 'failed', 'processing')",
                            (now, cid, guild_id, int(cur_row[0])),
                        )
                    except aiosqlite.Error as exc:
                        if "no such table" not in str(exc).lower():
                            raise
            await db.commit()
            return changed
        except BaseException:
            await db.rollback()
            raise


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
