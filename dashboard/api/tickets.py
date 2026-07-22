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

# ── Tickets ───────────────────────────────────────────────────────────────────

@api_bp.route("/tickets/list")
@require_api_permission(LEVEL_MODERATOR)
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
        # r[4] (category) is admin-configured but still free text —
        # escaped defensively rather than assumed trusted.
        html += (
            f"<tr>"
            f"<td><strong>#{r[0]}</strong></td>"
            f"<td><code>{r[2]}</code></td>"
            f"<td>{_esc(r[4]) if r[4] else 'General'}</td>"
            f"<td><span class='badge {color}'>{_esc(r[3])}</span></td>"
            f"<td class='text-muted'>{str(r[5])[:10] if r[5] else '—'}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No tickets found</td></tr>"


@api_bp.route("/tickets/settings", methods=["GET"])
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_MODERATOR)
def api_tickets_categories():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT id, name, emoji, viewer_roles, closer_roles,
                       auto_assign_roles, open_embed, enabled, sort_order,
                       required_role_id
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
                "required_role_id": r[9],
            } for r in rows]

    return jsonify({"categories": run_async(get())})


@api_bp.route("/tickets/categories", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def api_tickets_save_category():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            if data.get("id"):
                await db.execute("""
                    UPDATE ticket_categories SET
                        name=?, emoji=?, viewer_roles=?, closer_roles=?,
                        auto_assign_roles=?, open_embed=?, enabled=?,
                        required_role_id=?
                    WHERE id=? AND guild_id=?
                """, (data["name"], data.get("emoji", "🎫"),
                      json.dumps(data.get("viewer_roles", [])),
                      json.dumps(data.get("closer_roles", [])),
                      json.dumps(data.get("auto_assign_roles", [])),
                      json.dumps(data.get("open_embed", {})),
                      int(data.get("enabled", 1)),
                      data.get("required_role_id") or None,
                      data["id"], guild_id))
            else:
                await db.execute("""
                    INSERT INTO ticket_categories
                        (guild_id, name, emoji, viewer_roles, closer_roles,
                         auto_assign_roles, open_embed, required_role_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (guild_id, data["name"], data.get("emoji", "🎫"),
                      json.dumps(data.get("viewer_roles", [])),
                      json.dumps(data.get("closer_roles", [])),
                      json.dumps(data.get("auto_assign_roles", [])),
                      json.dumps(data.get("open_embed", {})),
                      data.get("required_role_id") or None))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved ticket category: {data.get('name')}", "tickets")
    return jsonify({"success": True})


@api_bp.route("/tickets/categories/<int:cat_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_MODERATOR)
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
@require_api_permission(LEVEL_MODERATOR)
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
