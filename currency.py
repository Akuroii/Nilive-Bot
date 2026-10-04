"""
Unified currency display configuration — THE single source of truth.

Everywhere the bot renders a coin or diamond amount or label (wallet,
economy, shop, inventory, trade, leveling, minigames, missions, events,
leaderboards, receipts, dashboard pages and partials) must resolve names
and emojis through this module. Nothing else may read or write the four
`guild_settings` currency columns, and nothing else may declare a default
currency name or emoji.

Storage: the four columns live in `guild_settings`, but their OWNER is the
Economy page (`/economy` → Currency tab, `POST /api/economy/currency`).

    Economy owns the currency configuration; the rest of the project
    consumes it.

That is deliberate and matches the precedent already set by
`guild_settings.diamond_exchange_rate`, which likewise lives in
guild_settings while being read/written only by the economy surface
(utils/economy_safe.get_guild_exchange_rate + the Economy page). Keeping
the columns in an existing table avoids a migration for every deployed
guild, and keeping the OWNER in the economy surface is what makes Economy
the source of truth — not which table the bytes happen to sit in.

Internal keys vs display values
-------------------------------
The internal currency keys are `balance` and `diamonds` (the two real
columns on the `economy` table, and the values stored in
transaction_ledger.currency / leveling_currency_rewards.currency /
item_catalog.value_currency / purchase_history.currency_paid). Reward
definitions store the TYPE key `coins` / `diamonds`.

Those keys are STABLE and must never be renamed to match a display name.
A guild that names its primary currency "Moon" still stores `balance` in
the ledger, still stores `coins` on its mission reward rows, and still
migrates nothing. Renaming is therefore a pure presentation change: no
stored row needs rewriting, and changing a name or emoji can never
invalidate an existing mission, reward, purchase or ledger entry — the
label is computed at render time, every time.

Defaults
--------
A guild that has configured nothing gets the historical look. Each of the
four values falls back INDEPENDENTLY, so configuring only a name, only an
emoji, or neither is valid — nothing requires both fields, and an empty
string is always equivalent to "unset". Callers never need their own
fallback: this module always returns a usable name and emoji.
"""
from __future__ import annotations

import aiosqlite
from database import DB_PATH


# ── Defaults — the ONLY place these values exist ────────────────────────
# database.py's schema intentionally declares no DEFAULT for the four
# columns, so "unset" is stored as NULL and resolves here. Anything that
# needs a fallback value imports it from this module rather than repeating
# the literal.
DEFAULT_COIN_NAME = "Coins"
DEFAULT_COIN_EMOJI = "🪙"
DEFAULT_DIAMOND_NAME = "Diamonds"
DEFAULT_DIAMOND_EMOJI = "💎"


# Internal column keys used by economy_safe / ledger. Only these two real
# currency columns exist on the economy table; "xp" is tracked by ledger
# but isn't a wallet currency and has no icon.
COIN_CURRENCY = "balance"
DIAMOND_CURRENCY = "diamonds"


# Mapping from a stored DEFINTION key ("coins", as written on mission /
# event / minigame reward rows) to its economy column key ("balance").
# Reward rows have always used "coins" while the wallet column is
# "balance"; this is the one place that translation is written down.
_DEFINITION_KEY_TO_COLUMN = {
    "coins": COIN_CURRENCY,
    "balance": COIN_CURRENCY,
    "diamonds": DIAMOND_CURRENCY,
}


def to_column_key(currency: str) -> str:
    """Normalize any currency identifier (definition key or column key) to
    the stable column key used by economy_safe / ledger.

    Accepts 'coins' (reward-definition spelling) and 'balance' (economy
    column spelling) alike, so callers never have to care which of the two
    vocabularies they are holding. Unknown values fall back to the primary
    currency — the same lenient posture the rest of this module takes.
    """
    return _DEFINITION_KEY_TO_COLUMN.get(currency, COIN_CURRENCY)


async def get_currency_config(guild_id: int) -> dict:
    """
    Returns the resolved display configuration for one guild:

        {
            "coins":    {"key": "balance",  "name": "...", "emoji": "..."},
            "diamonds": {"key": "diamonds", "name": "...", "emoji": "..."},
        }

    Every value is guaranteed non-empty: a missing guild_settings row, a
    NULL cell, and a whitespace-only cell all fall back to the defaults
    above, each field independently.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT currency_name, coin_emoji_id, diamond_name, "
            "diamond_emoji_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,))
        row = await cursor.fetchone()

    if row:
        coin_name = (row[0] or "").strip() or DEFAULT_COIN_NAME
        coin_emoji = (row[1] or "").strip() or DEFAULT_COIN_EMOJI
        diamond_name = (row[2] or "").strip() or DEFAULT_DIAMOND_NAME
        diamond_emoji = (row[3] or "").strip() or DEFAULT_DIAMOND_EMOJI
    else:
        coin_name, coin_emoji = DEFAULT_COIN_NAME, DEFAULT_COIN_EMOJI
        diamond_name, diamond_emoji = DEFAULT_DIAMOND_NAME, DEFAULT_DIAMOND_EMOJI

    return {
        "coins": {
            "key": COIN_CURRENCY,
            "name": coin_name,
            "emoji": coin_emoji,
        },
        "diamonds": {
            "key": DIAMOND_CURRENCY,
            "name": diamond_name,
            "emoji": diamond_emoji,
        },
    }


# Raw column -> config bucket, for set/get below. Kept next to the reader
# so the four column names appear exactly once in the codebase.
_COLUMN_TO_BUCKET = {
    "currency_name": "coins",
    "coin_emoji_id": "coins",
    "diamond_name": "diamonds",
    "diamond_emoji_id": "diamonds",
}


async def get_currency_config_raw(guild_id: int) -> dict:
    """The stored values exactly as they sit in the database, with no
    default substitution — what the Economy form pre-fills with.

    A field the owner has never set (or deliberately cleared) comes back as
    "" rather than the default, so the form shows an empty box and the
    "leave blank for the default" affordance stays truthful. Rendered
    output must use get_currency_config() instead; this exists purely for
    editing UI.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT currency_name, coin_emoji_id, diamond_name, "
            "diamond_emoji_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,))
        row = await cursor.fetchone()
    if not row:
        return {"currency_name": "", "coin_emoji_id": "",
                "diamond_name": "", "diamond_emoji_id": ""}
    return {
        "currency_name": (row[0] or "").strip(),
        "coin_emoji_id": (row[1] or "").strip(),
        "diamond_name": (row[2] or "").strip(),
        "diamond_emoji_id": (row[3] or "").strip(),
    }


