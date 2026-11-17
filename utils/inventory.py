import aiosqlite
import json
from database import DB_PATH


class InsufficientItems(Exception):
    pass


class ItemTypeConflict(Exception):
    """A grant named an owned stack of a different kind. Message is user-facing."""


# Types whose Inventory behaviour depends on the type itself: a potion is
# activated, a title or role is equipped. Every other type (custom,
# shop_custom, event_drop, ...) is a plain collectible with no action.
FUNCTIONAL_ITEM_TYPES = frozenset({"potion", "title", "role", "temp_role"})


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
    quantity change, so raising rolls BOTH back: the purchase/potion/trade
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
                     quantity: int = 1, item_type: str | None = None,
                     metadata: dict = None, source: str = "system",
                     db: aiosqlite.Connection | None = None) -> int:
    """Add inventory quantity. An optional caller-owned transaction is not committed.

    Existing callers keep the standalone connection/commit behavior.
    A passed connection joins that transaction, matching safe_credit(..., db=).

    A stack is one row per (guild, user, item_name), so a grant can meet an
    owned stack of the same name. It never changes that stack's type:
      * `item_type=None` (a restore that only knows the name) keeps the
        stored type, or creates a plain `custom` stack;
      * the same type stacks;
      * two plain collectible types stack and the stored type stays;
      * anything else - a potion, title or role meeting a different kind -
        raises ItemTypeConflict and writes nothing.
    A row left at quantity 0 is not owned, so a typed grant may reuse it.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    meta_json = json.dumps(metadata) if metadata else None

    async def apply(connection):
        nonlocal item_type
        cursor = await connection.execute("""
            SELECT item_type, quantity FROM inventory_items
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (guild_id, user_id, item_name))
        existing = await cursor.fetchone()
        stored_type = (existing[0] or "custom") if existing else None
        owned = bool(existing) and int(existing[1] or 0) > 0
        if item_type is None:
            item_type = stored_type or "custom"
        elif owned and item_type != stored_type:
            if (item_type in FUNCTIONAL_ITEM_TYPES
                    or stored_type in FUNCTIONAL_ITEM_TYPES):
                raise ItemTypeConflict(
                    "An unrelated item already uses this name")
            item_type = stored_type
        if existing and not owned and item_type != stored_type:
            # Reusing an emptied row for a different kind of item: the old
            # kind's metadata (a potion's effect, a role id) must not carry
            # over to it.
            await connection.execute("""
                UPDATE inventory_items SET item_type=?, metadata=NULL
                WHERE guild_id=? AND user_id=? AND item_name=?
            """, (item_type, guild_id, user_id, item_name))
        await connection.execute("""
            INSERT INTO inventory_items
                (guild_id, user_id, item_name, item_type,
                 quantity, metadata, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id, item_name) DO UPDATE SET
                quantity   = quantity + ?,
                metadata   = COALESCE(excluded.metadata, inventory_items.metadata),
                updated_at = CURRENT_TIMESTAMP
        """, (guild_id, user_id, item_name, item_type,
              quantity, meta_json, source, quantity))
        cursor = await connection.execute("""
            SELECT quantity FROM inventory_items
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (guild_id, user_id, item_name))
        row = await cursor.fetchone()
        # Potion refund is a compensation, not a new acquisition. Its separate
        # transaction and possible failure require explicit outcome handling.
        if source != "refund":
            from utils.inventory_activity import record_activity
            await record_activity(connection, guild_id=guild_id, user_id=user_id,
                                  event_type=("restored" if source == "level_correction_restore"
                                              else "acquired"),
                                  item_name=item_name, item_type=item_type,
                                  quantity=quantity, source=source)
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
                       quantity: int = 1,
                       db: aiosqlite.Connection | None = None, *,
                       expected_id: int | None = None,
                       expected_type: str | None = None,
                       expected_metadata: dict | None = None) -> int:
    """Consume quantity and provenance together, optionally in a caller transaction.

    A supplied connection must already be in the caller's write transaction;
    this helper never commits or rolls it back.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    async def apply(connection):
        cursor = await connection.execute("""
            SELECT quantity, id, item_type, metadata FROM inventory_items
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (guild_id, user_id, item_name))
        row = await cursor.fetchone()
        current = row[0] if row else 0
        if (expected_id is not None and (not row or row[1] != expected_id)) or (
                expected_type is not None and (not row or row[2] != expected_type)):
            raise InsufficientItems("That inventory entry has changed")
        if expected_metadata is not None:
            try:
                actual_metadata = json.loads(row[3]) if row and row[3] else {}
            except (ValueError, TypeError):
                raise InsufficientItems("That inventory entry has changed")
            if actual_metadata != expected_metadata:
                raise InsufficientItems("That inventory entry has changed")
        if current < quantity:
            raise InsufficientItems(
                f"Has {current}x {item_name}, needs {quantity}")
        new_qty = current - quantity
        await connection.execute("""
            UPDATE inventory_items
            SET quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (new_qty, guild_id, user_id, item_name))
        await _consume_provenance(
            connection, guild_id, user_id, item_name, quantity)
        return new_qty

    if db is not None:
        return await apply(db)
    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            result = await apply(connection)
            await connection.commit()
            return result
        except BaseException:
            await connection.rollback()
            raise


async def set_quantity(guild_id: int, user_id: int, item_name: str,
                        quantity: int, item_type: str = "custom",
                        source: str = "admin", metadata: dict = None,
                        db: aiosqlite.Connection | None = None) -> int:
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

    async def apply(db):
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
        return quantity

    if db is not None:
        return await apply(db)
    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            result = await apply(connection)
            await connection.commit()
            return result
        except BaseException:
            await connection.rollback()
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


async def get_inventory_entries(guild_id: int, user_id: int, *,
                                include_empty: bool = False) -> list[dict]:
    """Return distinct permanent stacks and live timed holdings for UI readers.

    Unlike get_inventory, this is a new opt-in interface: existing name-based
    consumers continue to see only permanent stacks. Each key is scoped by the
    guild and user supplied to this reader, not a global authorization token.
    """
    from utils.timed_ownership import get_timed_holdings, is_expired

    result = []
    for row in await get_inventory(guild_id, user_id, include_empty=include_empty):
        meta = row.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        if (row.get("item_type") == "temp_role" and meta.get("expires_at")
                and is_expired(meta["expires_at"])):
            continue
        result.append({**row, "entry_key": f"i:{row['id']}", "timed": False,
                       "expires_at": meta.get("expires_at"),
                       "category": meta.get("category"),
                       "role_id": meta.get("role_id")})
    result.extend(await get_timed_holdings(guild_id, user_id))
    return sorted(result, key=lambda it: (str(it.get("item_name") or "").lower(),
                                         str(it.get("expires_at") or ""),
                                         it["entry_key"]))


def find_entry(entries: list[dict], *, entry_key: str | None = None,
               item_name: str | None = None, item_type: str | None = None) -> dict | None:
    """Resolve one exact entry or refuse an absent/ambiguous name."""
    matches = [entry for entry in entries
               if (entry.get("entry_key") == entry_key if entry_key is not None
                   else item_name is not None and entry.get("item_name") == item_name)
               and (item_name is None or entry.get("item_name") == item_name)
               and (item_type is None or entry.get("item_type") == item_type)]
    return matches[0] if len(matches) == 1 else None


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
