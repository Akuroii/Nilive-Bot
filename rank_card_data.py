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
                              member=None,
                              max_grid_items: int = 12) -> dict:
    """
    member: the live discord.Member, when the caller has one (e.g. the
    /rank command). Supplies username/avatar/join-date/booster-status --
    none of that lives in the DB. When omitted, those fields come back
    None and is_booster is treated as False (permanent prestige only).
    """
    from utils.xp_calculator import xp_progress
    from utils.inventory import get_inventory
    from utils.economy_safe import get_balance
    from utils.equip_engine import get_equipped
    from utils.item_catalog import get_catalog_entry, item_sort_key
    from utils.currency import get_currency_config
    from utils.prestige import get_effective_prestige, is_booster as _is_booster
    from cogs.minigames import get_user_win_count

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT xp, level, prestige FROM levels
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        level_row = await cursor.fetchone()

        xp_val       = level_row[0] if level_row else 0
        raw_prestige = (level_row[2] if level_row else 0) or 0

        # Finalized Prestige: legacy levels.prestige above the permanent max
        # is treated as the max tier (V) for ranking/display, never rewritten.
        from utils.prestige import MAX_PERMANENT_TIER
        prestige_val = min(raw_prestige, MAX_PERMANENT_TIER)

        rank_cursor = await db.execute("""
            SELECT COUNT(*) FROM levels
            WHERE guild_id=? AND
                (MIN(prestige, ?) > ? OR
                 (MIN(prestige, ?) = ? AND xp > ?))
        """, (guild_id, MAX_PERMANENT_TIER,
              prestige_val, MAX_PERMANENT_TIER, prestige_val, xp_val))
        rank = (await rank_cursor.fetchone())[0] + 1

        # Same population the rank COUNT above draws from (all `levels`
        # rows for this guild) so rank and percentile can never disagree
        # over a different filter.
        total_cursor = await db.execute(
            "SELECT COUNT(*) FROM levels WHERE guild_id=?", (guild_id,))
        total_ranked = (await total_cursor.fetchone())[0]

        totals_cursor = await db.execute("""
            SELECT COALESCE(SUM(messages_count), 0),
                   COALESCE(SUM(voice_minutes), 0)
            FROM activity_stats WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        totals = await totals_cursor.fetchone()

    lvl, current_xp, needed_xp = xp_progress(xp_val)
    percentile = (rank / total_ranked * 100) if total_ranked else 0.0

    balance  = await get_balance(guild_id, user_id, currency="balance")
    diamonds = await get_balance(guild_id, user_id, currency="diamonds")
    currency = await get_currency_config(guild_id)

    # Booster Prestige (VI) only exists via an active boost -- there is no
    # "ordinary Prestige VI" in this system (purchase_prestige caps at V),
    # so effective_prestige == BOOSTER_TIER already implies is_booster.
    booster_flag = _is_booster(member) if member is not None else False
    effective_prestige = await get_effective_prestige(
        guild_id, user_id, is_booster=booster_flag)

    equipped = await get_equipped(guild_id, user_id)

    # Wallet pass: the equipped TITLE is exposed here too, so the future
    # card renderer can read it from the same single aggregation call
    # instead of learning about a second table. Read-only — nothing in
    # this module decides how (or whether) a title is drawn. Independent
    # from equipped_role above by design: a member can wear one of each.
    from utils.title_engine import get_equipped_title
    equipped_title = await get_equipped_title(guild_id, user_id)

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
    # Actual owned count (role/temp_role excluded, matches what the grid
    # draws from) -- available for internal use; not required to be shown.
    owned_count = len(grid_candidates)
    grid_items = grid_candidates[:max_grid_items]

    win_count = await get_user_win_count(guild_id, user_id)

    return {
        "user_id": user_id, "guild_id": guild_id,
        "username": member.display_name if member is not None else None,
        "avatar_url": str(member.display_avatar.url) if member is not None else None,
        "member_since": member.joined_at if member is not None else None,
        "level": lvl, "xp_total": xp_val,
        "xp_current": current_xp, "xp_needed": needed_xp,
        "prestige": prestige_val,              # permanent tier, 0-5
        "effective_prestige": effective_prestige,  # 6 (Booster VI) when active
        "is_booster": booster_flag,
        "rank": rank, "percentile": percentile, "total_ranked": total_ranked,
        "balance": balance, "diamonds": diamonds, "currency": currency,
        "messages_count": totals[0] if totals else 0,
        "voice_minutes": totals[1] if totals else 0,
        "minigame_wins": win_count,
        "equipped_role": equipped,
        "equipped_title": equipped_title,
        "inventory_grid": grid_items,
        "owned_count": owned_count,
    }
