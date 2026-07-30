import aiosqlite
from flask import jsonify, request
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.api import api_bp
from utils import creator_notify_engine as engine

# ═══════════════════════════════════════════════════════════════════════
# CREATOR HUB — dashboard CRUD for YouTube / Twitch
#
# Before this pass, dashboard/templates/config/announcements.html was
# read-only and mislabeled "Announcements" — it showed the two existing
# tables (youtube_config, twitch_config) but every add/remove/toggle had
# to go through Discord slash commands. This file is the missing
# write-side surface: full add/delete/enable-toggle for both platforms,
# same shape as every other feature's dashboard CRUD
# (dashboard/api/minigames.py, dashboard/api/missions.py).
#
# youtube_config / twitch_config already exist in database.py's central
# init_db() — reused as-is, no schema change needed for either.
#
# CREATOR pass 2: every route below now also awaits
# engine.ensure_tables() — that's what actually adds/backfills the
# video_mention_type/shorts_*/live_* columns on youtube_config and the
# mention_type column on twitch_config (see
# utils/creator_notify_engine.py's _migrate_*_config() functions).
# Calling engine.ensure_tables() at the top of every route is
# deliberately cheap-but-redundant, matching the "belt and suspenders"
# idiom already used by dashboard/api/minigames.py and
# dashboard/api/missions.py — every statement inside it is either
# CREATE ... IF NOT EXISTS or a PRAGMA-gated ALTER, so calling it once
# per request costs a few no-op checks, not a real migration re-run.
#
# A Kick integration existed in an earlier pass of this feature and has
# been dropped per direction — youtube_config/twitch_config and the
# engine's session machinery were never Kick-specific to begin with, so
# nothing here needed unwinding beyond removing Kick's own table/routes.
# ═══════════════════════════════════════════════════════════════════════


VALID_MENTION_TYPES = ("role", "everyone", "none")


def _clean_mention_type(value) -> str:
    value = (value or "none").strip().lower()
    return value if value in VALID_MENTION_TYPES else "none"


# ── YouTube ──────────────────────────────────────────────────────────────
#
# One row per watched channel. The base columns (enabled,
# discord_channel_id, custom_message, ping_role_id, video_mention_type)
# ARE the "Video" notification type — there's no separate video_enabled
# column, since Video is the original/primary type this table was built
# around, and the base `enabled` flag also gates whether the row is
# polled at all (cogs/youtube.py's check_videos query filters
# WHERE enabled = 1) — turning Video off for a watch turns off Shorts
# and Live for it too, since nothing gets polled. Shorts and Live each
# get their own enabled flag on top of that (shorts_enabled, live_enabled)
# so either can be selectively switched off while Video (and polling)
# stays on.

@api_bp.route("/creator/youtube", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_youtube_configs():
    guild_id = get_session_guild_id()

    async def fetch():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, youtube_channel_url, youtube_channel_id,
                       enabled, discord_channel_id, custom_message,
                       ping_role_id, video_mention_type,
                       shorts_enabled, shorts_discord_channel_id,
                       shorts_custom_message, shorts_mention_type,
                       shorts_mention_role_id,
                       live_enabled, live_discord_channel_id,
                       live_custom_message, live_mention_type,
                       live_mention_role_id, live_video_id,
                       creator_group_id
                FROM youtube_config WHERE guild_id = ? ORDER BY id DESC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"configs": [{
        "id": r[0], "channel_url": r[1], "channel_id": r[2],
        "enabled": bool(r[3]), "creator_group_id": r[19],
        "video": {
            "discord_channel_id": r[4], "custom_message": r[5],
            "mention_type": r[7] or "role", "mention_role_id": r[6],
        },
        "shorts": {
            "enabled": bool(r[8]), "discord_channel_id": r[9],
            "custom_message": r[10], "mention_type": r[11] or "none",
            "mention_role_id": r[12],
        },
        "live": {
            "enabled": bool(r[13]), "discord_channel_id": r[14],
            "custom_message": r[15], "mention_type": r[16] or "none",
            "mention_role_id": r[17], "is_live": bool(r[18]),
        },
    } for r in rows]})


