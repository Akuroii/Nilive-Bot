import os
import time
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
# Bot invite permission bitfield — defaults to Administrator (8) like the
# existing invite flow implied by DEBUG_GUIDE.md's bot+applications.commands
# scopes. Overridable via env if a narrower permission set is ever wanted.
BOT_INVITE_PERMISSIONS = os.getenv("BOT_INVITE_PERMISSIONS", "8")


def get_discord_oauth_url() -> str:
    return (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify+guilds"
    )


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


def guild_permissions_include_admin(permissions) -> bool:
    """
    Phase 0 Extension. `permissions` is the decimal-string bitfield
    Discord returns per-guild in /users/@me/guilds. Guild owners
    already have this bit set by Discord itself, so no separate
    owner check is needed.
    """
    try:
        perm_int = int(permissions)
    except (TypeError, ValueError):
        return False
    return bool(perm_int & DISCORD_PERMISSION_ADMINISTRATOR)


def fetch_discord_bot_guilds() -> set[int]:
    """
    Phase 0 Extension. Returns the set of guild IDs the bot is
    currently a member of, read via the BOT token (not the user's
    OAuth token), so this works regardless of what scopes the user
    granted. Used to flag which of a user's admin guilds already
    have the bot installed vs. still need an invite.

    Returns an empty set (never raises) if DISCORD_TOKEN isn't set
    or the API call fails — callers treat that as "assume not a
    member yet", which just shows the Invite button, the safe
    default.
    """
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


def bot_is_in_guild(guild_id: int) -> bool:
    """
    Phase 0 — Server Select fix. Single-guild membership check via the
    BOT token (GET /guilds/{id} — 200 if the bot is a member, 403/404
    otherwise). Used by /select-guild to verify the bot is actually
    installed before auto-granting OWNER_DISCORD_ID dashboard access,
    without paying for a full fetch_discord_bot_guilds() page-through
    just to check one guild.

    Returns False (never raises) on any failure or missing token — the
    safe default is "assume not a member", which just means the
    auto-grant path is skipped and the normal dashboard_users check
    (403 if no row) applies as before.
    """
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return False
    try:
        r = requests.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[AUTH] bot_is_in_guild error: {e}")
        return False


def get_bot_invite_url(guild_id: int) -> str:
    """
    Phase 0 Extension. Builds a bot-invite OAuth2 URL locked to one
    guild (disable_guild_select=true) so clicking "Invite Bot" from
    the dashboard can't accidentally add the bot to the wrong
    server. Uses the bot+applications.commands scopes DEBUG_GUIDE.md
    already documents as required.
    """
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
