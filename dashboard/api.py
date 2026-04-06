import os
import json
import csv
import io
import datetime
import requests as _req
import aiosqlite
from flask import Blueprint, jsonify, request, session, abort, Response
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.auth import login_required, current_user_id, current_user
from dashboard.permissions import (
    get_session_guild_id, log_action,
    LEVEL_RANK, LEVEL_OWNER,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")


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


# ── Discord helpers ───────────────────────────────────────────────────────────

@api_bp.route("/guild/roles")
@require_guild
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
@require_guild
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
@require_guild
def get_roles():
    return get_guild_roles()


@api_bp.route("/channels")
@require_guild
def get_channels():
    return get_guild_channels()


# ── Members ───────────────────────────────────────────────────────────────────

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


# ── Moderation ────────────────────────────────────────────────────────────────

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

    rows   = run_async(fetch())
    colors = {
        "ban":"danger","kick":"warning","timeout":"warning",
        "warn":"accent","unban":"success","lock":"danger",
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


@api_bp.route("/moderation/edit-reason/<int:log_id>", methods=["POST"])
@require_guild
def api_mod_edit_reason(log_id: int):
    guild_id = get_session_guild_id()
    reason   = request.json.get("reason", "").strip()
    if not reason:
        return jsonify({"success": False, "error": "Reason required"})

    async def update():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE moderation_logs
                SET reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND guild_id = ?
            """, (reason, log_id, guild_id))
            await db.commit()

    run_async(update())
    log_action(guild_id, f"Edited reason for log #{log_id}", "moderation")
    return jsonify({"success": True})


@api_bp.route("/moderation/delete-log/<int:log_id>", methods=["DELETE"])
@require_guild
def api_mod_delete_log(log_id: int):
    guild_id   = get_session_guild_id()
    user_level = session.get("user_level", "")
    if LEVEL_RANK.get(user_level, 0) < LEVEL_RANK[LEVEL_OWNER]:
        return jsonify({"success": False, "error": "Owner only"}), 403

    async def soft_delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE moderation_logs SET deleted = 1
                WHERE id = ? AND guild_id = ?
            """, (log_id, guild_id))
            await db.commit()

    run_async(soft_delete())
    log_action(guild_id, f"Deleted mod log #{log_id}", "moderation")
    return jsonify({"success": True})


@api_bp.route("/moderation/export")
@require_guild
def api_mod_export():
    guild_id = get_session_guild_id()
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")

    async def get_logs():
        async with aiosqlite.connect(DB_PATH) as db:
            where  = ["guild_id = ?", "deleted = 0"]
            params = [guild_id]
            if date_from:
                where.append("created_at >= ?"); params.append(date_from)
            if date_to:
                where.append("created_at <= ?"); params.append(date_to + " 23:59:59")
            cur = await db.execute(f"""
                SELECT id, user_display_name, user_id, action, reason,
                       moderator_display_name, source, evidence_url, created_at
                FROM moderation_logs WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
            """, params)
            return await cur.fetchall()

    rows = run_async(get_logs())
    si   = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["ID","User","UserID","Action","Reason","Moderator","Source","Evidence","Date"])
    writer.writerows(rows)
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=mod_logs_{guild_id}.csv"},
    )


