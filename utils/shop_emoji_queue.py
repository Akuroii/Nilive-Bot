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
``utils.shop_emoji`` behind its existing fences; the runtime lives in
``utils/shop_emoji_worker``.

Read-only consumers are untouched by design: Inventory (``cogs/wallet.py``) and
Rank (``utils/rank_card_data.py``) import neither this module nor the worker,
so opening either can never enqueue, download, upload or create.

Three properties this module is responsible for
------------------------------------------------
1. **Terminal is terminal.** A row that exhausts its retry budget is written
   with ``state='parked'``; ``due()`` never selects a parked row. Only an
   explicit owner retry or a genuinely new image URL reactivates it.
2. **Generation fencing.** Every row carries a ``generation``. A worker claims a
   generation before doing network work and every progress write is a
   compare-and-swap on that generation, so a slow worker finishing URL A can
   never delete or overwrite newer intent for URL B. This does not rely on the
   in-process lock: the Dashboard and the Bot are separate processes writing to
   the same SQLite file.
3. **Backfill honesty.** A product is only skipped when its *exact current*
   canonical URL resolves to a ready asset under the *active* application and
   the *correct* guild. An older alias, or another application's alias, never
   suppresses the queue.

Two databases, deliberately
---------------------------
There are two audiences with different authority:

* ``shop_emoji_queue`` — **work**, written by the Dashboard on save and read by
  the Bot.
* ``shop_icon_backfill`` — **owner authorisation**, written only by an explicit
  owner action.

The Bot's periodic discovery reads the *authorisation* table, never the work
table, so it can never enqueue work nobody authorised. Both live in one SQLite
file for a single restart-survivable source of truth.
"""

from __future__ import annotations

import aiosqlite

from utils.shop_emoji import SourceImageError, canonicalize_url

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
# Attempts after which a product is parked for a human. Keeps a permanently
# dead URL from being fetched forever.
MAX_AUTO_ATTEMPTS = 4

# Row states. 'queued' and 'retry' are the only ones ``due()`` will select.
STATE_QUEUED = "queued"
STATE_RETRY = "retry"
STATE_PARKED = "parked"      # terminal until a new URL or an owner retry
STATE_DONE = "done"          # terminal success (kept only when pinned)

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_emoji_queue (
        guild_id      INTEGER NOT NULL,
        product_id    INTEGER NOT NULL,
        item_name     TEXT NOT NULL,
        source_url    TEXT NOT NULL,
        content_hash  TEXT,
        reason        TEXT NOT NULL DEFAULT 'backfill',
        state         TEXT NOT NULL DEFAULT 'queued',
        generation    INTEGER NOT NULL DEFAULT 1,
        queued_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        next_attempt_at TEXT,
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        PRIMARY KEY (guild_id, product_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_icon_backfill (
        guild_id        INTEGER PRIMARY KEY,
        enabled_by      TEXT,
        authorized_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_sweep_at   TEXT,
        last_sweep_result TEXT
    )
    """,
)

# Columns as they existed before `state`/`generation`: used to migrate a queue
# created by the previous revision of this module.
_LEGACY_QUEUE_COLUMNS = {
    "guild_id", "product_id", "item_name", "source_url", "content_hash", "reason",
    "queued_at", "updated_at", "next_attempt_at", "attempts", "last_error",
}


async def ensure_schema(db):
    """Create/upgrade queue tables. Additive only; never alters job tables."""
    for statement in SCHEMA:
        await db.execute(statement)
    cur = await db.execute("PRAGMA table_info(shop_emoji_queue)")
    columns = {row[1] for row in await cur.fetchall()}
    # Same check-then-act race as the asset table: the Bot and the Dashboard can
    # initialise at once, so the ALTER is retried-then-verified rather than
    # assumed to be the only writer.
    from utils.shop_emoji import _add_column

    if await _add_column(db, "shop_emoji_queue", "state",
                         "TEXT NOT NULL DEFAULT 'queued'", columns):
        # A row carrying a future next_attempt_at was backing off, not parked.
        await db.execute(
            "UPDATE shop_emoji_queue SET state=CASE WHEN next_attempt_at IS NULL "
            "THEN 'queued' ELSE 'retry' END")
    await _add_column(db, "shop_emoji_queue", "generation",
                      "INTEGER NOT NULL DEFAULT 1", columns)
    # Built only after the additive columns exist: a queue table created by the
    # previous revision of this module has no `state` column yet.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_emoji_queue_next "
        "ON shop_emoji_queue(state, next_attempt_at)")
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


