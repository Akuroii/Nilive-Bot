import os
import re
import io
import time
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
from datetime import datetime, timedelta, timezone
from utils import app_emoji_cache
from utils.emoji import parse_emoji_input

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


def _verify_channel_in_guild(guild_id: int, channel_id: str, bot_token: str) -> tuple[bool, str | None, int]:
    """
    Verify via Discord REST that `channel_id` exists and belongs to `guild_id`.
    Supports both guild channels and threads (both include `guild_id` in
    GET /channels/{channel_id}), while rejecting DM channels, cross-guild
    channels, and indeterminate upstream failures (fail-closed).
    """
    try:
        ch_resp = _req.get(
            f"https://discord.com/api/v10/channels/{channel_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=8,
        )
    except Exception as exc:
        return False, f"Unable to verify target channel with Discord ({type(exc).__name__})", 502

    if ch_resp.status_code == 200:
        try:
            ch_data = ch_resp.json()
        except Exception:
            return False, "Malformed channel metadata from Discord", 502
        if not isinstance(ch_data, dict):
            return False, "Malformed channel metadata from Discord", 502
        ch_guild = str(ch_data.get("guild_id") or "").strip()
        if not ch_guild or ch_guild != str(guild_id):
            return False, "Target channel does not belong to the selected server", 403
        return True, None, 200

    if ch_resp.status_code == 403:
        return False, "Nero cannot access the selected channel in this server", 403
    if ch_resp.status_code == 404:
        return False, "Selected channel was not found in Discord", 404
    return False, f"Unable to verify target channel ownership (Discord {ch_resp.status_code})", 502


