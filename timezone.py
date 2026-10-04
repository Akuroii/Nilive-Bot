"""
Bot-wide canonical timezone: Africa/Cairo.

- Store timestamps in UTC.
- Interpret calendar/day/week boundaries using Africa/Cairo.
- User-facing dates/times should use Cairo where appropriate.
- Discord-native timestamps remain viewer-local.
- Duration/expiry (rolling) systems NOT changed.

Normalization:
 - Old guild_settings.timezone='UTC' (historical default) -> Africa/Cairo
 - Legacy 'Asia/Cairo' -> Africa/Cairo
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CANONICAL_TZ = "Africa/Cairo"
CAIRO_TZ = ZoneInfo(CANONICAL_TZ)

# Legacy values that must be treated as Cairo
_LEGACY_TO_CANONICAL = {
    "UTC": CANONICAL_TZ,
    "Etc/UTC": CANONICAL_TZ,
    "GMT": CANONICAL_TZ,
    "Etc/GMT": CANONICAL_TZ,
    "Asia/Cairo": CANONICAL_TZ,
}

def normalize_timezone(tz: str | None) -> str:
    """
    Normalize a stored timezone string to the canonical value where
    appropriate. Old 'UTC' and 'Asia/Cairo' are mapped to Africa/Cairo.
    Empty/None -> Africa/Cairo (bot default).
    Valid IANA zones are returned unchanged (future per-guild support),
    invalid zones fallback to Africa/Cairo.
    """
    if not tz or not isinstance(tz, str):
        return CANONICAL_TZ
    tz = tz.strip()
    if not tz:
        return CANONICAL_TZ
    # Legacy mapping (case-insensitive for safety)
    if tz in _LEGACY_TO_CANONICAL:
        return CANONICAL_TZ
    low = tz.lower()
    if low == "utc" or low == "asia/cairo":
        return CANONICAL_TZ
    # Try to validate as real IANA zone
    try:
        ZoneInfo(tz)
        return tz
    except Exception:
        return CANONICAL_TZ

def get_cairo_tz() -> ZoneInfo:
    return CAIRO_TZ

def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def cairo_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(CAIRO_TZ)

def cairo_date_of(dt: datetime | None = None) -> datetime.date:
    """Calendar date in Cairo for the given UTC instant."""
    utc = _ensure_utc(dt) if dt is not None else datetime.now(timezone.utc)
    return utc.astimezone(CAIRO_TZ).date()

def get_cairo_daily_key(now: datetime | None = None) -> str:
    """Daily period key = Cairo calendar date ISO (YYYY-MM-DD)."""
    return cairo_date_of(now).isoformat()

def _cairo_saturday_of(dt: datetime) -> datetime.date:
    """
    Saturday that starts the week containing dt (in Cairo).
    Week is Saturday 00:00 Cairo -> next Saturday 00:00 Cairo.
    Saturday weekday == 5 (Mon=0)
    """
    c = dt.astimezone(CAIRO_TZ) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(CAIRO_TZ)
    days_since_sat = (c.weekday() - 5) % 7
    return (c.date() - timedelta(days=days_since_sat))

def get_cairo_weekly_key(now: datetime | None = None) -> str:
    """Weekly period key = Saturday date ISO in Cairo."""
    utc = _ensure_utc(now) if now is not None else datetime.now(timezone.utc)
    return _cairo_saturday_of(utc).isoformat()

def seconds_until_cairo_midnight(now: datetime | None = None) -> int:
    utc = _ensure_utc(now) if now is not None else datetime.now(timezone.utc)
    now_cairo = utc.astimezone(CAIRO_TZ)
    next_day = now_cairo.date() + timedelta(days=1)
    next_midnight = datetime(next_day.year, next_day.month, next_day.day, tzinfo=CAIRO_TZ)
    # Use total_seconds between Cairo instants (handles DST correctly)
    delta = (next_midnight - now_cairo).total_seconds()
    return max(0, int(delta))

def seconds_until_cairo_saturday(now: datetime | None = None) -> int:
    utc = _ensure_utc(now) if now is not None else datetime.now(timezone.utc)
    now_cairo = utc.astimezone(CAIRO_TZ)
    # Find this week's Saturday, then next Saturday
    this_sat = _cairo_saturday_of(utc)
    next_sat = this_sat + timedelta(days=7)
    next_midnight = datetime(next_sat.year, next_sat.month, next_sat.day, tzinfo=CAIRO_TZ)
    delta = (next_midnight - now_cairo).total_seconds()
    # If we're exactly at Saturday 00:00, delta is 7 days, which is correct
    return max(0, int(delta))

def format_cairo_countdown(seconds: int) -> str:
    """
    H:MMH formatting for countdowns.
    Example: 1:12H, 0:05H, 24:00H etc.
    Hours is total hours remaining (not modulo 24), minutes 00-59.
    At <60s remaining we still show 0:01H so the counter never shows 0:00H
    before the reset actually happens.
    """
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if seconds > 0 and hours == 0 and minutes == 0:
        # less than a minute left -> show 0:01H
        minutes = 1
    return f"{hours}:{minutes:02d}H"

# Backwards-compatible coarse formatter (kept for reference, not used)
def format_reset_countdown_coarse(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = max(1, rem // 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"

def utc_to_cairo_str(ts: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Convert stored UTC ISO string to Cairo display string."""
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        c = dt.astimezone(CAIRO_TZ)
        return c.strftime(fmt)
    except Exception:
        # fallback: slice
        return ts[:16] if len(ts) >= 16 else ts

def utc_to_cairo_display(ts: str | None) -> str:
    """For dashboard: 'YYYY-MM-DD · HH:MM' in Cairo."""
    return utc_to_cairo_str(ts, "%Y-%m-%d · %H:%M")

async def get_guild_timezone(guild_id: int) -> str:
    """
    Read guild_settings.timezone and normalize it. This is intentionally
    NOT used to vary calendar logic (bot is Cairo-canonical), but is
    provided so any future per-guild call can get a sane value without
    reimplementing normalization. Currently all calendar helpers use
    Cairo directly regardless of this value.
    """
    import aiosqlite
    from database import DB_PATH
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT timezone FROM guild_settings WHERE guild_id=?", (guild_id,))
            row = await cur.fetchone()
            if row and row[0]:
                return normalize_timezone(row[0])
    except Exception:
        pass
    return CANONICAL_TZ

async def normalize_guild_timezone_in_db(guild_id: int):
    """Ensure DB row uses canonical value if it held UTC/Asia/Cairo."""
    import aiosqlite
    from database import DB_PATH
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT timezone FROM guild_settings WHERE guild_id=?", (guild_id,))
            row = await cur.fetchone()
            if row and row[0]:
                norm = normalize_timezone(row[0])
                if norm != row[0]:
                    await db.execute("UPDATE guild_settings SET timezone=? WHERE guild_id=?", (norm, guild_id))
                    await db.commit()
    except Exception as e:
        print(f"[TIMEZONE] normalize guild {guild_id} failed: {e}")
