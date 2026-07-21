import aiosqlite
from datetime import datetime, timezone, timedelta
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# MISSIONS (Phase 6) — v1
#
# Gate note: STATUS.md's locked sequencing said Missions comes "after
# Trade is verified live." Trade shipped but is unverified as of this
# build — Dark explicitly chose to build Missions now anyway
# (override). Flagging here so a future session reading this file
# doesn't assume the gate was silently ignored/forgotten.
#
# Own standalone schema (own ensure_tables(), not in database.py's
# init_db()) — same pattern cogs/minigames.py and utils/trade_engine.py
# already established for exactly this reason (isolated, low-risk
# additions that don't touch the shared migration path).
#
# Three trackable types for v1, all sourced from events
# cogs/activity_engine.py already dispatches — no new tracking hooks
# needed:
#   messages      — +1 per on_activity_message
#   words         — +word_count per on_activity_message
#   voice_minutes — +1 per on_activity_voice_tick
#
# Reward is granted the instant a mission crosses its target (no
# separate "claim" step) — reuses utils/reward_engine.give_reward(),
# the same single grant path every other reward source in this
# project uses (shop, events, minigames, leveling).
#
# Daily/weekly reset is implicit: progress is keyed by
# (guild_id, user_id, mission_id, period_key), and period_key changes
# every day/week — a new period just means a fresh row starting at 0,
# no cron/reset job needed. "once" missions use a fixed period_key so
# they can only ever be completed a single time per member.
# ═══════════════════════════════════════════════════════════════════════

VALID_TYPES = ("messages", "words", "voice_minutes")

VALID_PERIODS = ("daily", "weekly", "once")
VALID_REWARD_TYPES = ("coins", "diamonds", "xp", "role", "temp_role", "item")


