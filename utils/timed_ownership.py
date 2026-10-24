"""
TIMED OWNERSHIP — separately identifiable temporary holdings.

Why this module exists
----------------------
`inventory_items` has UNIQUE(guild_id, user_id, item_name) and every write path
(`give_item`, `set_quantity`) upserts on that key, overwriting `item_type` and
`metadata`. That model cannot represent what the Shop now sells:

  * a permanent "Golden Key" AND an 8-hour "Golden Key" at the same time,
  * several timed copies of one name with different expiry times.

Re-keying `inventory_items` would mean rewriting trade, reward provenance
(`reward_grants`/`reward_reversals`) and the level-test-reset reversal guards,
all of which deliberately rely on "one row per name" and on `rowcount == 1`
checks to fail closed. That trade is not worth it, so timed holdings live in
their own table and `utils.inventory.get_inventory()` presents a unified view.

Design rules enforced here
--------------------------
1. ADDITIVE ONLY. No existing row is migrated, rewritten or reclassified.
2. The lifetime of a timed holding starts when it is CREATED, and callers create
   it only after delivery succeeded — never at publication or purchase attempt.
3. `expires_at` is stored as ISO-8601 UTC with an explicit offset, the same
   representation `temp_roles`, `leveling_active_boosts` and
   `temp_roles.expires_at` already use. Comparisons parse, never string-compare.
4. Read paths filter expired rows, so `/inventory` and `/rank` never show an
   expired holding even if the periodic cleanup has not run yet.
5. Cleanup is retry-safe: a row whose Discord role still has to be removed is
   NOT deleted (that row is the retry marker). Nothing else is used to remember
   outstanding work.
6. A timed expiry never revokes an independently held entitlement to the same
   Discord role — see `role_still_entitled()`.

Category identity
-----------------
A Color is a Shop product category, not a reward source. Purchases carry the
product id directly. Reward paths that only know a raw `role_id` resolve the
category through `resolve_color_category()`: exactly one enabled `type='color'`
listing for that role in that guild means "Color"; zero or several listings
means "cannot establish it", and the grant stays an ordinary role item. The
category is stored on the ownership record so it survives the listing being
edited or deleted later.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import DB_PATH

# ── Category identity ───────────────────────────────────────────────────

CATEGORY_COLOR = "color"

# The public Shop type whose grants are Colors. Temporary vs permanent is
# derived from the product's duration, never from a second type.
SHOP_TYPE_COLOR = "color"
ROLE_ITEM_TYPES = ("role", "temp_role")


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS timed_holdings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        item_name  TEXT NOT NULL,
        item_type  TEXT NOT NULL,
        quantity   INTEGER NOT NULL DEFAULT 1,
        metadata   TEXT,
        source     TEXT DEFAULT 'system',
        product_id INTEGER,
        category   TEXT,
        role_id    INTEGER,
        expires_at TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_timed_holdings_user
    ON timed_holdings(guild_id, user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_timed_holdings_expiry
    ON timed_holdings(expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_timed_holdings_role
    ON timed_holdings(guild_id, role_id)
    """,
)


async def ensure_timed_ownership_schema(db) -> None:
    """Create the timed-holding table and indexes. Additive and idempotent."""
    for statement in SCHEMA_STATEMENTS:
        await db.execute(statement)
    await db.commit()


