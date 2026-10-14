import discord
import aiosqlite
import sqlite3
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# CREATOR LIVE NOTIFICATION ENGINE
#
# Shared by cogs/youtube.py (Live only) and cogs/twitch.py. One state
# machine, one message-persistence model, used identically by both
# platforms — each cog only supplies: an embed, a Link button
# URL, mention text, and the raw "is it live right now" answer from its
# own API. Everything about "did we already post this / do we edit or
# send / what happens if the message is gone" lives here exactly once.
#
# STATE MACHINE (per guild+platform+watch):
#   (no session) --start_live_session--> LIVE --end_live_session--> ENDED
#
# CONCURRENCY: every cog's poll loop processes its configured watches
# sequentially (plain `for cfg in configs: await ...`, never
# asyncio.gather across rows), so there is no in-process race for the
# same watch. As defense-in-depth against any future change to that
# assumption (or a botched double-deploy running two bot processes),
# creator_live_sessions carries a PARTIAL UNIQUE INDEX — at most one
# row with status='live' per (guild_id, platform, watch_id) can ever
# exist, enforced by SQLite itself, not just application logic. If a
# second start_live_session() ever loses that race after already
# sending its Discord message (only possible with two live processes),
# it detects the resulting IntegrityError, deletes the message it just
# sent, and logs loudly — the failure mode becomes "one log line", not
# a visible duplicate notification.
#
# TRANSIENT FAILURES: nothing in here assumes an API check always
# succeeds. Every caller in cogs/{youtube,twitch}.py must treat
# its own live-check function's three possible answers distinctly:
#   dict          -> confirmed live right now
#   False         -> confirmed NOT live right now (a real, successful
#                    API response that said so)
#   None          -> the check FAILED (timeout, rate limit, bad
#                    response, network error) — UNKNOWN, not a
#                    transition. Callers must skip the tick entirely
#                    on None: don't touch offline-debounce counters,
#                    don't end a session, don't start one. This is the
#                    fix for a real pre-existing bug where "confirmed
#                    offline" and "API error" were indistinguishable
#                    and a rate-limited request could have looked
#                    identical to the stream actually ending.
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_OFFLINE_THRESHOLD = 2  # consecutive confirmed-offline polls
                                # required before we actually end a
                                # session — guards against a single
                                # flaky/borderline API read flapping a
                                # still-live stream to "ended" and back.


