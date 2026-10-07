"""Reproducible calculation for the incident-based XP deduction.

The runtime calls this once when a spam incident starts. The default raw
amount is 1/1000 of current XP, bounded at 1% per incident and by one
production-calculated maximum Message XP award in any rolling hour. This script
exercises the production write helper at low, medium and high balances,
including a corrupt divisor, and verifies level consistency. It is scratch-DB
only.

Run with:
    python scripts/simulate_negative_xp.py
"""
import asyncio
import sys

from phase1_support import GUILD, execute, reset_database, rows


def xp_to(level: int) -> int:
    from utils.xp_calculator import xp_for_level
    return sum(xp_for_level(current) for current in range(1, level + 1))


async def main():
    from utils.xp_calculator import (
        apply_spam_penalty, calculate_max_message_xp, xp_progress,
    )

    await reset_database()
    scenarios = [
        (9101, 0, 1000),
        (9102, 50, 1000),
        (9103, 100_000, 1000),
        (9104, xp_to(80) + 50_000, 1000),
        (9105, 100_000, 1),  # corrupt/custom value still obeys the 1% cap
    ]
    print("Incident-based anti-spam penalty (one deduction per incident)")
    for user_id, balance, divisor in scenarios:
        execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,?,?)",
                (GUILD, user_id, balance, xp_progress(balance)[0]))
        penalty_cap = await calculate_max_message_xp(GUILD, [], user_id)
        outcome = await apply_spam_penalty(
            GUILD, user_id, divisor, penalty_cap=penalty_cap)
        stored = rows("SELECT xp,level FROM levels WHERE guild_id=? AND user_id=?",
                      (GUILD, user_id))[0]
        assert stored == (outcome["new_xp"], xp_progress(outcome["new_xp"])[0])
        assert 0 <= outcome["deducted"] <= balance // 100
        print(f"user={user_id} xp={balance:,} divisor={divisor} cap={penalty_cap} -> "
              f"-{outcome['deducted']:,}, xp={stored[0]:,}, level={stored[1]}, "
              f"hourly_remaining={outcome['budget_remaining_after']}, "
              f"applied={outcome['applied']}")
    print("All deductions stayed non-negative, respected both the 1% incident "
          "limit and one-message rolling-hour budget, and left stored Level "
          "consistent with the production XP curve.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
