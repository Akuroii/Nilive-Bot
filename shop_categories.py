"""
Shop Publisher — Category CRUD (Step 1, Slice 1).

The flat, guild-scoped presentation Category. shop_categories holds NO
commerce data (no price/stock/duration/type) and shop_items.category_id is
the ONLY link between a product and its category. Category membership is
pure presentation metadata and NEVER implies publication — a categorized
product is not published anywhere.

CONTRACT (locked by the Slice 1 v2 plan):

    GET    /shop-publisher/categories          -> list + product counts
    POST   /shop-publisher/categories          -> create OR update
    DELETE /shop-publisher/categories/<id>     -> delete + NULL the links
    POST   /shop-publisher/categories/reorder  -> bulk sort_order
    POST   /shop-publisher/products/category   -> assign/unassign the link

  * Create when `id` is absent or null. Update only when `id` exists in the
    SESSION guild. An unknown id and an id belonging to another guild return
    the SAME indistinguishable 404 {"code": "unknown_category"} — cross-guild
    existence is never disclosed, and no request can move or mutate a
    category across guilds: `guild_id` comes only from the session and is
    never read from any request body.
  * Mutable fields: name, emoji, enabled, sort_order ONLY. `id`, `guild_id`
    and `created_at` are immutable. `updated_at` is refreshed on every
    successful update and reorder.
  * Product assignment requires the product AND the category to resolve
    within the same session guild; misses return the same indistinguishable
    "unknown_product" / "unknown_category" 404s.
  * Deleting a category NULLs the matching shop_items.category_id links in
    the same guild, so this layer never leaves stale product references.
  * The ONLY shop_items write this module ever performs is the nullable
    category_id link (assign/unassign and the delete-time NULL reset).
    Commerce fields are never written here.

Error envelope (same shape as the Step 0 preview contract):
    400/404 {"success": false, "error", "problems": [{code, path, message}]}

Permission: LEVEL_OWNER, matching the rest of the Shop Publisher surface.

Out of scope (later, separately approved slices): picker/UI integration
(Slice 2), the option_of_id schema (gated Slice 3), Designer/shop_designs,
publication/snapshots, any warning machinery.
"""

import aiosqlite
from flask import jsonify, request

from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, require_api_permission, LEVEL_OWNER,
)
from dashboard.api import api_bp

_NAME_MAX = 100
_EMOJI_MAX = 100
_DEFAULT_EMOJI = "\U0001F3AB"  # 🎫 — the column default, applied explicitly

_CATEGORY_KEYS = (
    "id", "name", "emoji", "enabled", "sort_order", "created_at", "updated_at",
)


def _row_dict(row, keys):
    return dict(zip(keys, row))


def _problem(code, path, message):
    return {"code": code, "path": path, "message": message}


def _bad_request(problems):
    return jsonify({
        "success": False,
        "error": problems[0]["message"] if problems else "Invalid request.",
        "problems": problems,
    }), 400


def _not_found(code, path):
    # Unknown and cross-guild ids share this exact response — no cross-guild
    # existence disclosure, ever.
    return jsonify({
        "success": False,
        "error": "Not found.",
        "problems": [_problem(code, path, "Not found.")],
    }), 404