async def ensure_tables():
    """
    Idempotent. Safe to call from both the bot process and the
    dashboard process, on every startup, as many times as needed —
    every statement here is either CREATE ... IF NOT EXISTS or an
    ALTER TABLE gated by a PRAGMA table_info() check first. Never
    drops, renames, or overwrites an existing column's data; the one
    backfill UPDATE below only ever sets the brand-new column it just
    added, based on a pre-existing column's value, and never touches
    any other column or row shape.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS creator_live_sessions (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id               INTEGER NOT NULL,
                platform               TEXT NOT NULL,
                watch_id               INTEGER NOT NULL,
                external_id            TEXT,
                discord_channel_id     INTEGER NOT NULL,
                message_id             INTEGER,
                status                 TEXT NOT NULL DEFAULT 'live',
                offline_confirmations  INTEGER NOT NULL DEFAULT 0,
                started_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at               TIMESTAMP
            )
        """)
        # Partial unique index: only rows with status='live' are
        # constrained, so history (many past 'ended' rows for the same
        # watch, one per stream session over time) is preserved while
        # "at most one currently-active session per watch" is enforced
        # by SQLite itself — see the concurrency note above.
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cls_active_unique
            ON creator_live_sessions(guild_id, platform, watch_id)
            WHERE status = 'live'
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_cls_guild
            ON creator_live_sessions(guild_id)
        """)
        await db.commit()

    await _migrate_youtube_config()
    await _migrate_twitch_config()
    await ensure_group_tables()


async def _migrate_youtube_config():
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute("PRAGMA table_info(youtube_config)")
            cols = [c[1] for c in await cursor.fetchall()]
            if not cols:
                return  # table doesn't exist yet on this process — the
                        # owning table's own CREATE TABLE will run
                        # elsewhere (database.py's init_db); nothing to
                        # migrate yet, safe no-op.

            alters = []
            if "video_mention_type" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "video_mention_type TEXT DEFAULT 'role'")
            if "shorts_enabled" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "shorts_enabled INTEGER DEFAULT 0")
            if "shorts_discord_channel_id" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "shorts_discord_channel_id INTEGER")
            if "shorts_custom_message" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "shorts_custom_message TEXT")
            if "shorts_mention_type" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "shorts_mention_type TEXT DEFAULT 'none'")
            if "shorts_mention_role_id" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "shorts_mention_role_id INTEGER")
            if "live_enabled" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "live_enabled INTEGER DEFAULT 0")
            if "live_discord_channel_id" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "live_discord_channel_id INTEGER")
            if "live_custom_message" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "live_custom_message TEXT")
            if "live_mention_type" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "live_mention_type TEXT DEFAULT 'none'")
            if "live_mention_role_id" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "live_mention_role_id INTEGER")
            if "live_video_id" not in cols:
                alters.append(
                    "ALTER TABLE youtube_config ADD COLUMN "
                    "live_video_id TEXT")

            needed_backfill = "video_mention_type" not in cols

            for stmt in alters:
                await db.execute(stmt)
            if alters:
                await db.commit()

            if needed_backfill:
                # Rows that already had a ping role configured were
                # implicitly "mention that role" before this column
                # existed; rows without one were implicitly "no
                # mention". This only SETS the new column — every
                # other existing column/row is untouched.
                await db.execute("""
                    UPDATE youtube_config SET video_mention_type = 'role'
                    WHERE ping_role_id IS NOT NULL
                """)
                await db.commit()
        except Exception as e:
            print(f"[CREATOR_ENGINE] youtube_config migration error: {e}")


async def _migrate_twitch_config():
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute("PRAGMA table_info(twitch_config)")
            cols = [c[1] for c in await cursor.fetchall()]
            if not cols:
                return
            if "mention_type" not in cols:
                await db.execute(
                    "ALTER TABLE twitch_config ADD COLUMN "
                    "mention_type TEXT DEFAULT 'none'")
                await db.commit()
                await db.execute("""
                    UPDATE twitch_config SET mention_type = 'role'
                    WHERE ping_role_id IS NOT NULL
                """)
                await db.commit()
        except Exception as e:
            print(f"[CREATOR_ENGINE] twitch_config migration error: {e}")


# ── Mention / view helpers ───────────────────────────────────────────────

def build_mention(guild: discord.Guild, mention_type: str, role_id) -> str:
    if mention_type == "everyone":
        return "@everyone"
    if mention_type == "role" and role_id:
        role = guild.get_role(int(role_id))
        if role:
            return role.mention
    return ""


class WatchNowView(discord.ui.View):
    """
    A single link-style button. Link buttons carry no custom_id and
    need no interaction handling or bot.add_view() registration —
    Discord resolves them client-side forever, so this survives bot
    restarts with zero extra code, unlike a callback-based button
    would.
    """
    def __init__(self, url: str, label: str = "▶ Watch Now"):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, url=url, label=label))


# ── Session state ─────────────────────────────────────────────────────

async def get_active_session(guild_id: int, platform: str, watch_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, external_id, discord_channel_id, message_id,
                   status, offline_confirmations
            FROM creator_live_sessions
            WHERE guild_id=? AND platform=? AND watch_id=? AND status='live'
        """, (guild_id, platform, watch_id))
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "external_id": row[1], "discord_channel_id": row[2],
        "message_id": row[3], "status": row[4], "offline_confirmations": row[5],
    }


