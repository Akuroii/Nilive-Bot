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

@api_bp.route("/guild/roles")
@require_api_permission(LEVEL_MODERATOR)
def get_guild_roles():
    guild_id  = get_session_guild_id()
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"results": [], "error": "BOT_TOKEN not set"})
    resp = _req.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/roles",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=8,
    )
    if resp.status_code != 200:
        return jsonify({"results": [], "error": f"Discord {resp.status_code}"})
    roles = resp.json()
    def sort_key(r):
        if r["id"] == str(guild_id): return (2, 0)
        if r.get("managed"):         return (1, -r["position"])
        return (0, -r["position"])
    roles.sort(key=sort_key)
    return jsonify({"results": [{
        "id":       r["id"],
        "text":     r["name"],
        "color":    f"#{r['color']:06x}" if r["color"] else None,
        "position": r["position"],
        "managed":  r.get("managed", False),
    } for r in roles]})


@api_bp.route("/guild/channels")
@require_api_permission(LEVEL_MODERATOR)
def get_guild_channels():
    guild_id  = get_session_guild_id()
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"results": [], "error": "BOT_TOKEN not set"})
    resp = _req.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=8,
    )
    if resp.status_code != 200:
        return jsonify({"results": [], "error": f"Discord {resp.status_code}"})
    channels = resp.json()
    TYPE_ICON = {0:"💬",2:"🔊",4:"📁",5:"📢",10:"🧵",11:"🧵",12:"🧵",13:"🎙️",15:"📋"}
    TYPE_NAME = {0:"text",2:"voice",4:"category",5:"announcement",13:"stage",15:"forum"}
    categories = {c["id"]: c["name"] for c in channels if c["type"] == 4}
    results = []
    for ch in channels:
        if ch["type"] == 4:
            continue
        results.append({
            "id":        ch["id"],
            "text":      ch["name"],
            "type_icon": TYPE_ICON.get(ch["type"], "💬"),
            "category":  categories.get(str(ch.get("parent_id", "")), ""),
            "type":      TYPE_NAME.get(ch["type"], "text"),
        })
    type_order = {"text":0,"announcement":1,"voice":2,"stage":3,"forum":4}
    results.sort(key=lambda c: (type_order.get(c["type"], 9), c["text"].lower()))
    return jsonify({"results": results})


# Compatibility shims
@api_bp.route("/roles")
@require_api_permission(LEVEL_MODERATOR)
def get_roles():
    return get_guild_roles()


@api_bp.route("/channels")
@require_api_permission(LEVEL_MODERATOR)
def get_channels():
    return get_guild_channels()


# ── Members ───────────────────────────────────────────────────────────────────

@api_bp.route("/members/search")
@require_api_permission(LEVEL_MODERATOR)
def members_search():
    guild_id = get_session_guild_id()
    query    = request.args.get("q", "").strip()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT l.user_id, l.xp, l.level,
                       COALESCE(e.balance, 0) AS coins
                FROM levels l
                LEFT JOIN economy e
                  ON l.user_id = e.user_id AND l.guild_id = e.guild_id
                WHERE l.guild_id = ?
                ORDER BY l.xp DESC LIMIT 100
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    if query:
        rows = [r for r in rows if query in str(r[0])]
    html = ""
    for r in rows:
        # r[0] (user_id) is always numeric — safe to interpolate raw
        # into both the HTML and the inline onclick's JS-string args.
        html += (
            f"<tr>"
            f"<td><code>{r[0]}</code></td>"
            f"<td><span class='badge badge-accent'>Level {r[2]}</span></td>"
            f"<td>{r[1]:,} XP</td>"
            f"<td>🪙 {r[3]:,}</td>"
            f"<td><button class='btn btn-sm btn-secondary' "
            f"onclick=\"openEditModal('{r[0]}', {r[1]}, {r[3]})\">Edit</button></td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No members found</td></tr>"


# ── Activity (Phase 3 E1 CLOSEOUT) ─────────────────────────────────────────

@api_bp.route("/activity/<int:user_id>")
@require_api_permission(LEVEL_ADMIN)
def api_activity_user(user_id: int):
    guild_id = get_session_guild_id()
    days     = min(int(request.args.get("days", 30)), 365)

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            totals_cur = await db.execute("""
                SELECT COALESCE(SUM(messages_count), 0),
                       COALESCE(SUM(words_count), 0),
                       COALESCE(SUM(voice_minutes), 0),
                       COALESCE(SUM(forum_posts_count), 0)
                FROM activity_stats
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            totals = await totals_cur.fetchone()

            daily_cur = await db.execute("""
                SELECT date, messages_count, words_count,
                       voice_minutes, forum_posts_count
                FROM activity_stats
                WHERE guild_id = ? AND user_id = ?
                ORDER BY date DESC LIMIT ?
            """, (guild_id, user_id, days))
            daily = await daily_cur.fetchall()

        return totals, daily

    totals, daily = run_async(fetch())

    return jsonify({
        "guild_id": guild_id,
        "user_id": user_id,
        "messages_count": totals[0],
        "words_count": totals[1],
        "voice_minutes": totals[2],
        "forum_posts_count": totals[3],
        "daily": [{
            "date": r[0],
            "messages_count": r[1],
            "words_count": r[2],
            "voice_minutes": r[3],
            "forum_posts_count": r[4],
        } for r in daily],
    })


