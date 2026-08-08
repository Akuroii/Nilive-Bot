import aiosqlite
from flask import jsonify, request

from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission,
    LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Server Tag Partners (Feature 2) ─────────────────────────────────────────
# Config-only CRUD -- the actual join-time check lives in
# cogs/tagpartners.py's on_member_join listener, not here.

VALID_REWARD_TYPES = {"coins", "diamonds", "xp", "role"}


@api_bp.route("/tagpartners/list", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def api_tagpartners_list():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, partner_guild_id, partner_label, reward_type,
                       reward_amount, reward_role_id, welcome_message, enabled
                FROM tag_partner_rewards WHERE guild_id = ?
                ORDER BY created_at DESC
            """, (guild_id,))
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]

    return jsonify(run_async(fetch()))


@api_bp.route("/tagpartners/save", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def api_tagpartners_save():
    guild_id = get_session_guild_id()
    data = request.json or {}

    try:
        partner_guild_id = int(data.get("partner_guild_id", 0))
    except (TypeError, ValueError):
        return jsonify({"success": False,
                         "error": "Partner server ID must be a number."}), 400

    if not partner_guild_id:
        return jsonify({"success": False,
                         "error": "Partner server ID is required."}), 400

    if partner_guild_id == guild_id:
        return jsonify({"success": False,
                         "error": ("Partner server can't be this server — "
                                   "that's the Tag Mission feature, not a "
                                   "partner reward.")}), 400

    reward_type = data.get("reward_type", "coins")
    if reward_type not in VALID_REWARD_TYPES:
        return jsonify({"success": False,
                         "error": f"Unknown reward type: {reward_type}"}), 400

    if reward_type in ("role",) and not data.get("reward_role_id"):
        return jsonify({"success": False,
                         "error": "This reward type requires a role."}), 400
    if reward_type in ("coins", "diamonds", "xp") and not data.get("reward_amount"):
        return jsonify({"success": False,
                         "error": "This reward type requires an amount."}), 400

    row_id = data.get("id")
    params = (
        data.get("partner_label") or None,
        reward_type,
        str(data.get("reward_amount")) if data.get("reward_amount") else None,
        data.get("reward_role_id") or None,
        data.get("welcome_message") or None,
        int(bool(data.get("enabled", True))),
    )

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            if row_id:
                await db.execute("""
                    UPDATE tag_partner_rewards SET
                        partner_guild_id=?, partner_label=?, reward_type=?,
                        reward_amount=?, reward_role_id=?, welcome_message=?,
                        enabled=?
                    WHERE id=? AND guild_id=?
                """, (partner_guild_id, *params, row_id, guild_id))
            else:
                await db.execute("""
                    INSERT INTO tag_partner_rewards
                        (guild_id, partner_guild_id, partner_label, reward_type,
                         reward_amount, reward_role_id, welcome_message, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (guild_id, partner_guild_id, *params))
            await db.commit()

    try:
        run_async(save())
    except aiosqlite.IntegrityError:
        return jsonify({"success": False,
                         "error": "A reward for that partner server already exists."}), 400

    log_action(guild_id, f"Saved tag partner reward: {partner_guild_id}", "tagpartners")
    return jsonify({"success": True})


@api_bp.route("/tagpartners/<int:row_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def api_tagpartners_delete(row_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM tag_partner_rewards WHERE id=? AND guild_id=?",
                (row_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})
