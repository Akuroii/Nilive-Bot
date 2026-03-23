import aiosqlite
import asyncio
import os

# ══════════════════════════════════════════════════════
# IMPORTANT: DB_PATH points to a Railway persistent volume
# Go to Railway → your service → Volumes → Add Volume
# Mount path: /app/data
# Without this, nero.db is wiped on every redeploy!
# ══════════════════════════════════════════════════════
DB_PATH = "/app/data/nero.db"

OWNER_DISCORD_ID = int(os.getenv("OWNER_ID", "704453350384730237"))

# Your server IDs — owner access is guaranteed for these
# even on a completely fresh database
FALLBACK_GUILD_IDS = [
    1360461358486913145,
]


async def init_db():
    # Make sure the directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:

        # ══════════════════════════════════════════
        # EXISTING TABLES — PRESERVED
        # ══════════════════════════════════════════

        await db.execute("""
            CREATE TABLE IF NOT EXISTS mvp_scores (
                guild_id      INTEGER,
                user_id       INTEGER,
                date          TEXT,
                message_score REAL DEFAULT 0,
                voice_minutes REAL DEFAULT 0,
                total_score   REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS voice_sessions (
                guild_id  INTEGER,
                user_id   INTEGER,
                join_time REAL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER,
                user_id  INTEGER,
                xp       INTEGER DEFAULT 0,
                level    INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS economy (
                guild_id INTEGER,
                user_id  INTEGER,
                balance  INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER,
                channel_id    INTEGER,
                user_id       INTEGER,
                staff_role_id INTEGER,
                status        TEXT DEFAULT 'open',
                category      TEXT,
                created_at    TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_config (
                guild_id           INTEGER PRIMARY KEY,
                staff_role_id      INTEGER,
                ticket_category_id INTEGER,
                log_channel_id     INTEGER,
                categories         TEXT DEFAULT 'General Support,Report,Ban Appeal,Other'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS embed_templates (
                guild_id INTEGER,
                name     TEXT,
                data     TEXT,
                PRIMARY KEY (guild_id, name)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id         INTEGER,
                channel_id       INTEGER,
                message_id       INTEGER,
                button_label     TEXT,
                button_emoji     TEXT,
                button_color     TEXT DEFAULT 'blurple',
                role_id          INTEGER,
                booster_only     INTEGER DEFAULT 0,
                required_role_id INTEGER DEFAULT NULL,
                PRIMARY KEY (message_id, role_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_role_panels (
                message_id           INTEGER PRIMARY KEY,
                guild_id             INTEGER,
                exclusive            INTEGER DEFAULT 0,
                max_roles            INTEGER DEFAULT 0,
                require_confirmation INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_role_expiry (
                guild_id   INTEGER,
                user_id    INTEGER,
                role_id    INTEGER,
                expires_at TEXT,
                PRIMARY KEY (guild_id, user_id, role_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS rr_panels (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                title                TEXT,
                description          TEXT,
                color                TEXT,
                channel_id           TEXT,
                buttons              TEXT,
                exclusive            INTEGER DEFAULT 0,
                max_roles            INTEGER DEFAULT 0,
                require_confirmation INTEGER DEFAULT 0,
                required_role        TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS custom_commands (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER,
                trigger           TEXT,
                allowed_roles     TEXT DEFAULT '[]',
                actions           TEXT DEFAULT '[]',
                embed_title       TEXT,
                embed_description TEXT,
                embed_color       TEXT DEFAULT '#ED4245',
                log_channel_id    INTEGER,
                same_channel      INTEGER DEFAULT 0,
                dm_member         INTEGER DEFAULT 0,
                dm_message        TEXT,
                requires_mention  INTEGER DEFAULT 1,
                requires_reason   INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS disabled_commands (
                guild_id INTEGER,
                command  TEXT,
                PRIMARY KEY (guild_id, command)
            )
        """)

        # ══════════════════════════════════════════
        # EXISTING TABLES — ADD MISSING COLUMNS
        # ══════════════════════════════════════════

        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                guild_id               INTEGER,
                user_id                INTEGER,
                moderator_id           INTEGER,
                reason                 TEXT,
                timestamp              TEXT,
                user_display_name      TEXT DEFAULT 'Unknown User',
                user_avatar_url        TEXT,
                moderator_display_name TEXT DEFAULT 'Unknown Moderator'
            )
        """)
        for col in [
            ("user_display_name",      "TEXT DEFAULT 'Unknown User'"),
            ("user_avatar_url",        "TEXT"),
            ("moderator_display_name", "TEXT DEFAULT 'Unknown Moderator'"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE warnings ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS mod_logs (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id               INTEGER,
                action                 TEXT,
                moderator_id           INTEGER,
                target_id              INTEGER,
                reason                 TEXT,
                timestamp              TEXT,
                user_display_name      TEXT DEFAULT 'Unknown User',
                user_avatar_url        TEXT,
                moderator_display_name TEXT DEFAULT 'Unknown Moderator',
                source                 TEXT DEFAULT 'bot',
                extra_actions          TEXT,
                duration_minutes       INTEGER,
                evidence_url           TEXT,
                deleted                INTEGER DEFAULT 0,
                deleted_by             INTEGER,
                deleted_at             TEXT
            )
        """)
        for col in [
            ("user_display_name",      "TEXT DEFAULT 'Unknown User'"),
            ("user_avatar_url",        "TEXT"),
            ("moderator_display_name", "TEXT DEFAULT 'Unknown Moderator'"),
            ("source",                 "TEXT DEFAULT 'bot'"),
            ("extra_actions",          "TEXT"),
            ("duration_minutes",       "INTEGER"),
            ("evidence_url",           "TEXT"),
            ("deleted",                "INTEGER DEFAULT 0"),
            ("deleted_by",             "INTEGER"),
            ("deleted_at",             "TEXT"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE mod_logs ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS mvp_config (
                guild_id            INTEGER PRIMARY KEY,
                mvp_role_id         INTEGER,
                announce_channel_id INTEGER,
                reset_hours         INTEGER DEFAULT 24,
                enabled             INTEGER DEFAULT 1,
                cycle_hours         INTEGER DEFAULT 6,
                chat_word_weight    REAL DEFAULT 1.0,
                voice_minute_weight REAL DEFAULT 2.0,
                daily_reset_hour    INTEGER DEFAULT 0
            )
        """)
        for col in [
            ("enabled",             "INTEGER DEFAULT 1"),
            ("cycle_hours",         "INTEGER DEFAULT 6"),
            ("chat_word_weight",    "REAL DEFAULT 1.0"),
            ("voice_minute_weight", "REAL DEFAULT 2.0"),
            ("daily_reset_hour",    "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE mvp_config ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS boost_config (
                guild_id               INTEGER PRIMARY KEY,
                role1_id               INTEGER,
                role2_id               INTEGER,
                announce_channel_id    INTEGER,
                enabled                INTEGER DEFAULT 1,
                boost1_role_id         INTEGER,
                boost2_role_id         INTEGER,
                boost2_channel_id      INTEGER,
                color_roles_enabled    INTEGER DEFAULT 0,
                auto_remove_on_unboost INTEGER DEFAULT 1
            )
        """)
        for col in [
            ("enabled",                "INTEGER DEFAULT 1"),
            ("boost1_role_id",         "INTEGER"),
            ("boost2_role_id",         "INTEGER"),
            ("boost2_channel_id",      "INTEGER"),
            ("color_roles_enabled",    "INTEGER DEFAULT 0"),
            ("auto_remove_on_unboost", "INTEGER DEFAULT 1"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE boost_config ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER,
                trigger           TEXT,
                response          TEXT,
                embed_title       TEXT,
                embed_color       TEXT,
                input_channel_id  INTEGER,
                output_channel_id INTEGER,
                trigger_words     TEXT,
                response_text     TEXT,
                response_embed    TEXT,
                response_type     TEXT DEFAULT 'text',
                match_type        TEXT DEFAULT 'contains',
                fuzzy_match       INTEGER DEFAULT 0,
                case_sensitive    INTEGER DEFAULT 0,
                response_chance   INTEGER DEFAULT 100,
                allowed_channels  TEXT,
                enabled           INTEGER DEFAULT 1
            )
        """)
        for col in [
            ("trigger_words",    "TEXT"),
            ("response_text",    "TEXT"),
            ("response_embed",   "TEXT"),
            ("response_type",    "TEXT DEFAULT 'text'"),
            ("match_type",       "TEXT DEFAULT 'contains'"),
            ("fuzzy_match",      "INTEGER DEFAULT 0"),
            ("case_sensitive",   "INTEGER DEFAULT 0"),
            ("response_chance",  "INTEGER DEFAULT 100"),
            ("allowed_channels", "TEXT"),
            ("enabled",          "INTEGER DEFAULT 1"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE triggers ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass

        # ══════════════════════════════════════════
        # NEW TABLES — BLUEPRINT v2.0
        # ══════════════════════════════════════════

        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id                 INTEGER PRIMARY KEY,
                prefix                   TEXT DEFAULT '/',
                timezone                 TEXT DEFAULT 'UTC',
                language                 TEXT DEFAULT 'en',
                log_channel_id           INTEGER,
                currency_name            TEXT DEFAULT 'Coins',
                currency_emoji_id        TEXT,
                status_rotation_enabled  INTEGER DEFAULT 0,
                status_rotation_interval INTEGER DEFAULT 5,
                updated_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_gs_guild
            ON guild_settings(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS status_messages (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                text     TEXT NOT NULL,
                type     TEXT DEFAULT 'playing',
                position INTEGER DEFAULT 0,
                enabled  INTEGER DEFAULT 1
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS welcome_config (
                guild_id          INTEGER PRIMARY KEY,
                join_enabled      INTEGER DEFAULT 0,
                join_channel_id   INTEGER,
                auto_role_id      INTEGER,
                join_message_mode TEXT DEFAULT 'random',
                leave_enabled     INTEGER DEFAULT 0,
                leave_channel_id  INTEGER,
                rules_enabled     INTEGER DEFAULT 0,
                rules_channel_id  INTEGER,
                rules_role_id     INTEGER,
                rules_button_text TEXT DEFAULT '✅ I Accept',
                updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS welcome_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                type       TEXT NOT NULL,
                embed_data TEXT NOT NULL,
                position   INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_wm_guild
            ON welcome_messages(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS warning_thresholds (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER NOT NULL,
                warn_count       INTEGER NOT NULL,
                action           TEXT NOT NULL,
                duration_minutes INTEGER,
                role_id          INTEGER,
                enabled          INTEGER DEFAULT 1,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_wt_guild
            ON warning_thresholds(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS moderation_logs (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id               INTEGER NOT NULL,
                user_id                INTEGER NOT NULL,
                user_display_name      TEXT NOT NULL,
                user_avatar_url        TEXT,
                moderator_id           INTEGER NOT NULL,
                moderator_display_name TEXT NOT NULL,
                action                 TEXT NOT NULL,
                reason                 TEXT,
                source                 TEXT NOT NULL,
                extra_actions          TEXT,
                duration_minutes       INTEGER,
                evidence_url           TEXT,
                deleted                INTEGER DEFAULT 0,
                deleted_by             INTEGER,
                deleted_at             TIMESTAMP,
                created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_guild
            ON moderation_logs(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_user
            ON moderation_logs(user_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ml_date
            ON moderation_logs(created_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_config (
                guild_id               INTEGER PRIMARY KEY,
                enabled                INTEGER DEFAULT 1,
                xp_per_word            INTEGER DEFAULT 1,
                xp_min_per_message     INTEGER DEFAULT 5,
                xp_max_per_message     INTEGER DEFAULT 50,
                xp_cooldown_seconds    INTEGER DEFAULT 30,
                voice_xp_enabled       INTEGER DEFAULT 1,
                voice_xp_per_minute    INTEGER DEFAULT 3,
                voice_require_unmuted  INTEGER DEFAULT 1,
                spam_detection_enabled INTEGER DEFAULT 1,
                spam_xp_penalty        INTEGER DEFAULT 10,
                spam_threshold         INTEGER DEFAULT 3,
                levelup_announce       INTEGER DEFAULT 1,
                levelup_channel_id     INTEGER,
                levelup_message        TEXT,
                levelup_embed_data     TEXT,
                remove_old_reward_role INTEGER DEFAULT 0,
                updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_rewards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                level      INTEGER NOT NULL,
                role_id    INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lr_guild
            ON leveling_rewards(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_bonus_roles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                role_id    INTEGER NOT NULL,
                multiplier REAL DEFAULT 1.5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lbr_guild
            ON leveling_bonus_roles(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_blacklist_roles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                role_id    INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS mvp_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                user_id           INTEGER NOT NULL,
                user_display_name TEXT NOT NULL,
                cycle_start       TIMESTAMP NOT NULL,
                cycle_end         TIMESTAMP NOT NULL,
                score             INTEGER NOT NULL,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mvph_guild
            ON mvp_history(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS boost_color_roles (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id             INTEGER NOT NULL,
                role_id              INTEGER NOT NULL,
                role_name            TEXT NOT NULL,
                requires_boost_level INTEGER DEFAULT 2,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER NOT NULL,
                name             TEXT NOT NULL,
                description      TEXT,
                price            INTEGER NOT NULL,
                type             TEXT NOT NULL,
                role_id          INTEGER,
                duration_hours   INTEGER,
                show_button      INTEGER DEFAULT 1,
                limited          INTEGER DEFAULT 0,
                limited_until    TIMESTAMP,
                max_stock        INTEGER,
                current_stock    INTEGER,
                featured         INTEGER DEFAULT 0,
                required_level   INTEGER DEFAULT 0,
                required_role_id INTEGER,
                enabled          INTEGER DEFAULT 1,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_si_guild
            ON shop_items(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchase_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                user_id           INTEGER NOT NULL,
                user_display_name TEXT NOT NULL,
                item_id           INTEGER NOT NULL,
                item_name         TEXT NOT NULL,
                price_paid        INTEGER NOT NULL,
                purchased_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at        TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ph_guild
            ON purchase_history(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ph_user
            ON purchase_history(user_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS temp_roles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                role_id     INTEGER NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at  TIMESTAMP NOT NULL,
                source      TEXT DEFAULT 'shop'
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tr_expires
            ON temp_roles(expires_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id              INTEGER NOT NULL,
                title                 TEXT NOT NULL,
                description           TEXT,
                type                  TEXT NOT NULL,
                reward_type           TEXT NOT NULL,
                reward_value          TEXT NOT NULL,
                reward_duration_hours INTEGER,
                max_winners           INTEGER DEFAULT 3,
                channel_id            INTEGER,
                schedule_type         TEXT DEFAULT 'manual',
                schedule_time         TIMESTAMP,
                random_min_hours      INTEGER,
                random_max_hours      INTEGER,
                embed_data            TEXT,
                enabled               INTEGER DEFAULT 1,
                created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ev_guild
            ON events(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS event_winners (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id          INTEGER NOT NULL,
                guild_id          INTEGER NOT NULL,
                user_id           INTEGER NOT NULL,
                user_display_name TEXT NOT NULL,
                won_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS youtube_config (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            INTEGER NOT NULL,
                youtube_channel_url TEXT NOT NULL,
                youtube_channel_id  TEXT,
                discord_channel_id  INTEGER NOT NULL,
                custom_message      TEXT,
                embed_data          TEXT,
                ping_role_id        INTEGER,
                check_interval_min  INTEGER DEFAULT 10,
                last_video_id       TEXT,
                enabled             INTEGER DEFAULT 1,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS twitch_config (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id            INTEGER NOT NULL,
                twitch_username     TEXT NOT NULL,
                discord_channel_id  INTEGER NOT NULL,
                custom_message      TEXT DEFAULT '🔴 {streamer} is LIVE!',
                embed_data          TEXT,
                ping_role_id        INTEGER,
                give_role_id        INTEGER,
                role_duration_hours INTEGER,
                is_live             INTEGER DEFAULT 0,
                enabled             INTEGER DEFAULT 1,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                permission_level TEXT NOT NULL,
                added_by         INTEGER,
                added_by_name    TEXT,
                enabled          INTEGER DEFAULT 1,
                added_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_du_guild
            ON dashboard_users(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_du_user
            ON dashboard_users(user_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                user_id           INTEGER NOT NULL,
                user_display_name TEXT NOT NULL,
                target_id         INTEGER,
                target_name       TEXT,
                action            TEXT NOT NULL,
                details           TEXT,
                page              TEXT,
                ip_address        TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_al_guild
            ON audit_log(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_al_date
            ON audit_log(created_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS command_toggles (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER NOT NULL,
                command_name     TEXT NOT NULL,
                enabled          INTEGER DEFAULT 1,
                allowed_roles    TEXT,
                allowed_channels TEXT,
                cooldown_seconds INTEGER DEFAULT 0,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ct_guild
            ON command_toggles(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        INTEGER NOT NULL,
                channel_id      INTEGER NOT NULL,
                message_text    TEXT,
                embed_data      TEXT,
                send_at         TIMESTAMP NOT NULL,
                repeat_type     TEXT,
                repeat_interval INTEGER,
                last_sent       TIMESTAMP,
                enabled         INTEGER DEFAULT 1,
                created_by      INTEGER NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS backup_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT NOT NULL,
                size_bytes INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()

    await ensure_owner_access()
    print("Database initialized — all tables ready")
    print(f"Owner access ensured for user ID: {OWNER_DISCORD_ID}")


async def ensure_owner_access():
    """
    Guarantees OWNER_DISCORD_ID has dashboard owner access
    for every known guild, including FALLBACK_GUILD_IDS.
    Safe to run on every startup.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        guild_ids = set()

        # Collect from DB
        for table in ["levels", "economy", "warnings", "tickets",
                      "mvp_scores", "mod_logs", "boost_config",
                      "mvp_config", "guild_settings"]:
            try:
                cursor = await db.execute(
                    f"SELECT DISTINCT guild_id FROM {table} "
                    f"WHERE guild_id IS NOT NULL"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    if row[0]:
                        guild_ids.add(int(row[0]))
            except Exception:
                pass

        # Always include your known servers
        for gid in FALLBACK_GUILD_IDS:
            guild_ids.add(gid)

        for gid in guild_ids:
            await db.execute("""
                INSERT OR IGNORE INTO dashboard_users
                    (guild_id, user_id, permission_level,
                     added_by_name, enabled)
                VALUES (?, ?, 'owner', 'auto-setup', 1)
            """, (gid, OWNER_DISCORD_ID))

        await db.commit()
        print(f"Owner access confirmed for guilds: {guild_ids}")


async def add_guild_owner(guild_id: int):
    """Called from main.py on_guild_join for instant access on new servers."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO dashboard_users
                (guild_id, user_id, permission_level,
                 added_by_name, enabled)
            VALUES (?, ?, 'owner', 'auto-setup', 1)
        """, (guild_id, OWNER_DISCORD_ID))
        await db.commit()
    print(f"Owner access granted for new guild: {guild_id}")


if __name__ == "__main__":
    asyncio.run(init_db())
