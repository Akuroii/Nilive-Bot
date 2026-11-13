import hashlib
import os
import time
import requests as _req

MANAGE_ROLES_PERM = 0x10000000  # (1 << 28)
ADMINISTRATOR_PERM = 0x8        # (1 << 3)

DISCORD_API = "https://discord.com/api/v10"
_BOT_IDENTITY_TTL_SECONDS = 900.0
_BOT_IDENTITY_CACHE: dict[str, tuple[str, float]] = {}


class BotMemberStatus:
    PRESENT = "present"  # Confirmed guild member (HTTP 200 valid member payload)
    ABSENT = "absent"    # Explicitly confirmed absent (404 Discord code 10007 Unknown Member)
    UNKNOWN = "unknown"  # Indeterminate (10004 Unknown Guild, missing/invalid token, 401/403/429/5xx/network/malformed)


def _token_cache_key(bot_token: str) -> str:
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]


def clear_bot_identity_cache(bot_token: str | None = None) -> None:
    if bot_token is None:
        _BOT_IDENTITY_CACHE.clear()
    else:
        _BOT_IDENTITY_CACHE.pop(_token_cache_key(bot_token), None)


def resolve_bot_user_id(bot_token: str | None = None) -> tuple[str | None, str | None]:
    """
    Resolve the authenticated bot's Discord user snowflake via GET /users/@me.
    Never logs or includes the raw bot token in error strings.
    """
    token = (bot_token if bot_token is not None else os.getenv("DISCORD_TOKEN", "")).strip()
    if not token:
        return None, "BOT_TOKEN not configured"

    key = _token_cache_key(token)
    now = time.monotonic()
    cached = _BOT_IDENTITY_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0], None

    try:
        resp = _req.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=8,
        )
    except Exception as exc:
        return None, f"Network error resolving bot identity ({type(exc).__name__})"

    if resp.status_code == 401:
        clear_bot_identity_cache(token)
        return None, "Discord error 401 resolving bot identity"
    if resp.status_code != 200:
        return None, f"Discord error {resp.status_code} resolving bot identity"

    try:
        data = resp.json()
    except Exception:
        return None, "Malformed JSON resolving bot identity"

    if not isinstance(data, dict):
        return None, "Malformed bot identity payload"
    raw_id = str(data.get("id", "")).strip()
    if not raw_id.isascii() or not raw_id.isdigit():
        return None, "Discord bot identity response missing valid id"

    _BOT_IDENTITY_CACHE[key] = (raw_id, now + _BOT_IDENTITY_TTL_SECONDS)
    return raw_id, None


def fetch_bot_guild_member(
    guild_id: int, bot_token: str | None = None
) -> tuple[dict | None, str, str | None]:
    """
    Fetch the bot's guild member object via GET /guilds/{guild_id}/members/{bot_user_id}.
    Returns (member_dict_or_none, BotMemberStatus, safe_error_or_none).
    Note: GET /guilds/{guild_id}/members/@me is an OAuth2 Bearer endpoint and is NOT
    valid for Bot-token authentication.
    """
    token = (bot_token if bot_token is not None else os.getenv("DISCORD_TOKEN", "")).strip()
    if not token:
        return None, BotMemberStatus.UNKNOWN, "BOT_TOKEN not configured"

    bot_user_id, id_err = resolve_bot_user_id(token)
    if id_err or not bot_user_id:
        return None, BotMemberStatus.UNKNOWN, id_err or "Unable to resolve bot user ID"

    try:
        member_resp = _req.get(
            f"{DISCORD_API}/guilds/{guild_id}/members/{bot_user_id}",
            headers={"Authorization": f"Bot {token}"},
            timeout=8,
        )
    except Exception as exc:
        return None, BotMemberStatus.UNKNOWN, f"Network error fetching bot member ({type(exc).__name__})"

    if member_resp.status_code == 200:
        try:
            member = member_resp.json()
        except Exception:
            return None, BotMemberStatus.UNKNOWN, "Malformed JSON fetching bot member"
        if not isinstance(member, dict):
            return None, BotMemberStatus.UNKNOWN, "Malformed bot member object"
        roles_field = member.get("roles", [])
        if not isinstance(roles_field, list):
            return None, BotMemberStatus.UNKNOWN, "Malformed bot member roles list"
        user_obj = member.get("user")
        if not isinstance(user_obj, dict) or not user_obj.get("id"):
            member = dict(member)
            member["user"] = {"id": bot_user_id}
        return member, BotMemberStatus.PRESENT, None

    if member_resp.status_code == 404:
        try:
            err_body = member_resp.json()
            code = err_body.get("code") if isinstance(err_body, dict) else None
        except Exception:
            code = None
        if code == 10007:
            return None, BotMemberStatus.ABSENT, "Nero is not a member of this server"
        if code == 10004:
            return None, BotMemberStatus.UNKNOWN, "Discord 404 Unknown Guild (10004) fetching bot member"
        return None, BotMemberStatus.UNKNOWN, "Ambiguous 404 fetching bot member"

    if member_resp.status_code == 401:
        clear_bot_identity_cache(token)
        return None, BotMemberStatus.UNKNOWN, "Discord error 401 fetching bot member"

    return None, BotMemberStatus.UNKNOWN, f"Discord error {member_resp.status_code} fetching bot member"


