import aiosqlite
from flask import jsonify, request, session
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission,
    LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Minigames v2 API (Phase 4) ───────────────────────────────────────────────
#
# The v1 tier-based API (GET/POST/DELETE /minigames/tiers, /tier) is
# RETIRED: v2's data model is categories → templates → rewards
# (utils/minigame_store.py), which is also what the spawn engine
# (cogs/minigames.py, Phase 2/3) already consumes. This module is the
# dashboard's write-ownership surface (plan §19: categories,
# templates, rewards and spawn requests are written ONLY here — the
# bot writes bag/log/counter state).
#
# Conventions (same as the rest of dashboard/api):
#   * guild id comes ONLY from the session (get_session_guild_id) —
#     a client-supplied guild_id is never trusted (IDOR guard);
#   * every route is @require_api_permission(LEVEL_ADMIN);
#   * validation/business errors return HTTP 200 + {"success": False,
#     "error": ...} — the dashboard's ajaxSave/fetch handlers key off
#     the JSON flag; only decorator-level failures (401/403/CSRF) use
#     HTTP status codes. The one exception is DELETE
#     /minigames/categories/<id>, which returns 409 when the category
#     still has content (plan §10: "409 if it has direct templates or
#     subcategories").
#   * mutations are audited with log_action(guild_id, action,
#     "minigames", ...).
#
# Importing utils/minigame_store is safe from the Flask process: it is
# aiosqlite-only (no discord import), and ensure_tables() is a pure,
# idempotent CREATE TABLE IF NOT EXISTS guard (the dashboard process
# runs database.py's central init, not the cog's cog_load).


# ═══════════════════════════════════════════════════════════════════
# Validation helpers (plan §17 — the server is the enforcement point;
# the builder UI mirrors these limits so a normal user never sees a
# rejection, but a crafted API call still cannot store a broken game).
# ═══════════════════════════════════════════════════════════════════

_DISCORD_LIMITS = {
    "title": 256, "description": 4096, "author": 256, "footer": 256,
    "field_name": 256, "field_value": 1024, "max_fields": 25,
}


def _validate_embed(embed) -> str | None:
    """Return a user-facing error string, or None when the embed is
    Discord-legal. `embed` is the API shape (author/footer/image/
    thumbnail may be dicts or strings — both are accepted and stored
    as sent; the engine normalizes before posting)."""
    if embed is None:
        return None
    if not isinstance(embed, dict):
        return "embed must be an object"

    # title/description are always strings; author/footer may arrive
    # as plain strings OR as API-shape dicts ({name} / {text}) — the
    # composer's cleanEmbedForPayload produces the dict form.
    for key in ("title", "description", "author", "footer"):
        v = embed.get(key, "")
        if v is None:
            continue
        limit = _DISCORD_LIMITS[key]
        if isinstance(v, str):
            if len(v) > limit:
                return f"embed {key} exceeds Discord's {limit}-character limit"
        elif key in ("author", "footer") and isinstance(v, dict):
            inner = v.get("name" if key == "author" else "text", "")
            if not isinstance(inner, str):
                return f"embed {key} must be a string or an object"
            if len(inner) > limit:
                return f"embed {key} exceeds Discord's {limit}-character limit"
        else:
            return f"embed {key} must be a string"

    color = embed.get("color")
    if color is not None and color != "":
        if isinstance(color, bool) or not isinstance(color, int) or not (0 <= color <= 0xFFFFFF):
            return "embed color must be an integer 0..16777215"

    fields = embed.get("fields") or []
    if not isinstance(fields, list) or len(fields) > _DISCORD_LIMITS["max_fields"]:
        return f"embed may have at most {_DISCORD_LIMITS['max_fields']} fields"
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            return f"embed field {i + 1} must be an object"
        for fk, key in (("name", "field_name"), ("value", "field_value")):
            v = f.get(fk, "")
            if v is None:
                v = ""
            if not isinstance(v, str):
                return f"embed field {i + 1} {fk} must be a string"
            if len(v) > _DISCORD_LIMITS[key]:
                return (f"embed field {i + 1} {fk} exceeds Discord's "
                        f"{_DISCORD_LIMITS[key]}-character limit")
    return None