@api_bp.route("/embedbuilder/send", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_embedbuilder_send():
    req_start_ts = time.time()
    guild_id = get_session_guild_id()

    channel_id = request.form.get("channel_id") or (request.json or {}).get("channel_id") if request.is_json else request.form.get("channel_id")
    payload_raw = request.form.get("payload_json")
    if not channel_id:
        return jsonify({"success": False, "error": "Pick a channel to send to"})
    channel_id = str(channel_id).strip()
    if not channel_id.isascii() or not channel_id.isdigit() or not (0 < int(channel_id) <= 2**63 - 1):
        return jsonify({"success": False, "error": "Invalid Discord channel ID"}), 400
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

    # Server-side nonce & enforce_nonce for idempotent message creation (within Discord's 15-min window)
    nonce = payload.get("nonce")
    if nonce:
        discord_payload["nonce"] = str(nonce)[:25]
        discord_payload["enforce_nonce"] = True

    buttons = payload.get("buttons") or []
    select_menus = payload.get("select_menus") or []
    panel_config = payload.get("panel_config") or {}

    from dashboard.utils.role_validation import validate_assignable_role

    # Validate buttons & role assignments if present
    if buttons:
        if len(buttons) > 25:
            return jsonify({"success": False, "error": "A message can carry at most 25 buttons (5 Action Rows of 5 buttons)"}), 400

        for btn in buttons:
            role_id = btn.get("role_id") or btn.get("role")
            if not role_id or not str(role_id).strip().isdigit():
                return jsonify({"success": False, "error": "Each button must be assigned a valid Discord role"}), 400
            valid, err = validate_assignable_role(guild_id, str(role_id))
            if not valid:
                return jsonify({"success": False, "error": f"Invalid Button Role ({role_id}): {err}"}), 400

    # Validate select menus & role assignments if present
    if select_menus:
        if len(select_menus) > 5:
            return jsonify({"success": False, "error": "A message can carry at most 5 select menus (1 per Action Row)"}), 400

        for s_idx, sm in enumerate(select_menus):
            options = sm.get("options") or []
            if not options or len(options) == 0:
                return jsonify({"success": False, "error": f"Select menu #{s_idx + 1} must have at least 1 option"}), 400
            if len(options) > 25:
                return jsonify({"success": False, "error": f"Select menu #{s_idx + 1} exceeds 25 options limit"}), 400

            for opt_idx, opt in enumerate(options):
                role_id = opt.get("role_id") or opt.get("value")
                if not role_id or not str(role_id).strip().isdigit():
                    return jsonify({"success": False, "error": f"Option #{opt_idx + 1} in select menu #{s_idx + 1} must be assigned a valid Discord role"}), 400
                valid, err = validate_assignable_role(guild_id, str(role_id))
                if not valid:
                    return jsonify({"success": False, "error": f"Invalid Select Option Role ({role_id}): {err}"}), 400

    # Check total Action Rows limit (max 5 Action Rows)
    btn_row_count = (len(buttons) + 4) // 5 if buttons else 0
    sel_row_count = len(select_menus) if select_menus else 0
    if btn_row_count + sel_row_count > 5:
        return jsonify({"success": False, "error": f"Components exceed Discord's 5 Action Row limit ({btn_row_count} button row(s) + {sel_row_count} select menu(s) = {btn_row_count + sel_row_count} rows)"}), 400

    style_map = {
        "blurple": 1, "primary": 1, 1: 1,
        "gray": 2, "grey": 2, "secondary": 2, 2: 2,
        "green": 3, "success": 3, 3: 3,
        "red": 4, "danger": 4, 4: 4
    }

    action_rows = []
    # 1. Build button action rows
    for chunk_idx, i in enumerate(range(0, min(len(buttons), 25), 5)):
        chunk = buttons[i:i+5]
        row_components = []
        for b_idx, btn in enumerate(chunk):
            b_role = str(btn.get("role_id") or btn.get("role") or "").strip()
            b_label = str(btn.get("label") or "Role").strip()[:80]
            b_style = style_map.get(btn.get("color") or btn.get("style"), 1)
            b_custom_id = f"rr_{b_role}_draft_{chunk_idx}_{b_idx}"
            comp = {
                "type": 2,
                "style": b_style,
                "label": b_label,
                "custom_id": b_custom_id
            }
            if btn.get("emoji"):
                raw_em = str(btn.get("emoji")).strip()
                em_match = re.match(r"^<(a?):(\w+):(\d+)>$", raw_em)
                if em_match:
                    comp["emoji"] = {"id": em_match.group(3), "name": em_match.group(2), "animated": bool(em_match.group(1))}
                else:
                    comp["emoji"] = {"name": raw_em}
            row_components.append(comp)
        action_rows.append({"type": 1, "components": row_components})

    # 2. Build select menu action rows
    for s_idx, sm in enumerate(select_menus):
        row_idx = len(action_rows)
        s_custom_id = f"rr_sel_draft_{row_idx}"
        s_placeholder = str(sm.get("placeholder") or "Select a role...").strip()[:150]
        options_list = []
        for opt in (sm.get("options") or [])[:25]:
            opt_role = str(opt.get("role_id") or opt.get("value") or "").strip()
            opt_label = str(opt.get("label") or "Role Option").strip()[:100]
            opt_desc = str(opt.get("description") or "").strip()[:100] if opt.get("description") else None
            opt_comp = {
                "label": opt_label,
                "value": opt_role
            }
            if opt_desc:
                opt_comp["description"] = opt_desc
            if opt.get("emoji"):
                raw_em = str(opt.get("emoji")).strip()
                em_match = re.match(r"^<(a?):(\w+):(\d+)>$", raw_em)
                if em_match:
                    opt_comp["emoji"] = {"id": em_match.group(3), "name": em_match.group(2), "animated": bool(em_match.group(1))}
                else:
                    opt_comp["emoji"] = {"name": raw_em}
            options_list.append(opt_comp)

        sel_comp = {
            "type": 3,
            "custom_id": s_custom_id,
            "placeholder": s_placeholder,
            "min_values": 1,
            "max_values": 1,
            "options": options_list
        }
        action_rows.append({"type": 1, "components": [sel_comp]})

    if action_rows:
        discord_payload["components"] = action_rows

    ch_ok, ch_err, ch_status = _verify_channel_in_guild(guild_id, channel_id, bot_token)
    if not ch_ok:
        return jsonify({"success": False, "error": ch_err}), ch_status

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
        return jsonify({"success": False, "error": f"Network error contacting Discord: {e}"}), 502

    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        if resp.status_code == 403:
            return jsonify({"success": False, "error": f"Permission denied (403): Nero lacks permissions in channel {channel_id}"}), 403
        elif resp.status_code == 404:
            return jsonify({"success": False, "error": f"Channel not found (404): Channel {channel_id} does not exist or bot cannot access it"}), 404
        elif resp.status_code == 429:
            retry_after = None
            if hasattr(resp, "headers") and hasattr(resp.headers, "get"):
                raw_ra = resp.headers.get("Retry-After")
                if isinstance(raw_ra, (str, int, float)):
                    retry_after = str(raw_ra)
            if not retry_after:
                try:
                    j = resp.json()
                    if isinstance(j, dict) and isinstance(j.get("retry_after"), (int, float, str)):
                        retry_after = str(j["retry_after"])
                except Exception:
                    pass
            msg = f"Discord rate limit reached (429). Please retry after {retry_after}s." if retry_after else "Discord rate limit reached (429). Please wait a moment before trying again."
            res_dict = {"success": False, "error": msg}
            if retry_after:
                res_dict["retry_after"] = retry_after
            return jsonify(res_dict), 429
        elif resp.status_code >= 500:
            return jsonify({"success": False, "error": f"Discord service error ({resp.status_code}): {detail}"}), 502
        return jsonify({"success": False, "error": f"Discord API error {resp.status_code}: {detail}"}), 400

    msg_id = resp.json().get("id")
    msg_snowflake = int(msg_id) if msg_id and str(msg_id).isdigit() else None

    # If buttons or select menus were included and Discord created/returned the message, persist reaction role records
    if (buttons or select_menus) and msg_id:
        # Check whether this message pre-existed the current HTTP request attempt
        async def check_prior_state():
            async with aiosqlite.connect(DB_PATH) as db:
                c1 = await db.execute("SELECT 1 FROM reaction_role_panels WHERE message_id = ?", (int(msg_id),))
                has_panel = (await c1.fetchone()) is not None

                has_nonce = False
                if nonce:
                    c2 = await db.execute("SELECT 1 FROM embed_send_nonces WHERE nonce = ? AND guild_id = ?", (str(nonce)[:25], guild_id))
                    has_nonce = (await c2.fetchone()) is not None
                return has_panel, has_nonce

        try:
            prior_panel_existed, prior_nonce_existed = run_async(check_prior_state())
        except Exception:
            prior_panel_existed, prior_nonce_existed = False, False

        # Snowflake creation timestamp check: if Discord snowflake was created > 2 seconds prior to request start,
        # it was created by an earlier attempt and returned due to nonce deduplication.
        prior_snowflake_timestamp = False
        if nonce and msg_snowflake and msg_snowflake > (1 << 40):
            msg_created_ts = ((msg_snowflake >> 22) + 1420070400000) / 1000.0
            if msg_created_ts < req_start_ts - 2.0:
                prior_snowflake_timestamp = True

        is_preexisting = prior_panel_existed or prior_nonce_existed or prior_snowflake_timestamp

        # Record nonce mapping in database so subsequent retries identify pre-existence
        if nonce:
            async def record_nonce():
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT OR REPLACE INTO embed_send_nonces (nonce, guild_id, channel_id, message_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (str(nonce)[:25], guild_id, int(channel_id), int(msg_id), time.time()))
                    await db.commit()
            try:
                run_async(record_nonce())
            except Exception:
                pass

        async def persist_panel():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO reaction_role_panels
                    (message_id, guild_id, exclusive, max_roles, require_confirmation)
                    VALUES (?, ?, ?, ?, ?)
                """, (int(msg_id), guild_id, int(bool(panel_config.get("exclusive"))),
                      int(panel_config.get("max_roles", 0)),
                      int(bool(panel_config.get("require_confirmation")))))

                # Clean up any existing records for this message_id to ensure idempotency on retries
                await db.execute("DELETE FROM reaction_role_select_options WHERE message_id = ?", (int(msg_id),))
                await db.execute("DELETE FROM reaction_role_selects WHERE message_id = ?", (int(msg_id),))
                await db.execute("DELETE FROM reaction_roles WHERE message_id = ?", (int(msg_id),))
                await db.execute("DELETE FROM reaction_role_expiry WHERE message_id = ? AND user_id = 0", (int(msg_id),))

                # Persist buttons
                for chunk_idx, i in enumerate(range(0, min(len(buttons), 25), 5)):
                    chunk = buttons[i:i+5]
                    for b_idx, btn in enumerate(chunk):
                        raw_role = str(btn.get("role_id") or btn.get("role") or "").strip()
                        if not raw_role.isdigit():
                            continue
                        b_role = int(raw_role)
                        b_label = str(btn.get("label") or "").strip()
                        b_emoji = str(btn.get("emoji") or "").strip() or None
                        b_color = str(btn.get("color") or "blurple").lower()
                        b_booster = int(bool(btn.get("booster_only")))
                        raw_req = btn.get("required_role_id")
                        b_req = int(str(raw_req).strip()) if raw_req and str(raw_req).strip().isdigit() else None
                        b_expiry = int(btn.get("expiry_days", 0))
                        b_custom_id = f"rr_{b_role}_{msg_id}_{chunk_idx}_{b_idx}"

                        await db.execute("""
                            INSERT INTO reaction_roles
                            (guild_id, channel_id, message_id, button_label, button_emoji,
                             button_color, role_id, booster_only, required_role_id,
                             row_index, button_index, custom_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (guild_id, int(channel_id), int(msg_id), b_label, b_emoji,
                              b_color, b_role, b_booster, b_req, chunk_idx, b_idx, b_custom_id))

                        if b_expiry > 0:
                            expires_str = (datetime.now(timezone.utc) + timedelta(days=b_expiry)).isoformat()
                            await db.execute("""
                                INSERT OR REPLACE INTO reaction_role_expiry
                                (guild_id, user_id, role_id, message_id, expires_at)
                                VALUES (?, 0, ?, ?, ?)
                            """, (guild_id, b_role, int(msg_id), expires_str))

                # Persist select menus
                btn_rows = (len(buttons) + 4) // 5 if buttons else 0
                for s_idx, sm in enumerate(select_menus):
                    row_idx = btn_rows + s_idx
                    s_custom_id = f"rr_sel_{msg_id}_{row_idx}"
                    s_placeholder = str(sm.get("placeholder") or "Select a role...").strip()[:150]
                    cursor = await db.execute("""
                        INSERT INTO reaction_role_selects
                        (guild_id, channel_id, message_id, row_index, custom_id, placeholder, min_values, max_values)
                        VALUES (?, ?, ?, ?, ?, ?, 1, 1)
                    """, (guild_id, int(channel_id), int(msg_id), row_idx, s_custom_id, s_placeholder))
                    select_id = cursor.lastrowid

                    for opt_pos, opt in enumerate(sm.get("options") or []):
                        raw_role = str(opt.get("role_id") or opt.get("value") or "").strip()
                        if not raw_role.isdigit():
                            continue
                        opt_role = int(raw_role)
                        opt_label = str(opt.get("label") or "Role Option").strip()[:100]
                        opt_desc = str(opt.get("description") or "").strip()[:100] or None
                        opt_emoji = str(opt.get("emoji") or "").strip() or None
                        opt_booster = int(bool(opt.get("booster_only")))
                        raw_req = opt.get("required_role_id")
                        opt_req = int(str(raw_req).strip()) if raw_req and str(raw_req).strip().isdigit() else None
                        opt_expiry = int(opt.get("expiry_days", 0))

                        await db.execute("""
                            INSERT INTO reaction_role_select_options
                            (select_id, message_id, role_id, label, description, emoji, booster_only, required_role_id, expiry_days, position)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (select_id, int(msg_id), opt_role, opt_label, opt_desc, opt_emoji, opt_booster, opt_req, opt_expiry, opt_pos))

                        if opt_expiry > 0:
                            expires_str = (datetime.now(timezone.utc) + timedelta(days=opt_expiry)).isoformat()
                            await db.execute("""
                                INSERT OR REPLACE INTO reaction_role_expiry
                                (guild_id, user_id, role_id, message_id, expires_at)
                                VALUES (?, 0, ?, ?, ?)
                            """, (guild_id, opt_role, int(msg_id), expires_str))

                # Also insert into rr_panels for dashboard backwards compatibility
                panel_title = (embeds[0].get("title") if embeds else None) or content[:50] or "Interactive Panel"
                panel_desc = (embeds[0].get("description") if embeds else None) or content or ""
                panel_color = (f"#{embeds[0].get('color'):06x}" if embeds and embeds[0].get("color") else "#7c5cbf")
                all_buttons_repr = list(buttons)
                for sm in select_menus:
                    for opt in sm.get("options", []):
                        all_buttons_repr.append({
                            "label": opt.get("label", "Role"),
                            "role_id": opt.get("role_id"),
                            "color": "blurple",
                            "emoji": opt.get("emoji")
                        })
                await db.execute("""
                    INSERT INTO rr_panels
                        (guild_id, title, description, color, channel_id, buttons,
                         exclusive, max_roles, require_confirmation, required_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    guild_id, panel_title, panel_desc, panel_color, str(channel_id),
                    json.dumps(all_buttons_repr), int(bool(panel_config.get("exclusive"))),
                    int(panel_config.get("max_roles", 0)),
                    int(bool(panel_config.get("require_confirmation"))), ""
                ))
                await db.commit()

        try:
            run_async(persist_panel())
        except Exception as e:
            if is_preexisting:
                # Retry scenario: Discord returned an existing message (via nonce deduplication or pre-existing SQLite panel).
                # Transaction rollback preserves previously committed records; DO NOT delete existing Discord message.
                return jsonify({
                    "success": False,
                    "error": f"Failed to refresh reaction role persistence on retry: {e}. Prior configuration was preserved.",
                    "message_id": str(msg_id)
                }), 500

            # First-attempt orphan scenario: message was newly minted on Discord but never successfully persisted in SQLite.
            delete_ok = False
            try:
                del_resp = _req.delete(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}",
                    headers={"Authorization": f"Bot {bot_token}"},
                    timeout=5,
                )
                if del_resp.status_code in (200, 204, 404):
                    delete_ok = True
            except Exception:
                delete_ok = False

            if delete_ok:
                return jsonify({
                    "success": False,
                    "error": f"Failed to persist reaction role configuration: {e}. Message was rolled back."
                }), 500
            else:
                return jsonify({
                    "success": False,
                    "error": f"Failed to persist reaction roles ({e}) and compensation deletion failed. Orphan message {msg_id} remains in channel {channel_id} — please delete manually.",
                    "orphan_message_id": str(msg_id),
                    "channel_id": str(channel_id)
                }), 500

        # Patch message on Discord with deterministic custom_ids
        try:
            patched_rows = []
            for r_idx, chunk in enumerate(discord_payload.get("components", [])):
                new_row = {"type": 1, "components": []}
                for b_idx, c in enumerate(chunk.get("components", [])):
                    new_comp = dict(c)
                    if c.get("type") == 2:
                        if c.get("style") == 5:
                            # Link buttons keep url and have no custom_id
                            pass
                        elif "custom_id" in c:
                            c_role = c["custom_id"].split("_")[1]
                            new_comp["custom_id"] = f"rr_{c_role}_{msg_id}_{r_idx}_{b_idx}"
                    elif c.get("type") == 3:
                        new_comp["custom_id"] = f"rr_sel_{msg_id}_{r_idx}"
                    new_row["components"].append(new_comp)
                patched_rows.append(new_row)

            patch_resp = _req.patch(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}",
                headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                json={"components": patched_rows},
                timeout=10,
            )
            if patch_resp.status_code >= 400:
                # Discord rejected component update. Rollback SQLite records to prevent desynchronization
                async def rollback_panel():
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("DELETE FROM reaction_role_select_options WHERE message_id = ?", (int(msg_id),))
                        await db.execute("DELETE FROM reaction_role_selects WHERE message_id = ?", (int(msg_id),))
                        await db.execute("DELETE FROM reaction_roles WHERE message_id = ?", (int(msg_id),))
                        await db.execute("DELETE FROM reaction_role_panels WHERE message_id = ?", (int(msg_id),))
                        await db.commit()
                try:
                    run_async(rollback_panel())
                except Exception:
                    pass

                if not is_preexisting:
                    try:
                        _req.delete(
                            f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}",
                            headers={"Authorization": f"Bot {bot_token}"},
                            timeout=5,
                        )
                    except Exception:
                        pass

                return jsonify({
                    "success": False,
                    "error": f"Failed to configure interactive components on Discord (API error {patch_resp.status_code}). Configuration was not applied."
                }), 502
        except Exception as patch_exc:
            return jsonify({
                "success": False,
                "error": f"Failed to communicate with Discord to configure interactive components: {patch_exc}"
            }), 502

    log_action(guild_id, f"Sent embed builder message to channel {channel_id}", "embedbuilder")
    return jsonify({"success": True, "message_id": msg_id})


# ── Templates (whole message: content + all embeds + buttons + selects) ──

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

    doc = {
        "content": data.get("content") or "",
        "embeds": embeds,
        "buttons": data.get("buttons") or [],
        "select_menus": data.get("select_menus") or [],
        "panel_config": data.get("panel_config") or {}
    }

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
_VALID_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


# The raw-ID / <:name:id> / <a:name:id> parser used to live here. It moved
# to utils/emoji.py so the Economy currency fields can accept exactly the
# same formats without a second implementation; this alias keeps every
# existing call site working unchanged.
_parse_emoji_input = parse_emoji_input


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