def _now_minus(seconds):
    """SQLite expression for "now minus N seconds" (string comparison safe)."""
    return f"strftime('%Y-%m-%dT%H:%M:%fZ','now','-{int(seconds)} seconds')"


# ── Enqueue ─────────────────────────────────────────────────────────────────
async def enqueue_product(db, guild_id, product_id, item_name, icon_url, *,
                          reason="product_saved", reset_attempts=False,
                          embed_item_name=False):
    """Queue one product's current icon URL. Returns the canonical URL or None.

    A product with no usable URL is *removed* from the queue rather than stored
    broken: absence is the honest state, and it stops a stale row from being
    retried forever after an admin clears the image.

    Generation semantics — this is the compare-and-swap the worker fences on:

    * new URL       -> ``generation + 1``, attempts reset, state 'queued'
    * same URL      -> a TRUE no-op while the row is queued/retry: no
                       generation bump, no attempt reset, no state change, no
                       ``next_attempt_at``/``last_error`` change. Periodic
                       discovery must never invalidate an in-flight worker or
                       defeat a backoff. A parked row is left parked (only the
                       explicit retry paths revive it).
    * ``embed_item_name`` / ``reset_attempts`` (owner retry) -> reactivate

    The generation is bumped in BOTH the URL-change and the explicit-retry case,
    so an in-flight worker holding the old generation fails its compare-and-swap
    and cannot clobber this newer intent.
    """
    url = queue_source_url(icon_url)
    if url is None:
        await db.execute(
            "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (guild_id, product_id))
        await db.commit()
        return None
    # The same canonical URL with live intent (queued/retry) is a TRUE no-op.
    # Periodic discovery runs while the worker is mid-flight; bumping the
    # generation there would invalidate work that is still correct, and moving
    # `retry` to `queued` would strand a scheduled backoff. A parked row and a
    # genuinely changed URL fall through to the update below.
    if not reset_attempts:
        cur = await db.execute(
            "SELECT state FROM shop_emoji_queue "
            "WHERE guild_id=? AND product_id=? AND source_url=?",
            (guild_id, product_id, url))
        existing = await cur.fetchone()
        if existing and existing[0] in (STATE_QUEUED, STATE_RETRY):
            return url

    await db.execute(
        "INSERT INTO shop_emoji_queue "
        "(guild_id,product_id,item_name,source_url,reason,state,generation,"
        f"queued_at,updated_at,next_attempt_at,attempts) "
        f"VALUES (?,?,?,?,?,'{STATE_QUEUED}',1,{_now_sql()},{_now_sql()},NULL,0) "
        "ON CONFLICT(guild_id,product_id) DO UPDATE SET "
        "  item_name=excluded.item_name,"
        "  updated_at=excluded.updated_at,"
        "  generation=shop_emoji_queue.generation+1,"
        "  reason=excluded.reason,"
        "  source_url=excluded.source_url,"
        "  content_hash=CASE WHEN shop_emoji_queue.source_url=excluded.source_url "
        "                    THEN shop_emoji_queue.content_hash ELSE NULL END,"
        "  attempts=CASE WHEN shop_emoji_queue.source_url=excluded.source_url AND ?=0 "
        "                THEN shop_emoji_queue.attempts ELSE 0 END,"
        "  next_attempt_at=CASE WHEN shop_emoji_queue.source_url=excluded.source_url AND ?=0 "
        "                       THEN shop_emoji_queue.next_attempt_at ELSE NULL END,"
        "  last_error=CASE WHEN shop_emoji_queue.source_url=excluded.source_url AND ?=0 "
        "                  THEN shop_emoji_queue.last_error ELSE NULL END,"
        "  state=CASE WHEN shop_emoji_queue.source_url=excluded.source_url AND ?=0 "
        "                  AND shop_emoji_queue.state='parked' "
        "             THEN shop_emoji_queue.state ELSE 'queued' END",
        (guild_id, product_id, item_name, url, reason,
         1 if reset_attempts else 0, 1 if reset_attempts else 0,
         1 if reset_attempts else 0, 1 if reset_attempts else 0))
    await db.commit()
    return url


async def reactivate(db, guild_id, product_id, *, expect_generation=None,
                     expect_source_url=None):
    """Owner retry of a parked row: new generation, fresh retry budget.

    ``expect_generation``/``expect_source_url`` turn this into a compare-and-swap
    so an OLD Backfill snapshot cannot reset a newer intent that arrived after
    its catalog was read. Returns True when a row was actually reactivated.
    """
    sql = ("UPDATE shop_emoji_queue SET state=?, generation=generation+1, attempts=0, "
           f"next_attempt_at=NULL, last_error=NULL, updated_at={_now_sql()} "
           "WHERE guild_id=? AND product_id=?")
    params = [STATE_QUEUED, guild_id, product_id]
    if expect_generation is not None:
        sql += " AND generation=?"
        params.append(int(expect_generation))
    if expect_source_url is not None:
        sql += " AND source_url=?"
        params.append(expect_source_url)
    cur = await db.execute(sql, tuple(params))
    await db.commit()
    return bool(cur.rowcount)


async def enqueue_guild_products(db, guild_id, *, reason="backfill",
                                 only_unprepared=True, application_id=None,
                                 limit=None, reactivate_parked=False):
    """Queue every eligible listing in one guild — the bulk backfill entry point.

    "Eligible" is deliberately narrow and read straight from the authoritative
    table, so no Shop Designer roster entry is needed:

    * the row exists in ``shop_items`` for this guild,
    * it carries a non-empty ``icon_url`` that canonicalizes,
    * it is enabled (a disabled listing is not for sale).

    With ``only_unprepared`` a product is skipped **only** when its exact
    current canonical URL resolves to a ``ready`` asset under the given
    application AND this guild. An older alias, another application's alias, a
    wrong-guild alias, or a non-ready asset never suppresses the queue — that
    false positive is what silently left products unprepared before.

    ``reactivate_parked`` is the owner's explicit "retry the parked ones too"
    switch. It goes through ``reactivate()`` — the same fenced operation
    ``/emojis/retry`` uses — so the row really becomes ``queued`` with a fresh
    budget and a new generation, instead of being counted as work while staying
    invisible to ``due()``.

    Returns ``{"queued": <new intent>, "reactivated": <parked rows revived>}``.
    """
    from utils.shop_emoji import configured_application_id

    app_id = str(application_id or configured_application_id() or "")
    params = [guild_id]
    sql = ("SELECT s.id, s.name, s.icon_url FROM shop_items s "
           "WHERE s.guild_id=? AND s.icon_url IS NOT NULL AND TRIM(s.icon_url)<>'' "
           "AND COALESCE(s.enabled,1)=1 ORDER BY s.id")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    cur = await db.execute(sql, tuple(params))
    rows = await cur.fetchall()
    if not rows:
        # Same shape on every path: callers must never see an int here.
        return {"queued": 0, "reactivated": 0}

    # Resolve "already prepared" through the SAME predicate the Dashboard report
    # uses, so the two can never disagree. An unusable (non-numeric/zero/NULL)
    # emoji ID is not prepared and must not suppress backfill.
    ready_urls = set()
    if only_unprepared and app_id:
        for _pid, _name, raw in rows:
            canon = queue_source_url(raw)
            if canon and canon in ready_urls:
                continue
            if canon and await _resolves_ready(db, app_id, guild_id, canon):
                ready_urls.add(canon)

    # One read of the current queue, so "queued" reports NEW intent rather than
    # rows that were already pending. This is what makes a re-run provably a
    # no-op instead of an unobservable upsert.
    cur = await db.execute(
        f"SELECT {','.join(QUEUE_FIELDS)} FROM shop_emoji_queue WHERE guild_id=?",
        (guild_id,))
    existing_rows = {r["product_id"]: r for r in (_row(x) for x in await cur.fetchall())}

    queued = 0
    reactivated = 0
    for product_id, name, icon_url in rows:
        pid = int(product_id)
        canon = queue_source_url(icon_url)
        if only_unprepared and canon and canon in ready_urls:
            continue
        existing = existing_rows.get(pid)
        if existing:
            same_url = existing["source_url"] == canon
            if same_url and existing["state"] == STATE_PARKED:
                if not reactivate_parked:
                    # Parked for this exact URL: ordinary discovery must keep it
                    # parked. Only an explicit owner retry may revive it.
                    continue
                # The owner asked for it: use the SAME explicit reactivation the
                # /emojis/retry endpoint uses. Calling enqueue_product() here
                # would preserve the parked state for an unchanged URL, so the
                # row would be reported as queued while due() still ignored it.
                if await reactivate(db, guild_id, pid,
                                    expect_generation=existing["generation"],
                                    expect_source_url=existing["source_url"]):
                    reactivated += 1
                continue
            if same_url and existing["state"] in (STATE_QUEUED, STATE_RETRY):
                # Already pending for this URL: true no-op (kept for clarity even
                # though enqueue_product() now enforces it too).
                continue
        if await enqueue_product(db, guild_id, pid, name, icon_url,
                                 reason=reason) is not None:
            queued += 1
    return {"queued": queued, "reactivated": reactivated}


async def usable_emoji_id_for(db, application_id, guild_id, canonical_url):
    """The emoji ID a product's CURRENT image resolves to, if it is usable.

    Single source of truth for "prepared successfully". Backfill, the Dashboard
    report and the worker all call this, so they can never disagree about
    whether a product still needs work. "Usable" is the read-side rule
    (``shop_emoji._valid_emoji_id``: a positive numeric ID), because an ID that
    Inventory and Rank cannot render is not prepared, whatever its row says.
    """
    if not canonical_url:
        return None
    from utils.shop_emoji import _valid_emoji_id

    cur = await db.execute(
        "SELECT e.emoji_id FROM shop_emoji_url_aliases a "
        "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
        "  AND e.content_hash=a.content_hash "
        "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=? "
        "AND e.status='ready' ORDER BY e.emoji_id",
        (application_id, guild_id, canonical_url))
    for (emoji_id,) in await cur.fetchall():
        if _valid_emoji_id(emoji_id):
            return str(emoji_id)
    return None


async def _resolves_ready(db, application_id, guild_id, canonical_url):
    """True only when this exact URL has a *usable* ready asset, this app+guild.

    "Usable" is deliberately the SAME rule the read side uses
    (``shop_emoji._valid_emoji_id``, via ``attach_prepared_emojis``): a positive
    numeric ID. A ``ready`` row whose ``emoji_id`` is NULL, ``''``, ``'abc'`` or
    ``0`` renders in neither Inventory nor Rank, so it must never suppress
    backfill — otherwise the product looks prepared and is permanently
    invisible. There is exactly one definition, imported from the consumer path.
    """
    return await usable_emoji_id_for(
        db, application_id, guild_id, canonical_url) is not None


# ── Backfill authorisation (owner-gated discovery) ──────────────────────────
async def classify_catalog(db, guild_id, application_id=None):
    """Every eligible listing, split into prepared / unprepared / unsupported.

    The Dashboard report is built from this so its numbers are BY CONSTRUCTION
    the same partition Backfill acts on: a product is either already usable, or
    queueable, or its stored URL cannot be used at all. Nothing is silently
    dropped, and ``fully_prepared`` therefore cannot be true while an eligible
    product was excluded from the accounting.
    """
    from utils.shop_emoji import configured_application_id

    app_id = str(application_id or configured_application_id() or "")
    cur = await db.execute(
        "SELECT id,name,icon_url FROM shop_items WHERE guild_id=? "
        "AND icon_url IS NOT NULL AND TRIM(icon_url)<>'' AND COALESCE(enabled,1)=1 "
        "ORDER BY id", (guild_id,))
    rows = await cur.fetchall()

    prepared, unprepared, unsupported = [], [], []
    for product_id, name, raw in rows:
        canon = queue_source_url(raw)
        if canon is None:
            unsupported.append({"product_id": int(product_id), "name": name,
                                "icon_url": raw,
                                "reason": "The stored image URL is not a usable HTTPS URL."})
            continue
        emoji_id = (await usable_emoji_id_for(db, app_id, guild_id, canon)
                    if app_id else None)
        entry = {"product_id": int(product_id), "name": name, "source_url": canon,
                 "emoji_id": emoji_id}
        (prepared if emoji_id else unprepared).append(entry)
    return {"eligible": len(rows), "prepared": prepared,
            "unprepared": unprepared, "unsupported": unsupported}


async def authorize_backfill(db, guild_id, *, actor=None):
    """Record that an owner asked for this guild's existing catalog to be filled.

    Periodic discovery refuses to create work for a guild without this row, so
    deploying the build can never start creating application emojis for every
    connected server.
    """
    await db.execute(
        "INSERT INTO shop_icon_backfill (guild_id,enabled_by,authorized_at) "
        f"VALUES (?,?,{_now_sql()}) ON CONFLICT(guild_id) DO UPDATE SET "
        "enabled_by=excluded.enabled_by, authorized_at=excluded.authorized_at",
        (guild_id, str(actor) if actor is not None else None))
    await db.commit()


async def is_authorized(db, guild_id):
    cur = await db.execute(
        "SELECT 1 FROM shop_icon_backfill WHERE guild_id=?", (guild_id,))
    return await cur.fetchone() is not None


async def authorized_guilds(db):
    cur = await db.execute("SELECT guild_id FROM shop_icon_backfill ORDER BY guild_id")
    return [row[0] for row in await cur.fetchall()]


async def revoke_backfill(db, guild_id):
    await db.execute("DELETE FROM shop_icon_backfill WHERE guild_id=?", (guild_id,))
    await db.commit()


async def record_sweep(db, guild_id, result):
    await db.execute(
        "UPDATE shop_icon_backfill SET last_sweep_at=" + _now_sql() +
        ", last_sweep_result=? WHERE guild_id=?", (str(result)[:200], guild_id))
    await db.commit()


# ── Lifecycle ───────────────────────────────────────────────────────────────
async def clear_product(db, guild_id, product_id, *, generation=None):
    """Drop queued work for a product (deleted listing, cleared image).

    With ``generation`` the delete is a compare-and-swap: a worker acting on an
    older snapshot must never destroy intent that was written after it. Returns
    True when a row was removed.
    """
    if generation is None:
        cur = await db.execute(
            "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=?",
            (guild_id, product_id))
    else:
        cur = await db.execute(
            "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=? "
            "AND generation=?", (guild_id, product_id, int(generation)))
    await db.commit()
    return bool(cur.rowcount)


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
                "reason", "state", "generation", "queued_at", "updated_at",
                "next_attempt_at", "attempts", "last_error")


