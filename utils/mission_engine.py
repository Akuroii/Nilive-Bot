import aiosqlite
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.timezone import (
    get_cairo_daily_key, get_cairo_weekly_key,
    seconds_until_cairo_midnight, seconds_until_cairo_saturday,
    format_cairo_countdown, CAIRO_TZ,
)

# ═══════════════════════════════════════════════════════════════════════
# MISSIONS (Phase 6) — v2
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
# Trackable types, all sourced from events cogs/activity_engine.py
# already dispatches — no new tracking hooks needed:
#   messages          — +1 per on_activity_message
#   words             — +word_count per on_activity_message
#   voice_minutes     — +1 per on_activity_voice_tick
#   daily_completions — +1 per daily-period mission completed (chained
#                       from record_activities itself, so a weekly
#                       "Complete 5 daily missions" mission is just a
#                       normal definition; no separate subsystem)
#
# v2 additions (per the locked spec):
#   * Optional per-mission channel restriction (missions_definitions
#     .channel_id, NULL = counts anywhere). Existing rows migrate to
#     NULL and therefore keep their old "everywhere" behavior.
#   * record_activities() batches multiple counters from ONE event
#     (messages + words of the same chat message) into one definitions
#     query and one SQLite connection, instead of the v1 pattern of one
#     full record_activity() pass per counter.
#   * create/delete/toggle definitions live here (one validation +
#     write path) instead of being duplicated between the cog's slash
#     command and the dashboard API.
#   * Reset countdown helpers (display-only — the reset itself stays
#     the implicit period_key rollover, no timer job exists or is
#     needed).
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

VALID_TYPES = ("messages", "words", "voice_minutes", "daily_completions")

VALID_PERIODS = ("daily", "weekly", "once")
VALID_REWARD_TYPES = ("coins", "diamonds", "xp", "role", "temp_role", "item")

# Types whose progress comes from an event that happened in a specific
# channel, and can therefore be restricted to one configured channel.
# daily_completions is triggered by mission completions (server-wide
# events, not channel-bound), so a channel restriction is meaningless
# for it and is ignored at counting time.
CHANNEL_BOUND_TYPES = ("messages", "words", "voice_minutes")

# Display-length caps, enforced at creation so no definition can grow
# past what the member display can safely render. These match the
# dashboard form's maxlengths — the slash command is the only other
# entry point, and both now agree. (v1 had no explicit cap; its
# display crashed on names over Discord's 256-char embed-field limit,
# so this is strictly tighter than what ever actually worked.)
MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 200


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
                channel_id             INTEGER,
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
        # v2 migration: optional per-mission channel restriction. New
        # databases get the column from CREATE TABLE above; databases
        # created by v1 need the ALTER. Same idempotent PRAGMA guard
        # pattern database.py uses for guild_settings — and the same
        # tolerance: if the bot process and a dashboard request race
        # the very first migration, the loser's ALTER fails with
        # "duplicate column" and is safely swallowed, because the
        # column existing is all this migration needed. NULL (the
        # ALTER default) means "counts anywhere", so every pre-v2
        # mission keeps exactly its old behavior.
        try:
            cursor = await db.execute("PRAGMA table_info(missions_definitions)")
            cols = [c[1] for c in await cursor.fetchall()]
            if "channel_id" not in cols:
                await db.execute(
                    "ALTER TABLE missions_definitions ADD COLUMN channel_id INTEGER")
        except Exception as e:
            print(f"[MISSIONS] channel_id migration: {e}")
        await db.commit()


def _saturday_of_cairo(dt: datetime) -> str:
    """Saturday starting the Cairo week containing dt."""
    return get_cairo_weekly_key(dt)


def get_period_key(period: str, now: datetime = None) -> str:
    # Calendar boundaries are Cairo (bot-wide canonical).
    # Storage stays UTC, but period keys are Cairo dates.
    now = now or datetime.now(timezone.utc)
    if period == "weekly":
        return get_cairo_weekly_key(now)
    if period == "once":
        return "once"
    return get_cairo_daily_key(now)


# ── Reset countdowns (display-only) ────────────────────────────────────
# The reset itself is the period_key rollover above; these helpers just
# answer "how long until the current period ends" for the member-facing
# display. Boundaries are Cairo (daily 00:00 Africa/Cairo, weekly
# Saturday 00:00 Africa/Cairo). Storage stays UTC.

def seconds_until_daily_reset(now: datetime = None) -> int:
    return seconds_until_cairo_midnight(now)


def seconds_until_weekly_reset(now: datetime = None) -> int:
    return seconds_until_cairo_saturday(now)


def format_reset_countdown(seconds: int) -> str:
    """H:MMH — e.g. '1:12H', '0:05H', '24:00H'. Never 0:00H while time remains."""
    return format_cairo_countdown(seconds)


