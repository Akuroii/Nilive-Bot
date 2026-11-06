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
* **Ambiguity is never retried blindly.** ``_prepare_one`` already treats an
  unconfirmed POST as ``uncertain`` and leaves it quarantined; the worker
  reacts by reconciling against Discord (read-only) and deferring the row, never
  by issuing a second create.
* **Reconcile before create.** Every pass lists Discord's application emojis
  first and lets ``_prepare_one``/``_create_claimed`` reconcile against that
  snapshot before any POST.
* **No deletion, ever.** The worker has no code path that calls
  ``delete_emoji``. Duplicates are surfaced for human review only.
* **Read-only consumers stay read-only.** Inventory and Rank import neither
  this module nor the queue.

Renaming
--------
A ready asset may be renamed in place (PATCH, same ID) to the readable policy
name. ``managed_name`` is set *before* the PATCH so a crash between request and
bookkeeping reconciles from Discord instead of declaring a metadata conflict.
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

# Serialises the whole worker for this process. Cross-process safety comes from
# the DB fences in _prepare_one/_claim_existing, not from this lock.
_pass_lock = None


def _lock():
    global _pass_lock
    if _pass_lock is None:
        _pass_lock = asyncio.Lock()
    return _pass_lock


class PassReport(dict):
    """Plain dict subclass so callers/tests can read fields without a class."""


def _empty_report():
    return PassReport(processed=0, created=0, reused=0, reconciled=0, renamed=0,
                      deferred=0, failed=0, skipped=0, pruned=0, budget_exhausted=False,
                      results=[])


async def _load_listing(db, guild_id, product_id):
    cur = await db.execute(
        "SELECT id, guild_id, name, icon_url, COALESCE(enabled,1) FROM shop_items "
        "WHERE id=? AND guild_id=?", (product_id, guild_id))
    return await cur.fetchone()


async def _alias_asset(db, application_id, guild_id, source_url):
    """Ready asset for this URL in this guild, if any (read-only)."""
    cur = await db.execute(
        f"SELECT {SE._ASSET_SELECT_E} FROM shop_emoji_url_aliases a "
        "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
        "AND e.content_hash=a.content_hash "
        "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=? "
        "AND e.status='ready'",
        (application_id, guild_id, source_url))
    return SE._asset(await cur.fetchone())


async def _maybe_rename(db, discord, asset, item_name, report, bot_user_id):
    """Rename a ready, bound asset to the readable policy name (PATCH, same ID).

    Returns the (possibly updated) asset. Never creates, never deletes, and
    never renames while the bound emoji is absent or lost from Discord.
    """
    target = SE.rename_target_name(asset, item_name)
    if not target:
        return asset
    # Only touch what Discord just confirmed is present and ours.
    created = await _rename_or_defer(db, discord, asset, target, report)
    return created


async def _rename_or_defer(db, discord, asset, target, report):
    # Mark management FIRST: if the PATCH succeeds and we crash before the name
    # is written, the next _refresh_ready_asset must not call this a conflict.
    await db.execute(
        "UPDATE shop_emoji_assets SET managed_name=1, updated_at=? "
        "WHERE application_id=? AND content_hash=?",
        (SE._iso(), asset["application_id"], asset["content_hash"]))
    await db.commit()
    try:
        await discord.rename_emoji(asset["emoji_id"], target)
    except SE.DiscordAPIError as exc:
        if exc.uncertain:
            # The rename may or may not have applied. Identity is unchanged, so
            # this is cosmetic and safe to leave: the next refresh accepts either
            # name for a managed asset.
            report["results"].append({"code": "rename_uncertain", "message": str(exc)})
            return await SE._get_asset(db, asset["application_id"], asset["content_hash"])
        # A definitive rejection (e.g. invalid name) keeps the old name; the
        # asset stays ready and resolvable, which is what matters.
        report["results"].append({"code": "rename_rejected", "message": str(exc)})
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
    return await SE._get_asset(db, asset["application_id"], asset["content_hash"])


