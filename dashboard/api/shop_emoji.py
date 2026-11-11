"""Owner-only Shop product-image emoji preparation and recovery routes.

Browser input identifies products only. Source URLs always come from the
server's guild-scoped shop_items rows. Preview/publication routes never call
these mutating preparation/deletion operations.
"""

import asyncio
import math

import aiosqlite
from flask import jsonify, request

from dashboard.api import api_bp
from dashboard.permissions import (
    LEVEL_OWNER, get_session_guild_id, require_api_permission,
)
from dashboard.utils.async_utils import run_async
from database import DB_PATH
from utils.shop_emoji import (
    GUILD_ASSET_LIMIT,
    INVALID_READY_PREFIX,
    SQL_VALID_EMOJI_ID,
    DiscordRateLimited,
    ShopEmojiError,
    _valid_emoji_id,
    delete_confirmed_duplicate,
    prepare_product_emojis,
    reconcile_product_emojis,
)


def _error_response(exc):
    payload = {"success": False, "error": str(exc), "code": exc.code}
    if exc.details:
        payload["details"] = exc.details
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        payload["retry_after"] = retry_after
    response = jsonify(payload)
    if isinstance(exc, DiscordRateLimited) and retry_after is not None:
        # Whole seconds, rounded up: the client must never retry earlier than Discord said.
        response.headers["Retry-After"] = str(max(1, math.ceil(float(retry_after))))
    return response, exc.status


def _reject_url_targets(data):
    if any(key in data for key in ("url", "source_url", "icon_url", "urls")):
        raise ShopEmojiError(
            "Preparation accepts product IDs only; source URLs are read from the server's Shop catalog.",
            code="product_ids_only", status=400)


