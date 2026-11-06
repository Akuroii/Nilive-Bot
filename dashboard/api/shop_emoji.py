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
    """Queue every eligible existing product for preparation (one-time bulk).

    Reads ``shop_items`` directly, so no Shop Designer roster entry is needed.
    This endpoint only writes queue rows: the background worker performs the
    uploads in bounded batches, so the request returns immediately and nothing
    here can create 100 emojis inside one HTTP call.
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

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            await queue.ensure_schema(db)
            queued = await queue.enqueue_guild_products(
                get_session_guild_id(),
                reason=str(data.get("reason") or "backfill"),
                only_unprepared=data.get("include_prepared") is not True)
            return {"success": True, "queued": queued, "queue": await queue.stats(db)}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001 - owner-visible, never a 500 stack
        return jsonify({"success": False, "error": str(exc), "code": "backfill_failed"}), 500


@api_bp.route("/shop-publisher/emojis/queue", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_queue_status():
    """Read-only queue report: depth, retries and the next scheduled attempt."""
    from utils import shop_emoji_queue as queue

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            await queue.ensure_schema(db)
            guild_id = get_session_guild_id()
            rows = await queue.due(db, guild_id=guild_id, limit=queue.MAX_BATCH_LIMIT)
            return {"success": True,
                    "queue": await queue.stats(db, guild_id=guild_id),
                    "next_attempt_at": await queue.next_attempt_at(db),
                    "pending_sample": [
                        {"product_id": r["product_id"], "item_name": r["item_name"],
                         "attempts": r["attempts"], "last_error": r["last_error"],
                         "next_attempt_at": r["next_attempt_at"]}
                        for r in rows]}

    try:
        return jsonify(run_async(run()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc), "code": "queue_read_failed"}), 500


@api_bp.route("/shop-publisher/emojis/names", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_emoji_names():
    """Preview or apply readable renames via Discord's Modify Emoji API.

    ``apply`` performs a PATCH per asset — same emoji ID, no recreate, no
    delete, and every alias/hash row is untouched. Only ready, bound assets are
    eligible, so a rename can never disturb an in-flight creation.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "code": "invalid_json"}), 400
    try:
        _reject_url_targets(data)
    except ShopEmojiError as exc:
        return _error_response(exc)

    from utils import shop_emoji as SE
    from utils.shop_emoji_worker import rename_pass

    try:
        result = run_async(rename_pass(guild_id=get_session_guild_id(),
                                       apply=data.get("apply") is True))
        return jsonify({"success": True, **result})
    except ShopEmojiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": str(exc), "code": "rename_failed"}), 500