def _row(row):
    return dict(zip(QUEUE_FIELDS, row)) if row else None


async def due(db, *, guild_id=None, limit=DEFAULT_BATCH_LIMIT):
    """Rows eligible for a pass right now, oldest intent first.

    Parked (terminal) rows are excluded here, which is what makes ``mark_failed``
    after ``MAX_AUTO_ATTEMPTS`` actually stop the work rather than re-admit it.
    """
    sql = (f"SELECT {','.join(QUEUE_FIELDS)} FROM shop_emoji_queue "
           f"WHERE state IN ('{STATE_QUEUED}','{STATE_RETRY}') "
           "AND (next_attempt_at IS NULL OR next_attempt_at <= " + _now_sql() + ")")
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
        " SUM(CASE WHEN state='parked' THEN 1 ELSE 0 END), "
        " SUM(CASE WHEN next_attempt_at IS NOT NULL AND next_attempt_at > "
        "   strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN 1 ELSE 0 END), "
        " SUM(CASE WHEN last_error IS NOT NULL AND state<>'parked' THEN 1 ELSE 0 END), "
        " MAX(attempts) FROM shop_emoji_queue" + where, tuple(params))
    total, parked, deferred, errored, max_attempts = await cur.fetchone()
    # `deferred` is EVERY row in a future backoff — a source failure waiting to
    # be retried as much as an uncertain create pending reconciliation. Callers
    # must not label the whole count "uncertain"; `retrying_backoff` names the
    # same set explicitly, and `uncertain_queue` counts only genuinely ambiguous
    # creations so the two can never be confused in the UI.
    cur = await db.execute(
        "SELECT COUNT(*) FROM shop_emoji_queue WHERE state='retry' "
        "AND last_error LIKE '%uncertain%' " + ("AND guild_id=?" if guild_id is not None else ""),
        tuple(params))
    uncertain_queue = (await cur.fetchone())[0] or 0
    return {"total": total or 0, "parked": parked or 0, "deferred": deferred or 0,
            "retrying_backoff": deferred or 0,
            "uncertain": uncertain_queue,
            "errored": errored or 0, "max_attempts": max_attempts or 0}


