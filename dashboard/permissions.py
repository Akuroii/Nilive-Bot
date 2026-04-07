import os
import sys
import aiosqlite
from functools import wraps
from flask import session, redirect, url_for, abort

# Add root to path before any local imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import DB_PATH
from dashboard.utils.async_utils import run_async

# Permission levels (copied from utils/permissions.py to avoid path issues)
LEVEL_OWNER     = "owner"
LEVEL_ADMIN     = "admin"
LEVEL_MODERATOR = "moderator"

LEVEL_RANK = {
    LEVEL_OWNER:     3,
    LEVEL_ADMIN:     2,
    LEVEL_MODERATOR: 1,
}

PAGE_PERMISSIONS = {
    "overview":           LEVEL_MODERATOR,
    "members_view":       LEVEL_MODERATOR,
    "members_edit":       LEVEL_ADMIN,
    "members_delete":     LEVEL_OWNER,
    "audit_log":          LEVEL_ADMIN,
    "moderation_view":    LEVEL_MODERATOR,
    "moderation_action":  LEVEL_MODERATOR,
    "moderation_edit":    LEVEL_ADMIN,
    "moderation_delete":  LEVEL_OWNER,
    "tickets":            LEVEL_MODERATOR,
    "embedbuilder":       LEVEL_ADMIN,
    "reactionroles":      LEVEL_ADMIN,
    "triggers":           LEVEL_ADMIN,
    "customcommands":     LEVEL_ADMIN,
    "mvp":                LEVEL_ADMIN,
    "leveling":           LEVEL_ADMIN,
    "economy":            LEVEL_ADMIN,
    "shop":               LEVEL_ADMIN,
    "events":             LEVEL_ADMIN,
    "leaderboards":       LEVEL_MODERATOR,
    "general_settings":   LEVEL_OWNER,
    "welcome":            LEVEL_ADMIN,
    "boost":              LEVEL_ADMIN,
    "announcements":      LEVEL_ADMIN,
    "commands":           LEVEL_OWNER,
    "dashboard_access":   LEVEL_OWNER,
}


def get_required_level(page: str) -> str:
    return PAGE_PERMISSIONS.get(page, LEVEL_OWNER)


def user_can_access_page(user_level: str, page: str) -> bool:
    required = get_required_level(page)
    return LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK.get(required, 3)


async def _get_permission_level(guild_id: int, user_id: int) -> str | None:
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
