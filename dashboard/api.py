import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import aiosqlite
import asyncio
from flask import Blueprint, jsonify, request, session, abort
from database import DB_PATH
from dashboard.auth import login_required, current_user_id
from dashboard.permissions import get_session_guild_id

api_bp = Blueprint("api", __name__, url_prefix="/api")


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def require_guild(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            abort(401)
        if not get_session_guild_id():
            abort(400)
        return f(*args, **kwargs)
    return decorated


@api_bp.route("/guild/roles")
@require_guild
def get_guild_roles():
    """
    Returns all roles in the guild via Discord Bot API.
    Response: { results: [{id, text, color, position, managed}] }
    Sorted: custom roles by position desc, then managed/bot roles, then @everyone last.
    """
    guild_id    = get_session_guild_id()
    bot_token   = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"results": [], "error": "BOT_TOKEN not set"})

    import requests as _req
    resp = _req.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/roles",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=8,
    )
    if resp.status_code != 200:
        return jsonify({"results": [], "error": f"Discord API {resp.status_code}"})

    roles = resp.json()
    # Sort: custom roles (highest position first), managed last, @everyone last
    def sort_key(r):
        if r["id"] == str(guild_id):   return (2, 0)   # @everyone
        if r.get("managed"):           return (1, -r["position"])
        return (0, -r["position"])

    roles.sort(key=sort_key)

    results = []
    for r in roles:
        color_hex = f"#{r['color']:06x}" if r["color"] else None
        results.append({
            "id":       r["id"],
            "text":     r["name"],
            "color":    color_hex,
            "position": r["position"],
            "managed":  r.get("managed", False),
        })

    return jsonify({"results": results})


@api_bp.route("/guild/channels")
@require_guild
def get_guild_channels():
    """
    Returns all channels in the guild via Discord Bot API.
    Response: { results: [{id, text, type_icon, category, type}] }
    """
    guild_id  = get_session_guild_id()
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"results": [], "error": "BOT_TOKEN not set"})

    import requests as _req
    resp = _req.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        headers={"Authorization": f"Bot {bot_token}"},
        timeout=8,
    )
    if resp.status_code != 200:
        return jsonify({"results": [], "error": f"Discord API {resp.status_code}"})

    channels = resp.json()

    TYPE_ICON = {
        0:  "💬",   # text
        2:  "🔊",   # voice
        4:  "📁",   # category
        5:  "📢",   # announcement
        10: "🧵",   # announcement thread
        11: "🧵",   # public thread
        12: "🧵",   # private thread
        13: "🎙️",  # stage
        15: "📋",   # forum
    }
    TYPE_NAME = {0: "text", 2: "voice", 4: "category", 5: "announcement",
                 13: "stage", 15: "forum"}

    # Build category map
    categories = {c["id"]: c["name"] for c in channels if c["type"] == 4}

    results = []
    for ch in channels:
        if ch["type"] == 4:
            continue  # skip category rows themselves
        icon     = TYPE_ICON.get(ch["type"], "💬")
        cat_name = categories.get(str(ch.get("parent_id", "")), "")
        results.append({
            "id":       ch["id"],
            "text":     ch["name"],
            "type_icon": icon,
            "category": cat_name,
            "type":     TYPE_NAME.get(ch["type"], "text"),
        })

    # Sort: text/announce first, then voice, then others; alpha within type
    type_order = {"text": 0, "announcement": 1, "voice": 2, "stage": 3, "forum": 4}
    results.sort(key=lambda c: (type_order.get(c["type"], 9), c["text"].lower()))

    return jsonify({"results": results})


@api_bp.route("/members/search")
@require_guild
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


