import os
import asyncio
import aiohttp
import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# DISCORD USER CACHE
#
# Foundation piece for showing "Username (big) / User ID (small)" across
# dashboard pages that only ever stored a raw user_id (economy, trade,
# missions, tickets, inventory, ledger, etc) — as opposed to tables like
# moderation_logs/purchase_history/mvp_history/warnings, which already
# snapshot user_display_name AT THE TIME of the action for audit-trail
# accuracy. This module is deliberately NOT used to overwrite those —
# a moderation log should keep showing who someone WAS named when they
# were warned, not silently rewrite history to their current name.
#
# Own standalone schema (own ensure_table(), not in database.py's
# init_db()) — same pattern utils/trade_engine.py, cogs/minigames.py,
# and utils/mission_engine.py already established for exactly this
# reason: isolated, low-risk additions that don't touch the shared
# migration path.
#
# TTL-based cache (default 24h) rather than fetch-every-page-load:
# Discord usernames/nicknames don't change often, and every dashboard
# page render that shows a table of members would otherwise mean one
# Discord API call per row, per view — that's both slow and a good way
# to get rate-limited. A stale cache entry still gets returned
# immediately; refreshing it happens on the same call but never blocks
# the page from getting SOME answer.
#
# Fetch order per user_id:
#   1. GET /guilds/{guild_id}/members/{user_id} — gives server nickname
#      (display_name) + global username + avatar. Best case.
#   2. If that 404s (member left the server), fall back to
#      GET /users/{user_id} — global user object still usually exists,
#      gives username + avatar but no server-specific nickname.
#   3. If BOTH fail (deleted account, invalid ID, no bot token
#      configured, network error) — resolved=False, caller falls back
#      to showing the raw ID only. This never raises; a broken lookup
#      must never break the page it's rendering on.
#
# Concurrency capped via a semaphore — resolving a page of e.g. 50
# distinct user_ids fires up to MAX_CONCURRENT_FETCHES requests at
# once rather than 50 simultaneously, which is friendlier to Discord's
# per-route rate limits.
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_MAX_AGE_HOURS = 24
MAX_CONCURRENT_FETCHES = 8
DISCORD_API = "https://discord.com/api/v10"


async def ensure_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS discord_user_cache (
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                username     TEXT,
                display_name TEXT,
                avatar_url   TEXT,
                resolved     INTEGER DEFAULT 1,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.commit()


def _fallback_entry(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "username": None,
        "display_name": None,
        "avatar_url": None,
        "resolved": False,
    }


def _avatar_url(user_id: int, avatar_hash: str | None,
                 discriminator: str = "0") -> str | None:
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}"
    return None


async def _fetch_one(session: aiohttp.ClientSession, headers: dict,
                      guild_id: int, user_id: int) -> dict:
    # 1. Guild member — best case, has nickname.
    try:
        async with session.get(
                f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}",
                headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                user = data.get("user", {})
                username = user.get("username") or f"User {user_id}"
                display_name = (data.get("nick")
                                or user.get("global_name")
                                or username)
                avatar = _avatar_url(user_id, user.get("avatar"))
                return {
                    "user_id": user_id, "username": username,
                    "display_name": display_name, "avatar_url": avatar,
                    "resolved": True,
                }
    except Exception as e:
        print(f"[USER_CACHE] guild member fetch failed for {user_id}: {e}")

    # 2. Fall back to the global user object (member left the guild,
    # but the account itself may still exist).
    try:
        async with session.get(
                f"{DISCORD_API}/users/{user_id}",
                headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                user = await resp.json()
                username = user.get("username") or f"User {user_id}"
                display_name = user.get("global_name") or username
                avatar = _avatar_url(user_id, user.get("avatar"))
                return {
                    "user_id": user_id, "username": username,
                    "display_name": display_name, "avatar_url": avatar,
                    "resolved": True,
                }
    except Exception as e:
        print(f"[USER_CACHE] global user fetch failed for {user_id}: {e}")

    return _fallback_entry(user_id)


async def _fetch_many(guild_id: int, user_ids: list[int],
                       bot_token: str) -> dict[int, dict]:
    headers = {"Authorization": f"Bot {bot_token}"}
    sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    results: dict[int, dict] = {}

    async def bound_fetch(session, uid):
        async with sem:
            results[uid] = await _fetch_one(session, headers, guild_id, uid)

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(bound_fetch(session, uid) for uid in user_ids))
    return results


async def resolve_users(guild_id: int, user_ids: list[int],
                         max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict[int, dict]:
    """
    Returns {user_id: {"username", "display_name", "avatar_url", "resolved"}}
    for every ID in user_ids. Cached entries newer than max_age_hours are
    returned as-is; everything else (missing or stale) is re-fetched from
    Discord, written back to the cache, then returned. Never raises —
    on total failure (no token, network down) every requested ID still
    gets a well-formed fallback entry with resolved=False.
    """
    user_ids = list({int(u) for u in user_ids if u is not None})
    if not user_ids:
        return {}

    await ensure_table()

    now = datetime.now(timezone.utc)
    fresh: dict[int, dict] = {}
    stale_or_missing: list[int] = []

    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in user_ids)
        cursor = await db.execute(f"""
            SELECT user_id, username, display_name, avatar_url,
                   resolved, updated_at
            FROM discord_user_cache
            WHERE guild_id = ? AND user_id IN ({placeholders})
        """, (guild_id, *user_ids))
        rows = await cursor.fetchall()

    cached_ids = set()
    for (uid, username, display_name, avatar_url, resolved, updated_at) in rows:
        cached_ids.add(uid)
        is_stale = True
        try:
            updated_dt = datetime.fromisoformat(updated_at)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_hours = (now - updated_dt).total_seconds() / 3600
            is_stale = age_hours >= max_age_hours
        except Exception:
            is_stale = True

        if is_stale:
            stale_or_missing.append(uid)
        else:
            fresh[uid] = {
                "user_id": uid, "username": username,
                "display_name": display_name, "avatar_url": avatar_url,
                "resolved": bool(resolved),
            }

    for uid in user_ids:
        if uid not in cached_ids:
            stale_or_missing.append(uid)

    if not stale_or_missing:
        return fresh

    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        # No token configured — every uncached/stale ID falls back to
        # raw-ID display. Anything already fresh in cache still returns
        # normally above.
        for uid in stale_or_missing:
            fresh[uid] = _fallback_entry(uid)
        return fresh

    fetched = await _fetch_many(guild_id, stale_or_missing, bot_token)

    async with aiosqlite.connect(DB_PATH) as db:
        for uid, entry in fetched.items():
            await db.execute("""
                INSERT INTO discord_user_cache
                    (guild_id, user_id, username, display_name,
                     avatar_url, resolved, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    username     = excluded.username,
                    display_name = excluded.display_name,
                    avatar_url   = excluded.avatar_url,
                    resolved     = excluded.resolved,
                    updated_at   = CURRENT_TIMESTAMP
            """, (guild_id, uid, entry["username"], entry["display_name"],
                  entry["avatar_url"], int(entry["resolved"])))
        await db.commit()

    fresh.update(fetched)
    return fresh
