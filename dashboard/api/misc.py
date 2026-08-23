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

# ── Audit log ─────────────────────────────────────────────────────────────────

@api_bp.route("/audit-log/entries")
@require_api_permission(LEVEL_ADMIN)
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
        # SECURITY FIX (dark-fixes pass, CRITICAL — stored XSS): same
        # class of bug as moderation_logs_partial above. r[0] is the
        # dashboard user's Discord username (attacker-controlled via
        # their own Discord profile), and r[2] ("details") is free
        # text built from other admin actions (e.g. trigger words,
        # item names) that themselves may contain unescaped input.
        html += (
            f"<tr>"
            f"<td>{_esc(r[0])}</td><td>{_esc(r[1])}</td>"
            f"<td class='text-muted'>{_esc(r[2]) if r[2] else '—'}</td>"
            f"<td><span class='badge badge-accent'>{_esc(r[3]) if r[3] else '—'}</span></td>"
            f"<td class='text-muted'>{str(r[4])[:16] if r[4] else '—'}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No actions logged yet</td></tr>"


# ── Settings ──────────────────────────────────────────────────────────────────

@api_bp.route("/settings/general", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_ADMIN)
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
@require_api_permission(LEVEL_OWNER)
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
@require_api_permission(LEVEL_OWNER)
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
@require_api_permission(LEVEL_OWNER)
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


# ── Phase 3 E3/E4 CLOSEOUT — Ledger & Inventory (read-only) ────────────────────

@api_bp.route("/ledger")
@require_api_permission(LEVEL_ADMIN)
def api_ledger():
    guild_id = get_session_guild_id()
    user_id  = request.args.get("user_id")
    currency = request.args.get("currency") or None
    source   = request.args.get("source") or None
    limit    = min(int(request.args.get("limit", 100)), 500)

    from utils.ledger import get_user_ledger, get_guild_ledger

    async def fetch():
        if user_id:
            return await get_user_ledger(
                guild_id, int(user_id), limit=limit, currency=currency)
        return await get_guild_ledger(
            guild_id, limit=limit, currency=currency, source=source)

    entries = run_async(fetch())

    # dark-fixes pass #18 (username resolver rollout): one batched
    # resolve_users() call covering every user on the page. The map
    # travels in the JSON payload so loadLedger() renders the user cell
    # client-side via dashboard.js's userIdentityHtml() — same pattern
    # as /api/trade/history.
    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = {e["user_id"] for e in entries}
        if not ids:
            return {}
        return await resolve_users(guild_id, list(ids))

    user_map = run_async(resolve())
    return jsonify({"entries": entries, "guild_id": guild_id,
                    "user_map": user_map})


@api_bp.route("/ledger/reverse/<int:ledger_id>", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_ledger_reverse(ledger_id: int):
    guild_id = get_session_guild_id()
    reason   = (request.json or {}).get("reason", "Reversed via dashboard")

    from utils.ledger import reverse_transaction
    result = run_async(reverse_transaction(
        ledger_id, guild_id,
        reversed_by=current_user_id(), reason=reason))

    if result.get("success"):
        log_action(guild_id, f"Reversed ledger entry #{ledger_id}", "ledger")
    return jsonify(result)


@api_bp.route("/inventory")
@require_api_permission(LEVEL_ADMIN)
def api_inventory_guild():
    guild_id = get_session_guild_id()
    limit    = min(int(request.args.get("limit", 200)), 500)

    from utils.inventory import get_guild_inventory_summary
    rows = run_async(get_guild_inventory_summary(guild_id, limit=limit))
    return jsonify({"items": rows, "guild_id": guild_id})


@api_bp.route("/inventory/<int:user_id>")
@require_api_permission(LEVEL_ADMIN)
def api_inventory_user(user_id: int):
    guild_id      = get_session_guild_id()
    include_empty = request.args.get("include_empty") == "1"

    from utils.inventory import get_inventory
    rows = run_async(get_inventory(
        guild_id, user_id, include_empty=include_empty))
    return jsonify({"items": rows, "guild_id": guild_id, "user_id": user_id})


# ── Rewards (Phase 3 E2 read endpoint) ─────────────────────────────────────

REWARD_SOURCES = ("leveling", "shop", "event", "admin")


@api_bp.route("/rewards/<int:user_id>")
@require_api_permission(LEVEL_ADMIN)
def api_rewards_user(user_id: int):
    guild_id  = get_session_guild_id()
    limit     = min(int(request.args.get("limit", 50)), 200)
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ",".join("?" for _ in REWARD_SOURCES)
            where  = [
                "guild_id = ?", "user_id = ?",
                f"source IN ({placeholders})",
            ]
            params = [guild_id, user_id, *REWARD_SOURCES]
            if date_from:
                where.append("created_at >= ?"); params.append(date_from)
            if date_to:
                where.append("created_at <= ?"); params.append(date_to + " 23:59:59")
            params.append(limit)
            cursor = await db.execute(f"""
                SELECT id, currency, amount, balance_after, type, reason,
                       source, related_user_id, reversed, reversed_at, created_at
                FROM transaction_ledger
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC LIMIT ?
            """, params)
            return await cursor.fetchall()

    rows = run_async(fetch())
    rewards = [{
        "id": r[0], "currency": r[1], "amount": r[2],
        "balance_after": r[3], "type": r[4], "reason": r[5],
        "source": r[6], "related_user_id": r[7],
        "reversed": bool(r[8]), "reversed_at": r[9], "created_at": r[10],
    } for r in rows]

    return jsonify({"guild_id": guild_id, "user_id": user_id, "rewards": rewards})