async def set_currency_config(guild_id: int, *,
                              currency_name: str | None = None,
                              coin_emoji_id: str | None = None,
                              diamond_name: str | None = None,
                              diamond_emoji_id: str | None = None) -> dict:
    """THE only writer of the four currency display columns.

    Semantics that matter:

    * `None` means "do not touch this field". Only the fields the caller
      explicitly passes are written, so a partial form save cannot blank out
      a field it didn't render.
    * `""` (or whitespace) means "revert to the default" and is stored as
      NULL — never as a copy of the default string. If the default were
      written into the row, a later change to DEFAULT_* in this module
      would fail to reach guilds that never configured anything, which
      would quietly break the whole point of having defaults in one place.
    * Values are stored as given otherwise. Emoji normalisation is applied
      by normalize_currency_emoji() before this is called (see the Economy
      API), so this function stays a straight writer.

    Returns the RESOLVED config (the same shape get_currency_config()
    returns), which is what the caller wants to echo back to the UI.
    """
    updates: dict[str, str | None] = {}
    for column, value in (
        ("currency_name", currency_name),
        ("coin_emoji_id", coin_emoji_id),
        ("diamond_name", diamond_name),
        ("diamond_emoji_id", diamond_emoji_id),
    ):
        if value is None:
            continue
        cleaned = str(value).strip()
        updates[column] = cleaned or None

    if updates:
        # guild_settings rows are created by the general-settings save and
        # by this upsert; a guild that has never saved anything still needs
        # a row before its currency can be stored, hence INSERT..ON CONFLICT
        # with the currency columns named explicitly.
        columns = ", ".join(updates)
        placeholders = ", ".join("?" for _ in updates)
        assignments = ", ".join(f"{c} = excluded.{c}" for c in updates)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"""
                INSERT INTO guild_settings (guild_id, {columns})
                VALUES (?, {placeholders})
                ON CONFLICT(guild_id) DO UPDATE SET
                    {assignments},
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, *updates.values()))
            await db.commit()

    return await get_currency_config(guild_id)


# ── Lookup + display helpers ────────────────────────────────────────────

def for_currency(config: dict, currency: str) -> dict:
    """Look up one currency's {key, name, emoji} by ANY currency identifier
    ('coins', 'balance', 'diamonds')."""
    bucket = ("diamonds" if to_column_key(currency) == DIAMOND_CURRENCY
              else "coins")
    return config[bucket]


def currency_name_for(config: dict, currency: str) -> str:
    """The configured display name for a currency identifier."""
    return for_currency(config, currency)["name"]


def currency_emoji_for(config: dict, currency: str) -> str:
    """The configured display emoji for a currency identifier."""
    return for_currency(config, currency)["emoji"]


def currency_label(config: dict, currency: str) -> str:
    """`emoji name` — e.g. '🪙 Coins', or '🌙 Moon' once configured.
    The one place a currency's inline label is composed."""
    info = for_currency(config, currency)
    return f"{info['emoji']} {info['name']}"


def currency_amount(config: dict, currency: str, amount, *,
                    label_first: bool = False,
                    emoji_last: bool = False) -> str:
    """A formatted amount with its currency name and emoji.

    Default order is `emoji 1,250 Name` (the wallet/shop/receipt look);
    label_first=True gives `Name 1,250` for prose and titles;
    emoji_last=True gives `1,250 Name emoji` (the Missions completion
    line, where the currency glyph trails the amount). Amounts are
    thousands-separated here so no caller re-implements the format.

    `amount` may be int-like or already-formatted text; anything that
    int() rejects is passed through unchanged rather than raising, because
    a display helper must never be able to break a command.
    """
    info = for_currency(config, currency)
    try:
        shown = f"{int(amount):,}"
    except (TypeError, ValueError):
        shown = str(amount)
    if label_first:
        return f"{info['name']} {shown}"
    if emoji_last:
        return f"{shown} {info['name']} {info['emoji']}"
    return f"{info['emoji']} {shown} {info['name']}"
