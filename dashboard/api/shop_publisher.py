"""
Shop Publisher (Phase 1) — read-only catalog + preview routes.

Phase 1 is the front half of the approved plan only: template selection,
product selection, fixed token resolution, Publisher preview, preview warnings.
There is NO publish/send route here (Phase 2), NO shop_publications table
(Phase 2), and nothing in this module writes to the database — the preview is
pure computation over embed_templates + shop_items + the currency config.

Why a separate submodule instead of dashboard/api/economy_shop.py or
dashboard/api/embedbuilder.py: both of those are frozen delivery boundaries of
previous work (the Shop admin and the Embed Builder). Registering on the shared
api_bp from a new file is the same zero-risk pattern every other surface here
uses — existing routes, URLs and response shapes are untouched.

Permission: LEVEL_OWNER, matching the Embed Builder send/template routes. The
Publisher consumes embed_templates (managed at LEVEL_OWNER) and Phase 2 will
send live Discord messages from the same surface, so it must never be a
widening path for either one.

Phase 2 boundary note (for the next pass): the publish route that lands here
must reuse utils/shop_publisher.preview_message() (so preview == publish) and
the purchase_action() descriptor (so the published button IS the previewed
button), and must decide its blocking policy over these same warning codes.
"""

import json

import aiosqlite
from flask import jsonify, request

from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, require_api_permission, LEVEL_OWNER,
)
from dashboard.api import api_bp
import utils.shop_publisher as SP
from utils.currency import get_currency_config

# shop_items columns the picker needs: identity, grouping (type), the price
# line, and the state the preview warns about. The picker groups by `type`
# only (locked decision — no new category system).
_PRODUCT_COLUMNS = (
    "id, name, type, price, price_diamonds, enabled, "
    "current_stock, max_stock, prestige_tier, featured"
)


def _product_dict(row) -> dict:
    keys = ("id", "name", "type", "price", "price_diamonds", "enabled",
            "current_stock", "max_stock", "prestige_tier", "featured")
    return dict(zip(keys, row))


@api_bp.route("/shop-publisher/catalog", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publisher_catalog():
    """Both pickers + the fixed token catalog, one round trip.

    templates: embed_templates names (the presentation layer).
    products:  shop_items rows (the product source of truth), pre-sorted for
               V1 picker grouping — the existing `type` groups first in shop-
               form order, then name. The client renders that order as
               <optgroup>s and invents no category of its own.
    tokens:    utils/shop_publisher.token_catalog_payload() — the FIXED catalog,
               so the UI reference and the resolver cannot drift.
    """
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT name FROM embed_templates WHERE guild_id = ? "
                "ORDER BY name ASC",
                (guild_id,))
            templates = [r[0] for r in await cursor.fetchall()]
            cursor = await db.execute(
                f"SELECT {_PRODUCT_COLUMNS} FROM shop_items "
                "WHERE guild_id = ? ORDER BY id ASC",
                (guild_id,))
            products = [_product_dict(r) for r in await cursor.fetchall()]
        products.sort(key=lambda p: (SP.type_group_key(p["type"] or ""),
                                     (p["name"] or "").lower(), p["id"]))
        return templates, products

    templates, products = run_async(fetch())
    return jsonify({
        "templates": templates,
        "products": products,
        "tokens": SP.token_catalog_payload(),
    })


@api_bp.route("/shop-publisher/preview", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publisher_preview():
    """Resolve one Template + Product pair server-side and return the preview.

    The resolution runs through utils/shop_publisher — the same pure module
    Phase 2's publish path will call — so what this route returns IS the
    message that would go to Discord, plus the purchase action that will
    actually be published and the deterministic preview warnings.

    Read-only by construction: no INSERT/UPDATE/DELETE, no audit-log entry
    (previewing is not an action worth a log row; publishing will be).
    """
    guild_id = get_session_guild_id()
    data = request.json or {}

    name = data.get("template")
    name = name.strip().lower() if isinstance(name, str) else ""
    if not name:
        return jsonify({"success": False,
                        "error": "Pick a template to preview."}), 400

    product_id = data.get("product_id")
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        return jsonify({"success": False,
                        "error": "Pick a product to preview."}), 400

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT data FROM embed_templates WHERE guild_id = ? AND name = ?",
                (guild_id, name))
            template_row = await cursor.fetchone()
            cursor = await db.execute(
                f"SELECT {_PRODUCT_COLUMNS}, description, duration_hours, "
                "required_level, rarity, icon_url "
                "FROM shop_items WHERE guild_id = ? AND id = ?",
                (guild_id, product_id))
            product_row = await cursor.fetchone()
        return template_row, product_row

    template_row, product_row = run_async(fetch())

    if template_row is None:
        return jsonify({"success": False,
                        "error": f"Template '{name}' was not found."}), 404
    if product_row is None:
        return jsonify({"success": False,
                        "error": "That product was not found."}), 404

    try:
        template_doc = json.loads(template_row[0])
    except Exception:
        template_doc = None

    product = _product_dict(product_row[:10])
    for key, value in zip(("description", "duration_hours", "required_level",
                           "rarity", "icon_url"), product_row[10:]):
        product[key] = value

    currency = run_async(get_currency_config(guild_id))
    preview = SP.preview_message(template_doc, product, currency)

    return jsonify({
        "success": True,
        "template": name,
        "product": {"id": product["id"], "name": product["name"],
                    "type": product["type"]},
        "preview": preview,
    })