async def start_live_session(bot, guild_id: int, platform: str, watch_id: int,
                              discord_channel_id: int, external_id: str,
                              content: str | None, embed: discord.Embed,
                              view: discord.ui.View | None) -> dict:
    """
    Sends the go-live notification and persists the session row.
    Every failure mode returns a result dict instead of raising —
    callers should log unexpected `sent: False` results but never need
    to wrap this in their own try/except for it to be safe.
    """
    existing = await get_active_session(guild_id, platform, watch_id)
    if existing:
        return {"sent": False, "reason": "already_active", "session": existing}

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"sent": False, "reason": "guild_not_found"}
    channel = guild.get_channel(int(discord_channel_id))
    if not channel:
        return {"sent": False, "reason": "channel_not_found"}

    try:
        msg = await channel.send(content=content or None, embed=embed, view=view)
    except Exception as e:
        print(f"[CREATOR_ENGINE] Failed to send live notification "
              f"(guild={guild_id} platform={platform} watch={watch_id}): {e}")
        return {"sent": False, "reason": f"send_failed: {e}"}

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO creator_live_sessions
                    (guild_id, platform, watch_id, external_id,
                     discord_channel_id, message_id, status)
                VALUES (?, ?, ?, ?, ?, ?, 'live')
            """, (guild_id, platform, watch_id, external_id,
                  discord_channel_id, msg.id))
            await db.commit()
        except sqlite3.IntegrityError as e:
            # The partial unique index rejected this insert — a live
            # session for this watch already exists (only reachable if
            # two bot processes are somehow running concurrently, since
            # a single process's poll loop is strictly sequential per
            # watch). We've already sent a real Discord message at this
            # point; best-effort delete it so the failure mode is "one
            # loud log line", not a visible duplicate notification.
            print(f"[CREATOR_ENGINE] Duplicate live session detected for "
                  f"guild={guild_id} platform={platform} watch={watch_id} "
                  f"(insert conflict: {e}) — deleting the message we just "
                  f"sent (id={msg.id}) to avoid a visible duplicate.")
            try:
                await msg.delete()
            except Exception as del_err:
                print(f"[CREATOR_ENGINE] Could not delete duplicate "
                      f"message {msg.id}: {del_err}")
            return {"sent": False, "reason": "duplicate_suppressed"}

    return {"sent": True, "message_id": msg.id}


async def note_still_live(guild_id: int, platform: str, watch_id: int):
    """
    Resets the offline-confirmation debounce counter. Call this on
    every poll tick where the source is freshly confirmed live (i.e.
    the check returned a real `dict`, not `None`) — so a brief
    ambiguous/failed read followed by a genuine "still live" doesn't
    carry stale progress toward the offline threshold.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE creator_live_sessions SET offline_confirmations = 0
            WHERE guild_id=? AND platform=? AND watch_id=? AND status='live'
        """, (guild_id, platform, watch_id))
        await db.commit()


async def note_offline_tick(guild_id: int, platform: str, watch_id: int,
                             threshold: int = DEFAULT_OFFLINE_THRESHOLD) -> bool:
    """
    Records one CONFIRMED-offline poll (caller must only call this when
    its own API check returned `False`, never on `None`/unknown).
    Returns True once `threshold` consecutive confirmed-offline polls
    have been recorded — the caller should then call end_live_session.
    Returning False means "not yet, keep the session open."
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE creator_live_sessions
            SET offline_confirmations = offline_confirmations + 1
            WHERE guild_id=? AND platform=? AND watch_id=? AND status='live'
        """, (guild_id, platform, watch_id))
        cursor = await db.execute("""
            SELECT offline_confirmations FROM creator_live_sessions
            WHERE guild_id=? AND platform=? AND watch_id=? AND status='live'
        """, (guild_id, platform, watch_id))
        row = await cursor.fetchone()
        await db.commit()
    return bool(row) and row[0] >= threshold


async def end_live_session(bot, guild_id: int, platform: str, watch_id: int,
                            ended_content: str | None, ended_embed: discord.Embed,
                            view: discord.ui.View | None = None) -> dict:
    """
    Edits the active session's Discord message into its "ended" state
    and closes the session. Never raises — every failure mode (guild
    gone, channel gone, message deleted, missing permission, any other
    Discord error) is caught and resolved to a clean result, never a
    crash and never a replacement message.
    """
    session = await get_active_session(guild_id, platform, watch_id)
    if not session:
        return {"ended": False, "reason": "no_active_session"}

    async def _close(reason: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE creator_live_sessions
                SET status='ended', ended_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (session["id"],))
            await db.commit()
        return {"ended": True, "reason": reason}

    guild = bot.get_guild(guild_id)
    if not guild:
        return await _close("guild_not_found")
    channel = guild.get_channel(int(session["discord_channel_id"]))
    if not channel:
        return await _close("channel_not_found")
    if not session["message_id"]:
        return await _close("no_message_id")

    try:
        msg = await channel.fetch_message(int(session["message_id"]))
        await msg.edit(content=ended_content, embed=ended_embed, view=view)
        return await _close("edited")
    except discord.NotFound:
        # Message (or its channel) was deleted manually — nothing to
        # edit. Close the session quietly. Per spec: never send a
        # replacement message here.
        return await _close("message_deleted")
    except discord.Forbidden:
        print(f"[CREATOR_ENGINE] Missing permission to edit live message "
              f"(guild={guild_id} platform={platform} watch={watch_id})")
        return await _close("forbidden")
    except Exception as e:
        # Transient failure (rate limit, network blip) — leave the
        # session marked 'live' so the NEXT poll tick's end attempt
        # retries the edit, rather than losing track of an ended
        # stream because Discord hiccuped once.
        print(f"[CREATOR_ENGINE] Failed to edit ended-state message "
              f"(guild={guild_id} platform={platform} watch={watch_id}): {e}")
        return {"ended": False, "reason": f"retry: {e}"}


# ═══════════════════════════════════════════════════════════════════════
# CREATOR GROUPS — consolidated cross-platform live notifications
# (CREATOR pass 3, additive-only)
#
# Everything above this line is untouched and still the DEFAULT for
# every watch that exists today: a platform watch with no
# creator_group_id (every existing row, and every new row unless the
# dashboard's Creator Groups panel is used to link it) keeps calling
# start_live_session()/end_live_session() exactly as before and posts
# its own independent message. Nothing about that path changes.
#
# A Creator Group is an opt-in, named bundle of watches across
# platforms (e.g. "MeowlyVA" = a twitch_config row + a youtube_config
# row) that should announce as ONE message instead of N separate ones:
#
#   {mention} 🔴 **{display_name} is LIVE!**
#   🟣 Twitch — https://twitch.tv/meowlyva
#   🔴 YouTube — https://youtube.com/live/XVJpU9UgXY4
#
# — sent once when the FIRST linked platform goes live, then EDITED
# (not resent) to add a line each time another linked platform also
# goes live, and edited again to drop a line each time one goes
# offline. Editing a Discord message never re-fires push
# notifications/pings, so the @everyone/role mention baked into the
# first line is safe to leave in place across every edit — it only
# actually pinged anyone at the original send.
#
# This deliberately reuses the EXACT SAME tri-state detection and
# offline-debounce machinery every cog already has for its solo path
# (note_still_live/note_offline_tick, unchanged, still keyed by
# (guild_id, platform, watch_id) in creator_live_sessions) — grouping
# only changes what happens at the two transition points (first
# confirmed live / confirmed offline), not how "is it live" gets
# decided. start_watch_tracking()/stop_watch_tracking() below are the
# group-mode equivalents of start_live_session()/end_live_session()'s
# own row bookkeeping, MINUS the "send/edit my own message" part —
# get_active_session() (above) works identically for either path since
# both write to the same creator_live_sessions table.
#
# The shared message itself is tracked separately, in
# creator_group_sessions (one row per "is there currently a live
# consolidated message for this group" — same live/ended shape and
# same partial-unique-index protection as creator_live_sessions) plus
# creator_group_session_lines (which platforms currently have a line
# in it). Plain message content, not an embed — matches the reference
# screenshot's format and makes editing a single line in/out a simple
# string rebuild rather than juggling embed fields.
# ═══════════════════════════════════════════════════════════════════════


async def ensure_group_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS creator_groups (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            INTEGER NOT NULL,
                display_name        TEXT NOT NULL,
                discord_channel_id  INTEGER NOT NULL,
                mention_type        TEXT DEFAULT 'none',
                mention_role_id     INTEGER,
                enabled             INTEGER DEFAULT 1,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_cg_guild
            ON creator_groups(guild_id)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS creator_group_sessions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            INTEGER NOT NULL,
                group_id            INTEGER NOT NULL,
                discord_channel_id  INTEGER NOT NULL,
                message_id          INTEGER,
                status              TEXT NOT NULL DEFAULT 'live',
                started_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at            TIMESTAMP
            )
        """)
        # Same protection as creator_live_sessions' own partial unique
        # index — at most one 'live' consolidated session per group,
        # enforced by SQLite itself.
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cgs_active_unique
            ON creator_group_sessions(group_id) WHERE status = 'live'
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_cgs_guild
            ON creator_group_sessions(guild_id)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS creator_group_session_lines (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL,
                platform    TEXT NOT NULL,
                watch_id    INTEGER NOT NULL,
                label       TEXT NOT NULL,
                url         TEXT NOT NULL,
                emoji       TEXT,
                UNIQUE(session_id, platform, watch_id)
            )
        """)
        await db.commit()

    # One nullable link column per platform table. NULL — the default
    # for every row that exists today, and for every new row unless
    # explicitly linked — means "not part of a group", i.e. exactly
    # today's independent-message behavior. Same defensive shape as
    # _migrate_youtube_config() etc above: skip silently if the table
    # itself doesn't exist yet on this guild.
    for table in ("youtube_config", "twitch_config"):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(f"PRAGMA table_info({table})")
                cols = [c[1] for c in await cursor.fetchall()]
                if not cols:
                    continue
                if "creator_group_id" not in cols:
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN creator_group_id INTEGER")
                    await db.commit()
        except Exception as e:
            print(f"[CREATOR_ENGINE] {table}.creator_group_id migration error: {e}")


async def get_creator_group(group_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, guild_id, display_name, discord_channel_id,
                   mention_type, mention_role_id, enabled
            FROM creator_groups WHERE id = ?
        """, (group_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "guild_id": row[1], "display_name": row[2],
        "discord_channel_id": row[3], "mention_type": row[4] or "none",
        "mention_role_id": row[5], "enabled": bool(row[6]),
    }


