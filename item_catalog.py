import aiosqlite
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# ITEM CATALOG — Rank Card foundation (pass 1: schema + backend)
#
# Source-agnostic display metadata for an item, keyed by
# (guild_id, item_name) — NOT by shop_items.id. Matches how
# inventory_items already references items purely by name (a
# purchased item's inventory row already survives its shop listing
# being edited/deleted); this follows the same loose coupling so the
# same item_name works whether it came from the shop, an event, a
# minigame tier, or a mission reward.
#
# Auto-populated from dashboard/api/economy_shop.py whenever a shop
# item is saved. Items granted ONLY via events/minigames/missions have
# no row here yet (no admin UI for that this pass, per Dark: "not
# now") — get_catalog_entry() below returns a safe default
# (rarity='common', no icon, no value) rather than erroring, so the
# future card renderer never has to special-case a missing row.
#
# RARITY is the PRIMARY sort key for the future /rank card's item
# grid (locked with Dark) — value (diamonds > coins, then amount) is
# only a tiebreaker within the same rarity. This is what lets a free
# Legendary minigame drop outrank a boring 50-coin shop item, which a
# price-only sort could never do.
# ═══════════════════════════════════════════════════════════════════════

RARITY_ORDER = ["common", "rare", "epic", "legendary", "mythical", "secret"]
RARITY_RANK = {r: i for i, r in enumerate(RARITY_ORDER)}

# Locked with Dark — gradient stops per tier, kept consistent with the
# site's purple/violet palette rather than generic game-UI colors.
# Mythical and Secret are intentionally 3-stop gradients; every other
# tier is 2-stop. Used by the future /rank card Pillow renderer for
# tile borders/badges — stored here now (rather than re-asked for
# later) since Dark already gave exact values.
RARITY_COLORS = {
    "common":    ["#777777", "#B8B8B8"],
    "rare":      ["#1769D1", "#55B7FF"],
    "epic":      ["#6A22C9", "#B45CFF"],
    "legendary": ["#B56A00", "#FFD76A"],
    "mythical":  ["#D900A8", "#FF4FD8", "#65E8FF"],
    "secret":    ["#3A050B", "#8B0F1F", "#D21F35"],
}

_CURRENCY_RANK = {"diamonds": 2, "balance": 1}


def is_valid_rarity(rarity: str) -> bool:
    return rarity in RARITY_RANK


def rarity_rank(rarity: str) -> int:
    """Unknown/missing rarity ranks as Common (0), never errors."""
    return RARITY_RANK.get(rarity, 0)


def item_sort_key(rarity: str, value_currency: str | None,
                   value_amount: int | None) -> tuple:
    """
    Higher = "more valuable". Rarity first, then currency tier
    (diamonds > coins > none), then amount — all descending. Use with
    sorted(items, key=lambda it: item_sort_key(...), reverse=True).
    """
    return (
        rarity_rank(rarity),
        _CURRENCY_RANK.get(value_currency, 0),
        value_amount or 0,
    )


async def get_catalog_entry(guild_id: int, item_name: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT icon_url, rarity, value_currency, value_amount
            FROM item_catalog WHERE guild_id=? AND item_name=?
        """, (guild_id, item_name))
        row = await cursor.fetchone()
    if not row:
        return {"icon_url": None, "rarity": "common",
                "value_currency": None, "value_amount": None}
    return {
        "icon_url": row[0], "rarity": row[1] or "common",
        "value_currency": row[2], "value_amount": row[3],
    }


async def upsert_catalog_entry(guild_id: int, item_name: str,
                                icon_url: str = None, rarity: str = None,
                                value_currency: str = None,
                                value_amount: int = None):
    """
    icon_url/rarity use COALESCE against the existing row — a caller
    that doesn't know an item's icon (e.g. a future minigame-tier
    save that only knows value) shouldn't be able to silently wipe an
    icon/rarity an admin already set elsewhere. value_currency/
    value_amount are written as-is (None is meaningful here — "this
    item has no price", distinct from "leave the existing price
    alone") since today the only writer (shop item save) always knows
    the current, correct price.
    """
    if rarity and not is_valid_rarity(rarity):
        raise ValueError(f"Unknown rarity: {rarity!r}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO item_catalog
                (guild_id, item_name, icon_url, rarity,
                 value_currency, value_amount, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, item_name) DO UPDATE SET
                icon_url       = COALESCE(excluded.icon_url, item_catalog.icon_url),
                rarity         = COALESCE(excluded.rarity, item_catalog.rarity),
                value_currency = excluded.value_currency,
                value_amount   = excluded.value_amount,
                updated_at     = CURRENT_TIMESTAMP
        """, (guild_id, item_name, icon_url, rarity or "common",
              value_currency, value_amount))
        await db.commit()