# ── Time handling (one place, so formats cannot drift) ──────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    """Canonical stored form: ISO-8601 UTC with an explicit offset."""
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def parse_time(value) -> datetime | None:
    """Tolerant ISO parser: trailing Z, naive-as-UTC, space separator.

    Returns None for anything unparseable. Never raises — a bad stored value
    must not break a panel, a cleanup tick or a rank render.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            text = str(value).strip().replace(" ", "T", 1)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def expiry_from_hours(duration_hours) -> str | None:
    """`now + duration_hours` as the canonical stored string.

    None/0/negative/invalid means "permanent" — the caller must then use the
    existing permanent ownership path instead of this table.
    """
    try:
        hours = float(duration_hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return iso(utcnow() + timedelta(hours=hours))


def is_expired(expires_at, *, now: datetime | None = None) -> bool:
    """True when `expires_at` has passed. Unparseable counts as expired.

    Unparseable-means-expired is deliberate: a corrupt row must not become an
    eternal entitlement. It is also invisible to the UI, so nothing is lost by
    keeping it out of the member's view while cleanup removes it.
    """
    parsed = parse_time(expires_at)
    if parsed is None:
        return True
    return parsed <= (now or utcnow())


def format_remaining(expires_at, *, now: datetime | None = None) -> str | None:
    """Readable remaining lifetime: `3d 5h`, `5h 20m`, `12m`.

    None when the value is absent, invalid or already expired — the caller then
    shows no countdown at all rather than a negative or invented one.
    """
    parsed = parse_time(expires_at)
    if parsed is None:
        return None
    remaining = int((parsed - (now or utcnow())).total_seconds())
    if remaining <= 0:
        return None
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


# ── Grant / read ────────────────────────────────────────────────────────

def _row_to_holding(row, columns) -> dict:
    data = dict(zip(columns, row))
    try:
        data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else None
    except (TypeError, ValueError):
        data["metadata"] = None
    data["entry_key"] = f"h:{data['id']}"
    data["timed"] = True
    return data


_HOLDING_COLUMNS = ("id", "guild_id", "user_id", "item_name", "item_type",
                    "quantity", "metadata", "source", "product_id", "category",
                    "role_id", "expires_at", "created_at")


async def grant_timed_holding(guild_id: int, user_id: int, item_name: str,
                              *, expires_at: str, item_type: str = "custom",
                              quantity: int = 1, metadata: dict | None = None,
                              source: str = "system",
                              product_id: int | None = None,
                              category: str | None = None,
                              role_id: int | None = None,
                              db: aiosqlite.Connection | None = None) -> int:
    """Insert one timed holding. Call ONLY after delivery has succeeded.

    Each call creates its own record, even for an identical name: two grants
    with different expiry times are two independent holdings, and merging them
    would let one expiry destroy the other's remaining time.

    An optional caller-owned connection is joined, not committed — the same
    contract as `utils.inventory.give_item(..., db=)`.
    """
    if not expires_at:
        raise ValueError("grant_timed_holding requires an explicit expires_at")
    if int(quantity) <= 0:
        raise ValueError("quantity must be positive")
    payload = json.dumps(metadata) if metadata else None
    role_value = role_id
    if role_value is None and metadata:
        try:
            role_value = int(metadata.get("role_id")) if metadata.get("role_id") else None
        except (TypeError, ValueError):
            role_value = None

    async def apply(connection):
        cursor = await connection.execute(
            """
            INSERT INTO timed_holdings
                (guild_id, user_id, item_name, item_type, quantity, metadata,
                 source, product_id, category, role_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, item_name, item_type, int(quantity), payload,
             source, product_id, category, role_value, expires_at))
        return cursor.lastrowid

    if db is not None:
        return await apply(db)

    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            new_id = await apply(connection)
            await connection.commit()
            return new_id
        except Exception:
            await connection.execute("ROLLBACK")
            raise


async def get_timed_holdings(guild_id: int, user_id: int, *,
                             include_expired: bool = False,
                             category: str | None = None) -> list[dict]:
    """This member's timed holdings, expired ones excluded by default."""
    sql = (f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings "
           "WHERE guild_id=? AND user_id=?")
    params: list = [guild_id, user_id]
    if category is not None:
        sql += " AND category IS ?"
        params.append(category)
    sql += " ORDER BY expires_at ASC, id ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    holdings = [_row_to_holding(row, _HOLDING_COLUMNS) for row in rows]
    if include_expired:
        return holdings
    return [h for h in holdings if not is_expired(h["expires_at"])]