async def parked_rows(db, *, guild_id=None, limit=MAX_BATCH_LIMIT):
    """Terminal rows with their reason, for the owner-facing report."""
    sql = ("SELECT product_id,item_name,attempts,last_error,source_url FROM shop_emoji_queue "
           "WHERE state='parked'")
    params = []
    if guild_id is not None:
        sql += " AND guild_id=?"
        params.append(guild_id)
    sql += " ORDER BY product_id LIMIT ?"
    params.append(int(limit))
    cur = await db.execute(sql, tuple(params))
    return [{"product_id": r[0], "item_name": r[1], "attempts": r[2],
             "last_error": r[3]} for r in await cur.fetchall()]


async def next_attempt_at(db, *, guild_id=None):
    """Earliest future retry time (guild-scoped when a guild is given)."""
    sql = ("SELECT MIN(next_attempt_at) FROM shop_emoji_queue "
           f"WHERE state IN ('{STATE_QUEUED}','{STATE_RETRY}') "
           "AND next_attempt_at IS NOT NULL AND next_attempt_at > " + _now_sql())
    params = []
    if guild_id is not None:
        sql += " AND guild_id=?"
        params.append(guild_id)
    cur = await db.execute(sql, tuple(params))
    row = await cur.fetchone()
    return row[0] if row else None


