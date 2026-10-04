import aiosqlite
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# APPLICATION EMOJI IMPORT CACHE
#
# Discord's Application Emojis (docs.discord.com/developers/resources/emoji)
# are owned by the BOT APPLICATION, not any one guild — up to 2,000 of them,
# usable in any message the bot sends anywhere, with no USE_EXTERNAL_EMOJIS
# permission required on the destination channel. That's what makes them
# the right fallback for "this emoji isn't available to the bot": once
# imported, it's permanently available everywhere, unlike a raw external
# mention which only renders if the destination channel happens to grant
# that permission.
#
# This table is deliberately NOT guild-scoped (no guild_id column) — same
# reasoning as the emoji resource itself being application-wide, not
# per-guild. One row per distinct SOURCE emoji ID we've ever imported,
# mapping it to the application emoji Discord created for it, so a second
# import request for the same source emoji is a free cache hit instead of
# spending one of the 2,000-emoji budget and one more upload call.
# ═══════════════════════════════════════════════════════════════════════


async def ensure_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS app_emoji_imports (
                source_emoji_id  TEXT PRIMARY KEY,
                source_name      TEXT,
                animated         INTEGER DEFAULT 0,
                app_emoji_id     TEXT NOT NULL,
                app_emoji_name   TEXT NOT NULL,
                imported_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_by_source(source_emoji_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT app_emoji_id, app_emoji_name, animated
            FROM app_emoji_imports WHERE source_emoji_id = ?
        """, (source_emoji_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "animated": bool(row[2])}


async def save_mapping(source_emoji_id: str, source_name: str, animated: bool,
                        app_emoji_id: str, app_emoji_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO app_emoji_imports
                (source_emoji_id, source_name, animated, app_emoji_id, app_emoji_name)
            VALUES (?, ?, ?, ?, ?)
        """, (source_emoji_id, source_name, int(animated), app_emoji_id, app_emoji_name))
        await db.commit()


async def list_all() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT app_emoji_id, app_emoji_name, animated
            FROM app_emoji_imports ORDER BY imported_at DESC
        """)
        rows = await cursor.fetchall()
    return [{"id": r[0], "name": r[1], "animated": bool(r[2])} for r in rows]
