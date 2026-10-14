"""
Shop Publisher — read-only catalog + design-draft preview routes (Step 0).

These routes serve the Shop Designer's front half only: template source
listing, product listing and the fixed-token PREVIEW of a Design draft. There
is NO publish/send route here (a later step), NO shop_publications table, and
nothing in this module writes to the database — the preview is pure
computation over embed_templates + shop_items + the currency config.

THE PREVIEW CONTRACT (Step 0, locked):

    POST /api/shop-publisher/preview
    {
      "presentation": {"mode": "per_product" | "frame", "content", "embeds"},
      "products":  [root_product_id, ...],      # ROOT-PRODUCT roster
      "action":    {"kind": "buttons" | "product_select" | "option_select",
                    "entries": [{"product_id", "label", ...}, ...]}
    }

    -> {"success": true, "preview": {mode, content, embeds, action, warnings,
                                     tokens}}
    -> 400 {"success": false, "error", "problems": [{code, path, message}]}

products[] is the Design's explicit ROOT-PRODUCT roster: only rows with
option_of_id IS NULL may appear there. An action entry may reference a
purchase-option row only when its root product is in products[]. Violations
are rejected by utils/shop_publisher.validate_design() (option_in_roster,
option_outside_roster, entry_not_in_roster, …). The option_of_id column is
read through the row dicts the shared resolver builds; until that column
lands every DB row is simply a root, and the validator's synthetic field is
exercised by scripts/test_shop_publisher.py.

Why a separate submodule instead of dashboard/api/economy_shop.py or
dashboard/api/embedbuilder.py: both of those are frozen delivery boundaries of
previous work (the Shop admin and the Embed Builder). Registering on the shared
api_bp from a new file is the same zero-risk pattern every other surface here
uses — existing routes, URLs and response shapes are untouched.

Permission: LEVEL_OWNER, matching the Embed Builder send/template routes. The
Publisher consumes embed_templates (managed at LEVEL_OWNER) and later steps
will send live Discord messages from the same surface, so it must never be a
widening path for either one.

Publish boundary note (for the next steps): the publish route that lands here
must reuse utils/shop_publisher.preview_design() (so preview == publish) and
the build_purchase_action() descriptor (so the published action IS the
previewed action), and must decide its blocking policy over these same
warning codes.
"""

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

# shop_items columns the preview needs: identity, the price line, every
# token/state field, and the persisted one-level option relationship used by
# the shared root/option validator.
_PRODUCT_TOKEN_COLUMNS = (
    "id, name, type, price, price_diamonds, enabled, "
    "current_stock, max_stock, prestige_tier, featured, "
    "description, duration_hours, required_level, rarity, icon_url, "
    "option_of_id"
)
_PRODUCT_LIST_COLUMNS = (
    "id, name, type, price, price_diamonds, enabled, "
    "current_stock, max_stock, prestige_tier, featured, category_id, "
    "option_of_id"
)
_PRODUCT_LIST_KEYS = (
    "id", "name", "type", "price", "price_diamonds", "enabled",
    "current_stock", "max_stock", "prestige_tier", "featured", "category_id",
    "option_of_id",
)
_PRODUCT_DICT_KEYS = (
    "id", "name", "type", "price", "price_diamonds", "enabled",
    "current_stock", "max_stock", "prestige_tier", "featured",
    "description", "duration_hours", "required_level", "rarity", "icon_url",
    "option_of_id",
)


def _row_dict(row, keys) -> dict:
    return dict(zip(keys, row))