async def _process_row(db, discord, row, remote, bot_user_id, report, *, allow_create):
    """Advance one queue row by exactly one step. Returns the report code."""
    guild_id, product_id = row["guild_id"], row["product_id"]
    listing = await _load_listing(db, guild_id, product_id)
    if listing is None:
        await Q.mark_done(db, row)      # listing deleted: intent is void
        report["skipped"] += 1
        return "listing_gone"
    _id, _gid, item_name, icon_url, enabled = listing
    if not enabled:
        await Q.clear_product(db, guild_id, product_id)
        report["skipped"] += 1
        return "listing_disabled"

    current_url = Q.queue_source_url(icon_url)
    if current_url is None:
        # The admin cleared/replaced the image with something unusable.
        await Q.clear_product(db, guild_id, product_id)
        report["skipped"] += 1
        return "no_usable_url"
    if current_url != row["source_url"]:
        # The listing changed after this row was written; re-queue for the new
        # URL instead of preparing an image the storefront no longer uses.
        await Q.enqueue_product(db, guild_id, product_id, item_name, current_url,
                                reason="icon_url_changed")
        report["skipped"] += 1
        return "requeued_new_url"

    # ── already prepared? (reuse) ────────────────────────────────────────
    ready = await _alias_asset(db, discord.application_id, guild_id, current_url)
    if ready is not None:
        await _maybe_rename(db, discord, ready, item_name, report, bot_user_id)
        await Q.mark_done(db, row)
        report["reused"] += 1
        return "reused"

    if not allow_create:
        report["deferred"] += 1
        return "budget"

    await Q.record_attempt(db, row)
    try:
        # The single existing entry point: hashing, aliasing, reconciliation,
        # create-fencing, quota and duplicate protection all live inside it.
        # A brand-new emoji is created with the readable name directly; a
        # reused one is renamed in place further down, so no new emoji ever
        # starts life as the opaque hash name.
        result, remote = await SE._prepare_one(
            db, discord, guild_id, current_url, remote, bot_user_id,
            False, False,
            emoji_name=lambda h: SE.readable_emoji_name(item_name, h))
    except SE.SourceImageError as exc:
        # Dead / non-image / oversized / timeout: retryable, the image may come back.
        await Q.mark_failed(db, row, f"{exc.code}: {exc}", retryable=True)
        report["failed"] += 1
        report["results"].append({"product_id": product_id, "code": exc.code,
                                  "message": str(exc)})
        return "source_error"
    except SE.ShopEmojiError as exc:
        retryable = exc.code in {"guild_emoji_quota", "asset_creator_mismatch",
                                 "asset_attribution_required"}
        await Q.mark_failed(db, row, f"{exc.code}: {exc}", retryable=retryable)
        report["failed"] += 1
        report["results"].append({"product_id": product_id, "code": exc.code,
                                  "message": str(exc)})
        return "shop_error"
    except SE.DiscordAPIError as exc:
        # Transport/5xx/429 — the asset fence already records any uncertainty.
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
            await _maybe_rename(db, discord, asset, item_name, report, bot_user_id)
        await Q.mark_done(db, row)
        if code in {"created", "remapped_changed_source"}:
            report["created"] += 1
        else:
            report["reused"] += 1
        report["results"].append({"product_id": product_id, "code": code,
                                  "status": status, "emoji_id": result.get("emoji_id")})
        return "ready"

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


async def _asset_by_hash(db, application_id, content_hash):
    if not content_hash:
        return None
    return await SE._get_asset(db, application_id, content_hash)


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
                    await discord.verify_application()
                    bot_user_id = await discord.bot_user_id()

                    if reconcile_uncertain:
                        report["reconcile"] = await _reconcile_uncertain(
                            db, discord, bot_user_id)

                    if not allow_create:
                        return report
                    rows = await Q.due(db, guild_id=guild_id, limit=limit)
                    if not rows:
                        return report
                    # One snapshot for the whole pass; later creations/adoptions
                    # are folded back in so intra-pass reuse is correct.
                    remote = await discord.list_emojis()
                    for row in rows:
                        if time.monotonic() - started >= time_budget:
                            report["budget_exhausted"] = True
                            break
                        report["processed"] += 1
                        try:
                            await _process_row(db, discord, row, remote,
                                               bot_user_id, report,
                                               allow_create=True)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - a pass must survive
                            log.warning("shop emoji worker: row %s/%s raised: %r",
                                        row.get("guild_id"), row.get("product_id"), exc)
                            await Q.mark_failed(db, row, f"{exc.__class__.__name__}: {exc}",
                                                retryable=True)
                            report["failed"] += 1
    return report


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


