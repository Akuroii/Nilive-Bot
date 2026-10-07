"""One-time Leveling config migration and Dashboard-default regressions.

This creates a disposable legacy schema and exercises the same migration helper
used by database.init_db(); it does not inspect or claim to migrate a deployed DB.
Run with:
    python scripts/test_leveling_config_migration.py
"""
import asyncio

import aiosqlite
from phase1_support import DB_PATH, GUILD, execute, reset_database, rows


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


async def install_legacy_schema():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DROP TABLE leveling_config")
        await db.execute("DELETE FROM leveling_config_migrations")
        await db.execute("""
            CREATE TABLE leveling_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                xp_per_word INTEGER DEFAULT 1,
                xp_min_per_message INTEGER DEFAULT 5,
                xp_max_per_message INTEGER DEFAULT 50,
                xp_cooldown_seconds INTEGER DEFAULT 20,
                voice_xp_enabled INTEGER DEFAULT 1,
                voice_xp_per_minute INTEGER DEFAULT 3,
                voice_require_unmuted INTEGER DEFAULT 1,
                spam_detection_enabled INTEGER DEFAULT 1,
                spam_xp_penalty INTEGER DEFAULT 10,
                spam_threshold INTEGER DEFAULT 3,
                spam_window_seconds INTEGER DEFAULT 10,
                levelup_announce INTEGER DEFAULT 1,
                levelup_channel_id INTEGER,
                levelup_message TEXT,
                levelup_embed_data TEXT,
                remove_old_reward_role INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            INSERT INTO leveling_config
                (guild_id, enabled, xp_cooldown_seconds, spam_threshold,
                 spam_window_seconds, spam_xp_penalty)
            VALUES (?, 0, 20, 3, 10, 10)
        """, (GUILD,))
        await db.execute("""
            INSERT INTO leveling_config
                (guild_id, enabled, xp_cooldown_seconds, spam_threshold,
                 spam_window_seconds, spam_xp_penalty, xp_per_word)
            VALUES (?, 1, 37, 5, 45, 75, 4)
        """, (GUILD + 1,))
        await db.commit()


async def main():
    await reset_database()
    await install_legacy_schema()
    from database import LEVELING_CONFIG_BASELINE_MIGRATION, migrate_leveling_config

    async with aiosqlite.connect(DB_PATH) as db:
        await migrate_leveling_config(db)

    columns = {row[1] for row in rows("PRAGMA table_info(leveling_config)")}
    check("Message XP replaces the master gate; old fixed penalty is preserved but inert",
          "enabled" not in columns and "spam_xp_penalty" in columns
          and {"message_xp_enabled", "spam_xp_penalty_divisor"} <= columns)
    baseline = rows("SELECT message_xp_enabled,xp_cooldown_seconds,spam_threshold,"
                    "spam_window_seconds,spam_xp_penalty_divisor,spam_xp_penalty "
                    "FROM leveling_config WHERE guild_id=?", (GUILD,))[0]
    check("legacy cooldown/window migrate once; spam threshold/fixed value are preserved",
          baseline == (0, 10, 3, 20, 1000, 10), str(baseline))
    custom = rows("SELECT message_xp_enabled,xp_cooldown_seconds,spam_threshold,"
                  "spam_window_seconds,spam_xp_penalty_divisor,xp_per_word,"
                  "spam_xp_penalty FROM leveling_config WHERE guild_id=?",
                  (GUILD + 1,))[0]
    check("the baseline migration resets only cooldown/window for every guild",
          custom == (1, 10, 5, 20, 1000, 4, 75), str(custom))
    from utils.xp_calculator import get_leveling_config
    effective_custom = await get_leveling_config(GUILD + 1)
    check("legacy fixed penalty is not exposed to runtime config",
          "spam_xp_penalty" not in effective_custom
          and effective_custom["spam_threshold"] == 5,
          str(effective_custom))

    marker = rows("SELECT migration_name FROM leveling_config_migrations")
    check("the one-time migration marker is recorded",
          marker == [(LEVELING_CONFIG_BASELINE_MIGRATION,)], str(marker))

    # Existing deployments need the rebuilt SQL defaults too, not just the
    # helper fallbacks. A guild row inserted later without explicit values now
    # gets the new defaults, and a later startup cannot overwrite edits.
    execute("INSERT INTO leveling_config (guild_id) VALUES (?)", (GUILD + 2,))
    new_row = rows("SELECT message_xp_enabled,xp_cooldown_seconds,spam_threshold,"
                   "spam_window_seconds,spam_xp_penalty_divisor "
                   "FROM leveling_config WHERE guild_id=?", (GUILD + 2,))[0]
    check("new rows on an upgraded database use 10s/20s/threshold-10 defaults",
          new_row == (1, 10, 10, 20, 1000), str(new_row))

    execute("UPDATE leveling_config SET xp_cooldown_seconds=31,spam_threshold=7,"
            "spam_window_seconds=40 WHERE guild_id=?", (GUILD,))
    async with aiosqlite.connect(DB_PATH) as db:
        await migrate_leveling_config(db)
    after_restart = rows("SELECT xp_cooldown_seconds,spam_threshold,spam_window_seconds "
                         "FROM leveling_config WHERE guild_id=?", (GUILD,))[0]
    check("re-running startup migration never overwrites Dashboard choices",
          after_restart == (31, 7, 40), str(after_restart))

    from utils.xp_calculator import LEVELING_CONFIG_DEFAULTS, get_leveling_config
    fresh = await get_leveling_config(GUILD + 99)
    check("unconfigured/new guild effective defaults match SQL baselines",
          fresh["xp_cooldown_seconds"] == 10
          and fresh["spam_threshold"] == 10
          and fresh["spam_window_seconds"] == 20
          and LEVELING_CONFIG_DEFAULTS["spam_xp_penalty_divisor"] == 1000,
          str(fresh))

    print("ALL LEVELING CONFIG MIGRATION CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail:
        raise SystemExit(1)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