@api_bp.route("/shop-publisher/catalog", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publisher_catalog():
    """Both pickers + the fixed token catalog, one round trip.

    templates: embed_templates names only (["name"]) — the EXPLICIT final
               Step 0 contract (Q2#12 CLOSED 2026-09-30, superseding the
               {name,data} ratification): at the expected ~100-template
               scale the catalog stays lightweight and only the selected
               template's document is loaded. The preview page reads that
               document on selection from the Embed Builder's existing
               GET /api/embedbuilder/template/<name>, used ONLY as the Step 0
               transitional presentation source — NOT the final Shop
               template load/snapshot architecture, which stays deferred to
               the Step 1+ design discussion.
    products:  shop_items rows (the product source of truth) with the additive
               `category_id` link (Slice 2 — the nullable Shop Category
               membership; presentation metadata, NEVER publication). The
               deterministic sort (type-group order, then name, then id) is
               preserved exactly as before; the picker groups by category
               client-side and keeps `type` as display metadata only.
    tokens:    utils/shop_publisher.token_catalog_payload() — the FIXED catalog
               (canonical spellings + friendly labels/groups + aliases), so the
               Insert Dynamic Field menu and the resolver cannot drift.
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
                f"SELECT {_PRODUCT_LIST_COLUMNS} FROM shop_items "
                "WHERE guild_id = ? ORDER BY id ASC",
                (guild_id,))
            products = [_row_dict(r, _PRODUCT_LIST_KEYS)
                        for r in await cursor.fetchall()]
        products.sort(key=lambda p: (SP.type_group_key(p["type"] or ""),
                                     (p["name"] or "").lower(), p["id"]))
        return templates, products

    templates, products = run_async(fetch())
    return jsonify({
        "templates": templates,
        "products": products,
        "tokens": SP.token_catalog_payload(),
    })


@api_bp.route("/shop-publisher/products/<int:root_id>/options", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publisher_product_options(root_id):
    """Additive guild-scoped family read; leaves the accepted catalog route intact."""
    guild_id = get_session_guild_id()
    keys = ("id", "name", "type", "price", "price_diamonds", "enabled",
            "current_stock", "max_stock", "prestige_tier", "featured",
            "option_of_id")
    columns = ", ".join(keys)

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT {columns} FROM shop_items "
                "WHERE id = ? AND guild_id = ? AND option_of_id IS NULL",
                (root_id, guild_id))
            root = await cursor.fetchone()
            if root is None:
                return None, []
            cursor = await db.execute(
                f"SELECT {columns} FROM shop_items "
                "WHERE guild_id = ? AND option_of_id = ? ORDER BY id ASC",
                (guild_id, root_id))
            return root, await cursor.fetchall()

    root, options = run_async(fetch())
    if root is None:
        return jsonify({"success": False, "error": "Not found."}), 404
    return jsonify({
        "success": True,
        "root": _row_dict(root, keys),
        "options": [_row_dict(row, keys) for row in options],
    })


def _collect_ids(design) -> list:
    ids = []
    for pid in design.get("products") or []:
        if isinstance(pid, int) and not isinstance(pid, bool):
            ids.append(pid)
    action = design.get("action")
    entries = action.get("entries") if isinstance(action, dict) else None
    for entry in entries or []:
        entry = entry if isinstance(entry, dict) else {}
        pid = entry.get("product_id")
        if isinstance(pid, int) and not isinstance(pid, bool):
            ids.append(pid)
    seen, ordered = set(), []
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return ordered


@api_bp.route("/shop-publisher/preview", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publisher_preview():
    """Resolve a Design draft server-side and return the preview.

    The resolution runs through utils/shop_publisher.preview_design() — the
    same pure module the later publish path must call — so what this route
    returns IS the message that would go to Discord, plus the purchase action
    that would actually be published and the deterministic preview warnings.

    Read-only by construction: no INSERT/UPDATE/DELETE, no audit-log entry
    (previewing is not an action worth a log row; publishing will be).
    """
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object.",
                        "problems": []}), 400

    presentation = data.get("presentation")
    if not isinstance(presentation, dict):
        return jsonify({"success": False,
                        "error": "Send a presentation object ({mode, content, embeds}).",
                        "problems": []}), 400

    products = data.get("products")
    action = data.get("action")
    if not isinstance(action, dict):
        return jsonify({"success": False,
                        "error": "Send a purchase action object ({kind, entries}).",
                        "problems": []}), 400

    design = {
        "id": data.get("id") if isinstance(data.get("id"), int) else 0,
        "presentation": presentation,
        "products": products,
        "action": action,
    }

    wanted = _collect_ids(design)

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            if wanted:
                marks = ",".join("?" * len(wanted))
                cursor = await db.execute(
                    f"SELECT {_PRODUCT_TOKEN_COLUMNS} FROM shop_items "
                    f"WHERE guild_id = ? AND id IN ({marks})",
                    (guild_id, *wanted))
                rows = await cursor.fetchall()
            else:
                rows = []
        return rows

    rows_by_id = {}
    for row in run_async(fetch()):
        product = _row_dict(row, _PRODUCT_DICT_KEYS)
        rows_by_id[product["id"]] = product

    problems = SP.validate_design(design, rows_by_id)
    if problems:
        return jsonify({
            "success": False,
            "error": problems[0]["message"],
            "problems": problems,
        }), 400

    currency = run_async(get_currency_config(guild_id))
    preview = SP.preview_design(design, rows_by_id, currency)

    return jsonify({"success": True, "preview": preview})
