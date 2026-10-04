"""Currency display config for dashboard templates and API partials.

Economy owns the currency configuration (see `utils/currency.py`); the
dashboard only ever CONSUMES it. This module is the dashboard-side adapter:
the single place that knows how to (a) read the config for the current
session's guild, (b) hand it to Jinja, and (c) fall back to the shipped
defaults when there is no session/guild — or when the read fails, because a
template must still render.

Nothing here defines a currency name or emoji. The defaults come from
`utils.currency` like everywhere else, so a rename in the Economy form is
the only change ever needed.

Three shapes, deliberately:

  * `context()` -> Jinja variables. `currency` is the resolved config
    (`{"coins": {"key", "name", "emoji"}, "diamonds": {...}}`), the same
    object cogs receive, so templates and cogs read it identically.
    `currency_defaults` is the flat `{coin_name, coin_emoji, ...}` mapping
    the Economy form uses for its placeholders. `currency_icon` /
    `currency_icon_css` render a configured icon as browser-safe HTML.
  * `resolved(guild_id)` -> just the config dict, for hand-built HTML
    partials in `dashboard/api/*.py`.
  * `icon_html(emoji)` -> the SAME icon as Markup, for those partials and
    for any other f-string HTML builder.

Registered as a Flask context processor (dashboard/app.py) so every page gets
it without each route passing it: a new dashboard page cannot forget the
guild's currency, because there is nothing to remember.

Why the icon needs its own renderer
-----------------------------------
A configured currency icon may be a Discord custom emoji — `<:name:id>` or
the animated `<a:name:id>`. That markup is a Discord *message* feature; a
browser has no idea what it means, and both ways of dropping it into a page
are wrong:

  * escaped (what Jinja autoescape does) the page shows the literal text
    `<a:gold:1549…>`, and
  * unescaped (what an f-string partial does) the HTML tokenizer reads
    `<a:gold:1549…>` as the start of an `<a>` tag and swallows it, so the
    icon silently disappears.

So a custom icon is rendered as the image Discord's public emoji CDN serves
by ID — the exact mechanism `dashboard/utils/check_icon.py` already uses for
the success indicator. Unicode emoji (🪙, 💎, 🌙, any script) are plain text
that a browser renders natively, so those pass through untouched. `icon_html`
is the one place that knows the difference; every currency surface in the
dashboard calls it instead of interpolating `currency.*.emoji` directly.
"""

from markupsafe import Markup, escape

from dashboard.utils.async_utils import run_async


def _default_config() -> dict:
    """The shipped defaults in resolved-config shape.

    Used only when there is no guild in the session (login/server-select
    pages) or the read failed — never as a second definition of the
    defaults themselves.
    """
    from utils.currency import (
        DEFAULT_COIN_NAME, DEFAULT_COIN_EMOJI,
        DEFAULT_DIAMOND_NAME, DEFAULT_DIAMOND_EMOJI,
    )
    return {
        "coins": {"key": "balance", "name": DEFAULT_COIN_NAME,
                  "emoji": DEFAULT_COIN_EMOJI},
        "diamonds": {"key": "diamonds", "name": DEFAULT_DIAMOND_NAME,
                     "emoji": DEFAULT_DIAMOND_EMOJI},
    }


def resolved(guild_id=None) -> dict:
    """This guild's resolved currency config, or the defaults.

    Never raises: a broken settings row must not take a dashboard page
    down, and the fallback is exactly what `get_currency_config()` itself
    returns for an unconfigured guild.
    """
    if not guild_id:
        return _default_config()
    try:
        from utils.currency import get_currency_config
        return run_async(get_currency_config(guild_id))
    except Exception as e:  # pragma: no cover - defensive
        print(f"[DASHBOARD] currency config read failed for guild "
              f"{guild_id}: {e}")
        return _default_config()


def defaults_flat() -> dict:
    """`{coin_name, coin_emoji, diamond_name, diamond_emoji}` — the flat
    shape the Economy currency form binds its placeholders to."""
    cur = _default_config()
    return {
        "coin_name": cur["coins"]["name"],
        "coin_emoji": cur["coins"]["emoji"],
        "diamond_name": cur["diamonds"]["name"],
        "diamond_emoji": cur["diamonds"]["emoji"],
    }


# ── Browser-safe rendering of the configured icon ───────────────────────
#
# Scoped to CURRENCY icons only — the success/check indicator has its own
# equivalent in dashboard/utils/check_icon.py, and the two are deliberately
# separate (different emoji, different sources, different fallbacks). What
# they share is the primitive: utils/emoji.emoji_cdn_url, which serves any
# emoji image by ID with no auth and no bot membership, so a preview can
# never be fooled into showing nothing for a perfectly valid ID.

