import math
import aiosqlite
from datetime import datetime, timezone, timedelta
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


# BUGFIX (dark-fixes pass #7): XP blacklist roles not applying to
# voice XP. cogs/leveling.py's on_activity_voice_tick() grants voice
# XP directly (calculate_voice_xp + give_reward) and never once
# consulted leveling_blacklist_roles — only calculate_message_xp()
# (via get_xp_multiplier above) ever checked it. A member given a
# blacklist role to explicitly opt them out of the leveling system
# (e.g. a "no XP" role for staff/bots-adjacent accounts) still
# silently earned XP for every voice tick.
#
# This is a standalone, cheap query rather than reusing
# get_xp_multiplier() wholesale — get_xp_multiplier also folds in
# leveling_bonus_roles (multiplier > 1x), and voice XP has never
# applied bonus-role multipliers. Fixing the blacklist gap shouldn't
# silently change voice XP's payout math for bonus-role holders too;
# that's a separate design decision, out of scope for this fix.
async def is_role_blacklisted(guild_id: int, member_role_ids: list[int]) -> bool:
    if not member_role_ids:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT role_id FROM leveling_blacklist_roles
            WHERE guild_id = ?
        """, (guild_id,))
        blacklist_rows = await cursor.fetchall()
    blacklisted_role_ids = {row[0] for row in blacklist_rows}
    return any(rid in blacklisted_role_ids for rid in member_role_ids)


# The effective Leveling config: every setting the engine reads, at the value
# the engine uses when the guild has no row at all. Values mirror the schema
# defaults in database.py's leveling_config table (and the .get(...) fallbacks
# sprinkled through cogs/leveling.py), so this dict is the single definition of
# "unconfigured". The dashboard's GET /api/leveling/config returns the same
# merged view, which is why a fresh guild shows the values the bot is actually
# enforcing instead of OFF/blank.
LEVELING_CONFIG_DEFAULTS = {
    "enabled":                1,
    "xp_per_word":            1,
    "xp_min_per_message":     5,
    "xp_max_per_message":     50,
    "xp_cooldown_seconds":    20,
    "voice_xp_enabled":       1,
    "voice_xp_per_minute":    3,
    "voice_require_unmuted":  1,
    "spam_detection_enabled": 1,
    "spam_threshold":         3,
    "spam_xp_penalty":        10,
    # Anti-spam window (cogs/leveling.py reads it as spam_window_seconds).
    # The column was added by migration with DEFAULT 10 and never had a UI;
    # it is admin-editable now, and this is the value until it is set.
    "spam_window_seconds":    10,
    "levelup_announce":       1,
    "levelup_channel_id":     None,
    "levelup_message":        None,
    "remove_old_reward_role": 0,
}


async def get_leveling_config(guild_id: int) -> dict:
    """The effective Leveling config: the stored row laid over the defaults.

    A guild that never opened/saved the Leveling page has no row and runs on
    LEVELING_CONFIG_DEFAULTS unchanged. A row written by an older schema keeps
    the documented default for any column it lacks or holds as NULL, instead of
    handing callers None (which read as "off" and made the dashboard and the
    runtime disagree about the same guild)."""
    config = dict(LEVELING_CONFIG_DEFAULTS)
    config["guild_id"] = guild_id
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT * FROM leveling_config WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
        if row:
            for key, value in zip([desc[0] for desc in cursor.description], row):
                if value is not None:
                    config[key] = value
    return config


async def calculate_message_xp(
    guild_id: int,
    member_role_ids: list[int],
    word_count: int,
    user_id: int = None,
) -> int:
    config = await get_leveling_config(guild_id)
    if not config.get("enabled", 1):
        return 0
    role_multiplier = await get_xp_multiplier(guild_id, member_role_ids)
    if role_multiplier == 0.0:
        return 0
    # Phase 5 / Leveling expansion: XP boost items. Stacks
    # multiplicatively on top of the role multiplier above (a boost
    # item is a separate, purchasable effect from role-based bonuses,
    # not an alternative to them). Only applied when a user_id is
    # given — voice XP intentionally does not call this with a
    # user_id, keeping the existing voice XP economy untouched.
    boost_multiplier = 1.0
    if user_id is not None:
        boost_multiplier = await get_active_boost_multiplier(guild_id, user_id)
    base_xp = word_count * config["xp_per_word"]
    base_xp = max(config["xp_min_per_message"],
                  min(config["xp_max_per_message"], base_xp))
    final_xp = int(base_xp * role_multiplier * boost_multiplier)
    return final_xp


# ─── Phase 5 / Leveling expansion — XP boost items ──────────────────────
#
# Purchasable, temporary XP multipliers granted via cogs/shop.py's
# process_purchase() for shop items with type='xp_boost'. A user can
# hold more than one active boost at once (e.g. two stacked
# purchases) — get_active_boost_multiplier() takes the MAX across all
# non-expired rows for that guild+user rather than stacking them
# additively, the same "highest wins" rule leveling_bonus_roles
# already uses for role multipliers.
async def get_active_boost_multiplier(guild_id: int, user_id: int) -> float:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT MAX(multiplier) FROM leveling_active_boosts
            WHERE guild_id = ? AND user_id = ? AND expires_at > ?
        """, (guild_id, user_id, now))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else 1.0


