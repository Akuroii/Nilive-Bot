"""Background processor for the Shop icon preparation queue.

The worker is the only automatic caller of anything that can create an emoji,
and it does so by delegating to the *existing* explicit machinery in
``utils.shop_emoji``:

    queue row -> _prepare_one() -> (_create_claimed / _reconcile_asset / ...)

Nothing here re-implements creation, claiming, quota, hashing, ownership
verification, duplicate detection or recovery. That is deliberate: the manual
"prepare" action and this loop must share one set of fences, or two writers
could race on the same content hash.

Safety properties
-----------------
* **Bounded.** ``DEFAULT_BATCH_LIMIT`` rows and ``DEFAULT_TIME_BUDGET_SECONDS``
  per pass, one ``list_emojis()`` snapshot per pass, and a hard stop before any
  new item starts once the budget is spent.
* **Resumable.** Progress lives in SQLite, so a Railway restart mid-pass simply
  leaves the row queued. ``prepare`` is idempotent per (guild, product).
* **Generation-fenced.** The row is claimed with its ``generation`` before any
  network work, and every progress write is a compare-and-swap on that
  generation. A worker finishing URL A therefore cannot delete or overwrite
  intent the Dashboard queued for URL B meanwhile. This does not depend on the
  in-process lock: the Dashboard and Bot are separate processes.
* **Idle is free.** An empty queue produces zero Discord requests.
* **Ambiguity is never retried blindly.** ``_prepare_one`` already treats an
  unconfirmed POST as ``uncertain``; the worker reacts by reconciling against
  Discord (read-only) and deferring, never by issuing a second create.
* **No deletion, ever.** The worker has no code path that calls ``delete_emoji``.
* **Read-only consumers stay read-only.** Inventory and Rank import neither this
  module nor the queue.

Renaming
--------
A shared image is one emoji serving several products, so its name must not
depend on which product was processed first. The name is derived from the
**canonical product label** of the content (``canonical_product_label``): the
lexicographically smallest listing name bound to that asset. That is stable
across queue order, guild order, restarts and repeated passes, so repeated runs
issue zero extra PATCHes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiosqlite

from database import DB_PATH
from utils import shop_emoji as SE
from utils import shop_emoji_queue as Q

log = logging.getLogger(__name__)

# Serialises passes *within* this process. Cross-process safety comes from the
# generation compare-and-swap in the queue and the DB fences in _prepare_one,
# never from this lock.
_pass_lock = None


def _lock():
    global _pass_lock
    if _pass_lock is None:
        _pass_lock = asyncio.Lock()
    return _pass_lock


class PassReport(dict):
    """Plain dict subclass so callers/tests read fields without a class."""


# Discord told this process to wait (HTTP 429, Retry-After / retry_after). Kept as
# a wall-clock deadline so every later pass in this process stays quiet until it
# elapses instead of probing Discord again every tick. Per-process by design; a
# second process learns the same limit from its own 429.
_rate_limit_until = 0.0


def _rate_limit_remaining():
    return max(0.0, _rate_limit_until - time.time())


def _note_rate_limit(retry_after, report=None):
    global _rate_limit_until
    seconds = max(0.0, float(retry_after))
    _rate_limit_until = max(_rate_limit_until, time.time() + seconds)
    if report is not None:
        report["rate_limited"] = report.get("rate_limited", 0) + 1
        report["rate_limited_for"] = round(_rate_limit_remaining(), 3)
    log.warning("shop emoji worker: Discord rate limited; next Discord request in this "
                "process no earlier than %.1fs from now", seconds)


def _empty_report():
    return PassReport(processed=0, created=0, reused=0, reconciled=0, renamed=0,
                      deferred=0, failed=0, parked=0, skipped=0, stale=0, pruned=0,
                      budget_exhausted=False, rate_limited=0, results=[])


async def _load_listing(db, guild_id, product_id):
    cur = await db.execute(
        "SELECT id, guild_id, name, icon_url, COALESCE(enabled,1) FROM shop_items "
        "WHERE id=? AND guild_id=?", (product_id, guild_id))
    return await cur.fetchone()


async def _alias_asset(db, application_id, guild_id, source_url):
    """Prepared asset for this exact URL in this guild, if any (read-only).

    "Prepared" means ready AND a syntactically valid bound emoji ID, the same
    definition the report, Backfill and attachment use. A ready row with an
    unusable ID is deliberately NOT returned: it must not be reused or marked
    done. It continues into ``_prepare_one``, which withdraws its trust and holds
    it as an unreconciled asset instead of creating a replacement.
    """
    cur = await db.execute(
        f"SELECT {SE._ASSET_SELECT_E} FROM shop_emoji_url_aliases a "
        "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
        "AND e.content_hash=a.content_hash "
        "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=? "
        "AND e.status='ready' AND " + SE.SQL_VALID_EMOJI_ID.format(col="e.emoji_id"),
        (application_id, guild_id, source_url))
    return SE._asset(await cur.fetchone())


async def _maybe_rename(db, discord, asset, report):
    """Rename a ready, bound asset to its canonical (label-based) name.

    The label is re-derived from the alias table, so a shared emoji always gets
    the same name regardless of which product triggered this call.
    """
    if not isinstance(asset, dict) or asset.get("status") != "ready":
        return asset
    label = await SE.canonical_label_for_asset(db, asset["application_id"],
                                               asset["content_hash"])
    target = SE.rename_target_name(asset, label)
    if not target:
        return asset
    return await _rename_or_defer(db, discord, asset, target, report)


async def _rename_or_defer(db, discord, asset, target, report):
    # Mark management FIRST: if the PATCH succeeds and we crash before the name
    # is written, the next _refresh_ready_asset must not call this a conflict.
    await db.execute(
        "UPDATE shop_emoji_assets SET managed_name=1, updated_at=? "
        "WHERE application_id=? AND content_hash=?",
        (SE._iso(), asset["application_id"], asset["content_hash"]))
    await db.commit()
    if _rate_limit_remaining() > 0:
        report["results"].append({"code": "rename_rate_limited",
                                  "message": "Discord asked this process to wait; rename deferred.",
                                  "content_hash": asset["content_hash"]})
        return await SE._get_asset(db, asset["application_id"], asset["content_hash"])
    try:
        await discord.rename_emoji(asset["emoji_id"], target)
    except SE.DiscordRateLimited as exc:
        # Cosmetic: nothing changed. Remember the wait so no further request is
        # sent; the rename is retried by a later pass.
        _note_rate_limit(exc.retry_after, report)
        report["results"].append({"code": "rename_rate_limited", "message": str(exc),
                                  "content_hash": asset["content_hash"],
                                  "retry_after": exc.retry_after})
        return await SE._get_asset(db, asset["application_id"], asset["content_hash"])
    except SE.DiscordAPIError as exc:
        if exc.uncertain:
            # Cosmetic and identity-preserving: the next refresh accepts either
            # name for a managed asset, so this needs no repair.
            report["results"].append({"code": "rename_uncertain", "message": str(exc),
                                      "content_hash": asset["content_hash"],
                                      "emoji_id": str(asset["emoji_id"]),
                                      "guild_id": asset.get("creator_guild_id"),
                                      "current_name": asset.get("emoji_name"),
                                      "proposed_name": target})
            return await SE._get_asset(db, asset["application_id"], asset["content_hash"])
        report["results"].append({"code": "rename_rejected", "message": str(exc),
                                  "content_hash": asset["content_hash"],
                                  "emoji_id": str(asset["emoji_id"]),
                                  "guild_id": asset.get("creator_guild_id"),
                                  "current_name": asset.get("emoji_name"),
                                  "proposed_name": target})
        await db.execute(
            "UPDATE shop_emoji_assets SET managed_name=0, updated_at=? "
            "WHERE application_id=? AND content_hash=?",
            (SE._iso(), asset["application_id"], asset["content_hash"]))
        await db.commit()
        return await SE._get_asset(db, asset["application_id"], asset["content_hash"])
    await db.execute(
        "UPDATE shop_emoji_assets SET emoji_name=?, managed_name=1, updated_at=? "
        "WHERE application_id=? AND content_hash=? AND emoji_id=?",
        (target, SE._iso(), asset["application_id"], asset["content_hash"],
         asset["emoji_id"]))
    await db.commit()
    report["renamed"] += 1
    report["results"].append({"code": "renamed",
                              "content_hash": asset["content_hash"],
                              "emoji_id": str(asset["emoji_id"]),
                              "guild_id": asset.get("creator_guild_id"),
                              "current_name": asset.get("emoji_name"),
                              "proposed_name": target,
                              "renamed": True})
    return await SE._get_asset(db, asset["application_id"], asset["content_hash"])


async def _source_still_current(db, row):
    """Is the image this worker is preparing STILL the product's current image?

    Called after the download finishes and immediately before any creation
    request. Two independent facts must both still hold:

    * the queue generation this worker claimed is still the queued intent
      (nothing newer was written), and
    * ``shop_items.icon_url`` still canonicalizes to the URL being prepared.

    Either check alone is insufficient. The generation catches a Dashboard save
    that queued a new URL; the listing check also catches an edit that has not
    yet produced a queue row, and a listing that was disabled or cleared.
    """
    if not await Q.still_current(db, row):
        return False
    listing = await _load_listing(db, row["guild_id"], row["product_id"])
    if listing is None:
        return False
    _id, _gid, _name, icon_url, enabled = listing
    if not enabled:
        return False
    return Q.queue_source_url(icon_url) == row["source_url"]


async def _asset_by_hash(db, application_id, content_hash):
    if not content_hash:
        return None
    return await SE._get_asset(db, application_id, content_hash)


async def _process_row(db, discord, row, remote, bot_user_id, report):
    """Advance one queue row by exactly one step.

    Returns a short status string. Every progress write is generation-fenced,
    and the row is re-validated against ``shop_items`` right before network
    work, so work for a URL that has since changed is abandoned rather than
    completed into stale intent.
    """
    guild_id, product_id = row["guild_id"], row["product_id"]
    listing = await _load_listing(db, guild_id, product_id)
    if listing is None:
        # Intent is void, but only for THIS generation: mark_done is the
        # compare-and-swap, so a newer row written meanwhile survives intact.
        if not await Q.mark_done(db, row):
            report["stale"] += 1
            return "stale_listing_gone"
        report["skipped"] += 1
        return "listing_gone"
    _id, _gid, item_name, icon_url, enabled = listing
    if not enabled:
        # Fenced completion FIRST. Only if this generation is still the current
        # intent may the row be removed; a failed CAS means the Dashboard has
        # already written newer intent, which must not be destroyed.
        if not await Q.mark_done(db, row):
            report["stale"] += 1
            return "stale_listing_disabled"
        await Q.clear_product(db, guild_id, product_id,
                              generation=row["generation"])
        report["skipped"] += 1
        return "listing_disabled"

    current_url = Q.queue_source_url(icon_url)
    if current_url is None:
        # Same rule: never delete a generation this worker does not own.
        if not await Q.clear_product(db, guild_id, product_id,
                                     generation=row["generation"]):
            report["stale"] += 1
            return "stale_no_usable_url"
        report["skipped"] += 1
        return "no_usable_url"
    if current_url != row["source_url"]:
        # The listing changed after this generation was written. Re-queue for the
        # new URL instead of preparing an image the storefront no longer uses —
        # but ONLY against the snapshot we read. The write happens after several
        # awaits, so the Dashboard may already have saved something newer; an
        # unconditional enqueue would overwrite it with our stale URL.
        if await Q.requeue_if_current(db, row, item_name, current_url):
            report["skipped"] += 1
            return "requeued_new_url"
        report["stale"] += 1
        return "stale_requeue"

    # ── already prepared for THIS exact URL? (reuse) ─────────────────────
    ready = await _alias_asset(db, discord.application_id, guild_id, current_url)
    if ready is not None:
        await _maybe_rename(db, discord, ready, report)
        if not await Q.mark_done(db, row):
            report["stale"] += 1
            return "stale"
        report["reused"] += 1
        return "reused"

    # Claim this generation. If the Dashboard replaced it meanwhile, stop.
    if not await Q.record_attempt(db, row):
        report["stale"] += 1
        return "stale"
    # Re-read: another process may have changed the URL between claim and now.
    fresh = await Q.descriptor(db, guild_id, product_id)
    if fresh is None or fresh["generation"] != row["generation"] \
            or fresh["source_url"] != row["source_url"]:
        report["stale"] += 1
        return "stale"
    row = fresh

    # Resolve the canonical label ONCE, before the hash exists, so a new emoji
    # is created with the same stable name a later rename would choose.
    label = SE.canonical_product_label(
        await _labels_for(db, guild_id, current_url, item_name))
    try:
        # The single existing entry point: hashing, aliasing, reconciliation,
        # create-fencing, quota and duplicate protection all live inside it.
        result, remote = await SE._prepare_one(
            db, discord, guild_id, current_url, remote, bot_user_id,
            False, False,
            emoji_name=lambda h: SE.safe_emoji_name(
                SE.readable_emoji_name(label or "", h), h),
            # Download happens inside _prepare_one; this hook runs after it and
            # before any creation request, so an obsolete image can never be
            # uploaded even though the download started while it was current.
            validate_source=lambda: _source_still_current(db, row))
    except SE.StalePreparation as exc:
        # The product's image changed while it was downloading. Nothing was
        # created. Leave the row alone: if a newer generation exists, the CAS
        # below protects it; if not, the next pass re-validates from scratch.
        report["stale"] += 1
        report["results"].append({"product_id": product_id,
                                  "code": "stale_preparation",
                                  "message": str(exc)})
        return "stale"
    except SE.SourceImageError as exc:
        await Q.mark_failed(db, row, f"{exc.code}: {exc}", retryable=True)
        report["failed"] += 1
        report["results"].append({"product_id": product_id, "code": exc.code,
                                  "message": str(exc)})
        return "source_error"
    except SE.DiscordRateLimited as exc:
        # Not a business failure: honour the server-provided delay, hand back the
        # attempt counted at claim time and stop spending requests. Any claim the
        # pipeline held was released to the state it came from.
        _note_rate_limit(exc.retry_after, report)
        await Q.defer_without_attempt(db, row, f"discord_rate_limited: {exc}", exc.retry_after)
        log.info("shop emoji worker: product %s rescheduled no earlier than %.1fs from now "
                 "(Discord rate limit; attempt not counted)", product_id, float(exc.retry_after))
        report["deferred"] += 1
        report["results"].append({"product_id": product_id, "code": "rate_limited",
                                  "retry_after": exc.retry_after, "message": str(exc)})
        return "rate_limited"
    except SE.ShopEmojiError as exc:
        # Quota is a real, reportable state; do not turn it into a hot retry
        # loop (mark_failed backs off), and never raise the limit silently.
        await Q.mark_failed(db, row, f"{exc.code}: {exc}", retryable=True)
        report["failed"] += 1
        report["results"].append({"product_id": product_id, "code": exc.code,
                                  "message": str(exc)})
        return "shop_error"
    except SE.DiscordAPIError as exc:
        await Q.mark_failed(db, row, str(exc), retryable=True)
        report["failed"] += 1
        report["results"].append({"product_id": product_id, "code": exc.code,
                                  "message": str(exc)})
        return "discord_error"

    status = result.get("status")
    code = result.get("code")
    if status == "ready":
        asset = await _asset_by_hash(db, discord.application_id, result.get("content_hash"))
        if asset is not None:
            await _maybe_rename(db, discord, asset, report)
        if not await Q.mark_done(db, row):
            report["stale"] += 1
            report["results"].append({"product_id": product_id, "code": "stale_but_prepared",
                                      "status": status})
            return "stale"
        if code in {"created", "remapped_changed_source"}:
            report["created"] += 1
        else:
            report["reused"] += 1
        report["results"].append({"product_id": product_id, "code": code,
                                  "status": status, "emoji_id": result.get("emoji_id")})
        return "ready"

    if result.get("rate_limit_retry_after") is not None:
        # The create request itself was rate limited. The asset is `uncertain`
        # (a 429 on a mutating request stays ambiguous), so nothing is retried
        # blindly; the row is rescheduled no earlier than Discord's delay or the
        # normal recheck window, whichever is later, and the attempt is refunded.
        retry_after = float(result["rate_limit_retry_after"])
        _note_rate_limit(retry_after, report)
        await Q.defer_without_attempt(
            db, row, f"discord_rate_limited: {result.get('message')}",
            max(retry_after, Q.RETRY_BACKOFF_SECONDS))
        log.info("shop emoji worker: product %s rescheduled no earlier than %.1fs from now "
                 "(rate-limited create; asset stays uncertain; attempt not counted)",
                 product_id, max(retry_after, float(Q.RETRY_BACKOFF_SECONDS)))
        report["deferred"] += 1
        report["results"].append({"product_id": product_id, "code": "rate_limited",
                                  "status": status, "retry_after": retry_after})
        return "rate_limited"
    if code == "emoji_state_unknown":
        # Discord did not say whether a previously bound emoji still exists. No
        # replacement may be created, and an inconclusive answer is not a failure.
        await Q.defer_without_attempt(db, row, result.get("message") or code, Q.RETRY_BACKOFF_SECONDS)
        report["deferred"] += 1
        report["results"].append({"product_id": product_id, "code": code, "status": status})
        return "state_unknown"

    if status in {"preparing", "uncertain"}:
        # Ambiguous create. Never POST again from here: reconcile read-only and
        # defer the row until Discord can tell us what actually happened.
        await Q.defer_uncertain(db, row, result.get("message") or code or status)
        report["deferred"] += 1
        report["results"].append({"product_id": product_id, "code": code,
                                  "status": status, "duplicates": result.get("duplicates")})
        return "uncertain"

    await Q.mark_failed(db, row, result.get("message") or code or "failed",
                        retryable=code not in {"emoji_capacity"})
    report["failed"] += 1
    report["results"].append({"product_id": product_id, "code": code,
                              "status": status, "duplicates": result.get("duplicates")})
    return "failed"


async def _labels_for(db, guild_id, source_url, fallback_name):
    """Every product name bound to this URL in this guild (plus the current one).

    The canonical label must not depend on which row triggered the work, so the
    full bound set is resolved before the name is chosen. Matching goes through
    the SAME canonicalization the alias table uses (``SE._canonical_or_raw``):
    ``shop_items.icon_url`` keeps whatever the owner typed, so raw string
    equality would miss an uppercase host or an explicit ``:443``.
    """
    cur = await db.execute(
        "SELECT name, icon_url FROM shop_items WHERE guild_id=?", (guild_id,))
    wanted = SE._canonical_or_raw(source_url)
    names = [name for name, icon_url in await cur.fetchall()
             if SE._canonical_or_raw(icon_url) == wanted]
    if fallback_name and fallback_name not in names:
        names.append(fallback_name)
    return names


async def _ambiguous_count(db, application_id=None):
    """How many assets still need a reconciliation read (cheap indexed count)."""
    app_id = application_id or SE.configured_application_id()
    if not app_id:
        return 0
    cur = await db.execute(
        "SELECT COUNT(*) FROM shop_emoji_assets WHERE application_id=? "
        "AND status IN ('uncertain','preparing')", (app_id,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def run_pass(*, guild_id=None, limit=Q.DEFAULT_BATCH_LIMIT,
                   time_budget=Q.DEFAULT_TIME_BUDGET_SECONDS,
                   reconcile_uncertain=True, allow_create=True):
    """One bounded pass over the queue. Safe to call at any time; never raises.

    Returns a report dict plus ``skipped_no_credentials`` when Discord
    credentials are absent (the queue is left untouched in that case).
    """
    report = _empty_report()
    if not SE.configured_application_id() or not os.getenv("DISCORD_TOKEN", "").strip():
        report["skipped_no_credentials"] = True
        return report

    waiting = _rate_limit_remaining()
    if waiting > 0:
        # Discord asked this process to wait; do not authenticate, list or fetch.
        report["rate_limited"] = 1
        report["rate_limited_for"] = round(waiting, 3)
        report["skipped_rate_limited"] = True
        return report

    async with _lock():
        started = time.monotonic()
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.ensure_schema(db)
            report["pruned"] = await Q.prune_orphans(db)

            # Nothing due and nothing ambiguous: do not authenticate, do not
            # list, do not fetch. An idle bot must cost Discord zero requests.
            pending = await Q.due(db, guild_id=guild_id, limit=1)
            ambiguous = await _ambiguous_count(db)
            if not pending and not ambiguous:
                report["skipped_idle"] = True
                return report

            async with SE.DiscordAPI() as discord:
                async with SE._preparation_lock(discord.application_id):
                    # Credentials/ownership are verified before any work, exactly
                    # as the explicit path does.
                    try:
                        await discord.verify_application()
                        bot_user_id = await discord.bot_user_id()
                        if reconcile_uncertain and ambiguous:
                            report["reconciled"] += await _reconcile_uncertain(
                                db, discord, bot_user_id)
                    except (SE.DiscordAPIError, SE.ShopEmojiError) as exc:
                        # A 429/5xx here is a transient "Discord is unavailable",
                        # not a product failure: leave every row untouched and
                        # record it so the next tick retries after the interval.
                        if isinstance(exc, SE.DiscordRateLimited):
                            _note_rate_limit(exc.retry_after, report)
                        report["discord_unavailable"] = str(exc)
                        return report

                    if not allow_create:
                        return report
                    rows = await Q.due(db, guild_id=guild_id, limit=limit)
                    if not rows:
                        return report
                    # One snapshot for the whole pass; later creations/adoptions
                    # are folded back in so intra-pass reuse is correct.
                    try:
                        remote = await discord.list_emojis()
                    except SE.DiscordAPIError as exc:
                        # Same rule as above: back off, do not consume attempts.
                        if isinstance(exc, SE.DiscordRateLimited):
                            _note_rate_limit(exc.retry_after, report)
                        report["discord_unavailable"] = str(exc)
                        return report
                    for row in rows:
                        if time.monotonic() - started >= time_budget:
                            report["budget_exhausted"] = True
                            break
                        report["processed"] += 1
                        try:
                            outcome = await _process_row(db, discord, row, remote,
                                                         bot_user_id, report)
                            if outcome == "rate_limited":
                                break       # Discord said wait: send nothing more
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - a pass must survive
                            log.warning("shop emoji worker: row %s/%s raised: %r",
                                        row.get("guild_id"), row.get("product_id"), exc)
                            await Q.mark_failed(db, row,
                                                f"{exc.__class__.__name__}: {exc}",
                                                retryable=True, count_attempt=True)
                            report["failed"] += 1
    return report


async def _reconcile_uncertain(db, discord, bot_user_id, limit=SE.STARTUP_RECONCILE_LIMIT):
    """Read-only recovery sweep over ambiguous assets.

    Reuses ``_reconcile_asset`` unchanged, so absence from one listing is never
    treated as proof of a failed create, and an adopted emoji must pass the same
    ownership AND content-identity checks. Returns the number of assets examined.
    """
    cur = await db.execute(
        f"SELECT {SE._ASSET_SELECT} FROM shop_emoji_assets WHERE application_id=? "
        "AND status IN ('uncertain','preparing') ORDER BY updated_at ASC LIMIT ?",
        (discord.application_id, limit))
    rows = await cur.fetchall()
    if not rows:
        return 0
    remote = await discord.list_emojis()
    for row in rows:
        asset = SE._asset(row)
        try:
            await SE._reconcile_asset(db, asset, remote, bot_user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("shop emoji worker: reconcile %s raised: %r",
                        asset.get("content_hash"), exc)
    return len(rows)


# ── Legacy rename：complete, bounded, restart-safe traversal ────────────────
RENAME_PAGE_SIZE = Q.MAX_BATCH_LIMIT


async def _rename_candidates(db, application_id, *, guild_id=None, after=None,
                             limit=RENAME_PAGE_SIZE):
    """One page of ready assets that still need the readable name.

    Keyset pagination on ``content_hash`` (a stable primary-key component),
    NOT on ``updated_at``: a successful rename updates ``updated_at``, so an
    updated_at-ordered LIMIT would keep re-selecting the same newest rows and
    never reach the older legacy assets.

    Filtering happens in SQL against the canonical-name prefix, not in Python
    after a LIMIT, so a page that is already correct costs no Discord calls.
    """
    sql = (f"SELECT {SE._ASSET_SELECT_E} FROM shop_emoji_assets e "
           "WHERE e.application_id=? AND e.status='ready' "
           "AND " + SE.SQL_VALID_EMOJI_ID.format(col="e.emoji_id"))
    params = [application_id]
    if guild_id is not None:
        sql += " AND e.creator_guild_id=?"
        params.append(guild_id)
    if after is not None:
        sql += " AND e.content_hash > ?"
        params.append(after)
    sql += " ORDER BY e.content_hash ASC LIMIT ?"
    params.append(int(limit))
    cur = await db.execute(sql, tuple(params))
    rows = await cur.fetchall()
    n = len(SE._ASSET_FIELDS)
    out = []
    for row in rows:
        asset = SE._asset(row[:n])
        if not asset:
            continue
        label = await SE.canonical_label_for_asset(db, application_id,
                                                   asset["content_hash"])
        target = SE.rename_target_name(asset, label)
        if target:
            out.append((asset, target))
    return out


async def rename_all(*, guild_id=None, apply=False, limit=None,
                     page_size=RENAME_PAGE_SIZE, max_pages=200):
    """Preview or apply readable names for the WHOLE existing catalog.

    Walks every ready asset in content_hash order, page by page, so:
    * more than 25 legacy emojis are all reachable,
    * an interruption resumes from the assets still needing work (they are
      still the ones whose name differs),
    * a second complete run issues zero PATCHes,
    * emoji IDs, aliases and content hashes are never touched.
    """
    report = {"preview": [], "renamed": 0, "pages": 0, "examined": 0,
              "patch_requests": 0, "skipped_no_credentials": False, "results": []}
    app_id = SE.configured_application_id()
    if not app_id:
        report["skipped_no_credentials"] = True
        return report
    async with aiosqlite.connect(DB_PATH) as db:
        await Q.ensure_schema(db)
        after = None
        while report["pages"] < int(max_pages):
            page = await _rename_candidates(db, app_id, guild_id=guild_id,
                                            after=after, limit=page_size)
            # Advance the cursor on the RAW page, not only on renamed rows:
            # already-correct assets must still move the cursor forward.
            # The cursor must use EXACTLY the same scope as _rename_candidates().
            # Without the guild filter, another guild's interleaved hashes would
            # advance this cursor past the target guild's own assets and skip
            # them permanently.
            cursor_sql = (
                "SELECT MAX(content_hash) FROM ("
                "  SELECT content_hash FROM shop_emoji_assets WHERE application_id=? "
                "  AND status='ready' AND " + SE.SQL_VALID_EMOJI_ID.format(col="emoji_id") + " ")
            cursor_params = [app_id]
            if guild_id is not None:
                cursor_sql += "  AND creator_guild_id=? "
                cursor_params.append(guild_id)
            cursor_sql += (
                "  AND content_hash > ? ORDER BY content_hash ASC LIMIT ?)")
            cursor_params += [after or "", int(page_size)]
            cur = await db.execute(cursor_sql, tuple(cursor_params))
            row = await cur.fetchone()
            if not row or row[0] is None:
                break
            after = row[0]
            report["pages"] += 1
            report["examined"] += 1
            if not page:
                continue
            if not apply:
                report["preview"].extend(
                    {"content_hash": a["content_hash"], "emoji_id": a["emoji_id"],
                     "current_name": a["emoji_name"], "proposed_name": t}
                    for a, t in page)
                if limit and len(report["preview"]) >= limit:
                    break
                continue
            if not os.getenv("DISCORD_TOKEN", "").strip():
                report["skipped_no_credentials"] = True
                return report
            async with SE.DiscordAPI() as discord:
                async with SE._preparation_lock(discord.application_id):
                    await discord.verify_application()
                    bot_user_id = await discord.bot_user_id()
                    remote = await discord.list_emojis()
                    for asset, target in page:
                        bound = next((e for e in remote
                                      if str(e.get("id")) == str(asset["emoji_id"])), None)
                        if bound is None or not SE._ownership_supported(
                                bound, discord.application_id, bot_user_id):
                            # Never rename what Discord no longer confirms is ours.
                            continue
                        report["patch_requests"] += 1
                        updated = await _rename_or_defer(db, discord, asset, target, report)
                        if updated is None:  # pragma: no cover - defensive
                            continue
            if limit and report["renamed"] >= limit:
                break
    return report


# Backwards-compatible name used by the dashboard endpoint.
async def rename_pass(*, guild_id=None, apply=False, limit=None):
    result = await rename_all(guild_id=guild_id, apply=apply, limit=limit)
    return {"preview": result["preview"], "renamed": result["renamed"],
            "patch_requests": result["patch_requests"],
            "pages": result["pages"],
            "skipped_no_credentials": result.get("skipped_no_credentials", False),
            "results": result.get("results", [])}


async def process_product_now(db, discord, guild_id, product_id, *, bot_user_id=None):
    """Test/support hook: advance one product through the same code path."""
    row = await Q.descriptor(db, guild_id, product_id)
    if row is None:
        return "not_queued"
    report = _empty_report()
    remote = await discord.list_emojis()
    if bot_user_id is None:
        bot_user_id = await discord.bot_user_id()
    status = await _process_row(db, discord, row, remote, bot_user_id, report)
    return status, report