# ── Progress: every write is a generation compare-and-swap ─────────────────
def _fence(row):
    return (row["guild_id"], row["product_id"], int(row["generation"]))


async def record_attempt(db, row, *, content_hash=None):
    """Persist that we are about to work on this generation.

    Returns True when the row is still the generation we hold. False means a
    newer intent replaced it while we were deciding; the caller must abandon.
    """
    cur = await db.execute(
        "UPDATE shop_emoji_queue SET attempts=attempts+1, content_hash=COALESCE(?,content_hash), "
        f"updated_at={_now_sql()} WHERE guild_id=? AND product_id=? AND generation=?",
        (content_hash, *_fence(row)))
    await db.commit()
    return bool(cur.rowcount)


async def mark_done(db, row):
    """Finish this generation. Deletes the row only if it is still ours.

    If the Dashboard has since queued a newer URL (generation bumped), the
    row is left untouched so the new intent survives.
    """
    cur = await db.execute(
        "DELETE FROM shop_emoji_queue WHERE guild_id=? AND product_id=? AND generation=?",
        _fence(row))
    await db.commit()
    return bool(cur.rowcount)


async def mark_failed(db, row, message, *, retryable=True, count_attempt=False):
    """Record a failure for THIS generation; park it when the budget is spent.

    The attempt is normally counted by ``record_attempt`` at claim time (so a
    process killed mid-download still consumes budget). ``count_attempt=True``
    is for paths that fail before a claim, so one attempt is never counted twice.

    Parked rows are excluded by ``due()``, so a dead URL stops being fetched
    instead of looping. A stale generation is ignored entirely.
    """
    attempts = int(row.get("attempts") or 0) + (1 if count_attempt else 0)
    attempts = max(attempts, 1)
    if not retryable or attempts >= MAX_AUTO_ATTEMPTS:
        cur = await db.execute(
            "UPDATE shop_emoji_queue SET attempts=?, last_error=?, state=?, "
            f"next_attempt_at=NULL, updated_at={_now_sql()} "
            "WHERE guild_id=? AND product_id=? AND generation=?",
            (attempts, str(message)[:500], STATE_PARKED, *_fence(row)))
    else:
        backoff = RETRY_BACKOFF_SECONDS * attempts
        cur = await db.execute(
            "UPDATE shop_emoji_queue SET attempts=?, last_error=?, state=?, "
            f"next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now',?), updated_at={_now_sql()} "
            "WHERE guild_id=? AND product_id=? AND generation=?",
            (attempts, str(message)[:500], STATE_RETRY, f"+{backoff} seconds", *_fence(row)))
    await db.commit()
    return bool(cur.rowcount)


