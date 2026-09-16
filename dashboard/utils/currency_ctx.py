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

Two shapes, deliberately:

  * `context()` -> Jinja variables. `currency` is the resolved config
    (`{"coins": {"key", "name", "emoji"}, "diamonds": {...}}`), the same
    object cogs receive, so templates and cogs read it identically.
    `currency_defaults` is the flat `{coin_name, coin_emoji, ...}` mapping
    the Economy form uses for its placeholders.
  * `resolved(guild_id)` -> just the config dict, for hand-built HTML
    partials in `dashboard/api/*.py`.

Registered as a Flask context processor (dashboard/app.py) so every page gets
it without each route passing it: a new dashboard page cannot forget the
guild's currency, because there is nothing to remember.
"""

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


def context(guild_id=None) -> dict:
    """Jinja context fragment injected into every template render."""
    return {
        "currency": resolved(guild_id),
        "currency_defaults": defaults_flat(),
    }
