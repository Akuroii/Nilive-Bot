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


_TITLE_UNSPECIFIED = object()


async def equip_title(guild_id: int, user_id: int, item_name: str, *,
                      entry_key: str | None = None,
                      expected_title=_TITLE_UNSPECIFIED) -> dict:
    """Validate the permanent title and equip it in one write transaction."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT item_name FROM equipped_titles WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            current = await cursor.fetchone()
            if expected_title is not _TITLE_UNSPECIFIED and (current[0] if current else None) != expected_title:
                await db.rollback()
                return {"success": False, "error": "The equipped title changed. Open the item again."}
            cursor = await db.execute(
                "SELECT id, item_type, quantity FROM inventory_items "
                "WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, item_name))
            owned = await cursor.fetchone()
            if (not owned or int(owned[2]) <= 0 or owned[1] != TITLE_ITEM_TYPE
                    or (entry_key is not None and entry_key != f"i:{owned[0]}")):
                await db.rollback()
                return {"success": False, "error": "That title is no longer owned."}
            await db.execute("""
                INSERT INTO equipped_titles (guild_id, user_id, item_name, equipped_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    item_name=excluded.item_name, equipped_at=CURRENT_TIMESTAMP
            """, (guild_id, user_id, item_name))
            if not current or current[0] != item_name:
                from utils.inventory_activity import record_activity
                await record_activity(db, guild_id=guild_id, user_id=user_id,
                                      event_type="equipped", item_name=item_name,
                                      item_type=TITLE_ITEM_TYPE, quantity=1, source="equipment")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return {"success": True, "item_name": item_name}


async def unequip_title(guild_id: int, user_id: int,
                        item_name: str | None = None, *,
                        entry_key: str | None = None) -> dict:
    """
    Clears the member's equipped title. When item_name is given the
    delete is scoped to it, so a stale button on an old ephemeral view
    ("Unequip The Silent One") can't clear a *different* title the
    member equipped in the meantime.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT item_name FROM equipped_titles WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        prior = await cursor.fetchone()
        if entry_key is not None:
            cursor = await db.execute(
                "SELECT id FROM inventory_items WHERE guild_id=? AND user_id=? "
                "AND item_name=? AND item_type='title' AND quantity>0",
                (guild_id, user_id, item_name))
            row = await cursor.fetchone()
            if not row or entry_key != f"i:{row[0]}":
                await db.rollback()
                return {"success": False, "error": "That title is no longer owned."}
        if item_name is None:
            cursor = await db.execute(
                "DELETE FROM equipped_titles WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
        else:
            cursor = await db.execute(
                "DELETE FROM equipped_titles "
                "WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, item_name))
        removed = cursor.rowcount
        if removed and prior:
            from utils.inventory_activity import record_activity
            await record_activity(db, guild_id=guild_id, user_id=user_id,
                                  event_type="unequipped", item_name=prior[0],
                                  item_type=TITLE_ITEM_TYPE, quantity=1, source="equipment")
        await db.commit()

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
