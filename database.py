import aiosqlite
import asyncio
import os

DB_PATH = os.getenv("DATABASE_PATH", "/app/data/nero.db")

OWNER_DISCORD_ID = int(os.getenv("OWNER_ID", "704453350384730237"))

FALLBACK_GUILD_IDS = [
    1360461358486913145,
]


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:

        # PERFORMANCE FIX (dark-fixes pass #1): every cog/route in this
        # project opens a brand-new aiosqlite connection per query
        # (dozens of call sites, by design). SQLite's default
        # rollback-journal mode serializes ALL readers behind a
        # writer, so under real concurrent load (message XP + voice XP
        # ticks + dashboard requests landing at once) that
        # connection-churn pattern starts throwing "database is
        # locked" errors. WAL mode lets readers proceed while a writer
        # holds the lock — set once here, since WAL IS a persistent,
        # file-level setting: every subsequent connection to this
        # file, bot or dashboard, inherits it.
        #
        # CORRECTION (pass #2 review): busy_timeout, unlike
        # journal_mode, is NOT a persistent file-level setting — it's
        # per-connection. Setting it here only affects this one
        # connection, not the dozens of others opened elsewhere via
        # plain aiosqlite.connect(DB_PATH). This isn't a live bug only
        # because Python's sqlite3 (which aiosqlite wraps) already
        # defaults new connections to a 5-second busy timeout on its
        # own — so those other connections get equivalent behavior by
        # accident, not because of this pragma. Left in place since
        # it's harmless and self-documenting; just don't rely on this
        # line alone if that default ever changes.
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
        except Exception as e:
            print(f"[DB] Failed to set WAL mode / busy_timeout: {e}")

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

        # CLEANUP (dark-fixes pass #2): voice_sessions dropped from
        # schema — confirmed zero read/write references anywhere in
        # cogs/ or dashboard/ (only comments referencing the old name
        # remain, e.g. cogs/mvp.py). Superseded entirely by the
        # tick-based activity engine (on_activity_voice_tick), which
        # doesn't need join/leave bookkeeping. Not DROPping the table
        # from any already-deployed DB file here (that's a manual,
        # explicit operator action) — this just stops a fresh DB from
        # ever creating it again.

        await db.execute("""
            CREATE TABLE IF NOT EXISTS activity_stats (
                guild_id          INTEGER,
                user_id           INTEGER,
                date              TEXT,
                messages_count    INTEGER DEFAULT 0,
                words_count       INTEGER DEFAULT 0,
                voice_minutes     REAL DEFAULT 0,
                forum_posts_count INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, date)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_guild
            ON activity_stats(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_guild_date
            ON activity_stats(guild_id, date)
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

        # Phase 5 / Prestige system: prestige tier lives on the same
        # per-member row as xp/level rather than a separate table —
        # it's a third dimension of the same "how far has this member
        # progressed" state, and every existing query that already
        # SELECTs from levels by (guild_id, user_id) only needs one
        # more column, not a join, to also know prestige tier.
        try:
            cursor = await db.execute("PRAGMA table_info(levels)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "prestige" not in cols:
                await db.execute(
                    "ALTER TABLE levels ADD COLUMN prestige INTEGER DEFAULT 0")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] levels.prestige: {e}")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS economy (
                guild_id INTEGER,
                user_id  INTEGER,
                balance  INTEGER DEFAULT 0,
                diamonds INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        try:
            cursor = await db.execute("PRAGMA table_info(economy)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "diamonds" not in cols:
                await db.execute(
                    "ALTER TABLE economy ADD COLUMN diamonds INTEGER DEFAULT 0")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] economy.diamonds: {e}")

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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER,
                channel_id    INTEGER,
                user_id       INTEGER,
                staff_role_id INTEGER,
                status        TEXT DEFAULT 'open',
                category      TEXT,
                claimed_by    INTEGER,
                tags          TEXT,
                created_at    TEXT
            )
        """)

        try:
            cursor = await db.execute("PRAGMA table_info(tickets)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "staff_role_id" not in cols:
                await db.execute(
                    "ALTER TABLE tickets ADD COLUMN staff_role_id INTEGER")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] tickets.staff_role_id: {e}")

        # CLEANUP (dark-fixes pass #2): ticket_config dropped from
        # schema — confirmed zero read/write references anywhere (only
        # comments remain, e.g. cogs/tickets.py's P1 #10 fix note).
        # /ticket_setup writes to ticket_settings now; this legacy
        # table was never populated by any current code path. See
        # voice_sessions cleanup note above for the same reasoning re:
        # not touching already-deployed DB files here.

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

        # PHASE 5 TAIL FIX (reaction_role_expiry sentinel collision):
        # this table used to be keyed by (guild_id, user_id, role_id)
        # only. That let the SAME role, added to two different
        # reaction-role panels with two different expiry_days values,
        # collide: the second /reactionrole_add's sentinel row
        # (user_id=0, "this role's template expiry") silently
        # overwrote the first panel's, and a member claiming the role
        # from EITHER panel picked up whichever panel's expiry was
        # configured last. message_id is now part of the key so each
        # panel's copy of a role's expiry is tracked independently.
        #
        # message_id is NOT NULL DEFAULT 0 rather than nullable —
        # SQLite treats NULL as distinct-from-itself in a PRIMARY KEY,
        # which would silently defeat the "INSERT OR REPLACE dedupes
        # on conflict" behavior every read/write in cogs/reactionroles.py
        # relies on. 0 is never a real Discord message ID, so it's a
        # safe non-nullable default for legacy rows that predate this
        # column (see the migration block below).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_role_expiry (
                guild_id   INTEGER,
                user_id    INTEGER,
                role_id    INTEGER,
                message_id INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                PRIMARY KEY (guild_id, user_id, role_id, message_id)
            )
        """)
        try:
            cursor = await db.execute("PRAGMA table_info(reaction_role_expiry)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "message_id" not in cols:
                # Table predates message_id being part of the primary
                # key — SQLite can't ALTER a PK in place, so rebuild.
                # Legacy rows get message_id=0 (best we can do — the
                # panel they belonged to isn't recoverable from the
                # old schema); this only affects expiry rows written
                # before this fix shipped, and worst case is one
                # already-in-flight expiry falling back to "no known
                # panel" instead of being lost.
                await db.execute(
                    "ALTER TABLE reaction_role_expiry "
                    "RENAME TO reaction_role_expiry_old")
                await db.execute("""
                    CREATE TABLE reaction_role_expiry (
                        guild_id   INTEGER,
                        user_id    INTEGER,
                        role_id    INTEGER,
                        message_id INTEGER NOT NULL DEFAULT 0,
                        expires_at TEXT,
                        PRIMARY KEY (guild_id, user_id, role_id, message_id)
                    )
                """)
                await db.execute("""
                    INSERT INTO reaction_role_expiry
                        (guild_id, user_id, role_id, message_id, expires_at)
                    SELECT guild_id, user_id, role_id, 0, expires_at
                    FROM reaction_role_expiry_old
                """)
                await db.execute("DROP TABLE reaction_role_expiry_old")
                await db.commit()
                print("[MIGRATION] reaction_role_expiry rebuilt with "
                      "message_id in primary key")
        except Exception as e:
            print(f"[MIGRATION] reaction_role_expiry.message_id: {e}")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS rr_panels (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id             INTEGER,
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
                requires_reason   INTEGER DEFAULT 0,
                enabled           INTEGER DEFAULT 1,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER,
                trigger_words     TEXT,
                response_text     TEXT,
                response_embed    TEXT,
                response_type     TEXT DEFAULT 'text',
                match_type        TEXT DEFAULT 'contains',
                fuzzy_match       INTEGER DEFAULT 0,
                fuzzy_threshold   INTEGER DEFAULT 80,
                case_sensitive    INTEGER DEFAULT 0,
                response_chance   INTEGER DEFAULT 100,
                cooldown_seconds  INTEGER DEFAULT 0,
                allowed_channels  TEXT,
                enabled           INTEGER DEFAULT 1
            )
        """)

        try:
            cursor = await db.execute("PRAGMA table_info(triggers)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "fuzzy_threshold" not in cols:
                await db.execute(
                    "ALTER TABLE triggers ADD COLUMN fuzzy_threshold INTEGER DEFAULT 80")
            if "cooldown_seconds" not in cols:
                await db.execute(
                    "ALTER TABLE triggers ADD COLUMN cooldown_seconds INTEGER DEFAULT 0")
            await db.commit()
        except Exception as e:
            print(f"[MIGRATION] triggers.fuzzy_threshold/cooldown_seconds: {e}")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # CLEANUP (dark-fixes pass #2): disabled_commands dropped from
        # schema — confirmed zero references anywhere. Superseded by
        # command_toggles (see main.py, dashboard/app.py's
        # config_commands route), which is the table actually in use.

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
                expires_at             TIMESTAMP,
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

        try:
            cursor = await db.execute("PRAGMA table_info(guild_settings)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "diamond_exchange_rate" not in cols:
                await db.execute(
                    "ALTER TABLE guild_settings ADD COLUMN "
                    "diamond_exchange_rate INTEGER DEFAULT 500")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] guild_settings.diamond_exchange_rate: {e}")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings_kv (
                guild_id   INTEGER NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, key)
            )
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

        try:
            cursor = await db.execute("PRAGMA table_info(leveling_config)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "spam_window_seconds" not in cols:
                await db.execute(
                    "ALTER TABLE leveling_config ADD COLUMN spam_window_seconds INTEGER DEFAULT 10")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] leveling_config.spam_window_seconds: {e}")

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
            CREATE TABLE IF NOT EXISTS leveling_currency_rewards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                level      INTEGER NOT NULL,
                currency   TEXT NOT NULL DEFAULT 'balance',
                amount     INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lcr_guild
            ON leveling_currency_rewards(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_reset_config (
                guild_id   INTEGER PRIMARY KEY,
                enabled    INTEGER DEFAULT 0,
                period     TEXT DEFAULT 'weekly',
                last_reset TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_leaderboard_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                xp           INTEGER NOT NULL,
                level        INTEGER NOT NULL,
                rank         INTEGER NOT NULL,
                period       TEXT NOT NULL,
                period_end   TIMESTAMP NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_llh_guild
            ON leveling_leaderboard_history(guild_id, period_end)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS leveling_active_boosts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                multiplier REAL NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                source     TEXT DEFAULT 'shop',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lab_guild_user
            ON leveling_active_boosts(guild_id, user_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_lab_expires
            ON leveling_active_boosts(expires_at)
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

        # Phase 5 / Prestige system. One config row per guild
        # (min_level gate + on/off), plus one row per (guild, tier)
        # mapping a prestige tier number to the Discord role granted
        # for reaching it. Mirrors leveling_rewards' shape (a small
        # per-guild CRUD table of level/tier -> role_id) rather than
        # inventing a new pattern.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prestige_config (
                guild_id   INTEGER PRIMARY KEY,
                enabled    INTEGER DEFAULT 1,
                min_level  INTEGER DEFAULT 50,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS prestige_roles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                tier       INTEGER NOT NULL,
                role_id    INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, tier)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pr_guild
            ON prestige_roles(guild_id)
        """)

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
            CREATE TABLE IF NOT EXISTS boost_config (
                guild_id               INTEGER PRIMARY KEY,
                enabled                INTEGER DEFAULT 1,
                boost1_role_id         INTEGER,
                boost2_role_id         INTEGER,
                boost2_channel_id      INTEGER,
                color_roles_enabled    INTEGER DEFAULT 0,
                auto_remove_on_unboost INTEGER DEFAULT 1
            )
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

        try:
            cursor = await db.execute("PRAGMA table_info(shop_items)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "price_diamonds" not in cols:
                await db.execute(
                    "ALTER TABLE shop_items ADD COLUMN "
                    "price_diamonds INTEGER DEFAULT NULL")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] shop_items.price_diamonds: {e}")

        try:
            cursor = await db.execute("PRAGMA table_info(shop_items)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "xp_boost_multiplier" not in cols:
                await db.execute(
                    "ALTER TABLE shop_items ADD COLUMN "
                    "xp_boost_multiplier REAL DEFAULT NULL")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] shop_items.xp_boost_multiplier: {e}")

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

        try:
            cursor = await db.execute("PRAGMA table_info(purchase_history)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "currency_paid" not in cols:
                await db.execute(
                    "ALTER TABLE purchase_history ADD COLUMN "
                    "currency_paid TEXT DEFAULT 'balance'")
                await db.commit()
        except Exception as e:
            print(f"[MIGRATION] purchase_history.currency_paid: {e}")

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
            CREATE TABLE IF NOT EXISTS transaction_ledger (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                currency         TEXT NOT NULL,
                amount           INTEGER NOT NULL,
                balance_after    INTEGER,
                type             TEXT NOT NULL,
                reason           TEXT,
                source           TEXT DEFAULT 'system',
                related_user_id  INTEGER,
                reversed         INTEGER DEFAULT 0,
                reversed_at      TIMESTAMP,
                reversed_by      INTEGER,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tl_guild
            ON transaction_ledger(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tl_guild_user
            ON transaction_ledger(guild_id, user_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tl_date
            ON transaction_ledger(created_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                item_name  TEXT NOT NULL,
                item_type  TEXT DEFAULT 'custom',
                quantity   INTEGER NOT NULL DEFAULT 0,
                metadata   TEXT,
                source     TEXT DEFAULT 'system',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, user_id, item_name)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_inv_guild_user
            ON inventory_items(guild_id, user_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS temp_bans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                banned_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at  TIMESTAMP NOT NULL,
                reason      TEXT,
                source      TEXT DEFAULT 'auto-threshold'
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tb_expires
            ON temp_bans(expires_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tb_guild_user
            ON temp_bans(guild_id, user_id)
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
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id              INTEGER PRIMARY KEY,
                enabled               INTEGER DEFAULT 1,
                max_per_user          INTEGER DEFAULT 1,
                auto_close_hours      INTEGER DEFAULT 0,
                save_transcripts      INTEGER DEFAULT 1,
                transcript_channel_id INTEGER,
                support_role_id       INTEGER,
                name_format           TEXT DEFAULT 'ticket-{number}',
                updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_categories (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                name              TEXT NOT NULL,
                emoji             TEXT DEFAULT '🎫',
                viewer_roles      TEXT DEFAULT '[]',
                closer_roles      TEXT DEFAULT '[]',
                auto_assign_roles TEXT DEFAULT '[]',
                open_embed        TEXT DEFAULT '{}',
                enabled           INTEGER DEFAULT 1,
                sort_order        INTEGER DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tc_guild
            ON ticket_categories(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_panels (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                name       TEXT,
                channel_id INTEGER,
                embed_data TEXT DEFAULT '{}',
                buttons    TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tp_guild
            ON ticket_panels(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_ratings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                ticket_id  INTEGER,
                user_id    INTEGER NOT NULL,
                rating     INTEGER NOT NULL,
                comment    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_trat_guild
            ON ticket_ratings(guild_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS report_config (
                guild_id          INTEGER PRIMARY KEY,
                enabled           INTEGER DEFAULT 0,
                report_channel_id INTEGER,
                staff_role_id     INTEGER,
                updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id           INTEGER NOT NULL,
                reporter_id        INTEGER NOT NULL,
                reporter_name      TEXT NOT NULL,
                reported_user_id   INTEGER NOT NULL,
                reported_user_name TEXT NOT NULL,
                message_id         INTEGER NOT NULL,
                channel_id         INTEGER NOT NULL,
                report_message_id  INTEGER,
                report_channel_id  INTEGER,
                message_content    TEXT,
                message_jump_url   TEXT,
                reason             TEXT,
                status             TEXT DEFAULT 'open',
                created_at         TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_guild
            ON reports(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_status
            ON reports(guild_id, status)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_status (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                started_at        TEXT,
                last_heartbeat    TEXT,
                guild_count       INTEGER DEFAULT 0,
                latency_ms        INTEGER DEFAULT 0,
                loaded_cogs       TEXT DEFAULT '[]',
                failed_cogs       TEXT DEFAULT '[]',
                last_error        TEXT,
                last_error_at     TEXT,
                discord_py_version TEXT,
                python_version    TEXT
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

        # SECURITY / DATA-INTEGRITY FIX (dark-fixes pass #7):
        # dashboard_users had NO unique constraint on (guild_id,
        # user_id) — only the surrogate `id` AUTOINCREMENT column was
        # unique. Two places relied on "INSERT OR IGNORE" actually
        # ignoring a duplicate (guild_id, user_id) pair:
        #   - ensure_owner_access(), called from init_db() on EVERY
        #     process start (bot AND dashboard both call init_db())
        #   - add_guild_owner(), called from on_guild_join and the
        #     select_guild() auto-grant path
        # Without a real UNIQUE index, "INSERT OR IGNORE" never had
        # anything to conflict against, so a new surrogate-keyed row
        # was silently inserted every single time — meaning the owner
        # accumulated a fresh duplicate `dashboard_users` row on every
        # bot/dashboard restart.
        #
        # This directly broke "Remove User" in the dashboard
        # (config_access()'s remove_user()): that route deletes by a
        # single surrogate `id`, so removing one duplicate row left
        # every other duplicate (still enabled=1, same guild_id +
        # user_id) granting that user access — access revocation
        # silently failed to actually revoke anything as soon as more
        # than one duplicate existed, which was virtually guaranteed
        # for the owner given the every-restart insert above.
        #
        # Migration: dedupe first (a real UNIQUE index creation fails
        # outright if duplicate rows already exist on disk), keeping
        # one row per (guild_id, user_id) — preferring 'owner' over
        # 'admin' over 'moderator' if levels ever differed across
        # duplicates, then the highest id (most recent) as a
        # tiebreaker — then create the UNIQUE index. From this point
        # forward every existing "INSERT OR IGNORE ... dashboard_users"
        # call site (ensure_owner_access, add_guild_owner) actually
        # ignores true duplicates instead of accumulating new rows,
        # and a single "Remove User" delete is guaranteed to be the
        # only row for that (guild_id, user_id) pair.
        try:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM (
                    SELECT guild_id, user_id FROM dashboard_users
                    GROUP BY guild_id, user_id HAVING COUNT(*) > 1
                )
            """)
            dup_groups = (await cursor.fetchone())[0]

            if dup_groups:
                await db.execute("""
                    DELETE FROM dashboard_users
                    WHERE id NOT IN (
                        SELECT id FROM (
                            SELECT id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY guild_id, user_id
                                       ORDER BY
                                           CASE permission_level
                                               WHEN 'owner' THEN 0
                                               WHEN 'admin' THEN 1
                                               WHEN 'moderator' THEN 2
                                               ELSE 3
                                           END ASC,
                                           id DESC
                                   ) AS rn
                            FROM dashboard_users
                        )
                        WHERE rn = 1
                    )
                """)
                await db.commit()
                print(f"[MIGRATION] dashboard_users: deduped "
                      f"{dup_groups} (guild_id, user_id) group(s) "
                      f"with duplicate rows")

            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_du_unique
                ON dashboard_users(guild_id, user_id)
            """)
            await db.commit()
        except Exception as e:
            print(f"[MIGRATION] dashboard_users unique index: {e}")

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
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id              INTEGER NOT NULL,
                command_name          TEXT NOT NULL,
                enabled               INTEGER DEFAULT 1,
                allowed_roles         TEXT,
                allowed_channels      TEXT,
                cooldown_seconds      INTEGER DEFAULT 0,
                aliases               TEXT,
                enabled_roles         TEXT,
                disabled_roles        TEXT,
                enabled_channels      TEXT,
                disabled_channels     TEXT,
                delete_user_msg       INTEGER DEFAULT 0,
                delete_bot_reply      INTEGER DEFAULT 0,
                delete_bot_after      INTEGER DEFAULT 0,
                custom_cooldown       TEXT,
                success_message       TEXT,
                error_message         TEXT,
                ephemeral             INTEGER DEFAULT 0,
                dm_response           INTEGER DEFAULT 0,
                bypass_cooldown_roles TEXT,
                require_permission    TEXT,
                owner_only            INTEGER DEFAULT 0,
                cmd_emoji             TEXT,
                category_color        TEXT,
                hide_from_help        INTEGER DEFAULT 0,
                updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    print("✅ Database initialized — all tables ready")
    print(f"✅ Owner access ensured for user ID: {OWNER_DISCORD_ID}")


async def ensure_owner_access():
    async with aiosqlite.connect(DB_PATH) as db:
        guild_ids = set()

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

        for gid in FALLBACK_GUILD_IDS:
            guild_ids.add(gid)

        for gid in guild_ids:
            # Now that idx_du_unique on (guild_id, user_id) exists,
            # this actually ignores an existing row instead of
            # inserting a fresh duplicate on every restart.
            await db.execute("""
                INSERT OR IGNORE INTO dashboard_users
                    (guild_id, user_id, permission_level,
                     added_by_name, enabled)
                VALUES (?, ?, 'owner', 'auto-setup', 1)
            """, (gid, OWNER_DISCORD_ID))

        await db.commit()
        print(f"✅ Owner access confirmed for {len(guild_ids)} guilds")


async def add_guild_owner(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Same fix as ensure_owner_access() above — relies on
        # idx_du_unique(guild_id, user_id) to actually dedupe.
        await db.execute("""
            INSERT OR IGNORE INTO dashboard_users
                (guild_id, user_id, permission_level,
                 added_by_name, enabled)
            VALUES (?, ?, 'owner', 'auto-setup', 1)
        """, (guild_id, OWNER_DISCORD_ID))
        await db.commit()
    print(f"✅ Owner access granted for new guild: {guild_id}")


if __name__ == "__main__":
    asyncio.run(init_db())
