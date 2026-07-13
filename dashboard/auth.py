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


# ── PHASE 0: SERVER PERMISSION GATING ───────────────────────────────
#
# Two additions in support of the server-gating feature on
# /server-select: knowing which of the user's guilds the bot is
# ALREADY in (fetch_discord_bot_guilds, uses the bot token — the
# user's own OAuth2 token has no visibility into that), and building
# a guild-targeted invite link (get_bot_invite_url). Also a small
# permission-bit helper so app.py doesn't need to know Discord's
# permission bitfield layout.
#
# This is deliberately separate from dashboard_users (Nilive's own
# per-guild access-approval table, see database.py). dashboard_users
# still gates who can actually USE the dashboard for a guild.
# fetch_discord_bot_guilds / get_bot_invite_url only answer "is the
# bot in this Discord server yet" and "here's how to add it" — a
# setup aid, not an access grant.

ADMINISTRATOR_PERMISSION_BIT = 0x8

# Default invite scope: bot + slash commands, Administrator permission.
# Administrator (not a hand-picked subset of permission bits) is used
# deliberately here: Nilive runs on a small number of trusted,
# approval-based friend servers (see project memory), and the bot's
# cogs already span moderation, channel/role management, and ticket
# channel creation — a narrower bitmask would need constant upkeep as
# cogs grow, for no real security benefit in this deployment model.
# Override via DISCORD_BOT_PERMISSIONS if a narrower grant is wanted.
DEFAULT_BOT_INVITE_PERMISSIONS = os.getenv(
    "DISCORD_BOT_PERMISSIONS", str(ADMINISTRATOR_PERMISSION_BIT))


def fetch_discord_bot_guilds() -> list[str]:
    """
    Returns the guild IDs (as strings, matching Discord API shape) that
    the bot itself is currently a member of, via the bot token. Used to
    compute is_bot_member for each of the user's guilds in
    /api/user/servers. Returns [] (not an exception) on any failure —
    callers treat that the same as "bot membership unknown / assume
    not a member", which is the safe default for a gating feature
    (worst case: a server that already has the bot shows an
    unnecessary Invite button, not a hidden active server).
    """
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return []
    try:
        r = requests.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=10,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return [g["id"] for g in r.json()]


def guild_permissions_include_admin(permissions_str: str | None,
                                     is_owner: bool) -> bool:
    """
    Discord's /users/@me/guilds response includes `owner` (bool) and
    `permissions` (a base-10 string of the computed permission
    bitfield) per guild. A user counts as "admin" for gating purposes
    if they own the guild OR have the ADMINISTRATOR bit set.
    """
    if is_owner:
        return True
    if not permissions_str:
        return False
    try:
        perms = int(permissions_str)
    except (TypeError, ValueError):
        return False
    return bool(perms & ADMINISTRATOR_PERMISSION_BIT)


def get_bot_invite_url(guild_id: int) -> str:
    """
    Builds a Discord OAuth2 bot-invite URL pre-targeted at a specific
    guild. disable_guild_select=true locks Discord's own guild picker
    so the inviting admin can't accidentally add the bot to the wrong
    server.
    """
    return (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope=bot+applications.commands"
        f"&permissions={DEFAULT_BOT_INVITE_PERMISSIONS}"
        f"&guild_id={guild_id}"
        f"&disable_guild_select=true"
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