async def defer_uncertain(db, row, message):
    """An ambiguous create: keep the intent, never retry it blindly.

    The row stays live so reconciliation can still advance it, but it is not
    selected again until the read-only recheck window elapses. A stale
    generation is ignored.
    """
    cur = await db.execute(
        "UPDATE shop_emoji_queue SET last_error=?, state=?, "
        f"next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+{RETRY_BACKOFF_SECONDS} seconds'), "
        f"updated_at={_now_sql()} WHERE guild_id=? AND product_id=? AND generation=?",
        (str(message)[:500], STATE_RETRY, *_fence(row)))
    await db.commit()
    return bool(cur.rowcount)


async def still_current(db, row):
    """Is this exact generation still the queued intent? (cheap pre-check)."""
    cur = await db.execute(
        "SELECT 1 FROM shop_emoji_queue WHERE guild_id=? AND product_id=? AND generation=?",
        _fence(row))
    return await cur.fetchone() is not None


async def descriptor(db, guild_id, product_id):
    """Return a canonicalized view of one queued row (report/test helper)."""
    cur = await db.execute(
        f"SELECT {','.join(QUEUE_FIELDS)} FROM shop_emoji_queue "
        "WHERE guild_id=? AND product_id=?", (guild_id, product_id))
    return _row(await cur.fetchone())


__all__ = [
    "DEFAULT_BATCH_LIMIT", "MAX_BATCH_LIMIT", "DEFAULT_TIME_BUDGET_SECONDS",
    "RETRY_BACKOFF_SECONDS", "MAX_AUTO_ATTEMPTS", "SCHEMA",
    "STATE_QUEUED", "STATE_RETRY", "STATE_PARKED", "STATE_DONE",
    "ensure_schema", "queue_source_url", "enqueue_product", "enqueue_guild_products",
    "reactivate", "authorize_backfill", "is_authorized", "authorized_guilds",
    "revoke_backfill", "record_sweep", "clear_product", "prune_orphans", "due",
    "stats", "parked_rows", "next_attempt_at", "record_attempt", "mark_done",
    "mark_failed", "defer_uncertain", "still_current", "descriptor",
]