@api_bp.route("/shop-publisher/emojis/prepare", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_prepare():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    try:
        _reject_url_targets(data)
        result = run_async(prepare_product_emojis(
            get_session_guild_id(), data.get("product_ids"),
            retry_uncertain=data.get("retry_uncertain") is True,
            acknowledge_duplicate_risk=data.get("acknowledge_duplicate_risk") is True))
        return jsonify(result)
    except ShopEmojiError as exc:
        return _error_response(exc)
    except asyncio.TimeoutError:
        return jsonify({"success": False, "error": "Preparation exceeded its deadline; reconcile before any retry.",
                        "code": "preparation_deadline"}), 504


@api_bp.route("/shop-publisher/emojis/reconcile", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_reconcile():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    try:
        _reject_url_targets(data)
        result = run_async(reconcile_product_emojis(
            get_session_guild_id(), data.get("product_ids")))
        return jsonify(result)
    except ShopEmojiError as exc:
        return _error_response(exc)
    except asyncio.TimeoutError:
        return jsonify({"success": False, "error": "Reconciliation exceeded its deadline; retry after checking Discord.",
                        "code": "reconciliation_deadline"}), 504


@api_bp.route("/shop-publisher/emojis/delete-duplicate", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_delete_duplicate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    try:
        _reject_url_targets(data)
        emoji_id = str(data.get("emoji_id") or "")
        if data.get("confirmation") != f"DELETE {emoji_id}":
            raise ShopEmojiError("Type the exact confirmation DELETE <emoji_id>.",
                                 code="delete_confirmation_required", status=409)
        result = run_async(delete_confirmed_duplicate(
            get_session_guild_id(), data.get("content_hash"), emoji_id, confirm=True))
        return jsonify(result)
    except ShopEmojiError as exc:
        return _error_response(exc)


@api_bp.route("/shop-publisher/emojis/backfill", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_backfill():
    """Authorise and queue this guild's existing catalog (one bulk action).

    Two distinct effects, both scoped to the caller's OWN guild:

    * an authorisation row, which is what the bot's periodic discovery reads —
      without it, deploying the build cannot start creating emojis for other
      servers;
    * queue rows for every eligible product, read straight from ``shop_items``,
      so no Shop Designer roster entry and no per-product selection is needed.

    This returns immediately: the background worker performs the uploads in
    bounded batches, so nothing here can create 100 emojis inside one request.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    try:
        _reject_url_targets(data)
    except ShopEmojiError as exc:
        return _error_response(exc)

    from utils import shop_emoji_queue as queue

    guild_id = get_session_guild_id()

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            await queue.authorize_backfill(db, guild_id,
                                          actor=data.get("actor") or "dashboard-owner")
            result = await queue.enqueue_guild_products(
                db, guild_id,
                reason=str(data.get("reason") or "backfill"),
                only_unprepared=data.get("include_prepared") is not True,
                reactivate_parked=data.get("retry_parked") is True)
            report = await queue.stats(db, guild_id=guild_id)
            report["authorized"] = await queue.is_authorized(db, guild_id)
            # `queued` is NEW intent; `reactivated` is parked rows the owner's
            # retry_parked switch genuinely revived. Reporting them separately is
            # what makes the numbers checkable against the rows.
            return {"success": True, "queued": result["queued"],
                    "reactivated": result["reactivated"], "queue": report}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001 - owner-visible, never a 500 stack
        return jsonify({"success": False, "error": str(exc), "code": "backfill_failed"}), 500


@api_bp.route("/shop-publisher/emojis/queue", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_queue_status():
    """READ-ONLY queue report for this guild.

    Deliberately performs no DDL: schema creation belongs to application
    initialisation (``init_db``), not to a GET. If the queue table does not
    exist yet, the report says so instead of creating it as a side effect.
    """
    from utils import shop_emoji_queue as queue

    guild_id = get_session_guild_id()

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            if not await _table_exists(db, "shop_emoji_queue"):
                return {"success": True, "schema_ready": False,
                        "queue": {"total": 0, "parked": 0, "deferred": 0,
                                  "errored": 0, "max_attempts": 0},
                        "authorized": False, "next_attempt_at": None,
                        "pending_sample": [], "parked": []}
            rows = await queue.due(db, guild_id=guild_id, limit=queue.MAX_BATCH_LIMIT)
            return {"success": True, "schema_ready": True,
                    "queue": await queue.stats(db, guild_id=guild_id),
                    "authorized": await queue.is_authorized(db, guild_id),
                    # Guild-scoped, matching every other number in this payload.
                    "next_attempt_at": await queue.next_attempt_at(db, guild_id=guild_id),
                    "pending_sample": [
                        {"product_id": r["product_id"], "item_name": r["item_name"],
                         "attempts": r["attempts"], "last_error": r["last_error"],
                         "next_attempt_at": r["next_attempt_at"]}
                        for r in rows],
                    "parked": await queue.parked_rows(db, guild_id=guild_id)}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc),
                        "code": "queue_read_failed"}), 500


@api_bp.route("/shop-publisher/emojis/retry", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_retry():
    """Explicit owner retry: reactivate parked products with a fresh budget."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    product_ids = data.get("product_ids")
    if not isinstance(product_ids, list) or not product_ids:
        return jsonify({"success": False, "error": "Send a list of product IDs.",
                        "code": "invalid_product_ids"}), 400

    from utils import shop_emoji_queue as queue

    guild_id = get_session_guild_id()

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            await queue.ensure_schema(db)
            reactivated = 0
            for pid in product_ids:
                if not isinstance(pid, int) or isinstance(pid, bool):
                    continue
                current = await queue.descriptor(db, guild_id, pid)
                if current is not None:
                    # Only a parked row is reactivated. A queued row is already
                    # pending (and may be mid-flight) and a retry row is in a
                    # deliberate backoff; neither is this endpoint's contract.
                    # A force retry of an active row is a separate, explicit
                    # decision and is not offered implicitly here.
                    if await queue.reactivate(
                            db, guild_id, pid,
                            expect_generation=current["generation"],
                            expect_source_url=current["source_url"]):
                        reactivated += 1
                    # Deliberately no `else`: an existing non-parked row is a
                    # no-op, not a reason to re-queue and reset it.
                else:
                    # Never queued (or already finished): queue it now.
                    cur = await db.execute(
                        "SELECT name, icon_url FROM shop_items WHERE id=? AND guild_id=?",
                        (pid, guild_id))
                    row = await cur.fetchone()
                    # Fenced to what was just read: no queue row was seen and the
                    # listing held row[1]. A Dashboard save (or a queue row) that
                    # lands before this write is newer intent and is left alone.
                    if row and await queue.enqueue_product(
                            db, guild_id, pid, row[0], row[1],
                            reason="owner_retry", reset_attempts=True,
                            expect_icon_url=row[1], expect_generation=None) is not None:
                        reactivated += 1
            return {"success": True, "reactivated": reactivated,
                    "queue": await queue.stats(db, guild_id=guild_id)}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc),
                        "code": "retry_failed"}), 500


@api_bp.route("/shop-publisher/emojis/names", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_names():
    """Preview or apply readable renames across the WHOLE existing catalog.

    ``apply`` performs one PATCH per asset — same emoji ID, no recreate, no
    delete, and every alias/hash row is untouched. Pagination is keyset-based
    over ``content_hash``, so more than one page of legacy emojis is reachable
    and a second complete run performs zero PATCHes.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    try:
        _reject_url_targets(data)
    except ShopEmojiError as exc:
        return _error_response(exc)

    from utils.shop_emoji_worker import rename_all

    try:
        result = run_async(rename_all(guild_id=get_session_guild_id(),
                                      apply=data.get("apply") is True))
        return jsonify({"success": True, **result})
    except ShopEmojiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc), "code": "rename_failed"}), 500


async def _table_exists(db, name):
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return await cur.fetchone() is not None


@api_bp.route("/shop-publisher/emojis/report", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_report():
    """Operational report: what a backfill would do, and what is stuck.

    Read-only. Built from ``queue.classify_catalog()`` — the SAME partition
    Backfill acts on — so the numbers here can never disagree with what the
    worker will actually queue. In particular an asset whose ``emoji_id`` is not
    a positive numeric value is NOT "already prepared", because Inventory and
    Rank cannot render it.

    ``eligible = already_ready + unprepared_products + invalid_source_products``
    always holds, so "fully prepared" cannot be claimed while some eligible
    product was excluded from the accounting.
    """
    from utils import shop_emoji_queue as queue
    from utils.shop_emoji import GUILD_ASSET_LIMIT, configured_application_id
    from utils.shop_emoji_queue import queue_source_url

    guild_id = get_session_guild_id()
    app_id = configured_application_id()

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            catalog = await queue.classify_catalog(db, guild_id, app_id)

            # Reuse through shared CONTENT is the real dedupe axis (several
            # products, identical bytes, one emoji) — distinct from shared URLs.
            content_users = await (await db.execute(
                "SELECT a.content_hash, COUNT(DISTINCT a.source_url) "
                "FROM shop_emoji_url_aliases a "
                "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
                "  AND e.content_hash=a.content_hash "
                "WHERE a.application_id=? AND a.guild_id=? AND e.status='ready' "
                "AND " + SQL_VALID_EMOJI_ID.format(col="e.emoji_id") + " "
                "GROUP BY a.content_hash HAVING COUNT(DISTINCT a.source_url) > 1",
                (app_id, guild_id))).fetchall()

            uncertain_n = (await (await db.execute(
                "SELECT COUNT(*) FROM shop_emoji_assets WHERE application_id=? "
                "AND status IN ('uncertain','preparing')", (app_id,))).fetchone())[0]
            failed_n = (await (await db.execute(
                "SELECT COUNT(*) FROM shop_emoji_assets WHERE application_id=? "
                "AND status IN ('failed','missing')", (app_id,))).fetchone())[0]
            used = (await (await db.execute(
                "SELECT COUNT(*) FROM shop_emoji_assets WHERE application_id=? "
                "AND status IN ('ready','preparing','uncertain') AND creator_guild_id=?",
                (app_id, guild_id))).fetchone())[0]

            # Assets this guild's aliases point at whose "ready" state cannot be
            # trusted (unusable bound ID), plus the ones already withdrawn into
            # `uncertain` for that reason. Surfaced, never silently consumed.
            invalid_ready = []
            for h, status, emoji_id, last_error in await (await db.execute(
                    "SELECT DISTINCT e.content_hash,e.status,e.emoji_id,e.last_error "
                    "FROM shop_emoji_url_aliases a JOIN shop_emoji_assets e "
                    "ON e.application_id=a.application_id AND e.content_hash=a.content_hash "
                    "WHERE a.application_id=? AND a.guild_id=? AND "
                    "((e.status='ready' AND NOT " + SQL_VALID_EMOJI_ID.format(col="e.emoji_id") + ") "
                    "OR (e.status='uncertain' AND e.last_error LIKE ?))",
                    (app_id, guild_id, INVALID_READY_PREFIX + "%"))).fetchall():
                if status == "ready" and _valid_emoji_id(emoji_id):
                    continue
                invalid_ready.append({
                    "content_hash": h, "status": status,
                    "reason": last_error or "The ready asset's bound emoji ID is unusable.",
                    "action": ("Reconcile application emojis. If it cannot be matched safely, "
                               "an explicit, acknowledged recovery is required; no replacement "
                               "is created automatically.")})

            url_counts = {}
            for entry in catalog["prepared"] + catalog["unprepared"]:
                url_counts[entry["source_url"]] = url_counts.get(
                    entry["source_url"], 0) + 1

            stats = (await queue.stats(db, guild_id=guild_id)) \
                if await _table_exists(db, "shop_emoji_queue") else {}
            parked = (await queue.parked_rows(db, guild_id=guild_id)
                      if await _table_exists(db, "shop_emoji_queue") else [])

            invalid = catalog["unsupported"]
            return {"success": True,
                    "eligible_products": catalog["eligible"],
                    "already_ready": len(catalog["prepared"]),
                    "unprepared_products": len(catalog["unprepared"]),
                    "invalid_source_products": invalid,
                    "invalid_source_count": len(invalid),
                    # Only truthful when nothing is outstanding AND nothing was
                    # excluded from the ready/unprepared accounting.
                    "fully_prepared": (not catalog["unprepared"] and not invalid),
                    "distinct_urls": len(url_counts),
                    "assets_to_attempt_max": len(catalog["unprepared"]),
                    "shared_url_groups": sorted(c for c in url_counts.values() if c > 1),
                    "content_reused_groups": [{"content_hash": h, "products": n}
                                              for h, n in content_users],
                    "invalid_ready_assets": invalid_ready,
                    "quota": {"used": used, "limit": GUILD_ASSET_LIMIT},
                    "quota_limited": used >= GUILD_ASSET_LIMIT,
                    "uncertain": uncertain_n,
                    "failed_or_missing": failed_n,
                    "queue": stats,
                    # `stats()["deferred"]` is every row in a future backoff,
                    # NOT only uncertain Discord creations — source failures and
                    # retries are counted there too. Kept separate from
                    # `uncertain` so the UI never mislabels them.
                    "parked": parked}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc),
                        "code": "report_failed"}), 500
