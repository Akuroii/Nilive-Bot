import aiosqlite
from database import DB_PATH

LEVEL_OWNER     = "owner"
LEVEL_ADMIN     = "admin"
LEVEL_MODERATOR = "moderator"

LEVEL_RANK = {
    LEVEL_OWNER:     3,
    LEVEL_ADMIN:     2,
    LEVEL_MODERATOR: 1,
}


async def get_user_permission_level(guild_id: int, user_id: int) -> str | None:
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


def check_hierarchy(actor, target) -> tuple[bool, str]:
    if target.guild.owner_id == actor.id:
        return True, "Actor is guild owner"
    if actor.top_role.position <= target.top_role.position:
        return False, (
            f"Your highest role ({actor.top_role.name}) must be above "
            f"target's highest role ({target.top_role.name})"
        )
    return True, "OK"


async def can_moderate(actor, target, guild_id: int) -> tuple[bool, str]:
    if await is_owner(guild_id, actor.id):
        return True, "Dashboard owner bypass"
    if actor.guild.owner_id == actor.id:
        return True, "Guild owner bypass"
    return check_hierarchy(actor, target)


def check_bot_role_position(guild, role) -> tuple[bool, str]:
    bot_member = guild.me
    if bot_member is None:
        return False, "⚠️ Could not find Nero in this server"
    if role.position >= bot_member.top_role.position:
        return False, (
            f"⚠️ Nero's role must be above @{role.name} to assign it. "
            f"Go to Server Settings > Roles and drag Nero above @{role.name}."
        )
    return True, "OK"


PAGE_PERMISSIONS = {
    # General
    "overview":           LEVEL_MODERATOR,
    "members_view":       LEVEL_MODERATOR,
    "members_edit":       LEVEL_ADMIN,
    "members_delete":     LEVEL_OWNER,
    "audit_log":          LEVEL_ADMIN,

    # Manage
    "moderation_view":    LEVEL_MODERATOR,
    "moderation_action":  LEVEL_MODERATOR,
    "moderation_edit":    LEVEL_ADMIN,
    "moderation_delete":  LEVEL_OWNER,
    "tickets":            LEVEL_MODERATOR,
    "embedbuilder":       LEVEL_ADMIN,
    "reactionroles":      LEVEL_ADMIN,
    "triggers":           LEVEL_ADMIN,
    "customcommands":     LEVEL_ADMIN,

    # Systems
    "mvp":                LEVEL_ADMIN,
    "leveling":           LEVEL_ADMIN,
    "economy":            LEVEL_ADMIN,
    "shop":               LEVEL_ADMIN,
    "events":             LEVEL_ADMIN,
    "leaderboards":       LEVEL_MODERATOR,

    # Config
    "general_settings":   LEVEL_OWNER,
    "welcome":            LEVEL_ADMIN,
    "boost":              LEVEL_ADMIN,
    "announcements":      LEVEL_ADMIN,
    "commands":           LEVEL_OWNER,
    "dashboard_access":   LEVEL_OWNER,
}


def get_required_level(page: str) -> str:
    return PAGE_PERMISSIONS.get(page, LEVEL_OWNER)


def user_can_access_page(user_level: str, page: str) -> bool:
    required = get_required_level(page)
    return LEVEL_RANK.get(user_level, 0) >= LEVEL_RANK.get(required, 3)
