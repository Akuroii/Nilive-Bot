import aiosqlite
from flask import jsonify, request

from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission,
    LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Server Tag Missions (Feature 1) ─────────────────────────────────────────
# Config-only CRUD -- posting the mission, restoring its persistent
# view, and resolving it at ends_at all happen in cogs/tagmissions.py's
# mission_poll task, not here.

VALID_REWARD_TYPES = {"coins", "diamonds", "xp", "role", "temp_role"}


@api_bp.route("/tagmissions/list", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def api_tagmissions_list():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, title, reward_type, reward_amount, reward_role_id,
                       channel_id, starts_at, ends_at, status
                FROM tag_missions WHERE guild_id = ?
                ORDER BY starts_at DESC
            """, (guild_id,))
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            missions = [dict(zip(cols, r)) for r in rows]

            for m in missions:
                p_cursor = await db.execute("""
                    SELECT
                        COUNT(*),
                        SUM(CASE WHEN outcome='rewarded' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN outcome='removed_tag' THEN 1 ELSE 0 END)
                    FROM tag_mission_participants WHERE mission_id = ?
                """, (m["id"],))
                total, rewarded, removed = await p_cursor.fetchone()
                m["participant_count"] = total or 0
                m["rewarded_count"] = rewarded or 0
                m["removed_tag_count"] = removed or 0

        return missions

    return jsonify(run_async(fetch()))


@api_bp.route("/tagmissions/save", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def api_tagmissions_save():
    guild_id = get_session_guild_id()
    data = request.json or {}

    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"success": False, "error": "Title is required."}), 400

    starts_at = data.get("starts_at")
    ends_at = data.get("ends_at")
    if not starts_at or not ends_at:
        return jsonify({"success": False,
                         "error": "Start and end time are required."}), 400
    if ends_at <= starts_at:
        return jsonify({"success": False,
                         "error": "End time must be after start time."}), 400

    channel_id = data.get("channel_id")
    if not channel_id:
        return jsonify({"success": False,
                         "error": "A channel to post the mission in is required."}), 400

    reward_type = data.get("reward_type", "coins")
    if reward_type not in VALID_REWARD_TYPES:
        return jsonify({"success": False,
                         "error": f"Unknown reward type: {reward_type}"}), 400

    if reward_type in ("role", "temp_role") and not data.get("reward_role_id"):
        return jsonify({"success": False,
                         "error": "This reward type requires a role."}), 400
    if reward_type == "temp_role" and not data.get("reward_duration_hours"):
        return jsonify({"success": False,
                         "error": "Temporary roles need a duration in hours."}), 400
    if reward_type in ("coins", "diamonds", "xp") and not data.get("reward_amount"):
        return jsonify({"success": False,
                         "error": "This reward type requires an amount."}), 400

    row_id = data.get("id")

    message_fields = {}
    for field in ("confirm_message", "not_wearing_message", "success_message",
                  "failure_message", "already_message"):
        if data.get(field):
            message_fields[field] = data[field]

    async def existing_status():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT status FROM tag_missions WHERE id=? AND guild_id=?",
                (row_id, guild_id))
            row = await cursor.fetchone()
            return row[0] if row else None

    if row_id:
        status = run_async(existing_status())
        if status is None:
            return jsonify({"success": False, "error": "Mission not found."}), 404
        if status != "scheduled":
            return jsonify({"success": False,
                             "error": ("Only a mission that hasn't started yet "
                                       "can be edited. Cancel it first if you "
                                       "need to change an active one.")}), 400

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            if row_id:
                set_clauses = ["title=?", "reward_type=?", "reward_amount=?",
                                "reward_role_id=?", "reward_duration_hours=?",
                                "channel_id=?", "starts_at=?", "ends_at=?"]
                params = [title, reward_type,
                          str(data.get("reward_amount")) if data.get("reward_amount") else None,
                          data.get("reward_role_id") or None,
                          data.get("reward_duration_hours") or None,
                          channel_id, starts_at, ends_at]
                for field, value in message_fields.items():
                    set_clauses.append(f"{field}=?")
                    params.append(value)
                params.extend([row_id, guild_id])
                await db.execute(
                    f"UPDATE tag_missions SET {', '.join(set_clauses)} "
                    f"WHERE id=? AND guild_id=?",
                    params)
            else:
                cols = ["guild_id", "title", "reward_type", "reward_amount",
                        "reward_role_id", "reward_duration_hours",
                        "channel_id", "starts_at", "ends_at"]
                vals = [guild_id, title, reward_type,
                        str(data.get("reward_amount")) if data.get("reward_amount") else None,
                        data.get("reward_role_id") or None,
                        data.get("reward_duration_hours") or None,
                        channel_id, starts_at, ends_at]
                for field, value in message_fields.items():
                    cols.append(field)
                    vals.append(value)
                placeholders = ", ".join("?" for _ in cols)
                await db.execute(
                    f"INSERT INTO tag_missions ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    vals)
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved tag mission: {title}", "tagmissions")
    return jsonify({"success": True})


@api_bp.route("/tagmissions/<int:row_id>/cancel", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def api_tagmissions_cancel(row_id: int):
    guild_id = get_session_guild_id()

    async def cancel():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT status FROM tag_missions WHERE id=? AND guild_id=?",
                (row_id, guild_id))
            row = await cursor.fetchone()
            if not row:
                return "not_found"
            if row[0] not in ("scheduled", "active"):
                return "bad_status"
            await db.execute(
                "UPDATE tag_missions SET status='cancelled' WHERE id=? AND guild_id=?",
                (row_id, guild_id))
            await db.commit()
            return "ok"

    result = run_async(cancel())
    if result == "not_found":
        return jsonify({"success": False, "error": "Mission not found."}), 404
    if result == "bad_status":
        return jsonify({"success": False,
                         "error": "That mission is already finished."}), 400

    log_action(guild_id, f"Cancelled tag mission {row_id}", "tagmissions")
    return jsonify({"success": True})


@api_bp.route("/tagmissions/<int:row_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def api_tagmissions_delete(row_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT status FROM tag_missions WHERE id=? AND guild_id=?",
                (row_id, guild_id))
            row = await cursor.fetchone()
            if not row:
                return "not_found"
            if row[0] == "active":
                return "bad_status"
            await db.execute(
                "DELETE FROM tag_mission_participants WHERE mission_id=?",
                (row_id,))
            await db.execute(
                "DELETE FROM tag_missions WHERE id=? AND guild_id=?",
                (row_id, guild_id))
            await db.commit()
            return "ok"

    result = run_async(delete())
    if result == "not_found":
        return jsonify({"success": False, "error": "Mission not found."}), 404
    if result == "bad_status":
        return jsonify({"success": False,
                         "error": ("Can't delete a mission that's currently "
                                   "active — cancel it first.")}), 400

    return jsonify({"success": True})