async def get_timed_holding(entry_key: str) -> dict | None:
    """One holding by its stable UI key (`h:<id>`). None if absent/invalid."""
    try:
        holding_id = int(str(entry_key).split(":", 1)[1])
    except (IndexError, ValueError, AttributeError):
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings "
            "WHERE id=?", (holding_id,))
        row = await cursor.fetchone()
    return _row_to_holding(row, _HOLDING_COLUMNS) if row else None


async def delete_timed_holding(entry_key_or_id) -> bool:
    """Delete exactly one holding. Returns True when a row was removed."""
    raw = str(entry_key_or_id)
    try:
        holding_id = int(raw.split(":", 1)[1]) if ":" in raw else int(raw)
    except (IndexError, ValueError):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM timed_holdings WHERE id=?",
                                  (holding_id,))
        await db.commit()
        return cursor.rowcount == 1


# ── Category resolution (Color identity) ────────────────────────────────

async def resolve_color_category(guild_id: int, role_id) -> dict | None:
    """The Color product behind a raw role_id, when it can be established.

    Returns {"product_id": ..., "item_name": ...} for EXACTLY ONE enabled
    `type='color'` listing bound to that role in this guild.

    Several listings for one role share the category, but which variant was
    actually awarded is unknowable from a bare role_id — and inheriting one
    variant's expiry would be a guess. So an ambiguous role still resolves its
    CATEGORY (category is a property of the role's listings) while the product
    id is left as None. No listing at all means "not a Color".
    """
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name FROM shop_items "
            "WHERE guild_id=? AND type=? AND role_id=? AND enabled=1 "
            "ORDER BY id ASC",
            (guild_id, SHOP_TYPE_COLOR, rid))
        rows = await cursor.fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return {"product_id": rows[0][0], "item_name": rows[0][1]}
    return {"product_id": None, "item_name": None}


async def color_metadata(guild_id: int, role_id, metadata: dict | None) -> dict:
    """Metadata enriched with stable Color identity, when the role is a Color.

    Called by reward delivery so the ownership record keeps its category even
    after the listing is renamed or deleted. An unrelated role reward is
    returned unchanged — it never becomes a Color by accident.
    """
    meta = dict(metadata or {})
    if meta.get("category") == CATEGORY_COLOR:
        return meta
    resolved = await resolve_color_category(guild_id, role_id)
    if not resolved:
        return meta
    meta["category"] = CATEGORY_COLOR
    if resolved.get("product_id") is not None:
        meta["product_id"] = resolved["product_id"]
    return meta


def category_of(item: dict) -> str | None:
    """Category marker for one unified inventory entry (metadata wins)."""
    direct = item.get("category")
    if direct:
        return direct
    meta = item.get("metadata") or {}
    value = meta.get("category")
    return value if isinstance(value, str) and value else None


def is_color(item: dict) -> bool:
    return category_of(item) == CATEGORY_COLOR


def role_id_of(item: dict) -> int | None:
    """The Discord role an entry points at, if any."""
    explicit = item.get("role_id")
    if explicit not in (None, ""):
        try:
            return int(explicit)
        except (TypeError, ValueError):
            return None
    meta = item.get("metadata") or {}
    raw = meta.get("role_id")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ── Entitlements: may this role be removed? ─────────────────────────────

async def guild_managed_role_ids(guild_id: int) -> set[int]:
    """Roles a guild-level subsystem assigns and revokes on its own.

    Prestige tier roles, Booster reward roles and self-picked booster colour
    roles are granted straight to the member without an inventory row
    (utils/prestige.sync_prestige_roles, cogs/boost.py). A timed expiry must
    never remove one of these: the owning subsystem decides, and expiring a
    purchased Color is not a reason to strip a tier role.
    """
    managed: set[int] = set()
    async with aiosqlite.connect(DB_PATH) as db:
        for sql, params in (
            ("SELECT role_id FROM prestige_roles WHERE guild_id=?", (guild_id,)),
            ("SELECT boost1_role_id, boost2_role_id FROM boost_config "
             "WHERE guild_id=?", (guild_id,)),
            ("SELECT role_id FROM boost_color_roles WHERE guild_id=?",
             (guild_id,)),
        ):
            try:
                cursor = await db.execute(sql, params)
                for row in await cursor.fetchall():
                    for value in row:
                        if value not in (None, ""):
                            try:
                                managed.add(int(value))
                            except (TypeError, ValueError):
                                continue
            except aiosqlite.Error:
                # Optional subsystems: a missing table simply manages nothing.
                continue
    return managed