def _validate_rewards(rows, label="rewards") -> str | None:
    """Reward pool rows (plan §7/§17). Empty list is VALID (D11)."""
    from utils.minigame_store import VALID_REWARD_TYPES
    if rows is None:
        return None
    if not isinstance(rows, list):
        return f"{label} must be a list"
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return f"{label}[{i + 1}] must be an object"
        rtype = row.get("reward_type") or row.get("type")
        if rtype not in VALID_REWARD_TYPES:
            return (f"{label}[{i + 1}] reward_type must be one of: "
                    f"{', '.join(VALID_REWARD_TYPES)}")
        value = row.get("reward_value", row.get("value"))
        if value is None or str(value).strip() == "":
            return f"{label}[{i + 1}] reward_value is required"
        if len(str(value)) > 200:
            return f"{label}[{i + 1}] reward_value is too long"
        weight = row.get("weight")
        if weight is not None:
            try:
                if int(weight) < 1:
                    return f"{label}[{i + 1}] weight must be >= 1"
            except (TypeError, ValueError):
                return f"{label}[{i + 1}] weight must be an integer"
        dur = row.get("duration_hours")
        if dur is not None:
            try:
                if int(dur) <= 0:
                    return f"{label}[{i + 1}] duration_hours must be positive"
            except (TypeError, ValueError):
                return f"{label}[{i + 1}] duration_hours must be an integer"
    return None


def _num(value, lo, hi, label) -> str | None:
    """Finite number within [lo, hi]; returns error or None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"{label} must be a number"
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return f"{label} must be a number"
    if v < lo or v > hi:
        return f"{label} must be between {lo:g} and {hi:g}"
    return None


def _validate_game_config(game_type: str, config) -> str | None:
    """Per-type game settings (plan §11.2/§17). `config` is the dict
    that will be stored as config_json; only the keys the engine reads
    are validated, anything else passes through untouched."""
    from utils.minigame_store import VALID_GAME_TYPES
    if game_type not in VALID_GAME_TYPES:
        return f"game_type must be one of: {', '.join(VALID_GAME_TYPES)}"
    if config is None:
        return None
    if not isinstance(config, dict):
        return "game config must be an object"

    if game_type == "quick_click":
        b = config.get("buttons")
        if b is not None:
            try:
                b = int(b)
            except (TypeError, ValueError):
                return "buttons must be an integer"
            if not (2 <= b <= 6):
                return "buttons must be between 2 and 6"
        err = _num(config.get("reveal_min"), 1, 600, "reveal_min")
        if err:
            return err
        err = _num(config.get("reveal_max"), 1, 600, "reveal_max")
        if err:
            return err
        err = _num(config.get("wait_after"), 0, 600, "wait_after")
        if err:
            return err
        try:
            if config.get("reveal_min") is not None and config.get("reveal_max") is not None \
                    and float(config["reveal_max"]) < float(config["reveal_min"]):
                return "reveal_max must be >= reveal_min"
        except (TypeError, ValueError):
            pass
        return None

    if game_type == "wheel":
        return _num(config.get("join_seconds"), 5, 600, "join_seconds")

    if game_type in ("math", "colors", "emoji"):
        answers = config.get("answers")
        if not isinstance(answers, list) or not (2 <= len(answers) <= 6):
            return "answers must be a list of 2 to 6 options"
        for i, a in enumerate(answers):
            if not isinstance(a, str) or a.strip() == "":
                return f"answer {i + 1} must be non-empty text"
            if len(a) > 80:
                return f"answer {i + 1} exceeds Discord's 80-character button limit"
        correct = config.get("correct")
        try:
            correct = int(correct)
        except (TypeError, ValueError):
            return "correct must be a number"
        if not (0 <= correct < len(answers)):
            return "select which answer is correct"
        return _num(config.get("seconds"), 5, 600, "seconds")

    if game_type == "rps":
        err = _num(config.get("seating_seconds"), 5, 600, "seating_seconds")
        if err:
            return err
        return _num(config.get("choice_seconds"), 5, 600, "choice_seconds")

    return None


def _validate_colors_image(game_type: str, embed) -> str | None:
    """§11.2: the Colors game's image (required) lives in the embed —
    the engine posts the embed plus the answer buttons as one
    message, so the image the players see IS the embed's image."""
    if game_type != "colors" or not isinstance(embed, dict):
        return None
    image = embed.get("image")
    url = image.get("url") if isinstance(image, dict) else image
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return "the Colors game needs an image — set a valid image URL in the Embed section"
    return None


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

