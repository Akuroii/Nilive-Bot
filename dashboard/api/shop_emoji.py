"""Owner-only Shop product-image emoji preparation and recovery routes.

Browser input identifies products only. Source URLs always come from the
server's guild-scoped shop_items rows. Preview/publication routes never call
these mutating preparation/deletion operations.
"""

import asyncio

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
    ShopEmojiError,
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
    return jsonify(payload), exc.status


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
            queued = await queue.enqueue_guild_products(
                db, guild_id,
                reason=str(data.get("reason") or "backfill"),
                only_unprepared=data.get("include_prepared") is not True,
                reactivate_parked=data.get("retry_parked") is True)
            report = await queue.stats(db, guild_id=guild_id)
            report["authorized"] = await queue.is_authorized(db, guild_id)
            return {"success": True, "queued": queued, "queue": report}

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
                if await queue.descriptor(db, guild_id, pid):
                    await queue.reactivate(db, guild_id, pid)
                    reactivated += 1
                else:
                    # Never queued (or already finished): queue it now.
                    cur = await db.execute(
                        "SELECT name, icon_url FROM shop_items WHERE id=? AND guild_id=?",
                        (pid, guild_id))
                    row = await cur.fetchone()
                    if row and await queue.enqueue_product(
                            db, guild_id, pid, row[0], row[1],
                            reason="owner_retry", reset_attempts=True) is not None:
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

    Read-only. Separates eligible products, already-ready products, the assets a
    backfill would still have to attempt, reuse through SHARED CONTENT (the real
    dedupe axis: several products, identical image bytes, one emoji), quota
    usage, permanently failed products, uncertain assets and rows waiting on a
    retry.
    """
    from utils import shop_emoji_queue as queue
    from utils.shop_emoji import GUILD_ASSET_LIMIT, configured_application_id
    from utils.shop_emoji_queue import queue_source_url

    guild_id = get_session_guild_id()
    app_id = configured_application_id()

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute(
                "SELECT id,name,icon_url FROM shop_items WHERE guild_id=? "
                "AND icon_url IS NOT NULL AND TRIM(icon_url)<>'' "
                "AND COALESCE(enabled,1)=1", (guild_id,))).fetchall()
            eligible = len(rows)
            url_owner = {}       # canonical url -> product count
            ready_urls, unprepared_urls = set(), set()
            for _pid, _name, raw in rows:
                canon = queue_source_url(raw)
                if not canon:
                    continue
                url_owner[canon] = url_owner.get(canon, 0) + 1
                found = await (await db.execute(
                    "SELECT 1 FROM shop_emoji_url_aliases a "
                    "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
                    "  AND e.content_hash=a.content_hash "
                    "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=? "
                    "AND e.status='ready' LIMIT 1", (app_id, guild_id, canon))).fetchone()
                (ready_urls if found else unprepared_urls).add(canon)

            # Content reuse, measured on what is already prepared: products
            # whose current image resolves to the same content hash.
            content_users = await (await db.execute(
                "SELECT a.content_hash, COUNT(DISTINCT a.source_url) "
                "FROM shop_emoji_url_aliases a "
                "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
                "  AND e.content_hash=a.content_hash "
                "WHERE a.application_id=? AND a.guild_id=? AND e.status='ready' "
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

            stats = (await queue.stats(db, guild_id=guild_id)) \
                if await _table_exists(db, "shop_emoji_queue") else {}
            return {"success": True,
                    "eligible_products": eligible,
                    "already_ready": sum(url_owner[u] for u in ready_urls),
                    "unprepared_products": sum(url_owner[u] for u in unprepared_urls),
                    "distinct_urls": len(url_owner),
                    # An attempt per unprepared URL; identical bytes will still
                    # collapse to one asset once hashed, which is why this is a
                    # ceiling and not a promise.
                    "assets_to_attempt_max": len(unprepared_urls),
                    "shared_url_groups": sorted(
                        c for c in url_owner.values() if c > 1),
                    "content_reused_groups": [{"content_hash": h, "products": n}
                                              for h, n in content_users],
                    "quota": {"used": used, "limit": GUILD_ASSET_LIMIT},
                    "quota_limited": used >= GUILD_ASSET_LIMIT,
                    "uncertain": uncertain_n,
                    "failed_or_missing": failed_n,
                    "queue": stats,
                    "parked": (await queue.parked_rows(db, guild_id=guild_id)
                               if await _table_exists(db, "shop_emoji_queue") else [])}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc),
                        "code": "report_failed"}), 500
