"""The success/check indicator as HTML, for the dashboard.

Discord's `<a:check:id>` markup is a *message* feature — a browser cannot
render it, and printing the token would show members the raw
`<a:check:1549…>` text. HTML therefore needs the underlying image, which is
exactly what Discord's public emoji CDN serves by ID (`utils/emoji.py:
emoji_cdn_url`). The bot's Discord-side sites use the token itself; both
sides resolve the SAME id/constant from `utils/emoji.py`, so the check mark
is defined once.

Exposed two ways, from this one implementation:

  * as a Jinja global (`check_icon`) for server-rendered markup,
  * as `window.__CHECK_ICON__` (base.html) for client-side toasts/status
    lines, via `checkIconHtml()` in dashboard.js.

Every branch is failure-tolerant: if the configured emoji cannot be parsed
the unicode fallback is returned, and the `<img>` itself carries an
`onerror` that swaps in the same fallback — a deleted emoji must never
leave a broken-image icon in the UI.
"""

import time

from markupsafe import Markup

# The app-emoji cache changes only when an admin imports an emoji, and the
# fallback path is a constant — so the lookup is memoised rather than
# queried on every template render (this runs on every dashboard page).
_CACHE_TTL_SECONDS = 300
_cache: dict = {"at": 0.0, "value": None}


def _cached_resolve():
    now = time.time()
    if _cache["value"] is None or (now - _cache["at"]) > _CACHE_TTL_SECONDS:
        _cache["value"] = _resolve()
        _cache["at"] = now
    return _cache["value"]


def _resolve():
    """(emoji_id, animated) for the check emoji, or None for unicode.

    Prefers the application-emoji cache when it knows this emoji: the cache
    row carries the real `animated` flag, which decides between the .gif
    and .png CDN URL. Falls back to the constant's own token.
    """
    from utils.emoji import (
        CHECK_EMOJI, CHECK_EMOJI_ID, parse_emoji_input,
    )
    try:
        from dashboard.utils.async_utils import run_async
        from utils.app_emoji_cache import list_all
        for row in run_async(list_all()):
            if str(row.get("id")) == str(CHECK_EMOJI_ID):
                return str(CHECK_EMOJI_ID), bool(row.get("animated"))
    except Exception:
        # No cache table / no DB in this process — the constant below still
        # knows the emoji's own animated flag.
        pass
    parsed = parse_emoji_input(CHECK_EMOJI)
    if not parsed:
        return None
    emoji_id, _name, animated = parsed
    return emoji_id, animated


def check_icon_html() -> Markup:
    """`<img>` for a custom check emoji, or the unicode check itself."""
    from utils.emoji import (
        CHECK_EMOJI_FALLBACK, emoji_cdn_url,
    )
    resolved = _cached_resolve()
    if resolved is None:
        return Markup(CHECK_EMOJI_FALLBACK)
    emoji_id, animated = resolved
    return Markup(
        f'<img src="{emoji_cdn_url(emoji_id, animated)}" '
        f'class="nero-check-icon" alt="{CHECK_EMOJI_FALLBACK}" '
        f'width="16" height="16" loading="lazy" '
        f'onerror="this.outerHTML=\'{CHECK_EMOJI_FALLBACK}\'">'
    )


def check_icon_css() -> Markup:
    """Sizing for the check image.

    Inline `width`/`height` attributes already keep the layout stable; this
    only aligns it with the surrounding text baseline, mirroring the
    existing `.eb-inline-emoji` rule the Embed Builder uses for the same
    job.
    """
    return Markup(
        "<style>"
        ".nero-check-icon{vertical-align:-2px;display:inline-block;}"
        "</style>"
    )
