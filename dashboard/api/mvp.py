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
    from datetime import date
    guild_id = get_session_guild_id()
    today    = date.today().isoformat()

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

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM mvp_config WHERE guild_id = ?", (guild_id,))
            row = await cursor.fetchone()
            if row:
                return dict(zip([d[0] for d in cursor.description], row))
        return {}

    return jsonify({"config": run_async(fetch())})


@api_bp.route("/mvp/config", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_mvp_config_api():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mvp_config
                    (guild_id, cycle_hours, mvp_role_id,
                     announce_channel_id, chat_word_weight,
                     voice_minute_weight, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    cycle_hours         = excluded.cycle_hours,
                    mvp_role_id         = excluded.mvp_role_id,
                    announce_channel_id = excluded.announce_channel_id,
                    chat_word_weight    = excluded.chat_word_weight,
                    voice_minute_weight = excluded.voice_minute_weight
            """, (
                guild_id,
                int(data.get("cycle_hours", 6)),
                data.get("mvp_role_id") or None,
                data.get("announce_channel_id") or None,
                float(data.get("chat_word_weight", 1.0)),
                float(data.get("voice_minute_weight", 2.0)),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated MVP config", "mvp")
    return jsonify({"success": True})


