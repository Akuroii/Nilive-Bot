import aiosqlite

DB_PATH = "nero.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mvp_scores (
                guild_id INTEGER,
                user_id INTEGER,
                date TEXT,
                message_score REAL DEFAULT 0,
                voice_minutes REAL DEFAULT 0,
                total_score REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mvp_config (
                guild_id INTEGER PRIMARY KEY,
                mvp_role_id INTEGER,
                announce_channel_id INTEGER,
                reset_hours INTEGER DEFAULT 24
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER,
                user_id INTEGER,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS economy (
                guild_id INTEGER,
                user_id INTEGER,
                balance INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS voice_sessions (
                guild_id INTEGER,
                user_id INTEGER,
                join_time REAL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.commit()
    print("Database initialized")
