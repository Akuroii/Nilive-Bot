"""Durable, bounded, resumable preparation queue for Shop product icons.

Why this module exists
----------------------
Preparation was entirely *pull* based: an owner had to name product IDs in the
Shop Designer, one request per 25 products. Nothing recorded that a product
still needed an icon, so a restart lost the intent and 100 products meant 100
manual actions.

This module is the *push* half. It stores intent only — a row per
(guild_id, product_id) saying "this product's current icon_url should become a
prepared application emoji". It never talks to Discord, never downloads an
image, and never creates or deletes anything. All of that stays in
``utils/shop_emoji`` behind its existing fences; the runtime lives in
``utils/shop_emoji_worker``.

Read-only consumers are untouched by design: Inventory (``cogs/wallet.py``) and
Rank (``utils/rank_card_data.py``) import neither this module nor the worker,
so opening either can never enqueue, download, upload or create.

Durability
----------
The queue is SQLite, in the same file as the aliases, so it survives a Railway
restart with the volume. A row is only marked ``done`` after Discord has been
reconciled against the asset's current state, so an interrupted pass resumes
rather than repeating. ``attempts`` and ``last_error`` make progress and the
reason for a stall both visible.

Idempotence
-----------
``UNIQUE(guild_id, product_id)`` plus an upsert keyed on the canonical URL
means re-enqueuing the same product for the same URL is a no-op, and a *new*
URL replaces the pending work while leaving the old alias in place (it is what
keeps the previously prepared emoji resolvable until the new image is ready).
"""

from __future__ import annotations

import aiosqlite

from utils.shop_emoji import (
    SourceImageError,
    canonicalize_url,
    content_identity,
    deterministic_emoji_name,
    readable_emoji_name,
    rename_target_name,
)

