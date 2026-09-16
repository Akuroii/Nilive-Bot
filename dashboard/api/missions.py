from flask import jsonify, request
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Missions (Phase 6, built ahead of the Trade-live-verification gate
# per Dark's explicit override — see utils/mission_engine.py header) ───
#
# Same shape as dashboard/api/minigames.py: reuse utils.mission_engine
# rather than re-declaring schema/query logic here. Since v2 that also
# includes create_definition()/delete_definition() — validation and
# writes live in the engine once, shared with the /mission_create
# slash command, so the two surfaces can't drift apart.


@api_bp.route("/missions/list", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_missions_list_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from utils.mission_engine import ensure_tables, get_definitions
        await ensure_tables()
        return await get_definitions(guild_id, enabled_only=False)

    # Snowflake safety: channel_id travels to the client as a STRING —
    # channel IDs exceed JS's 2^53 safe-integer range, and a JSON
    # number would silently corrupt the trailing digits (breaking both
    # the table's channel-name lookup and the map key match against
    # /api/guild/channels' string ids). Same discipline as the user_id
    # stringification in /api/missions/completions below.
    missions = run_async(fetch())
    for m in missions:
        if m.get("channel_id") is not None:
            m["channel_id"] = str(m["channel_id"])
    return jsonify({"missions": missions})


@api_bp.route("/missions/definition", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_mission_definition_api():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    async def save():
        from utils.mission_engine import ensure_tables, create_definition
        await ensure_tables()
        # All validation (type/period/reward shape, target, channel id)
        # lives in create_definition — a ValueError here is a member-
        # facing message, mapped to a 400 below rather than a 500.
        return await create_definition(
            guild_id,
            name=data.get("name"),
            mtype=data.get("type"),
            target=data.get("target"),
            period=data.get("period") or "daily",
            reward_type=data.get("reward_type"),
            reward_value=data.get("reward_value"),
            description=data.get("description"),
            reward_duration_hours=data.get("reward_duration_hours"),
            channel_id=data.get("channel_id"),
        )

    try:
        run_async(save())
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    log_action(guild_id, f"Added mission: {(data.get('name') or '').strip()} "
                         f"({(data.get('type') or '').strip()})", "missions")
    return jsonify({"success": True})


@api_bp.route("/missions/definition/<int:mission_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_mission_definition_api(mission_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        from utils.mission_engine import ensure_tables, delete_definition
        await ensure_tables()
        return await delete_definition(guild_id, mission_id)

    if not run_async(delete()):
        return jsonify({"success": False,
                        "error": "Mission not found"}), 404
    log_action(guild_id, f"Removed mission #{mission_id}", "missions")
    return jsonify({"success": True})


@api_bp.route("/missions/definition/<int:mission_id>/toggle", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def toggle_mission_definition_api(mission_id: int):
    guild_id = get_session_guild_id()
    enabled  = bool((request.json or {}).get("enabled", True))

    async def toggle():
        from utils.mission_engine import ensure_tables, set_definition_enabled
        await ensure_tables()
        return await set_definition_enabled(guild_id, mission_id, enabled)

    if not run_async(toggle()):
        return jsonify({"success": False,
                        "error": "Mission not found"}), 404
    return jsonify({"success": True})


@api_bp.route("/missions/completions", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_mission_completions_api():
    guild_id = get_session_guild_id()
    # Guarded parse: ?limit=abc used to raise a bare 500.
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    async def fetch():
        from utils.mission_engine import ensure_tables, get_recent_completions
        await ensure_tables()
        return await get_recent_completions(guild_id, limit=limit)

    rows = run_async(fetch())

    # Snowflake safety: user IDs travel to the client as STRINGS — same
    # reason as /api/trade/history (JS numbers lose precision past 2^53,
    # which would corrupt trailing digits and break loadMissionLog()'s
    # userMap lookup).
    rows = [{**c, "user_id": str(c["user_id"])} for c in rows]

    # Presentation timestamp ('2026-09-14 · 12:51') built server-side
    # from the same stored UTC instant — storage stays untouched, this
    # is purely how it's shown (spec: don't migrate data for display).
    from utils.formatters import format_timestamp
    rows = [{**c,
             "completed_at_display":
                 format_timestamp(c["completed_at"], "%Y-%m-%d · %H:%M")
                 if c["completed_at"] else None}
            for c in rows]

    # dark-fixes pass #18 (username resolver rollout): one batched
    # resolve_users() call covering every user on the page. The map
    # travels in the JSON payload so loadMissionLog() renders the user
    # cell client-side via dashboard.js's userIdentityHtml() — same
    # pattern as /api/trade/history.
    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = {c["user_id"] for c in rows}
        if not ids:
            return {}
        return await resolve_users(guild_id, list(ids))

    user_map = run_async(resolve())
    return jsonify({"completions": rows, "user_map": user_map})
