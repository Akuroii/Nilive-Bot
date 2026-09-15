"""
Unified currency display configuration.

Everywhere the bot renders a coin or diamond amount (wallet, economy,
shop, trade, leveling, minigames, receipts) must pull names and emojis
from here so admins can rename/re-icon currencies from the dashboard
without a code change.

Storage: guild_settings (General settings), NOT economy-specific config.
Reason: currency is cross-cutting — leveling rewards, minigames,
missions, trade, shop, prestige and the dashboard leaderboards all
display it, so owning it inside the economy module would force every
other system to import an economy namespace. guild_settings already
hosted currency_name + an unused currency_emoji_id column; this helper
is the single reader/writer for the full set now.

Defaults are the historical look (Coins / 🪙 / Diamonds / 💎) so a
guild that never configures anything gets the previous UI unchanged.
"""
from __future__ import annotations

import aiosqlite
from database import DB_PATH


DEFAULT_COIN_NAME = "Coins"
DEFAULT_COIN_EMOJI = "🪙"
DEFAULT_DIAMOND_NAME = "Diamonds"
DEFAULT_DIAMOND_EMOJI = "💎"


# Internal column keys used by economy_safe / ledger. Only these two
# real currency columns exist on the economy table; "xp" is tracked by
# ledger but isn't a wallet currency and has no icon.
COIN_CURRENCY = "balance"
DIAMOND_CURRENCY = "diamonds"


async def get_currency_config(guild_id: int) -> dict:
    """
    Returns:
        {
            "coins":    {"key": "balance",  "name": "...", "emoji": "..."},
            "diamonds": {"key": "diamonds", "name": "...", "emoji": "..."},
        }

    Missing config rows or NULL cells fall back to the defaults above
    so a fresh guild is never missing an icon or name.
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


def for_currency(config: dict, currency: str) -> dict:
    """Look up one currency's {name, emoji} by its internal key ('balance'/'diamonds')."""
    if currency == DIAMOND_CURRENCY:
        return config["diamonds"]
    return config["coins"]


def coin_name(config: dict) -> str:
    return config["coins"]["name"]


def coin_emoji(config: dict) -> str:
    return config["coins"]["emoji"]


def diamond_name(config: dict) -> str:
    return config["diamonds"]["name"]


def diamond_emoji(config: dict) -> str:
    return config["diamonds"]["emoji"]