# ── Tunables ────────────────────────────────────────────────────────────────
# One Discord list_emojis() covers a whole pass, so the pass cost is dominated
# by image downloads (one per distinct unhashed URL) and creates. 8 keeps a
# single pass well under the 55s batch deadline the manual path uses and well
# under Discord's per-route limits.
DEFAULT_BATCH_LIMIT = 8
MAX_BATCH_LIMIT = 25
# Stop starting new work after this long, so a pass never overruns a scheduler
# tick or a Railway health check. In-flight items are allowed to finish.
DEFAULT_TIME_BUDGET_SECONDS = 45.0
# How long a soft failure (dead URL, timeout) waits before another attempt.
RETRY_BACKOFF_SECONDS = 900
# Attempts after which a product stops being retried automatically and waits
# for a human. Keeps a permanently dead URL from being fetched forever.
MAX_AUTO_ATTEMPTS = 4

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_emoji_queue (
        guild_id      INTEGER NOT NULL,
        product_id    INTEGER NOT NULL,
        item_name     TEXT NOT NULL,
        source_url    TEXT NOT NULL,
        content_hash  TEXT,
        reason        TEXT NOT NULL DEFAULT 'backfill',
        queued_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        next_attempt_at TEXT,
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        PRIMARY KEY (guild_id, product_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_emoji_queue_next
    ON shop_emoji_queue(next_attempt_at)
    """,
)


async def ensure_schema(db):
    """Create the queue tables. Additive only; never alters existing tables."""
    for statement in SCHEMA:
        await db.execute(statement)
    await db.commit()


# ── URL helpers ─────────────────────────────────────────────────────────────
def queue_source_url(raw):
    """Canonical form of a listing icon URL, or None when it is unusable.

    Uses the SAME canonicalizer the alias table is keyed on, so a queued URL
    and a prepared alias can never disagree about identity.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        return canonicalize_url(raw)
    except SourceImageError:
        return None


def _now_sql():
    return "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


# ── Enqueue ─────────────────────────────────────────────────────────────────
async def enqueue_product(db, guild_id, product_id, item_name, icon_url, *,
                          reason="product_saved", reset_attempts=False):
    """Queue one product's current icon URL.

    A product with no usable URL is *removed* from the queue rather than
    stored broken: absence is the honest state, and it stops a stale row from
    being retried forever after an admin clears the image.

    Returns the canonical URL that is now queued, or None.
    """
    url = queue_source_url(icon_url)
    if url is None:
        await db.execute(
            "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (guild_id, product_id))
        await db.commit()
        return None
    # A genuinely new URL restarts the retry budget; re-queueing the same URL
    # leaves an existing backoff intact so a scheduler tick cannot defeat it.
    await db.execute(
        "INSERT INTO shop_emoji_queue "
        "(guild_id,product_id,item_name,source_url,reason,queued_at,updated_at,next_attempt_at,attempts) "
        f"VALUES (?,?,?,?,?,{_now_sql()},{_now_sql()},NULL,0) "
        "ON CONFLICT(guild_id,product_id) DO UPDATE SET "
        "  item_name=excluded.item_name,"
        "  reason=excluded.reason,"
        "  updated_at=excluded.updated_at,"
        "  source_url=excluded.source_url,"
        "  content_hash=CASE WHEN shop_emoji_queue.source_url=excluded.source_url "
        "                    THEN shop_emoji_queue.content_hash ELSE NULL END,"
        "  attempts=CASE WHEN shop_emoji_queue.source_url=excluded.source_url "
        "                THEN shop_emoji_queue.attempts ELSE 0 END,"
        "  next_attempt_at=CASE WHEN shop_emoji_queue.source_url=excluded.source_url "
        "                       THEN shop_emoji_queue.next_attempt_at ELSE NULL END,"
        "  last_error=CASE WHEN shop_emoji_queue.source_url=excluded.source_url "
        "                  THEN shop_emoji_queue.last_error ELSE NULL END",
        (guild_id, product_id, item_name, url, reason))
    await db.commit()
    return url


async def enqueue_guild_products(db, guild_id, *, reason="backfill",
                                 only_unprepared=True, limit=None):
    """Queue every eligible listing in one guild — the bulk backfill entry point.

    "Eligible" is deliberately narrow and read straight from the authoritative
    table, so no Shop Designer roster entry is needed:

    * the row exists in ``shop_items`` for this guild,
    * it carries a non-empty ``icon_url`` that canonicalizes,
    * it is enabled (a disabled listing is not for sale; it can be queued when
      it is re-enabled).

    With ``only_unprepared`` the rows that already resolve to a *ready* asset
    are skipped, which is what makes a re-run cheap and is why a backfill can
    be interrupted and restarted without repeating finished work.

    Returns the number of products queued.
    """
    params = [guild_id]
    sql = ("SELECT s.id, s.name, s.icon_url FROM shop_items s "
           "WHERE s.guild_id=? AND s.icon_url IS NOT NULL AND TRIM(s.icon_url)<>'' "
           "AND COALESCE(s.enabled,1)=1")
    if only_unprepared:
        # Already prepared for THIS guild (any application row is fine: the
        # worker re-checks scope with the authenticated application).
        sql += (" AND NOT EXISTS (SELECT 1 FROM shop_items q "
                "JOIN shop_emoji_url_aliases a ON a.guild_id=q.guild_id "
                "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
                "  AND e.content_hash=a.content_hash "
                "WHERE q.id=s.id AND e.status='ready')")
    sql += " ORDER BY s.id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    cur = await db.execute(sql, tuple(params))
    rows = await cur.fetchall()

    queued = 0
    for product_id, name, icon_url in rows:
        if await enqueue_product(db, guild_id, int(product_id), name, icon_url,
                                 reason=reason) is not None:
            queued += 1
    return queued


async def clear_product(db, guild_id, product_id):
    """Drop queued work for a product (deleted listing, cleared image)."""
    await db.execute(
        "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
        (guild_id, product_id))
    await db.commit()


async def prune_orphans(db):
    """Remove queue rows whose listing no longer exists or lost its URL."""
    cur = await db.execute(
        "DELETE FROM shop_emoji_queue WHERE NOT EXISTS ("
        "  SELECT 1 FROM shop_items s WHERE s.id=shop_emoji_queue.product_id "
        "  AND s.guild_id=shop_emoji_queue.guild_id "
        "  AND s.icon_url IS NOT NULL AND TRIM(s.icon_url)<>'')")
    removed = cur.rowcount or 0
    await db.commit()
    return removed


# ── Read ────────────────────────────────────────────────────────────────────
QUEUE_FIELDS = ("guild_id", "product_id", "item_name", "source_url", "content_hash",
                "reason", "queued_at", "updated_at", "next_attempt_at", "attempts",
                "last_error")


def _row(row):
    return dict(zip(QUEUE_FIELDS, row)) if row else None


async def due(db, *, guild_id=None, limit=DEFAULT_BATCH_LIMIT):
    """Rows eligible for a pass right now, oldest intent first."""
    sql = (f"SELECT {','.join(QUEUE_FIELDS)} FROM shop_emoji_queue "
           "WHERE (next_attempt_at IS NULL OR next_attempt_at <= " + _now_sql() + ")")
    params = []
    if guild_id is not None:
        sql += " AND guild_id=?"
        params.append(guild_id)
    sql += " ORDER BY COALESCE(next_attempt_at,'') ASC, queued_at ASC, product_id ASC LIMIT ?"
    params.append(max(1, min(int(limit or DEFAULT_BATCH_LIMIT), MAX_BATCH_LIMIT)))
    cur = await db.execute(sql, tuple(params))
    return [_row(r) for r in await cur.fetchall()]


async def stats(db, *, guild_id=None):
    """Queue depth by state — the report a human needs, no Discord involved."""
    where, params = "", []
    if guild_id is not None:
        where, params = " WHERE guild_id=?", [guild_id]
    cur = await db.execute(
        "SELECT COUNT(*), "
        " SUM(CASE WHEN next_attempt_at IS NOT NULL AND next_attempt_at > "
        "   strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN 1 ELSE 0 END), "
        " SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END), "
        " MAX(attempts) FROM shop_emoji_queue" + where, tuple(params))
    total, deferred, errored, max_attempts = await cur.fetchone()
    return {"total": total or 0, "deferred": deferred or 0,
            "errored": errored or 0, "max_attempts": max_attempts or 0}


async def next_attempt_at(db):
    """Earliest future retry time, or None when nothing is waiting on a clock.

    The scheduler reads this to decide whether a pass is worth running, and it
    costs one indexed row lookup.
    """
    cur = await db.execute(
        "SELECT MIN(next_attempt_at) FROM shop_emoji_queue "
        "WHERE next_attempt_at IS NOT NULL AND next_attempt_at > " + _now_sql())
    row = await cur.fetchone()
    return row[0] if row else None


# ── Progress ────────────────────────────────────────────────────────────────
async def record_attempt(db, row, *, content_hash=None):
    """Persist that we are about to work on this row (survives a hard restart)."""
    await db.execute(
        "UPDATE shop_emoji_queue SET attempts=attempts+1, content_hash=COALESCE(?,content_hash), "
        f"updated_at={_now_sql()} WHERE guild_id=? AND product_id=?",
        (content_hash, row["guild_id"], row["product_id"]))
    await db.commit()


async def mark_done(db, row):
    await db.execute(
        "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
        (row["guild_id"], row["product_id"]))
    await db.commit()


async def mark_failed(db, row, message, *, retryable=True):
    """Record a soft failure and schedule the next attempt.

    Retryable failures back off; a non-retryable one (an image Discord
    definitively rejected) parks the row with its reason visible instead of
    hammering the API.
    """
    attempts = int(row.get("attempts") or 0) + 1
    if not retryable or attempts >= MAX_AUTO_ATTEMPTS:
        await db.execute(
            "UPDATE shop_emoji_queue SET attempts=?, last_error=?, next_attempt_at=NULL, "
            f"updated_at={_now_sql()} WHERE guild_id=? AND product_id=?",
            (attempts, str(message)[:500], row["guild_id"], row["product_id"]))
    else:
        backoff = RETRY_BACKOFF_SECONDS * attempts
        await db.execute(
            "UPDATE shop_emoji_queue SET attempts=?, last_error=?, "
            f"next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now',?), updated_at={_now_sql()} "
            "WHERE guild_id=? AND product_id=?",
            (attempts, str(message)[:500], f"+{backoff} seconds",
             row["guild_id"], row["product_id"]))
    await db.commit()


async def defer_uncertain(db, row, message):
    """An ambiguous create: keep the intent, never retry it blindly.

    The row stays queued and is only advanced after Discord reconciliation
    resolves the asset. ``next_attempt_at`` is used to space out the read-only
    recheck, not to authorise a new POST.
    """
    await db.execute(
        "UPDATE shop_emoji_queue SET last_error=?, "
        f"next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+{RETRY_BACKOFF_SECONDS} seconds'), "
        f"updated_at={_now_sql()} WHERE guild_id=? AND product_id=?",
        (str(message)[:500], row["guild_id"], row["product_id"]))
    await db.commit()


async def descriptor(db, guild_id, product_id):
    """Return a canonicalized view of one queued row (test/report helper)."""
    cur = await db.execute(
        f"SELECT {','.join(QUEUE_FIELDS)} FROM shop_emoji_queue "
        "WHERE guild_id=? AND product_id=?", (guild_id, product_id))
    return _row(await cur.fetchone())


def name_preview(item_name, content_hash, current_name=None):
    """What the emoji for this product *would* be called.

    Pure function over the naming rules in ``utils.shop_emoji`` — used by
    reports and tests so the rename policy has exactly one definition.
    """
    proposed = readable_emoji_name(item_name, content_hash)
    return {"proposed": proposed,
            "current": current_name or deterministic_emoji_name(content_hash),
            "rename_needed": (current_name or deterministic_emoji_name(content_hash)) != proposed}


__all__ = [
    "DEFAULT_BATCH_LIMIT", "MAX_BATCH_LIMIT", "DEFAULT_TIME_BUDGET_SECONDS",
    "RETRY_BACKOFF_SECONDS", "MAX_AUTO_ATTEMPTS", "SCHEMA",
    "ensure_schema", "queue_source_url", "enqueue_product", "enqueue_guild_products",
    "clear_product", "prune_orphans", "due", "stats", "next_attempt_at",
    "record_attempt", "mark_done", "mark_failed", "defer_uncertain", "descriptor",
    "name_preview", "content_identity",
]
