"""Reproducible Leveling pacing and penalty-budget report.

All XP calculations and Level milestones come from utils.xp_calculator; this
script simulates the actual cooldown gate for evenly spaced messages across a
six-hour active day and adds the production Voice XP calculation. It uses a
throwaway database from phase1_support and fake role/boost records only.

Run from the repository root:
    python scripts/simulate_xp_pacing.py          # print the Markdown report
    python scripts/simulate_xp_pacing.py --write  # regenerate XP_PACING_SIMULATION.md
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from phase1_support import GUILD, USER, execute, reset_database

ACTIVE_SECONDS_PER_DAY = 6 * 60 * 60
VOICE_MINUTES_PER_DAY = 6 * 60
VOICE_XP_PER_MINUTE = 3
MESSAGE_WORDS = 10
COOLDOWNS = (20, 10)
ACTIVITY_PROFILES = (
    ("Low", 200),
    ("Normal", 800),
    ("High", 1_440),
)
MULTIPLIER_CASES = (
    ("Neutral", False, False),
    ("Role 2x", True, False),
    ("XP boost 2x", False, True),
    ("Role 2x + XP boost 2x", True, True),
)
MILESTONES = (1, 10, 25, 50, 100)


def count_cooldown_eligible_messages(requested: int, cooldown: int) -> int:
    """Apply the runtime's `now - last_award >= cooldown` check to a schedule."""
    if requested <= 0:
        return 0
    interval = ACTIVE_SECONDS_PER_DAY / requested
    last_award_at = None
    accepted = 0
    for index in range(requested):
        sent_at = index * interval
        if last_award_at is None or sent_at - last_award_at >= cooldown:
            accepted += 1
            last_award_at = sent_at
    return accepted


def milestone_totals(xp_for_level, xp_progress) -> dict[int, int]:
    totals = {}
    cumulative = 0
    targets = set(MILESTONES)
    for level in range(1, max(MILESTONES) + 1):
        cumulative += xp_for_level(level)
        if level in targets:
            calculated_level = xp_progress(cumulative)[0]
            if calculated_level != level:
                raise AssertionError(
                    f"production curve mismatch at L{level}: {calculated_level}")
            totals[level] = cumulative
    return totals