def fetch_guild_roles_and_bot_member(guild_id: int) -> tuple[list[dict] | None, dict | None, str | None]:
    """
    Fetch raw Discord roles list and bot member state for `guild_id`.
    Returns (roles_list, bot_member_dict_or_sentinel, error_message).
    """
    bot_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not bot_token:
        if os.getenv("DASHBOARD_DEMO_LOGIN") == "1" and str(guild_id) == os.getenv("DASHBOARD_DEMO_GUILD_ID", "900000000000000001"):
            demo_roles = [
                {"id": "900000000000000001", "name": "@everyone", "position": 0, "color": 0, "managed": False, "permissions": "0"},
                {"id": "970000000000000001", "name": "Member", "position": 1, "color": 0x10b981, "managed": False, "permissions": "0"},
                {"id": "970000000000000002", "name": "VIP", "position": 2, "color": 0xf59e0b, "managed": False, "permissions": "0"},
                {"id": "970000000000000003", "name": "Nero", "position": 3, "color": 0x8b5cf6, "managed": True, "permissions": str(MANAGE_ROLES_PERM)},
                {"id": "970000000000000004", "name": "Admin", "position": 4, "color": 0xef4444, "managed": False, "permissions": str(ADMINISTRATOR_PERM)},
            ]
            demo_bot_member = {
                "user": {"id": "970000000000000099"},
                "roles": ["970000000000000003"],
                "permissions": str(MANAGE_ROLES_PERM | ADMINISTRATOR_PERM),
            }
            return demo_roles, demo_bot_member, None
        return None, None, "BOT_TOKEN not configured"

    headers = {"Authorization": f"Bot {bot_token}"}

    # 1. Fetch roles
    try:
        roles_resp = _req.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            headers=headers,
            timeout=8,
        )
        if roles_resp.status_code == 401:
            clear_bot_identity_cache(bot_token)
        if roles_resp.status_code != 200:
            return None, None, f"Discord error {roles_resp.status_code} fetching roles"
        roles = roles_resp.json()
        if not isinstance(roles, list) or any(not isinstance(r, dict) or "id" not in r for r in roles):
            return None, None, "Malformed guild roles payload from Discord"
    except Exception as exc:
        return None, None, f"Network error fetching guild roles: {type(exc).__name__}"

    # 2. Fetch bot guild member via /users/@me -> /guilds/{guild_id}/members/{bot_user_id}
    bot_member, bot_status, member_err = fetch_bot_guild_member(guild_id, bot_token)
    if bot_status == BotMemberStatus.PRESENT:
        return roles, bot_member, None
    if bot_status == BotMemberStatus.ABSENT:
        return roles, None, None
    return roles, {"_bot_status": BotMemberStatus.UNKNOWN, "_error": member_err}, None


