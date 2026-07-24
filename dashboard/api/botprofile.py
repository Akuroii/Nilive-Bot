from flask import jsonify, request
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Bot Profile (per-server nickname + branding icon) ───────────────────
#
# Nickname changes are applied immediately via a direct Discord REST call
# using the bot token (utils.bot_profile.apply_nickname_via_rest) — same
# "Flask process talks to Discord's API directly with the bot token"
# pattern dashboard/api/core.py already uses for role/channel lookups
# and dashboard/auth.py uses for bot_is_in_guild()/get_bot_invite_url().
# No dependency on the live bot process being reachable from here.
#
# avatar_url is stored as branding metadata ONLY — see utils/bot_profile.py
# and cogs/botprofile.py for why Discord doesn't support a true
# per-server bot avatar via any API.


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

    if nickname and len(nickname) > 32:
        return jsonify({"success": False,
                        "error": "Nickname must be 32 characters or fewer (Discord's limit)"})

    async def save():
        from utils.bot_profile import save_guild_bot_profile
        await save_guild_bot_profile(guild_id, nickname, avatar_url)

    run_async(save())

    from utils.bot_profile import apply_nickname_via_rest
    applied, error = apply_nickname_via_rest(guild_id, nickname)

    log_action(guild_id,
               f"Updated bot profile (nickname={nickname or 'default'})",
               "botprofile")

    if not applied:
        return jsonify({
            "success": True,
            "nickname_applied": False,
            "warning": f"Saved, but couldn't apply the nickname live: {error}",
        })
    return jsonify({"success": True, "nickname_applied": True})


@api_bp.route("/botprofile/reset", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def reset_botprofile():
    guild_id = get_session_guild_id()

    async def save():
        from utils.bot_profile import save_guild_bot_profile
        await save_guild_bot_profile(guild_id, None, None)

    run_async(save())

    from utils.bot_profile import apply_nickname_via_rest
    applied, error = apply_nickname_via_rest(guild_id, None)

    log_action(guild_id, "Reset bot profile to default", "botprofile")

    if not applied:
        return jsonify({
            "success": True,
            "nickname_applied": False,
            "warning": f"Reset saved, but couldn't clear the live nickname: {error}",
        })
    return jsonify({"success": True, "nickname_applied": True})
