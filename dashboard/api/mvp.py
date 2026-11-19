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

# ── MVP ───────────────────────────────────────────────────────────────────────

@api_bp.route("/mvp/scores")
@require_api_permission(LEVEL_ADMIN)
def mvp_scores_partial():
    from utils.timezone import get_cairo_daily_key
    guild_id = get_session_guild_id()
    today    = get_cairo_daily_key()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, message_score, voice_minutes, total_score
                FROM mvp_scores
                WHERE guild_id = ? AND date = ?
                ORDER BY total_score DESC LIMIT 20
            """, (guild_id, today))
            return await cursor.fetchall()

    rows = run_async(fetch())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [r[0] for r in rows])

    user_map = run_async(resolve()) if rows else {}

    from dashboard.utils.user_identity import render_user_identity_html
    html = ""
    for i, r in enumerate(rows, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
        u = user_map.get(r[0], {})
        identity_html = render_user_identity_html(
            r[0], u.get("display_name"), u.get("username"), u.get("avatar_url"))
        html += (
            f"<tr><td>{medal}</td>"
            f"<td>{identity_html}</td>"
            f"<td>{r[1]:.1f}</td>"
            f"<td>{r[2]:.1f}</td>"
            f"<td><strong>{r[3]:.1f}</strong></td></tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No activity today yet</td></tr>"


@api_bp.route("/mvp/config", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_mvp_config_api():
    guild_id = get_session_guild_id()
    from utils.mvp_engine import get_config, threshold_points, threshold_voice_minutes

    async def fetch():
        cfg = await get_config(guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            exists = await (await db.execute(
                "SELECT 1 FROM mvp_config WHERE guild_id = ?", (guild_id,))).fetchone()
        if not exists:
            return {}
        for k in ("mvp_role_id", "announce_channel_id"):
            if cfg.get(k) is not None:
                cfg[k] = str(cfg[k])
        cfg["guild_id"] = str(guild_id)
        cfg["min_activity_points"] = threshold_points(cfg)
        cfg["min_activity_voice_minutes"] = threshold_voice_minutes(cfg)
        return cfg

    return jsonify({"config": run_async(fetch())})


def _validated_mvp_numbers(data: dict):
    """Returns (values, error). Ranks 1-10, rewards per rank, threshold >= 0."""
    from utils.mvp_engine import MAX_RANKS, MAX_REWARD
    try:
        cycle_hours = int(data.get("cycle_hours", 6))
        ranks = int(data.get("winner_ranks", 1))
        chat_w = float(data.get("chat_word_weight", 1.0))
        voice_w = float(data.get("voice_minute_weight", 2.0))
        min_words = float(data.get("min_activity_words", 0) or 0)
        raw_rewards = data.get("rank_rewards") or []
        rewards = [int(v or 0) for v in raw_rewards][:ranks]
    except (TypeError, ValueError):
        return None, "Cycle hours, ranks, rewards, weights and the activity minimum must be numbers."
    if not 1 <= cycle_hours <= 168:
        return None, "Cycle hours must be from 1 to 168."
    if not 1 <= ranks <= MAX_RANKS:
        return None, f"Winner ranks must be from 1 to {MAX_RANKS}."
    if chat_w < 0 or voice_w < 0:
        return None, "Weights cannot be negative."
    if min_words < 0 or min_words > 10_000_000:
        return None, "The minimum activity must be from 0 to 10,000,000 words."
    rewards += [0] * (ranks - len(rewards))
    if any(v < 0 or v > MAX_REWARD for v in rewards):
        return None, f"Each rank reward must be from 0 to {MAX_REWARD:,} Coins."
    return {"cycle_hours": cycle_hours, "ranks": ranks, "chat_w": chat_w,
            "voice_w": voice_w, "min_words": min_words, "rewards": rewards}, None


@api_bp.route("/mvp/config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_mvp_config_api():
    guild_id = get_session_guild_id()
    data     = request.json or {}

    values, error = _validated_mvp_numbers(data)
    if error:
        return jsonify({"success": False, "error": error}), 400

    new_role = data.get("mvp_role_id")
    if new_role:
        async def get_curr():
            async with aiosqlite.connect(DB_PATH) as db:
                c = await db.execute("SELECT mvp_role_id FROM mvp_config WHERE guild_id = ?", (guild_id,))
                row = await c.fetchone()
                return row[0] if row else None
        curr_role = run_async(get_curr())
        from dashboard.utils.role_validation import validate_assignable_role
        valid, err = validate_assignable_role(guild_id, new_role, curr_role)
        if not valid:
            return jsonify({"success": False, "error": f"Invalid MVP Role: {err}"}), 400

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_config
                    (guild_id, cycle_hours, mvp_role_id,
                     announce_channel_id, chat_word_weight,
                     voice_minute_weight, enabled,
                     winner_ranks, rank_rewards, min_activity_words)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    cycle_hours         = excluded.cycle_hours,
                    mvp_role_id         = excluded.mvp_role_id,
                    announce_channel_id = excluded.announce_channel_id,
                    chat_word_weight    = excluded.chat_word_weight,
                    voice_minute_weight = excluded.voice_minute_weight,
                    winner_ranks        = excluded.winner_ranks,
                    rank_rewards        = excluded.rank_rewards,
                    min_activity_words  = excluded.min_activity_words
            """, (
                guild_id,
                values["cycle_hours"],
                data.get("mvp_role_id") or None,
                data.get("announce_channel_id") or None,
                values["chat_w"],
                values["voice_w"],
                values["ranks"],
                json.dumps(values["rewards"]),
                values["min_words"],
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated MVP config", "mvp")
    return jsonify({"success": True})



