"""Cairo-calendar daily claim and shared streak reward engine."""
from __future__ import annotations

import random
import aiosqlite
from datetime import datetime, timezone, timedelta, date
from database import DB_PATH
from utils.timezone import seconds_until_cairo_midnight, CAIRO_TZ
from utils.streak_settings import (
    get_streak_settings, receipt_destinations, stage_reward_range,
)
from utils.streak_notifications import record_break_event

LEGACY_STREAK_BONUS_CAP_DAYS = 7
# Backward-compatible alias for existing wallet tests/helpers.
STREAK_BONUS_CAP_DAYS = LEGACY_STREAK_BONUS_CAP_DAYS
STREAK_STAGE_CAP = 6
DEFAULT_STREAK_BONUS_PER_DAY = 20


class DailyAlreadyClaimed(Exception):
    """Raised when a member has already claimed during the current Cairo day."""
    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = max(0, int(seconds_remaining))
        super().__init__("Already claimed for the current Cairo day")


def _seconds_until_next_cairo_midnight(now: datetime) -> int:
    return seconds_until_cairo_midnight(now)


def _claim_dates(now: datetime) -> tuple[str, str]:
    cairo_today = now.astimezone(CAIRO_TZ).date()
    return cairo_today.isoformat(), (cairo_today - timedelta(days=1)).isoformat()


def _days_since(last_claim_date: str | None, today: date) -> int | None:
    if not last_claim_date:
        return None
    try:
        return (today - date.fromisoformat(str(last_claim_date))).days
    except (TypeError, ValueError):
        return None


def _effective_streak(last_claim_date: str | None, stored_count: int,
                      today_str: str, yesterday_str: str) -> int:
    if last_claim_date == today_str or last_claim_date == yesterday_str:
        return max(0, int(stored_count or 0))
    return 0


async def get_daily_range(guild_id: int) -> tuple[int, int]:
    """Overall reward range: the V2 table's span, else the legacy global range."""
    config = await get_streak_settings(guild_id)
    table = config["reward_table"]
    if table:
        amounts = table["stages"] + [table["stage6_min"], table["stage6_max"]]
        return min(amounts), max(amounts)
    return int(config["daily_min"]), int(config["daily_max"])


async def get_streak_bonus(guild_id: int, streak_count: int,
                           base_amount: int | None = None) -> int:
    """Legacy flat additive bonus. V2 guilds have no separate bonus (0):
    their stage reward is the whole pre-Prestige amount."""
    config = await get_streak_settings(guild_id)
    if config["v2_enabled"]:
        return 0
    return min(max(0, int(streak_count)), LEGACY_STREAK_BONUS_CAP_DAYS) * int(
        config["legacy_bonus_per_day"])


async def get_streak_state(guild_id: int, user_id: int,
                           now: datetime | None = None) -> dict:
    """Read-only streak state including a derived persistent broken marker."""
    now = now or datetime.now(timezone.utc)
    today_str, yesterday_str = _claim_dates(now)
    cairo_today = now.astimezone(CAIRO_TZ).date()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("""
            SELECT last_claim_date, streak_count, last_claimed_at
            FROM daily_claims WHERE guild_id = ? AND user_id = ?
        """, (guild_id, user_id))).fetchone()

    if not row:
        return {
            "streak": 0, "stored_streak": 0, "claimed_today": False,
            "seconds_remaining": 0, "last_claim_date": None,
            "days_since_last_claim": None, "broken": False,
        }

    last_date, stored, _last_at = row[0], int(row[1] or 0), row[2]
    elapsed = _days_since(last_date, cairo_today)
    claimed_today = last_date == today_str
    current = _effective_streak(last_date, stored, today_str, yesterday_str)
    broken = elapsed is not None and elapsed >= 2 and stored > 0
    return {
        "streak": current,
        "stored_streak": stored,
        "claimed_today": claimed_today,
        "seconds_remaining": (
            _seconds_until_next_cairo_midnight(now) if claimed_today else 0),
        "last_claim_date": last_date,
        "days_since_last_claim": elapsed,
        "broken": broken,
    }


async def get_streak_preview(guild_id: int, user_id: int,
                             now: datetime | None = None) -> dict:
    """Return the next stage and its pre-Prestige payout range without mutation."""
    state = await get_streak_state(guild_id, user_id, now=now)
    next_streak = (state["streak"] if state["claimed_today"]
                   else state["streak"] + 1)
    stage = min(STREAK_STAGE_CAP, max(1, next_streak))
    config = await get_streak_settings(guild_id)
    table = config["reward_table"]
    if table:
        low, high = stage_reward_range(next_streak, table)
        bonus_low = bonus_high = 0
    else:
        low, high = int(config["daily_min"]), int(config["daily_max"])
        bonus_low = bonus_high = min(next_streak, LEGACY_STREAK_BONUS_CAP_DAYS) * int(
            config["legacy_bonus_per_day"])
    return {
        **state,
        "next_streak": next_streak,
        "stage": stage,
        "daily_min": low,
        "daily_max": high,
        "next_bonus": bonus_high,
        "next_bonus_min": bonus_low,
        "next_bonus_max": bonus_high,
        "next_total_min": low + bonus_low,
        "next_total_max": high + bonus_high,
        "bonus_capped": (next_streak >= STREAK_STAGE_CAP
                          if table
                          else next_streak >= LEGACY_STREAK_BONUS_CAP_DAYS),
        "v2_enabled": bool(config["v2_enabled"]),
    }


