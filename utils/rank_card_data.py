import aiosqlite
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# RANK CARD DATA — Rank Card foundation (pass 1: schema + backend)
#
# Pure data aggregation, no image work, no Discord API calls beyond
# what's already cached on the bot. Built now so the NEXT session
# (the actual Pillow card build) only has to consume this dict and
# draw — every table/engine it needs (item_catalog, equip_engine,
# minigame win count) already exists by the time that session starts.
#
# inventory_grid is sorted rarity-first, value-second (item_catalog.
# item_sort_key), and excludes role/temp_role items — those live in
# their own equipped_role slot, per Dark's locked design: the rank
# card and the equip system are visually independent, but the card
# still needs to know what's equipped to render that slot.
# ═══════════════════════════════════════════════════════════════════════


async def get_rank_card_data(guild_id: int, user_id: int,
                              max_grid_items: int = 9) -> dict:
    from utils.xp_calculator import xp_progress
    from utils.inventory import get_inventory
    from utils.economy_safe import get_balance
    from utils.equip_engine import get_equipped
    from utils.item_catalog import get_catalog_entry, item_sort_key
    from cogs.minigames import get_user_win_count

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT xp, level, prestige FROM levels
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        level_row = await cursor.fetchone()

        xp_val       = level_row[0] if level_row else 0
        prestige_val = (level_row[2] if level_row else 0) or 0

        rank_cursor = await db.execute("""
            SELECT COUNT(*) FROM levels
            WHERE guild_id=? AND
                (prestige > ? OR (prestige = ? AND xp > ?))
        """, (guild_id, prestige_val, prestige_val, xp_val))
        rank = (await rank_cursor.fetchone())[0] + 1

        totals_cursor = await db.execute("""
            SELECT COALESCE(SUM(messages_count), 0),
                   COALESCE(SUM(voice_minutes), 0)
            FROM activity_stats WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        totals = await totals_cursor.fetchone()

    lvl, current_xp, needed_xp = xp_progress(xp_val)

    balance  = await get_balance(guild_id, user_id, currency="balance")
    diamonds = await get_balance(guild_id, user_id, currency="diamonds")

    equipped = await get_equipped(guild_id, user_id)

    items = await get_inventory(guild_id, user_id, include_empty=False)
    grid_candidates = []
    for it in items:
        if it["item_type"] in ("role", "temp_role"):
            continue
        catalog = await get_catalog_entry(guild_id, it["item_name"])
        grid_candidates.append({
            **it,
            "icon_url": catalog["icon_url"],
            "rarity": catalog["rarity"],
            "value_currency": catalog["value_currency"],
            "value_amount": catalog["value_amount"],
            "_sort": item_sort_key(
                catalog["rarity"], catalog["value_currency"],
                catalog["value_amount"]),
        })
    grid_candidates.sort(key=lambda x: x["_sort"], reverse=True)
    grid_items = grid_candidates[:max_grid_items]

    win_count = await get_user_win_count(guild_id, user_id)

    return {
        "user_id": user_id, "guild_id": guild_id,
        "level": lvl, "xp_total": xp_val,
        "xp_current": current_xp, "xp_needed": needed_xp,
        "prestige": prestige_val, "rank": rank,
        "balance": balance, "diamonds": diamonds,
        "messages_count": totals[0] if totals else 0,
        "voice_minutes": totals[1] if totals else 0,
        "minigame_wins": win_count,
        "equipped_role": equipped,
        "inventory_grid": grid_items,
    }
