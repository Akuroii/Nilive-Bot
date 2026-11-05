"""Future-facing inventory activity; never reconstructs events from old rows.

Writers must use the connection that owns the underlying inventory transaction.
The helper does not commit: a rollback rolls back the event as well.
"""
import aiosqlite
from database import DB_PATH

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS inventory_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        item_name TEXT NOT NULL,
        item_type TEXT,
        quantity INTEGER NOT NULL,
        source TEXT,
        related_user_id INTEGER,
        event_key TEXT UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_inventory_activity_member "
    "ON inventory_activity(guild_id, user_id, id DESC)",
    """CREATE TABLE IF NOT EXISTS inventory_activity_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "INSERT OR IGNORE INTO inventory_activity_settings(id) VALUES (1)",
)


async def record_activity(db, *, guild_id, user_id, event_type, item_name,
                          quantity, item_type=None, source=None,
                          related_user_id=None, event_key=None):
    """Record an established movement in the caller's transaction.

    A key may only be used when the producer has a durable unique operation ID;
    duplicate keys are ignored. Unkeyed events represent actual committed writes,
    not idempotent business operations.
    """
    await db.execute(
        "INSERT INTO inventory_activity "
        "(guild_id,user_id,event_type,item_name,item_type,quantity,source,related_user_id,event_key) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING",
        (guild_id, user_id, event_type, item_name, item_type, int(quantity),
         source, related_user_id, event_key))


async def get_activity_page(guild_id, user_id, *, offset=0, limit=15):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,event_type,item_name,item_type,quantity,source,related_user_id,created_at "
            "FROM inventory_activity WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (guild_id, user_id, limit, offset))
        cols = ("id", "event_type", "item_name", "item_type", "quantity", "source", "related_user_id", "created_at")
        return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def count_activity(guild_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM inventory_activity WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        return (await cur.fetchone())[0]


async def get_activity_start():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT started_at FROM inventory_activity_settings WHERE id=1")
        row = await cur.fetchone()
        return row[0] if row else None
