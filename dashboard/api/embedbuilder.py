import os
import json
import aiosqlite
import requests as _req
from flask import jsonify, request
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_OWNER,
)
from dashboard.api import api_bp

# ── Embed Builder v2 — Composer (Content + multi-Embed + Attachments) ──────
#
# The dashboard Embed Builder previously only supported building ONE embed
# and either saving it as a template (embed_templates: {guild_id, name,
# data}) or copying its JSON — actually sending anything to Discord only
# ever happened Discord-side, via /embed_send loading a saved template
# (cogs/embedbuilder.py).
#
# This adds the missing "compose and send right now" path: a plain
# message content field, up to 10 embeds, and up to 10 real file
# attachments, sent directly to a chosen channel via Discord's REST API
# using the bot token — same "call Discord directly from the Flask
# process" pattern dashboard/api/botprofile.py's apply_bot_profile_via_rest
# and dashboard/api/moderation.py's quick-action route already use.
#
# Attachments are NEVER written to disk or the database — the browser
# holds them (IndexedDB, client-side only) and uploads them directly in
# the multipart POST at send time. This route only ever sees them as
# an in-memory multipart upload for the duration of one request, exactly
# like any other file-upload endpoint; nothing here persists them.
#
# embed_templates.data now stores the WHOLE message
# ({"content": "...", "embeds": [...]}) instead of a single embed dict.
# cogs/embedbuilder.py's /embed_send and /embed_list are updated to
# understand both the new shape and legacy single-embed rows saved
# before this change, so old templates keep working.

MAX_EMBEDS = 10
MAX_ATTACHMENTS = 10
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024  # Discord's default non-boosted upload cap


def _validate_embeds(embeds) -> tuple[bool, str]:
    if not isinstance(embeds, list):
        return False, "embeds must be a list"
    if len(embeds) > MAX_EMBEDS:
        return False, f"A message can carry at most {MAX_EMBEDS} embeds"
    for e in embeds:
        if not isinstance(e, dict):
            return False, "Each embed must be an object"
    return True, ""


@api_bp.route("/embedbuilder/send", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_embedbuilder_send():
    guild_id = get_session_guild_id()

    channel_id = request.form.get("channel_id") or (request.json or {}).get("channel_id") if request.is_json else request.form.get("channel_id")
    payload_raw = request.form.get("payload_json")
    if not channel_id:
        return jsonify({"success": False, "error": "Pick a channel to send to"})
    if not payload_raw:
        return jsonify({"success": False, "error": "Missing message payload"})

    try:
        payload = json.loads(payload_raw)
    except Exception:
        return jsonify({"success": False, "error": "Malformed payload_json"})

    content = (payload.get("content") or "").strip()
    embeds  = payload.get("embeds") or []

    ok, err = _validate_embeds(embeds)
    if not ok:
        return jsonify({"success": False, "error": err})

    files = request.files.getlist("files")
    if len(files) > MAX_ATTACHMENTS:
        return jsonify({"success": False,
                        "error": f"A message can carry at most {MAX_ATTACHMENTS} attachments"})
    total_bytes = 0
    for f in files:
        f.stream.seek(0, os.SEEK_END)
        total_bytes += f.stream.tell()
        f.stream.seek(0)
    if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
        return jsonify({"success": False,
                        "error": "Attachments exceed the 25MB total upload limit"})

    if not content and not embeds and not files:
        return jsonify({"success": False, "error": "Nothing to send — add content, an embed, or an attachment"})

    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return jsonify({"success": False, "error": "Bot token not configured"})

    discord_payload = {}
    if content:
        discord_payload["content"] = content
    if embeds:
        discord_payload["embeds"] = embeds

    multipart_files = []
    for i, f in enumerate(files):
        multipart_files.append(
            (f"files[{i}]", (f.filename or f"attachment{i}", f.stream.read(), f.mimetype or "application/octet-stream")))

    try:
        if multipart_files:
            resp = _req.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}"},
                data={"payload_json": json.dumps(discord_payload)},
                files=multipart_files,
                timeout=30,
            )
        else:
            resp = _req.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                json=discord_payload,
                timeout=15,
            )
    except Exception as e:
        return jsonify({"success": False, "error": f"Network error contacting Discord: {e}"})

    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        return jsonify({"success": False, "error": f"Discord API error {resp.status_code}: {detail}"})

    log_action(guild_id, f"Sent embed builder message to channel {channel_id}", "embedbuilder")
    return jsonify({"success": True, "message_id": resp.json().get("id")})


# ── Templates (whole message: content + all embeds) ────────────────────────

@api_bp.route("/embedbuilder/template/save", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_embedbuilder_save_template():
    guild_id = get_session_guild_id()
    data = request.json or {}
    name = (data.get("name") or "").strip().lower()
    if not name:
        return jsonify({"success": False, "error": "Template name required"})

    embeds = data.get("embeds") or []
    ok, err = _validate_embeds(embeds)
    if not ok:
        return jsonify({"success": False, "error": err})

    doc = {"content": data.get("content") or "", "embeds": embeds}

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO embed_templates (guild_id, name, data)
                VALUES (?, ?, ?)
            """, (guild_id, name, json.dumps(doc)))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved embed builder template '{name}'", "embedbuilder")
    return jsonify({"success": True})


@api_bp.route("/embedbuilder/templates", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_embedbuilder_list_templates():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT name FROM embed_templates WHERE guild_id = ? ORDER BY name ASC",
                (guild_id,))
            return [r[0] for r in await cursor.fetchall()]

    return jsonify({"templates": run_async(fetch())})


@api_bp.route("/embedbuilder/template/<name>", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_embedbuilder_get_template(name: str):
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT data FROM embed_templates WHERE guild_id = ? AND name = ?",
                (guild_id, name.lower()))
            row = await cursor.fetchone()
            return row[0] if row else None

    raw = run_async(fetch())
    if not raw:
        return jsonify({"template": None})

    try:
        doc = json.loads(raw)
    except Exception:
        return jsonify({"template": None})

    # Legacy rows: data was a single embed dict, no "embeds"/"content" keys.
    if "embeds" not in doc and "content" not in doc:
        doc = {"content": "", "embeds": [doc]}

    return jsonify({"template": doc})


@api_bp.route("/embedbuilder/template/<name>", methods=["DELETE"])
@require_api_permission(LEVEL_OWNER)
def api_embedbuilder_delete_template(name: str):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM embed_templates WHERE guild_id = ? AND name = ?",
                (guild_id, name.lower()))
            await db.commit()

    run_async(delete())
    log_action(guild_id, f"Deleted embed builder template '{name}'", "embedbuilder")
    return jsonify({"success": True})