def _format_role_ref(role_obj: dict | None, guild_id: int | str) -> str:
    if not role_obj:
        return "@Nero's role"
    if str(role_obj.get("id")) == str(guild_id):
        return "@everyone"
    text = str(role_obj.get("name") or "Nero's role")
    return text if text.startswith("@") else f"@{text}"


def compute_role_assignability(
    guild_id: int,
    roles: list[dict],
    bot_member: dict | None,
    guild_owner_id: int | None = None,
    bot_status: str | None = None,
) -> dict:
    """
    Compute assignability metadata for all roles in a guild.
    Preserves tri-state bot_status ("present" | "absent" | "unknown") so transient
    Discord/API failures never become false `bot_missing` ("Nero not found in server").
    """
    role_map = {str(r["id"]): r for r in roles}

    status_error = None
    if bot_status is not None:
        effective_status = bot_status
    elif isinstance(bot_member, dict) and bot_member.get("_bot_status") == BotMemberStatus.UNKNOWN:
        effective_status = BotMemberStatus.UNKNOWN
        status_error = bot_member.get("_error")
    elif isinstance(bot_member, dict) and bot_member:
        effective_status = BotMemberStatus.PRESENT
    else:
        effective_status = BotMemberStatus.ABSENT

    active_member = bot_member if effective_status == BotMemberStatus.PRESENT and isinstance(bot_member, dict) else None
    bot_role_ids = active_member.get("roles", []) if active_member else []
    bot_user_id = None
    if active_member and isinstance(active_member.get("user"), dict) and active_member["user"].get("id"):
        try:
            bot_user_id = int(active_member["user"]["id"])
        except (ValueError, TypeError):
            bot_user_id = None
    is_owner = (guild_owner_id is not None and bot_user_id is not None and guild_owner_id == bot_user_id)

    # Calculate bot permissions (explicit permissions field if provided, else @everyone | member roles)
    has_manage_roles = False
    if active_member and active_member.get("permissions") is not None:
        try:
            perms = int(active_member["permissions"])
            has_manage_roles = bool(perms & ADMINISTRATOR_PERM or perms & MANAGE_ROLES_PERM)
        except (ValueError, TypeError):
            has_manage_roles = False
    elif active_member is not None:
        total_perms = 0
        everyone_role = role_map.get(str(guild_id))
        if everyone_role and everyone_role.get("permissions") is not None:
            try:
                total_perms |= int(everyone_role["permissions"])
            except (ValueError, TypeError):
                pass
        for rid in bot_role_ids:
            r_obj = role_map.get(str(rid))
            if r_obj and r_obj.get("permissions") is not None:
                try:
                    total_perms |= int(r_obj["permissions"])
                except (ValueError, TypeError):
                    pass
        has_manage_roles = bool(total_perms & ADMINISTRATOR_PERM or total_perms & MANAGE_ROLES_PERM)

    # Determine bot's highest role: higher position wins; on ties, smaller (older) role ID wins;
    # @everyone (role.id == guild.id) is always the lowest role in Discord's hierarchy.
    guild_id_str = str(guild_id)
    bot_roles_objs = [role_map[str(rid)] for rid in bot_role_ids if str(rid) in role_map]

    def role_hierarchy_key(r):
        is_default = str(r.get("id")) == guild_id_str
        pos = int(r.get("position", 0))
        try:
            rid_int = int(r["id"])
        except (ValueError, TypeError):
            rid_int = 0
        return (0 if is_default else 1, pos, -rid_int)

    bot_top_role = (
        (max(bot_roles_objs, key=role_hierarchy_key) if bot_roles_objs else role_map.get(guild_id_str))
        if active_member is not None
        else None
    )
    bot_top_key = role_hierarchy_key(bot_top_role) if bot_top_role else (0, 0, 0)
    bot_top_ref = _format_role_ref(bot_top_role, guild_id_str)

    enriched_roles = []
    for r in roles:
        rid_str = str(r["id"])
        is_everyone = rid_str == guild_id_str
        is_managed = bool(r.get("managed", False))
        r_key = role_hierarchy_key(r)

        assignable = True
        reason = None
        label = None

        if effective_status == BotMemberStatus.UNKNOWN:
            assignable = False
            reason = "bot_status_unknown"
            label = "Unable to verify Nero's server permissions right now"
        elif effective_status == BotMemberStatus.ABSENT or active_member is None:
            assignable = False
            reason = "bot_missing"
            label = "Nero not found in server"
        elif not has_manage_roles and not is_owner:
            assignable = False
            reason = "no_manage_roles_perm"
            label = "Missing Manage Roles permission"
        elif is_everyone:
            assignable = False
            reason = "everyone"
            label = "@everyone cannot be assigned"
        elif is_managed:
            assignable = False
            reason = "managed"
            label = "Managed by an integration"
        elif not is_owner and r_key >= bot_top_key:
            assignable = False
            if r_key > bot_top_key:
                reason = "above_bot"
                label = f"Above {bot_top_ref}"
            else:
                reason = "equal_to_bot"
                label = f"Equal to {bot_top_ref}"

        enriched_role = dict(r)
        enriched_role["assignable"] = assignable
        enriched_role["unassignable_reason"] = reason
        enriched_role["unassignable_label"] = label
        enriched_roles.append(enriched_role)

    return {
        "roles": enriched_roles,
        "bot_top_role": bot_top_role,
        "has_manage_roles": has_manage_roles,
        "bot_status": effective_status,
        "bot_status_error": status_error,
        "role_map": {str(r["id"]): r for r in enriched_roles},
    }


