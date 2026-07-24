import os
import aiosqlite
import requests
from database import DB_PATH

DISCORD_API = "https://discord.com/api/v10"


async def get_guild_bot_profile(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT nickname, avatar_url, updated_at FROM guild_bot_profile WHERE guild_id = ?",
            (guild_id,))
        row = await cursor.fetchone()
    if not row:
        return {"guild_id": guild_id, "nickname": None, "avatar_url": None, "updated_at": None}
    return {"guild_id": guild_id, "nickname": row[0], "avatar_url": row[1], "updated_at": row[2]}


async def save_guild_bot_profile(guild_id: int, nickname: str | None, avatar_url: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO guild_bot_profile (guild_id, nickname, avatar_url, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                nickname   = excluded.nickname,
                avatar_url = excluded.avatar_url,
                updated_at = CURRENT_TIMESTAMP
        """, (guild_id, nickname or None, avatar_url or None))
        await db.commit()


def apply_nickname_via_rest(guild_id: int, nickname: str | None) -> tuple[bool, str | None]:
    """
    Applies (or clears, if nickname is falsy) the bot's own per-guild
    nickname immediately via Discord's REST API, using the bot token —
    same direct-REST-with-bot-token pattern dashboard/api/core.py already
    uses for role/channel lookups. PATCH /guilds/{id}/members/@me is
    Discord's documented "Modify Current Member" endpoint (the modern
    replacement for the old /members/@me/nick endpoint); it works purely
    over REST, so this has zero dependency on the bot process holding a
    live gateway connection at the moment this is called.

    Returns (success, error_message).
    """
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return False, "Bot token not configured"
    try:
        resp = requests.patch(
            f"{DISCORD_API}/guilds/{guild_id}/members/@me",
            headers={"Authorization": f"Bot {bot_token}",
                     "Content-Type": "application/json"},
            json={"nick": nickname or None},
            timeout=8,
        )
        if resp.status_code in (200, 204):
            return True, None
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        return False, f"Discord API error {resp.status_code}: {detail}"
    except Exception as e:
        return False, str(e)


def get_live_bot_member(guild_id: int) -> dict | None:
    """
    Reads the bot's own current member object (live nick, global
    username, global avatar) straight from Discord, so the dashboard can
    show what's actually live right now next to what's stored in
    guild_bot_profile — the two can drift (nickname changed manually in
    Discord's UI, or the bot got kicked/re-invited).
    """
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return None
    try:
        resp = requests.get(
            f"{DISCORD_API}/guilds/{guild_id}/members/@me",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        user = data.get("user", {})
        avatar_hash = user.get("avatar")
        ext = "gif" if (avatar_hash or "").startswith("a_") else "png"
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{user.get('id')}/{avatar_hash}.{ext}"
            if avatar_hash else None
        )
        return {
            "nick": data.get("nick"),
            "username": user.get("username"),
            "global_avatar_url": avatar_url,
        }
    except Exception as e:
        print(f"[BOTPROFILE] get_live_bot_member error: {e}")
        return None
