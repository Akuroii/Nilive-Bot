import aiosqlite
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# TITLE ENGINE — Wallet pass
#
# A Title is a purely cosmetic, DB-only label ("The Silent One"). It is
# deliberately NOT a Discord role and shares nothing with
# utils/equip_engine.py beyond the shape of its API, because the locked
# project decision is that the Title equip slot is INDEPENDENT from the
# Discord Role equip slot: a member can wear one role item AND one title
# at the same time, and equipping one must never disturb the other.
#
# That independence is exactly why this is a separate table rather than
# a second column on equipped_roles — equipped_roles' PK is
# (guild_id, user_id) with a NOT NULL role_id, so a title could only
# have shared that row by either faking a role_id or relaxing a
# constraint that currently protects the single-role invariant.
# equipped_titles mirrors its shape (one row per member, PK enforces
# "at most one equipped title") without touching it.
#
# Ownership is always inventory_items (item_type='title', quantity>0) —
# equipped_titles only ever answers "which of the owned ones is worn".
# equip_title() re-validates ownership on every call, so a title that
# was traded away or admin-removed can't stay equipped through a stale
# row.
#
# Read by utils/rank_card_data.py (get_equipped_title) so the Rank Card
# can render it later. No rendering decisions are made here.
# ═══════════════════════════════════════════════════════════════════════

TITLE_ITEM_TYPE = "title"


async def get_equipped_title(guild_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT item_name, equipped_at
            FROM equipped_titles WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"item_name": row[0], "equipped_at": row[1]}


async def equip_title(guild_id: int, user_id: int, item_name: str) -> dict:
    """
    Equips an owned title. Never raises for user-facing failures; every
    rejection returns {"success": False, "error": "..."} so callers can
    surface the message straight to Discord.
    """
    from utils.inventory import get_inventory

    items = await get_inventory(guild_id, user_id, include_empty=False)
    owned = next((it for it in items if it["item_name"] == item_name), None)
    if not owned:
        return {"success": False, "error": f"You don't own **{item_name}**."}
    if owned["item_type"] != TITLE_ITEM_TYPE:
        return {"success": False,
                "error": f"**{item_name}** isn't a title."}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO equipped_titles
                (guild_id, user_id, item_name, equipped_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                item_name   = excluded.item_name,
                equipped_at = CURRENT_TIMESTAMP
        """, (guild_id, user_id, item_name))
        await db.commit()

    return {"success": True, "item_name": item_name}


async def unequip_title(guild_id: int, user_id: int,
                        item_name: str | None = None) -> dict:
    """
    Clears the member's equipped title. When item_name is given the
    delete is scoped to it, so a stale button on an old ephemeral view
    ("Unequip The Silent One") can't clear a *different* title the
    member equipped in the meantime.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if item_name is None:
            cursor = await db.execute(
                "DELETE FROM equipped_titles WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
        else:
            cursor = await db.execute(
                "DELETE FROM equipped_titles "
                "WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, item_name))
        await db.commit()
        removed = cursor.rowcount

    if not removed:
        return {"success": False, "error": "That title isn't equipped."}
    return {"success": True, "item_name": item_name}


async def cleanup_unowned_title(guild_id: int, user_id: int):
    """
    Drops the equipped_titles row if the member no longer owns the
    title it points at (traded away, admin-removed). Cheap enough to
    call opportunistically on read paths; mirrors
    equip_engine.cleanup_expired_role_item()'s "bookkeeping catches up
    after the fact" approach rather than adding FK cascades to a table
    that has never had them.
    """
    current = await get_equipped_title(guild_id, user_id)
    if not current:
        return

    from utils.inventory import has_item
    if not await has_item(guild_id, user_id, current["item_name"], 1):
        await unequip_title(guild_id, user_id, current["item_name"])
