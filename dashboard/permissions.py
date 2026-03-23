import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
import asyncio
from functools import wraps
from flask import session, redirect, url_for, abort
from database import DB_PATH
from utils.permissions import (
    LEVEL_OWNER, LEVEL_ADMIN, LEVEL_MODERATOR,
    LEVEL_RANK, user_can_access_page, get_required_level
)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
                     target_id: int = None, target_name: str = None,
                     ip: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO audit_log
            (guild_id, user_id, user_display_name, target_id, target_name,
             action, details, page, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, display_name, target_id, target_name,
              action, details, page, ip))
        await db.commit()


def log_action(guild_id: int, action: str, page: str,
               details: str = None, target_id: int = None,
               target_name: str = None):
    user         = session.get("user", {})
    user_id      = int(user.get("id", 0))
    display_name = user.get("username", "Unknown")
    run_async(_log_audit(guild_id, user_id, display_name,
                         action, page, details, target_id, target_name, None))


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


def require_owner(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from dashboard.auth import is_session_valid
        if not is_session_valid():
            return redirect(url_for("login"))
        user     = session.get("user", {})
        user_id  = int(user.get("id", 0))
        guild_id = get_session_guild_id()
        if not guild_id:
            return redirect(url_for("server_select"))
        level = run_async(_get_permission_level(guild_id, user_id))
        if level != LEVEL_OWNER:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from dashboard.auth import is_session_valid
        if not is_session_valid():
            return redirect(url_for("login"))
        user     = session.get("user", {})
        user_id  = int(user.get("id", 0))
        guild_id = get_session_guild_id()
        if not guild_id:
            return redirect(url_for("server_select"))
        level = run_async(_get_permission_level(guild_id, user_id))
        if LEVEL_RANK.get(level, 0) < LEVEL_RANK[LEVEL_ADMIN]:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def require_moderator(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from dashboard.auth import is_session_valid
        if not is_session_valid():
            return redirect(url_for("login"))
        user     = session.get("user", {})
        user_id  = int(user.get("id", 0))
        guild_id = get_session_guild_id()
        if not guild_id:
            return redirect(url_for("server_select"))
        level = run_async(_get_permission_level(guild_id, user_id))
        if LEVEL_RANK.get(level, 0) < LEVEL_RANK[LEVEL_MODERATOR]:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def get_current_user_context() -> dict:
    user       = session.get("user", {})
    user_id    = int(user.get("id", 0))
    guild_id   = get_session_guild_id()
    user_level = session.get("user_level")
    if not user_level and guild_id:
        user_level = run_async(_get_permission_level(guild_id, user_id))
    return {
        "user":         user,
        "user_level":   user_level or "",
        "guild_id":     guild_id,
        "is_owner":     user_level == LEVEL_OWNER,
        "is_admin":     LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK[LEVEL_ADMIN],
        "is_moderator": LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK[LEVEL_MODERATOR],
    }
