"""
Shop Publisher — saved Design drafts (Design Draft Persistence).

shop_designs is the persistent source for saved Shop Designer drafts. It
stores ORCHESTRATION/PRESENTATION configuration only and never duplicates
commerce truth from shop_items (no product snapshots, prices, stock, type or
category data) and never stores resolved output (tokens, warnings, custom_ids,
rendered messages) or publication state.

THE PERSISTED PRESENTATION IS THE DESIGN'S OWN SNAPSHOT (locked):
  A template is loaded into the in-memory draft (the page reads the document
  from the Embed Builder's existing read route) and the draft may be edited;
  on Save the presentation is normalized (utils.shop_publisher
  .normalize_template_doc) and persisted as the design's own presentation
  snapshot. source_template_name is PROVENANCE ONLY — after save it is NEVER
  dereferenced to reload or mutate the saved presentation, and later Embed
  Builder edits to (or deletion of) the source template cannot touch a saved
  design.

CONTRACT:

    GET    /shop-publisher/designs         -> list (session guild, full records)
    POST   /shop-publisher/designs         -> create OR update (full overwrite)
    DELETE /shop-publisher/designs/<id>    -> hard delete

  * Single POST (the Slice 1 pattern): `id` absent or null = create (server
    assigns), `id` present = update. Update is a FULL OVERWRITE of name,
    source_template_name and design_json. `id`, `guild_id` and `created_at`
    are immutable; `updated_at` is refreshed on every successful update.
  * Unknown and cross-guild design ids return the SAME indistinguishable
    404 {"code": "unknown_design"} — cross-guild existence is never disclosed,
    and `guild_id` comes only from the session (a body guild_id is ignored).
  * Delete is a HARD delete (MVP): no revisions, no version history, no soft
    delete, no autosave, no collaboration model. Nothing references designs
    yet, so there is nothing to unlink.
  * Saves are validated with the SAME Step 0 design contract the preview
    route uses (utils.shop_publisher.validate_design): broken drafts are
    rejected with the existing problem codes (empty_roster,
    invalid_product_ref, unknown_product, entry_not_in_roster, ...). A stale
    product reference discovered later (product deleted after save) surfaces
    those SAME existing codes at preview time — this module never invents a
    product-reference error code. `unknown_design` is only for the design id
    in this CRUD.
  * products[] remains a ROOT-product roster. Root/option membership is
    resolved from shop_items.option_of_id; this design document stores only
    product IDs and action presentation configuration, never option commerce.

Error envelope (same shape as the rest of the surface):
    400/404 {"success": false, "error", "problems": [{"code","path","message"}]}

Permission: LEVEL_OWNER (like the whole Shop Publisher surface); the shared
api blueprint's CSRF enforcement covers the POST/DELETE.

Out of scope (locked): option_of_id / Slice 3, publication /
shop_publications, publish or send paths, audit rows (publishing will be the
audited action), category lifecycle UI, /shop changes, purchase executor or
dispatch changes, resolver/token/warning changes.
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
from dashboard.api.shop_publisher import (
    _collect_ids, _row_dict, _PRODUCT_TOKEN_COLUMNS, _PRODUCT_DICT_KEYS,
)

_NAME_MAX = 100
_SOURCE_MAX = 100

_DESIGN_KEYS = ("id", "name", "source_template_name", "created_at", "updated_at")


def _record(row):
    return {
        "id": row[0],
        "name": row[1],
        "source_template_name": row[2],
        "design": json.loads(row[3]),
        "created_at": row[4],
        "updated_at": row[5],
    }


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


async def _get_design(db, design_id, guild_id):
    cursor = await db.execute(
        "SELECT id FROM shop_designs WHERE id = ? AND guild_id = ?",
        (design_id, guild_id))
    return await cursor.fetchone()


async def _fetch_design(db, design_id, guild_id):
    cursor = await db.execute(
        "SELECT id, name, source_template_name, design_json, "
        "created_at, updated_at FROM shop_designs "
        "WHERE id = ? AND guild_id = ?",
        (design_id, guild_id))
    row = await cursor.fetchone()
    return _record(row) if row else None


def _rows_by_id(guild_id, wanted):
    """The session-guild shop_items rows a draft references — the same read
    the preview route performs (shared column lists and row mapping)."""

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
    return rows_by_id


@api_bp.route("/shop-publisher/designs", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_designs_list():
    """Saved Design drafts for the session guild (full records).

    Each record's `design` is the stored Step 0 draft
    ({presentation, products, action}); `presentation` is the design's own
    snapshot and `source_template_name` is provenance only.
    """
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, name, source_template_name, design_json, "
                "created_at, updated_at FROM shop_designs "
                "WHERE guild_id = ? ORDER BY name ASC, id ASC",
                (guild_id,))
            return await cursor.fetchall()

    designs = [_record(row) for row in run_async(fetch())]
    return jsonify({"success": True, "designs": designs})


@api_bp.route("/shop-publisher/designs", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_designs_save():
    """Create (id absent/null) or full-overwrite update (id present).

    The presentation is normalized and persisted as the design's own
    snapshot here — source_template_name is stored as provenance and never
    dereferenced after this point.
    """
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request([_problem("invalid_body", "", "Request body must be a JSON object.")])

    problems = []
    raw_id = data.get("id")
    design_id = None
    if raw_id is not None:
        design_id = _plain_int(raw_id)
        if design_id is None:
            problems.append(_problem("invalid_id", "id", "id must be an integer."))
        else:
            # Resolve the scoped id before validating the overwrite body so an
            # unknown/cross-guild id is ALWAYS the same 404, not a body-dependent
            # 400 that could distinguish existence.
            async def exists():
                async with aiosqlite.connect(DB_PATH) as db:
                    return await _get_design(db, design_id, guild_id)
            if not run_async(exists()):
                return _not_found("unknown_design", "id")

    name = data.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= _NAME_MAX):
        problems.append(_problem(
            "invalid_name", "name",
            f"name must be 1–{_NAME_MAX} characters."))

    source = data.get("source_template_name")
    if source is not None and (not isinstance(source, str)
                               or len(source) > _SOURCE_MAX):
        problems.append(_problem(
            "invalid_source_template", "source_template_name",
            f"source_template_name must be null or at most {_SOURCE_MAX} characters."))

    design = data.get("design")
    if not isinstance(design, dict):
        return jsonify({"success": False,
                        "error": "Send a design object ({presentation, products, action}).",
                        "problems": []}), 400
    presentation = design.get("presentation")
    if (isinstance(presentation, dict)
            and presentation.get("mode", "per_product") not in SP.PRESENTATION_MODES):
        problems.append(_problem("invalid_presentation_mode", "presentation.mode",
                                 "presentation.mode is unsupported."))
    if not isinstance(presentation, dict):
        return jsonify({"success": False,
                        "error": "Send a presentation object ({mode, content, embeds}).",
                        "problems": []}), 400
    action = design.get("action")
    if not isinstance(action, dict):
        return jsonify({"success": False,
                        "error": "Send a purchase action object ({kind, entries}).",
                        "problems": []}), 400

    if problems:
        return _bad_request(problems)

    # Normalize the presentation into the design's OWN snapshot (the same
    # normalizer the read path and the resolver use).
    mode = presentation.get("mode")
    normalized = {
        "mode": mode if isinstance(mode, str) and mode else "per_product",
        **SP.normalize_template_doc(presentation),
    }
    # Persist the orchestration schema only. Ignore unknown request keys rather
    # than accidentally persisting commerce snapshots, resolver output,
    # warnings, generated custom_ids, rendered output, or publication data.
    entries = action.get("entries")
    if isinstance(entries, list):
        stored_entries = [
            ({key: entry[key] for key in ("product_id", "label", "description",
                                          "emoji", "style") if key in entry}
             if isinstance(entry, dict) else entry)
            for entry in entries
        ]
    else:
        stored_entries = entries
    stored_action = {
        "kind": action.get("kind"),
        "entries": stored_entries,
    }
    for key in ("placeholder",):
        if key in action:
            stored_action[key] = action[key]
    draft = {
        "presentation": normalized,
        "products": design.get("products"),
        "action": stored_action,
    }

    problems = SP.validate_design(draft, _rows_by_id(guild_id, _collect_ids(draft)))
    if problems:
        return _bad_request(problems)

    payload = json.dumps(draft)

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            if design_id is None:
                cursor = await db.execute(
                    "INSERT INTO shop_designs (guild_id, name, "
                    "source_template_name, design_json) VALUES (?, ?, ?, ?)",
                    (guild_id, name, source, payload))
                await db.commit()
                return await _fetch_design(db, cursor.lastrowid, guild_id), False

            if not await _get_design(db, design_id, guild_id):
                return None, True  # unknown or cross-guild — same 404

            await db.execute(
                "UPDATE shop_designs SET name = ?, source_template_name = ?, "
                "design_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND guild_id = ?",
                (name, source, payload, design_id, guild_id))
            await db.commit()
            return await _fetch_design(db, design_id, guild_id), False

    record, missing = run_async(write())
    if missing:
        return _not_found("unknown_design", "id")
    return jsonify({"success": True, "design": record})


@api_bp.route("/shop-publisher/designs/<int:design_id>", methods=["DELETE"])
@require_api_permission(LEVEL_OWNER)
def api_shop_designs_delete(design_id: int):
    """Hard delete only while no Publication association references the Design."""
    guild_id = get_session_guild_id()

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            # Serialize this check/delete against pending Publication inserts.
            # SQLite FK enforcement is connection-local in this project, so
            # the association check is explicit and server-authoritative.
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM shop_designs WHERE id = ? AND guild_id = ?",
                (design_id, guild_id))
            if await cursor.fetchone() is None:
                await db.rollback()
                return "missing", 0
            cursor = await db.execute(
                "SELECT 1 FROM shop_publications "
                "WHERE design_id = ? AND guild_id = ? LIMIT 1",
                (design_id, guild_id))
            if await cursor.fetchone() is not None:
                await db.rollback()
                return "published", 0
            cursor = await db.execute(
                "DELETE FROM shop_designs WHERE id = ? AND guild_id = ?",
                (design_id, guild_id))
            await db.commit()
            return "deleted", cursor.rowcount

    outcome, deleted = run_async(write())
    if outcome == "missing" or not deleted:
        return _not_found("unknown_design", "design_id")
    if outcome == "published":
        return jsonify({
            "success": False,
            "code": "design_has_publications",
            "error": "This Design has Publications. Unpublish them first.",
        }), 409
    return jsonify({"success": True})
