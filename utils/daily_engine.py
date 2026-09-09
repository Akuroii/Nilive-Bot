import aiosqlite
from datetime import datetime, timezone, timedelta
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# DAILY / STREAK ENGINE
#
# Replaces the old in-memory `_daily_cooldowns` dict in cogs/economy.py
# (module-level, wiped on every restart, no streak concept at all) with
# persisted, per-(guild_id, user_id) state in the daily_claims table.
#
# Day boundary is UTC calendar day, not a rolling 24h window:
#   - last_claim_date == today            -> already claimed, rejected
#   - last_claim_date == yesterday        -> streak continues (+1)
#   - anything older, or no prior claim   -> streak resets to 1
#
# The streak COUNT is never capped — it keeps incrementing past 7
# indefinitely. Only the derived BONUS caps at the day-7 value
# (get_streak_bonus). This is a deliberate split: streak_count is a
# simple fact ("how many days in a row"), the bonus is a policy
# decision layered on top of it, and the two must not be conflated by
# artificially capping the stored count.
#
# claim_daily_streak() is the single place that checks-and-writes this
# state, inside one BEGIN IMMEDIATE transaction — matching the same
# pattern already used by utils/equip_engine.equip_role() and
# utils/prestige.apply_prestige_purchase()/purchase_prestige() in this
# project. Two concurrent calls (double-click, or /daily racing a
# future /wallet "Claim Daily" button) can't both read "not claimed
# yet": BEGIN IMMEDIATE acquires the write lock on the first
# statement, so the second call blocks until the first transaction
# commits, then sees the already-updated row and is correctly
# rejected — even if the first call hasn't gotten around to crediting
# the reward yet (crediting happens after this function returns, as a
# separate step, same division as the existing shop purchase flow).
# ═══════════════════════════════════════════════════════════════════════

STREAK_BONUS_CAP_DAYS = 7

# Coins added per streak day, capped at STREAK_BONUS_CAP_DAYS. Same
# configurability level as the existing daily_min/daily_max settings
# (a bot_settings key, no dedicated dashboard field yet) — not a new
# or reduced level of admin control relative to what already exists.
DEFAULT_STREAK_BONUS_PER_DAY = 20


class DailyAlreadyClaimed(Exception):
    """Raised when the member has already claimed for the current UTC day."""
    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = max(0, int(seconds_remaining))
        super().__init__("Already claimed for the current UTC day")


def _seconds_until_next_utc_midnight(now: datetime) -> int:
    today = now.date()
    next_midnight = datetime(
        today.year, today.month, today.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    return int((next_midnight - now).total_seconds())


async def claim_daily_streak(guild_id: int, user_id: int) -> dict:
    """
    Atomically checks and updates daily-claim/streak state.
    Returns {"streak": new_streak_count} on success.
    Raises DailyAlreadyClaimed(seconds_remaining) if already claimed
    for the current UTC calendar day. Never leaves a partial write —
    every rejection path rolls back before raising.
    """
    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    yesterday_str = (now.date() - timedelta(days=1)).isoformat()

    new_streak = None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT last_claim_date, streak_count FROM daily_claims
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            row = await cursor.fetchone()

            if row and row[0] == today_str:
                await db.execute("ROLLBACK")
                raise DailyAlreadyClaimed(_seconds_until_next_utc_midnight(now))

            if row and row[0] == yesterday_str:
                new_streak = int(row[1]) + 1
            else:
                # No prior row (first-ever claim) or the gap is more
                # than one day (missed a full UTC day) -> restart at 1.
                new_streak = 1

            await db.execute("""
                INSERT INTO daily_claims
                    (guild_id, user_id, last_claim_date, streak_count, last_claimed_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    last_claim_date = excluded.last_claim_date,
                    streak_count    = excluded.streak_count,
                    last_claimed_at = CURRENT_TIMESTAMP
            """, (guild_id, user_id, today_str, new_streak))
            await db.commit()
        except DailyAlreadyClaimed:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    return {"streak": new_streak}


async def get_streak_bonus(guild_id: int, streak_count: int) -> int:
    """
    min(streak_count, 7) * the configured per-day bonus. streak_count
    itself must NOT be capped before calling this — only the bonus
    derived from it is capped, per the locked design.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = 'daily_streak_bonus_per_day'")
        row = await cursor.fetchone()
    per_day = int(row[0]) if row and row[0] is not None else DEFAULT_STREAK_BONUS_PER_DAY
    capped_days = min(streak_count, STREAK_BONUS_CAP_DAYS)
    return capped_days * per_day