@api_bp.route("/moderation/quick-action", methods=["POST"])
@require_guild
def api_mod_quick_action():
    guild_id    = get_session_guild_id()
    data        = request.json
    action      = data.get("action")
    target_id   = data.get("user_id")
    reason      = data.get("reason", "No reason provided")
    evidence    = data.get("evidence_url", "")
    duration    = data.get("duration_seconds")
    delete_days = data.get("delete_message_days", 0)
    bot_token   = os.getenv("DISCORD_TOKEN", "")
    user        = current_user()
    mod_name    = user.get("username", "Dashboard") if user else "Dashboard"
    mod_id      = current_user_id()

    if not bot_token:
        return jsonify({"success": False, "error": "Bot token not configured"})

    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    base    = "https://discord.com/api/v10"
    result  = {"success": True, "message": ""}

    try:
        if action == "ban":
            resp = _req.put(
                f"{base}/guilds/{guild_id}/bans/{target_id}",
                headers=headers,
                json={"delete_message_days": int(delete_days), "reason": reason},
            )
            result["message"] = f"Banned <@{target_id}>"
        elif action == "kick":
            resp = _req.delete(
                f"{base}/guilds/{guild_id}/members/{target_id}",
                headers=headers, params={"reason": reason},
            )
            result["message"] = f"Kicked <@{target_id}>"
        elif action == "timeout":
            until = (
                datetime.datetime.utcnow()
                + datetime.timedelta(seconds=int(duration or 300))
            ).isoformat() + "Z"
            resp = _req.patch(
                f"{base}/guilds/{guild_id}/members/{target_id}",
                headers=headers,
                json={"communication_disabled_until": until, "reason": reason},
            )
            result["message"] = f"Timed out <@{target_id}>"
        elif action == "unban":
            resp = _req.delete(
                f"{base}/guilds/{guild_id}/bans/{target_id}",
                headers=headers,
            )
            result["message"] = f"Unbanned <@{target_id}>"
        elif action == "remove_timeout":
            resp = _req.patch(
                f"{base}/guilds/{guild_id}/members/{target_id}",
                headers=headers,
                json={"communication_disabled_until": None},
            )
            result["message"] = f"Removed timeout for <@{target_id}>"
        elif action == "warn":
            resp = type("R", (), {"status_code": 200})()
            result["message"] = f"Warned <@{target_id}>"
        elif action == "massban":
            user_ids = data.get("user_ids", [])
            failed   = []
            for uid in user_ids:
                r = _req.put(
                    f"{base}/guilds/{guild_id}/bans/{uid}",
                    headers=headers, json={"reason": reason},
                )
                if r.status_code not in (200, 204):
                    failed.append(uid)
            result["message"] = f"Massbanned {len(user_ids)-len(failed)}/{len(user_ids)} users"
            if failed:
                result["failed"] = failed
            resp = type("R", (), {"status_code": 200})()
        else:
            return jsonify({"success": False, "error": f"Unknown action: {action}"})

        if hasattr(resp, "status_code") and resp.status_code not in (200, 201, 204):
            return jsonify({"success": False, "error": f"Discord API error {resp.status_code}"})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    async def log_mod():
        expires = None
        if action == "timeout" and duration:
            expires = (
                datetime.datetime.utcnow()
                + datetime.timedelta(seconds=int(duration))
            ).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            if action == "warn":
                await db.execute("""
                    INSERT INTO warnings
                        (guild_id, user_id, moderator_id, reason, moderator_display_name)
                    VALUES (?, ?, ?, ?, ?)
                """, (guild_id, target_id, mod_id, reason, mod_name))
            await db.execute("""
                INSERT INTO moderation_logs
                    (guild_id, user_id, moderator_id, moderator_display_name,
                     action, reason, source, evidence_url, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 'dashboard', ?, ?)
            """, (guild_id, target_id, mod_id, mod_name,
                  action, reason, evidence, expires))
            await db.commit()

    run_async(log_mod())
    log_action(guild_id, f"Quick action: {action} on {target_id}", "moderation",
               target_id=int(target_id) if target_id else None)
    return jsonify(result)


@api_bp.route("/moderation/warning-thresholds", methods=["GET"])
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


