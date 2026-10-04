from flask import jsonify, request
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Bot Profile (real per-server nickname + avatar + banner + bio) ─────────
#
# All four fields go in ONE call to Discord's "Modify Current Member"
# endpoint (PATCH /guilds/{id}/members/@me), which accepts
# nick/avatar/banner/bio for whichever token calls it — bot tokens
# included. avatar/banner must be base64 data URIs, so
# utils/bot_profile.py downloads whatever URL is pasted here and
# re-encodes it before sending.
#
# Applied directly from this Flask process via the bot token — same
# pattern dashboard/api/core.py already uses for role/channel lookups
# — so there's no dependency on the bot's gateway connection being up.


@api_bp.route("/botprofile/config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_botprofile_config():
    guild_id = get_session_guild_id()

    async def fetch():
        from utils.bot_profile import get_guild_bot_profile
        return await get_guild_bot_profile(guild_id)

    stored = run_async(fetch())

    from utils.bot_profile import get_live_bot_member
    live = get_live_bot_member(guild_id)

    return jsonify({"stored": stored, "live": live})


@api_bp.route("/botprofile/config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_botprofile_config():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    nickname   = (data.get("nickname") or "").strip() or None
    avatar_url = (data.get("avatar_url") or "").strip() or None
    banner_url = (data.get("banner_url") or "").strip() or None
    bio        = (data.get("bio") or "").strip() or None

    if nickname and len(nickname) > 32:
        return jsonify({"success": False,
                        "error": "Nickname must be 32 characters or fewer (Discord's limit)"})
    if bio and len(bio) > 190:
        return jsonify({"success": False,
                        "error": "Bio must be 190 characters or fewer"})

    from utils.bot_profile import apply_bot_profile_via_rest, save_guild_bot_profile
    result = apply_bot_profile_via_rest(guild_id, nickname, avatar_url, banner_url, bio)

    async def save():
        await save_guild_bot_profile(guild_id, nickname, avatar_url, banner_url, bio)

    run_async(save())

    log_action(guild_id,
               f"Updated bot profile (nickname={nickname or 'default'})",
               "botprofile")

    return jsonify({
        "success": result["success"],
        "applied": result.get("applied", []),
        "errors": result.get("errors", {}),
    })


@api_bp.route("/botprofile/reset", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def reset_botprofile():
    guild_id = get_session_guild_id()

    from utils.bot_profile import apply_bot_profile_via_rest, save_guild_bot_profile
    result = apply_bot_profile_via_rest(guild_id, None, None, None, None)

    async def save():
        await save_guild_bot_profile(guild_id, None, None, None, None)

    run_async(save())
    log_action(guild_id, "Reset bot profile to default", "botprofile")

    return jsonify({
        "success": result["success"],
        "applied": result.get("applied", []),
        "errors": result.get("errors", {}),
    })
