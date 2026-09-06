import os
import json
import csv
import io
import datetime
import requests as _req
import aiosqlite
from flask import jsonify, request, session, abort, Response
from markupsafe import escape as _esc
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.auth import login_required, current_user_id, current_user
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission,
    LEVEL_OWNER, LEVEL_ADMIN, LEVEL_MODERATOR,
)
from dashboard.api import api_bp

# ── Leveling ──────────────────────────────────────────────────────────────────

@api_bp.route("/leveling/leaderboard")
@require_api_permission(LEVEL_ADMIN)
def leveling_leaderboard_partial():
    guild_id = get_session_guild_id()

    # Finalized Prestige: legacy levels.prestige above the permanent max is
    # treated as V for ranking/badge display (never rewritten).
    from utils.prestige import MAX_PERMANENT_TIER

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level, prestige FROM levels
                WHERE guild_id = ?
                ORDER BY MIN(prestige, ?) DESC, xp DESC LIMIT 50
            """, (guild_id, MAX_PERMANENT_TIER))
            return await cursor.fetchall()

    rows = run_async(fetch())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [r[0] for r in rows])

    user_map = run_async(resolve()) if rows else {}

    from dashboard.utils.user_identity import render_user_identity_html
    html = ""
    for i, r in enumerate(rows, 1):
        prestige = min(r[3] or 0, MAX_PERMANENT_TIER)
        badge = f"<span class='badge badge-accent'>★{prestige}</span>" if prestige else ""
        u = user_map.get(r[0], {})
        identity_html = render_user_identity_html(
            r[0], u.get("display_name"), u.get("username"), u.get("avatar_url"))
        html += (
            f"<tr><td>#{i}</td>"
            f"<td><div style='display:flex;align-items:center;gap:8px;'>{badge}{identity_html}</div></td>"
            f"<td><span class='badge badge-accent'>Lv {r[2]}</span></td>"
            f"<td>{r[1]:,} XP</td></tr>"
        )
    return html or "<tr><td colspan='4' class='empty'>No data yet</td></tr>"


@api_bp.route("/leveling/config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_leveling_config_api():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM leveling_config WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if row:
                return dict(zip([d[0] for d in cursor.description], row))
        return {}

    return jsonify({"config": run_async(fetch())})


@api_bp.route("/leveling/config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_leveling_config_api():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO leveling_config
                    (guild_id, enabled, xp_per_word,
                     xp_min_per_message, xp_max_per_message,
                     xp_cooldown_seconds, voice_xp_enabled,
                     voice_xp_per_minute, voice_require_unmuted,
                     spam_detection_enabled, spam_threshold,
                     spam_xp_penalty, levelup_announce,
                     levelup_channel_id, levelup_message,
                     remove_old_reward_role)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled                = excluded.enabled,
                    xp_per_word            = excluded.xp_per_word,
                    xp_min_per_message     = excluded.xp_min_per_message,
                    xp_max_per_message     = excluded.xp_max_per_message,
                    xp_cooldown_seconds    = excluded.xp_cooldown_seconds,
                    voice_xp_enabled       = excluded.voice_xp_enabled,
                    voice_xp_per_minute    = excluded.voice_xp_per_minute,
                    voice_require_unmuted  = excluded.voice_require_unmuted,
                    spam_detection_enabled = excluded.spam_detection_enabled,
                    spam_threshold         = excluded.spam_threshold,
                    spam_xp_penalty        = excluded.spam_xp_penalty,
                    levelup_announce       = excluded.levelup_announce,
                    levelup_channel_id     = excluded.levelup_channel_id,
                    levelup_message        = excluded.levelup_message,
                    remove_old_reward_role = excluded.remove_old_reward_role,
                    updated_at             = CURRENT_TIMESTAMP
            """, (
                guild_id,
                int(data.get("enabled", 1)),
                int(data.get("xp_per_word", 1)),
                int(data.get("xp_min_per_message", 5)),
                int(data.get("xp_max_per_message", 50)),
                int(data.get("xp_cooldown_seconds", 30)),
                int(data.get("voice_xp_enabled", 1)),
                int(data.get("voice_xp_per_minute", 3)),
                int(data.get("voice_require_unmuted", 1)),
                int(data.get("spam_detection_enabled", 1)),
                int(data.get("spam_threshold", 3)),
                int(data.get("spam_xp_penalty", 10)),
                int(data.get("levelup_announce", 1)),
                data.get("levelup_channel_id") or None,
                data.get("levelup_message") or None,
                int(data.get("remove_old_reward_role", 0)),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated leveling config", "leveling")
    return jsonify({"success": True})


@api_bp.route("/leveling/reward", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_leveling_reward():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO leveling_rewards (guild_id, level, role_id)
                VALUES (?, ?, ?)
            """, (guild_id, int(data.get("level")), data.get("role_id")))
            await db.commit()

    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/leveling/reward/<int:reward_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_leveling_reward(reward_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM leveling_rewards WHERE id=? AND guild_id=?",
                (reward_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/leveling/currency-rewards", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_leveling_currency_rewards():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, level, currency, amount
                FROM leveling_currency_rewards
                WHERE guild_id = ? ORDER BY level ASC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"rewards": [{
        "id": r[0], "level": r[1], "currency": r[2], "amount": r[3],
    } for r in rows]})


@api_bp.route("/leveling/currency-reward", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_leveling_currency_reward():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    try:
        level = int(data.get("level"))
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Level and amount must be numbers"})
    if level <= 0 or amount <= 0:
        return jsonify({"success": False, "error": "Level and amount must be positive"})

    currency = data.get("currency", "balance")
    if currency not in ("balance", "diamonds"):
        return jsonify({"success": False, "error": "Currency must be 'balance' or 'diamonds'"})

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount)
                VALUES (?, ?, ?, ?)
            """, (guild_id, level, currency, amount))
            await db.commit()

    run_async(save())
    log_action(guild_id,
               f"Added level {level} currency reward: {amount} {currency}",
               "leveling")
    return jsonify({"success": True})


@api_bp.route("/leveling/currency-reward/<int:reward_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_leveling_currency_reward(reward_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM leveling_currency_rewards WHERE id=? AND guild_id=?",
                (reward_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/leveling/reset-config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_leveling_reset_config():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT enabled, period, last_reset
                FROM leveling_reset_config WHERE guild_id = ?
            """, (guild_id,))
            row = await cursor.fetchone()
        if not row:
            return {"enabled": 0, "period": "weekly", "last_reset": None}
        return {"enabled": row[0], "period": row[1], "last_reset": row[2]}

    return jsonify({"config": run_async(fetch())})


@api_bp.route("/leveling/reset-config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_leveling_reset_config():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    period   = data.get("period", "weekly")
    if period not in ("weekly", "monthly"):
        return jsonify({"success": False, "error": "Period must be 'weekly' or 'monthly'"})
    enabled = int(bool(data.get("enabled")))

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT last_reset FROM leveling_reset_config WHERE guild_id = ?",
                (guild_id,))
            existing = await cursor.fetchone()
            last_reset = existing[0] if existing and existing[0] else \
                datetime.datetime.now(datetime.timezone.utc).isoformat()

            await db.execute("""
                INSERT INTO leveling_reset_config (guild_id, enabled, period, last_reset)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    period  = excluded.period
            """, (guild_id, enabled, period, last_reset))
            await db.commit()

    run_async(save())
    log_action(guild_id,
               f"{'Enabled' if enabled else 'Disabled'} {period} leaderboard reset",
               "leveling")
    return jsonify({"success": True})


@api_bp.route("/leveling/force-reset", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def force_leveling_reset():
    guild_id = get_session_guild_id()

    async def run():
        from cogs.leveling import perform_leaderboard_reset, get_reset_config
        cfg = await get_reset_config(guild_id)
        period = cfg.get("period") or "weekly"
        count = await perform_leaderboard_reset(guild_id, period)
        return count

    count = run_async(run())
    log_action(guild_id, f"Forced leaderboard reset ({count} members)", "leveling")
    return jsonify({"success": True, "count": count})


@api_bp.route("/leveling/bonus-roles", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_bonus_roles():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, role_id, multiplier FROM leveling_bonus_roles
                WHERE guild_id = ? ORDER BY multiplier DESC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"roles": [
        {"id": r[0], "role_id": r[1], "multiplier": r[2]} for r in rows]})


@api_bp.route("/leveling/bonus-role", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_bonus_role():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO leveling_bonus_roles (guild_id, role_id, multiplier)
                VALUES (?, ?, ?)
            """, (guild_id, data.get("role_id"), float(data.get("multiplier", 1.5))))
            await db.commit()

    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/leveling/bonus-role/<int:role_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_bonus_role(role_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM leveling_bonus_roles WHERE id=? AND guild_id=?",
                (role_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/leveling/blacklist", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_blacklist():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, role_id FROM leveling_blacklist_roles
                WHERE guild_id = ?
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    return jsonify({"roles": [{"id": r[0], "role_id": r[1]} for r in rows]})


@api_bp.route("/leveling/blacklist", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_blacklist():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO leveling_blacklist_roles (guild_id, role_id)
                VALUES (?, ?)
            """, (guild_id, data.get("role_id")))
            await db.commit()

    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/leveling/blacklist/<int:entry_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_blacklist(entry_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM leveling_blacklist_roles WHERE id=? AND guild_id=?",
                (entry_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


# ── Prestige (finalized Prestige system) ────────────────────────────────────
#
# Same LEVEL_ADMIN gate + guild-scoped CRUD shape as the leveling
# reward/bonus-role/blacklist routes directly above. All state and rule
# enforcement (purchase, sequential progression, effective/booster VI,
# multipliers) live in utils/prestige.py. These routes are only the
# dashboard CRUD surface for prestige_config (enabled + per-tier
# multipliers) and prestige_roles (tier -> role_id mapping).

@api_bp.route("/leveling/prestige-config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_prestige_config_api():
    from utils.prestige import get_prestige_config as _get_prestige_config
    guild_id = get_session_guild_id()
    return jsonify({"config": run_async(_get_prestige_config(guild_id))})


@api_bp.route("/leveling/prestige-config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_prestige_config_api():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    enabled  = int(bool(data.get("enabled", True)))

    # Optional per-tier multipliers, keyed by tier number (1..6). Each is
    # {"coins": float, "diamonds": float}. Blank/invalid entries are skipped
    # so a partial save never clobbers a tier with garbage.
    tiers = None
    raw_tiers = data.get("tiers")
    if isinstance(raw_tiers, dict):
        tiers = {}
        for k, v in raw_tiers.items():
            try:
                tier = int(k)
            except (TypeError, ValueError):
                continue
            if tier not in (1, 2, 3, 4, 5, 6):
                continue
            if not isinstance(v, dict):
                continue
            try:
                tiers[tier] = {
                    "coins": max(0.0, float(v.get("coins", 1.0))),
                    "diamonds": max(0.0, float(v.get("diamonds", 1.0))),
                }
            except (TypeError, ValueError):
                continue

    from utils.prestige import set_prestige_multipliers

    async def save():
        await set_prestige_multipliers(guild_id, enabled=enabled, tiers=tiers)

    run_async(save())
    log_action(guild_id,
               f"Updated prestige config: enabled={bool(enabled)}",
               "leveling")
    return jsonify({"success": True})


@api_bp.route("/leveling/prestige-roles", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_prestige_roles_api():
    from utils.prestige import get_prestige_roles as _get_prestige_roles
    guild_id = get_session_guild_id()
    return jsonify({"roles": run_async(_get_prestige_roles(guild_id))})


@api_bp.route("/leveling/prestige-role", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_prestige_role_api():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    try:
        tier = int(data.get("tier"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "tier must be a number"})
    role_id = data.get("role_id")
    if tier <= 0 or not role_id:
        return jsonify({"success": False, "error": "tier and role_id are required"})

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO prestige_roles (guild_id, tier, role_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, tier) DO UPDATE SET
                    role_id = excluded.role_id
            """, (guild_id, tier, role_id))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Set prestige tier {tier} role", "leveling")
    return jsonify({"success": True})


@api_bp.route("/leveling/prestige-role/<int:entry_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_prestige_role_api(entry_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM prestige_roles WHERE id=? AND guild_id=?",
                (entry_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