@api_bp.route("/moderation/warning-thresholds", methods=["POST"])
@require_guild
def save_warning_threshold():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            if data.get("id"):
                await db.execute("""
                    UPDATE warning_thresholds
                    SET warn_count=?, action=?, duration_minutes=?, role_id=?, enabled=?
                    WHERE id=? AND guild_id=?
                """, (data["warn_count"], data["action"],
                      data.get("duration_minutes"), data.get("role_id"),
                      int(data.get("enabled", 1)),
                      data["id"], guild_id))
            else:
                await db.execute("""
                    INSERT INTO warning_thresholds
                        (guild_id, warn_count, action, duration_minutes, role_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (guild_id, int(data.get("warn_count", 3)),
                      data.get("action", "timeout"),
                      data.get("duration_minutes") or None,
                      data.get("role_id") or None))
            await db.commit()

    run_async(save())
    log_action(guild_id,
               f"Saved threshold: {data.get('warn_count')} warns -> {data.get('action')}",
               "moderation")
    return jsonify({"success": True})


@api_bp.route("/moderation/warning-thresholds/<int:tid>", methods=["DELETE"])
@require_guild
def delete_warning_threshold(tid: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM warning_thresholds WHERE id=? AND guild_id=?",
                (tid, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/moderation/auto-escalation", methods=["POST"])
@require_guild
def api_toggle_auto_escalation():
    guild_id = get_session_guild_id()
    enabled  = request.json.get("enabled", True)

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO guild_settings_kv (guild_id, key, value)
                VALUES (?, 'auto_escalation_enabled', ?)
                ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value
            """, (guild_id, "1" if enabled else "0"))
            await db.commit()

    run_async(save())
    return jsonify({"success": True})


@api_bp.route("/moderation/clear-warnings", methods=["POST"])
@require_guild
def api_clear_warnings():
    guild_id  = get_session_guild_id()
    target_id = request.json.get("user_id")
    count     = request.json.get("count")

    async def clear():
        async with aiosqlite.connect(DB_PATH) as db:
            if count:
                cur = await db.execute("""
                    SELECT rowid FROM warnings
                    WHERE guild_id=? AND user_id=?
                    ORDER BY timestamp ASC LIMIT ?
                """, (guild_id, target_id, int(count)))
                for (rid,) in await cur.fetchall():
                    await db.execute("DELETE FROM warnings WHERE rowid=?", (rid,))
            else:
                await db.execute(
                    "DELETE FROM warnings WHERE guild_id=? AND user_id=?",
                    (guild_id, target_id))
            await db.commit()

    run_async(clear())
    log_action(guild_id, f"Cleared warnings for {target_id}", "moderation",
               target_id=int(target_id) if target_id else None)
    return jsonify({"success": True})


@api_bp.route("/moderation/delete-warning/<int:warning_id>", methods=["DELETE"])
@require_guild
def api_delete_warning(warning_id: int):
    guild_id   = get_session_guild_id()
    user_level = session.get("user_level", "")
    if LEVEL_RANK.get(user_level, 0) < LEVEL_RANK[LEVEL_OWNER]:
        return jsonify({"success": False, "error": "Owner only"}), 403

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM warnings WHERE rowid = ? AND guild_id = ?",
                (warning_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Deleted warning #{warning_id}", "moderation")
    return jsonify({"success": True})


# ── Tickets ───────────────────────────────────────────────────────────────────

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


@api_bp.route("/tickets/settings", methods=["GET"])
@require_guild
def api_tickets_settings_get():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT * FROM ticket_settings WHERE guild_id=?", (guild_id,))
            row = await cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
        return {}

    return jsonify(run_async(get()))


@api_bp.route("/tickets/settings", methods=["POST"])
@require_guild
def api_tickets_settings_save():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO ticket_settings
                    (guild_id, enabled, max_per_user, auto_close_hours,
                     save_transcripts, transcript_channel_id, support_role_id,
                     name_format)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    max_per_user=excluded.max_per_user,
                    auto_close_hours=excluded.auto_close_hours,
                    save_transcripts=excluded.save_transcripts,
                    transcript_channel_id=excluded.transcript_channel_id,
                    support_role_id=excluded.support_role_id,
                    name_format=excluded.name_format,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                guild_id,
                int(bool(data.get("enabled", True))),
                int(data.get("max_per_user", 1)),
                int(data.get("auto_close_hours", 0)),
                int(bool(data.get("save_transcripts", True))),
                data.get("transcript_channel_id") or None,
                data.get("support_role_id") or None,
                data.get("name_format", "ticket-{number}"),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated ticket settings", "tickets")
    return jsonify({"success": True})


@api_bp.route("/tickets/categories", methods=["GET"])
@require_guild
def api_tickets_categories():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT id, name, emoji, viewer_roles, closer_roles,
                       auto_assign_roles, open_embed, enabled, sort_order
                FROM ticket_categories WHERE guild_id=? ORDER BY sort_order ASC
            """, (guild_id,))
            rows = await cur.fetchall()
            return [{
                "id": r[0], "name": r[1], "emoji": r[2],
                "viewer_roles":      json.loads(r[3] or "[]"),
                "closer_roles":      json.loads(r[4] or "[]"),
                "auto_assign_roles": json.loads(r[5] or "[]"),
                "open_embed":        json.loads(r[6] or "{}"),
                "enabled": r[7], "sort_order": r[8],
            } for r in rows]

    return jsonify({"categories": run_async(get())})


@api_bp.route("/tickets/categories", methods=["POST"])
@require_guild
def api_tickets_save_category():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            if data.get("id"):
                await db.execute("""
                    UPDATE ticket_categories SET
                        name=?, emoji=?, viewer_roles=?, closer_roles=?,
                        auto_assign_roles=?, open_embed=?, enabled=?
                    WHERE id=? AND guild_id=?
                """, (data["name"], data.get("emoji", "🎫"),
                      json.dumps(data.get("viewer_roles", [])),
                      json.dumps(data.get("closer_roles", [])),
                      json.dumps(data.get("auto_assign_roles", [])),
                      json.dumps(data.get("open_embed", {})),
                      int(data.get("enabled", 1)),
                      data["id"], guild_id))
            else:
                await db.execute("""
                    INSERT INTO ticket_categories
                        (guild_id, name, emoji, viewer_roles, closer_roles,
                         auto_assign_roles, open_embed)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (guild_id, data["name"], data.get("emoji", "🎫"),
                      json.dumps(data.get("viewer_roles", [])),
                      json.dumps(data.get("closer_roles", [])),
                      json.dumps(data.get("auto_assign_roles", [])),
                      json.dumps(data.get("open_embed", {}))))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved ticket category: {data.get('name')}", "tickets")
    return jsonify({"success": True})