async def grant_xp_boost(guild_id: int, user_id: int, multiplier: float,
                          duration_hours: int, source: str = "shop",
                          db: aiosqlite.Connection | None = None) -> str:
    """Insert one active boost. Shop and potion callers keep the old path.

    A passed connection is not committed. A level_claim source is
    idempotent: an existing row is returned unchanged, including its expiry.
    """
    if duration_hours <= 0:
        raise ValueError("duration_hours must be positive")
    claim_source = str(source).startswith("level_claim:")

    async def apply(connection) -> str:
        if claim_source:
            cursor = await connection.execute(
                "SELECT expires_at FROM leveling_active_boosts WHERE source=?",
                (source,))
            existing = await cursor.fetchone()
            if existing and existing[0]:
                return existing[0]
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        ).isoformat()
        try:
            await connection.execute("""
                INSERT INTO leveling_active_boosts
                    (guild_id, user_id, multiplier, expires_at, source)
                VALUES (?, ?, ?, ?, ?)
            """, (guild_id, user_id, multiplier, expires_at, source))
        except Exception as exc:
            if not claim_source or "UNIQUE" not in str(exc).upper():
                raise
            cursor = await connection.execute(
                "SELECT expires_at FROM leveling_active_boosts WHERE source=?",
                (source,))
            existing = await cursor.fetchone()
            if existing and existing[0]:
                return existing[0]
            raise
        return expires_at

    if db is not None:
        return await apply(db)
    if not claim_source:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        ).isoformat()
        async with aiosqlite.connect(DB_PATH) as connection:
            await connection.execute("""
                INSERT INTO leveling_active_boosts
                    (guild_id, user_id, multiplier, expires_at, source)
                VALUES (?, ?, ?, ?, ?)
            """, (guild_id, user_id, multiplier, expires_at, source))
            await connection.commit()
        return expires_at

    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            result = await apply(connection)
            await connection.commit()
            return result
        except Exception:
            await connection.execute("ROLLBACK")
            raise


def calculate_voice_xp(minutes: float, voice_xp_per_minute: int) -> int:
    return int(minutes * voice_xp_per_minute)


def xp_for_level(level: int) -> int:
    return math.floor(100 * (level ** 1.5))


# NOTE: the old XP/level-gated "Prestige reset" mechanic (and its
# cumulative-XP helper total_xp_for_level) has been retired in favour of
# the finalized Shop-purchased Prestige system in utils/prestige.py. The
# helpers below (calculate_level_from_xp / xp_progress) remain because
# they are still used by the rank card, setxp and the reward engine.

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


# ─── Anti-spam penalty ───────────────────────────────────────────────────
# The spam penalty is a REAL XP deduction: it subtracts from the member's
# current XP, floors the result at zero (XP is never allowed to go negative,
# and a penalty never creates a debt the member has to work off), and
# recomputes `level` from the resulting XP in the same transaction so
# levels.xp and levels.level can never disagree — the split between the two
# used to be able to leave a member with a stale level, which the next
# legitimate XP grant would then silently correct by demoting them.
#
# A penalty can therefore legitimately demote a member (see the caller in
# cogs/leveling.py). That is not a level *crossing*: crossings are increases,
# and `record_crossing()` only ever inserts entitlement rows for increases, so
# nothing here touches the claim ledger — no row is created, fulfilled claims
# are not revoked, and the Level reward roles a member already earned stay on
# them (with `remove_old_reward_role` ON, nothing is removed on the way down
# either: enforcement only runs after a delivery). Re-earning the level later
# is an ordinary crossing, and the claim ledger's
# UNIQUE(guild_id, user_id, reward_level, track, reward_ref) +
# INSERT OR IGNORE is what stops that from paying a reward twice.
#
# Deliberately NOT logged to the XP transaction ledger: the penalty has never
# written a ledger row, and adding one would be a separate, visible change to
# the dashboard's ledger page. (The ledger does support it —
# `utils/ledger.py` records a negative amount as type="deduct".)
async def apply_spam_penalty(guild_id: int, user_id: int, penalty: int) -> dict:
    """Deduct `penalty` XP (floored at zero) and keep `level` consistent.

    Returns {"old_xp", "new_xp", "old_level", "new_level", "deducted",
    "applied"}; `deducted` is what the member actually lost (the XP floor can
    make it smaller than `penalty`) and `applied` is False when there was no XP
    to lose at all (a no-op, never a debt).
    """
    penalty = max(0, int(penalty))
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            old_xp = int(row[0]) if row else 0
            old_level = int(row[1]) if row else 0
            new_xp = max(0, old_xp - penalty)
            new_level = xp_progress(new_xp)[0]
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = ?, level = ?
            """, (guild_id, user_id, new_xp, new_level, new_xp, new_level))
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    deducted = old_xp - new_xp
    return {"old_xp": old_xp, "new_xp": new_xp, "old_level": old_level,
            "new_level": new_level, "deducted": deducted,
            "applied": deducted > 0}


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
        # Finalized Prestige: apply the earn-time multiplier based on the
        # member's effective Prestige tier. member is already resolved so
        # we pass is_booster directly (avoids a re-lookup). Level-up
        # currency rewards are a genuine earn path, so they are scaled;
        # XP/level/other state are not. Defensive: default to 1.0 on any
        # lookup failure so a level-up reward never crashes.
        try:
            from utils.prestige import get_prestige_earn_multiplier, is_booster
            mult = await get_prestige_earn_multiplier(
                guild_id, member.id, currency, member=member)
        except Exception as e:
            print(f"[PRESTIGE] level reward multiplier lookup failed; "
                  f"granting raw (guild={guild_id} user={member.id}): {e}")
            mult = 1.0
        final_amount = int(round(int(amount) * mult))
        if final_amount == 0 and amount > 0:
            final_amount = int(amount)
        try:
            await safe_credit(
                guild_id, member.id, final_amount, currency=currency,
                reason=f"Level {reward_level} reward", source="leveling")
        except Exception as e:
            print(f"[LEVEL CURRENCY REWARD ERROR] level={reward_level} "
                  f"guild={guild_id} user={member.id}: {e}")