@api_bp.route("/creator/youtube", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_youtube_config():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    url = (data.get("channel_url") or "").strip()
    discord_channel_id = data.get("discord_channel_id")
    if not url or not discord_channel_id:
        return jsonify({
            "success": False,
            "error": "A YouTube channel URL and a Discord channel are required",
        })

    async def save():
        await engine.ensure_tables()
        from cogs.youtube import extract_channel_id
        yt_channel_id = await extract_channel_id(url)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO youtube_config
                    (guild_id, youtube_channel_url, youtube_channel_id,
                     discord_channel_id, video_mention_type, enabled)
                VALUES (?, ?, ?, ?, 'none', 1)
            """, (guild_id, url, yt_channel_id, discord_channel_id))
            await db.commit()
        return yt_channel_id

    yt_channel_id = run_async(save())
    log_action(guild_id, f"Added YouTube watch: {url}", "creator")
    result = {"success": True}
    if not yt_channel_id:
        result["warning"] = (
            "Could not extract a channel ID from that URL — notifications "
            "may not work. Try the youtube.com/channel/UC... form instead.")
    return jsonify(result)


@api_bp.route("/creator/youtube/<int:entry_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_youtube_config(entry_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM youtube_config WHERE id = ? AND guild_id = ?",
                (entry_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Removed YouTube watch #{entry_id}", "creator")
    return jsonify({"success": True})


@api_bp.route("/creator/youtube/<int:entry_id>/toggle", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def toggle_youtube_config(entry_id: int):
    guild_id = get_session_guild_id()
    enabled  = int(bool((request.json or {}).get("enabled", True)))

    async def toggle():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE youtube_config SET enabled = ? WHERE id = ? AND guild_id = ?",
                (enabled, entry_id, guild_id))
            await db.commit()

    run_async(toggle())
    log_action(guild_id,
               f"{'Enabled' if enabled else 'Disabled'} YouTube watch #{entry_id}",
               "creator")
    return jsonify({"success": True})


def _save_youtube_type_settings(entry_id: int, column_prefix: str, require_channel: bool):
    """
    Shared body for the three per-type YouTube settings endpoints below.
    column_prefix is '' for Video (the base columns carry no prefix —
    they predate Shorts/Live existing at all), or 'shorts_'/'live_' for
    the other two. require_channel=True (Video only) rejects a missing
    discord_channel_id instead of silently nulling out a NOT NULL
    column; Shorts/Live leave it nullable so "no channel picked" means
    "post to the same channel as Video" (see cogs/youtube.py's
    `target_ch_id = shorts_ch_id or discord_ch_id` fallback).
    """
    guild_id = get_session_guild_id()
    data     = request.json or {}

    discord_channel_id = data.get("discord_channel_id") or None
    if require_channel and not discord_channel_id:
        return jsonify({"success": False, "error": "A Discord channel is required"})

    mention_type = _clean_mention_type(data.get("mention_type"))
    mention_role_id = data.get("mention_role_id") or None
    custom_message = data.get("custom_message") or None
    enabled = int(bool(data.get("enabled", True)))

    enabled_col  = "enabled" if not column_prefix else f"{column_prefix}enabled"
    channel_col  = f"{column_prefix}discord_channel_id" if column_prefix else "discord_channel_id"
    message_col  = f"{column_prefix}custom_message" if column_prefix else "custom_message"
    mtype_col    = f"{column_prefix}mention_type" if column_prefix else "video_mention_type"
    role_col     = f"{column_prefix}mention_role_id" if column_prefix else "ping_role_id"

    async def save():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"""
                UPDATE youtube_config
                SET {enabled_col} = ?, {channel_col} = ?, {message_col} = ?,
                    {mtype_col} = ?, {role_col} = ?
                WHERE id = ? AND guild_id = ?
            """, (enabled, discord_channel_id, custom_message,
                  mention_type, mention_role_id, entry_id, guild_id))
            await db.commit()

    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/creator/youtube/<int:entry_id>/video", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_youtube_video_settings(entry_id: int):
    result = _save_youtube_type_settings(entry_id, "", require_channel=True)
    log_action(get_session_guild_id(), f"Updated YouTube Video settings #{entry_id}", "creator")
    return result


@api_bp.route("/creator/youtube/<int:entry_id>/shorts", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_youtube_shorts_settings(entry_id: int):
    result = _save_youtube_type_settings(entry_id, "shorts_", require_channel=False)
    log_action(get_session_guild_id(), f"Updated YouTube Shorts settings #{entry_id}", "creator")
    return result


@api_bp.route("/creator/youtube/<int:entry_id>/live", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_youtube_live_settings(entry_id: int):
    result = _save_youtube_type_settings(entry_id, "live_", require_channel=False)
    log_action(get_session_guild_id(), f"Updated YouTube Live settings #{entry_id}", "creator")
    return result


# ── Twitch ───────────────────────────────────────────────────────────────

@api_bp.route("/creator/twitch", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_twitch_configs():
    guild_id = get_session_guild_id()

    async def fetch():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, twitch_username, discord_channel_id, custom_message,
                       ping_role_id, mention_type, give_role_id,
                       role_duration_hours, is_live, enabled, creator_group_id
                FROM twitch_config WHERE guild_id = ? ORDER BY id DESC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"configs": [{
        "id": r[0], "username": r[1], "discord_channel_id": r[2],
        "custom_message": r[3], "mention_role_id": r[4],
        "mention_type": r[5] or "none", "give_role_id": r[6],
        "role_duration_hours": r[7],
        "is_live": bool(r[8]), "enabled": bool(r[9]),
        "creator_group_id": r[10],
    } for r in rows]})


@api_bp.route("/creator/twitch", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_twitch_config():
    import os
    guild_id = get_session_guild_id()
    data     = request.json or {}

    username = (data.get("username") or "").strip().lower().lstrip("@")
    discord_channel_id = data.get("discord_channel_id")
    if not username or not discord_channel_id:
        return jsonify({
            "success": False,
            "error": "A Twitch username and a Discord channel are required",
        })

    mention_type = _clean_mention_type(data.get("mention_type"))
    mention_role_id = data.get("mention_role_id") or None

    result = {"success": True}
    if not os.getenv("TWITCH_CLIENT_ID"):
        result["warning"] = (
            "TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET aren't set on Railway — "
            "this entry will save but won't check live status until they are.")

    async def save():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO twitch_config
                    (guild_id, twitch_username, discord_channel_id, custom_message,
                     ping_role_id, mention_type, give_role_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (guild_id, username, discord_channel_id,
                  data.get("custom_message") or None,
                  mention_role_id, mention_type,
                  data.get("give_role_id") or None))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added Twitch watch: {username}", "creator")
    return jsonify(result)


@api_bp.route("/creator/twitch/<int:entry_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_twitch_config(entry_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM twitch_config WHERE id = ? AND guild_id = ?",
                (entry_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Removed Twitch watch #{entry_id}", "creator")
    return jsonify({"success": True})


@api_bp.route("/creator/twitch/<int:entry_id>/toggle", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def toggle_twitch_config(entry_id: int):
    guild_id = get_session_guild_id()
    enabled  = int(bool((request.json or {}).get("enabled", True)))

    async def toggle():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE twitch_config SET enabled = ? WHERE id = ? AND guild_id = ?",
                (enabled, entry_id, guild_id))
            await db.commit()

    run_async(toggle())
    log_action(guild_id,
               f"{'Enabled' if enabled else 'Disabled'} Twitch watch #{entry_id}",
               "creator")
    return jsonify({"success": True})


@api_bp.route("/creator/twitch/<int:entry_id>/settings", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_twitch_settings(entry_id: int):
    """
    Previously the only way to change a Twitch watch's channel, message,
    or ping after creation was to delete it and re-add it (losing its
    is_live/history in the process). This is the missing edit surface —
    covers every field that made sense to add at creation (custom_message,
    mention_type/mention_role_id, give_role_id, role_duration_hours) plus
    the target Discord channel. Renaming the tracked username stays a
    delete-and-re-add — a different username is a different entity, not
    an edit of this one.
    """
    guild_id = get_session_guild_id()
    data     = request.json or {}

    discord_channel_id = data.get("discord_channel_id")
    if not discord_channel_id:
        return jsonify({"success": False, "error": "A Discord channel is required"})

    mention_type = _clean_mention_type(data.get("mention_type"))

    async def save():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE twitch_config
                SET discord_channel_id = ?, custom_message = ?,
                    ping_role_id = ?, mention_type = ?,
                    give_role_id = ?, role_duration_hours = ?
                WHERE id = ? AND guild_id = ?
            """, (discord_channel_id, data.get("custom_message") or None,
                  data.get("mention_role_id") or None, mention_type,
                  data.get("give_role_id") or None,
                  data.get("role_duration_hours") or None,
                  entry_id, guild_id))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Updated Twitch settings #{entry_id}", "creator")
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════
# Creator Groups — consolidated cross-platform live notifications
# (CREATOR pass 3, additive-only — see utils/creator_notify_engine.py's
# "CREATOR GROUPS" section for the full design and utils/mission_engine.py
# -style header comments already established elsewhere in this codebase).
#
# A group is a named bundle of existing YouTube/Twitch watches
# (e.g. "MeowlyVA" = one twitch_config row + one youtube_config row)
# that announce as a single shared message instead of N independent
# ones. Creating a group and linking watches to it is entirely
# separate from each platform's own add/settings endpoints above —
# nothing about those changes, and a watch with no group link (every
# watch that exists today) behaves exactly as it always has.
# ═══════════════════════════════════════════════════════════════════════

