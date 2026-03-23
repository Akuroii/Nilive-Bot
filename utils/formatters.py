from datetime import datetime, timezone

# ══════════════════════════════════════════════════════
# RULE 1 — SNAPSHOT HELPERS
# Every log table must save display_name + avatar_url
# at the TIME of the action, not looked up later.
# If a user leaves the server, we still show their name.
# ══════════════════════════════════════════════════════

def snapshot_user(member) -> dict:
    """
    Takes a discord.Member or discord.User and returns
    a snapshot dict with id, display_name, avatar_url.

    Usage in cogs:
        snap = snapshot_user(member)
        # Then use snap["display_name"] and snap["avatar_url"]
        # when inserting into any log table.
    """
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
    """Alias for snapshot_user — same thing, clearer name in mod contexts."""
    return snapshot_user(member)


# ══════════════════════════════════════════════════════
# DATE + TIME FORMATTERS
# ══════════════════════════════════════════════════════

def now_iso() -> str:
    """Returns current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def format_timestamp(ts: str | None, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    """
    Formats an ISO timestamp string for display.
    Returns 'Unknown' if ts is None or unparseable.
    """
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return ts[:16] if len(ts) >= 16 else ts


def format_date_only(ts: str | None) -> str:
    """Returns just the date part: YYYY-MM-DD"""
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if len(ts) >= 10 else ts


def format_relative(ts: str | None) -> str:
    """
    Returns a human-readable relative time string.
    e.g. '2 hours ago', 'just now', '3 days ago'
    """
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


# ══════════════════════════════════════════════════════
# NUMBER FORMATTERS
# ══════════════════════════════════════════════════════

def format_number(n: int | float) -> str:
    """Formats a number with commas: 1234567 → '1,234,567'"""
    return f"{int(n):,}"


def format_coins(n: int, currency_name: str = "Coins") -> str:
    """
    Formats a coin amount with currency name.
    currency_name comes from guild_settings.currency_name
    """
    return f"{format_number(n)} {currency_name}"


def format_duration(minutes: int | None) -> str:
    """Formats a duration in minutes to readable string."""
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


# ══════════════════════════════════════════════════════
# AVATAR URL HELPERS
# ══════════════════════════════════════════════════════

def avatar_url_or_default(avatar_url: str | None,
                           user_id: int | None = None) -> str:
    """
    Returns the avatar URL or a Discord default avatar URL.
    Used in dashboard templates to always show an image.
    """
    if avatar_url:
        return avatar_url
    if user_id:
        default_index = (user_id >> 22) % 6
        return f"https://cdn.discordapp.com/embed/avatars/{default_index}.png"
    return "https://cdn.discordapp.com/embed/avatars/0.png"
