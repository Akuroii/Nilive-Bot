import aiosqlite
import json
from database import DB_PATH


class InsufficientItems(Exception):
    pass


async def _consume_provenance(db, guild_id: int, user_id: int, item_name: str,
                              quantity: int) -> None:
    """Take units off attributable level-reward grants, FIFO. Fails closed.

    Bookkeeping for `utils/level_ops.reward_grants`: after this runs, every later
    reversal judges `remaining_quantity` as the number of that reward's units
    still in the member's hands. If it silently failed while the inventory
    decrement committed, `remaining_quantity` would keep counting units the member
    no longer has, and a later correction would "reverse" a reward by removing
    units that came from a quest, a trade or a purchase - the exact class of
    unrelated-asset loss this provenance exists to prevent.

    Every caller runs this inside the same `BEGIN IMMEDIATE` transaction as the
    quantity change, so raising rolls BOTH back: the purchase/potion/drop/trade
    does not happen, and no stale attribution survives. That is deliberately the
    opposite of "never break a purchase": a refused action the member can retry is
    recoverable; a silently wrong attribution is not.
    """
    from utils.level_ops import consume_grants
    await consume_grants(db, guild_id, user_id, item_name, int(quantity))


async def _invalidate_provenance(db, guild_id: int, user_id: int,
                                 item_name: str, reason: str) -> None:
    """Mark an item stack's reward provenance unusable. Fails closed.

    `set_quantity()` writes an absolute quantity, so no grant for that stack can
    be trusted afterwards. If this write failed while the absolute quantity
    committed, the old `remaining_quantity` values would survive and a later
    correction could treat the new stack as containing the old reward's units.
    The caller's transaction is rolled back instead.
    """
    from utils.level_ops import invalidate_grants
    await invalidate_grants(db, guild_id, user_id, item_name, reason)


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


# Ownership rows that represent a Discord role rather than a stack of goods.
# The Inventory UI groups them into the Colors tab, but the *category* (does
# this role correspond to a Colors listing?) is a separate question answered by
# the record's metadata, never inferred from the type alone.
ROLE_ITEM_TYPES = ("role", "temp_role")


async def get_inventory(guild_id: int, user_id: int,
                         include_empty: bool = False,
                         include_timed: bool = True) -> list[dict]:
    """Unified ownership view: permanent rows + unexpired timed holdings.

    Every entry carries a stable `entry_key`:
        `i:<inventory_items.id>`  permanent (legacy) ownership
        `h:<timed_holdings.id>`   a timed holding

    `item_name` is NOT a key. A permanent and a temporary "Golden Key" coexist,
    and two timed grants with different expiry times are two entries, so any
    action on one must use `entry_key` to say which. Callers that still match on
    `item_name` keep working exactly as before when only one entry has that name
    (see `find_entry`).

    Expired timed holdings are filtered HERE, not only by the cleanup loop, so
    /inventory and /rank can never show one even if cleanup is behind. Timed
    entries also carry `expires_at`, `timed=True`, `category` and `product_id`.
    """
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
            "entry_key": f"i:{r[0]}", "timed": False, "expires_at": None,
            "product_id": (meta or {}).get("product_id"),
            "category": (meta or {}).get("category"),
            "role_id": (meta or {}).get("role_id"),
        })

    if include_timed:
        from utils.timed_ownership import get_timed_holdings

        for holding in await get_timed_holdings(guild_id, user_id):
            entry = dict(holding)
            entry.setdefault("updated_at", holding.get("created_at"))
            result.append(entry)

    result.sort(key=lambda it: (str(it.get("item_name") or "").lower(),
                                it.get("expires_at") or "", it["entry_key"]))
    return result


def find_entry(entries: list[dict], *, entry_key: str | None = None,
               item_name: str | None = None,
               item_type: str | None = None) -> dict | None:
    """Locate one ownership entry unambiguously.

    `entry_key` wins when supplied (the only safe way to name one of several
    same-name entries). Otherwise this falls back to the historical
    name lookup and REFUSES to guess between duplicates: an ambiguous name
    returns None rather than letting Drop/Equip touch the wrong holding.
    """
    if entry_key:
        for entry in entries:
            if entry.get("entry_key") == entry_key:
                return entry
        return None
    if item_name is None:
        return None
    matches = [e for e in entries if e.get("item_name") == item_name]
    if item_type is not None:
        typed = [e for e in matches if e.get("item_type") == item_type]
        if typed:
            matches = typed
    if len(matches) > 1:
        # A timed copy and a permanent copy (or two timers) share the name; the
        # name alone does not identify one of them.
        return None
    return matches[0] if matches else None


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