@api_bp.route("/moderation/logs")
@require_guild
def moderation_logs_partial():
    guild_id      = get_session_guild_id()
    action_filter = request.args.get("action", "")
    page          = int(request.args.get("page", 1))
    per_page      = 25
    offset        = (page - 1) * per_page
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            if action_filter:
                cursor = await db.execute("""
                    SELECT id, user_display_name, user_avatar_url,
                           moderator_display_name, action, reason,
                           source, created_at
                    FROM moderation_logs
                    WHERE guild_id = ? AND deleted = 0 AND action = ?
                    ORDER BY created_at DESC LIMIT ? OFFSET ?
                """, (guild_id, action_filter, per_page, offset))
            else:
                cursor = await db.execute("""
                    SELECT id, user_display_name, user_avatar_url,
                           moderator_display_name, action, reason,
                           source, created_at
                    FROM moderation_logs
                    WHERE guild_id = ? AND deleted = 0
                    ORDER BY created_at DESC LIMIT ? OFFSET ?
                """, (guild_id, per_page, offset))
            return await cursor.fetchall()
    rows = run_async(fetch())
    colors = {
        "ban": "danger", "kick": "warning", "timeout": "warning",
        "warn": "accent", "unban": "success", "lock": "danger",
    }
    html = ""
    for r in rows:
        avatar = r[2] or "https://cdn.discordapp.com/embed/avatars/0.png"
        color  = colors.get(str(r[4]).lower(), "accent")
        html  += (
            f"<tr>"
            f"<td><div class='user-cell'>"
            f"<img src='{avatar}' class='avatar-sm'>"
            f"<span>{r[1]}</span></div></td>"
            f"<td>{r[3]}</td>"
            f"<td><span class='badge badge-{color}'>{r[4]}</span></td>"
            f"<td>{r[5] or '—'}</td>"
            f"<td><span class='badge badge-source'>{r[6]}</span></td>"
            f"<td class='text-muted'>{str(r[7])[:10] if r[7] else '—'}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='6' class='empty'>No logs found</td></tr>"


@api_bp.route("/mvp/scores")
@require_guild
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
    html = ""
    for i, r in enumerate(rows, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
        html += (
            f"<tr><td>{medal}</td>"
            f"<td><code>{r[0]}</code></td>"
            f"<td>{r[1]:.1f}</td>"
            f"<td>{r[2]:.1f}</td>"
            f"<td><strong>{r[3]:.1f}</strong></td></tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No activity today yet</td></tr>"


@api_bp.route("/economy/leaderboard")
@require_guild
def economy_leaderboard_partial():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, balance FROM economy
                WHERE guild_id = ? ORDER BY balance DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for i, r in enumerate(rows, 1):
        html += (
            f"<tr><td>#{i}</td>"
            f"<td><code>{r[0]}</code></td>"
            f"<td><strong>🪙 {r[1]:,}</strong></td></tr>"
        )
    return html or "<tr><td colspan='3' class='empty'>No data yet</td></tr>"


@api_bp.route("/leveling/leaderboard")
@require_guild
def leveling_leaderboard_partial():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                WHERE guild_id = ? ORDER BY xp DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for i, r in enumerate(rows, 1):
        html += (
            f"<tr><td>#{i}</td>"
            f"<td><code>{r[0]}</code></td>"
            f"<td><span class='badge badge-accent'>Lv {r[2]}</span></td>"
            f"<td>{r[1]:,} XP</td></tr>"
        )
    return html or "<tr><td colspan='4' class='empty'>No data yet</td></tr>"


@api_bp.route("/audit-log/entries")
@require_guild
def audit_log_partial():
    guild_id = get_session_guild_id()
    page     = int(request.args.get("page", 1))
    per_page = 50
    offset   = (page - 1) * per_page
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_display_name, action, details, page, created_at
                FROM audit_log
                WHERE guild_id = ?
                ORDER BY created_at DESC LIMIT ? OFFSET ?
            """, (guild_id, per_page, offset))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for r in rows:
        html += (
            f"<tr>"
            f"<td>{r[0]}</td><td>{r[1]}</td>"
            f"<td class='text-muted'>{r[2] or '—'}</td>"
            f"<td><span class='badge badge-accent'>{r[3] or '—'}</span></td>"
            f"<td class='text-muted'>{str(r[4])[:16] if r[4] else '—'}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No actions logged yet</td></tr>"


@api_bp.route("/shop/items")
@require_guild
def shop_items_partial():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, name, description, price, type,
                       role_id, duration_hours, featured, enabled
                FROM shop_items WHERE guild_id = ?
                ORDER BY featured DESC, created_at DESC
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for r in rows:
        status = "badge-success" if r[8] else "badge-danger"
        label  = "Active" if r[8] else "Disabled"
        dur    = f"{r[6]}h" if r[6] else "Permanent"
        html  += (
            f"<tr>"
            f"<td><strong>{r[1]}</strong>{'⭐' if r[7] else ''}</td>"
            f"<td class='text-muted'>{r[2] or '—'}</td>"
            f"<td>🪙 {r[3]:,}</td>"
            f"<td>{r[4]}</td>"
            f"<td>{dur}</td>"
            f"<td><span class='badge {status}'>{label}</span></td>"
            f"<td><button class='btn btn-sm btn-danger' "
            f"hx-delete='/api/shop/item/{r[0]}' "
            f"hx-confirm='Delete this item?' "
            f"hx-target='closest tr' "
            f"hx-swap='outerHTML'>Delete</button></td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='7' class='empty'>No shop items yet</td></tr>"


@api_bp.route("/shop/item/<int:item_id>", methods=["DELETE"])
@require_guild
def delete_shop_item(item_id: int):
    guild_id = get_session_guild_id()
    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM shop_items WHERE id = ? AND guild_id = ?",
                (item_id, guild_id))
            await db.commit()
    run_async(delete())
    return ""


@api_bp.route("/shop/item", methods=["POST"])
@require_guild
def add_shop_item():
    guild_id = get_session_guild_id()
    data     = request.json or request.form.to_dict()
    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO shop_items
                    (guild_id, name, description, price, type,
                     role_id, duration_hours, featured,
                     required_level, required_role_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                guild_id,
                data.get("name"),
                data.get("description"),
                int(data.get("price", 0)),
                data.get("type", "role"),
                data.get("role_id") or None,
                data.get("duration_hours") or None,
                int(data.get("featured", 0)),
                int(data.get("required_level", 0)),
                data.get("required_role_id") or None,
            ))
            await db.commit()
    run_async(save())
    from dashboard.permissions import log_action
    log_action(guild_id, f"Added shop item: {data.get('name')}", "shop")
    return jsonify({"success": True})


@api_bp.route("/tickets/list")
@require_guild
def tickets_partial():
    guild_id      = get_session_guild_id()
    status_filter = request.args.get("status", "")
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            if status_filter:
                cursor = await db.execute("""
                    SELECT id, channel_id, user_id, status, category, created_at
                    FROM tickets WHERE guild_id = ? AND status = ?
                    ORDER BY created_at DESC LIMIT 100
                """, (guild_id, status_filter))
            else:
                cursor = await db.execute("""
                    SELECT id, channel_id, user_id, status, category, created_at
                    FROM tickets WHERE guild_id = ?
                    ORDER BY created_at DESC LIMIT 100
                """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for r in rows:
        color = "badge-success" if r[3] == "open" else "badge-danger"
        html += (
            f"<tr>"
            f"<td><strong>#{r[0]}</strong></td>"
            f"<td><code>{r[2]}</code></td>"
            f"<td>{r[4] or 'General'}</td>"
            f"<td><span class='badge {color}'>{r[3]}</span></td>"
            f"<td class='text-muted'>{str(r[5])[:10] if r[5] else '—'}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No tickets found</td></tr>"


@api_bp.route("/status-messages", methods=["GET"])
@require_guild
def get_status_messages():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, text, type, position, enabled
                FROM status_messages WHERE guild_id = ?
                ORDER BY position ASC
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    return jsonify([{
        "id": r[0], "text": r[1], "type": r[2],
        "position": r[3], "enabled": r[4],
    } for r in rows])


@api_bp.route("/status-messages", methods=["POST"])
@require_guild
def add_status_message():
    guild_id = get_session_guild_id()
    data     = request.json
    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM status_messages WHERE guild_id = ?",
                (guild_id,))
            count = (await cursor.fetchone())[0]
            await db.execute("""
                INSERT INTO status_messages (guild_id, text, type, position)
                VALUES (?, ?, ?, ?)
            """, (guild_id, data.get("text"), data.get("type", "playing"), count))
            await db.commit()
    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/status-messages/<int:msg_id>", methods=["DELETE"])
@require_guild
def delete_status_message(msg_id: int):
    guild_id = get_session_guild_id()
    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM status_messages WHERE id = ? AND guild_id = ?",
                (msg_id, guild_id))
            await db.commit()
    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/warning-thresholds", methods=["GET"])
@require_guild
def get_warning_thresholds():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, warn_count, action, duration_minutes, role_id, enabled
                FROM warning_thresholds WHERE guild_id = ?
                ORDER BY warn_count ASC
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    return jsonify([{
        "id": r[0], "warn_count": r[1], "action": r[2],
        "duration_minutes": r[3], "role_id": r[4], "enabled": r[5],
    } for r in rows])


@api_bp.route("/warning-thresholds", methods=["POST"])
@require_guild
def add_warning_threshold():
    guild_id = get_session_guild_id()
    data     = request.json
    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO warning_thresholds
                    (guild_id, warn_count, action, duration_minutes, role_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                guild_id,
                int(data.get("warn_count", 3)),
                data.get("action", "timeout"),
                data.get("duration_minutes") or None,
                data.get("role_id") or None,
            ))
            await db.commit()
    run_async(save())
    from dashboard.permissions import log_action
    log_action(guild_id,
               f"Added threshold: {data.get('warn_count')} warns -> {data.get('action')}",
               "moderation")
    return jsonify({"success": True})


@api_bp.route("/warning-thresholds/<int:threshold_id>", methods=["DELETE"])
@require_guild
def delete_warning_threshold(threshold_id: int):
    guild_id = get_session_guild_id()
    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM warning_thresholds WHERE id = ? AND guild_id = ?",
                (threshold_id, guild_id))
            await db.commit()
    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/mvp/config", methods=["GET"])
@require_guild
def get_mvp_config_api():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM mvp_config WHERE guild_id = ?",
                (guild_id,))
            row = await cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                return dict(zip(cols, row))
        return {}
    return jsonify({"config": run_async(fetch())})


@api_bp.route("/mvp/config", methods=["POST"])
@require_guild
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
    from dashboard.permissions import log_action
    log_action(guild_id, "Updated MVP config", "mvp")
    return jsonify({"success": True})


@api_bp.route("/shop/purchase-history")
@require_guild
def shop_purchase_history():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_display_name, item_name,
                       price_paid, purchased_at, expires_at
                FROM purchase_history
                WHERE guild_id = ?
                ORDER BY purchased_at DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for r in rows:
        exp = r[4][:10] if r[4] else "Permanent"
        html += (
            f"<tr>"
            f"<td>{r[0]}</td>"
            f"<td><strong>{r[1]}</strong></td>"
            f"<td>🪙 {r[2]:,}</td>"
            f"<td class='text-muted'>{str(r[3])[:10] if r[3] else '—'}</td>"
            f"<td class='text-muted'>{exp}</td>"
            f"</tr>"
        )
    return html or (
        "<tr><td colspan='5' class='empty'>"
        "No purchases yet</td></tr>")


@api_bp.route("/shop/temp-roles")
@require_guild
def shop_temp_roles():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, role_id, expires_at, source
                FROM temp_roles
                WHERE guild_id = ?
                ORDER BY expires_at ASC
            """, (guild_id,))
            return await cursor.fetchall()
    rows = run_async(fetch())
    html = ""
    for r in rows:
        html += (
            f"<tr>"
            f"<td><code>{r[0]}</code></td>"
            f"<td><code>{r[1]}</code></td>"
            f"<td class='text-muted'>{str(r[2])[:16] if r[2] else '—'}</td>"
            f"<td><span class='badge badge-accent'>{r[3]}</span></td>"
            f"</tr>"
        )
    return html or (
        "<tr><td colspan='4' class='empty'>"
        "No active temp roles</td></tr>")


@api_bp.route("/leveling/config", methods=["GET"])
@require_guild
def get_leveling_config_api():
    guild_id = get_session_guild_id()
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM leveling_config WHERE guild_id = ?",
                (guild_id,))
            row = await cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                return dict(zip(cols, row))
        return {}
    return jsonify({"config": run_async(fetch())})


@api_bp.route("/leveling/config", methods=["POST"])
@require_guild
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
    from dashboard.permissions import log_action
    log_action(guild_id, "Updated leveling config", "leveling")
    return jsonify({"success": True})


@api_bp.route("/leveling/reward", methods=["POST"])
@require_guild
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
@require_guild
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


@api_bp.route("/leveling/bonus-roles", methods=["GET"])
@require_guild
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
        {"id": r[0], "role_id": r[1], "multiplier": r[2]}
        for r in rows]})


@api_bp.route("/leveling/bonus-role", methods=["POST"])
@require_guild
def add_bonus_role():
    guild_id = get_session_guild_id()
    data     = request.json
    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO leveling_bonus_roles
                    (guild_id, role_id, multiplier)
                VALUES (?, ?, ?)
            """, (guild_id, data.get("role_id"),
                  float(data.get("multiplier", 1.5))))
            await db.commit()
    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/leveling/bonus-role/<int:role_id>", methods=["DELETE"])
@require_guild
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
@require_guild
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
    return jsonify({"roles": [
        {"id": r[0], "role_id": r[1]} for r in rows]})


@api_bp.route("/leveling/blacklist", methods=["POST"])
@require_guild
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
@require_guild
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