async def _reconcile_uncertain(db, discord, bot_user_id, limit=SE.STARTUP_RECONCILE_LIMIT):
    """Read-only recovery sweep over ambiguous assets.

    Reuses ``_reconcile_asset`` unchanged, so absence from one listing is never
    treated as proof of a failed create, and an adopted emoji must pass the same
    ownership check. Returns the number of assets examined.
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


async def process_product_now(db, discord, guild_id, product_id, *,
                              item_name=None, bot_user_id=None):
    """Test/support hook: advance one product through the same code path.

    Used by the test-suite to drive the worker's per-row logic without a bot
    connection or a scheduler tick.
    """
    row = await Q.descriptor(db, guild_id, product_id)
    if row is None:
        return "not_queued"
    report = _empty_report()
    remote = await discord.list_emojis()
    if bot_user_id is None:
        bot_user_id = await discord.bot_user_id()
    return await _process_row(db, discord, row, remote, bot_user_id, report,
                              allow_create=True), report


async def rename_pass(*, guild_id=None, apply=False, limit=Q.MAX_BATCH_LIMIT):
    """Preview (or apply) readable names for this guild's ready assets.

    Renaming is purely cosmetic: the emoji ID, the bound ID, the content hash
    and every alias row stay exactly as they are, which is why this can be run
    later, repeatedly, and independently of preparation.

    Only ``ready`` + bound assets owned by this guild are considered, and a
    Discord 404/conflict is reported rather than repaired.
    """
    if not SE.configured_application_id() or not os.getenv("DISCORD_TOKEN", "").strip():
        return {"preview": [], "renamed": 0, "skipped_no_credentials": True}

    async with aiosqlite.connect(DB_PATH) as db:
        await Q.ensure_schema(db)
        params = [guild_id] if guild_id is not None else []
        where = " AND e.creator_guild_id=?" if guild_id is not None else ""
        cur = await db.execute(
            f"SELECT {SE._ASSET_SELECT_E}, ("
            "  SELECT s.name FROM shop_emoji_url_aliases a2 "
            "  JOIN shop_items s ON s.guild_id=a2.guild_id "
            "  WHERE a2.application_id=e.application_id "
            "    AND a2.content_hash=e.content_hash AND s.icon_url IS NOT NULL "
            "  ORDER BY a2.updated_at DESC LIMIT 1) "
            "FROM shop_emoji_assets e WHERE e.application_id=? AND e.status='ready' "
            "AND e.emoji_id IS NOT NULL" + where + " ORDER BY e.updated_at DESC LIMIT ?",
            tuple([SE.configured_application_id(), *params, int(limit)]))
        rows = await cur.fetchall()
        n = len(SE._ASSET_FIELDS)
        candidates = []
        for row in rows:
            asset = SE._asset(row[:n])
            item_name = row[n]
            target = SE.rename_target_name(asset, item_name)
            if target:
                candidates.append((asset, item_name, target))

        if not candidates:
            return {"preview": [], "renamed": 0}
        report = _empty_report()
        preview = [{"content_hash": a["content_hash"], "emoji_id": a["emoji_id"],
                    "current_name": a["emoji_name"], "proposed_name": t,
                    "item_name": name}
                   for a, name, t in candidates]
        if not apply:
            return {"preview": preview, "renamed": 0}

        async with SE.DiscordAPI() as discord:
            async with SE._preparation_lock(discord.application_id):
                await discord.verify_application()
                bot_user_id = await discord.bot_user_id()
                remote = await discord.list_emojis()
                for asset, item_name, target in candidates:
                    bound = next((e for e in remote
                                  if str(e.get("id")) == str(asset["emoji_id"])), None)
                    if bound is None or not SE._ownership_supported(
                            bound, discord.application_id, bot_user_id):
                        # Never rename something Discord no longer confirms as ours.
                        continue
                    await _maybe_rename(db, discord, asset, item_name, report, bot_user_id)
        return {"preview": preview, "renamed": report["renamed"],
                "results": report["results"]}
