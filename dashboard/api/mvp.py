import os
import json
import math
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

MAX_WEIGHT = 1_000_000   # per-word / per-minute weight sanity cap

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


def _whole_number(value, label: str, *, blank_ok: bool = False, default: int = 0) -> int:
    """Strict whole number: rejects NaN/Infinity, booleans and fractions (no silent truncation)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if blank_ok:
            return default
        raise ValueError(f"{label} is required.")
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not math.isfinite(number) or number != int(number):
        raise ValueError(f"{label} must be a whole number.")
    return int(number)


def _finite_number(value, label: str) -> float:
    """Finite non-negative decimal; rejects NaN/Infinity, which JSON parsing lets through."""
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{label} is required.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def _validated_mvp_numbers(data: dict, current: dict | None = None):
    """Returns (values, error). Ranks 1-10, rewards per rank, threshold >= 0.

    A key that is absent from ``data`` keeps the guild's stored value
    (``current``, or the engine defaults for a guild with no saved row), so a
    partial request can never silently reset another field. A key that is
    present must be valid; blanks are errors, not silent defaults.
    """
    from utils.mvp_engine import MAX_RANKS, MAX_REWARD, DEFAULTS
    base = {**DEFAULTS, **(current or {})}

    def pick(key):
        return data[key] if key in data else base[key]

    try:
        cycle_hours = _whole_number(pick("cycle_hours"), "Cycle hours")
        ranks = _whole_number(pick("winner_ranks"), "Winner ranks")
        chat_w = _finite_number(pick("chat_word_weight"), "Chat word weight")
        voice_w = _finite_number(pick("voice_minute_weight"), "Voice weight")
        min_words = _finite_number(pick("min_activity_words"), "Minimum activity")
        raw_rewards = pick("rank_rewards") or []
        if not isinstance(raw_rewards, (list, tuple)):
            return None, "Rank rewards must be a list."
        rewards = [_whole_number(v, f"Rank {i} reward", blank_ok=True)
                   for i, v in enumerate(raw_rewards[:ranks], 1)]
    except ValueError as exc:
        return None, str(exc)
    if not 1 <= cycle_hours <= 168:
        return None, "Cycle hours must be from 1 to 168."
    if not 1 <= ranks <= MAX_RANKS:
        return None, f"Winner ranks must be from 1 to {MAX_RANKS}."
    if chat_w < 0 or voice_w < 0:
        return None, "Weights cannot be negative."
    if chat_w > MAX_WEIGHT or voice_w > MAX_WEIGHT:
        return None, f"Weights must be at most {MAX_WEIGHT:,}."
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
    data     = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Send the MVP settings as a JSON object."}), 400

    from utils.mvp_engine import get_config
    current = run_async(get_config(guild_id))
    values, error = _validated_mvp_numbers(data, current)
    if error:
        return jsonify({"success": False, "error": error}), 400

    # A key that is absent keeps the stored role/channel; an explicit null/""
    # clears it. This is what stops a partial request wiping them.
    role_id = (data.get("mvp_role_id") or None) if "mvp_role_id" in data else current.get("mvp_role_id")
    channel_id = ((data.get("announce_channel_id") or None) if "announce_channel_id" in data
                  else current.get("announce_channel_id"))

    new_role = data.get("mvp_role_id")
    if new_role:
        curr_role = current.get("mvp_role_id")
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
                role_id,
                channel_id,
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