def validate_assignable_role(
    guild_id: int,
    role_id: str | int | None,
    existing_role_id: str | int | None = None
) -> tuple[bool, str | None]:
    """
    Validate whether `role_id` can be newly assigned by Nero in `guild_id`.

    Delta Rule:
    - If `role_id` is empty / None: valid (assignment cleared).
    - If `str(role_id) == str(existing_role_id)`: valid (existing assignment preserved).
    - If `role_id` is a new/modified assignment: must satisfy Discord assignability.
    """
    if not role_id:
        return True, None

    str_role = str(role_id).strip()
    if not str_role or str_role == "0":
        return True, None

    if existing_role_id and str(existing_role_id).strip() == str_role:
        return True, None

    roles, bot_member, err = fetch_guild_roles_and_bot_member(guild_id)
    if err:
        return False, f"Unable to verify role with Discord: {err}"

    if not roles:
        return False, "No roles found for this server."

    info = compute_role_assignability(guild_id, roles, bot_member)
    role_obj = info["role_map"].get(str_role)

    if not role_obj:
        return False, f"Selected role (ID: {str_role}) was not found in this server."

    if not role_obj.get("assignable", False):
        reason = role_obj.get("unassignable_reason")
        label = role_obj.get("unassignable_label", "Role is unassignable")
        role_name = role_obj.get("name", str_role)
        top_ref = _format_role_ref(info.get("bot_top_role"), guild_id)

        if reason == "bot_status_unknown":
            detail = info.get("bot_status_error") or label
            return False, f"Unable to verify Nero's permissions in Discord right now ({detail}). Please try again shortly."
        elif reason == "no_manage_roles_perm":
            return False, "Nero lacks the 'Manage Roles' permission in Discord. Grant Nero Manage Roles or Administrator."
        elif reason == "everyone":
            return False, "The @everyone role cannot be used as a role reward."
        elif reason == "managed":
            return False, f"Role @{role_name} is managed by an external integration (bot/booster) and cannot be assigned."
        elif reason in ("above_bot", "equal_to_bot"):
            return False, f"Nero's highest role ({top_ref}) must be above @{role_name} in Server Settings > Roles."
        else:
            return False, f"Role @{role_name} cannot be assigned by Nero ({label})."

    return True, None
