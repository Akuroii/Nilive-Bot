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
# SECURITY HARDENING: default narrowed from Administrator (8) to the
# specific permissions the bot actually uses (manage roles/channels,
# kick/ban/timeout, manage nicknames, manage messages, send/embed/
# attach, mention everyone, connect/speak). Only affects the invite
# link generated for servers that DON'T have the bot yet — already-
# installed servers are untouched. Still overridable via env if
# Administrator is ever wanted back.
_DEFAULT_BOT_PERMISSIONS = (
    0x10 | 0x2 | 0x4 | 0x400 | 0x800 | 0x2000 | 0x4000 | 0x8000
    | 0x10000 | 0x40 | 0x10000000 | 0x10000000000
)
BOT_INVITE_PERMISSIONS = os.getenv(
    "BOT_INVITE_PERMISSIONS", str(_DEFAULT_BOT_PERMISSIONS))


def get_discord_oauth_url(remember: bool = False) -> str:
    # SECURITY FIX: OAuth `state` parameter was previously never
    # generated or checked — a login CSRF gap where an attacker could
    # pre-generate/replay an authorization request and bind a victim's
    # session to an attacker-controlled Discord account. A random
    # per-flow token is stored in the pre-auth session and must match
    # what Discord echoes back to /callback before any code exchange
    # happens (see verify_oauth_state below).
    #
    # BUGFIX, same change: "remember me" was already non-functional —
    # login.html wrote a `nero_remember` cookie nothing server-side
    # ever read, and `?remember=1` on /discord_login was silently
    # dropped since Discord's redirect back to /callback only ever
    # echoes `code` (and now `state`), not arbitrary extra params from
    # the original authorize request. The choice is now threaded
    # through the session alongside the state token so it actually
    # survives the round trip — see consume_oauth_remember below.
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
    """
    Call from /callback before exchanging the code. Consumes the
    stored state (single-use) so a replayed callback URL can't be
    reused. Returns False on any mismatch or missing state — callers
    should treat that as an auth failure and redirect back to /login,
    never proceed.
    """
    expected = session.pop("oauth_state", None)
    if not expected or not returned_state:
        return False
    return secrets.compare_digest(expected, returned_state)


def consume_oauth_remember() -> bool:
    """Pops the remember-me choice stashed by get_discord_oauth_url()."""
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


def fetch_bot_guilds_full() -> list[dict]:
    """
    Full guild objects (id, name, icon, ...) the bot is currently a
    member of, read via the BOT token — same endpoint
    fetch_discord_bot_guilds() above reduces to a bare ID set. Used
    by /server-select to render the developer's "every bot guild"
    view (dashboard/permissions.py's is_trusted_super_admin), which
    needs a name/icon per server to render, not just membership IDs.

    Kept as its own function rather than having
    fetch_discord_bot_guilds() build on top of it, so neither
    function's existing behavior is at risk from touching the other.

    Empty list (never raises) if DISCORD_TOKEN isn't set or the API
    call fails.
    """
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
    # SECURITY FIX (Critical, this pass): dashboard had zero CSRF
    # protection — every state-changing route relied solely on the
    # session cookie. Minting a random per-session token here, checked
    # against the X-CSRF-Token header on every non-GET /api/* request
    # (see dashboard/api.py before_request hook), closes that gap for
    # every fetch()/htmx-driven action without touching each route.
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