def _plain_int(value):
    """JSON int (bools are NOT ints here); everything else is invalid."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def _get_category(db, cat_id, guild_id):
    cursor = await db.execute(
        "SELECT id FROM shop_categories WHERE id = ? AND guild_id = ?",
        (cat_id, guild_id))
    return await cursor.fetchone()


async def _fetch_category(db, cat_id, guild_id):
    cursor = await db.execute(
        "SELECT id, name, emoji, enabled, sort_order, created_at, updated_at "
        "FROM shop_categories WHERE id = ? AND guild_id = ?",
        (cat_id, guild_id))
    row = await cursor.fetchone()
    return _row_dict(row, _CATEGORY_KEYS) if row else None


@api_bp.route("/shop-publisher/categories", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_categories_list():
    """Categories for the session guild + product counts.

    Counts are pure presentation metadata: how many shop_items rows carry
    each category_id (and how many are uncategorized). Nothing here implies
    publication.
    """
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, name, emoji, enabled, sort_order, "
                "created_at, updated_at FROM shop_categories "
                "WHERE guild_id = ? ORDER BY sort_order ASC, name ASC, id ASC",
                (guild_id,))
            categories = [_row_dict(r, _CATEGORY_KEYS)
                          for r in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT category_id, COUNT(*) FROM shop_items "
                "WHERE guild_id = ? GROUP BY category_id",
                (guild_id,))
            counts = {r[0]: r[1] for r in await cursor.fetchall()}
        uncategorized = counts.pop(None, 0)
        for category in categories:
            category["product_count"] = counts.get(category["id"], 0)
        return categories, uncategorized

    categories, uncategorized = run_async(fetch())
    return jsonify({
        "success": True,
        "categories": categories,
        "uncategorized": uncategorized,
    })


@api_bp.route("/shop-publisher/categories", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_categories_save():
    """Create (id absent/null) or update (id in the session guild).

    name is required on both paths. emoji / enabled / sort_order are
    optional: provided -> validated and set; omitted -> default on create,
    kept on update. id/guild_id/created_at are immutable; updated_at is
    refreshed on every successful update.
    """
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request([_problem("invalid_body", "", "Request body must be a JSON object.")])
    problems = []

    raw_id = data.get("id")
    if raw_id is None:
        cat_id = None
    else:
        cat_id = _plain_int(raw_id)
        if cat_id is None or cat_id < 1:
            problems.append(_problem(
                "invalid_id", "id", "id must be a positive integer or null."))

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(_problem(
            "invalid_name", "name", "name must be a non-empty string."))
        name = None
    elif len(name.strip()) > _NAME_MAX:
        problems.append(_problem(
            "invalid_name", "name",
            f"name must be at most {_NAME_MAX} characters."))
        name = None
    else:
        name = name.strip()

    values = {}
    if "emoji" in data:
        emoji = data.get("emoji")
        if not isinstance(emoji, str):
            problems.append(_problem(
                "invalid_emoji", "emoji", "emoji must be a string."))
        elif len(emoji.strip()) > _EMOJI_MAX:
            problems.append(_problem(
                "invalid_emoji", "emoji",
                f"emoji must be at most {_EMOJI_MAX} characters."))
        else:
            values["emoji"] = emoji.strip() or _DEFAULT_EMOJI
    if "enabled" in data:
        enabled = data.get("enabled")
        if isinstance(enabled, bool):
            values["enabled"] = 1 if enabled else 0
        elif isinstance(enabled, int) and enabled in (0, 1):
            values["enabled"] = enabled
        else:
            problems.append(_problem(
                "invalid_enabled", "enabled", "enabled must be 0 or 1."))
    if "sort_order" in data:
        sort_order = _plain_int(data.get("sort_order"))
        if sort_order is None or sort_order < 0:
            problems.append(_problem(
                "invalid_sort_order", "sort_order",
                "sort_order must be an integer >= 0."))
        else:
            values["sort_order"] = sort_order

    if problems:
        return _bad_request(problems)

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            if cat_id is None:
                # CREATE — server-assigned id, session guild only. The
                # body's guild_id (if any) is deliberately never read.
                values.setdefault("emoji", _DEFAULT_EMOJI)
                values.setdefault("enabled", 1)
                values.setdefault("sort_order", 0)
                cursor = await db.execute(
                    "INSERT INTO shop_categories "
                    "(guild_id, name, emoji, enabled, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (guild_id, name, values["emoji"], values["enabled"],
                     values["sort_order"]))
                new_id = cursor.lastrowid
                await db.commit()
                return await _fetch_category(db, new_id, guild_id), False

            if not await _get_category(db, cat_id, guild_id):
                return None, True  # unknown or cross-guild — same 404

            assignments = ["name = ?"]
            params = [name]
            for field in ("emoji", "enabled", "sort_order"):
                if field in values:
                    assignments.append(f"{field} = ?")
                    params.append(values[field])
            assignments.append("updated_at = CURRENT_TIMESTAMP")
            params.extend([cat_id, guild_id])
            await db.execute(
                f"UPDATE shop_categories SET {', '.join(assignments)} "
                "WHERE id = ? AND guild_id = ?", params)
            await db.commit()
            return await _fetch_category(db, cat_id, guild_id), False

    category, missing = run_async(write())
    if missing:
        return _not_found("unknown_category", "id")
    return jsonify({"success": True, "category": category})


@api_bp.route("/shop-publisher/categories/<int:cat_id>", methods=["DELETE"])
@require_api_permission(LEVEL_OWNER)
def api_shop_categories_delete(cat_id: int):
    """Delete a category and NULL its product links (same guild).

    Products become uncategorized — never stale. Nothing else about the
    products changes: the only shop_items write is category_id = NULL.
    """
    guild_id = get_session_guild_id()

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            if not await _get_category(db, cat_id, guild_id):
                return None
            cursor = await db.execute(
                "UPDATE shop_items SET category_id = NULL "
                "WHERE category_id = ? AND guild_id = ?",
                (cat_id, guild_id))
            unlinked = cursor.rowcount
            await db.execute(
                "DELETE FROM shop_categories WHERE id = ? AND guild_id = ?",
                (cat_id, guild_id))
            await db.commit()
            return unlinked

    unlinked = run_async(write())
    if unlinked is None:
        return _not_found("unknown_category", "category_id")
    return jsonify({"success": True, "unlinked_products": unlinked})


@api_bp.route("/shop-publisher/categories/reorder", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_categories_reorder():
    """Bulk-set sort_order from an ordered list of session-guild ids.

    All-or-nothing: any unknown/cross-guild id (or duplicate) rejects the
    whole request before a single row is written. Each written row gets
    sort_order = its index and a refreshed updated_at.
    """
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request([_problem("invalid_body", "", "Request body must be a JSON object.")])
    order = data.get("order")
    if not isinstance(order, list):
        return _bad_request([_problem(
            "invalid_reorder", "order", "order must be a list of ids.")])

    problems = []
    seen = set()
    for index, raw in enumerate(order):
        cid = _plain_int(raw)
        if cid is None:
            problems.append(_problem(
                "invalid_reorder", f"order[{index}]",
                "order entries must be integers."))
            continue
        if cid in seen:
            problems.append(_problem(
                "duplicate_entry", f"order[{index}]",
                "order entries must be unique."))
            continue
        seen.add(cid)
    if problems:
        return _bad_request(problems)

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            for index, cid in enumerate(order):
                if not await _get_category(db, cid, guild_id):
                    return index
                await db.execute(
                    "UPDATE shop_categories "
                    "SET sort_order = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND guild_id = ?",
                    (index, cid, guild_id))
            await db.commit()
            return None

    missing_index = run_async(write())
    if missing_index is not None:
        return _not_found("unknown_category", f"order[{missing_index}]")
    return jsonify({"success": True})


@api_bp.route("/shop-publisher/products/category", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_product_category():
    """Assign (category_id int) or unassign (category_id null) one link.

    The product AND the category must resolve within the session guild;
    misses return the same indistinguishable 404s used everywhere else. The
    only column written on shop_items is category_id — commerce fields are
    never touched by Category operations.
    """
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request([_problem("invalid_body", "", "Request body must be a JSON object.")])
    problems = []

    product_id = _plain_int(data.get("product_id"))
    if product_id is None or product_id < 1:
        problems.append(_problem(
            "invalid_product_id", "product_id",
            "product_id must be a positive integer."))

    raw_category = data.get("category_id")
    if raw_category is None:
        category_id = None
    else:
        category_id = _plain_int(raw_category)
        if category_id is None or category_id < 1:
            problems.append(_problem(
                "invalid_category_id", "category_id",
                "category_id must be a positive integer or null."))

    if problems:
        return _bad_request(problems)

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id FROM shop_items WHERE id = ? AND guild_id = ?",
                (product_id, guild_id))
            if not await cursor.fetchone():
                return "product"
            if category_id is not None:
                if not await _get_category(db, category_id, guild_id):
                    return "category"
            await db.execute(
                "UPDATE shop_items SET category_id = ? "
                "WHERE id = ? AND guild_id = ?",
                (category_id, product_id, guild_id))
            await db.commit()
            return None

    missing = run_async(write())
    if missing == "product":
        return _not_found("unknown_product", "product_id")
    if missing == "category":
        return _not_found("unknown_category", "category_id")
    return jsonify({"success": True})
