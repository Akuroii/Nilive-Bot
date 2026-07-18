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

# ── Moderation ────────────────────────────────────────────────────────────────

@api_bp.route("/moderation/logs")
@require_api_permission(LEVEL_MODERATOR)
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
        # SECURITY FIX (dark-fixes pass, CRITICAL — stored XSS): every
        # value below except the numeric id and the hardcoded badge
        # color can be attacker-influenced (a member's Discord display
        # name is fully attacker-controlled, and moderation reasons are
        # free text typed by staff who could themselves be compromised
        # or malicious). This response bypasses Jinja's autoescaping
        # entirely (it's a hand-built string returned straight to an
        # htmx innerHTML swap), so anything containing raw HTML/JS here
        # used to execute directly in an admin's browser the next time
        # they opened the Moderation Logs tab. Every interpolated field
        # that isn't a guaranteed-numeric id or a hardcoded literal is
        # now passed through markupsafe.escape() before being placed
        # in the HTML string.
        avatar = _esc(r[2] or "https://cdn.discordapp.com/embed/avatars/0.png")
        color  = colors.get(str(r[4]).lower(), "accent")
        html  += (
            f"<tr>"
            f"<td><div class='user-cell'>"
            f"<img src='{avatar}' class='avatar-sm'>"
            f"<span>{_esc(r[1])}</span></div></td>"
            f"<td>{_esc(r[3])}</td>"
            f"<td><span class='badge badge-{color}'>{_esc(r[4])}</span></td>"
            f"<td>{_esc(r[5]) if r[5] else '—'}</td>"
            f"<td><span class='badge badge-source'>{_esc(r[6])}</span></td>"
            f"<td class='text-muted'>{str(r[7])[:10] if r[7] else '—'}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='6' class='empty'>No logs found</td></tr>"


@api_bp.route("/moderation/edit-reason/<int:log_id>", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_OWNER)
def api_mod_delete_log(log_id: int):
    guild_id = get_session_guild_id()

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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_MODERATOR)
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

    # HARDENING (dark-fixes pass): target_id must be a real Discord
    # snowflake before it's formatted into a Discord API URL or a
    # DB write — previously an arbitrary string could be submitted
    # here and would be silently coerced/used as-is downstream.
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "user_id must be a valid Discord ID"})

    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "duration_seconds must be a number"})
        if duration <= 0 or duration > 60 * 60 * 24 * 28:
            return jsonify({"success": False, "error": "duration_seconds out of range (max 28 days)"})

    try:
        delete_days = max(0, min(int(delete_days or 0), 7))
    except (TypeError, ValueError):
        delete_days = 0

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
            # HARDENING (dark-fixes pass): cap batch size and validate
            # each id — an unbounded list here could hammer Discord's
            # ban endpoint hundreds of times in a single request,
            # risking a rate-limit ban on the bot's own IP.
            if not isinstance(user_ids, list) or len(user_ids) == 0:
                return jsonify({"success": False, "error": "user_ids must be a non-empty list"})
            if len(user_ids) > 25:
                return jsonify({"success": False,
                                "error": "massban is capped at 25 users per request"})
            failed   = []
            for uid in user_ids:
                try:
                    uid = int(uid)
                except (TypeError, ValueError):
                    failed.append(uid)
                    continue
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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_OWNER)
def api_delete_warning(warning_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM warnings WHERE rowid = ? AND guild_id = ?",
                (warning_id, guild_id))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Deleted warning #{warning_id}", "moderation")
    return jsonify({"success": True})


