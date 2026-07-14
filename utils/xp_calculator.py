import math
import aiosqlite
from database import DB_PATH

async def get_xp_multiplier(guild_id: int, member_role_ids: list[int]) -> float:
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
    return int(minutes * voice_xp_per_minute)


def xp_for_level(level: int) -> int:
    return math.floor(100 * (level ** 1.5))


def calculate_level_from_xp(total_xp: int) -> int:
    level = 0
    while total_xp >= xp_for_level(level + 1):
        total_xp -= xp_for_level(level + 1)
        level += 1
    return level


def xp_progress(total_xp: int) -> tuple[int, int, int]:
    level = 0
    remaining = total_xp
    while remaining >= xp_for_level(level + 1):
        remaining -= xp_for_level(level + 1)
        level += 1
    needed = xp_for_level(level + 1)
    return level, remaining, needed


async def check_and_award_level_rewards(bot, member, guild_id: int,
                                         old_level: int, new_level: int):
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


# ─── Phase 5 / Leveling expansion — currency level rewards ─────────────
#
# Companion to check_and_award_level_rewards() above (which only ever
# handled role grants). Coin/diamond grants at configured levels live in
# their own table (leveling_currency_rewards) rather than being bolted
# onto leveling_rewards, since a single level can have both a role AND
# a currency reward, and leveling_rewards.role_id is NOT NULL so it
# can't represent a currency-only row.
#
# Routed through utils.economy_safe.safe_credit() — the same atomic,
# ledgered path every other coin/diamond grant in the project uses
# (shop purchases, /give, event rewards). That means every currency
# level-up grant automatically gets a transaction_ledger row with
# source='leveling', with no extra logging code needed here.
#
# Called from utils/reward_engine.py's give_reward() "xp" branch,
# right alongside check_and_award_level_rewards() — same trigger point,
# same guild_id/member already resolved by the caller.
async def check_and_award_level_currency_rewards(bot, member, guild_id: int,
                                                   old_level: int, new_level: int):
    if new_level <= old_level:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT level, currency, amount FROM leveling_currency_rewards
            WHERE guild_id = ? AND level > ? AND level <= ?
            ORDER BY level ASC
        """, (guild_id, old_level, new_level))
        rewards = await cursor.fetchall()

    if not rewards:
        return

    from utils.economy_safe import safe_credit

    for reward_level, currency, amount in rewards:
        if not amount or amount <= 0:
            continue
        currency = currency if currency in ("balance", "diamonds") else "balance"
        try:
            await safe_credit(
                guild_id, member.id, int(amount), currency=currency,
                reason=f"Level {reward_level} reward", source="leveling")
        except Exception as e:
            print(f"[LEVEL CURRENCY REWARD ERROR] level={reward_level} "
                  f"guild={guild_id} user={member.id}: {e}")