async def role_still_entitled(guild_id: int, user_id: int, role_id: int,
                              *, exclude_entry_key: str | None = None) -> str | None:
    """A human-readable reason the role must stay, or None to allow removal.

    Unlike `utils.level_claims._role_granted_elsewhere` (which answers a
    reversal question about one claim), this answers "is the Discord role still
    justified for this member after THIS timed holding expires". Both are
    conservative for the same reason: revoking a role someone still earned is
    unrecoverable without their action.

    Sources consulted, in order:
      1. another unexpired timed holding for the same role,
      2. an inventory `role`/`temp_role` marker for the same role,
      3. an unexpired legacy `temp_roles` row (level-claim units, older grants),
      4. a guild-managed role (Prestige / Booster), which this code must not touch.
    """
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return None
    now = utcnow()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, expires_at FROM timed_holdings "
            "WHERE guild_id=? AND user_id=? AND role_id=?",
            (guild_id, user_id, rid))
        for holding_id, expires_at in await cursor.fetchall():
            if exclude_entry_key and str(entry_key_id(exclude_entry_key)) == str(holding_id):
                continue
            if not is_expired(expires_at, now=now):
                return "another unexpired timed holding"

        try:
            cursor = await db.execute(
                "SELECT item_name, source, metadata, quantity "
                "FROM inventory_items WHERE guild_id=? AND user_id=? "
                "AND item_type IN ('role','temp_role')",
                (guild_id, user_id))
            for item_name, source, metadata, quantity in await cursor.fetchall():
                if not quantity or int(quantity) <= 0:
                    continue
                try:
                    meta = json.loads(metadata) if metadata else {}
                except (TypeError, ValueError):
                    continue
                try:
                    if int((meta or {}).get("role_id")) != rid:
                        continue
                except (TypeError, ValueError):
                    continue
                return f"owned role item ({item_name}, {source})"
        except aiosqlite.Error:
            pass

        try:
            cursor = await db.execute(
                "SELECT id, expires_at FROM temp_roles "
                "WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, rid))
            for _temp_id, expires_at in await cursor.fetchall():
                if not is_expired(expires_at, now=now):
                    return "unexpired temp_roles grant"
        except aiosqlite.Error:
            pass

    if rid in await guild_managed_role_ids(guild_id):
        return "guild-managed Prestige/Booster role"
    return None


def entry_key_id(entry_key: str) -> int | None:
    """Numeric id inside an `h:<id>` / `i:<id>` key (None when malformed)."""
    try:
        return int(str(entry_key).split(":", 1)[1])
    except (IndexError, ValueError, AttributeError):
        return None


# ── Expiry cleanup ──────────────────────────────────────────────────────

async def expired_holdings(limit: int = 200) -> list[dict]:
    """Holdings whose time has run out, oldest expiry first."""
    now = iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings "
            "WHERE expires_at <= ? ORDER BY expires_at ASC, id ASC LIMIT ?",
            (now, limit))
        rows = await cursor.fetchall()
    return [_row_to_holding(row, _HOLDING_COLUMNS) for row in rows]


