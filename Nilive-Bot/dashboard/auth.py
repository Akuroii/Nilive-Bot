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
