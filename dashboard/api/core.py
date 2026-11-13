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
    from dashboard.utils.role_validation import fetch_guild_roles_and_bot_member, compute_role_assignability
    roles, bot_member, err = fetch_guild_roles_and_bot_member(guild_id)
    if err:
        return jsonify({"results": [], "error": err})

    info = compute_role_assignability(guild_id, roles or [], bot_member)
    enriched_roles = info["roles"]

    def sort_key(r):
        if r["id"] == str(guild_id): return (2, 0)
        if r.get("managed"):         return (1, -r.get("position", 0))
        return (0, -r.get("position", 0))
    enriched_roles.sort(key=sort_key)

    # Contract consumed by nero-select.js: ``id`` + ``text``/``name`` (the
    # display string) + ``color`` (hex or null) + assignability metadata.
    return jsonify({
        "results": [{
            "id":                  r["id"],
            "name":                r["name"],
            "text":                r["name"],
            "color":               f"#{r['color']:06x}" if r.get("color") else None,
            "position":            r.get("position", 0),
            "managed":             r.get("managed", False),
            "assignable":          r.get("assignable", True),
            "unassignable_reason": r.get("unassignable_reason"),
            "unassignable_label":  r.get("unassignable_label"),
        } for r in enriched_roles],
        "bot_top_role": {
            "id":       str(info["bot_top_role"]["id"]),
            "name":     info["bot_top_role"].get("name", "Nero"),
            "position": info["bot_top_role"].get("position", 0),
        } if info.get("bot_top_role") else None,
        "has_manage_roles": info.get("has_manage_roles", False),
        "bot_status": info.get("bot_status", "unknown"),
        "bot_status_error": info.get("bot_status_error"),
    })


@api_bp.route("/guild/channels")
@require_api_permission(LEVEL_MODERATOR)
def get_guild_channels():
    guild_id  = get_session_guild_id()
    bot_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not bot_token:
        if os.getenv("DASHBOARD_DEMO_LOGIN") == "1" and str(guild_id) == os.getenv("DASHBOARD_DEMO_GUILD_ID", "900000000000000001"):
            return jsonify({
                "results": [
                    {"id": "980000000000000001", "name": "general", "text": "general", "type_icon": "#", "category": "Text Channels", "category_id": "980000000000000000", "category_position": 0, "type": "text", "raw_type": 0, "position": 0},
                    {"id": "980000000000000002", "name": "announcements", "text": "announcements", "type_icon": "megaphone", "category": "Text Channels", "category_id": "980000000000000000", "category_position": 0, "type": "announcement", "raw_type": 5, "position": 1},
                ]
            })
        return jsonify({"results": [], "error": "BOT_TOKEN not set"})
    try:
        resp = _req.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=8,
        )
        if resp.status_code != 200:
            return jsonify({"results": [], "error": f"Discord {resp.status_code}"})
        channels = resp.json()
    except Exception as exc:
        return jsonify({"results": [], "error": f"Network error ({type(exc).__name__})"})
    if not isinstance(channels, list):
        return jsonify({"results": [], "error": "Malformed channels payload"})
    TYPE_ICON = {0: "#", 2: "speaker", 4: "folder", 5: "megaphone", 10: "thread", 11: "thread", 12: "thread", 13: "mic", 15: "forum"}
    TYPE_NAME = {0: "text", 2: "voice", 4: "category", 5: "announcement", 10: "thread", 11: "thread", 12: "thread", 13: "stage", 15: "forum"}

    categories = {}
    cat_positions = {}
    for c in channels:
        if c.get("type") == 4:
            cid_str = str(c.get("id"))
            categories[cid_str] = c.get("name", "Category")
            cat_positions[cid_str] = c.get("position", 0)

    results = []
    for ch in channels:
        if ch.get("type") == 4:
            continue
        parent_id = str(ch.get("parent_id")) if ch.get("parent_id") else None
        cat_name = categories.get(parent_id, "") if parent_id else ""
        cat_pos = cat_positions.get(parent_id, 9999) if parent_id else 9999

        results.append({
            "id":                str(ch.get("id")),
            "name":              ch.get("name", ""),
            "text":              ch.get("name", ""),
            "type_icon":         TYPE_ICON.get(ch.get("type", 0), "#"),
            "category":          cat_name,
            "category_id":       parent_id,
            "category_position": cat_pos,
            "type":              TYPE_NAME.get(ch.get("type", 0), "text"),
            "raw_type":          ch.get("type", 0),
            "position":          ch.get("position", 0),
        })

    # Sort channels by Discord category position, then channel position, then name
    results.sort(key=lambda c: (
        c["category_position"],
        c["category"].lower() if c["category"] else "zzzz",
        c["position"],
        c["text"].lower()
    ))
    return jsonify({"results": results})


