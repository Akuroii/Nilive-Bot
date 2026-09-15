from datetime import datetime, timezone
import re

# Duration parsing: accepts strings like "30m", "2h", "3d"
# Returns total seconds as an integer, or raises ValueError for invalid input.
DURATION_RE = re.compile(r"^(\d+)\s*([mhd])\s*$", re.IGNORECASE)
MAX_DURATION_SECONDS = 60 * 60 * 24 * 28  # 28 days (matches dashboard limit)


def parse_duration(duration_str: str) -> int:
    """
    Parse a duration string into total seconds.
    
    Accepts relative shorthand like:
    - "30m" or "30M" -> 30 minutes
    - "2h" or "2H" -> 2 hours  
    - "3d" or "3D" -> 3 days
    
    Returns total seconds as an integer.
    
    Raises:
    - ValueError: if the string cannot be parsed or duration exceeds 28 days
    """
    if not duration_str or not isinstance(duration_str, str):
        raise ValueError("Duration must be a non-empty string")
    
    duration_str = duration_str.strip()
    
    # Try to match simple format: <number><unit>
    m = DURATION_RE.match(duration_str)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        
        if unit == "m":
            total_seconds = amount * 60
        elif unit == "h":
            total_seconds = amount * 60 * 60
        elif unit == "d":
            total_seconds = amount * 60 * 60 * 24
        else:
            raise ValueError(f"Unknown duration unit: {unit}")
        
        if total_seconds <= 0:
            raise ValueError("Duration must be positive")
        if total_seconds > MAX_DURATION_SECONDS:
            raise ValueError(f"Duration cannot exceed 28 days")
        
        return total_seconds
    
    # If no match, fail with helpful message
    raise ValueError(
        f"Invalid duration format: '{duration_str}'. "
        f"Use formats like '30m', '2h', or '3d'"
    )


def snapshot_user(member) -> dict:
    if member is None:
        return {
            "id":           0,
            "display_name": "Unknown User",
            "avatar_url":   None,
        }
    avatar = None
    if hasattr(member, "display_avatar") and member.display_avatar:
        avatar = str(member.display_avatar.url)
    elif hasattr(member, "avatar") and member.avatar:
        avatar = str(member.avatar.url)
    return {
        "id":           member.id,
        "display_name": getattr(member, "display_name", None) or str(member),
        "avatar_url":   avatar,
    }


def snapshot_member(member) -> dict:
    return snapshot_user(member)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_timestamp(ts: str | None, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return ts[:16] if len(ts) >= 16 else ts


def format_date_only(ts: str | None) -> str:
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if len(ts) >= 10 else ts


def format_relative(ts: str | None) -> str:
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            m = seconds // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        elif seconds < 86400:
            h = seconds // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        elif seconds < 604800:
            d = seconds // 86400
            return f"{d} day{'s' if d != 1 else ''} ago"
        else:
            return format_date_only(ts)
    except Exception:
        return "Unknown"


def format_number(n: int | float) -> str:
    return f"{int(n):,}"


def format_coins(n: int, currency_name: str = "Coins") -> str:
    return f"{format_number(n)} {currency_name}"


def format_duration(minutes: int | None) -> str:
    if not minutes:
        return "Permanent"
    if minutes < 60:
        return f"{minutes}m"
    elif minutes < 1440:
        h = minutes // 60
        m = minutes % 60
        return f"{h}h {m}m" if m else f"{h}h"
    else:
        d = minutes // 1440
        h = (minutes % 1440) // 60
        return f"{d}d {h}h" if h else f"{d}d"


def avatar_url_or_default(avatar_url: str | None,
                           user_id: int | None = None) -> str:
    if avatar_url:
        return avatar_url
    if user_id:
        default_index = (user_id >> 22) % 6
        return f"https://cdn.discordapp.com/embed/avatars/{default_index}.png"
    return "https://cdn.discordapp.com/embed/avatars/0.png"
