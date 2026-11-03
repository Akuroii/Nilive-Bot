import os
import requests as _req

MANAGE_ROLES_PERM = 0x10000000  # (1 << 28)
ADMINISTRATOR_PERM = 0x8        # (1 << 3)


def fetch_guild_roles_and_bot_member(guild_id: int) -> tuple[list[dict] | None, dict | None, str | None]:
    """
    Fetch raw Discord roles list and bot member object for `guild_id`.
    Returns (roles_list, bot_member_dict, error_message).
    """
    bot_token = os.getenv("DISCORD_TOKEN", "")
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
            f"https://discord.com/api/v10/guilds/{guild_id}/roles",
            headers=headers,
            timeout=8,
        )
        if roles_resp.status_code != 200:
            return None, None, f"Discord error {roles_resp.status_code} fetching roles"
        roles = roles_resp.json()
    except Exception as e:
        return None, None, f"Network error fetching guild roles: {e}"

    # 2. Fetch bot member (@me in guild)
    try:
        member_resp = _req.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/members/@me",
            headers=headers,
            timeout=8,
        )
        if member_resp.status_code == 200:
            bot_member = member_resp.json()
        else:
            bot_member = None
    except Exception:
        bot_member = None

    return roles, bot_member, None


def compute_role_assignability(
    guild_id: int,
    roles: list[dict],
    bot_member: dict | None,
    guild_owner_id: int | None = None
) -> dict:
    """
    Compute assignability metadata for all roles in a guild.
    Returns:
    {
        "roles": [enriched_role_dict, ...],
        "bot_top_role": dict | None,
        "has_manage_roles": bool,
        "role_map": dict[str, dict]
    }
    """
    role_map = {str(r["id"]): r for r in roles}

    # Default values if bot member is missing
    bot_role_ids = bot_member.get("roles", []) if bot_member else []
    bot_user_id = int(bot_member["user"]["id"]) if (bot_member and "user" in bot_member) else None
    is_owner = (guild_owner_id is not None and bot_user_id is not None and guild_owner_id == bot_user_id)

    # Calculate bot permissions
    has_manage_roles = False
    if bot_member and "permissions" in bot_member:
        try:
            perms = int(bot_member["permissions"])
            has_manage_roles = bool(perms & ADMINISTRATOR_PERM or perms & MANAGE_ROLES_PERM)
        except (ValueError, TypeError):
            has_manage_roles = False
    elif bot_member is not None:
        # Fallback if member object has no permissions field (calculate from roles)
        total_perms = 0
        everyone_role = role_map.get(str(guild_id))
        if everyone_role and "permissions" in everyone_role:
            try: total_perms |= int(everyone_role["permissions"])
            except Exception: pass
        for rid in bot_role_ids:
            r_obj = role_map.get(str(rid))
            if r_obj and "permissions" in r_obj:
                try: total_perms |= int(r_obj["permissions"])
                except Exception: pass
        has_manage_roles = bool(total_perms & ADMINISTRATOR_PERM or total_perms & MANAGE_ROLES_PERM)

    # Determine bot's highest role
    # In Discord, hierarchy ordering between two roles is:
    # 1. Higher position wins (higher position > lower position).
    # 2. On equal positions, smaller role ID wins (older role > newer role).
    bot_roles_objs = [role_map[str(rid)] for rid in bot_role_ids if str(rid) in role_map]
    
    def role_hierarchy_key(r):
        pos = int(r.get("position", 0))
        try:
            rid_int = int(r["id"])
        except (ValueError, TypeError):
            rid_int = 0
        return (pos, -rid_int)

    bot_top_role = max(bot_roles_objs, key=role_hierarchy_key) if bot_roles_objs else role_map.get(str(guild_id))
    bot_top_key = role_hierarchy_key(bot_top_role) if bot_top_role else (0, 0)
    bot_top_name = bot_top_role.get("name", "Nero's role") if bot_top_role else "Nero's role"

    enriched_roles = []
    for r in roles:
        rid_str = str(r["id"])
        is_everyone = (rid_str == str(guild_id) or r.get("position", 0) == 0)
        is_managed = bool(r.get("managed", False))
        r_key = role_hierarchy_key(r)

        assignable = True
        reason = None
        label = None

        if not bot_member:
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
            if r.get("position", 0) > (bot_top_role.get("position", 0) if bot_top_role else 0):
                reason = "above_bot"
                label = f"Above @{bot_top_name}"
            else:
                reason = "equal_to_bot"
                label = f"Equal to @{bot_top_name}"

        enriched_role = dict(r)
        enriched_role["assignable"] = assignable
        enriched_role["unassignable_reason"] = reason
        enriched_role["unassignable_label"] = label
        enriched_roles.append(enriched_role)

    return {
        "roles": enriched_roles,
        "bot_top_role": bot_top_role,
        "has_manage_roles": has_manage_roles,
        "role_map": {str(r["id"]): r for r in enriched_roles}
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

    Returns:
    (True, None) if valid.
    (False, actionable_error_message) if invalid.
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
        top_name = info["bot_top_role"].get("name", "Nero's role") if info.get("bot_top_role") else "Nero's role"

        if reason == "no_manage_roles_perm":
            return False, "Nero lacks the 'Manage Roles' permission in Discord. Grant Nero Manage Roles or Administrator."
        elif reason == "everyone":
            return False, "The @everyone role cannot be used as a role reward."
        elif reason == "managed":
            return False, f"Role @{role_name} is managed by an external integration (bot/booster) and cannot be assigned."
        elif reason in ("above_bot", "equal_to_bot"):
            return False, f"Nero's highest role (@{top_name}) must be above @{role_name} in Server Settings > Roles."
        else:
            return False, f"Role @{role_name} cannot be assigned by Nero ({label})."

    return True, None
