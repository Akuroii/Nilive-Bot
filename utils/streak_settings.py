"""Per-guild settings and reward table for Streak V2 (six-stage table).

V2 values live in the existing ``guild_settings_kv`` table. A guild has V2
rewards only when all seven reward keys are saved (stages 1-5 fixed, stage 6
min/max). Until then the guild keeps the existing global Daily range and
flat-per-day streak bonus; no reward amounts are invented.
"""
from __future__ import annotations

import aiosqlite
from database import DB_PATH

FIXED_STAGES = (1, 2, 3, 4, 5)
STAGE_KEYS = {n: f"streak_v2_stage_{n}" for n in FIXED_STAGES}
STAGE6_MIN_KEY = "streak_v2_stage6_min"
STAGE6_MAX_KEY = "streak_v2_stage6_max"
REWARD_KEYS = tuple(STAGE_KEYS.values()) + (STAGE6_MIN_KEY, STAGE6_MAX_KEY)
RECEIPT_MODE_KEY = "streak_receipt_visibility"
RECEIPT_CHANNEL_KEY = "streak_receipt_channel_id"

MAX_DAILY_AMOUNT = 1_000_000_000
RECEIPT_MODES = {"private", "public", "both"}


def _whole_amount(value, label: str) -> int:
    try:
        amount = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not 1 <= amount <= MAX_DAILY_AMOUNT:
        raise ValueError(f"{label} must be from 1 to {MAX_DAILY_AMOUNT:,}.")
    return amount


def validate_streak_settings(stages, stage6_min, stage6_max,
                             receipt_visibility="private", receipt_channel_id=None) -> dict:
    """Validate and normalize the dashboard payload without performing I/O.

    ``stages`` is the five fixed rewards for days 1-5. All six reward inputs
    blank means "no V2 table" (legacy behavior); a partial table is rejected.
    """
    stages = list(stages) if isinstance(stages, (list, tuple)) else [None] * 5
    if len(stages) != 5:
        raise ValueError("Provide exactly five fixed rewards for days 1 to 5.")
    raw = stages + [stage6_min, stage6_max]
    blank = [str(v if v is not None else "").strip() == "" for v in raw]
    if all(blank):
        table = None
    elif any(blank):
        raise ValueError("Fill all six reward stages, or leave all of them blank.")
    else:
        fixed = [_whole_amount(v, f"Day {i} reward") for i, v in enumerate(stages, 1)]
        low = _whole_amount(stage6_min, "Day 6+ minimum")
        high = _whole_amount(stage6_max, "Day 6+ maximum")
        if high < low:
            raise ValueError("Day 6+ maximum must be greater than or equal to the minimum.")
        table = {"stages": fixed, "stage6_min": low, "stage6_max": high}

    mode = str(receipt_visibility or "private").strip().lower()
    if mode not in RECEIPT_MODES:
        raise ValueError("Receipt visibility must be Private, Public, or Both.")
    channel = None
    raw_channel = str(receipt_channel_id or "").strip()
    if mode in {"public", "both"} and raw_channel:
        try:
            channel = str(int(raw_channel))
        except (TypeError, ValueError):
            raise ValueError("Choose a valid receipt channel or leave it blank.")
        if int(channel) <= 0:
            raise ValueError("Choose a valid receipt channel or leave it blank.")
    return {"table": table, "receipt_visibility": mode, "receipt_channel_id": channel}


def stage_reward_range(stage: int, table: dict) -> tuple[int, int]:
    """Pre-Prestige reward range for a stage. Stage >= 6 never cycles."""
    stage = max(1, int(stage))
    if stage <= 5:
        amount = int(table["stages"][stage - 1])
        return amount, amount
    return int(table["stage6_min"]), int(table["stage6_max"])


def receipt_destinations(mode: str) -> tuple[bool, bool]:
    """Return ``(private_receipt, public_receipt)`` for the selected mode."""
    mode = str(mode or "private").lower()
    if mode == "public":
        return False, True
    if mode == "both":
        return True, True
    return True, False