async def _claim_daily_streak_in_transaction(db: aiosqlite.Connection,
                                             guild_id: int, user_id: int,
                                             now: datetime,
                                             config: dict) -> dict:
    """Claim-row transition. Caller owns BEGIN IMMEDIATE and final commit."""
    today_str, yesterday_str = _claim_dates(now)
    row = await (await db.execute("""
        SELECT last_claim_date, streak_count FROM daily_claims
        WHERE guild_id = ? AND user_id = ?
    """, (guild_id, user_id))).fetchone()
    if row and row[0] == today_str:
        raise DailyAlreadyClaimed(_seconds_until_next_cairo_midnight(now))

    last_date = row[0] if row else None
    previous = int(row[1] or 0) if row else 0
    if last_date == yesterday_str:
        new_streak = previous + 1
    else:
        new_streak = 1

    elapsed = _days_since(last_date, now.astimezone(CAIRO_TZ).date())
    if last_date and previous > 0 and elapsed is not None and elapsed >= 2:
        # Unique episode key prevents the scheduled sweep and a returning
        # claim from creating two notices for the same missed streak.
        await record_break_event(db, guild_id, user_id, last_date, previous)

    await db.execute("""
        INSERT INTO daily_claims
            (guild_id, user_id, last_claim_date, streak_count, last_claimed_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            last_claim_date = excluded.last_claim_date,
            streak_count = excluded.streak_count,
            last_claimed_at = CURRENT_TIMESTAMP
    """, (guild_id, user_id, today_str, new_streak))
    return {"streak": new_streak, "previous_streak": previous,
            "previous_last_claim_date": last_date, "days_since_previous": elapsed}


async def claim_daily_streak(guild_id: int, user_id: int,
                             bot=None, member=None,
                             now: datetime | None = None) -> dict:
    """Compatibility wrapper; all claim calls now use the atomic reward path."""
    return await perform_streak_claim(
        bot, guild_id, user_id, member=member, now=now)


async def perform_streak_claim(bot, guild_id: int, user_id: int,
                               member=None, *, now: datetime | None = None,
                               base_amount: int | None = None) -> dict:
    """Single transactional claim/reward path used by the Streak panel.

    Claim transition, primary-currency credit, and its ledger attempt occur
    under one SQLite transaction. A credit exception rolls back the claim so
    a retry can succeed; a committed retry is rejected by the same daily row.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    injected_base = None if base_amount is None else int(base_amount)

    # Keep the existing Prestige behavior and lookup fallback. Resolve before
    # taking the SQLite write lock; no Discord/network work runs in transaction.
    try:
        from utils.prestige import get_prestige_earn_multiplier, is_booster
        booster = is_booster(member) if member is not None else None
        mult = await get_prestige_earn_multiplier(
            guild_id, user_id, "balance", is_booster=booster,
            bot=bot, member=member)
    except Exception as exc:
        print(f"[PRESTIGE] streak multiplier lookup failed; granting raw "
              f"(guild={guild_id} user={user_id}): {exc}")
        mult = 1.0

    from utils.economy_safe import safe_credit
    config = None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Read configuration after acquiring the writer lock. A save
                # committed before this transaction applies to this claim;
                # a concurrent save after it applies to the next claim.
                config = await get_streak_settings(guild_id, db=db)
                transition = await _claim_daily_streak_in_transaction(
                    db, guild_id, user_id, now, config)
                streak = transition["streak"]
                stage = min(STREAK_STAGE_CAP, streak)
                table = config["reward_table"]
                if table:
                    # Days 1-5 fixed; day 6 onward random in the stage-6
                    # range every day (no cycle). One draw per successful
                    # claim, only after the duplicate-claim guard passed.
                    low, high = stage_reward_range(streak, table)
                else:
                    low, high = int(config["daily_min"]), int(config["daily_max"])
                if injected_base is not None and not low <= injected_base <= high:
                    raise ValueError("Injected reward is outside this stage's configured range")
                base = (random.randint(low, high)
                        if injected_base is None else injected_base)
                if table:
                    streak_bonus = 0
                    bonus_capped = streak >= STREAK_STAGE_CAP
                else:
                    # Compatibility for guilds not yet explicitly opted into
                    # V2: preserve the old global additive rule and cap.
                    streak_bonus = min(streak, LEGACY_STREAK_BONUS_CAP_DAYS) * int(
                        config["legacy_bonus_per_day"])
                    bonus_capped = streak >= LEGACY_STREAK_BONUS_CAP_DAYS
                pre_prestige_total = base + streak_bonus
                amount = int(round(pre_prestige_total * mult))
                if amount <= 0:
                    amount = 1

                await safe_credit(
                    guild_id, user_id, amount,
                    reason=f"Daily reward (streak day {streak})",
                    source="daily", db=db)
                balance_row = await (await db.execute(
                    "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))).fetchone()
                new_balance = int(balance_row[0]) if balance_row else amount
                await db.commit()
            except DailyAlreadyClaimed:
                await db.execute("ROLLBACK")
                raise
            except Exception:
                await db.execute("ROLLBACK")
                raise
    except DailyAlreadyClaimed:
        raise

    receipt_private, receipt_public = receipt_destinations(
        config["receipt_visibility"])
    return {
        **transition,
        "guild_id": int(guild_id),
        "user_id": int(user_id),
        "streak": streak,
        "stage": stage,
        "base": base,
        "streak_bonus": streak_bonus,
        "pre_prestige_total": pre_prestige_total,
        "multiplier": float(mult),
        "amount": amount,
        "credited_total": amount,
        "new_balance": new_balance,
        "bonus_capped": bonus_capped,
        "v2_enabled": bool(config["v2_enabled"]),
        "receipt_visibility": config["receipt_visibility"],
        "receipt_channel_id": config["receipt_channel_id"],
        "receipt_private": receipt_private,
        "receipt_public": receipt_public,
        "member_name": getattr(member, "display_name", None) or str(user_id),
        "next_reset_seconds": _seconds_until_next_cairo_midnight(now),
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
