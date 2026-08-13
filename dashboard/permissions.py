import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from functools import wraps
from flask import session, redirect, url_for, abort, jsonify
from database import DB_PATH, OWNER_DISCORD_ID
from dashboard.utils.async_utils import run_async
from utils.permissions import (
    LEVEL_OWNER, LEVEL_ADMIN, LEVEL_MODERATOR,
    LEVEL_RANK, user_can_access_page, get_required_level,
)


async def _get_permission_level(guild_id: int, user_id: int) -> str | None:
    # Guild-blind developer bypass — checked before touching
    # dashboard_users at all, so the trusted developer never needs a
    # row (and therefore never appears in Current Access) in any
    # guild. See is_trusted_super_admin() below for the trust set.
    if is_trusted_super_admin(user_id):
        return LEVEL_OWNER

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT permission_level FROM dashboard_users
            WHERE guild_id = ? AND user_id = ? AND enabled = 1
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else None


async def _log_audit(guild_id: int, user_id: int, display_name: str,
                     action: str, page: str, details: str = None,
                     target_id: int = None, target_name: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO audit_log
            (guild_id, user_id, user_display_name, target_id, target_name,
             action, details, page, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, display_name, target_id, target_name,
              action, details, page, None))
        await db.commit()


def log_action(guild_id: int, action: str, page: str,
               details: str = None, target_id: int = None,
               target_name: str = None):
    user         = session.get("user", {})
    user_id      = int(user.get("id", 0))
    display_name = user.get("username", "Unknown")
    run_async(_log_audit(guild_id, user_id, display_name,
                         action, page, details, target_id, target_name))


def get_session_guild_id() -> int | None:
    return session.get("guild_id")


def set_session_guild(guild_id: int):
    session["guild_id"] = guild_id


def require_page(page_name: str):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            from dashboard.auth import is_session_valid, refresh_session_if_needed
            if not is_session_valid():
                return redirect(url_for("login"))
            refresh_session_if_needed()
            user     = session.get("user", {})
            user_id  = int(user.get("id", 0))
            guild_id = get_session_guild_id()
            if not guild_id:
                return redirect(url_for("server_select"))
            user_level = run_async(_get_permission_level(guild_id, user_id))
            if not user_level:
                abort(403)
            if not user_can_access_page(user_level, page_name):
                abort(403)
            session["user_level"] = user_level
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_api_permission(min_level: str):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            from dashboard.auth import is_session_valid, refresh_session_if_needed
            if not is_session_valid():
                return jsonify({"success": False, "error": "Not authenticated"}), 401
            refresh_session_if_needed()
            user     = session.get("user", {})
            user_id  = int(user.get("id", 0))
            guild_id = get_session_guild_id()
            if not guild_id:
                return jsonify({"success": False, "error": "No server selected"}), 400
            user_level = run_async(_get_permission_level(guild_id, user_id))
            if not user_level:
                return jsonify({"success": False, "error": "Forbidden"}), 403
            if LEVEL_RANK.get(user_level, 0) < LEVEL_RANK.get(min_level, 0):
                return jsonify({
                    "success": False,
                    "error": f"This action requires {min_level} access or higher.",
                }), 403
            session["user_level"] = user_level
            return f(*args, **kwargs)
        return decorated
    return decorator


def get_current_user_context() -> dict:
    user       = session.get("user", {})
    user_id    = int(user.get("id", 0))
    guild_id   = get_session_guild_id()
    guild_name = session.get("guild_name", "")
    user_level = session.get("user_level")
    if not user_level and guild_id:
        user_level = run_async(_get_permission_level(guild_id, user_id))
    return {
        "user":         user,
        "user_level":   user_level or "",
        "guild_id":     guild_id,
        "guild_name":   guild_name,
        "is_owner":     user_level == LEVEL_OWNER,
        "is_admin":     LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK[LEVEL_ADMIN],
        "is_moderator": LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK[LEVEL_MODERATOR],
    }


def _trusted_backup_user_ids() -> set[int]:
    """
    backup_log/BACKUP_DIR are bot-wide — one backup snapshots the
    ENTIRE database, every guild at once — but require_page("backups")
    / require_api_permission(LEVEL_OWNER) alone only check LEVEL_OWNER
    *within whichever guild happens to be selected in the current
    session*. A LEVEL_OWNER in ANY one guild could otherwise read AND
    trigger backups covering every OTHER guild's data too.

    This is the fix: a check that ignores guild_id entirely.
    OWNER_DISCORD_ID (database.py — the real bot owner, already used
    for the Discord-side /backup_now and /backup_list owner-only
    commands) is always trusted. BACKUP_TRUSTED_USER_IDS (env var,
    comma-separated Discord IDs) can add more without a code change.
    Deliberately scoped to backups only — every other LEVEL_OWNER page
    (health, commands, general_settings, dashboard_access) keeps its
    existing guild-scoped-only check, unchanged. The same latent gap
    still applies to those pages; out of scope here.
    """
    ids = {OWNER_DISCORD_ID}
    extra = os.getenv("BACKUP_TRUSTED_USER_IDS", "")
    for part in extra.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def require_bot_owner(f):
    """Page-route variant — stacks on top of require_page("backups"), which
    already handled login/guild-selection/guild-scoped-LEVEL_OWNER before
    this ever runs. Renders the existing 403 page on failure, same as
    require_page itself."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user", {})
        try:
            user_id = int(user.get("id", 0))
        except (TypeError, ValueError):
            user_id = 0
        if user_id not in _trusted_backup_user_ids():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def require_bot_owner_api(f):
    """API-route variant — stacks on top of require_api_permission(LEVEL_OWNER).
    Returns JSON, same shape as require_api_permission's own 403s."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user", {})
        try:
            user_id = int(user.get("id", 0))
        except (TypeError, ValueError):
            user_id = 0
        if user_id not in _trusted_backup_user_ids():
            return jsonify({
                "success": False,
                "error": "This action is restricted to the bot owner.",
            }), 403
        return f(*args, **kwargs)
    return decorated


def _trusted_super_admin_user_ids() -> set[int]:
    """
    Guild-blind trust set for the developer bypass — grants full
    LEVEL_OWNER access in every guild with no dashboard_users row
    anywhere, so the developer never shows up in Current Access.
    Same shape as _trusted_backup_user_ids() above, kept as its own
    function since backup-trust and full-dashboard-trust are
    different privilege scopes that won't always be the same people.
    OWNER_DISCORD_ID (database.py — Dark's real Discord ID) is always
    trusted. SUPER_ADMIN_USER_IDS (env var, comma-separated) can add
    more without a code change.
    """
    ids = {OWNER_DISCORD_ID}
    extra = os.getenv("SUPER_ADMIN_USER_IDS", "")
    for part in extra.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_trusted_super_admin(user_id: int) -> bool:
    """
    Public check for callers outside this module (e.g.
    dashboard/app.py's select_guild(), which needs to know "is this
    the developer bypass account" before it has a guild_id to check
    permissions against yet). _get_permission_level() above is the
    other consumer of this trust set.
    """
    return user_id in _trusted_super_admin_user_ids()