async def get_watch_group(platform: str, watch_id: int) -> dict | None:
    """
    Returns the linked, enabled Creator Group for a given platform
    watch, or None — either because it isn't linked to one, or because
    the group it's linked to has been disabled (treated the same as
    unlinked: falls back to that watch's own independent notification).
    """
    table = {"youtube": "youtube_config", "twitch": "twitch_config"}.get(platform)
    if not table:
        return None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT creator_group_id FROM {table} WHERE id = ?", (watch_id,))
            row = await cursor.fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    group = await get_creator_group(int(row[0]))
    if not group or not group.get("enabled"):
        return None
    return group


async def start_watch_tracking(guild_id: int, platform: str, watch_id: int,
                                discord_channel_id: int, external_id: str) -> dict:
    """
    Group-mode counterpart to start_live_session() — creates the exact
    same creator_live_sessions bookkeeping row (so get_active_session/
    note_still_live/note_offline_tick all keep working identically
    regardless of whether this watch is grouped), but does NOT send a
    Discord message of its own. discord_channel_id is stored for
    informational/debugging purposes only here (the group's channel,
    not this watch's) — the actual message lives in
    creator_group_sessions instead.
    """
    existing = await get_active_session(guild_id, platform, watch_id)
    if existing:
        return {"started": False, "reason": "already_active"}

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT INTO creator_live_sessions
                    (guild_id, platform, watch_id, external_id,
                     discord_channel_id, message_id, status)
                VALUES (?, ?, ?, ?, ?, NULL, 'live')
            """, (guild_id, platform, watch_id, external_id, discord_channel_id))
            await db.commit()
        except sqlite3.IntegrityError:
            return {"started": False, "reason": "already_active"}
    return {"started": True}


async def stop_watch_tracking(guild_id: int, platform: str, watch_id: int) -> dict:
    """Counterpart to stop tracking — closes the row start_watch_tracking opened."""
    session = await get_active_session(guild_id, platform, watch_id)
    if not session:
        return {"stopped": False, "reason": "no_active_session"}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE creator_live_sessions
            SET status = 'ended', ended_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (session["id"],))
        await db.commit()
    return {"stopped": True}


async def _render_group_message(group: dict, session_id: int, mention: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT label, url, emoji FROM creator_group_session_lines
            WHERE session_id = ? ORDER BY id ASC
        """, (session_id,))
        lines = await cursor.fetchall()

    header = f"🔴 **{group['display_name']} is LIVE!**"
    if mention:
        header = f"{mention} {header}"
    body = [f"{emoji or '🔴'} {label} — {url}" for (label, url, emoji) in lines]
    return "\n".join([header] + body)


