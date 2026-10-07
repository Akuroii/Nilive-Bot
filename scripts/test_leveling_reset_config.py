"""Regression: a manual /resetleaderboard must not arm the scheduled auto-reset.

The bug this locks down: perform_leaderboard_reset() used to UPSERT
leveling_reset_config with enabled=1, so resetting the leaderboard once by
hand silently enrolled a guild that had never configured a scheduled reset
into leaderboard_reset_task's 7/30-day loop (it picks up every enabled=1 row).

Also locks down the effective-config behavior the Dashboard now relies on:
message cooldown defaults to 10 seconds, the independent spam window to 20, preserves the legacy frequency threshold,
and a stored row wins.

Run from the scripts directory:
    python test_leveling_reset_config.py
"""
import asyncio

from phase1_support import DB_PATH, GUILD, execute, reset_database, rows


async def main():
    await reset_database()
    from cogs.leveling import perform_leaderboard_reset, get_reset_config

    checks = []

    def check(name, ok, extra=""):
        checks.append((name, ok, extra))
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not ok else ""))

    # ── 1. a guild that never configured a reset stays unconfigured ─────────
    print("== 1. manual reset on a guild with NO reset config ==")
    execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, 8201, 54321, 30))
    check("no reset-config row before the reset",
          rows("SELECT * FROM leveling_reset_config WHERE guild_id=?", (GUILD,)) == [])
    print("  before:", await get_reset_config(GUILD))

    count = await perform_leaderboard_reset(GUILD, "weekly")
    check("reset still ran for the guild's members", count == 1, f"count={count}")
    check("XP was zeroed (reset behaviour unchanged)",
          rows("SELECT xp, level FROM levels WHERE guild_id=?", (GUILD,)) == [(0, 0)])
    check("history was archived (unchanged)",
          len(rows("SELECT * FROM leveling_leaderboard_history WHERE guild_id=?", (GUILD,))) == 1)
    row = rows("SELECT enabled, period, last_reset FROM leveling_reset_config WHERE guild_id=?",
               (GUILD,))
    check("NO reset-config row was created", row == [], f"row={row}")
    cfg = await get_reset_config(GUILD)
    check("scheduled auto-reset stays disabled/configured-off",
          cfg["enabled"] in (0, False) and cfg["last_reset"] is None, str(cfg))

    # ── 2. a guild that DID configure a reset keeps its settings ────────────
    print("== 2. manual reset on a guild WITH a reset config ==")
    execute("INSERT INTO leveling_reset_config (guild_id, enabled, period, last_reset) "
            "VALUES (?,?,?,?)", (GUILD, 1, "monthly", None))
    execute("UPDATE levels SET xp=999, level=9 WHERE guild_id=? AND user_id=?",
            (GUILD, 8201))
    await perform_leaderboard_reset(GUILD, "weekly")
    row = rows("SELECT enabled, period, last_reset FROM leveling_reset_config WHERE guild_id=?",
               (GUILD,))[0]
    check("still enabled (auto-reset keeps running)", row[0] == 1, str(row))
    check("period/last_reset bookkeeping updated", row[1] == "weekly" and row[2], str(row))
    check("XP zeroed again", rows("SELECT xp FROM levels WHERE guild_id=?", (GUILD,)) == [(0,)])

    # ── 3. the reset was not silently turned off for anyone ────────────────
    print("== 3. an enabled row is never disabled by a manual reset ==")
    execute("UPDATE leveling_reset_config SET enabled=1, period='weekly' WHERE guild_id=?",
            (GUILD,))
    await perform_leaderboard_reset(GUILD, "weekly")
    check("enabled stays 1",
          rows("SELECT enabled FROM leveling_reset_config WHERE guild_id=?", (GUILD,)) == [(1,)])

    # ── 4. effective config: cooldown default and stored-row precedence ────
    print("== 4. effective leveling config ==")
    from utils.xp_calculator import LEVELING_CONFIG_DEFAULTS, get_leveling_config
    fresh = await get_leveling_config(GUILD + 1)
    check("default xp_cooldown_seconds is 10",
          fresh["xp_cooldown_seconds"] == 10 and LEVELING_CONFIG_DEFAULTS["xp_cooldown_seconds"] == 10,
          str(fresh["xp_cooldown_seconds"]))
    check("defaults covered: spam window 20, threshold 10, divisor 1000, XP 1–50",
          fresh["spam_window_seconds"] == 20 and fresh["spam_threshold"] == 10
          and fresh["spam_xp_penalty_divisor"] == 1000
          and fresh["xp_per_word"] == 1 and fresh["xp_max_per_message"] == 50,
          str(fresh))
    # remove_old_reward_role is a live product toggle again: OFF (0, default) =
    # Level reward roles accumulate; ON (1) = exclusive progression roles.
    check("remove_old_reward_role defaults to OFF (roles accumulate)",
          fresh["remove_old_reward_role"] == 0, str(fresh["remove_old_reward_role"]))
    from dashboard.api.leveling import _LEVELING_INT_FIELDS
    fields = {field: (default, lo, hi) for field, _, default, lo, hi in _LEVELING_INT_FIELDS}
    check("the config save path validates/writes the toggle",
          fields.get("remove_old_reward_role") == (0, 0, 1), str(fields.get("remove_old_reward_role")))
    execute("INSERT INTO leveling_config (guild_id, remove_old_reward_role, xp_per_word) "
            "VALUES (?,1,4)", (GUILD + 1,))
    stored2 = await get_leveling_config(GUILD + 1)
    check("a stored ON value is what the runtime reads",
          stored2["remove_old_reward_role"] == 1, str(stored2["remove_old_reward_role"]))
    execute("UPDATE leveling_config SET xp_cooldown_seconds=45, xp_per_word=3 "
            "WHERE guild_id=?", (GUILD + 1,))
    stored = await get_leveling_config(GUILD + 1)
    check("a stored row still wins over the defaults",
          stored["xp_cooldown_seconds"] == 45 and stored["xp_per_word"] == 3, str(stored))
    check("columns absent from the row fall back to defaults",
          stored["spam_window_seconds"] == 20 and stored["spam_threshold"] == 10
          and stored["xp_max_per_message"] == 50, str(stored))

    failed = [c for c in checks if not c[1]]
    print(f"\nLEVELING RESET/CONFIG: {len(checks) - len(failed)} passed, {len(failed)} failed")
    for name, _, extra in failed:
        print("  FAILED:", name, extra)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
