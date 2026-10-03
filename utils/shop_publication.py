"""Server-side Shop Publication lifecycle. No purchase/runtime dispatch lives here."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import aiosqlite
import requests

from dashboard.utils.async_utils import run_async
from database import DB_PATH
from utils.currency import get_currency_config
import utils.shop_publisher as SP

DISCORD_API = "https://discord.com/api/v10"
VIEW_CHANNEL, SEND_MESSAGES, EMBED_LINKS, READ_MESSAGE_HISTORY, ADMINISTRATOR = (
    1 << 10, 1 << 11, 1 << 14, 1 << 16, 1 << 3)
SUPPORTED_CHANNEL_TYPES = {0, 5}
BLOCKING_PREVIEW_CODES = {"validation", "action_limit", "presentation_limit"}
_PRODUCT_COLUMNS = ("id, name, type, price, price_diamonds, enabled, current_stock, max_stock, "
                    "prestige_tier, featured, description, duration_hours, required_level, "
                    "rarity, icon_url, option_of_id, role_id, required_role_id, "
                    "xp_boost_multiplier")
_PRODUCT_KEYS = ("id", "name", "type", "price", "price_diamonds", "enabled", "current_stock",
                 "max_stock", "prestige_tier", "featured", "description", "duration_hours",
                 "required_level", "rarity", "icon_url", "option_of_id", "role_id", "required_role_id",
                 "xp_boost_multiplier")


class PublicationError(Exception):
    def __init__(self, message, *, code="publication_error", status=400, uncertain=False):
        super().__init__(message)
        self.code, self.status, self.uncertain = code, status, uncertain


class DiscordFailure(Exception):
    def __init__(self, message, *, status=None, discord_code=None, uncertain=False):
        super().__init__(message)
        self.status, self.discord_code, self.uncertain = status, discord_code, uncertain

    @property
    def message_missing(self):
        return self.status == 404 and self.discord_code == 10008


class DiscordREST:
    def __init__(self, token=None, timeout=15.0):
        self.token = (token if token is not None else os.getenv("DISCORD_TOKEN", "")).strip()
        self.timeout = timeout
        if not self.token:
            raise PublicationError("Bot token is not configured.", code="bot_unavailable", status=503)

    def request(self, method, path, *, payload=None, mutating=False):
        headers = {"Authorization": f"Bot {self.token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = requests.request(method, f"{DISCORD_API}{path}", headers=headers,
                                        json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DiscordFailure(f"Discord transport result is uncertain: {exc}",
                                 uncertain=mutating) from exc
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise DiscordFailure("Discord returned an unreadable success response.",
                                     status=response.status_code, uncertain=mutating) from exc
        detail, code = response.text or f"HTTP {response.status_code}", None
        try:
            body = response.json()
            detail = str(body.get("message") or detail)
            code = int(body["code"]) if body.get("code") is not None else None
        except (ValueError, AttributeError, TypeError):
            pass
        raise DiscordFailure(f"Discord API {response.status_code}: {detail}",
                             status=response.status_code, discord_code=code,
                             uncertain=mutating and response.status_code >= 500)

    def validate_channel_and_permissions(self, guild_id, channel_id, operation):
        try:
            channels = self.request("GET", f"/guilds/{guild_id}/channels")
        except DiscordFailure as exc:
            raise PublicationError(f"Could not verify a channel in this server: {exc}",
                                   code="channel_verification_failed", status=502) from exc
        if not isinstance(channels, list):
            raise PublicationError("Discord returned an invalid channel list.", code="channel_verification_failed", status=502)
        channel = next((c for c in channels if isinstance(c, dict) and str(c.get("id")) == str(channel_id)), None)
        if channel is None:
            raise PublicationError("The selected channel is not available in this server.", code="unknown_channel", status=404)
        try:
            typ = int(channel.get("type"))
        except (TypeError, ValueError):
            typ = -1
        if typ not in SUPPORTED_CHANNEL_TYPES:
            raise PublicationError("Only guild text and announcement/news channels can be used.",
                                   code="unsupported_channel_type", status=400)
        required = VIEW_CHANNEL
        if operation in {"publish", "update"}:
            required |= SEND_MESSAGES | EMBED_LINKS
        if operation in {"update", "unpublish"}:
            required |= READ_MESSAGE_HISTORY
        bits = self._effective_bot_permissions(guild_id, channel)
        if bits & ADMINISTRATOR:
            return channel
        if bits & required != required:
            missing = []
            if not bits & VIEW_CHANNEL: missing.append("View Channel")
            if operation in {"publish", "update"}:
                if not bits & SEND_MESSAGES: missing.append("Send Messages")
                if not bits & EMBED_LINKS: missing.append("Embed Links")
            if operation in {"update", "unpublish"} and not bits & READ_MESSAGE_HISTORY:
                missing.append("Read Message History")
            raise PublicationError("The bot lacks required channel permissions: " + ", ".join(missing) + ".",
                                   code="bot_missing_permissions", status=403)
        return channel

    def _effective_bot_permissions(self, guild_id, channel):
        try:
            bot_user = self.request("GET", "/users/@me")
            bot_id = str(bot_user["id"])
            roles = self.request("GET", f"/guilds/{guild_id}/roles")
            member = self.request("GET", f"/guilds/{guild_id}/members/{bot_id}")
        except (DiscordFailure, KeyError, TypeError) as exc:
            raise PublicationError(f"Could not verify the bot's channel permissions: {exc}",
                                   code="permission_verification_failed", status=502) from exc
        if (not isinstance(bot_user, dict) or not bot_id.isascii() or not bot_id.isdigit()
                or not isinstance(roles, list) or not isinstance(member, dict)
                or not isinstance(member.get("roles"), list)):
            raise PublicationError("Discord returned incomplete bot permission data.",
                                   code="permission_verification_failed", status=502)
        if any(not isinstance(r, dict) or r.get("id") is None or
               not self._valid_permission_value(r.get("permissions")) for r in roles):
            raise PublicationError("Discord returned malformed role permissions.",
                                   code="permission_verification_failed", status=502)
        role_map = {str(r["id"]): r for r in roles}
        everyone = role_map.get(str(guild_id))
        if everyone is None:
            raise PublicationError("The server's @everyone role could not be verified.",
                                   code="permission_verification_failed", status=502)
        role_ids = {str(rid) for rid in member["roles"]}
        if any(not rid.isascii() or not rid.isdigit() or rid not in role_map for rid in role_ids):
            raise PublicationError("The bot's assigned roles could not be verified.",
                                   code="permission_verification_failed", status=502)
        base = self._permission_bits(everyone["permissions"])
        for rid in role_ids:
            base |= self._permission_bits(role_map[rid]["permissions"])
        if base & ADMINISTRATOR:
            return base
        if "permission_overwrites" not in channel or not isinstance(channel["permission_overwrites"], list):
            raise PublicationError("Discord did not provide channel permission overwrites.",
                                   code="permission_verification_failed", status=502)
        overwrites = channel["permission_overwrites"]
        for ow in overwrites:
            if (not isinstance(ow, dict) or ow.get("id") is None
                    or not str(ow.get("id")).isascii() or not str(ow.get("id")).isdigit()
                    or str(ow.get("type")) not in {"0", "1"}
                    or not self._valid_permission_value(ow.get("allow"))
                    or not self._valid_permission_value(ow.get("deny"))):
                raise PublicationError("Discord returned malformed channel permission overwrites.",
                                       code="permission_verification_failed", status=502)
        for ow in overwrites:
            if str(ow["id"]) == str(guild_id) and str(ow["type"]) == "0":
                base = self._apply_overwrite(base, ow)
                break
        deny = allow = 0
        for ow in overwrites:
            if str(ow["type"]) == "0" and str(ow["id"]) in role_ids:
                deny |= self._permission_bits(ow["deny"])
                allow |= self._permission_bits(ow["allow"])
        base = (base & ~deny) | allow
        for ow in overwrites:
            if str(ow["id"]) == bot_id and str(ow["type"]) == "1":
                base = self._apply_overwrite(base, ow)
                break
        return base

    @staticmethod
    def _valid_permission_value(value):
        if isinstance(value, bool): return False
        try: return int(value) >= 0
        except (TypeError, ValueError): return False

    @staticmethod
    def _permission_bits(value):
        try: return int(value or 0)
        except (TypeError, ValueError): return 0

    @classmethod
    def _apply_overwrite(cls, permissions, overwrite):
        return (permissions & ~cls._permission_bits(overwrite.get("deny"))) | cls._permission_bits(overwrite.get("allow"))

    def create_message(self, channel_id, payload):
        result = self.request("POST", f"/channels/{channel_id}/messages", payload=payload, mutating=True)
        if not isinstance(result, dict) or not result.get("id"):
            raise DiscordFailure("Discord's send response did not confirm a message ID.", uncertain=True)
        try: return int(result["id"])
        except (TypeError, ValueError) as exc:
            raise DiscordFailure("Discord returned an invalid message ID.", uncertain=True) from exc

    def fetch_message(self, channel_id, message_id):
        return self.request("GET", f"/channels/{channel_id}/messages/{message_id}")

    def edit_message(self, channel_id, message_id, payload):
        return self.request("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload=payload, mutating=True)

    def delete_message(self, channel_id, message_id):
        return self.request("DELETE", f"/channels/{channel_id}/messages/{message_id}", mutating=True)


def _record(row):
    return {"id": int(row[0]), "guild_id": int(row[1]), "design_id": int(row[2]),
            "channel_id": int(row[3]), "message_id": int(row[4]) if row[4] is not None else None,
            "status": row[5], "last_error": row[6], "created_at": row[7],
            "updated_at": row[8], "last_published_at": row[9]}

_RECORD_SELECT = ("SELECT id, guild_id, design_id, channel_id, message_id, status, last_error, "
                  "created_at, updated_at, last_published_at FROM shop_publications")


def list_publications(guild_id, design_id):
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(_RECORD_SELECT + " WHERE guild_id=? AND design_id=? ORDER BY created_at DESC,id DESC",
                                   (guild_id, design_id))
            return await cur.fetchall()
    return [_record(r) for r in run_async(fetch())]


def get_publication(guild_id, publication_id):
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(_RECORD_SELECT + " WHERE guild_id=? AND id=?", (guild_id, publication_id))
            return await cur.fetchone()
    row = run_async(fetch())
    return _record(row) if row else None


def design_exists(guild_id, design_id):
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT 1 FROM shop_designs WHERE guild_id=? AND id=?", (guild_id, design_id))
            return await cur.fetchone() is not None
    return bool(run_async(fetch()))


def _design_ids(design):
    ids = []
    for value in design.get("products") or []:
        if isinstance(value, int) and not isinstance(value, bool): ids.append(value)
    action = design.get("action")
    entries = action.get("entries") if isinstance(action, dict) else []
    for entry in entries or []:
        value = entry.get("product_id") if isinstance(entry, dict) else None
        if isinstance(value, int) and not isinstance(value, bool): ids.append(value)
    return list(dict.fromkeys(ids))


def _load_design_and_rows(guild_id, design_id):
    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT name,design_json FROM shop_designs WHERE guild_id=? AND id=?", (guild_id, design_id))
            row = await cur.fetchone()
            if not row: return None, [], ""
            name, raw = row
            try: design = json.loads(raw)
            except (TypeError, ValueError): return {"_invalid": True}, [], name
            if not isinstance(design, dict): return {"_invalid": True}, [], name
            design = dict(design); design["id"] = int(design_id)
            wanted = _design_ids(design)
            if not wanted: return design, [], name
            marks = ",".join("?" for _ in wanted)
            cur = await db.execute(f"SELECT {_PRODUCT_COLUMNS} FROM shop_items WHERE guild_id=? AND id IN ({marks})",
                                   (guild_id, *wanted))
            return design, await cur.fetchall(), name
    fetched = run_async(fetch())
    if fetched[0] is None:
        raise PublicationError("Saved Design was not found in this server.", code="unknown_design", status=404)
    design, rows, name = fetched
    if design.get("_invalid"):
        raise PublicationError("The saved Design data is invalid.", code="invalid_saved_design", status=409)
    return design, {int(r[0]): dict(zip(_PRODUCT_KEYS, r)) for r in rows}, str(name or "")


def prepare_design(guild_id, design_id):
    design, rows, name = _load_design_and_rows(guild_id, design_id)
    presentation = design.get("presentation")
    errors = []
    if not isinstance(presentation, dict):
        errors.append({"code": "invalid_presentation", "path": "presentation", "message": "Saved presentation must be an object."})
    else:
        if presentation.get("mode") not in SP.PRESENTATION_MODES:
            errors.append({"code": "invalid_presentation_mode", "path": "presentation.mode", "message": "Saved presentation mode is unsupported."})
        if not isinstance(presentation.get("content"), str):
            errors.append({"code": "invalid_presentation_content", "path": "presentation.content", "message": "Saved presentation content must be text."})
        embeds = presentation.get("embeds")
        if not isinstance(embeds, list) or any(not isinstance(embed, dict) for embed in embeds):
            errors.append({"code": "invalid_presentation_embeds", "path": "presentation.embeds", "message": "Saved presentation embeds must be a list of objects."})
    if errors:
        return {"design_id": design_id, "design_name": name, "blocking_errors": errors,
                "warnings": [], "warning_token": None, "payload": None, "action": None}
    try: problems = SP.validate_design(design, rows)
    except Exception as exc:
        return {"design_id": design_id, "design_name": name,
                "blocking_errors": [{"code": "design_validation_failed", "path": "", "message": f"Could not validate the saved Design: {exc}"}],
                "warnings": [], "warning_token": None, "payload": None, "action": None}
    if problems:
        return {"design_id": design_id, "design_name": name, "blocking_errors": problems,
                "warnings": [], "warning_token": None, "payload": None, "action": None}
    try:
        currency = run_async(get_currency_config(guild_id))
        resolved = SP.preview_design(design, rows, currency)
    except Exception as exc:
        return {"design_id": design_id, "design_name": name,
                "blocking_errors": [{"code": "resolution_failed", "path": "", "message": f"Could not resolve the saved Design: {exc}"}],
                "warnings": [], "warning_token": None, "payload": None, "action": None}
    warnings = list(resolved.get("warnings") or [])
    blocking = [w for w in warnings if w.get("code") in BLOCKING_PREVIEW_CODES]
    nonblocking = [w for w in warnings if w.get("code") not in BLOCKING_PREVIEW_CODES]
    payload = message_payload(resolved)
    fingerprint = _prepared_fingerprint(design, rows, resolved, payload)
    return {"design_id": design_id, "design_name": name, "blocking_errors": blocking,
            "warnings": nonblocking, "warning_token": _warning_token(nonblocking, fingerprint),
            "payload": None if blocking else payload, "action": resolved.get("action")}


def _prepared_fingerprint(design, rows, resolved, payload):
    canonical = json.dumps({"design": design, "products": rows, "resolved": resolved, "payload": payload},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _warning_token(warnings, fingerprint=""):
    raw = json.dumps({"warnings": warnings, "prepared": fingerprint}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def require_warning_ack(prepared, supplied_token):
    return not prepared.get("warnings") or (isinstance(supplied_token, str) and supplied_token == prepared.get("warning_token"))


def _emoji_component(raw):
    raw = (raw or "").strip()
    if not raw: return None
    if raw.startswith("<") and raw.endswith(">"):
        import re
        match = re.fullmatch(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>", raw)
        if not match: return None
        result = {"id": match.group(3), "name": match.group(2)}
        if match.group(1): result["animated"] = True
        return result
    return {"name": raw}


def action_components(action):
    entries = action.get("entries") or []
    if not entries: return []
    if action.get("kind") == "buttons":
        rows = []
        for start in range(0, len(entries), 5):
            buttons = []
            for entry in entries[start:start + 5]:
                button = {"type": 2, "style": 3, "custom_id": entry["custom_id"], "label": entry.get("label") or "Buy"}
                emoji = _emoji_component(entry.get("emoji") or "")
                if emoji: button["emoji"] = emoji
                buttons.append(button)
            rows.append({"type": 1, "components": buttons})
        return rows
    component = {"type": 3, "custom_id": action["component_custom_id"],
                 "placeholder": action.get("placeholder") or "Select…", "min_values": 1,
                 "max_values": 1, "options": []}
    for entry in entries:
        option = {"label": entry.get("label") or "Option", "value": entry["custom_id"]}
        if entry.get("description"): option["description"] = entry["description"]
        emoji = _emoji_component(entry.get("emoji") or "")
        if emoji: option["emoji"] = emoji
        component["options"].append(option)
    return [{"type": 1, "components": [component]}]


def message_payload(resolved):
    payload: dict[str, Any] = {}
    if resolved.get("content"): payload["content"] = resolved["content"]
    if resolved.get("embeds"): payload["embeds"] = resolved["embeds"]
    components = action_components(resolved.get("action") or {})
    if components: payload["components"] = components
    return payload


def create_pending(guild_id, design_id, channel_id):
    async def insert():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT 1 FROM shop_designs WHERE guild_id=? AND id=?", (guild_id, design_id))
            if await cur.fetchone() is None:
                await db.rollback()
                raise PublicationError("Saved Design was not found in this server.", code="unknown_design", status=404)
            cur = await db.execute("INSERT INTO shop_publications(guild_id,design_id,channel_id,status) VALUES(?,?,?,'pending')",
                                   (guild_id, design_id, channel_id))
            pid = cur.lastrowid; await db.commit()
            cur = await db.execute(_RECORD_SELECT + " WHERE id=? AND guild_id=?", (pid, guild_id))
            return await cur.fetchone()
    return _record(run_async(insert()))


def set_publication(guild_id, publication_id, *, status, message_id=None, update_message=False,
                    last_error=None, successful=False):
    allowed = {"pending", "published", "failed", "attention", "updating", "unpublishing", "replacement_sending", "replacement_send_uncertain", "replacement_retry_authorized"}
    if status not in allowed: raise ValueError("invalid publication status")
    async def update():
        async with aiosqlite.connect(DB_PATH) as db:
            fields = ["status=?", "last_error=?", "updated_at=CURRENT_TIMESTAMP"]
            vals = [status, last_error]
            if update_message: fields.append("message_id=?"); vals.append(message_id)
            if successful: fields.append("last_published_at=CURRENT_TIMESTAMP")
            vals.extend([publication_id, guild_id])
            await db.execute(f"UPDATE shop_publications SET {','.join(fields)} WHERE id=? AND guild_id=?", vals)
            await db.commit()
    run_async(update())
    return get_publication(guild_id, publication_id)


def _conditional_transition(guild_id, publication_id, expected_statuses, new_status, *,
                            expected_message_id=None, match_message=False, message_id=None,
                            update_message=False, last_error=None, successful=False):
    """Atomically transition only the exact state/message observed by caller."""
    async def transition():
        async with aiosqlite.connect(DB_PATH) as db:
            fields = ["status=?", "last_error=?", "updated_at=CURRENT_TIMESTAMP"]
            values = [new_status, last_error]
            if update_message:
                fields.append("message_id=?")
                values.append(message_id)
            if successful:
                fields.append("last_published_at=CURRENT_TIMESTAMP")
            marks = ",".join("?" for _ in expected_statuses)
            where = f"id=? AND guild_id=? AND status IN ({marks})"
            values.extend([publication_id, guild_id, *expected_statuses])
            if match_message:
                where += " AND message_id=?"
                values.append(expected_message_id)
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(f"UPDATE shop_publications SET {','.join(fields)} WHERE {where}", values)
            await db.commit()
            return cur.rowcount > 0
    changed = bool(run_async(transition()))
    return changed, get_publication(guild_id, publication_id)


def claim_update(guild_id, publication_id, old_message_id):
    return _conditional_transition(
        guild_id, publication_id,
        ("published", "attention", "replacement_retry_authorized"), "updating",
        expected_message_id=old_message_id, match_message=True,
    )[0]


def claim_replacement(guild_id, publication_id, old_message_id):
    return _conditional_transition(
        guild_id, publication_id, ("updating",), "replacement_sending",
        expected_message_id=old_message_id, match_message=True,
    )[0]


def claim_unpublish(guild_id, publication_id, old_message_id):
    return _conditional_transition(
        guild_id, publication_id, ("published", "attention"), "unpublishing",
        expected_message_id=old_message_id, match_message=True,
    )[0]


def remove_publication(guild_id, publication_id, *, expected_statuses=None,
                       expected_message_id=None, match_message=False):
    expected_statuses = tuple(expected_statuses or ())
    if not expected_statuses:
        return False
    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            marks = ",".join("?" for _ in expected_statuses)
            where = f"id=? AND guild_id=? AND status IN ({marks})"
            values = [publication_id, guild_id, *expected_statuses]
            if match_message:
                where += " AND message_id=?"
                values.append(expected_message_id)
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(f"DELETE FROM shop_publications WHERE {where}", values)
            await db.commit()
            return cur.rowcount > 0
    return bool(run_async(delete()))


def post_publish(guild_id, design_id, channel_id, warning_token=None, *, rest=None):
    rest = rest or DiscordREST()
    prepared = prepare_design(guild_id, design_id)
    if prepared["blocking_errors"]: return {"outcome": "blocked", "prepared": prepared}
    rest.validate_channel_and_permissions(guild_id, channel_id, "publish")
    if not require_warning_ack(prepared, warning_token): return {"outcome": "ack_required", "prepared": prepared}
    fresh = prepare_design(guild_id, design_id)
    if fresh["blocking_errors"]: return {"outcome": "blocked", "prepared": fresh}
    rest.validate_channel_and_permissions(guild_id, channel_id, "publish")
    if not require_warning_ack(fresh, warning_token): return {"outcome": "ack_required", "prepared": fresh}
    record = create_pending(guild_id, design_id, channel_id)
    try: message_id = rest.create_message(channel_id, fresh["payload"])
    except DiscordFailure as exc:
        status = "pending" if exc.uncertain else "failed"
        record = set_publication(guild_id, record["id"], status=status, last_error=str(exc))
        return {"outcome": "uncertain" if exc.uncertain else "failed", "publication": record, "error": str(exc)}
    record = set_publication(guild_id, record["id"], status="published", message_id=message_id,
                             update_message=True, successful=True)
    return {"outcome": "published", "publication": record, "warnings": fresh["warnings"]}


def update_publication(guild_id, publication, warning_token=None, *, rest=None):
    rest = rest or DiscordREST()
    if publication.get("status") in {"updating", "unpublishing", "replacement_sending", "replacement_send_uncertain"}:
        return {"outcome": "replacement_uncertain", "publication": publication,
                "error": "This Publication has an operation in progress or requires explicit recovery."}
    design_id, channel_id = int(publication["design_id"]), int(publication["channel_id"])
    prepared = prepare_design(guild_id, design_id)
    if prepared["blocking_errors"]: return {"outcome": "blocked", "prepared": prepared}
    try: rest.validate_channel_and_permissions(guild_id, channel_id, "update")
    except PublicationError as exc:
        return {"outcome": "attention", "publication": get_publication(guild_id, publication["id"]), "error": str(exc)}
    if not require_warning_ack(prepared, warning_token): return {"outcome": "ack_required", "prepared": prepared}
    fresh = prepare_design(guild_id, design_id)
    if fresh["blocking_errors"]: return {"outcome": "blocked", "prepared": fresh}
    try: rest.validate_channel_and_permissions(guild_id, channel_id, "update")
    except PublicationError as exc:
        return {"outcome": "attention", "publication": get_publication(guild_id, publication["id"]), "error": str(exc)}
    if not require_warning_ack(fresh, warning_token): return {"outcome": "ack_required", "prepared": fresh}
    pid, message_id = int(publication["id"]), publication.get("message_id")
    if not message_id:
        error = "This Publication has no confirmed message ID; it was not retried."
        return {"outcome": "attention", "publication": get_publication(guild_id, pid), "error": error}

    if not claim_update(guild_id, pid, int(message_id)):
        return {"outcome": "attention", "publication": get_publication(guild_id, pid),
                "error": "This Publication changed state; reload it before another operation."}

    def finish_replacement():
        if not claim_replacement(guild_id, pid, int(message_id)):
            return {"outcome": "replacement_uncertain", "publication": get_publication(guild_id, pid),
                    "error": "Replacement state changed; no send was attempted."}
        try:
            new_id = rest.create_message(channel_id, fresh["payload"])
        except DiscordFailure as exc:
            new_state = "replacement_send_uncertain" if exc.uncertain else "attention"
            _conditional_transition(guild_id, pid, ("replacement_sending",), new_state,
                                    expected_message_id=int(message_id), match_message=True,
                                    last_error=str(exc))
            if exc.uncertain:
                return {"outcome": "replacement_uncertain", "publication": get_publication(guild_id, pid),
                        "error": "Replacement send outcome is uncertain; no retry was attempted."}
            return {"outcome": "attention", "publication": get_publication(guild_id, pid), "error": str(exc)}
        changed, saved = _conditional_transition(
            guild_id, pid, ("replacement_sending",), "published",
            expected_message_id=int(message_id), match_message=True,
            message_id=new_id, update_message=True, successful=True,
        )
        if not changed:
            return {"outcome": "replacement_uncertain", "publication": saved,
                    "error": "Discord confirmed a replacement, but Publication state changed; operator review is required."}
        return {"outcome": "replaced", "publication": saved, "warnings": fresh["warnings"]}

    try:
        try:
            rest.fetch_message(channel_id, int(message_id))
        except DiscordFailure as exc:
            if not exc.message_missing:
                raise
            return finish_replacement()
        try:
            result = rest.edit_message(channel_id, int(message_id), fresh["payload"])
        except DiscordFailure as exc:
            if not exc.message_missing:
                raise
            return finish_replacement()
        if not isinstance(result, dict) or str(result.get("id")) != str(message_id):
            raise DiscordFailure("Discord did not confirm the edited message identity.", uncertain=True)
    except DiscordFailure as exc:
        _conditional_transition(guild_id, pid, ("updating",), "attention",
                                expected_message_id=int(message_id), match_message=True,
                                last_error=str(exc))
        current = get_publication(guild_id, pid)
        return {"outcome": "uncertain" if exc.uncertain else "attention",
                "publication": current, "error": str(exc)}
    changed, saved = _conditional_transition(
        guild_id, pid, ("updating",), "published",
        expected_message_id=int(message_id), match_message=True,
        last_error=None, successful=True,
    )
    if not changed:
        return {"outcome": "attention", "publication": saved,
                "error": "Publication state changed while updating; operator review is required."}
    return {"outcome": "updated", "publication": saved, "warnings": fresh["warnings"]}


def authorize_replacement_retry(guild_id, publication_id):
    changed, publication = _conditional_transition(
        guild_id, publication_id, ("replacement_send_uncertain",),
        "replacement_retry_authorized",
        last_error="Operator explicitly authorized a replacement retry.",
    )
    return publication if changed else None


def unpublish(guild_id, publication, *, rest=None):
    pid = int(publication["id"])
    current = get_publication(guild_id, pid)
    if current is None:
        return {"outcome": "attention", "publication": None, "error": "Publication no longer exists."}
    if current.get("status") in {"updating", "unpublishing", "replacement_sending",
                                 "replacement_send_uncertain", "replacement_retry_authorized"}:
        return {"outcome": "replacement_uncertain", "publication": current,
                "error": "Another operation is active or requires explicit recovery."}
    channel_id, message_id = int(current["channel_id"]), current.get("message_id")
    if not message_id:
        return {"outcome": "attention", "publication": current,
                "error": "No confirmed message ID is recorded; association retained for attention."}
    if not claim_unpublish(guild_id, pid, int(message_id)):
        return {"outcome": "attention", "publication": get_publication(guild_id, pid),
                "error": "Publication state changed; reload before Unpublish."}
    rest = rest or DiscordREST()

    def restore_attention(error):
        _conditional_transition(guild_id, pid, ("unpublishing",), "attention",
                                expected_message_id=int(message_id), match_message=True,
                                last_error=error)
        return get_publication(guild_id, pid)

    def remove_claimed():
        return remove_publication(guild_id, pid, expected_statuses=("unpublishing",),
                                  expected_message_id=int(message_id), match_message=True)

    try:
        rest.validate_channel_and_permissions(guild_id, channel_id, "unpublish")
        try:
            rest.fetch_message(channel_id, int(message_id))
        except DiscordFailure as exc:
            if exc.message_missing:
                if remove_claimed():
                    return {"outcome": "removed", "already_missing": True}
                return {"outcome": "attention", "publication": get_publication(guild_id, pid),
                        "error": "Publication state changed before cleanup; association was retained."}
            raise
        try:
            rest.delete_message(channel_id, int(message_id))
        except DiscordFailure as exc:
            if exc.message_missing:
                if remove_claimed():
                    return {"outcome": "removed", "already_missing": True}
                return {"outcome": "attention", "publication": get_publication(guild_id, pid),
                        "error": "Publication state changed before cleanup; association was retained."}
            raise
    except PublicationError as exc:
        saved = restore_attention(str(exc))
        return {"outcome": "attention", "publication": saved, "error": str(exc)}
    except DiscordFailure as exc:
        saved = restore_attention(str(exc))
        return {"outcome": "uncertain" if exc.uncertain else "attention",
                "publication": saved, "error": str(exc)}
    if not remove_claimed():
        return {"outcome": "attention", "publication": get_publication(guild_id, pid),
                "error": "Message was deleted, but Publication state changed; operator review is required."}
    return {"outcome": "removed", "already_missing": False}