_PLATFORM_TABLES = {
    "youtube": ("youtube_config", "youtube_channel_url"),
    "twitch": ("twitch_config", "twitch_username"),
}


async def _linked_watches(guild_id: int, group_id: int) -> list[dict]:
    watches = []
    async with aiosqlite.connect(DB_PATH) as db:
        for platform, (table, label_col) in _PLATFORM_TABLES.items():
            try:
                cursor = await db.execute(f"""
                    SELECT id, {label_col} FROM {table}
                    WHERE guild_id = ? AND creator_group_id = ?
                """, (guild_id, group_id))
                rows = await cursor.fetchall()
            except Exception:
                # Table doesn't exist yet on this guild — no matches,
                # not an error.
                rows = []
            for wid, label in rows:
                watches.append({"platform": platform, "id": wid, "label": label})
    return watches


@api_bp.route("/creator/groups", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_creator_groups():
    guild_id = get_session_guild_id()

    async def fetch():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, display_name, discord_channel_id,
                       mention_type, mention_role_id, enabled
                FROM creator_groups WHERE guild_id = ? ORDER BY id DESC
            """, (guild_id,))
            groups = await cursor.fetchall()

        result = []
        for g in groups:
            watches = await _linked_watches(guild_id, g[0])
            result.append({
                "id": g[0], "display_name": g[1], "discord_channel_id": g[2],
                "mention_type": g[3] or "none", "mention_role_id": g[4],
                "enabled": bool(g[5]), "watches": watches,
            })
        return result

    return jsonify({"groups": run_async(fetch())})


@api_bp.route("/creator/groups", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_creator_group():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    display_name = (data.get("display_name") or "").strip()
    discord_channel_id = data.get("discord_channel_id")
    if not display_name or not discord_channel_id:
        return jsonify({
            "success": False,
            "error": "A display name and a Discord channel are required",
        })

    mention_type = _clean_mention_type(data.get("mention_type"))
    mention_role_id = data.get("mention_role_id") or None

    async def save():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO creator_groups
                    (guild_id, display_name, discord_channel_id,
                     mention_type, mention_role_id, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (guild_id, display_name, discord_channel_id,
                  mention_type, mention_role_id))
            await db.commit()
            return cursor.lastrowid

    new_id = run_async(save())
    log_action(guild_id, f"Created creator group: {display_name}", "creator")
    return jsonify({"success": True, "id": new_id})


@api_bp.route("/creator/groups/<int:group_id>", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def update_creator_group(group_id: int):
    """
    Edits an existing group's name/channel/mention. Does NOT touch
    which watches are linked to it — that's the separate
    /<platform>/<id>/group endpoint below, so relinking watches never
    risks clobbering the group's own settings and vice versa.
    """
    guild_id = get_session_guild_id()
    data     = request.json or {}

    display_name = (data.get("display_name") or "").strip()
    discord_channel_id = data.get("discord_channel_id")
    if not display_name or not discord_channel_id:
        return jsonify({
            "success": False,
            "error": "A display name and a Discord channel are required",
        })

    mention_type = _clean_mention_type(data.get("mention_type"))

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE creator_groups
                SET display_name = ?, discord_channel_id = ?,
                    mention_type = ?, mention_role_id = ?
                WHERE id = ? AND guild_id = ?
            """, (display_name, discord_channel_id, mention_type,
                  data.get("mention_role_id") or None, group_id, guild_id))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Updated creator group #{group_id}", "creator")
    return jsonify({"success": True})


