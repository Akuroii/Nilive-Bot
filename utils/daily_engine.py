import random
import aiosqlite
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.timezone import get_cairo_daily_key, seconds_until_cairo_midnight, CAIRO_TZ

# ═══════════════════════════════════════════════════════════════════════
# DAILY / STREAK ENGINE
#
# Replaces the old in-memory `_daily_cooldowns` dict in cogs/economy.py
# (module-level, wiped on every restart, no streak concept at all) with
# persisted, per-(guild_id, user_id) state in the daily_claims table.
#
# Day boundary is Cairo calendar day (Africa/Cairo), not a rolling 24h window:
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
    """Raised when the member has already claimed for the current Cairo day."""
    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = max(0, int(seconds_remaining))
        super().__init__("Already claimed for the current Cairo day")


def _seconds_until_next_utc_midnight(now: datetime) -> int:
    # Kept for backwards-compat alias; new code uses Cairo midnight
    return seconds_until_cairo_midnight(now)


async def claim_daily_streak(guild_id: int, user_id: int) -> dict:
    """
    Atomically checks and updates daily-claim/streak state.
    Returns {"streak": new_streak_count} on success.
    Raises DailyAlreadyClaimed(seconds_remaining) if already claimed
    for the current Cairo calendar day. Never leaves a partial write —
    every rejection path rolls back before raising.
    """
    now = datetime.now(timezone.utc)
    today_str = get_cairo_daily_key(now)
    # Cairo yesterday = today -1 day in Cairo calendar
    cairo_today = now.astimezone(CAIRO_TZ).date()
    yesterday_str = (cairo_today - timedelta(days=1)).isoformat()

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
                # than one day (missed a full Cairo day) -> restart at 1.
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


# ═══════════════════════════════════════════════════════════════════════
# SHARED CLAIM ENGINE — Wallet pass
#
# Three entry points now exist for the same action: /streak (canonical),
# /daily (thin compatibility alias) and the Wallet's [🔥 Streak] button.
# All three call perform_streak_claim() below — there is exactly ONE
# implementation of the reward math and crediting, so the three can
# never drift apart, and the atomic claim guard in claim_daily_streak()
# above means they can't be raced against each other either (claiming
# via /daily then immediately via the Wallet button correctly rejects
# the second attempt: it's the same daily_claims row).
#
# ECONOMY NOTE (flagged for Akuroi, deliberately NOT changed here):
# the reward formula below is byte-for-byte the one that already lived
# inline in cogs/economy.py's /daily — random base in [daily_min,
# daily_max], plus the day-capped streak bonus, with the Prestige earn
# multiplier applied to the COMBINED total. It was MOVED, not
# redesigned, because the prompt says the economic model is still under
# review. get_streak_preview() reads the same settings without
# consuming a claim, so the Wallet can show "what you'd get" honestly
# while the model is still being decided.
# ═══════════════════════════════════════════════════════════════════════