@api_bp.route("/tickets/categories/<int:cat_id>", methods=["DELETE"])
@require_guild
def api_tickets_delete_category(cat_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM ticket_categories WHERE id=? AND guild_id=?",
                (cat_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/tickets/categories/reorder", methods=["POST"])
@require_guild
def api_tickets_reorder_categories():
    guild_id = get_session_guild_id()
    order    = request.json.get("order", [])

    async def reorder():
        async with aiosqlite.connect(DB_PATH) as db:
            for pos, cat_id in enumerate(order):
                await db.execute(
                    "UPDATE ticket_categories SET sort_order=? WHERE id=? AND guild_id=?",
                    (pos, cat_id, guild_id))
            await db.commit()

    run_async(reorder())
    return jsonify({"success": True})


@api_bp.route("/tickets/panels", methods=["GET"])
@require_guild
def api_tickets_panels():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT id, name, channel_id, embed_data, buttons, created_at
                FROM ticket_panels WHERE guild_id=? ORDER BY id DESC
            """, (guild_id,))
            rows = await cur.fetchall()
            return [{
                "id": r[0], "name": r[1], "channel_id": r[2],
                "embed_data": json.loads(r[3] or "{}"),
                "buttons":    json.loads(r[4] or "[]"),
                "created_at": r[5],
            } for r in rows]

    return jsonify({"panels": run_async(get())})


@api_bp.route("/tickets/panels", methods=["POST"])
@require_guild
def api_tickets_save_panel():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            if data.get("id"):
                await db.execute("""
                    UPDATE ticket_panels SET
                        name=?, channel_id=?, embed_data=?, buttons=?
                    WHERE id=? AND guild_id=?
                """, (data.get("name"), data.get("channel_id"),
                      json.dumps(data.get("embed_data", {})),
                      json.dumps(data.get("buttons", [])),
                      data["id"], guild_id))
            else:
                await db.execute("""
                    INSERT INTO ticket_panels
                        (guild_id, name, channel_id, embed_data, buttons)
                    VALUES (?, ?, ?, ?, ?)
                """, (guild_id, data.get("name"), data.get("channel_id"),
                      json.dumps(data.get("embed_data", {})),
                      json.dumps(data.get("buttons", []))))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved ticket panel: {data.get('name')}", "tickets")
    return jsonify({"success": True})


@api_bp.route("/tickets/panels/<int:panel_id>", methods=["DELETE"])
@require_guild
def api_tickets_delete_panel(panel_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM ticket_panels WHERE id=? AND guild_id=?",
                (panel_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


@api_bp.route("/tickets/claim/<int:ticket_id>", methods=["POST"])
@require_guild
def api_tickets_claim(ticket_id: int):
    guild_id = get_session_guild_id()
    user     = current_user()
    claimer  = user.get("username", "Unknown") if user else "Unknown"

    async def claim():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tickets SET claimed_by=? WHERE id=? AND guild_id=?",
                (claimer, ticket_id, guild_id))
            await db.commit()

    run_async(claim())
    log_action(guild_id, f"Claimed ticket #{ticket_id}", "tickets",
               target_id=ticket_id)
    return jsonify({"success": True})


@api_bp.route("/tickets/transfer/<int:ticket_id>", methods=["POST"])
@require_guild
def api_tickets_transfer(ticket_id: int):
    guild_id = get_session_guild_id()
    to_user  = request.json.get("to_user", "")

    async def transfer():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tickets SET claimed_by=? WHERE id=? AND guild_id=?",
                (to_user, ticket_id, guild_id))
            await db.commit()

    run_async(transfer())
    log_action(guild_id, f"Transferred ticket #{ticket_id} to {to_user}", "tickets",
               target_id=ticket_id)
    return jsonify({"success": True})


@api_bp.route("/tickets/tag/<int:ticket_id>", methods=["POST"])
@require_guild
def api_tickets_tag(ticket_id: int):
    guild_id = get_session_guild_id()
    tags     = request.json.get("tags", [])

    async def tag():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tickets SET tags=? WHERE id=? AND guild_id=?",
                (json.dumps(tags), ticket_id, guild_id))
            await db.commit()

    run_async(tag())
    return jsonify({"success": True})


@api_bp.route("/tickets/ratings", methods=["GET"])
@require_guild
def api_tickets_ratings():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT user_id, rating, comment, created_at
                FROM ticket_ratings WHERE guild_id=?
                ORDER BY created_at DESC LIMIT 50
            """, (guild_id,))
            return await cur.fetchall()

    rows = run_async(get())
    return jsonify([{
        "user_id": r[0], "rating": r[1],
        "comment": r[2], "created_at": r[3],
    } for r in rows])


# ── MVP ───────────────────────────────────────────────────────────────────────

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


@api_bp.route("/mvp/config", methods=["GET"])
@require_guild
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
    log_action(guild_id, "Updated MVP config", "mvp")
    return jsonify({"success": True})


# ── Economy / Shop ────────────────────────────────────────────────────────────

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
    log_action(guild_id, f"Added shop item: {data.get('name')}", "shop")
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
        exp   = r[4][:10] if r[4] else "Permanent"
        html += (
            f"<tr>"
            f"<td>{r[0]}</td>"
            f"<td><strong>{r[1]}</strong></td>"
            f"<td>🪙 {r[2]:,}</td>"
            f"<td class='text-muted'>{str(r[3])[:10] if r[3] else '—'}</td>"
            f"<td class='text-muted'>{exp}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No purchases yet</td></tr>"


@api_bp.route("/shop/temp-roles")
@require_guild
def shop_temp_roles():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, role_id, expires_at, source
                FROM temp_roles WHERE guild_id = ?
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
    return html or "<tr><td colspan='4' class='empty'>No active temp roles</td></tr>"


# ── Leveling ──────────────────────────────────────────────────────────────────

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


@api_bp.route("/leveling/config", methods=["GET"])
@require_guild
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
        {"id": r[0], "role_id": r[1], "multiplier": r[2]} for r in rows]})


