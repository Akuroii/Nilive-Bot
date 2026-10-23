"""Owner-only Shop product-image emoji preparation and recovery routes.

Browser input identifies products only. Source URLs always come from the
server's guild-scoped shop_items rows. Preview/publication routes never call
these mutating preparation/deletion operations.
"""

import asyncio

from flask import jsonify, request

from dashboard.api import api_bp
from dashboard.permissions import (
    LEVEL_OWNER, get_session_guild_id, require_api_permission,
)
from dashboard.utils.async_utils import run_async
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