# ── Custom guild emojis (Embed Builder emoji picker) ────────────────────────
#
# Same shape/pattern as get_guild_roles/get_guild_channels above — the bot
# token never reaches the frontend, only the fields the emoji picker needs
# (name, id, animated). Cached client-side for the session by the composer
# JS rather than re-fetched on every popover open.
@api_bp.route("/guild/emojis")
@require_api_permission(LEVEL_MODERATOR)
def get_guild_emojis():
    guild_id  = get_session_guild_id()
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"results": [], "error": "BOT_TOKEN not set"})
    try:
        resp = _req.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/emojis",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=8,
        )
        if resp.status_code != 200:
            return jsonify({"results": [], "error": f"Discord {resp.status_code}"})
        emojis = resp.json()
        if not isinstance(emojis, list):
            return jsonify({"results": [], "error": "Malformed emojis payload"})
    except (ValueError, TypeError):
        return jsonify({"results": [], "error": "Malformed emojis payload"})
    except Exception as exc:
        return jsonify({"results": [], "error": f"Network error ({type(exc).__name__})"})

    results = [{
        "id":       str(e["id"]),
        "name":     e["name"],
        "animated": bool(e.get("animated")),
    } for e in emojis if isinstance(e, dict) and e.get("id") and e.get("name")]
    results.sort(key=lambda e: e["name"].lower())
    return jsonify({"results": results})


# ── "External" custom emojis — every OTHER guild the bot is in ─────────────
#
# Discord has no concept of "any emoji from anywhere" for a bot — a bot can
# only reliably render/send a custom emoji from a guild it is actually a
# member of, and the DESTINATION channel needs "Use External Emojis" enabled
# for the bot for it to render there. There is no safe way to accept an
# arbitrary emoji ID from a server the bot has never joined.
#
# Nilive is already a small, controlled multi-server bot (2-10 friend
# servers, per project scope) — every one of those IS a guild the bot is a
# member of, so surfacing their emoji lists in the picker (clearly labeled
# by server) is both possible and safe: every emoji offered here is one the
# bot can actually deliver, unlike a free-text "type any ID" field would be.
# Discord's own message-send call is still the final authority — if the
# destination channel doesn't have Use External Emojis enabled for the bot,
# Discord's API rejects the send and dashboard/api/embedbuilder.py's
# /send route already surfaces that error back to the composer.
#
# Reuses fetch_bot_guilds_full() (dashboard/auth.py — already powers the
# developer's "every bot guild" server-select view) rather than adding a
# second way to enumerate the bot's guilds. Capped implicitly by the
# project's own guild count (2-10), so no pagination/rate-limit concerns.
@api_bp.route("/guild/emojis/external")
@require_api_permission(LEVEL_OWNER)
def get_external_guild_emojis():
    current_guild_id = get_session_guild_id()
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"servers": [], "error": "BOT_TOKEN not set"})

    uid = current_user_id()
    from dashboard.permissions import is_trusted_super_admin
    from dashboard.auth import fetch_bot_guilds_full

    if is_trusted_super_admin(uid):
        allowed_guild_ids = None  # Developer bypass covers all bot guilds
    else:
        async def _get_user_granted_guilds():
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    """
                    SELECT guild_id FROM dashboard_users
                    WHERE user_id = ? AND enabled = 1
                      AND permission_level IN ('owner', 'admin', 'moderator')
                    """,
                    (uid,),
                )
                return {int(row[0]) for row in await cur.fetchall()}

        allowed_guild_ids = run_async(_get_user_granted_guilds())

    other_guilds = [
        g for g in fetch_bot_guilds_full()
        if int(g["id"]) != current_guild_id
        and (allowed_guild_ids is None or int(g["id"]) in allowed_guild_ids)
    ]

    headers = {"Authorization": f"Bot {bot_token}"}
    servers = []
    for g in other_guilds:
        try:
            resp = _req.get(
                f"https://discord.com/api/v10/guilds/{g['id']}/emojis",
                headers=headers, timeout=8)
            if resp.status_code != 200:
                continue
            raw_emojis = resp.json()
            if not isinstance(raw_emojis, list):
                continue
            emojis = [{
                "id": str(e["id"]), "name": e["name"], "animated": bool(e.get("animated")),
            } for e in raw_emojis if isinstance(e, dict) and e.get("id") and e.get("name")]
            if not emojis:
                continue
            emojis.sort(key=lambda e: e["name"].lower())
            servers.append({
                "guild_id": str(g["id"]),
                "guild_name": g.get("name", "Unknown Server"),
                "emojis": emojis,
            })
        except Exception:
            continue

    servers.sort(key=lambda s: s["guild_name"].lower())
    return jsonify({"servers": servers})


