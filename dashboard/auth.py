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


class GuildDiscoveryList(list):
    """List of Discord guild objects annotated with discovery health metadata."""

    def __init__(self, iterable=(), *, ok: bool = True, complete: bool = True,
                 status: str = "ok", http_status: int | None = None, error: str | None = None):
        super().__init__(iterable)
        self.ok = ok
        self.complete = complete
        self.status = status
        self.http_status = http_status
        self.error = error


class GuildDiscoverySet(set):
    """Set of Discord guild IDs annotated with discovery health metadata."""

    def __init__(self, iterable=(), *, ok: bool = True, complete: bool = True,
                 status: str = "ok", http_status: int | None = None, error: str | None = None):
        super().__init__(iterable)
        self.ok = ok
        self.complete = complete
        self.status = status
        self.http_status = http_status
        self.error = error


_MAX_GUILD_PAGES = 25
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
    client_id = os.getenv("DISCORD_CLIENT_ID") or CLIENT_ID
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI") or REDIRECT_URI
    return (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
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
    client_id = os.getenv("DISCORD_CLIENT_ID") or CLIENT_ID
    client_secret = os.getenv("DISCORD_CLIENT_SECRET") or CLIENT_SECRET
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI") or REDIRECT_URI
    try:
        r = requests.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fetch_discord_user(access_token: str) -> dict | None:
    try:
        r = requests.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _fetch_paginated_guilds(auth_header: str, log_label: str) -> GuildDiscoveryList:
    """
    Bounded pagination over GET /users/@me/guilds (limit=200 + after=<last_id>).
    Preserves whether an empty return is genuine (HTTP 200 []) vs a failed/rate-limited
    or page-cap-truncated Discord request so callers never confuse incomplete results
    with confirmed guild absence.
    """
    guilds: list[dict] = []
    headers = {"Authorization": auth_header}
    params: dict = {"limit": 200}
    reached_end = False
    try:
        for _ in range(_MAX_GUILD_PAGES):
            r = requests.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers=headers,
                params=params,
                timeout=10,
            )
            if r.status_code != 200:
                status = (
                    "unauthorized" if r.status_code in (401, 403)
                    else "rate_limited" if r.status_code == 429
                    else "discord_error"
                )
                return GuildDiscoveryList(
                    [],
                    ok=False,
                    complete=False,
                    status=status,
                    http_status=r.status_code,
                    error=f"Discord HTTP {r.status_code}",
                )
            page = r.json()
            if not isinstance(page, list):
                return GuildDiscoveryList([], ok=False, complete=False, status="malformed", error="Malformed guilds list")
            if not page:
                reached_end = True
                break
            valid_page = []
            for g in page:
                if not isinstance(g, dict):
                    return GuildDiscoveryList([], ok=False, complete=False, status="malformed", error="Malformed guild entry")
                gid_raw = str(g.get("id", "")).strip()
                if not gid_raw.isascii() or not gid_raw.isdigit():
                    return GuildDiscoveryList([], ok=False, complete=False, status="malformed", error="Invalid guild id")
                valid_page.append(g)
            guilds.extend(valid_page)
            if len(valid_page) < params["limit"]:
                reached_end = True
                break
            next_after = str(valid_page[-1]["id"])
            if params.get("after") == next_after:
                break
            params["after"] = next_after

        if not reached_end:
            return GuildDiscoveryList(
                guilds,
                ok=False,
                complete=False,
                status="truncated",
                http_status=200,
                error=f"Guild pagination stopped at {_MAX_GUILD_PAGES} pages without reaching end of list",
            )
        return GuildDiscoveryList(guilds, ok=True, complete=True, status="ok", http_status=200)
    except Exception as exc:
        print(f"[AUTH] {log_label} error: {type(exc).__name__}")
        return GuildDiscoveryList([], ok=False, complete=False, status="network_error", error=type(exc).__name__)


def fetch_discord_guilds(access_token: str) -> GuildDiscoveryList:
    token = (access_token or "").strip()
    if not token:
        return GuildDiscoveryList([], ok=False, complete=False, status="missing_token", error="OAuth access token missing")
    return _fetch_paginated_guilds(f"Bearer {token}", "fetch_discord_guilds")


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
    return perm_int >= 0 and bool(perm_int & (DISCORD_PERMISSION_ADMINISTRATOR | DISCORD_PERMISSION_MANAGE_GUILD))


def guild_permissions_include_admin(permissions) -> bool:
    """
    Legacy helper maintained for backward compatibility. Checks only
    ADMINISTRATOR (0x8).
    """
    try:
        perm_int = int(permissions)
    except (TypeError, ValueError):
        return False
    return perm_int >= 0 and bool(perm_int & DISCORD_PERMISSION_ADMINISTRATOR)


def fetch_discord_bot_guilds() -> GuildDiscoverySet:
    """
    Phase 0 Extension. Returns the set of guild IDs the bot is
    currently a member of, read via the BOT token, annotated with
    .ok / .complete / .status / .error so callers can distinguish a genuine
    complete result from a missing token, failed request, or truncated list.
    """
    full = fetch_bot_guilds_full()
    return GuildDiscoverySet(
        (int(g["id"]) for g in full),
        ok=getattr(full, "ok", True),
        complete=getattr(full, "complete", True),
        status=getattr(full, "status", "ok"),
        http_status=getattr(full, "http_status", None),
        error=getattr(full, "error", None),
    )


def fetch_bot_guilds_full() -> GuildDiscoveryList:
    """
    Full guild objects (id, name, icon, ...) the bot is currently a
    member of, read via the BOT token with bounded pagination.
    Returns a GuildDiscoveryList (a list subclass with .ok, .complete,
    .status, .http_status, .error) so callers can distinguish genuine empty
    membership from missing DISCORD_TOKEN, API failures, or truncated lists.
    """
    bot_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not bot_token:
        return GuildDiscoveryList([], ok=False, complete=False, status="missing_token", error="BOT_TOKEN not configured")
    return _fetch_paginated_guilds(f"Bot {bot_token}", "fetch_bot_guilds_full")


def check_bot_guild_membership(guild_id: int) -> str:
    """
    Conservative tri-state membership check via the BOT token.
    Distinguishes confirmed installation (200), confirmed non-access
    (explicit Discord error codes 10004 Unknown Guild or 50001 Missing Access),
    and indeterminate states (timeouts, rate limits 429, auth errors 401,
    Discord server errors 5xx).
    """
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
    """
    Single-guild membership check via BOT token. Returns True only when
    confirmed installed (HTTP 200).
    """
    return check_bot_guild_membership(guild_id) == BotMembershipStatus.INSTALLED


def get_bot_invite_url(guild_id: int) -> str:
    """
    Phase 0 Extension. Builds a bot-invite OAuth2 URL locked to one
    guild (disable_guild_select=true) so clicking "Invite Bot" from
    the dashboard can't accidentally add the bot to the wrong
    server. Uses the bot+applications.commands scopes DEBUG_GUIDE.md
    already documents as required.
    """
    client_id = os.getenv("DISCORD_CLIENT_ID") or CLIENT_ID
    perms = os.getenv("BOT_INVITE_PERMISSIONS") or BOT_INVITE_PERMISSIONS
    return (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&guild_id={guild_id}"
        f"&disable_guild_select=true"
        f"&permissions={perms}"
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
