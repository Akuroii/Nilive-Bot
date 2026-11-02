# Sync of dashboard/auth.py
import os
import time
import secrets
import requests
from flask import session, redirect, url_for
import aiosqlite
from database import DB_PATH
from dashboard.utils.async_utils import run_async

DISCORD_API   = "https://discord.com/api/v10"
CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI")

SESSION_DURATION_DEFAULT  = 60 * 60 * 24
SESSION_DURATION_REMEMBER = 60 * 60 * 24 * 7

# Phase 0 Extension — Server Permission Gating (Sapphire-style)
DISCORD_PERMISSION_ADMINISTRATOR = 0x8
DISCORD_PERMISSION_MANAGE_GUILD  = 0x20


class BotMembershipStatus:
    INSTALLED           = "INSTALLED"            # HTTP 200: Confirmed member
    CONFIRMED_NO_ACCESS = "CONFIRMED_NO_ACCESS"  # Explicit Discord 10004 or 50001
    INDETERMINATE       = "INDETERMINATE"        # Rate limits, timeouts, 401, 5xx, or network drops


_DEFAULT_BOT_PERMISSIONS = (
    0x10 | 0x2 | 0x4 | 0x400 | 0x800 | 0x2000 | 0x4000 | 0x8000
    | 0x10000 | 0x40 | 0x10000000 | 0x10000000000
)
BOT_INVITE_PERMISSIONS = os.getenv(
    "BOT_INVITE_PERMISSIONS", str(_DEFAULT_BOT_PERMISSIONS))


def get_discord_oauth_url(remember: bool = False) -> str:
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["oauth_remember"] = bool(remember)
    return (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify+guilds"
        f"&state={state}"
    )


def verify_oauth_state(returned_state: str | None) -> bool:
    expected = session.pop("oauth_state", None)
    if not expected or not returned_state:
        return False
    return secrets.compare_digest(expected, returned_state)


def consume_oauth_remember() -> bool:
    return bool(session.pop("oauth_remember", False))


def exchange_code(code: str) -> dict | None:
    r = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    return r.json() if r.status_code == 200 else None


def fetch_discord_user(access_token: str) -> dict | None:
    r = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    return r.json() if r.status_code == 200 else None


def fetch_discord_guilds(access_token: str) -> list:
    r = requests.get(
        f"{DISCORD_API}/users/@me/guilds",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    return r.json() if r.status_code == 200 else []


def guild_permissions_can_manage_server(permissions) -> bool:
    """
    Checks if Discord permissions include ADMINISTRATOR (0x8) or
    MANAGE_GUILD (0x20). Used exclusively for server discovery and
    bot invitation eligibility in the server picker.
    """
    try:
        perm_int = int(permissions)
    except (TypeError, ValueError):
        return False
    return bool(perm_int & (DISCORD_PERMISSION_ADMINISTRATOR | DISCORD_PERMISSION_MANAGE_GUILD))


def guild_permissions_include_admin(permissions) -> bool:
    """
    Legacy helper maintained for backward compatibility. Checks only
    ADMINISTRATOR (0x8).
    """
    try:
        perm_int = int(permissions)
    except (TypeError, ValueError):
        return False
    return bool(perm_int & DISCORD_PERMISSION_ADMINISTRATOR)


def fetch_discord_bot_guilds() -> set[int]:
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return set()
    guild_ids: set[int] = set()
    headers = {"Authorization": f"Bot {bot_token}"}
    params  = {"limit": 200}
    try:
        while True:
            r = requests.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                break
            page = r.json()
            if not page:
                break
            guild_ids.update(int(g["id"]) for g in page)
            if len(page) < params["limit"]:
                break
            params["after"] = page[-1]["id"]
    except Exception as e:
        print(f"[AUTH] fetch_discord_bot_guilds error: {e}")
    return guild_ids


def fetch_bot_guilds_full() -> list[dict]:
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return []
    guilds: list[dict] = []
    headers = {"Authorization": f"Bot {bot_token}"}
    params  = {"limit": 200}
    try:
        while True:
            r = requests.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                break
            page = r.json()
            if not page:
                break
            guilds.extend(page)
            if len(page) < params["limit"]:
                break
            params["after"] = page[-1]["id"]
    except Exception as e:
        print(f"[AUTH] fetch_bot_guilds_full error: {e}")
    return guilds


def check_bot_guild_membership(guild_id: int) -> str:
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return BotMembershipStatus.INDETERMINATE
    try:
        r = requests.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=5,
        )
        if r.status_code == 200:
            return BotMembershipStatus.INSTALLED

        try:
            data = r.json()
            code = data.get("code")
        except Exception:
            code = None

        if r.status_code == 404 and code == 10004:
            return BotMembershipStatus.CONFIRMED_NO_ACCESS
        if r.status_code == 403 and code == 50001:
            return BotMembershipStatus.CONFIRMED_NO_ACCESS

        return BotMembershipStatus.INDETERMINATE
    except (requests.Timeout, requests.ConnectionError):
        return BotMembershipStatus.INDETERMINATE
    except Exception as e:
        print(f"[AUTH] check_bot_guild_membership error: {e}")
        return BotMembershipStatus.INDETERMINATE


def bot_is_in_guild(guild_id: int) -> bool:
    return check_bot_guild_membership(guild_id) == BotMembershipStatus.INSTALLED


def get_bot_invite_url(guild_id: int) -> str:
    return (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&guild_id={guild_id}"
        f"&disable_guild_select=true"
        f"&permissions={BOT_INVITE_PERMISSIONS}"
        f"&scope=bot+applications.commands"
    )


def create_session(user: dict, remember_me: bool = False):
    duration = SESSION_DURATION_REMEMBER if remember_me else SESSION_DURATION_DEFAULT
    session.permanent = remember_me
    session["user"] = {
        "id":       user.get("id"),
        "username": user.get("username"),
        "avatar":   user.get("avatar"),
    }
    session["expires_at"]  = time.time() + duration
    session["remember_me"] = remember_me
    session["csrf_token"] = secrets.token_hex(16)


def is_session_valid() -> bool:
    if "user" not in session:
        return False
    if time.time() > session.get("expires_at", 0):
        session.clear()
        return False
    return True


def refresh_session_if_needed():
    if not session.get("remember_me"):
        return
    remaining = session.get("expires_at", 0) - time.time()
    if remaining < 60 * 60 * 24 * 3:
        session["expires_at"] = time.time() + SESSION_DURATION_REMEMBER


def clear_session():
    session.clear()


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_session_valid():
            return redirect(url_for("login"))
        refresh_session_if_needed()
        return f(*args, **kwargs)
    return decorated


async def _get_user_level_async(guild_id: int, user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT permission_level FROM dashboard_users
            WHERE guild_id = ? AND user_id = ? AND enabled = 1
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else None


def get_current_user_level(guild_id: int) -> str | None:
    user = session.get("user")
    if not user:
        return None
    return run_async(_get_user_level_async(guild_id, int(user["id"])))


def current_user_id() -> int | None:
    user = session.get("user")
    return int(user["id"]) if user else None


def current_user() -> dict | None:
    return session.get("user")