# ── Resolve a single user by ID (mention preview) ───────────────────────────
#
# Discord's GET /users/{id} is a global lookup keyed on the bot's own token
# and works for ANY valid user ID, regardless of whether that user shares a
# guild with the bot — there is no live member-list endpoint in this
# project (by design, see utils/discord_user_cache.py's header), but a
# single-user global lookup is a different, much narrower, already-public
# capability and doesn't require one. Used only to show a real username in
# the Embed Builder's <@id> mention preview instead of the raw ID; never
# exposes the bot token to the frontend.
@api_bp.route("/guild/resolve-user/<user_id>")
@require_api_permission(LEVEL_MODERATOR)
def resolve_single_user(user_id: str):
    if not user_id.isdigit():
        return jsonify({"resolved": False})
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"resolved": False})
    try:
        resp = _req.get(
            f"https://discord.com/api/v10/users/{user_id}",
            headers={"Authorization": f"Bot {bot_token}"}, timeout=6)
    except Exception:
        return jsonify({"resolved": False})
    if resp.status_code != 200:
        return jsonify({"resolved": False})
    u = resp.json()
    return jsonify({
        "resolved": True,
        "id": user_id,
        "username": u.get("global_name") or u.get("username") or user_id,
    })


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

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [r[0] for r in rows])

    user_map = run_async(resolve()) if rows else {}

    from dashboard.utils.user_identity import render_user_identity_html
    from dashboard.utils.currency_ctx import resolved as _currency, icon_html
    # Rendered as a CDN <img>, not interpolated raw: a configured custom
    # emoji is Discord message markup and an HTML parser would swallow it.
    _coin_emoji = icon_html(_currency(guild_id)["coins"]["emoji"])
    html = ""
    for r in rows:
        # r[0] (user_id) is still interpolated raw into the inline
        # onclick's JS-string args below — safe because it is always
        # numeric (the identity cell itself goes through the escaped
        # render_user_identity_html helper).
        u = user_map.get(r[0], {})
        identity_html = render_user_identity_html(
            r[0], u.get("display_name"), u.get("username"), u.get("avatar_url"))
        html += (
            f"<tr>"
            f"<td>{identity_html}</td>"
            f"<td><span class='badge badge-accent'>Level {r[2]}</span></td>"
            f"<td>{r[1]:,} XP</td>"
            f"<td>{_coin_emoji} {r[3]:,}</td>"
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
