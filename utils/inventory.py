import aiosqlite
import json
from database import DB_PATH


class InsufficientItems(Exception):
    pass


async def _consume_provenance(db, guild_id: int, user_id: int, item_name: str,
                              quantity: int) -> None:
    """Take units off attributable level-reward grants, FIFO. Never raises.

    Bookkeeping for `utils/level_ops.reward_grants`. It must not be able to break
    a purchase, a potion use or a drop, so a failure is logged and the inventory
    change still commits - the worst case is a reward grant that later has to be
    reconciled by hand, never a lost item.
    """
    try:
        from utils.level_ops import consume_grants
        await consume_grants(db, guild_id, user_id, item_name, int(quantity))
    except Exception as exc:                      # pragma: no cover - safety net
        print(f"[PROVENANCE] consume_grants({item_name} x{quantity}) failed: {exc}")


async def _invalidate_provenance(db, guild_id: int, user_id: int,
                                 item_name: str, reason: str) -> None:
    """Mark an item stack's reward provenance unusable. Never raises."""
    try:
        from utils.level_ops import invalidate_grants
        await invalidate_grants(db, guild_id, user_id, item_name, reason)
    except Exception as exc:                      # pragma: no cover - safety net
        print(f"[PROVENANCE] invalidate_grants({item_name}) failed: {exc}")


async def give_item(guild_id: int, user_id: int, item_name: str,
                     quantity: int = 1, item_type: str = "custom",
                     metadata: dict = None, source: str = "system",
                     db: aiosqlite.Connection | None = None) -> int:
    """Add inventory quantity. An optional caller-owned transaction is not committed.

    Existing callers keep the standalone connection/commit behavior.
    A passed connection joins that transaction, matching safe_credit(..., db=).
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    meta_json = json.dumps(metadata) if metadata else None

    async def apply(connection):
        await connection.execute("""
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
        cursor = await connection.execute("""
            SELECT quantity FROM inventory_items
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (guild_id, user_id, item_name))
        row = await cursor.fetchone()
        return row[0] if row else quantity

    if db is not None:
        return await apply(db)

    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            result = await apply(connection)
            await connection.commit()
            return result
        except Exception:
            await connection.execute("ROLLBACK")
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
            # Level-reward provenance: the units leaving the stack are taken off
            # the oldest attributable reward grants first, inside this same
            # transaction, so `reward_grants.remaining_quantity` keeps meaning
            # "units of that reward that are still here". Without this the only
            # way to judge a reversal would be the merged stack total, which
            # cannot tell a reward's units from a quest's.
            await _consume_provenance(db, guild_id, user_id, item_name, quantity)
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
        # An absolute quantity write does not say which units it replaced, so
        # after one runs no reward grant for this stack can be trusted. Giving up
        # on that provenance explicitly is what stops a later reset from
        # deducting another source's items on a guess.
        await _invalidate_provenance(
            db, guild_id, user_id, item_name,
            "set_quantity() wrote an absolute quantity, which cannot be "
            "attributed to individual grants")
        await db.commit()
    return quantity


async def drop_item(guild_id: int, user_id: int, item_name: str) -> dict:
    """
    Remove ALL copies of an owned item, regardless of quantity. Used
    by Wallet → Item → Drop after the user has confirmed. Returns the
    quantity that was removed, or {"success": False, ...} if the item
    isn't owned. Does NOT touch Discord roles or equipped_* tables —
    the Wallet layer is responsible for that cleanup (equip_engine /
    title_engine / potion_engine) so this helper stays type-agnostic.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT quantity, item_type FROM inventory_items
                WHERE guild_id=? AND user_id=? AND item_name=?
            """, (guild_id, user_id, item_name))
            row = await cursor.fetchone()
            if not row or (row[0] or 0) <= 0:
                await db.execute("ROLLBACK")
                return {"success": False,
                        "error": f"You do not own **{item_name}**."}
            dropped_qty = int(row[0] or 0)
            dropped_type = row[1]
            await db.execute("""
                DELETE FROM inventory_items
                WHERE guild_id=? AND user_id=? AND item_name=?
            """, (guild_id, user_id, item_name))
            await _consume_provenance(db, guild_id, user_id, item_name,
                                      dropped_qty)
            await db.commit()
            return {"success": True, "item_name": item_name,
                    "quantity": dropped_qty, "item_type": dropped_type}
        except Exception:
            await db.execute("ROLLBACK")
            raise


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
