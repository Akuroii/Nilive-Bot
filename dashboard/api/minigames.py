import aiosqlite
from flask import jsonify, request
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission,
    LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Minigames / Event Stack Builder (dark-fixes pass #13) ──────────────────
#
# cogs/minigames.py is currently Discord-command-only
# (/minigames_setup, /minigames_tier_add, etc) — this is the missing
# dashboard CRUD surface, same shape as dashboard/api/leveling.py's
# prestige routes (config GET/POST + a related sub-resource
# GET/POST/DELETE).
#
# Reuses cogs.minigames' own ensure_tables()/get_config()/get_tiers()
# rather than re-declaring the schema or query logic here — same
# principle as dashboard/api/leveling.py importing
# utils.xp_calculator.get_prestige_config() instead of duplicating it.
# Importing cogs.minigames is safe from the Flask process: the module
# only defines classes/functions at import time, it never touches a
# live discord.Client or event loop until a Cog is actually
# instantiated and added to a running bot.
#
# ensure_tables() is awaited before every read/write below because the
# dashboard process only ever runs database.py's central init_db() —
# it does NOT run cogs.minigames.Minigames.cog_load(), which is what
# creates minigames_config/minigames_tiers/minigames_log on the BOT
# process. If the dashboard is opened before the bot has completed at
# least one cog_load, these tables would not exist yet and every query
# below would fail with "no such table". ensure_tables() is pure
# CREATE TABLE IF NOT EXISTS, so awaiting it here is a cheap, safe,
# idempotent guard — it does not conflict with the bot process running
# the identical statements concurrently.


@api_bp.route("/minigames/config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigames_config_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from cogs.minigames import ensure_tables, get_config
        await ensure_tables()
        return await get_config(guild_id)

    return jsonify({"config": run_async(fetch())})


@api_bp.route("/minigames/config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_minigames_config_api():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    try:
        min_events = int(data.get("min_events_per_week", 5))
        max_events = int(data.get("max_events_per_week", 10))
        claim_seconds = int(data.get("claim_seconds", 300))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "min/max events and claim_seconds must be numbers"})

    if min_events < 1:
        return jsonify({"success": False, "error": "min_events_per_week must be at least 1"})
    if max_events < min_events:
        return jsonify({"success": False, "error": "max_events_per_week must be >= min_events_per_week"})
    if claim_seconds < 30:
        return jsonify({"success": False, "error": "claim_seconds must be at least 30"})

    channel_id = data.get("channel_id") or None
    enabled    = int(bool(data.get("enabled", True)))

    async def save():
        from cogs.minigames import ensure_tables
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO minigames_config
                    (guild_id, enabled, channel_id, min_events_per_week,
                     max_events_per_week, claim_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled              = excluded.enabled,
                    channel_id           = excluded.channel_id,
                    min_events_per_week  = excluded.min_events_per_week,
                    max_events_per_week  = excluded.max_events_per_week,
                    claim_seconds        = excluded.claim_seconds,
                    updated_at           = CURRENT_TIMESTAMP
            """, (guild_id, enabled, channel_id, min_events,
                  max_events, claim_seconds))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated Event Stack Builder config", "minigames")
    return jsonify({"success": True})


@api_bp.route("/minigames/tiers", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_minigames_tiers_api():
    guild_id = get_session_guild_id()

    async def fetch():
        from cogs.minigames import ensure_tables
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, tier, weight, reward_type, reward_value,
                       reward_duration_hours, enabled
                FROM minigames_tiers WHERE guild_id = ?
                ORDER BY tier ASC, id ASC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"tiers": [{
        "id": r[0], "tier": r[1], "weight": r[2],
        "reward_type": r[3], "reward_value": r[4],
        "reward_duration_hours": r[5], "enabled": r[6],
    } for r in rows]})


@api_bp.route("/minigames/tier", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_minigames_tier_api():
    from cogs.minigames import VALID_TIERS
    guild_id = get_session_guild_id()
    data     = request.json or {}

    tier = (data.get("tier") or "").lower().strip()
    if tier not in VALID_TIERS:
        return jsonify({"success": False,
                        "error": f"tier must be one of: {', '.join(VALID_TIERS)}"})

    reward_type = data.get("reward_type")
    valid_reward_types = ("coins", "diamonds", "xp", "role", "temp_role", "item")
    if reward_type not in valid_reward_types:
        return jsonify({"success": False,
                        "error": f"reward_type must be one of: {', '.join(valid_reward_types)}"})

    reward_value = data.get("reward_value")
    if not reward_value:
        return jsonify({"success": False, "error": "reward_value is required"})

    try:
        weight = max(1, int(data.get("weight", 1)))
    except (TypeError, ValueError):
        weight = 1

    duration_hours = data.get("reward_duration_hours")
    try:
        duration_hours = int(duration_hours) if duration_hours else None
    except (TypeError, ValueError):
        duration_hours = None

    async def save():
        from cogs.minigames import ensure_tables
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO minigames_tiers
                    (guild_id, tier, weight, reward_type, reward_value,
                     reward_duration_hours)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (guild_id, tier, weight, reward_type,
                  str(reward_value), duration_hours))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added minigame tier: {tier} ({reward_type})", "minigames")
    return jsonify({"success": True})


@api_bp.route("/minigames/tier/<int:tier_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_minigames_tier_api(tier_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM minigames_tiers WHERE id = ? AND guild_id = ?",
                (tier_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Removed minigame tier #{tier_id}", "minigames")
    return jsonify({"success": True})


@api_bp.route("/minigames/log")
@require_api_permission(LEVEL_ADMIN)
def get_minigames_log_api():
    guild_id = get_session_guild_id()
    limit    = min(int(request.args.get("limit", 25)), 100)

    async def fetch():
        from cogs.minigames import ensure_tables
        await ensure_tables()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT event_date, tier, winner_id, winner_display_name,
                       forced, fired_at
                FROM minigames_log
                WHERE guild_id = ?
                ORDER BY fired_at DESC LIMIT ?
            """, (guild_id, limit))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"log": [{
        "event_date": r[0], "tier": r[1], "winner_id": r[2],
        "winner_display_name": r[3], "forced": bool(r[4]),
        "fired_at": r[5],
    } for r in rows]})