# ── Definitions CRUD (single validation + write path — the cog's slash
#    commands and the dashboard API both call these, so the two surfaces
#    can't drift apart like v1's duplicated INSERTs could) ─────────────

def _validate_reward_value(reward_type: str, reward_value: str) -> str:
    if reward_type in ("coins", "diamonds", "xp"):
        try:
            amount = int(reward_value)
        except (TypeError, ValueError):
            raise ValueError(
                f"reward_value must be a number for {reward_type} rewards")
        if amount <= 0:
            raise ValueError(
                f"reward_value must be positive for {reward_type} rewards")
    elif reward_type in ("role", "temp_role"):
        try:
            int(reward_value)
        except (TypeError, ValueError):
            raise ValueError(
                "reward_value must be a Role ID for role rewards")
    return reward_value


async def create_definition(guild_id: int, *, name: str, mtype: str,
                            target: int, reward_type: str, reward_value: str,
                            period: str = "daily", description: str = None,
                            reward_duration_hours=None,
                            channel_id=None) -> int:
    """Validates and inserts one mission definition, returning its id.

    Raises ValueError with a member-facing message on bad input — the
    slash command turns that into an ephemeral reply, the API into a
    400. This is the only place mission input shape is enforced."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(
            f"name must be {MAX_NAME_LENGTH} characters or fewer")

    mtype = (mtype or "").lower().strip()
    if mtype not in VALID_TYPES:
        raise ValueError(f"type must be one of: {', '.join(VALID_TYPES)}")

    period = (period or "daily").lower().strip()
    if period not in VALID_PERIODS:
        raise ValueError(f"period must be one of: {', '.join(VALID_PERIODS)}")

    if reward_type not in VALID_REWARD_TYPES:
        raise ValueError(
            f"reward_type must be one of: {', '.join(VALID_REWARD_TYPES)}")

    reward_value = str(reward_value).strip() if reward_value is not None else ""
    if not reward_value:
        raise ValueError("reward_value is required")
    reward_value = _validate_reward_value(reward_type, reward_value)

    try:
        target = int(target)
    except (TypeError, ValueError):
        raise ValueError("target must be a number")
    if target <= 0:
        raise ValueError("target must be positive")

    duration_hours = reward_duration_hours
    if duration_hours not in (None, ""):
        try:
            duration_hours = int(duration_hours)
        except (TypeError, ValueError):
            raise ValueError("reward_duration_hours must be a number")
        if duration_hours <= 0:
            raise ValueError("reward_duration_hours must be positive")
    else:
        duration_hours = None

    # daily_completions progress isn't channel-bound (see
    # CHANNEL_BOUND_TYPES), so a channel restriction would be a silent
    # no-op — drop it rather than storing a setting that can't work.
    if mtype == "daily_completions":
        channel_id = None
    elif channel_id in (None, ""):
        channel_id = None
    else:
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            raise ValueError("channel_id must be a channel ID")

    description = (description or "").strip() or None
    if description and len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"description must be {MAX_DESCRIPTION_LENGTH} characters or fewer")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO missions_definitions
                (guild_id, name, description, type, target, period,
                 reward_type, reward_value, reward_duration_hours, channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, name, description, mtype, target, period,
              reward_type, reward_value, duration_hours, channel_id))
        await db.commit()
        return cursor.lastrowid


async def delete_definition(guild_id: int, mission_id: int) -> bool:
    """Deletes a definition. Returns False when no such mission exists
    in this guild, so both admin surfaces can say so honestly instead
    of reporting success for a typo'd id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM missions_definitions WHERE id=? AND guild_id=?",
            (mission_id, guild_id))
        await db.commit()
        return cursor.rowcount > 0