async def ensure_tables():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS missions_definitions (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id               INTEGER NOT NULL,
                name                   TEXT NOT NULL,
                description            TEXT,
                type                   TEXT NOT NULL,
                target                 INTEGER NOT NULL,
                period                 TEXT NOT NULL DEFAULT 'daily',
                reward_type            TEXT NOT NULL,
                reward_value           TEXT NOT NULL,
                reward_duration_hours  INTEGER,
                enabled                INTEGER DEFAULT 1,
                created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_md_guild
            ON missions_definitions(guild_id)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mission_progress (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                mission_id   INTEGER NOT NULL,
                period_key   TEXT NOT NULL,
                progress     INTEGER NOT NULL DEFAULT 0,
                completed    INTEGER NOT NULL DEFAULT 0,
                completed_at TIMESTAMP,
                UNIQUE(guild_id, user_id, mission_id, period_key)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mp_guild_mission_period
            ON mission_progress(guild_id, mission_id, period_key)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mp_guild_user
            ON mission_progress(guild_id, user_id)
        """)
        await db.commit()


def _monday_of(d) -> str:
    day = d.date() if hasattr(d, "date") else d
    return (day - timedelta(days=day.weekday())).isoformat()


def get_period_key(period: str, now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    if period == "weekly":
        return _monday_of(now)
    if period == "once":
        return "once"
    return now.date().isoformat()  # daily (also the fallback default)


async def get_definitions(guild_id: int, enabled_only: bool = True) -> list[dict]:
    query = "SELECT * FROM missions_definitions WHERE guild_id = ?"
    if enabled_only:
        query += " AND enabled = 1"
    query += " ORDER BY id ASC"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, (guild_id,))
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


async def get_user_progress(guild_id: int, user_id: int) -> list[dict]:
    """
    Returns every enabled mission for the guild alongside this
    member's progress for its CURRENT period (a fresh 0/target row if
    they haven't touched it yet this period — nothing is written
    until record_activity() actually has progress to add).
    """
    defs = await get_definitions(guild_id, enabled_only=True)
    if not defs:
        return []
    now = datetime.now(timezone.utc)
    result = []
    async with aiosqlite.connect(DB_PATH) as db:
        for d in defs:
            period_key = get_period_key(d["period"], now)
            cursor = await db.execute("""
                SELECT progress, completed, completed_at FROM mission_progress
                WHERE guild_id=? AND user_id=? AND mission_id=? AND period_key=?
            """, (guild_id, user_id, d["id"], period_key))
            row = await cursor.fetchone()
            progress = row[0] if row else 0
            completed = bool(row[1]) if row else False
            completed_at = row[2] if row else None
            result.append({
                **d, "period_key": period_key,
                "progress": progress, "completed": completed,
                "completed_at": completed_at,
            })
    return result


async def record_activity(bot, guild_id: int, user_id: int,
                           mtype: str, amount: int) -> list[dict]:
    """
    Adds `amount` progress to every enabled mission of type `mtype` in
    this guild for this member's current period, granting the reward
    the instant a mission crosses its target. Each mission definition
    is updated in its own BEGIN IMMEDIATE transaction (mirrors
    utils/reward_engine.py's xp branch) so two ticks landing close
    together for the same member/mission can't both read
    pre-completion progress and both grant the reward.

    Returns the list of mission dicts that were newly completed by
    this call (for a caller that wants to announce it — v1's cog
    doesn't, but the hook is here for later).
    """
    if amount <= 0 or mtype not in VALID_TYPES:
        return []

    defs = await get_definitions(guild_id, enabled_only=True)
    matching = [d for d in defs if d["type"] == mtype]
    if not matching:
        return []

    now = datetime.now(timezone.utc)
    newly_completed = []

    for d in matching:
        period_key = get_period_key(d["period"], now)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    SELECT progress, completed FROM mission_progress
                    WHERE guild_id=? AND user_id=? AND mission_id=? AND period_key=?
                """, (guild_id, user_id, d["id"], period_key))
                row = await cursor.fetchone()
                old_progress = row[0] if row else 0
                already_done = bool(row[1]) if row else False

                if already_done:
                    # Already completed this period — nothing left to
                    # do (progress is capped, no partial re-completion).
                    await db.execute("ROLLBACK")
                    continue

                new_progress = old_progress + amount
                just_completed = new_progress >= d["target"]

                await db.execute("""
                    INSERT INTO mission_progress
                        (guild_id, user_id, mission_id, period_key,
                         progress, completed, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, mission_id, period_key)
                    DO UPDATE SET
                        progress     = excluded.progress,
                        completed    = excluded.completed,
                        completed_at = excluded.completed_at
                """, (
                    guild_id, user_id, d["id"], period_key,
                    new_progress, int(just_completed),
                    now.isoformat() if just_completed else None,
                ))
                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

        if just_completed:
            from utils.reward_engine import give_reward
            result = await give_reward(
                bot, guild_id, user_id, d["reward_type"],
                amount=d["reward_value"] if d["reward_type"] in ("coins", "diamonds", "xp") else None,
                role_id=d["reward_value"] if d["reward_type"] in ("role", "temp_role") else None,
                item_name=d["reward_value"] if d["reward_type"] == "item" else None,
                duration_hours=d.get("reward_duration_hours"),
                reason=f"Mission complete: {d['name']}",
                source="mission",
            )
            if not result.get("success"):
                print(f"[MISSIONS] Reward grant failed for mission "
                      f"{d['id']} ({d['name']}) guild={guild_id} "
                      f"user={user_id}: {result.get('error')}")
            newly_completed.append(d)

    return newly_completed


async def get_recent_completions(guild_id: int, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT mp.user_id, mp.period_key, mp.completed_at,
                   md.id, md.name, md.type, md.target,
                   md.reward_type, md.reward_value
            FROM mission_progress mp
            JOIN missions_definitions md ON mp.mission_id = md.id
            WHERE mp.guild_id = ? AND mp.completed = 1
            ORDER BY mp.completed_at DESC LIMIT ?
        """, (guild_id, limit))
        rows = await cursor.fetchall()
    return [{
        "user_id": r[0], "period_key": r[1], "completed_at": r[2],
        "mission_id": r[3], "mission_name": r[4], "type": r[5],
        "target": r[6], "reward_type": r[7], "reward_value": r[8],
    } for r in rows]
