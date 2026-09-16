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

The two kinds are also *looked up* differently, and that difference is the
trap `CHECK_EMOJI` resolution is built around:

  * guild emojis live in the gateway cache — `bot.emojis` /
    `bot.get_emoji(id)`. discord.py's own docstring for `Client.emojis`
    says "This does not include the emojis that are owned by the
    application", so a probe that only looks there can NEVER find an
    application emoji — no matter how many the application owns;
  * application emojis have no cache in discord.py 2.x at all. The only
    way to see them is the HTTP API: `await bot.fetch_application_emoji(id)`
    or `await bot.fetch_application_emojis()` (2.5+), both of which need a
    logged-in client (token + application_id).

So "can the bot reach this emoji?" is an ASYNC question, while rendering is
SYNC. `verify_check_emoji()` answers it (once, then cached — see the state
machine by CHECK_EMOJI); `resolve_check_emoji()` just reads that answer at
render time. An inconclusive answer — client not logged in yet, HTTP
hiccup, rate limit, a discord.py without the API — keeps the custom token,
because an application emoji renders in every guild by definition and
downgrading it to unicode on a *guess* is a silent visual regression. Only
a positive 404 from Discord's own application-emoji endpoint (the emoji was
really deleted from the application) degrades to `CHECK_EMOJI_FALLBACK`.

Unicode emoji (🪙, 💎, 🌙, and any letter/script such as Arabic) have none
of these constraints and always render.
"""
from __future__ import annotations

import asyncio
import re
import time

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
# THIS IS AN APPLICATION EMOJI: it was added in Developer Portal →
# Application → Emoji, so it belongs to the bot application (which owns up
# to ~2,000 of them) and NOT to any server. Consequences, all of them
# deliberate:
#
#   * it renders in every guild the bot is in, with no guild membership,
#     no USE_EXTERNAL_EMOJIS and no per-server setup;
#   * it must NEVER be looked up in a guild emoji list — `bot.emojis` /
#     `bot.get_emoji()` cannot see application emojis by design (see the
#     module header), so treating "not in this server's emoji list" as
#     "unavailable" is simply wrong and is what used to downgrade this to
#     a unicode ✅ on the Missions panel;
#   * the only authoritative check is the application-emoji HTTP endpoint,
#     which `verify_check_emoji()` below calls.
CHECK_EMOJI = "<a:Check:1549831102078787744>"
CHECK_EMOJI_ID = 1549831102078787744
# Last-resort glyph, used ONLY when Discord itself answers that this ID is
# not one of the application's emojis (see CHECK_STATE_MISSING). An
# inconclusive probe must never land here.
CHECK_EMOJI_FALLBACK = "✅"

# ── CHECK_EMOJI reachability state ───────────────────────────────────────
# One module-level answer shared by every renderer, because the question
# ("does the application own this emoji?") has nothing to do with which
# guild, channel or member is being rendered.
CHECK_STATE_UNKNOWN = "unknown"      # not probed yet, or the probe couldn't answer
CHECK_STATE_CONFIRMED = "confirmed"  # Discord: ours, application-owned → use the token
CHECK_STATE_MISSING = "missing"      # Discord: 404, not ours → unicode fallback

# How long a NEGATIVE or inconclusive answer is trusted before the next
# probe. Confirmed is terminal for the process (an application emoji can
# only stop existing by being deleted, which is a deliberate act followed
# in practice by a restart); `force=True` re-asks regardless. Five minutes
# is short enough that adding the emoji in the Portal self-heals without a
# redeploy, and long enough that a render path can never turn into an HTTP
# call storm.
CHECK_REPROBE_SECONDS = 300.0

_check_state: dict = {
    "state": CHECK_STATE_UNKNOWN,
    "token": CHECK_EMOJI,
    "detail": "not probed yet",
    "probed_at": 0.0,
}


def check_emoji_state() -> str:
    """Current reachability verdict for CHECK_EMOJI (see CHECK_STATE_*)."""
    return _check_state["state"]


def check_emoji_detail() -> str:
    """Human-readable reason behind `check_emoji_state()` — for logs."""
    return _check_state["detail"]


def _set_check_state(state: str, detail: str, token: str | None = None) -> None:
    _check_state["state"] = state
    _check_state["detail"] = detail
    if token:
        _check_state["token"] = token


def _is_check_emoji(obj) -> bool:
    """True when `obj` is an emoji object carrying CHECK_EMOJI_ID."""
    if obj is None:
        return False
    try:
        return int(getattr(obj, "id", 0) or 0) == CHECK_EMOJI_ID
    except (TypeError, ValueError):
        return False


def _token_for(emoji) -> str:
    """The canonical `<a:name:id>` token for an emoji object, falling back
    to the constant. discord.py builds `str(Emoji)` as exactly that token,
    and taking the name from Discord keeps it correct even if the emoji is
    ever renamed in the Portal (the ID is what renders; the name is
    cosmetic). Anything unexpected keeps the constant rather than risking
    a malformed token."""
    try:
        candidate = str(emoji)
    except Exception:
        return CHECK_EMOJI
    if is_custom_emoji_token(candidate) and str(CHECK_EMOJI_ID) in candidate:
        return candidate
    return CHECK_EMOJI


def _confirm(emoji, detail: str) -> bool:
    _set_check_state(CHECK_STATE_CONFIRMED, detail, token=_token_for(emoji))
    return True


def _is_not_found(exc) -> bool:
    """Discord's 404 — the one answer that legitimately means 'the
    application does not own this emoji'. Matched by status first (works
    for any HTTPException shape) and by class name second, so this module
    still behaves when it is imported somewhere discord.py isn't."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status == 404
    return type(exc).__name__ == "NotFound"