def icon_html(emoji, fallback: str = "") -> Markup:
    """A configured currency emoji as HTML a browser can actually render.

    * a unicode emoji / any literal text -> that text, escaped. A browser
      renders 🪙 and قمر natively; there is nothing to convert.
    * `<:name:id>` / `<a:name:id>`       -> an `<img>` on Discord's emoji
      CDN. The `animated` flag from the token picks the `.gif` (animated)
      or `.png` (static) asset, so animated custom emoji animate here too.
    * empty                             -> `fallback`, escaped.

    The `<img>` carries `onerror` swapping in `fallback`, so an icon that
    was deleted from Discord leaves neither a broken-image glyph nor the
    raw token text behind. `alt` is empty on purpose: the currency NAME
    always sits next to the icon in these surfaces, so the image is
    decorative and an alt string would only be read out twice.

    Returns Markup (already-safe HTML) so f-string partials can interpolate
    it directly — the same contract `render_user_identity_html` uses.
    """
    raw = (emoji or "").strip()
    if not raw:
        return Markup(escape(fallback or ""))

    from utils.emoji import emoji_cdn_url, is_custom_emoji_token, parse_emoji_input

    # Not a Discord token -> plain text (unicode emoji, or literal text an
    # admin typed). Escaped so nothing admin-entered can become markup.
    if not is_custom_emoji_token(raw):
        return Markup(escape(raw))

    parsed = parse_emoji_input(raw)
    if not parsed:                       # defensive: cannot happen for a token
        return Markup(escape(fallback or ""))
    emoji_id, _name, animated = parsed
    return Markup(
        f'<img src="{emoji_cdn_url(emoji_id, animated)}" '
        f'class="nero-currency-icon" alt="" '
        f'width="18" height="18" loading="lazy" '
        f'onerror="this.outerHTML=\'{escape(fallback or "")}\'">'
    )


def icon_css() -> Markup:
    """Baseline alignment for the icon image.

    Inline `width`/`height` already keep the layout stable (no reflow when
    the image lands); this only sits the glyph on the text baseline, so a
    custom icon lines up exactly where the unicode 🪙/💎 it replaced used
    to. No sizing, colour or spacing changes — same footprint as before.
    """
    return Markup(
        "<style>"
        ".nero-currency-icon{vertical-align:-3px;display:inline-block;"
        "object-fit:contain;}"
        "</style>"
    )


def icon_text(emoji) -> str:
    """The configured icon as PLAIN TEXT, for places HTML cannot help with.

    `<option>` elements (the currency pickers on the Ledger, Leveling,
    Missions, Tag Missions and Tag Partners pages) may contain text only —
    an `<img>` inside one is dropped by every browser, so `icon_html` would
    leave the row with no icon at all there. This is the text twin:

    * a unicode emoji / literal text -> itself (a browser renders it)
    * `<:name:id>` / `<a:name:id>`   -> the emoji's NAME (`gold`), never the
      raw token, so no Discord markup ever reaches the page
    * empty                          -> ""

    Deliberately returns a plain `str`, not Markup: callers put it inside
    `{{ }}`, where Jinja escapes it, and a name is user-chosen text.
    """
    raw = (emoji or "").strip()
    if not raw:
        return ""

    from utils.emoji import is_custom_emoji_token, parse_emoji_input

    if not is_custom_emoji_token(raw):
        return raw
    parsed = parse_emoji_input(raw)
    if not parsed:
        return ""
    name = parsed[1]
    # An ID-only emoji is stored under the `_` placeholder name (see
    # utils/emoji.normalize_currency_emoji). Showing `_` in a dropdown is
    # noise, so treat it as "no icon" — the currency NAME next to it
    # already identifies the row.
    return "" if name == "_" else name


def context(guild_id=None) -> dict:
    """Jinja context fragment injected into every template render."""
    return {
        "currency": resolved(guild_id),
        "currency_defaults": defaults_flat(),
        # Callable, not a value: a template passes the specific icon it is
        # rendering (`currency_icon(currency.coins.emoji)`), so one global
        # serves every currency on the page.
        "currency_icon": icon_html,
        # Text-only twin for `<option>` elements, which cannot hold an image.
        "currency_icon_text": icon_text,
        "currency_icon_css": icon_css(),
    }