@api_bp.route("/creator/groups/<int:group_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_creator_group(group_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            # Unlink every watch first so nothing is left pointing at
            # a group_id that no longer exists — get_watch_group()
            # would already treat a dangling reference as "not
            # grouped" and fail safe, but there's no reason to leave
            # stale data sitting there.
            for platform, (table, _label_col) in _PLATFORM_TABLES.items():
                try:
                    await db.execute(
                        f"UPDATE {table} SET creator_group_id = NULL "
                        f"WHERE guild_id = ? AND creator_group_id = ?",
                        (guild_id, group_id))
                except Exception:
                    pass
            await db.execute(
                "DELETE FROM creator_groups WHERE id = ? AND guild_id = ?",
                (group_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Deleted creator group #{group_id}", "creator")
    return jsonify({"success": True})


@api_bp.route("/creator/groups/<int:group_id>/toggle", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def toggle_creator_group(group_id: int):
    guild_id = get_session_guild_id()
    enabled  = int(bool((request.json or {}).get("enabled", True)))

    async def toggle():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE creator_groups SET enabled = ? WHERE id = ? AND guild_id = ?",
                (enabled, group_id, guild_id))
            await db.commit()

    run_async(toggle())
    log_action(guild_id,
               f"{'Enabled' if enabled else 'Disabled'} creator group #{group_id}",
               "creator")
    return jsonify({"success": True})


@api_bp.route("/creator/<platform>/<int:entry_id>/group", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def set_watch_group(platform: str, entry_id: int):
    """
    Links (or, with group_id=null, unlinks) one existing YouTube/Twitch
    watch to a Creator Group. This is the ONLY thing that changes a
    watch's notification style — there's no separate style dropdown to
    keep in sync with it. Linked = posts into the group's shared
    message; unlinked (group_id null, the default) = posts its own
    independent message exactly as before this feature existed.
    """
    if platform not in _PLATFORM_TABLES:
        return jsonify({"success": False, "error": f"Unknown platform: {platform}"})

    guild_id = get_session_guild_id()
    data     = request.json or {}
    group_id = data.get("group_id") or None
    table, _label_col = _PLATFORM_TABLES[platform]

    async def save():
        await engine.ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            if group_id:
                # Guard against linking to a group from a DIFFERENT
                # guild (e.g. a stale id from a previous selection) —
                # confirm it's actually this guild's before writing it.
                cursor = await db.execute(
                    "SELECT id FROM creator_groups WHERE id = ? AND guild_id = ?",
                    (group_id, guild_id))
                if not await cursor.fetchone():
                    return False
            await db.execute(
                f"UPDATE {table} SET creator_group_id = ? "
                f"WHERE id = ? AND guild_id = ?",
                (group_id, entry_id, guild_id))
            await db.commit()
            return True

    ok = run_async(save())
    if not ok:
        return jsonify({"success": False, "error": "Group not found"})

    log_action(guild_id,
               f"{'Linked' if group_id else 'Unlinked'} {platform} watch "
               f"#{entry_id} {'to' if group_id else 'from'} creator group",
               "creator")
    return jsonify({"success": True})
