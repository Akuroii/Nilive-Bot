from flask import jsonify, request
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Missions (Phase 6, built ahead of the Trade-live-verification gate
# per Dark's explicit override — see utils/mission_engine.py header) ───
#
# Same shape as dashboard/api/minigames.py: reuse
# utils.mission_engine's own ensure_tables()/get_definitions() rather
# than re-declaring schema/query logic here.


@api_bp.route("/missions/list", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_missions_list_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from utils.mission_engine import ensure_tables, get_definitions
        await ensure_tables()
        return await get_definitions(guild_id, enabled_only=False)

    return jsonify({"missions": run_async(fetch())})


@api_bp.route("/missions/definition", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_mission_definition_api():
    from utils.mission_engine import VALID_TYPES, VALID_PERIODS, VALID_REWARD_TYPES
    guild_id = get_session_guild_id()
    data     = request.json or {}

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name is required"})

    mtype = (data.get("type") or "").lower().strip()
    if mtype not in VALID_TYPES:
        return jsonify({"success": False, "error": f"type must be one of: {', '.join(VALID_TYPES)}"})

    period = (data.get("period") or "daily").lower().strip()
    if period not in VALID_PERIODS:
        return jsonify({"success": False, "error": f"period must be one of: {', '.join(VALID_PERIODS)}"})

    reward_type = data.get("reward_type")
    if reward_type not in VALID_REWARD_TYPES:
        return jsonify({"success": False,
                        "error": f"reward_type must be one of: {', '.join(VALID_REWARD_TYPES)}"})

    reward_value = data.get("reward_value")
    if not reward_value:
        return jsonify({"success": False, "error": "reward_value is required"})

    try:
        target = int(data.get("target"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "target must be a number"})
    if target <= 0:
        return jsonify({"success": False, "error": "target must be positive"})

    duration_hours = data.get("reward_duration_hours")
    try:
        duration_hours = int(duration_hours) if duration_hours else None
    except (TypeError, ValueError):
        duration_hours = None

    async def save():
        import aiosqlite
        from database import DB_PATH
        from utils.mission_engine import ensure_tables
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO missions_definitions
                    (guild_id, name, description, type, target, period,
                     reward_type, reward_value, reward_duration_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, name, data.get("description"), mtype, target,
                  period, reward_type, str(reward_value), duration_hours))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added mission: {name} ({mtype})", "missions")
    return jsonify({"success": True})


@api_bp.route("/missions/definition/<int:mission_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_mission_definition_api(mission_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        import aiosqlite
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM missions_definitions WHERE id=? AND guild_id=?",
                (mission_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Removed mission #{mission_id}", "missions")
    return jsonify({"success": True})


@api_bp.route("/missions/definition/<int:mission_id>/toggle", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def toggle_mission_definition_api(mission_id: int):
    guild_id = get_session_guild_id()
    enabled  = int(bool((request.json or {}).get("enabled", True)))

    async def toggle():
        import aiosqlite
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE missions_definitions SET enabled=? WHERE id=? AND guild_id=?",
                (enabled, mission_id, guild_id))
            await db.commit()

    run_async(toggle())
    return jsonify({"success": True})


@api_bp.route("/missions/completions", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_mission_completions_api():
    guild_id = get_session_guild_id()
    limit    = min(int(request.args.get("limit", 50)), 200)

    async def fetch():
        from utils.mission_engine import ensure_tables, get_recent_completions
        await ensure_tables()
        return await get_recent_completions(guild_id, limit=limit)

    rows = run_async(fetch())

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
