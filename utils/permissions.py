import aiosqlite
from database import DB_PATH

# ══════════════════════════════════════════════════════
# PERMISSION LEVELS
# These map to the dashboard_users.permission_level column
# Owner ID is NOT hardcoded — it is read from dashboard_users
# where permission_level = 'owner'
# ══════════════════════════════════════════════════════

LEVEL_OWNER     = "owner"
LEVEL_ADMIN     = "admin"
LEVEL_MODERATOR = "moderator"

LEVEL_RANK = {
    LEVEL_OWNER:     3,
    LEVEL_ADMIN:     2,
    LEVEL_MODERATOR: 1,
}


async def get_user_permission_level(guild_id: int, user_id: int) -> str | None:
    """
    Returns the permission level of a user for a guild.
    Returns None if the user has no entry in dashboard_users.
    Source table: dashboard_users (guild_id, user_id, permission_level)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT permission_level FROM dashboard_users
            WHERE guild_id = ? AND user_id = ? AND enabled = 1
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else None


async def is_owner(guild_id: int, user_id: int) -> bool:
    level = await get_user_permission_level(guild_id, user_id)
    return level == LEVEL_OWNER


async def is_admin_or_above(guild_id: int, user_id: int) -> bool:
    level = await get_user_permission_level(guild_id, user_id)
    return LEVEL_RANK.get(level, 0) >= LEVEL_RANK[LEVEL_ADMIN]


async def is_moderator_or_above(guild_id: int, user_id: int) -> bool:
    level = await get_user_permission_level(guild_id, user_id)
    return LEVEL_RANK.get(level, 0) >= LEVEL_RANK[LEVEL_MODERATOR]


# ══════════════════════════════════════════════════════
# RULE 2 — HIERARCHY SHIELD
# Used by bot commands AND dashboard actions
# ══════════════════════════════════════════════════════

def check_hierarchy(actor, target) -> tuple[bool, str]:
    """
    Checks if actor can perform actions on target based on role hierarchy.
    actor and target are discord.Member objects.
    Returns (allowed: bool, reason: str)

    Owner check: if actor has permission_level='owner' in dashboard_users,
    this check is bypassed — see can_moderate() below.
    """
    if target.guild.owner_id == actor.id:
        return True, "Actor is guild owner"
    if actor.top_role.position <= target.top_role.position:
        return False, (
            f"Your highest role ({actor.top_role.name}) must be above "
            f"target's highest role ({target.top_role.name})"
        )
    return True, "OK"


async def can_moderate(actor, target, guild_id: int) -> tuple[bool, str]:
    """
    Full moderation check combining:
    1. Dashboard owner bypass (reads dashboard_users table)
    2. Role hierarchy shield

    Usage in cogs:
        allowed, reason = await can_moderate(interaction.user, member, guild_id)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
    """
    # Owner bypass — reads from dashboard_users where permission_level='owner'
    # This is configured on the Dashboard Access page, not hardcoded
    db_owner = await is_owner(guild_id, actor.id)
    if db_owner:
        return True, "Dashboard owner bypass"

    # Discord guild owner always bypasses
    if actor.guild.owner_id == actor.id:
        return True, "Guild owner bypass"

    return check_hierarchy(actor, target)


# ══════════════════════════════════════════════════════
# RULE 5 — BOT-ROLE WARNING
# Call before any role assignment in cogs
# ══════════════════════════════════════════════════════

def check_bot_role_position(guild, role) -> tuple[bool, str]:
    """
    Checks if the bot's role is high enough to assign the target role.
    Returns (can_assign: bool, warning_message: str)

    If can_assign is False:
    - Log the warning (don't crash)
    - Show on dashboard: "⚠️ Nero's role must be above @RoleName"
    - The warning_message is ready to display directly

    Usage in cogs:
        can_assign, warning = check_bot_role_position(guild, role)
        if not can_assign:
            print(warning)  # log it
            return  # don't crash
    """
    bot_member = guild.me
    if bot_member is None:
        return False, "⚠️ Could not find Nero in this server"
    if role.position >= bot_member.top_role.position:
        return False, (
            f"⚠️ Nero's role must be above @{role.name} to assign it. "
            f"Go to Server Settings > Roles and drag Nero above @{role.name}."
        )
    return True, "OK"


# ══════════════════════════════════════════════════════
# PAGE-LEVEL ACCESS CONTROL
# Used by dashboard/permissions.py
# Maps page names to minimum required permission level
# ══════════════════════════════════════════════════════

PAGE_PERMISSIONS = {
    # General
    "overview":        LEVEL_MODERATOR,
    "members_view":    LEVEL_MODERATOR,
    "members_edit":    LEVEL_ADMIN,
    "members_delete":  LEVEL_OWNER,
    "audit_log":       LEVEL_ADMIN,

    # Manage
    "moderation_view": LEVEL_MODERATOR,
    "moderation_edit": LEVEL_ADMIN,
    "moderation_delete": LEVEL_OWNER,
    "tickets":         LEVEL_MODERATOR,
    "embedbuilder":    LEVEL_ADMIN,
    "reactionroles":   LEVEL_ADMIN,
    "triggers":        LEVEL_ADMIN,
    "customcommands":  LEVEL_ADMIN,

    # Systems
    "mvp":             LEVEL_ADMIN,
    "leveling":        LEVEL_ADMIN,
    "economy":         LEVEL_ADMIN,
    "shop":            LEVEL_ADMIN,
    "events":          LEVEL_ADMIN,
    "leaderboards":    LEVEL_MODERATOR,

    # Config
    "general_settings": LEVEL_OWNER,
    "welcome":          LEVEL_ADMIN,
    "boost":            LEVEL_ADMIN,
    "announcements":    LEVEL_ADMIN,
    "commands":         LEVEL_OWNER,
    "dashboard_access": LEVEL_OWNER,
}


def get_required_level(page: str) -> str:
    """
    Returns the minimum permission level string required for a page.
    Defaults to LEVEL_OWNER if page not found (safe default).
    """
    return PAGE_PERMISSIONS.get(page, LEVEL_OWNER)


def user_can_access_page(user_level: str, page: str) -> bool:
    """
    Returns True if the user's level meets the page requirement.
    """
    required = get_required_level(page)
    return LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK.get(required, 3)