async def clear_equip_for_holding(guild_id: int, user_id: int,
                                  entry_key: str,
                                  role_id: int | None = None) -> bool:
    """Drop `equipped_roles` only when it points at this exact holding.

    `entry_key` is the identity. Rows written before this feature have no
    entry_key, so a NULL key plus a matching role_id also counts — and that
    fallback is only ever reached after `role_still_entitled()` established
    that nothing else requires the role, so it cannot clear a slot that another
    entitlement still needs.
    """
    from utils.equip_engine import get_equipped

    current = await get_equipped(guild_id, user_id)
    if not current:
        return False
    stored = current.get("entry_key")
    if stored:
        if stored != entry_key:
            return False
    else:
        if role_id is None or int(current.get("role_id") or 0) != int(role_id):
            return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM equipped_roles WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        await db.commit()
    return True


async def expire_one(bot, holding: dict) -> dict:
    """Retire ONE expired holding, reconciling Discord and bookkeeping.

    Idempotent by construction: every step reads current state first, and the
    holding row is deleted only once nothing is left that would need it as a
    retry marker.

    Returns {"status": "removed"|"kept"|"skipped", "reason": ...}.
    """
    guild_id = int(holding["guild_id"])
    user_id = int(holding["user_id"])
    entry_key = holding["entry_key"]
    role_id = holding.get("role_id")

    if role_id is None:
        # Nothing Discord-side to undo: the ownership record IS the whole state.
        await delete_timed_holding(entry_key)
        return {"status": "removed", "reason": "no role"}

    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        # Cannot see the guild: keep the record so the removal is retried once
        # the bot is back. It is already hidden from every read path.
        return {"status": "kept", "reason": "guild unavailable"}

    try:
        reason = await role_still_entitled(
            guild_id, user_id, role_id, exclude_entry_key=entry_key)
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "kept", "reason": f"entitlement check failed: {exc}"}

    if reason:
        # Another entitlement keeps the role. Only this timed record dies.
        await delete_timed_holding(entry_key)
        return {"status": "removed", "reason": f"role retained ({reason})"}

    member = guild.get_member(user_id)
    role = guild.get_role(int(role_id))
    if member is not None and role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason="Timed entitlement expired")
        except Exception as exc:
            # Keep the row: it is the only marker that this removal is pending.
            return {"status": "kept", "reason": f"Discord removal failed: {exc}"}

    # Bookkeeping BEFORE the row is deleted: `clear_equip_for_holding` is
    # idempotent, so a crash after this point simply retries, whereas deleting
    # the holding first would leave an equipped slot pointing at nothing with
    # only this row left to remember it by.
    try:
        await clear_equip_for_holding(guild_id, user_id, entry_key,
                                      role_id=role_id)
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "kept",
                "reason": f"equip bookkeeping cleanup failed: {exc}"}

    await delete_timed_holding(entry_key)
    return {"status": "removed", "reason": "expired"}


async def cleanup_expired_holdings(bot, *, limit: int = 200) -> dict:
    """One cleanup pass. Safe to re-run at any time."""
    removed = kept = 0
    errors: list[str] = []
    for holding in await expired_holdings(limit=limit):
        try:
            result = await expire_one(bot, holding)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{holding.get('entry_key')}: {exc}")
            kept += 1
            continue
        if result["status"] == "removed":
            removed += 1
        else:
            kept += 1
    return {"removed": removed, "kept": kept, "errors": errors}


# ── Purchase saga journal ───────────────────────────────────────────────

PURCHASE_PENDING = "pending"
PURCHASE_DELIVERED = "delivered"
PURCHASE_REFUNDED = "refunded"
PURCHASE_UNCERTAIN = "uncertain"
PURCHASE_FAILED = "failed"