async def note_platform_live_grouped(bot, group: dict, platform: str, watch_id: int,
                                      label: str, emoji: str, url: str) -> dict:
    """
    Adds/updates this platform's line in the group's active
    consolidated message — sending a fresh message if this is the
    first linked platform to go live, editing the existing one
    otherwise. Call only after start_watch_tracking() reports
    started=True (mirrors start_live_session()'s own dedup contract).
    """
    guild_id, group_id = group["guild_id"], group["id"]

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"sent": False, "reason": "guild_not_found"}
    channel = guild.get_channel(int(group["discord_channel_id"]))
    if not channel:
        return {"sent": False, "reason": "channel_not_found"}

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, message_id FROM creator_group_sessions
            WHERE group_id = ? AND status = 'live'
        """, (group_id,))
        existing = await cursor.fetchone()

    mention = build_mention(guild, group.get("mention_type", "none"),
                             group.get("mention_role_id"))

    if existing:
        session_id, message_id = existing
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO creator_group_session_lines
                    (session_id, platform, watch_id, label, url, emoji)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, platform, watch_id) DO UPDATE SET
                    url = excluded.url, label = excluded.label, emoji = excluded.emoji
            """, (session_id, platform, watch_id, label, url, emoji))
            await db.commit()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    INSERT INTO creator_group_sessions
                        (guild_id, group_id, discord_channel_id, status)
                    VALUES (?, ?, ?, 'live')
                """, (guild_id, group_id, group["discord_channel_id"]))
                session_id = cursor.lastrowid
                await db.execute("""
                    INSERT INTO creator_group_session_lines
                        (session_id, platform, watch_id, label, url, emoji)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (session_id, platform, watch_id, label, url, emoji))
                await db.commit()
            except sqlite3.IntegrityError:
                await db.execute("ROLLBACK")
                return {"sent": False, "reason": "already_active"}
        message_id = None

    content = await _render_group_message(group, session_id, mention)

    try:
        if message_id:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(content=content)
            return {"sent": True, "message_id": msg.id}
        msg = await channel.send(content=content)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE creator_group_sessions SET message_id = ? WHERE id = ?",
                (msg.id, session_id))
            await db.commit()
        return {"sent": True, "message_id": msg.id}
    except discord.NotFound:
        # The shared message was deleted manually mid-broadcast —
        # recover by sending a fresh one rather than losing the
        # notification for every remaining/future platform in this
        # group until the session eventually closes on its own.
        try:
            msg = await channel.send(content=content)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE creator_group_sessions SET message_id = ? WHERE id = ?",
                    (msg.id, session_id))
                await db.commit()
            return {"sent": True, "message_id": msg.id}
        except Exception as e:
            return {"sent": False, "reason": f"resend_failed: {e}"}
    except Exception as e:
        print(f"[CREATOR_ENGINE] Failed to update group message "
              f"(guild={guild_id} group={group_id}): {e}")
        return {"sent": False, "reason": f"edit_failed: {e}"}