async def get_daily_range(guild_id: int) -> tuple[int, int]:
    """
    (daily_min, daily_max) from bot_settings, same keys and same
    100/300 defaults the old inline /daily used. Guarded so a
    mis-typed dashboard value can't make random.randint() raise.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = 'daily_min'")
        row = await cursor.fetchone()
        try:
            daily_min = int(row[0]) if row and row[0] is not None else 100
        except (TypeError, ValueError):
            daily_min = 100
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = 'daily_max'")
        row = await cursor.fetchone()
        try:
            daily_max = int(row[0]) if row and row[0] is not None else 300
        except (TypeError, ValueError):
            daily_max = 300

    if daily_max < daily_min:
        daily_min, daily_max = daily_max, daily_min
    return daily_min, daily_max


async def get_streak_state(guild_id: int, user_id: int) -> dict:
    """
    Read-only view of the member's streak, for the Wallet hub and for
    the cooldown message — never mutates anything.

    `streak` is the CURRENT effective streak: the stored count if the
    last claim was today or yesterday (Cairo calendar), otherwise 0.
    """
    now = datetime.now(timezone.utc)
    today_str = get_cairo_daily_key(now)
    cairo_today = now.astimezone(CAIRO_TZ).date()
    yesterday_str = (cairo_today - timedelta(days=1)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT last_claim_date, streak_count, last_claimed_at
            FROM daily_claims WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))
        row = await cursor.fetchone()

    if not row:
        return {"streak": 0, "stored_streak": 0, "claimed_today": False,
                "seconds_remaining": 0, "last_claim_date": None}

    last_date, stored, _last_at = row[0], int(row[1] or 0), row[2]
    claimed_today = (last_date == today_str)
    chain_alive = last_date in (today_str, yesterday_str)

    return {
        "streak": stored if chain_alive else 0,
        "stored_streak": stored,
        "claimed_today": claimed_today,
        "seconds_remaining": (
            _seconds_until_next_utc_midnight(now) if claimed_today else 0),
        "last_claim_date": last_date,
    }


async def get_streak_preview(guild_id: int, user_id: int) -> dict:
    """
    What the NEXT claim would look like, without consuming it. Used by
    the Wallet's Streak panel so the member sees the reward range and
    the bonus they're about to earn before deciding to claim.

    The bonus is computed for the streak the next claim would produce
    (current + 1, or 1 if the chain is broken) — not the current one —
    because that's the number that will actually be paid.
    """
    state = await get_streak_state(guild_id, user_id)
    next_streak = (state["streak"] + 1) if not state["claimed_today"] \
        else state["streak"]
    daily_min, daily_max = await get_daily_range(guild_id)
    bonus = await get_streak_bonus(guild_id, next_streak)
    return {
        **state,
        "next_streak": next_streak,
        "daily_min": daily_min,
        "daily_max": daily_max,
        "next_bonus": bonus,
        "bonus_capped": next_streak > STREAK_BONUS_CAP_DAYS,
    }


async def perform_streak_claim(bot, guild_id: int, user_id: int,
                               member=None) -> dict:
    """
    THE single claim implementation shared by /streak, /daily and the
    Wallet button.

    Raises DailyAlreadyClaimed (with seconds_remaining) when the member
    has already claimed for the current Cairo day — callers turn that
    into their own cooldown message. On success returns everything a
    caller needs to render a result, so no caller has to re-query.

    The atomic claim-check-and-write happens FIRST, before any reward
    math or crediting, exactly as the previous inline /daily did: the
    loser of a race is rejected before either call has touched a
    balance.
    """
    result = await claim_daily_streak(guild_id, user_id)
    streak = result["streak"]

    daily_min, daily_max = await get_daily_range(guild_id)
    base = random.randint(daily_min, daily_max)
    streak_bonus = await get_streak_bonus(guild_id, streak)

    # Prestige earn multiplier, applied to the COMBINED (base + bonus)
    # total — unchanged from the previous inline implementation.
    # Defensive: any lookup failure grants the raw amount rather than
    # breaking the claim, and the member has already consumed their
    # claim for the day at this point, so failing closed is not an
    # option.
    try:
        from utils.prestige import get_prestige_earn_multiplier, is_booster
        booster = is_booster(member) if member is not None else None
        mult = await get_prestige_earn_multiplier(
            guild_id, user_id, "balance", is_booster=booster, bot=bot)
    except Exception as e:
        print(f"[PRESTIGE] streak multiplier lookup failed; granting raw "
              f"(guild={guild_id} user={user_id}): {e}")
        mult = 1.0

    amount = int(round((base + streak_bonus) * mult))
    if amount <= 0:
        amount = 1

    # safe_credit ledgers the credit itself (source='daily'), which is
    # what makes streak rewards show up in Wallet → Receipts for free.
    # The source string stays 'daily' rather than becoming 'streak' so
    # existing ledger rows and any dashboard filtering keep matching.
    from utils.economy_safe import safe_credit, get_balance
    await safe_credit(
        guild_id, user_id, amount,
        reason=f"Daily reward (streak day {streak})", source="daily")
    new_balance = await get_balance(guild_id, user_id)

    return {
        "streak": streak,
        "base": base,
        "streak_bonus": streak_bonus,
        "multiplier": mult,
        "amount": amount,
        "new_balance": new_balance,
        "bonus_capped": streak > STREAK_BONUS_CAP_DAYS,
        "next_reset_seconds": _seconds_until_next_utc_midnight(
            datetime.now(timezone.utc)),
    }


def format_remaining(seconds: int) -> str:
    """'18h 42m' / '42m' / '30s' — precise without being noisy."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"