def _probe_sync(bot) -> bool:
    """Confirm from whatever the client already has in memory. Never
    disproves anything — absence from these caches says nothing about
    application ownership (that is the whole bug this guards against), and
    an exception here (a bot double without the attributes, say) leaves the
    current verdict untouched rather than erasing it."""
    try:
        # A cache of application emojis, if this discord.py (or a fork)
        # ever keeps one. 2.x does not; the attribute check keeps us
        # working with one that does.
        app_emojis = getattr(bot, "application_emojis", None)
        if callable(app_emojis):
            for e in (app_emojis() or ()):
                if _is_check_emoji(e):
                    return _confirm(e, "found in the client's application-emoji cache")
        # Guild-emoji cache. Checked for completeness — if the same ID
        # happens to exist as a guild emoji the bot can see, that is also
        # a reachable emoji — but it is NOT where an application emoji
        # lives, so a miss here means nothing.
        emoji = bot.get_emoji(CHECK_EMOJI_ID)
        if _is_check_emoji(emoji):
            return _confirm(emoji, "found in the client's emoji cache")
    except Exception:
        # A capability probe must never break rendering, and never rewrite
        # a verdict it has no information about.
        return False
    return False


async def _fetch_application_emoji(bot) -> tuple[str, object]:
    """Ask Discord's application-emoji endpoint about CHECK_EMOJI_ID.

    Returns `(state, payload)` where payload is the Emoji object for
    CONFIRMED and a reason string otherwise.

    A 404 on the single-emoji GET *probably* means "the application does
    not own this id", but nothing user-visible downgrades on one answer
    alone: the full list is consulted as a second opinion, because a
    single odd 404 shouldn't cost the panel its glyph and the extra
    request costs nothing. Any ERROR — as opposed to a clean 404 — leaves
    the state inconclusive, which keeps the token.
    """
    fetch_one = getattr(bot, "fetch_application_emoji", None)
    fetch_all = getattr(bot, "fetch_application_emojis", None)
    if not callable(fetch_one) and not callable(fetch_all):
        return (CHECK_STATE_UNKNOWN,
                "this discord.py exposes no application-emoji API (needs 2.5+)")
    # Not logged in yet (cogs load BEFORE bot.start() in main.py, so there
    # is neither a token nor an application_id at that point): the call
    # could only fail, and its failure would mean nothing.
    if getattr(bot, "application_id", None) is None:
        return (CHECK_STATE_UNKNOWN,
                "client is not logged in yet (no application_id)")

    # Set when the single-emoji GET answered 404. (Not the exception
    # object itself — Python unbinds `except … as exc` names at the end of
    # the handler.)
    single_not_found: str | None = None

    if callable(fetch_one):
        try:
            emoji = await fetch_one(CHECK_EMOJI_ID)
        except Exception as exc:
            if not _is_not_found(exc):
                return (CHECK_STATE_UNKNOWN,
                        f"application-emoji lookup failed: "
                        f"{type(exc).__name__}: {exc}")
            single_not_found = (f"{type(exc).__name__} (404) from "
                                f"GET /applications/@me/emojis/{CHECK_EMOJI_ID}")
        else:
            if _is_check_emoji(emoji):
                return (CHECK_STATE_CONFIRMED, emoji)
            return (CHECK_STATE_UNKNOWN,
                    f"application-emoji endpoint returned a different id "
                    f"({getattr(emoji, 'id', '?')!r})")

    if not callable(fetch_all):
        return (CHECK_STATE_MISSING,
                single_not_found or
                "the application does not own this emoji id")

    # Either the single GET 404'd (this is the second opinion), or this
    # client only exposes the list API.
    try:
        listing = await fetch_all()
    except Exception as exc:
        detail = (f"application-emoji list failed: "
                  f"{type(exc).__name__}: {exc}")
        if single_not_found:
            detail = (f"{single_not_found}, and the second-opinion list "
                      f"lookup also failed ({type(exc).__name__}) — "
                      f"inconclusive, keeping the custom token")
        return (CHECK_STATE_UNKNOWN, detail)

    items = listing.get("items") if isinstance(listing, dict) else listing
    items = list(items or ())
    for e in items:
        if _is_check_emoji(e):
            return (CHECK_STATE_CONFIRMED, e)
    return (CHECK_STATE_MISSING,
            f"not among the application's {len(items)} emoji "
            f"(Developer Portal → Application → Emoji)"
            + (f"; {single_not_found}" if single_not_found else ""))


