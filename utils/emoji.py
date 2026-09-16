"""
Shared emoji helpers — one parser, one normaliser, one place the bot's
own fixed emoji live.

Why this module exists
----------------------
The Embed Builder grew a robust "accept a raw ID, or a full `<:name:id>` /
`<a:name:id>` token" parser (`_parse_emoji_input`) because admins paste an
emoji in every form imaginable. Currency configuration needs exactly the
same tolerance, so rather than writing a second parser the original one was
hoisted here and both callers now share it. One implementation, no drift.

Emoji accessibility — the rule that shapes everything below
----------------------------------------------------------
A Discord custom emoji only *renders* when the bot can use it:

  * a guild emoji renders only in messages the bot sends to a guild it is
    a member of (and, for interaction responses that are later EDITED,
    only when the app holds the `bot` scope on the emoji's home guild —
    see discord/discord-api-docs#3092);
  * an **application emoji** belongs to the bot application itself and
    renders anywhere, with no guild membership and no permission needed.

That asymmetry is why `utils/app_emoji_cache.py` exists, and why it is the
right answer whenever a custom emoji must be guaranteed to show up. Nothing
in this module silently depends on guild membership: a custom emoji the bot
cannot reach renders as its literal `<:name:id>` text, so callers that need
a hard guarantee should point at an application emoji.

Unicode emoji (🪙, 💎, 🌙, and any letter/script such as Arabic) have none
of these constraints and always render.
"""
from __future__ import annotations

import re

# ═══════════════════════════════════════════════════════════════════════
# The bot's own fixed emoji
# ═══════════════════════════════════════════════════════════════════════

# Success/check indicator, used across Discord-facing success lines,
# confirmations and buttons. Single source of truth — do not write the
# rendered form of this emoji anywhere else in the codebase.
#
# Animated application-style emoji, so the plain `<a:name:id>` token is
# correct for both message content and buttons.
#
# ACCESSIBILITY CAVEAT (see module header): this ID must be reachable by
# the bot — either an application emoji (best: renders in every guild) or
# a guild emoji in a guild the bot is in. If it is neither, every site
# that uses this constant renders the literal token text. resolve_check_
# emoji() below exists so callers that can check (they have a Client)
# degrade to a plain unicode check instead of showing that text.
CHECK_EMOJI = "<a:check:1549593658867712090>"
CHECK_EMOJI_ID = 1549593658867712090
CHECK_EMOJI_FALLBACK = "✅"

# Discord's public emoji CDN. Serves any emoji image by ID with no auth
# and no bot membership, which is what makes it the right primitive for
# dashboard previews (the browser cannot render `<:name:id>` markdown —
# that is a Discord message feature, not HTML).
EMOJI_CDN = "https://cdn.discordapp.com/emojis/{id}.{ext}"


def emoji_cdn_url(emoji_id, animated: bool = False) -> str:
    """Direct image URL for an emoji ID. Works for emoji the bot has no
    access to, so a dashboard preview can never be fooled into showing
    nothing for a perfectly valid ID."""
    return EMOJI_CDN.format(id=str(emoji_id), ext="gif" if animated else "png")


def resolve_check_emoji(bot=None) -> str:
    """CHECK_EMOJI when the bot can actually use it, else a unicode check.

    `bot` is a discord.Client/commands.Bot (or anything with `.get_emoji`
    and `.emojis`). Passing it is optional: with no bot, or when the bot
    has no opinion, the constant is returned unchanged — a caller that
    can't verify shouldn't silently downgrade how things look.

    Resolution order mirrors how Discord itself decides reachability:
      1. the bot's application emojis (`bot.application_emojis()`, 2.4+)
      2. the bot's own emoji cache (guild emojis it can see)
    """
    if bot is None:
        return CHECK_EMOJI
    try:
        app_emojis = getattr(bot, "application_emojis", None)
        if callable(app_emojis):
            for e in app_emojis():
                if getattr(e, "id", None) == CHECK_EMOJI_ID:
                    return str(e)
        emoji = bot.get_emoji(CHECK_EMOJI_ID)
        if emoji is not None:
            return str(emoji)
    except Exception:
        # A capability probe must never break rendering — fall through to
        # the constant and let Discord show whatever it shows.
        return CHECK_EMOJI
    return CHECK_EMOJI_FALLBACK


