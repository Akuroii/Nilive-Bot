import math
import aiosqlite
from database import DB_PATH

# ══════════════════════════════════════════════════════
# RULE 3 — ANTI-INFLATION MATH
# XP multipliers do NOT stack.
# User has @Booster (1.5x) AND @VIP (1.2x)
# → They get max(1.5, 1.2) = 1.5x ONLY
# NOT 1.5 + 1.2 = 2.7x
# NOT 1.5 × 1.2 = 1.8x
# The highest single multiplier wins.
# Multiplier roles are stored in: leveling_bonus_roles table
# ══════════════════════════════════════════════════════

async def get_xp_multiplier(guild_id: int, member_role_ids: list[int]) -> float:
    """
    Returns the highest XP multiplier for a member.
    Reads from leveling_bonus_roles table.
    Never stacks — always returns max single multiplier.

    Args:
        guild_id: The Discord guild ID
        member_role_ids: List of role IDs the member currently has

    Returns:
        float: The multiplier to apply (1.0 = no bonus)
    """
    if not member_role_ids:
        return 1.0
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT multiplier FROM leveling_bonus_roles
            WHERE guild_id = ?
        """, (guild_id,))
        bonus_rows = await cursor.fetchall()
        cursor2 = await db.execute("""
            SELECT role_id FROM leveling_blacklist_roles
            WHERE guild_id = ?
        """, (guild_id,))
        blacklist_rows = await cursor2.fetchall()

    blacklisted_role_ids = {row[0] for row in blacklist_rows}
    if any(rid in blacklisted_role_ids for rid in member_role_ids):
        return 0.0

    if not bonus_rows:
        return 1.0

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT role_id, multiplier FROM leveling_bonus_roles
            WHERE guild_id = ?
        """, (guild_id,))
        bonus_roles = await cursor.fetchall()

    applicable = [
        multiplier for role_id, multiplier in bonus_roles
        if role_id in member_role_ids
    ]
    if not applicable:
        return 1.0
    return max(applicable)


async def get_leveling_config(guild_id: int) -> dict:
    """
    Returns the leveling config for a guild.
    Falls back to defaults if no config row exists.
    Config is stored in: leveling_config table
    Configurable from: dashboard Systems > Leveling page
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT * FROM leveling_config WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [desc[0] for desc in cursor.description]
            return dict(zip(cols, row))
    return {
        "guild_id":               guild_id,
        "enabled":                1,
        "xp_per_word":            1,
        "xp_min_per_message":     5,
        "xp_max_per_message":     50,
        "xp_cooldown_seconds":    30,
        "voice_xp_enabled":       1,
        "voice_xp_per_minute":    3,
        "voice_require_unmuted":  1,
        "spam_detection_enabled": 1,
        "spam_xp_penalty":        10,
        "spam_threshold":         3,
        "levelup_announce":       1,
        "levelup_channel_id":     None,
        "levelup_message":        None,
        "levelup_embed_data":     None,
        "remove_old_reward_role": 0,
    }


async def calculate_message_xp(
    guild_id: int,
    member_role_ids: list[int],
    word_count: int
) -> int:
    """
    Calculates XP to award for a message.
    Applies anti-inflation multiplier (Rule 3).
    Clamps to min/max from leveling_config.

    Returns 0 if leveling is disabled or member is blacklisted.
    """
    config = await get_leveling_config(guild_id)
    if not config.get("enabled", 1):
        return 0
    multiplier = await get_xp_multiplier(guild_id, member_role_ids)
    if multiplier == 0.0:
        return 0
    base_xp = word_count * config["xp_per_word"]
    base_xp = max(config["xp_min_per_message"],
                  min(config["xp_max_per_message"], base_xp))
    final_xp = int(base_xp * multiplier)
    return final_xp


def calculate_voice_xp(minutes: float, voice_xp_per_minute: int) -> int:
    """
    Calculates XP to award for voice time.
    Called every 60 seconds by the voice XP task.
    Multiplier is NOT applied to voice XP (Blueprint decision).

    Rule 4 guards (alone, deafened, AFK, muted) are checked
    BEFORE calling this function in cogs/leveling.py
    """
    return int(minutes * voice_xp_per_minute)


# ══════════════════════════════════════════════════════
# LEVEL CALCULATION
# Same formula used everywhere — centralized here
# ══════════════════════════════════════════════════════

def xp_for_level(level: int) -> int:
    """Total XP needed to reach a given level from 0."""
    return math.floor(100 * (level ** 1.5))


def calculate_level_from_xp(total_xp: int) -> int:
    """Returns the level for a given total XP amount."""
    level = 0
    while total_xp >= xp_for_level(level + 1):
        total_xp -= xp_for_level(level + 1)
        level += 1
    return level


def xp_progress(total_xp: int) -> tuple[int, int, int]:
    """
    Returns (current_level, xp_into_level, xp_needed_for_next_level).
    Used for rank cards and leaderboard displays.
    """
    level = 0
    remaining = total_xp
    while remaining >= xp_for_level(level + 1):
        remaining -= xp_for_level(level + 1)
        level += 1
    needed = xp_for_level(level + 1)
    return level, remaining, needed


async def check_and_award_level_rewards(bot, member, guild_id: int,
                                         old_level: int, new_level: int):
    """
    Checks leveling_rewards table and assigns/removes roles
    when a member levels up.
    Rule 5 check is done before assigning each role.

    Called from: cogs/leveling.py on_message after XP update
    """
    from utils.permissions import check_bot_role_position

    if new_level <= old_level:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT level, role_id FROM leveling_rewards
            WHERE guild_id = ? AND level <= ?
            ORDER BY level ASC
        """, (guild_id, new_level))
        rewards = await cursor.fetchall()
        config = await get_leveling_config(guild_id)

    guild = member.guild
    for reward_level, role_id in rewards:
        if reward_level > old_level:
            role = guild.get_role(role_id)
            if not role:
                continue
            can_assign, warning = check_bot_role_position(guild, role)
            if not can_assign:
                print(f"[ROLE WARNING] {warning}")
                continue
            if role not in member.roles:
                try:
                    await member.add_roles(role,
                        reason=f"Level {reward_level} reward")
                except Exception as e:
                    print(f"[LEVEL REWARD ERROR] {e}")

    if config.get("remove_old_reward_role"):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT role_id FROM leveling_rewards
                WHERE guild_id = ? AND level < ?
            """, (guild_id, new_level))
            old_rewards = await cursor.fetchall()
        for (role_id,) in old_rewards:
            role = guild.get_role(role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role,
                        reason="Replaced by higher level reward")
                except Exception:
                    pass