async def run_simulation() -> str:
    from utils.xp_calculator import (
        calculate_max_message_xp,
        calculate_message_xp,
        calculate_voice_xp,
        get_leveling_config,
        xp_for_level,
        xp_progress,
    )

    await reset_database()
    config = await get_leveling_config(GUILD)
    if (config["message_xp_enabled"] != 1
            or config["xp_per_word"] != 1
            or config["xp_min_per_message"] != 5
            or config["xp_max_per_message"] != 50):
        raise AssertionError("fresh-simulation defaults differ from production config")

    milestones = milestone_totals(xp_for_level, xp_progress)
    voice_daily = calculate_voice_xp(
        VOICE_MINUTES_PER_DAY, VOICE_XP_PER_MINUTE)
    boost_expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    calculations = {}
    next_user = USER + 1000
    next_role = 900_000

    # Ask the production calculator for each real multiplier combination; the
    # pacing loop below only counts when the production cooldown would accept.
    for label, role_on, boost_on in MULTIPLIER_CASES:
        user_id = next_user
        next_user += 1
        role_ids = []
        if role_on:
            role_id = next_role
            next_role += 1
            role_ids.append(role_id)
            execute("INSERT INTO leveling_bonus_roles (guild_id,role_id,multiplier) "
                    "VALUES (?,?,2.0)", (GUILD, role_id))
        if boost_on:
            execute("INSERT INTO leveling_active_boosts "
                    "(guild_id,user_id,multiplier,expires_at,source) "
                    "VALUES (?,?,2.0,?,'simulation')",
                    (GUILD, user_id, boost_expiry))

        message_award = await calculate_message_xp(
            GUILD, role_ids, MESSAGE_WORDS, user_id=user_id)
        max_message_award = await calculate_max_message_xp(
            GUILD, role_ids, user_id)
        calculations[label] = {
            "user_id": user_id,
            "role_ids": role_ids,
            "message_award": message_award,
            "penalty_cap": max_message_award,
        }

    rows = []
    for activity, requested in ACTIVITY_PROFILES:
        for cooldown in COOLDOWNS:
            accepted = count_cooldown_eligible_messages(requested, cooldown)
            for voice_on in (False, True):
                for multiplier, _role_on, _boost_on in MULTIPLIER_CASES:
                    msg_award = calculations[multiplier]["message_award"]
                    daily_xp = accepted * msg_award + (voice_daily if voice_on else 0)
                    days = {
                        level: milestones[level] / daily_xp
                        for level in MILESTONES
                    }
                    rows.append({
                        "activity": activity,
                        "requested": requested,
                        "interval": ACTIVE_SECONDS_PER_DAY / requested,
                        "cooldown": cooldown,
                        "voice": "On" if voice_on else "Off",
                        "multiplier": multiplier,
                        "accepted": accepted,
                        "message_award": msg_award,
                        "voice_daily": voice_daily if voice_on else 0,
                        "daily_xp": daily_xp,
                        "days": days,
                    })

    theoretical = {
        cooldown: ACTIVE_SECONDS_PER_DAY // cooldown
        for cooldown in COOLDOWNS
    }
    penalty_caps = [
        (label, calc["message_award"], calc["penalty_cap"])
        for label, calc in calculations.items()
    ]

    lines = [
        "# Leveling pacing and incident-penalty budget (reproducible)",
        "",
        "This report is generated by `python scripts/simulate_xp_pacing.py --write`.",
        "The script uses the production `calculate_message_xp()`, `calculate_voice_xp()`, `calculate_max_message_xp()`, `xp_for_level()` and `xp_progress()` functions against a disposable SQLite database. The fake role and XP-boost rows exercise the real production multiplier reads. It performs no Discord calls.",
        "",
        "## Inputs and model",
        "",
        f"- Six credited activity hours/day ({ACTIVE_SECONDS_PER_DAY:,} seconds); messages are evenly spaced throughout those hours, {MESSAGE_WORDS} words each, with no spam flags.",
        f"- Fresh production defaults: {config['xp_per_word']} XP/word, {config['xp_min_per_message']}–{config['xp_max_per_message']} base XP/message, Message XP enabled, Voice XP {VOICE_XP_PER_MINUTE} XP/minute.",
        f"- Voice ON adds `{VOICE_MINUTES_PER_DAY} × {VOICE_XP_PER_MINUTE} = {voice_daily:,}` XP/day; voice receives no role/boost multiplier, matching production.",
        f"- Activity requests: Low = {ACTIVITY_PROFILES[0][1]:,}/day, Normal = {ACTIVITY_PROFILES[1][1]:,}/day, High = {ACTIVITY_PROFILES[2][1]:,}/day. Average intervals are 108s, 27s and 15s respectively.",
        f"- The cooldown gate is simulated message-by-message: accept when `sent_at - last_accepted_at >= cooldown`. The theoretical six-hour ceilings are {theoretical[20]:,} at 20s and {theoretical[10]:,} at 10s; an evenly spaced 15s high-activity stream passes every other message at 20s (720) and all messages at 10s (1,440).",
        "- Multiplier cases are neutral, 2× role, 2× active XP boost, and both together (4× Message XP). The production calculator applies role and boost multipliers to Message XP only.",
        "",
        "The schedules are idealized pacing comparisons, not predictions of real conversation. The Level curve is cumulative and unchanged; days are `cumulative XP to milestone / modeled XP per day`, from zero.",
        "",
        "## Production curve milestones",
        "",
        "| Level | Cumulative XP |",
        "|---:|---:|",
    ]
    for level in MILESTONES:
        lines.append(f"| L{level} | {milestones[level]:,} |")

    lines += [
        "",
        "## Daily activity and time to each Level",
        "",
        "Rows compare both cooldowns for each activity profile, Voice state, and multiplier case. ‘Accepted’ is the message count after applying the simulated production cooldown.",
        "",
        "| Activity | Requests/day | Cooldown | Voice | Multiplier | Accepted/day | XP/day | L1 days | L10 days | L25 days | L50 days | L100 days |",
        "|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        days = row["days"]
        lines.append(
            f"| {row['activity']} | {row['requested']:,} | {row['cooldown']}s | "
            f"{row['voice']} | {row['multiplier']} | {row['accepted']:,} | "
            f"{row['daily_xp']:,} | {days[1]:.2f} | {days[10]:.2f} | "
            f"{days[25]:.2f} | {days[50]:.2f} | {days[100]:.2f} |")

    high_normal = next(row for row in rows if
                       row["activity"] == "High" and row["cooldown"] == 10
                       and row["voice"] == "On"
                       and row["multiplier"] == "Neutral")
    lines += [
        "",
        "## Rolling anti-spam penalty budget",
        "",
        "The listener still applies at most one incident deduction when an incident opens. The existing raw amount is `floor(current XP / spam_xp_penalty_divisor)` (default divisor 1,000), with the existing 1% per-incident maximum. A persistent SQLite ledger additionally limits all deductions in any rolling 3,600 seconds to **one production-calculated maximum Message XP award** for that member. This is a progress budget, not a fraction such as 1/8, 1/10 or 1/12 of an XP balance or Level bar.",
        "",
        "The one-hour unit is aligned with the pacing model’s per-active-hour rate: even repeated incidents cannot erase more than one maximally rewarded message per hour, and a six-hour active day can therefore lose at most six such awards. The budget is calculated from the current configured per-message cap and the same role/active-boost multipliers used by `calculate_message_xp()`. Message XP OFF does not disable the spam protection budget calculation; blacklist roles still resolve to zero potential message XP.",
        "",
        "| Multiplier case | 10-word production award | Rolling-hour penalty cap (max-message award) |",
        "|---|---:|---:|",
    ]
    for label, award, cap in penalty_caps:
        lines.append(f"| {label} | {award} XP | {cap} XP |")
    lines += [
        "",
        f"For context, the High/neutral/Voice-on 10s case earns {high_normal['daily_xp']:,} XP/day ({high_normal['daily_xp'] / 6:,.0f} per credited hour) and reaches L100 in {high_normal['days'][100]:.2f} days. Its 50 XP rolling cap is about {100 * 50 / (high_normal['daily_xp'] / 6):.2f}% of one modeled active hour. Low-activity, Voice-off members have a larger relative cap because one maximum message is a larger share of their much lower hourly pace; it is still never more than one message’s worth.",
        "No XP-per-message or XP-per-word reduction was made just because a 10s cooldown creates more opportunities. The 261.63-day High-profile result is a deliberately demanding six-hours-every-day activity case and evidence for balance review, not automatically a defect.",
        "",
        "The XP penalty transaction serializes on SQLite `BEGIN IMMEDIATE`, sums `leveling_spam_penalty_events` from the last hour, applies only the remaining budget, writes XP/Level and the event row together, and prunes expired rows. A process or cog-state reset cannot refill the budget. XP stays at or above zero; Level is recomputed from XP; the penalty does not call the reward engine or mutate claims/roles.",
        "",
        "## Spam detector thresholds and saved-value preservation",
        "",
        "The threshold-10 default is retained for new/unconfigured guilds. The marker-backed data migration changes existing guild rows only to a 10-second Message XP cooldown and a 20-second spam window; it does **not** rewrite any guild's `spam_threshold`, so saved values such as 3 remain 3. Production SQL/API/runtime/Dashboard defaults agree at 10. No threshold data migration was added.",
        "",
        "| Signal | Current rule | Purpose and necessity |",
        "|---|---|---|",
        "| Configurable frequency threshold | `spam_threshold` messages in `spam_window_seconds` (new-guild default 10 in 20s) | General sustained-rate signal for a flood of distinct or mixed content. Ten messages over 20 seconds is slower than the burst/repetition signals, so it remains a useful independent fallback for sustained rapid activity. |",
        "| Rapid burst | 6 messages within 3 seconds | Catches a short, even-distinct-content burst promptly, before the general 10-in-20 counter. It is necessary because distinct short bursts need not match the repetition signal. |",
        "| Repeated normalized content | 5 identical non-empty messages within `min(10s, spam_window_seconds)`; case-folded and whitespace-normalized | Catches copy/paste flooding sooner than the general count, even when spaced to avoid the 3-second burst. It complements rather than duplicates the distinct-message burst signal. |",
        "| Incident / warning | One penalty when the incident opens; later detections extend the quiet window but do not deduct again. Warning reply is independently rate-limited to once per configured spam window. | Keeps one message burst from charging once per flagged message; persistent rolling cap also bounds distinct incidents. |",
        "",
        "The legacy fixed `spam_xp_penalty` column is retained as inert compatibility storage so migration does not discard an administrator’s old value. Runtime/API do not read or expose it. The new incident divisor is separate; no fixed `-10` is charged per message.",
        "",
        "## Migration and verification boundary",
        "",
        "`database.init_db()` runs the marker-backed one-time data migration: every pre-existing guild row gets `xp_cooldown_seconds=10` and `spam_window_seconds=20`; the saved `spam_threshold` value, all other Leveling choices, and the deprecated fixed-penalty value are preserved. The SQL/API/runtime/Dashboard baselines for new guild rows are cooldown 10s, spam window 20s, and frequency threshold 10. The marker prevents subsequent startup from overwriting later administrator edits. Migration tests use disposable SQLite fixtures; no deployed database was inspected or migrated.",
        "",
        "The runtime scenarios and fake Discord callbacks are integration tests against the production handlers and a temporary SQLite database. They are not live Discord verification.",
        "",
    ]
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="write the generated report to XP_PACING_SIMULATION.md")
    args = parser.parse_args()
    report = await run_simulation()
    if args.write:
        target = Path(__file__).resolve().parents[1] / "XP_PACING_SIMULATION.md"
        target.write_text(report, encoding="utf-8")
        print(f"Wrote {target}")
    else:
        print(report, end="")


if __name__ == "__main__":
    asyncio.run(main())