async def _read_streak_settings(db: aiosqlite.Connection, guild_id: int) -> dict:
    keys = REWARD_KEYS + (RECEIPT_MODE_KEY, RECEIPT_CHANNEL_KEY)
    cursor = await db.execute(
        "SELECT key, value FROM guild_settings_kv WHERE guild_id = ? "
        f"AND key IN ({','.join('?' * len(keys))})",
        (guild_id, *keys),
    )
    guild_values = {str(k): v for k, v in await cursor.fetchall()}
    legacy_cursor = await db.execute(
        "SELECT key, value FROM bot_settings WHERE key IN ('daily_min', 'daily_max', 'daily_streak_bonus_per_day')"
    )
    legacy_values = {str(k): v for k, v in await legacy_cursor.fetchall()}

    def as_int(raw, default):
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    # V2 only when the complete, valid table exists for THIS guild; a
    # partial/corrupt table falls back as a whole rather than mixing scopes.
    table = None
    if all(key in guild_values for key in REWARD_KEYS):
        try:
            fixed = [_whole_amount(guild_values[STAGE_KEYS[n]], "stage") for n in FIXED_STAGES]
            low = _whole_amount(guild_values[STAGE6_MIN_KEY], "min")
            high = _whole_amount(guild_values[STAGE6_MAX_KEY], "max")
            if high >= low:
                table = {"stages": fixed, "stage6_min": low, "stage6_max": high}
        except ValueError:
            table = None

    legacy_low = as_int(legacy_values.get("daily_min"), 100)
    legacy_high = as_int(legacy_values.get("daily_max"), 300)
    if legacy_high < legacy_low:
        # Match the existing Daily engine's legacy behavior for inverted values.
        legacy_low, legacy_high = legacy_high, legacy_low

    mode = str(guild_values.get(RECEIPT_MODE_KEY) or "private").lower()
    if mode not in RECEIPT_MODES:
        mode = "private"
    channel_raw = guild_values.get(RECEIPT_CHANNEL_KEY)
    channel_id = str(channel_raw) if channel_raw and str(channel_raw).isdigit() else None
    return {
        "reward_table": table,
        "v2_enabled": table is not None,
        # Legacy range (used only while no V2 table is saved for the guild).
        "daily_min": legacy_low,
        "daily_max": legacy_high,
        "legacy_bonus_per_day": as_int(
            legacy_values.get("daily_streak_bonus_per_day"), 20),
        "receipt_visibility": mode,
        "receipt_mode_source": "guild" if RECEIPT_MODE_KEY in guild_values else "default",
        "receipt_channel_id": channel_id,
    }


async def get_streak_settings(guild_id: int,
                              db: aiosqlite.Connection | None = None) -> dict:
    """Read current settings, optionally within a caller's transaction.

    No V2 amounts are invented: ``reward_table is None`` means the existing
    global Daily range and flat bonus stay active until an admin saves the table.
    """
    if db is not None:
        return await _read_streak_settings(db, guild_id)
    async with aiosqlite.connect(DB_PATH) as connection:
        return await _read_streak_settings(connection, guild_id)


async def save_streak_settings(guild_id: int, settings: dict) -> dict:
    """Save validated guild-level keys atomically in the existing KV table."""
    normalized = validate_streak_settings(
        settings.get("stages"), settings.get("stage6_min"), settings.get("stage6_max"),
        settings.get("receipt_visibility", "private"),
        settings.get("receipt_channel_id"),
    )
    table = normalized["table"]
    values = {RECEIPT_MODE_KEY: normalized["receipt_visibility"]}
    delete_keys = []
    if table is None:
        # Removing the table is the explicit path back to the legacy reward.
        delete_keys.extend(REWARD_KEYS)
    else:
        for n, amount in zip(FIXED_STAGES, table["stages"]):
            values[STAGE_KEYS[n]] = str(amount)
        values[STAGE6_MIN_KEY] = str(table["stage6_min"])
        values[STAGE6_MAX_KEY] = str(table["stage6_max"])
    if normalized["receipt_channel_id"]:
        values[RECEIPT_CHANNEL_KEY] = normalized["receipt_channel_id"]
    else:
        delete_keys.append(RECEIPT_CHANNEL_KEY)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for key, value in values.items():
                await db.execute("""
                    INSERT INTO guild_settings_kv (guild_id, key, value, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                """, (guild_id, key, value))
            for key in delete_keys:
                await db.execute(
                    "DELETE FROM guild_settings_kv WHERE guild_id = ? AND key = ?",
                    (guild_id, key),
                )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return await get_streak_settings(guild_id)