async def verify_check_emoji(bot, *, force: bool = False,
                             timeout: float | None = None) -> str:
    """Answer "can this bot render CHECK_EMOJI?" and cache the answer.

    Call it once the client is logged in (`on_ready`), or let a renderer
    call it — it is cheap and self-throttling: after the first probe, an
    inconclusive or negative verdict is reused for
    `CHECK_REPROBE_SECONDS` and a confirmed one for the life of the
    process, so a busy render path never becomes an HTTP call per render.

    `timeout` caps how long the probe may take, for callers that are on a
    Discord clock (an interaction has ~3s to acknowledge, and /missions
    renders before it responds): a probe that runs long is abandoned and
    recorded as inconclusive, which keeps the custom token instead of
    delaying the member's panel. None (the default, used from on_ready)
    waits as long as discord.py itself would.

    Returns the string to render. Never raises: a probe that cannot run
    leaves the state inconclusive, and inconclusive means "keep the custom
    token" — an application emoji renders everywhere, so guessing 'no' is
    the only way this could visibly break the UI.
    """
    if bot is None:
        return resolve_check_emoji(None)

    now = time.monotonic()
    state = _check_state["state"]
    if not force:
        if state == CHECK_STATE_CONFIRMED:
            return _check_state["token"]
        if _check_state["probed_at"] and \
                (now - _check_state["probed_at"]) < CHECK_REPROBE_SECONDS:
            return resolve_check_emoji(bot)
    # Claim the probe slot BEFORE awaiting: concurrent renders arriving
    # while the GET is in flight then read the cached verdict instead of
    # each firing their own request. (No asyncio.Lock on purpose — this
    # module is imported by the dashboard too, whose coroutines run on a
    # different loop, and a lock bound to one loop breaks the other. The
    # call is an idempotent read, so the worst case is a rare duplicate.)
    _check_state["probed_at"] = now

    if _probe_sync(bot):
        return _check_state["token"]

    try:
        if timeout:
            outcome, payload = await asyncio.wait_for(
                _fetch_application_emoji(bot), timeout=timeout)
        else:
            outcome, payload = await _fetch_application_emoji(bot)
    except asyncio.TimeoutError:
        outcome, payload = (CHECK_STATE_UNKNOWN,
                            f"probe abandoned after {timeout}s — keeping the "
                            f"custom token rather than delaying the render")
    except Exception as exc:                       # absolutely never raise
        outcome, payload = (CHECK_STATE_UNKNOWN,
                            f"probe raised: {type(exc).__name__}: {exc}")

    if outcome == CHECK_STATE_CONFIRMED:
        _confirm(payload, "confirmed via the application-emoji endpoint "
                          "(owned by the bot application — renders in every server)")
    elif outcome == CHECK_STATE_MISSING:
        _set_check_state(CHECK_STATE_MISSING, str(payload))
    elif _check_state["state"] == CHECK_STATE_MISSING:
        # Already known-bad, and this re-probe couldn't answer: keep the
        # verdict instead of resurrecting a token Discord has said isn't
        # ours (which would render as literal text).
        _set_check_state(CHECK_STATE_MISSING,
                         f"{_check_state['detail']}; re-probe inconclusive "
                         f"({payload})")
    else:
        # Inconclusive: keep the token we already had; only the reason
        # changes. An application emoji renders everywhere, so "couldn't
        # verify" must never become "downgrade to unicode".
        _set_check_state(CHECK_STATE_UNKNOWN, str(payload))
    return resolve_check_emoji(bot)


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
    """The check glyph to render right now — sync, render-path safe.

    This is the application emoji `<a:check:…>` unless Discord has
    positively said the application no longer owns that ID
    (`check_emoji_state() == "missing"`), in which case the unicode ✅ is
    returned so members never see raw `<a:check:…>` token text.

    Crucially it does NOT require the emoji to be a guild emoji: being
    absent from this server's (or every server's) emoji list is normal and
    expected for an application emoji, and must not downgrade the render.
    Pass `bot` to also allow the free in-memory confirmation
    (`_probe_sync`); the authoritative check is `await verify_check_emoji(bot)`,
    which a caller with an event loop should have run at least once.
    """
    if bot is not None and _check_state["state"] != CHECK_STATE_CONFIRMED:
        _probe_sync(bot)
    if _check_state["state"] == CHECK_STATE_MISSING:
        return CHECK_EMOJI_FALLBACK
    return _check_state["token"] or CHECK_EMOJI


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


def as_partial_emoji(value: str):
    """Convert a configured emoji string into a `discord.PartialEmoji` for
    a component (button) — or None when it cannot be one.

    Components are stricter than message content: a button's emoji must be
    a real emoji, so a currency whose configured icon is literal text, or
    an empty string, gets no glyph instead of an invalid component that
    Discord rejects outright (which would break the whole view).

    Returns None rather than raising for every unusable input, because
    this runs inside view construction — a bad icon must not be able to
    take a panel down.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        # Imported here, not at module scope: this module is also imported
        # by the dashboard, which has no Discord client and must not need
        # discord.py to render a label.
        import discord
        pe = discord.PartialEmoji.from_str(raw)
    except Exception:
        return None
    # A bare word or digits parses into a "name" with no id and is not a
    # usable emoji; a unicode emoji parses into a single character.
    if pe.id is None and len(pe.name or "") > 2:
        return None
    return pe if (pe.id is not None or pe.name) else None


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
