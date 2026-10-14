import math
import time
import uuid
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
# voice XP. cogs/leveling.py's on_activity_voice_xp_tick() grants voice
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
    "message_xp_enabled":      1,
    "xp_per_word":             1,
    "xp_min_per_message":      5,
    "xp_max_per_message":      50,
    "xp_cooldown_seconds":     10,
    "voice_xp_enabled":        1,
    "voice_xp_per_minute":     3,
    "voice_require_unmuted":   1,
    # ON = no Voice XP while alone, deafened or in the AFK channel.
    "voice_farming_guard":     1,
    "spam_detection_enabled":  1,
    "spam_threshold":          10,
    "spam_window_seconds":     20,
    # A detected incident deducts at most 1/divisor of current XP, capped at
    # 1% of the balance per incident. 1000 is the conservative baseline.
    "spam_xp_penalty_divisor": 1000,
    "levelup_announce":        1,
    "levelup_channel_id":      None,
    "levelup_message":         None,
    "remove_old_reward_role":  0,
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
                # Historical fixed penalty is intentionally retained in the
                # schema for data preservation only; it is not exposed to the
                # API/runtime after the incident-based policy replaced it.
                if key == "spam_xp_penalty":
                    continue
                if value is not None:
                    config[key] = value
    return config


async def calculate_message_xp(
    guild_id: int,
    member_role_ids: list[int],
    word_count: int,
    user_id: int = None,
    *,
    ignore_message_toggle: bool = False,
) -> int:
    config = await get_leveling_config(guild_id)
    if (not ignore_message_toggle
            and not config.get("message_xp_enabled", 1)):
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


async def calculate_max_message_xp(guild_id: int,
                                   member_role_ids: list[int],
                                   user_id: int) -> int:
    """Return this member's largest ordinary message-XP award.

    The spam rolling budget is defined as one production-calculated message
    reward, not an arbitrary percentage of a Level bar. It uses the configured
    per-message cap plus the same role and active-boost multipliers as a real
    message. Spam protection can still run while Message XP is OFF, so this
    potential-reward calculation deliberately bypasses only that one enable
    switch; blacklist roles and all XP math remain effective.
    """
    config = await get_leveling_config(guild_id)
    xp_per_word = int(config.get("xp_per_word", 1) or 0)
    max_per_message = int(config.get("xp_max_per_message", 50) or 0)
    word_count = (math.ceil(max_per_message / xp_per_word)
                  if xp_per_word > 0 else 0)
    return await calculate_message_xp(
        guild_id, member_role_ids, word_count, user_id=user_id,
        ignore_message_toggle=True)


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


# ─── Incident-based anti-spam penalty ─────────────────────────────────────
# The penalty budget, current incident identity, quiet deadline, and warning
# claim all live in SQLite. BEGIN IMMEDIATE serializes the incident claim with
# its XP deduction across independent bot processes, not just Cog instances.
SPAM_PENALTY_ROLLING_WINDOW_SECONDS = 60 * 60
SPAM_PENALTY_EVENTS_TABLE = "leveling_spam_penalty_events"
SPAM_INCIDENTS_TABLE = "leveling_spam_incidents"


async def get_spam_incident_state(guild_id: int, user_id: int, *,
                                  now: float | None = None) -> dict:
    """Read the persisted quiet-window state for detector-history reset.

    ``active_until`` is the explicit quiet-window expiry. Every message received
    while the incident is active refreshes it; detector samples are used only
    before an incident opens. A row may remain after expiry so its last-warning
    timestamp can rate-limit warnings for the same member across later incidents.
    """
    timestamp = float(time.time() if now is None else now)
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            f"SELECT incident_id, active_until FROM {SPAM_INCIDENTS_TABLE} "
            "WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cursor.fetchone()
    if row is None:
        return {"exists": False, "active": False, "expired": False,
                "incident_id": None, "active_until": None}
    active_until = float(row[1])
    active = active_until > timestamp
    return {"exists": True, "active": active, "expired": not active,
            "incident_id": row[0], "active_until": active_until}