@api_bp.route("/minigames/config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigames_config_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.get_config(guild_id)

    return jsonify({"config": run_async(fetch())})


@api_bp.route("/minigames/config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_minigames_config_api():
    guild_id = get_session_guild_id()
    data = request.json or {}

    fields = {}
    if "enabled" in data:
        fields["enabled"] = int(bool(data["enabled"]))
    if "channel_id" in data:
        fields["channel_id"] = data["channel_id"]
    if "min_events_per_week" in data:
        try:
            fields["min_events_per_week"] = int(data["min_events_per_week"])
        except (TypeError, ValueError):
            return jsonify({"success": False,
                            "error": "min_events_per_week must be a number"})
    if "max_events_per_week" in data:
        try:
            fields["max_events_per_week"] = int(data["max_events_per_week"])
        except (TypeError, ValueError):
            return jsonify({"success": False,
                            "error": "max_events_per_week must be a number"})
    if "global_default_rewards" in data:
        fields["global_default_rewards"] = data["global_default_rewards"]

    err = _validate_rewards(fields.get("global_default_rewards"),
                            "global_default_rewards")
    if err:
        return jsonify({"success": False, "error": err})
    min_ev = fields.get("min_events_per_week")
    max_ev = fields.get("max_events_per_week")
    if min_ev is not None and min_ev < 1:
        return jsonify({"success": False,
                        "error": "min_events_per_week must be at least 1"})
    if min_ev is not None and max_ev is not None and max_ev < min_ev:
        return jsonify({"success": False,
                        "error": "max_events_per_week must be >= min_events_per_week"})

    async def save():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.save_config(guild_id, **fields)

    cfg = run_async(save())
    log_action(guild_id, "Updated minigames config", "minigames")
    return jsonify({"success": True, "config": cfg})


# ═══════════════════════════════════════════════════════════════════
# CATEGORIES
# ═══════════════════════════════════════════════════════════════════

@api_bp.route("/minigames/categories", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigames_categories_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.get_categories_tree(guild_id)

    return jsonify({"tree": run_async(fetch())})


@api_bp.route("/minigames/categories", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def create_minigame_category_api():
    guild_id = get_session_guild_id()
    data = request.json or {}

    parent_id = data.get("parent_id")
    if parent_id in ("", "null", "none"):
        parent_id = None

    async def create():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.create_category(
            guild_id, data.get("name", ""), parent_id=parent_id,
            weight=data.get("weight", 1), emoji=data.get("emoji"),
            color=data.get("color"),
            default_rewards=data.get("default_rewards"))

    try:
        cat = run_async(create())
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    log_action(guild_id, f"Created minigame category '{cat['name']}'",
               "minigames", target_id=cat["id"], target_name=cat["name"])
    return jsonify({"success": True, "category": cat})


@api_bp.route("/minigames/categories/<int:category_id>", methods=["PATCH"])
@require_api_permission(LEVEL_ADMIN)
def update_minigame_category_api(category_id: int):
    guild_id = get_session_guild_id()
    data = request.json or {}

    fields = {}
    for key in ("name", "weight", "parent_id", "emoji", "color",
                "default_rewards", "enabled"):
        if key in data:
            fields[key] = data[key]
    if "parent_id" in fields and fields["parent_id"] in ("", "null", "none"):
        fields["parent_id"] = None

    err = _validate_rewards(fields.get("default_rewards"), "default_rewards")
    if err:
        return jsonify({"success": False, "error": err})

    async def update():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.update_category(guild_id, category_id, fields)

    try:
        cat = run_async(update())
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    if cat is None:
        return jsonify({"success": False, "error": "category not found"})
    log_action(guild_id, f"Updated minigame category '{cat['name']}'",
               "minigames", target_id=category_id, target_name=cat["name"])
    return jsonify({"success": True, "category": cat})


@api_bp.route("/minigames/categories/<int:category_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_minigame_category_api(category_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.delete_category(guild_id, category_id)

    ok, msg = run_async(delete())
    if not ok:
        # 409 is the one deliberate HTTP-status use in this module —
        # plan §10: deleting a category with content is a conflict the
        # UI must surface as "move/delete its content first".
        return jsonify({"success": False, "error": msg}), 409
    log_action(guild_id, f"Deleted minigame category #{category_id}",
               "minigames", target_id=category_id)
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════
# TEMPLATES
# ═══════════════════════════════════════════════════════════════════

def _template_fields_from_payload(data, guild_id, require_name: bool):
    """Shared create/update payload → (store fields, error). The store
    raises ValueError on its own invariants; these helpers catch the
    per-type / embed / rewards rules so the error text is friendly."""
    fields = {}
    name = data.get("name")
    if require_name or name is not None:
        fields["name"] = name
    if "category_id" in data and data["category_id"] not in (None, ""):
        fields["category_id"] = data["category_id"]
    if "game_type" in data:
        fields["game_type"] = data["game_type"]
    if "enabled" in data:
        fields["enabled"] = bool(data["enabled"])
    if "auto_spawn" in data:
        fields["auto_spawn"] = bool(data["auto_spawn"])
    if "embed" in data:
        fields["embed"] = data["embed"] or {}
    if "config" in data:
        fields["config"] = data["config"] or {}
    if "channel_id" in data:
        fields["channel_id"] = data["channel_id"]
    if "rewards" in data:
        fields["rewards"] = data["rewards"]

    # Cross-field validation needs the EFFECTIVE game type (payload
    # value, or the stored one on update).
    game_type = fields.get("game_type")
    embed = fields.get("embed")
    config = fields.get("config")
    if game_type and "config" in data:
        err = _validate_game_config(game_type, config)
        if err:
            return None, err
    if "embed" in data:
        err = _validate_embed(embed)
        if err:
            return None, err
    if game_type and "embed" in data:
        err = _validate_colors_image(game_type, embed)
        if err:
            return None, err
    if "rewards" in data:
        err = _validate_rewards(fields.get("rewards"))
        if err:
            return None, err
    return fields, None


@api_bp.route("/minigames/templates", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def list_minigame_templates_api():
    guild_id = get_session_guild_id()
    category_id = request.args.get("category_id")
    category_id = int(category_id) if category_id not in (None, "") else None
    include_disabled = request.args.get("include_disabled", "1") not in ("0", "false")

    async def fetch():
        from utils import minigame_store as store
        await store.ensure_tables()
        rows = await store.list_templates(guild_id, category_id=category_id,
                                          include_disabled=include_disabled)
        # Live-run / queued flags (plan §10 list: "live-run flag") — a
        # template whose snapshot is running must show it, and an
        # in-flight spawn request must show as queued.
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT template_id FROM minigames_log "
                "WHERE guild_id = ? AND ended_at IS NULL AND status = 'running'",
                (guild_id,))
            live = {r[0] for r in await cursor.fetchall()}
            in_flight = await store.get_in_flight_requests(guild_id)
        queued = {r["template_id"] for r in in_flight}
        for row in rows:
            row["live_run"] = row["id"] in live
            row["queued"] = row["id"] in queued
        return rows

    return jsonify({"templates": run_async(fetch())})


@api_bp.route("/minigames/templates", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def create_minigame_template_api():
    guild_id = get_session_guild_id()
    data = request.json or {}

    category_id = data.get("category_id")
    if not category_id:
        return jsonify({"success": False, "error": "pick a category"})

    fields, err = _template_fields_from_payload(data, guild_id, require_name=True)
    if err:
        return jsonify({"success": False, "error": err})
    if not (fields.get("name") or "").strip():
        return jsonify({"success": False, "error": "give the template a name"})
    if "game_type" not in fields:
        return jsonify({"success": False, "error": "pick a game type"})

    # Prefill chain (plan §7): omitted rewards → the category's default
    # preset → the global preset. An EXPLICIT empty list means "no
    # pool" and is never prefilled (D11).
    if "rewards" not in fields:
        async def prefill():
            from utils import minigame_store as store
            await store.ensure_tables()
            cat = await store.get_category(guild_id, category_id)
            rewards = list(cat.get("default_rewards") or []) if cat else []
            if not rewards:
                cfg = await store.get_config(guild_id)
                rewards = list(cfg.get("global_default_rewards") or [])
            return rewards
        fields["rewards"] = run_async(prefill())

    async def create():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.create_template(
            guild_id, category_id, fields["name"], fields["game_type"],
            enabled=fields.get("enabled", True),
            auto_spawn=fields.get("auto_spawn", True),
            embed=fields.get("embed") or {},
            config=fields.get("config") or {},
            channel_id=fields.get("channel_id"),
            rewards=fields.get("rewards"))

    try:
        tpl = run_async(create())
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    log_action(guild_id, f"Created minigame template '{tpl['name']}'",
               "minigames", target_id=tpl["id"], target_name=tpl["name"])
    return jsonify({"success": True, "template": tpl})


@api_bp.route("/minigames/templates/<int:template_id>", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigame_template_api(template_id: int):
    guild_id = get_session_guild_id()

    async def fetch():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.get_template(guild_id, template_id)

    return jsonify({"template": run_async(fetch())})


@api_bp.route("/minigames/templates/<int:template_id>", methods=["PATCH"])
@require_api_permission(LEVEL_ADMIN)
def update_minigame_template_api(template_id: int):
    guild_id = get_session_guild_id()
    data = request.json or {}

    async def run():
        from utils import minigame_store as store
        await store.ensure_tables()
        existing = await store.get_template(guild_id, template_id)
        if existing is None:
            return None, "template not found", None
        # Validate against the EFFECTIVE values (payload wins, stored
        # fills in), so a partial PATCH can't smuggle in a broken
        # game-type/config/embed combination.
        effective_type = data.get("game_type") or existing["game_type"]
        effective_embed = data.get("embed") if "embed" in data else existing.get("embed")
        effective_config = data.get("config") if "config" in data else existing.get("config")
        if "game_type" in data:
            err = _validate_game_config(effective_type, effective_config)
            if err:
                return None, err, None
        if "embed" in data or "game_type" in data:
            err = _validate_embed(effective_embed)
            if err:
                return None, err, None
            err = _validate_colors_image(effective_type, effective_embed)
            if err:
                return None, err, None
        fields, err = _template_fields_from_payload(data, guild_id, require_name=False)
        if err:
            return None, err, None
        # Re-validate config with the EFFECTIVE type when the payload
        # carried a config but not a game_type (the helper above only
        # checks when the payload names both).
        if "config" in data and "game_type" not in data:
            err = _validate_game_config(effective_type, effective_config)
            if err:
                return None, err, None
        return await store.update_template(guild_id, template_id, fields), None, existing

    try:
        tpl, err, _existing = run_async(run())
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    if err:
        return jsonify({"success": False, "error": err})
    if tpl is None:
        return jsonify({"success": False, "error": "template not found"})
    log_action(guild_id, f"Updated minigame template '{tpl['name']}'",
               "minigames", target_id=template_id, target_name=tpl["name"])
    return jsonify({"success": True, "template": tpl})


@api_bp.route("/minigames/templates/<int:template_id>/duplicate",
              methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def duplicate_minigame_template_api(template_id: int):
    guild_id = get_session_guild_id()
    data = request.json or {}
    new_name = (data.get("new_name") or "").strip() or None

    async def dup():
        from utils import minigame_store as store
        await store.ensure_tables()
        return await store.duplicate_template(guild_id, template_id, new_name)

    try:
        tpl = run_async(dup())
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    if tpl is None:
        return jsonify({"success": False, "error": "template not found"})
    log_action(guild_id, f"Duplicated minigame template → '{tpl['name']}'",
               "minigames", target_id=tpl["id"], target_name=tpl["name"])
    return jsonify({"success": True, "template": tpl})


@api_bp.route("/minigames/templates/<int:template_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_minigame_template_api(template_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        from utils import minigame_store as store
        await store.ensure_tables()
        tpl = await store.get_template(guild_id, template_id)
        ok = await store.delete_template(guild_id, template_id)
        return tpl, ok

    tpl, ok = run_async(delete())
    if not ok:
        return jsonify({"success": False, "error": "template not found"})
    # Safe while a run is in progress — the running game keeps its
    # snapshot (plan §14); the row is only what future spawns read.
    log_action(guild_id, f"Deleted minigame template '{tpl['name']}'",
               "minigames", target_id=template_id, target_name=tpl["name"])
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════
# SPAWN (test / manual) — queue-based, plan §9/§16/§19
# ═══════════════════════════════════════════════════════════════════

@api_bp.route("/minigames/templates/<int:template_id>/spawn",
              methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def spawn_minigame_template_api(template_id: int):
    guild_id = get_session_guild_id()
    data = request.json or {}
    mode = (data.get("mode") or "").lower().strip()
    if mode not in ("manual", "test"):
        return jsonify({"success": False,
                        "error": "mode must be 'manual' or 'test'"})

    async def run():
        from utils import minigame_store as store
        await store.ensure_tables()
        tpl = await store.get_template(guild_id, template_id)
        if tpl is None:
            return None, "template not found"
        # D12 (plan §9): test spawn is ALWAYS allowed — it is the
        # build-then-test flow; manual spawn requires the template to
        # be Enabled. Auto-rotation is irrelevant to direct spawns.
        if mode == "manual" and not tpl["enabled"]:
            return None, "this game is disabled — enable it first (or use Test Spawn)"
        return tpl, None

    tpl, err = run_async(run())
    if err:
        return jsonify({"success": False, "error": err})

    # Broken-template guard, mirrored from the bot's preflight (D12 /
    # §16): don't queue a spawn that the engine would refuse.
    cfg = tpl.get("config") or {}
    if tpl["game_type"] in ("math", "colors", "emoji") and not cfg.get("answers"):
        return jsonify({"success": False,
                        "error": "this game has no answers configured — fix its settings first"})

    requester = (session.get("user") or {}).get("username") or str(
        (session.get("user") or {}).get("id") or "?")

    async def enqueue():
        from utils import minigame_store as store
        return await store.create_spawn_request(
            guild_id, template_id, mode, requested_by=requester)

    request_id, queue_err = run_async(enqueue())
    if queue_err:
        return jsonify({"success": False, "error": queue_err})
    log_action(guild_id, f"Queued {mode} spawn: '{tpl['name']}'",
               "minigames", target_id=template_id, target_name=tpl["name"])
    return jsonify({"success": True, "request_id": request_id,
                    "mode": mode, "template_name": tpl["name"]})


@api_bp.route("/minigames/spawn-requests", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigame_spawn_requests_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from utils import minigame_store as store
        await store.ensure_tables()
        reqs = await store.get_in_flight_requests(guild_id)
        if not reqs:
            return []
        async with aiosqlite.connect(DB_PATH) as db:
            names = {}
            for r in reqs:
                cur = await db.execute(
                    "SELECT name FROM minigame_templates "
                    "WHERE id = ? AND guild_id = ?",
                    (r["template_id"], guild_id))
                row = await cur.fetchone()
                names[r["template_id"]] = row[0] if row else None
        for r in reqs:
            r["template_name"] = names.get(r["template_id"])
        return reqs

    return jsonify({"requests": run_async(fetch())})


# ═══════════════════════════════════════════════════════════════════
# HISTORY (extended log — replaces the retired /minigames/log)
# ═══════════════════════════════════════════════════════════════════

@api_bp.route("/minigames/history", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigames_history_api():
    guild_id = get_session_guild_id()
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    category_id = request.args.get("category_id")
    category_id = int(category_id) if category_id not in (None, "") else None

    async def fetch():
        from utils import minigame_store as store
        await store.ensure_tables()
        rows = await store.get_history(guild_id, limit=limit,
                                       category_id=category_id)
        if not rows:
            return rows
        async with aiosqlite.connect(DB_PATH) as db:
            names = {}
            tids = {r.get("template_id") for r in rows if r.get("template_id")}
            if tids:
                cur = await db.execute(
                    "SELECT id, name FROM minigame_templates WHERE id IN "
                    "({})".format(",".join("?" * len(tids))), tuple(tids))
                for tid, name in await cur.fetchall():
                    names[tid] = name
            catnames = {}
            cids = {r.get("category_id") for r in rows if r.get("category_id")}
            if cids:
                cur = await db.execute(
                    "SELECT id, name FROM minigame_categories WHERE id IN "
                    "({})".format(",".join("?" * len(cids))), tuple(cids))
                for cid, name in await cur.fetchall():
                    catnames[cid] = name
        for r in rows:
            # The stored template_name/category_name are SNAPSHOTS taken
            # when the run opened (plan §14) — they survive template /
            # category deletion and renames. Fall back to a live join
            # only for rows written before those columns existed.
            if not r.get("template_name"):
                r["template_name"] = names.get(r.get("template_id"))
            if not r.get("category_name"):
                r["category_name"] = catnames.get(r.get("category_id"))
        return rows

    return jsonify({"history": run_async(fetch())})


# ═══════════════════════════════════════════════════════════════════
# LIVE PREVIEW — engine component rows (plan §12)
#
# The builder's preview must show the EXACT component rows the engine
# posts — so instead of re-implementing them in JS, the builder asks
# the server, and the server asks the ENGINE (the single source of
# truth, utils/minigame_engine.initial_component_rows — a pure,
# discord-free data function). Preview and real message cannot
# diverge because they consume the same JSON.
# ═══════════════════════════════════════════════════════════════════

@api_bp.route("/minigames/preview-rows", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def preview_minigame_rows_api():
    data = request.json or {}
    game_type = data.get("game_type")
    config = data.get("config") or {}
    if not isinstance(config, dict):
        return jsonify({"success": False, "error": "config must be an object"})
    try:
        from utils import minigame_engine as engine
        rows = engine.initial_component_rows(game_type, config)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)})
    return jsonify({"rows": rows, "game_type": game_type})
