import os
import re
import io
import json
import base64
import aiosqlite
import requests as _req
from flask import jsonify, request
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_OWNER,
)
from dashboard.api import api_bp
from utils import app_emoji_cache

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


# ═══════════════════════════════════════════════════════════════════════
# APPLICATION EMOJIS — the "isn't available to the bot" fallback
#
# Bot's own guild emoji and other-servers emoji (both handled by the
# /api/guild/emojis* routes in core.py) are used directly when they
# already work. This covers the remaining case: an emoji from ANYWHERE
# (including servers the bot has never joined) pasted as its raw ID or
# <:name:id>/<a:name:id> markdown. Discord's CDN serves emoji images
# publicly by ID with no auth required, so downloading doesn't need the
# bot to share a guild with the source — only UPLOADING it as an
# Application Emoji (which the bot's own token owns, usable everywhere,
# no USE_EXTERNAL_EMOJIS needed) does.
# ═══════════════════════════════════════════════════════════════════════

MAX_APP_EMOJI_BYTES = 256 * 1024
_EMOJI_TOKEN_RE = re.compile(r"<(a?):(\w+):(\d+)>")
_VALID_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _parse_emoji_input(raw: str) -> tuple[str, str, bool] | None:
    """Accepts a raw ID, or a full <:name:id> / <a:name:id> token. Returns
    (source_id, source_name, animated) or None if nothing usable was found."""
    raw = (raw or "").strip()
    m = _EMOJI_TOKEN_RE.match(raw)
    if m:
        return m.group(3), m.group(2), bool(m.group(1))
    if raw.isdigit():
        return raw, f"emoji_{raw}", False
    return None


def _sanitize_emoji_name(name: str) -> str:
    cleaned = _VALID_NAME_RE.sub("_", name or "emoji").strip("_") or "emoji"
    if len(cleaned) < 2:
        cleaned = (cleaned + "_emoji")
    return cleaned[:32]


def _downscale_static_image(data: bytes) -> bytes | None:
    """Iteratively shrinks a static image until it fits Discord's 256KiB
    application-emoji cap. Returns None if it still doesn't fit after the
    smallest attempted size — the caller treats that as an import failure
    rather than uploading something Discord will just reject anyway."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    for scale in (0.75, 0.5, 0.35, 0.25, 0.15):
        w, h = img.size
        resized = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        candidate = buf.getvalue()
        if len(candidate) <= MAX_APP_EMOJI_BYTES:
            return candidate
    return None


def _upload_application_emoji(bot_token: str, app_id: str, name: str,
                               image_bytes: bytes, content_type: str) -> dict:
    data_uri = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"
    for attempt_name in (name, f"{name}_{os.urandom(2).hex()}"):
        resp = _req.post(
            f"https://discord.com/api/v10/applications/{app_id}/emojis",
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json={"name": attempt_name, "image": data_uri},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return {"ok": True, "data": resp.json()}
        try:
            detail = resp.json()
        except Exception:
            detail = {"message": resp.text}
        # A name collision is the only failure worth a single silent
        # retry (with a randomized suffix) — every other failure (image
        # invalid, 2000-emoji cap reached, rate limited) is real and
        # should surface to the person importing, not get retried blind.
        if "name" in json.dumps(detail).lower() and attempt_name == name:
            continue
        return {"ok": False, "error": detail.get("message", str(detail))}
    return {"ok": False, "error": "Could not resolve a unique emoji name"}


@api_bp.route("/app-emojis", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_list_app_emojis():
    async def fetch():
        await app_emoji_cache.ensure_table()
        return await app_emoji_cache.list_all()

    return jsonify({"results": run_async(fetch())})


@api_bp.route("/app-emojis/import", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_import_app_emoji():
    guild_id = get_session_guild_id()
    data = request.json or {}
    parsed = _parse_emoji_input(data.get("raw") or "")
    if not parsed:
        return jsonify({"success": False,
                        "error": "Paste an emoji ID or its <:name:id> / <a:name:id> markdown"})
    source_id, source_name, animated = parsed

    bot_token = os.getenv("DISCORD_TOKEN", "")
    app_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not bot_token or not app_id:
        return jsonify({"success": False, "error": "Bot credentials not configured"})

    async def do_import():
        await app_emoji_cache.ensure_table()
        cached = await app_emoji_cache.get_by_source(source_id)
        if cached:
            return {"success": True, "cached": True, **cached}

        cdn_url = f"https://cdn.discordapp.com/emojis/{source_id}.{'gif' if animated else 'png'}"
        try:
            img_resp = _req.get(cdn_url, timeout=15)
        except Exception as e:
            return {"success": False, "error": f"Could not reach Discord's CDN: {e}"}
        if img_resp.status_code != 200:
            return {"success": False, "error": "That emoji ID doesn't resolve to a real image on Discord's CDN"}

        image_bytes = img_resp.content
        content_type = img_resp.headers.get("Content-Type", "image/gif" if animated else "image/png")

        if len(image_bytes) > MAX_APP_EMOJI_BYTES:
            if animated:
                return {"success": False,
                        "error": ("This animated emoji is over Discord's 256KiB application-emoji "
                                  "limit and can't be auto-downscaled yet — try a smaller source, "
                                  "or import it manually from the Developer Portal.")}
            resized = _downscale_static_image(image_bytes)
            if not resized:
                return {"success": False,
                        "error": "This emoji is too large to fit under Discord's 256KiB limit even after downscaling"}
            image_bytes = resized
            content_type = "image/png"

        clean_name = _sanitize_emoji_name(source_name)
        result = _upload_application_emoji(bot_token, app_id, clean_name, image_bytes, content_type)
        if not result["ok"]:
            return {"success": False, "error": f"Discord rejected the import: {result['error']}"}

        emoji_obj = result["data"]
        await app_emoji_cache.save_mapping(
            source_id, source_name, animated, emoji_obj["id"], emoji_obj["name"])
        return {"success": True, "cached": False,
                "id": emoji_obj["id"], "name": emoji_obj["name"], "animated": animated}

    result = run_async(do_import())
    if result.get("success"):
        log_action(guild_id, f"Imported application emoji '{result.get('name')}'", "embedbuilder")
    return jsonify(result)