# ═══════════════════════════════════════════════════════════════════════
# Parsing / normalising
# ═══════════════════════════════════════════════════════════════════════

_EMOJI_TOKEN_RE = re.compile(r"<(a?):(\w+):(\d+)>")
# Discord emoji names are [a-zA-Z0-9_], 2-32 chars. `_` is deliberately
# used as the placeholder name for an ID-only emoji (the long-standing
# community convention), so it is a valid token name, not a special case.
_PLACEHOLDER_NAME = "_"


def parse_emoji_input(raw: str) -> tuple[str, str, bool] | None:
    """Accepts a raw numeric ID, or a full `<:name:id>` / `<a:name:id>`
    token. Returns `(emoji_id, name, animated)` or None if nothing usable
    was found.

    Hoisted verbatim from dashboard/api/embedbuilder.py so the Embed
    Builder's picker and the Economy currency fields share one parser.
    """
    raw = (raw or "").strip()
    m = _EMOJI_TOKEN_RE.match(raw)
    if m:
        return m.group(3), m.group(2), bool(m.group(1))
    if raw.isdigit():
        return raw, f"emoji_{raw}", False
    return None


def is_custom_emoji_token(raw: str) -> bool:
    """True when `raw` is already a well-formed `<:name:id>` token."""
    return bool(_EMOJI_TOKEN_RE.match((raw or "").strip()))


def build_custom_emoji(emoji_id, name: str, animated: bool = False) -> str:
    """Canonical `<:name:id>` / `<a:name:id>` token — the exact form
    Discord itself produces, and the form the rest of the codebase
    recognises."""
    prefix = "a" if animated else ""
    return f"<{prefix}:{name}:{emoji_id}>"


def normalize_currency_emoji(raw: str, known_emojis=None) -> str:
    """Turn anything an admin might type into something Discord can render.

    Accepted input, and what gets stored:

      * `""` / whitespace / None    -> "" (meaning "unset -> use default")
      * a unicode emoji             -> unchanged (🪙, 💎, 🌙, and any
                                       script, e.g. Arabic text)
      * `<:name:id>` / `<a:name:id>` -> unchanged (already canonical)
      * a bare numeric emoji ID     -> the canonical token, using the
                                       real name when it is in
                                       `known_emojis`, otherwise the
                                       `_` placeholder name.

    `known_emojis` is an optional iterable of `{id, name, animated}`
    mappings — the shape `/api/guild/emojis` and `/api/app-emojis`
    already return. Supplying it is what lets a pasted bare ID become a
    properly-named token; without it the emoji still renders, the name is
    only used for Discord's "emoji was deleted" fallback text.

    Returns "" for empty input so the caller can treat that as "revert to
    the default" without a second empty-check.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    # Already a full token — leave it exactly as typed (admins often paste
    # the escaped form, and rewriting it would be pointless churn).
    if is_custom_emoji_token(raw):
        return raw

    # A bare ID is the one form Discord cannot render on its own, because
    # message emoji markup requires name:id. Resolve a name if we can.
    if raw.isdigit():
        name, animated = _PLACEHOLDER_NAME, False
        for e in (known_emojis or ()):
            if str(e.get("id")) == raw:
                name = e.get("name") or _PLACEHOLDER_NAME
                animated = bool(e.get("animated"))
                break
        return build_custom_emoji(raw, name, animated)

    # Anything else is treated as a unicode emoji (or literal text, if the
    # admin really wants that). Stored verbatim.
    return raw