async def _apply_spam_penalty_in_transaction(db, guild_id: int, user_id: int,
                                             divisor: int, penalty_cap: int,
                                             window_seconds: int, timestamp: float,
                                             incident_id: str | None = None) -> dict:
    """Apply the established deduction/budget math on the caller's txn."""
    cutoff = timestamp - window_seconds
    # Keep this audit/budget table bounded to each member's active rolling
    # window; expired deductions can no longer affect policy.
    await db.execute(
        f"DELETE FROM {SPAM_PENALTY_EVENTS_TABLE} "
        "WHERE guild_id=? AND user_id=? AND created_at<=?",
        (guild_id, user_id, cutoff))
    cursor = await db.execute(
        f"SELECT COALESCE(SUM(deducted), 0) "
        f"FROM {SPAM_PENALTY_EVENTS_TABLE} "
        "WHERE guild_id=? AND user_id=? AND created_at>?",
        (guild_id, user_id, cutoff))
    budget_used = int((await cursor.fetchone())[0] or 0)
    budget_remaining = max(0, penalty_cap - budget_used)

    cursor = await db.execute(
        "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
        (guild_id, user_id))
    row = await cursor.fetchone()
    old_xp = max(0, int(row[0])) if row else 0
    old_level = int(row[1]) if row else 0
    calculated = old_xp // divisor
    incident_cap = old_xp // 100
    requested = min(calculated, incident_cap)
    deducted = min(requested, budget_remaining)
    new_xp = max(0, old_xp - deducted)
    new_level = xp_progress(new_xp)[0]
    await db.execute("""
        INSERT INTO levels (guild_id, user_id, xp, level)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET xp = ?, level = ?
    """, (guild_id, user_id, new_xp, new_level, new_xp, new_level))
    if deducted:
        await db.execute(
            f"INSERT INTO {SPAM_PENALTY_EVENTS_TABLE} "
            "(guild_id, user_id, created_at, deducted, rolling_cap, incident_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, timestamp, deducted, penalty_cap, incident_id))

    return {
        "old_xp": old_xp, "new_xp": new_xp,
        "old_level": old_level, "new_level": new_level,
        "deducted": deducted, "requested": requested,
        "divisor": divisor, "applied": deducted > 0,
        "rolling_cap": penalty_cap,
        "rolling_window_seconds": window_seconds,
        "budget_used_before": budget_used,
        "budget_remaining_before": budget_remaining,
        "budget_remaining_after": max(0, budget_remaining - deducted),
        "budget_limited": deducted < requested,
    }


async def apply_spam_penalty(guild_id: int, user_id: int,
                             divisor: int = 1000, *,
                             penalty_cap: int | None = 50,
                             window_seconds: int = SPAM_PENALTY_ROLLING_WINDOW_SECONDS,
                             now: float | None = None,
                             incident_detected: bool | None = None,
                             incident_window_seconds: int | None = None,
                             warning_window_seconds: int | None = None,
                             detection_enabled: bool = True) -> dict:
    """Atomically claim a spam incident and, if new, apply its XP penalty.

    Callers that provide ``incident_detected`` use the durable incident path:
    the current identity, quiet-window expiry, warning claim, and penalty are
    read/written under one ``BEGIN IMMEDIATE`` transaction. Concurrent
    processes therefore observe one incident and at most one deduction. A
    detected message opens an incident through ``incident_window_seconds``;
    each subsequent message while that incident is active refreshes the quiet
    deadline without reapplying the penalty. The warning is claimable once per
    incident, and its member-wide cooldown is persisted separately in the row.

    Calls that omit ``incident_detected`` retain the direct penalty helper API
    used by existing maintenance/safety checks; they still share the persistent
    rolling-hour budget and transactional zero floor, but do not claim an
    incident. Production calculates ``penalty_cap`` from one maximum Message XP
    award before opening an incident.
    """
    divisor = max(100, int(divisor or 1000))
    cap = (None if penalty_cap is None else max(0, int(penalty_cap)))
    budget_window = max(1, int(window_seconds))
    timestamp = float(time.time() if now is None else now)

    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if incident_detected is None:
                if cap is None:
                    raise ValueError("penalty_cap is required for direct penalties")
                outcome = await _apply_spam_penalty_in_transaction(
                    db, guild_id, user_id, divisor, cap, budget_window,
                    timestamp)
                await db.commit()
                return outcome

            incident_window = max(1, int(incident_window_seconds or 20))
            warning_window = max(
                1, int(warning_window_seconds or incident_window))
            cursor = await db.execute(
                f"SELECT incident_id, active_until, penalty_attempted, "
                f"warning_attempted, last_warning_at "
                f"FROM {SPAM_INCIDENTS_TABLE} "
                "WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            prior = await cursor.fetchone()
            was_expired = bool(prior and float(prior[1]) <= timestamp)

            if not detection_enabled:
                if prior and float(prior[1]) > timestamp:
                    # Turning detection off ends suppression immediately; it
                    # does not delete the warning history or touch XP.
                    await db.execute(
                        f"UPDATE {SPAM_INCIDENTS_TABLE} "
                        "SET active_until=? WHERE guild_id=? AND user_id=?",
                        (timestamp, guild_id, user_id))
                await db.commit()
                return {
                    "in_incident": False, "incident_started": False,
                    "incident_id": prior[0] if prior else None,
                    "incident_expired": bool(prior), "warning_due": False,
                    "applied": False, "deducted": 0,
                }

            if prior and float(prior[1]) > timestamp:
                # The caller invokes this active-incident path for every member
                # message, without consulting process-local detector history.
                active_until = max(float(prior[1]),
                                   timestamp + incident_window)
                await db.execute(
                    f"UPDATE {SPAM_INCIDENTS_TABLE} SET active_until=? "
                    "WHERE guild_id=? AND user_id=?",
                    (active_until, guild_id, user_id))
                await db.commit()
                return {
                    "in_incident": True, "incident_started": False,
                    "incident_id": prior[0], "active_until": active_until,
                    "incident_expired": False, "warning_due": False,
                    "applied": False, "deducted": 0,
                }

            if not incident_detected:
                await db.commit()
                return {
                    "in_incident": False, "incident_started": False,
                    "incident_id": prior[0] if prior else None,
                    "active_until": float(prior[1]) if prior else None,
                    "incident_expired": was_expired, "warning_due": False,
                    "applied": False, "deducted": 0,
                }

            if cap is None:
                # The caller may have observed an active incident just before
                # another process ended it (for example, detection was turned
                # off). Let it calculate the production cap, then retry the
                # atomic claim instead of failing the message callback.
                await db.commit()
                return {
                    "in_incident": False, "incident_started": False,
                    "incident_id": prior[0] if prior else None,
                    "incident_expired": was_expired,
                    "needs_penalty_cap": True, "warning_due": False,
                    "applied": False, "deducted": 0,
                }

            incident_id = uuid.uuid4().hex
            active_until = timestamp + incident_window
            previous_warning = (
                float(prior[4]) if prior and prior[4] is not None else None)
            if prior:
                await db.execute(
                    f"UPDATE {SPAM_INCIDENTS_TABLE} SET incident_id=?, "
                    "active_until=?, penalty_attempted=1, "
                    "warning_attempted=0 WHERE guild_id=? AND user_id=?",
                    (incident_id, active_until, guild_id, user_id))
            else:
                await db.execute(
                    f"INSERT INTO {SPAM_INCIDENTS_TABLE} "
                    "(guild_id, user_id, incident_id, active_until, "
                    "penalty_attempted, warning_attempted, last_warning_at) "
                    "VALUES (?, ?, ?, ?, 1, 0, NULL)",
                    (guild_id, user_id, incident_id, active_until))

            outcome = await _apply_spam_penalty_in_transaction(
                db, guild_id, user_id, divisor, cap, budget_window,
                timestamp, incident_id)
            warning_due = bool(outcome["applied"] and (
                previous_warning is None
                or timestamp - previous_warning >= warning_window))
            last_warning_at = timestamp if warning_due else previous_warning
            await db.execute(
                f"UPDATE {SPAM_INCIDENTS_TABLE} SET last_warning_at=?, "
                "warning_attempted=? WHERE guild_id=? AND user_id=?",
                (last_warning_at, int(warning_due), guild_id, user_id))
            await db.commit()
            return {
                **outcome,
                "in_incident": True, "incident_started": True,
                "incident_id": incident_id, "active_until": active_until,
                "incident_expired": was_expired,
                "warning_due": warning_due,
            }
        except Exception:
            await db.rollback()
            raise


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
