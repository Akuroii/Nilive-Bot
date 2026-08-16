import aiosqlite
import json
from database import DB_PATH


class InsufficientItems(Exception):
    pass


async def give_item(guild_id: int, user_id: int, item_name: str,
                     quantity: int = 1, item_type: str = "custom",
                     metadata: dict = None, source: str = "system") -> int:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    meta_json = json.dumps(metadata) if metadata else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("""
                INSERT INTO inventory_items
                    (guild_id, user_id, item_name, item_type,
                     quantity, metadata, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, user_id, item_name) DO UPDATE SET
                    quantity   = quantity + ?,
                    item_type  = excluded.item_type,
                    metadata   = COALESCE(excluded.metadata, inventory_items.metadata),
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, user_id, item_name, item_type,
                  quantity, meta_json, source, quantity))
            cursor = await db.execute("""
                SELECT quantity FROM inventory_items
                WHERE guild_id=? AND user_id=? AND item_name=?
            """, (guild_id, user_id, item_name))
            row = await cursor.fetchone()
            await db.commit()
            return row[0] if row else quantity
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def remove_item(guild_id: int, user_id: int, item_name: str,
                       quantity: int = 1) -> int:
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT quantity FROM inventory_items
                WHERE guild_id=? AND user_id=? AND item_name=?
            """, (guild_id, user_id, item_name))
            row = await cursor.fetchone()
            current = row[0] if row else 0
            if current < quantity:
                await db.execute("ROLLBACK")
                raise InsufficientItems(
                    f"Has {current}x {item_name}, needs {quantity}")

            new_qty = current - quantity
            await db.execute("""
                UPDATE inventory_items
                SET quantity = ?, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id=? AND user_id=? AND item_name=?
            """, (new_qty, guild_id, user_id, item_name))
            await db.commit()
            return new_qty
        except InsufficientItems:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def set_quantity(guild_id: int, user_id: int, item_name: str,
                        quantity: int, item_type: str = "custom",
                        source: str = "admin", metadata: dict = None) -> int:
    """
    metadata is optional and additive (Rank Card foundation / equip
    system) — used for role/temp_role items to carry {"role_id":...,
    "expires_at":...} so equip_engine.py can resolve which Discord
    role an inventory row actually points to without a second table.
    Existing callers that don't pass it are unaffected: meta_json
    stays None, and COALESCE keeps whatever metadata (if any) the row
    already had, same as give_item()'s upsert already does.
    """
    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    meta_json = json.dumps(metadata) if metadata else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO inventory_items
                (guild_id, user_id, item_name, item_type,
                 quantity, metadata, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id, item_name) DO UPDATE SET
                quantity   = excluded.quantity,
                item_type  = excluded.item_type,
                metadata   = COALESCE(excluded.metadata, inventory_items.metadata),
                updated_at = CURRENT_TIMESTAMP
        """, (guild_id, user_id, item_name, item_type, quantity, meta_json, source))
        await db.commit()
    return quantity


async def has_item(guild_id: int, user_id: int, item_name: str,
                    quantity: int = 1) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT quantity FROM inventory_items
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (guild_id, user_id, item_name))
        row = await cursor.fetchone()
    return bool(row) and row[0] >= quantity


async def get_inventory(guild_id: int, user_id: int,
                         include_empty: bool = False) -> list[dict]:
    query = """
        SELECT id, item_name, item_type, quantity, metadata,
               source, created_at, updated_at
        FROM inventory_items
        WHERE guild_id=? AND user_id=?
    """
    if not include_empty:
        query += " AND quantity > 0"
    query += " ORDER BY item_name ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, (guild_id, user_id))
        rows = await cursor.fetchall()

    result = []
    for r in rows:
        meta = None
        if r[4]:
            try:
                meta = json.loads(r[4])
            except Exception:
                meta = None
        result.append({
            "id": r[0], "item_name": r[1], "item_type": r[2],
            "quantity": r[3], "metadata": meta,
            "source": r[5], "created_at": r[6], "updated_at": r[7],
        })
    return result


async def get_guild_inventory_summary(guild_id: int, limit: int = 200) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, item_name, item_type, quantity, updated_at
            FROM inventory_items
            WHERE guild_id=? AND quantity > 0
            ORDER BY updated_at DESC LIMIT ?
        """, (guild_id, limit))
        rows = await cursor.fetchall()
    return [{
        "user_id": r[0], "item_name": r[1], "item_type": r[2],
        "quantity": r[3], "updated_at": r[4],
    } for r in rows]