async def set_definition_enabled(guild_id: int, mission_id: int,
                                 enabled: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE missions_definitions SET enabled=? "
            "WHERE id=? AND guild_id=?",
            (int(bool(enabled)), mission_id, guild_id))
        await db.commit()
        return cursor.rowcount > 0


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
    they haven't touched it yet this period — nothing is written until
    record_activity() actually has progress to add).

    v2: one progress query for the whole member (period keys are at
    most three distinct values — daily/weekly/once) instead of one
    query per mission definition.
    """
    defs = await get_definitions(guild_id, enabled_only=True)
    if not defs:
        return []
    now = datetime.now(timezone.utc)
    key_by_mission = {d["id"]: get_period_key(d["period"], now) for d in defs}
    period_keys = list(set(key_by_mission.values()))

    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" * len(period_keys))
        cursor = await db.execute(f"""
            SELECT mission_id, period_key, progress, completed, completed_at
            FROM mission_progress
            WHERE guild_id=? AND user_id=? AND period_key IN ({placeholders})
        """, (guild_id, user_id, *period_keys))
        rows = {(r[0], r[1]): r for r in await cursor.fetchall()}

    result = []
    for d in defs:
        period_key = key_by_mission[d["id"]]
        row = rows.get((d["id"], period_key))
        result.append({
            **d, "period_key": period_key,
            "progress": row[2] if row else 0,
            "completed": bool(row[3]) if row else False,
            "completed_at": row[4] if row else None,
        })
    return result


def _passes_channel_filter(d: dict, channel_id: int | None) -> bool:
    """A mission restricted to a channel only counts activity in that
    channel; a mission with no channel counts anywhere. If the event
    carries no channel id at all (an orphaned thread whose parent is
    gone, a defensive caller), restricted missions are skipped rather
    than counted — counting them would silently widen the restriction.
    Non-channel-bound types (daily_completions) always pass."""
    if d["type"] not in CHANNEL_BOUND_TYPES:
        return True
    if not d.get("channel_id"):
        return True
    return channel_id is not None and d["channel_id"] == channel_id


async def record_activity(bot, guild_id: int, user_id: int,
                           mtype: str, amount: int,
                           channel_id: int | None = None) -> list[dict]:
    """Single-counter convenience wrapper (voice ticks, tests, and any
    future caller with one counter). Chat messages should use
    record_activities() so their messages+words counters share one
    definitions query and one connection."""
    return await record_activities(
        bot, guild_id, user_id, {mtype: amount}, channel_id=channel_id)


async def record_activities(bot, guild_id: int, user_id: int,
                            amounts: dict[str, int],
                            channel_id: int | None = None) -> list[dict]:
    """
    Adds progress to every enabled mission matching the given counters
    for this member's current period, granting rewards the instant a
    mission crosses its target. Each mission definition is updated in
    its own BEGIN IMMEDIATE transaction (mirrors
    utils/reward_engine.py's xp branch) so two events landing close
    together for the same member/mission can't both read pre-completion
    progress and both grant the reward.

    v2: all matching missions share ONE SQLite connection (per-mission
    transactions, one connect() instead of N), and rewards are granted
    after the write loop so one failing reward can't strand the
    remaining missions' progress.

    Returns the list of mission dicts newly completed by this call
    (including any chained daily_completions completions).
    """
    amounts = {t: a for t, a in (amounts or {}).items()
               if a and a > 0 and t in VALID_TYPES}
    if not amounts:
        return []

    defs = await get_definitions(guild_id, enabled_only=True)
    matching = [d for d in defs
                if d["type"] in amounts and _passes_channel_filter(d, channel_id)]
    if not matching:
        return []

    now = datetime.now(timezone.utc)
    newly_completed = []

    async with aiosqlite.connect(DB_PATH) as db:
        for d in matching:
            period_key = get_period_key(d["period"], now)
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    SELECT progress, completed FROM mission_progress
                    WHERE guild_id=? AND user_id=? AND mission_id=? AND period_key=?
                """, (guild_id, user_id, d["id"], period_key))
                row = await cursor.fetchone()
                already_done = bool(row[1]) if row else False

                if already_done:
                    # Already completed this period — nothing left to
                    # do (no partial re-completion, no double reward).
                    await db.execute("ROLLBACK")
                    continue

                new_progress = (row[0] if row else 0) + amounts[d["type"]]
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
                # in_transaction guard: if BEGIN IMMEDIATE itself failed
                # (e.g. "database is locked" after the busy timeout), the
                # connection is NOT in a transaction and a bare ROLLBACK
                # would raise "cannot rollback - no transaction is
                # active", masking the real error. Only roll back when
                # there is actually something to roll back.
                if db.in_transaction:
                    await db.execute("ROLLBACK")
                raise

            if just_completed:
                newly_completed.append(d)

    # Rewards AFTER the write loop (and outside the connection): a
    # broken reward config must not strand the remaining missions'
    # progress writes or the daily_completions chain below. The row is
    # already committed as completed, so this is grant-only territory —
    # same "a reward path must never crash the caller" stance the
    # prestige multiplier lookups in reward_engine take.
    for d in newly_completed:
        try:
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
        except Exception as e:
            print(f"[MISSIONS] Reward grant raised for mission "
                  f"{d['id']} ({d['name']}) guild={guild_id} "
                  f"user={user_id}: {e}")

    # daily_completions chain: every daily-period mission that just
    # completed counts as one "daily mission completed" toward any
    # daily_completions missions. The recursive call grants its own
    # rewards and can chain again (a daily_completions mission with
    # period=daily completing IS a daily completion) — recursion is
    # naturally bounded because a mission can only complete once per
    # period_key and completed missions are skipped above.
    daily_done = [d for d in newly_completed if d["period"] == "daily"]
    if daily_done:
        chained = await record_activities(
            bot, guild_id, user_id,
            {"daily_completions": len(daily_done)})
        newly_completed.extend(chained)

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