@api_bp.route("/leveling/bonus-role", methods=["POST"])
@require_guild
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
    return jsonify({"roles": [{"id": r[0], "role_id": r[1]} for r in rows]})


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


# ── Audit log ─────────────────────────────────────────────────────────────────

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


# ── Settings ──────────────────────────────────────────────────────────────────

@api_bp.route("/settings/general", methods=["POST"])
@require_guild
def save_settings_general():
    guild_id = get_session_guild_id()
    data     = request.get_json() or {}

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO guild_settings
                    (guild_id, prefix, timezone, language, log_channel_id,
                     currency_name, currency_emoji_id,
                     status_rotation_enabled, status_rotation_interval)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    prefix                   = excluded.prefix,
                    timezone                 = excluded.timezone,
                    language                 = excluded.language,
                    log_channel_id           = excluded.log_channel_id,
                    currency_name            = excluded.currency_name,
                    currency_emoji_id        = excluded.currency_emoji_id,
                    status_rotation_enabled  = excluded.status_rotation_enabled,
                    status_rotation_interval = excluded.status_rotation_interval,
                    updated_at               = CURRENT_TIMESTAMP
            """, (
                guild_id,
                data.get("prefix", "/"),
                data.get("timezone", "UTC"),
                data.get("language", "en"),
                data.get("log_channel_id") or None,
                data.get("currency_name", "Coins"),
                data.get("currency_emoji_id") or None,
                int(bool(data.get("status_rotation_enabled"))),
                int(data.get("status_rotation_interval", 5)),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated general settings", "config_general")
    return jsonify({"success": True})


@api_bp.route("/settings/welcome", methods=["POST"])
@require_guild
def save_settings_welcome():
    guild_id = get_session_guild_id()
    data     = request.get_json() or {}

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO welcome_config
                    (guild_id, join_enabled, join_channel_id, auto_role_id,
                     join_message_mode, leave_enabled, leave_channel_id,
                     rules_enabled, rules_channel_id, rules_role_id, rules_button_text)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    join_enabled       = excluded.join_enabled,
                    join_channel_id    = excluded.join_channel_id,
                    auto_role_id       = excluded.auto_role_id,
                    join_message_mode  = excluded.join_message_mode,
                    leave_enabled      = excluded.leave_enabled,
                    leave_channel_id   = excluded.leave_channel_id,
                    rules_enabled      = excluded.rules_enabled,
                    rules_channel_id   = excluded.rules_channel_id,
                    rules_role_id      = excluded.rules_role_id,
                    rules_button_text  = excluded.rules_button_text,
                    updated_at         = CURRENT_TIMESTAMP
            """, (
                guild_id,
                int(bool(data.get("join_enabled"))),
                data.get("join_channel_id") or None,
                data.get("auto_role_id") or None,
                data.get("join_message_mode", "random"),
                int(bool(data.get("leave_enabled"))),
                data.get("leave_channel_id") or None,
                int(bool(data.get("rules_enabled"))),
                data.get("rules_channel_id") or None,
                data.get("rules_role_id") or None,
                data.get("rules_button_text", "✅ I Accept"),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated welcome settings", "config_welcome")
    return jsonify({"success": True})


@api_bp.route("/settings/boost", methods=["POST"])
@require_guild
def save_settings_boost():
    guild_id = get_session_guild_id()
    data     = request.get_json() or {}

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_config
                    (guild_id, enabled, boost1_role_id, boost2_role_id,
                     boost2_channel_id, auto_remove_on_unboost, color_roles_enabled)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled                = excluded.enabled,
                    boost1_role_id         = excluded.boost1_role_id,
                    boost2_role_id         = excluded.boost2_role_id,
                    boost2_channel_id      = excluded.boost2_channel_id,
                    auto_remove_on_unboost = excluded.auto_remove_on_unboost,
                    color_roles_enabled    = excluded.color_roles_enabled
            """, (
                guild_id,
                int(bool(data.get("enabled", 1))),
                data.get("boost1_role_id") or None,
                data.get("boost2_role_id") or None,
                data.get("boost2_channel_id") or None,
                int(bool(data.get("auto_remove_on_unboost", 1))),
                int(bool(data.get("color_roles_enabled"))),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, "Updated boost settings", "config_boost")
    return jsonify({"success": True})


# ── Status messages ───────────────────────────────────────────────────────────

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