async def note_platform_offline_grouped(bot, guild_id: int, group_id: int,
                                         platform: str, watch_id: int) -> dict:
    """
    Removes this platform's line from the group's consolidated
    message. If other linked platforms are still live, edits the
    message down to just those remaining lines. If this was the last
    one, closes the group session out (edits the message to a plain
    "was live" line rather than deleting it, same "never delete
    Discord history" choice cogs/twitch.py already makes for its own
    solo ended-state edit).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, discord_channel_id, message_id FROM creator_group_sessions
            WHERE group_id = ? AND status = 'live'
        """, (group_id,))
        session_row = await cursor.fetchone()
    if not session_row:
        return {"updated": False, "reason": "no_active_group_session"}
    session_id, channel_id, message_id = session_row

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM creator_group_session_lines
            WHERE session_id = ? AND platform = ? AND watch_id = ?
        """, (session_id, platform, watch_id))
        await db.commit()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM creator_group_session_lines WHERE session_id = ?",
            (session_id,))
        remaining = (await cursor.fetchone())[0]

    guild = bot.get_guild(guild_id)
    group = await get_creator_group(group_id)

    async def _close():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE creator_group_sessions
                SET status = 'ended', ended_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (session_id,))
            await db.commit()

    if remaining > 0:
        if not guild:
            return {"updated": False, "reason": "guild_not_found"}
        channel = guild.get_channel(int(channel_id))
        if not channel or not message_id:
            return {"updated": False, "reason": "channel_or_message_not_found"}
        mention = build_mention(guild, (group or {}).get("mention_type", "none"),
                                 (group or {}).get("mention_role_id")) if group else ""
        if not group:
            return {"updated": False, "reason": "group_not_found"}
        content = await _render_group_message(group, session_id, mention)
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(content=content)
            return {"updated": True, "closed": False}
        except discord.NotFound:
            return {"updated": False, "reason": "message_deleted"}
        except Exception as e:
            print(f"[CREATOR_ENGINE] Failed to trim group message line "
                  f"(guild={guild_id} group={group_id}): {e}")
            return {"updated": False, "reason": f"edit_failed: {e}"}

    # That was the last live platform in the group.
    if guild and message_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            display_name = group["display_name"] if group else "Creator"
            try:
                msg = await channel.fetch_message(int(message_id))
                await msg.edit(content=f"⚫ **{display_name} was live**")
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"[CREATOR_ENGINE] Failed to close out group message "
                      f"(guild={guild_id} group={group_id}): {e}")
    await _close()
    return {"updated": True, "closed": True}