PURCHASE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_purchase_operations (
        op_id          TEXT PRIMARY KEY,
        guild_id       INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        item_id        INTEGER,
        item_name      TEXT,
        currency       TEXT,
        amount         INTEGER NOT NULL DEFAULT 0,
        stock_reserved INTEGER NOT NULL DEFAULT 0,
        charged        INTEGER NOT NULL DEFAULT 0,
        refunded       INTEGER NOT NULL DEFAULT 0,
        stock_returned INTEGER NOT NULL DEFAULT 0,
        status         TEXT NOT NULL,
        detail         TEXT,
        history_id     INTEGER,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_ops_status
    ON shop_purchase_operations(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_ops_user
    ON shop_purchase_operations(guild_id, user_id)
    """,
)


# Effect flags added after the first schema version. They let a compensation
# that crashed half-way (money returned, stock not yet) be finished later
# instead of being repeated or abandoned. PRAGMA-guarded so the migration is
# idempotent and touches no existing value.
_PURCHASE_EXTRA_COLUMNS = (
    ("refunded", "INTEGER NOT NULL DEFAULT 0"),
    ("stock_returned", "INTEGER NOT NULL DEFAULT 0"),
)


async def ensure_purchase_journal_schema(db) -> None:
    for statement in PURCHASE_SCHEMA:
        await db.execute(statement)
    cursor = await db.execute("PRAGMA table_info(shop_purchase_operations)")
    present = {row[1] for row in await cursor.fetchall()}
    for name, ddl in _PURCHASE_EXTRA_COLUMNS:
        if name not in present:
            await db.execute(
                f"ALTER TABLE shop_purchase_operations ADD COLUMN {name} {ddl}")
    await db.commit()


async def open_purchase(guild_id: int, user_id: int, *, item_id: int | None,
                        item_name: str, currency: str, amount: int,
                        db: aiosqlite.Connection | None = None) -> str:
    """Write the recovery anchor BEFORE any money or stock moves.

    A crash after this row exists is diagnosable and refundable; a crash before
    it is a purchase that never started, which is the safe side.
    """
    op_id = uuid.uuid4().hex
    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute(
            "INSERT INTO shop_purchase_operations "
            "(op_id, guild_id, user_id, item_id, item_name, currency, amount,"
            " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, guild_id, user_id, item_id, item_name, currency,
             int(amount or 0), PURCHASE_PENDING))
        await connection.commit()
    return op_id


async def update_purchase(op_id: str, *, status: str | None = None,
                          detail: str | None = None,
                          stock_reserved: int | None = None,
                          charged: int | None = None,
                          refunded: int | None = None,
                          stock_returned: int | None = None,
                          history_id: int | None = None) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if detail is not None:
        sets.append("detail=?")
        params.append(detail)
    if stock_reserved is not None:
        sets.append("stock_reserved=?")
        params.append(int(stock_reserved))
    if charged is not None:
        sets.append("charged=?")
        params.append(int(charged))
    if refunded is not None:
        sets.append("refunded=?")
        params.append(int(refunded))
    if stock_returned is not None:
        sets.append("stock_returned=?")
        params.append(int(stock_returned))
    if history_id is not None:
        sets.append("history_id=?")
        params.append(int(history_id))
    if not sets:
        return
    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(op_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE shop_purchase_operations SET {', '.join(sets)} "
            "WHERE op_id=?", params)
        await db.commit()


async def get_purchase(op_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT op_id, guild_id, user_id, item_id, item_name, currency,"
            " amount, stock_reserved, charged, refunded, stock_returned,"
            " status, detail, history_id, created_at"
            " FROM shop_purchase_operations WHERE op_id=?", (op_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    keys = ("op_id", "guild_id", "user_id", "item_id", "item_name", "currency",
            "amount", "stock_reserved", "charged", "refunded", "stock_returned",
            "status", "detail", "history_id", "created_at")
    return dict(zip(keys, row))


async def grant_timed_product(bot, guild_id: int, user_id: int, *,
                              item_name: str, duration_hours,
                              item_type: str = "shop_custom",
                              metadata: dict | None = None,
                              source: str = "shop",
                              product_id: int | None = None,
                              category: str | None = None,
                              role_id: int | None = None,
                              reason: str = "Timed reward") -> dict:
    """Deliver a timed holding. Call ONLY after the charge/reservation is safe.

    Returns {"success": bool, "entry_key": str|None, "expires_at": str|None,
             "error": str|None, "uncertain": bool}.

    `expires_at` is computed HERE, at the moment of delivery, never from the
    listing's publication time or from an abandoned purchase attempt — that is
    the whole point of "the lifetime starts when delivery succeeds".

    A role-backed holding adds the Discord role before the ownership row is
    written. If the role cannot be added, nothing is persisted and the caller
    can compensate cleanly. If the role IS added but the row write fails, the
    result is reported as `uncertain` rather than failed: the member may hold a
    role the bot has no record of, and refunding then would hand out a free
    colour.
    """
    from utils.permissions import check_bot_role_position

    expires_at = expiry_from_hours(duration_hours)
    if expires_at is None:
        return {"success": False, "entry_key": None, "expires_at": None,
                "error": "A timed product needs a positive duration.",
                "uncertain": False}

    meta = dict(metadata or {})
    if category:
        meta["category"] = category
    if role_id is not None:
        meta["role_id"] = int(role_id)
    if product_id is not None:
        meta["product_id"] = int(product_id)

    role_added = False
    if role_id is not None:
        if bot is None:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": "Bot is offline; the role cannot be granted.",
                    "uncertain": False}
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": "Bot is not in that guild.", "uncertain": False}
        member = guild.get_member(int(user_id))
        role = guild.get_role(int(role_id))
        if member is None or role is None:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": "Member or role not found.", "uncertain": False}
        can_assign, warning = check_bot_role_position(guild, role)
        if not can_assign:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": warning, "uncertain": False}
        if role not in member.roles:
            try:
                await member.add_roles(role, reason=reason)
                role_added = True
            except Exception as exc:
                return {"success": False, "entry_key": None,
                        "expires_at": None,
                        "error": f"Discord error assigning role: {exc}",
                        "uncertain": False}

    try:
        holding_id = await grant_timed_holding(
            guild_id, user_id, item_name, expires_at=expires_at,
            item_type=item_type, metadata=meta, source=source,
            product_id=product_id, category=category, role_id=role_id)
    except Exception as exc:
        return {"success": False, "entry_key": None, "expires_at": None,
                "error": f"Could not record ownership: {exc}",
                # The role may already be on the member: this must be
                # reconciled, not blindly refunded.
                "uncertain": role_added}

    entry_key = f"h:{holding_id}"
    if role_id is not None:
        # The timed colour becomes the worn one, matching how every other role
        # reward auto-equips today. A failure here is cosmetic: ownership and
        # the Discord role both exist and the member can equip manually.
        try:
            from utils.equip_engine import record_equipped
            await record_equipped(guild_id, user_id, item_name,
                                 role_id=int(role_id), entry_key=entry_key)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[TIMED] equip bookkeeping failed for {entry_key}: {exc}")

    return {"success": True, "entry_key": entry_key,
            "expires_at": expires_at, "error": None, "uncertain": False}


async def claim_purchase_transition(op_id: str, *,
                                    from_statuses: tuple[str, ...],
                                    to_status: str,
                                    detail: str | None = None) -> bool:
    """Atomically move one purchase operation between states.

    The `WHERE status IN (...)` clause is what makes compensation
    exactly-once: two concurrent retries (a double click, a restart mid-recovery)
    cannot both win the transition, so a refund or a stock restore can never be
    applied twice. Returns True only for the caller that won.
    """
    marks = ",".join("?" for _ in from_statuses)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE shop_purchase_operations SET status=?, detail=?, "
            f"updated_at=CURRENT_TIMESTAMP "
            f"WHERE op_id=? AND status IN ({marks})",
            (to_status, detail, op_id, *from_statuses))
        await db.commit()
        return cursor.rowcount == 1


# ── Compensation ────────────────────────────────────────────────────────

async def restore_stock(item_id) -> bool:
    """Give one unit back to a stock-limited listing.

    Mirrors `utils.economy_safe.safe_decrement_stock()`: an unlimited listing
    has nothing to give back, and `max_stock` (not the current value) decides
    whether the counter is meaningful at all.
    """
    if item_id is None:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT max_stock FROM shop_items WHERE id=?", (item_id,))
        row = await cursor.fetchone()
        if not row or not row[0]:
            return False
        await db.execute(
            "UPDATE shop_items SET current_stock = current_stock + 1 WHERE id=?",
            (item_id,))
        await db.commit()
        return True


async def refund_purchase(op_id: str) -> dict:
    """Return what a failed purchase took, at most once per effect.

    The two effects are tracked separately because they are not atomic with
    each other: a crash after the money is back but before the stock is
    restored leaves `refunded=1, stock_returned=0`, and the next reconciliation
    pass finishes exactly the outstanding half. A blind retry would double the
    refund, which is why the flags — not the caller's intent — decide.

    Returns {"refunded": bool, "stock_returned": bool, "complete": bool,
             "error": str|None}.
    """
    op = await get_purchase(op_id)
    if op is None:
        return {"refunded": False, "stock_returned": False,
                "complete": False, "error": "Unknown purchase operation"}
    if op["status"] == PURCHASE_DELIVERED:
        # Delivered means the member got the goods; anything after that is a
        # support decision, never an automatic refund.
        return {"refunded": False, "stock_returned": False,
                "complete": True, "error": None}

    error = None
    if op["charged"] and not op["refunded"]:
        from utils.economy_safe import safe_credit
        try:
            await safe_credit(
                op["guild_id"], op["user_id"], int(op["amount"] or 0),
                currency=op["currency"] or "balance",
                reason=f"Refund: {op['item_name']}", source="shop_refund")
            await update_purchase(op_id, refunded=1)
            op["refunded"] = 1
        except Exception as exc:
            error = f"refund failed: {exc}"

    if op["stock_reserved"] and not op["stock_returned"]:
        try:
            await restore_stock(op["item_id"])
            await update_purchase(op_id, stock_returned=1)
            op["stock_returned"] = 1
        except Exception as exc:
            error = error or f"stock restore failed: {exc}"

    complete = bool(op["refunded"] or not op["charged"]) and \
               bool(op["stock_returned"] or not op["stock_reserved"])
    return {"refunded": bool(op["refunded"]),
            "stock_returned": bool(op["stock_returned"]),
            "complete": complete and error is None, "error": error}


async def reconcile_purchases(*, older_than_minutes: int = 15,
                              limit: int = 100) -> dict:
    """Finish compensations and quarantine purchases that never resolved.

    Two buckets:

    * `failed` / `refunded` operations with an outstanding effect flag —
      completed here, idempotently.
    * `pending` operations older than `older_than_minutes` — the process died
      between the charge and the delivery result. The money may or may not have
      moved and the goods may or may not exist, so the row is parked as
      `uncertain` for a human decision. It is NOT auto-refunded: that is exactly
      the "don't refund when delivery is uncertain" rule.
    """
    stats = {"completed": 0, "uncertain": 0, "errors": 0}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT op_id FROM shop_purchase_operations"
            " WHERE status IN (?, ?)"
            "   AND (refunded = 0 AND charged = 1"
            "        OR stock_returned = 0 AND stock_reserved = 1)"
            " LIMIT ?",
            (PURCHASE_FAILED, PURCHASE_REFUNDED, limit))
        rows = [r[0] for r in await cursor.fetchall()]
    for op_id in rows:
        try:
            result = await refund_purchase(op_id)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[SHOP] Compensation failed for {op_id}: {exc}")
            stats["errors"] += 1
            continue
        if result["complete"]:
            stats["completed"] += 1
        else:
            stats["errors"] += 1

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shop_purchase_operations SET status=?, detail=?,"
            " updated_at=CURRENT_TIMESTAMP"
            " WHERE status=? AND created_at <= datetime('now', ?)",
            (PURCHASE_UNCERTAIN,
             "Timed out before a delivery result; needs review",
             PURCHASE_PENDING,
             f"-{int(older_than_minutes)} minutes"))
        await db.commit()
        stats["uncertain"] = cursor.rowcount or 0
    return stats
