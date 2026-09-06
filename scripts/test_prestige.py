#!/usr/bin/env python3
"""
Finalized Prestige system — verification suite.

Covers the locked product rules end-to-end against a scratch SQLite DB:

  1. I → II purchase succeeds and resets Coins to 0.
  2. II → IV skip is rejected.
  3. Re-buying an already-owned tier is rejected.
  4. Valid purchase sets Coins to 0.
  5. XP / Level / Diamonds remain unchanged after a purchase.
  6. Permanent V gives a 1.1× Coins earn multiplier.
  7. A Booster at any permanent tier gets effective Prestige VI.
  8. VI gives a 1.1× Coins + 1.2× Diamonds earn multiplier.
  9. Losing Booster returns effective Prestige to the permanent tier.
  10. Manual Prestige role assignment does NOT grant Prestige/multiplier.
  11. Transfer / conversion / admin currency grants are NOT multiplied.
  12. Existing user data remains intact (legacy levels.prestige > 5 clamps).
  13. The old XP/level-gated `/prestige` reset can no longer execute.

Run:  python3 scripts/test_prestige.py
(system python — aiosqlite + sqlite3 required; section 14 also imports
utils.reward_engine which pulls in discord.py. Install both in a venv:
  python3 -m venv .venv && .venv/bin/pip install aiosqlite==0.19.0 discord.py==2.7.1
  .venv/bin/python scripts/test_prestige.py
)
"""
import os
import sys
import asyncio
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="prestige_")
DB_PATH = os.path.join(_TMP, "prestige.db")
os.environ["DATABASE_PATH"] = DB_PATH
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import database                      # noqa: E402
from database import DB_PATH as REAL_DB_PATH  # noqa: E402
import utils.prestige as prestige     # noqa: E402
from utils.prestige import (          # noqa: E402
    get_permanent_prestige,
    get_effective_prestige,
    get_prestige_config,
    set_prestige_multipliers,
    get_prestige_earn_multiplier,
    purchase_prestige,
    PrestigeError,
    BOOSTER_TIER,
)

G = 1001
U = 5001

PASS = FAIL = 0
FAILURES = []


def check(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name + (f" — {extra}" if extra else ""))
        print(f"  FAIL  {name}  {extra}")


def section(t):
    print(f"\n== {t} ==")


async def setup_database():
    await database.init_db()


def sql_exec(sql, args=()):
    conn = sqlite3.connect(REAL_DB_PATH)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def sql_query(sql, args=()):
    conn = sqlite3.connect(REAL_DB_PATH)
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return rows


def reset_user(level_xp=0, level=0, coins=0, diamonds=0, prestige_val=0):
    sql_exec(
        "DELETE FROM levels WHERE guild_id=? AND user_id=?", (G, U))
    sql_exec(
        "DELETE FROM economy WHERE guild_id=? AND user_id=?", (G, U))
    sql_exec(
        "INSERT INTO levels (guild_id, user_id, xp, level, prestige) "
        "VALUES (?, ?, ?, ?, ?)", (G, U, level_xp, level, prestige_val))
    sql_exec(
        "INSERT INTO economy (guild_id, user_id, balance, diamonds) "
        "VALUES (?, ?, ?, ?)", (G, U, coins, diamonds))


async def main():
    section("Database init")
    await setup_database()
    check(True, "database.init_db() ran without error")

    section("Config defaults")
    cfg = await get_prestige_config(G)
    check(cfg["enabled"] == 1, "prestige enabled by default")
    check(set(cfg["tiers"].keys()) == {1, 2, 3, 4, 5, 6},
          "6 tiers present", str(sorted(cfg["tiers"].keys())))
    check(abs(cfg["tiers"][5]["coins"] - 1.1) < 1e-9, "V coins default 1.1x")
    check(abs(cfg["tiers"][6]["coins"] - 1.1) < 1e-9, "VI coins default 1.1x")
    check(abs(cfg["tiers"][6]["diamonds"] - 1.2) < 1e-9, "VI diamonds default 1.2x")

    section("1) I → II purchase succeeds (sequential)")
    reset_user(level_xp=12000, level=45, coins=3000, diamonds=7, prestige_val=0)
    res = await purchase_prestige(G, U, 1, 500, item_name="Prestige I",
                                   display_name="Tester")
    check(res["success"], "purchase tier I succeeds")
    check(res["new_tier"] == 1, "new_tier == 1")

    # Now purchase II (tier 2) from tier 1.
    reset_user(level_xp=12000, level=45, coins=5000, diamonds=7, prestige_val=1)
    res2 = await purchase_prestige(G, U, 2, 1000, item_name="Prestige II",
                                   display_name="Tester")
    check(res2["success"], "purchase tier II succeeds")
    check(res2["new_tier"] == 2, "new_tier == 2")
    check(res2["old_tier"] == 1, "old_tier == 1")

    section("2) II → IV skip is rejected")
    reset_user(level_xp=12000, level=45, coins=9000, diamonds=7, prestige_val=2)
    try:
        await purchase_prestige(G, U, 4, 500, item_name="Prestige IV")
        check(False, "skip II→IV rejected")
    except PrestigeError as e:
        check(True, "skip II→IV rejected", str(e))

    section("3) Re-buying an already-owned tier is rejected")
    reset_user(level_xp=12000, level=45, coins=9000, diamonds=7, prestige_val=3)
    try:
        await purchase_prestige(G, U, 3, 500, item_name="Prestige III")
        check(False, "rebuy tier III rejected")
    except PrestigeError as e:
        check(True, "rebuy tier III rejected", str(e))

    section("4) Valid purchase sets Coins to 0")
    reset_user(level_xp=12000, level=45, coins=1234, diamonds=7, prestige_val=1)
    await purchase_prestige(G, U, 2, 1000, item_name="Prestige II")
    row = sql_query(
        "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
        (G, U))
    check(row and row[0][0] == 0, "coins set to 0")

    section("5) XP / Level / Diamonds remain unchanged")
    reset_user(level_xp=12345, level=48, coins=2000, diamonds=9, prestige_val=3)
    await purchase_prestige(G, U, 4, 1500, item_name="Prestige IV")
    lv = sql_query(
        "SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
        (G, U))
    ec = sql_query(
        "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
        (G, U))
    check(lv and lv[0][0] == 12345, "xp unchanged", str(lv))
    check(lv and lv[0][1] == 48, "level unchanged", str(lv))
    check(lv and lv[0][2] == 4, "permanent prestige set to IV", str(lv))
    check(ec and ec[0][0] == 0, "coins reset to 0")
    check(ec and ec[0][1] == 9, "diamonds unchanged", str(ec))

    section("6) Permanent V gives 1.1x Coins")
    reset_user(level_xp=20000, level=50, coins=100, diamonds=1, prestige_val=5)
    mult = await get_prestige_earn_multiplier(G, U, "balance", is_booster=False)
    check(abs(mult - 1.1) < 1e-9, "V coins multiplier 1.1x", str(mult))
    dm = await get_prestige_earn_multiplier(G, U, "diamonds", is_booster=False)
    check(abs(dm - 1.0) < 1e-9, "V diamonds multiplier 1.0x", str(dm))

    section("7) Booster at any permanent tier gets effective VI")
    reset_user(level_xp=20000, level=50, coins=100, diamonds=1, prestige_val=1)
    eff = await get_effective_prestige(G, U, is_booster=True)
    check(eff == BOOSTER_TIER, "permanent I + booster → effective VI", str(eff))

    section("8) VI gives 1.1x Coins + 1.2x Diamonds")
    reset_user(level_xp=20000, level=50, coins=100, diamonds=1, prestige_val=0)
    mult_c = await get_prestige_earn_multiplier(G, U, "balance", is_booster=True)
    mult_d = await get_prestige_earn_multiplier(G, U, "diamonds", is_booster=True)
    check(abs(mult_c - 1.1) < 1e-9, "VI coins multiplier 1.1x", str(mult_c))
    check(abs(mult_d - 1.2) < 1e-9, "VI diamonds multiplier 1.2x", str(mult_d))

    section("9) Losing Booster returns effective to permanent tier")
    reset_user(level_xp=20000, level=50, coins=100, diamonds=1, prestige_val=3)
    eff_b = await get_effective_prestige(G, U, is_booster=True)
    eff_nb = await get_effective_prestige(G, U, is_booster=False)
    check(eff_b == BOOSTER_TIER, "boosting → VI")
    check(eff_nb == 3, "no longer boosting → permanent III", str(eff_nb))

    section("10) Manual role assignment does NOT grant Prestige/multiplier")
    # Insert a pointless prestige_roles entry: the multiplier MUST ignore it.
    sql_exec(
        "DELETE FROM prestige_roles WHERE guild_id=? AND tier=? AND role_id=?",
        (G, 5, 777))
    sql_exec(
        "INSERT INTO prestige_roles (guild_id, tier, role_id) VALUES (?, ?, ?)",
        (G, 5, 777))
    reset_user(level_xp=20000, level=50, coins=100, diamonds=1, prestige_val=2)
    # A tier-II user with the tier-V role manually assigned must be 1.0x
    # (not 1.1x). Roles never grant the multiplier.
    mult = await get_prestige_earn_multiplier(G, U, "balance", is_booster=False)
    check(abs(mult - 1.0) < 1e-9, "role assignment does not change multiplier",
          str(mult))
    perm = await get_permanent_prestige(G, U)
    check(perm == 2, "role assignment does not change permanent tier", str(perm))

    section("11) Transfer / conversion / admin grants not multiplied")
    # safe_transfer, safe_convert and safe_admin_deduct must move the raw
    # amount — they never consult prestige multipliers.
    from utils.economy_safe import (
        safe_transfer, safe_convert, safe_admin_deduct)
    reset_user(level_xp=20000, level=50, coins=5000, diamonds=5, prestige_val=5)
    U2 = 5002
    sql_exec("DELETE FROM economy WHERE guild_id=? AND user_id=?", (G, U2))
    sql_exec(
        "INSERT INTO economy (guild_id, user_id, balance, diamonds) "
        "VALUES (?, ?, ?, ?)", (G, U2, 0, 0))
    await safe_transfer(G, U, U2, 1000, currency="balance", source="test")
    row = sql_query(
        "SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (G, U2))
    check(row and row[0][0] == 1000, "transfer moves exact 1000 (not 1100)",
          str(row))
    # Conversion (coins→diamonds) must use the raw rate, never scaled.
    reset_user(level_xp=20000, level=50, coins=5000, diamonds=0, prestige_val=5)
    conv = await safe_convert(G, U, 5000, 500, reason="test", source="test")
    check(conv["diamonds_gained"] == 10,
          "conversion yields exact 10 diamonds (not 12)", str(conv))
    # Admin deduct (V user) must remove the exact requested amount.
    reset_user(level_xp=20000, level=50, coins=4000, diamonds=0, prestige_val=5)
    await safe_admin_deduct(G, U, 500, currency="balance", source="test")
    row = sql_query(
        "SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (G, U))
    check(row and row[0][0] == 3500, "admin deduct exact 500 (not 550)",
          str(row))

    section("12) Existing user data intact / legacy levels.prestige clamps")
    # Legacy value > 5 clamps to V (max permanent) without rewriting the row.
    reset_user(level_xp=5000, level=30, coins=10, diamonds=2, prestige_val=9)
    perm = await get_permanent_prestige(G, U)
    check(perm == 5, "legacy prestige 9 clamps to V on read", str(perm))
    row = sql_query(
        "SELECT prestige FROM levels WHERE guild_id=? AND user_id=?", (G, U))
    check(row and row[0][0] == 9, "legacy level row NOT rewritten (still 9)",
          str(row))
    # "existing users/data remain intact" — xp/level/coins/diamonds untouched
    # just by reading/querying the new system.
    row = sql_query(
        "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", (G, U))
    check(row and row[0][0] == 5000 and row[0][1] == 30,
          "existing xp/level preserved", str(row))
    # A user already at max permanent tier (clamped to V) cannot buy again.
    reset_user(level_xp=6000, level=33, coins=99999, diamonds=4, prestige_val=8)
    try:
        await purchase_prestige(G, U, 3, 5000, item_name="Prestige III")
        check(False, "max-tier user rejected on any purchase")
    except PrestigeError as e:
        check("maximum permanent" in str(e), "max-tier message returned", str(e))

    section("13) Old /prestige XP-reset behavior can no longer execute")
    # Static verification: no production command/cog calls the retired
    # perform_prestige() reset helper anymore. Only the new prestige module
    # and the read-only /prestige view should reference Prestige state.
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = subprocess.run(
        ["grep", "-rn", "perform_prestige", "--include=*.py", f"{root}"],
        capture_output=True, text=True).stdout
    # Allow the (now-unused) definition inside xp_calculator.py, but no
    # call-sites in cogs/ or dashboard/.
    offending = [l for l in src.splitlines()
                 if l and "/cogs/" in l or ("/dashboard/" in l)]
    check(not offending, "no production call-site for perform_prestige()",
          "\n".join(offending))

    # The /prestige command must no longer reset XP. It's a read-only viewer.
    lvl_src = open(os.path.join(root, "cogs/leveling.py")).read()
    check("reset past a level threshold" not in lvl_src,
          "/prestige grind description removed from command")
    check("View your Prestige state" in lvl_src,
          "/prestige is now read-only view")

    # An existing economy row must NEVER be modified by merely enabling/reading
    # the prestige system (no auto-migration/destructive transform).
    reset_user(level_xp=2222, level=22, coins=4444, diamonds=3, prestige_val=2)
    before = sql_query(
        "SELECT xp, level, balance, diamonds FROM levels l "
        "JOIN economy e ON e.guild_id=l.guild_id AND e.user_id=l.user_id "
        "WHERE l.guild_id=? AND l.user_id=?", (G, U))
    await get_prestige_earn_multiplier(G, U, "balance", is_booster=False)
    await get_effective_prestige(G, U, is_booster=False)
    after = sql_query(
        "SELECT xp, level, balance, diamonds FROM levels l "
        "JOIN economy e ON e.guild_id=l.guild_id AND e.user_id=l.user_id "
        "WHERE l.guild_id=? AND l.user_id=?", (G, U))
    check(before == after, "read-only prestige paths never mutate user data")

    section("14) End-to-end earn-time multiplier via reward_engine")
    # give_reward routes coins through safe_credit, which is where the
    # earn-time multiplier must land. A permanent-V member (non-booster)
    # earning 100 coins must receive 110; a booster earning 100 coins must
    # receive 110 and 100 diamonds → 120.
    from utils.reward_engine import give_reward

    class FakeMember:
        def __init__(self, premium_since=None):
            self.premium_since = premium_since

    class FakeGuild:
        def __init__(self, member):
            self._member = member
        def get_member(self, user_id):
            return self._member

    class FakeBot:
        def __init__(self, member):
            self._guild = FakeGuild(member)
        def get_guild(self, guild_id):
            return self._guild

    # Permanent V, not a booster → 1.1x coins.
    reset_user(level_xp=20000, level=50, coins=0, diamonds=0, prestige_val=5)
    res = await give_reward(None, G, U, "coins", amount=100,
                            reason="test", source="test")
    check(res.get("success"), "reward_engine coins grant succeeds")
    check(res.get("amount") == 110, "reward_engine applies 1.1x on coins",
          str(res.get("amount")))
    row = sql_query(
        "SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (G, U))
    check(row and row[0][0] == 110, "coins balance actually 110",
          str(row))

    # Permanent I, booster → effective VI → 1.1x coins + 1.2x diamonds.
    reset_user(level_xp=20000, level=50, coins=0, diamonds=0, prestige_val=1)
    bot = FakeBot(FakeMember(premium_since="2026-01-01"))
    await give_reward(bot, G, U, "coins", amount=100, reason="test",
                      source="test")
    await give_reward(bot, G, U, "diamonds", amount=100, reason="test",
                      source="test")
    row = sql_query(
        "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
        (G, U))
    check(row and row[0][0] == 110, "booster coins 110 (1.1x)", str(row))
    check(row and row[0][1] == 120, "booster diamonds 120 (1.2x)", str(row))

    # Loss of booster → permanent V (non-booster) → only 1.1x coins, 1.0x dia.
    reset_user(level_xp=20000, level=50, coins=0, diamonds=0, prestige_val=5)
    await give_reward(None, G, U, "diamonds", amount=100, reason="test",
                      source="test")
    row = sql_query(
        "SELECT diamonds FROM economy WHERE guild_id=? AND user_id=?", (G, U))
    check(row and row[0][0] == 100, "non-booster diamonds 100 (1.0x)",
          str(row))

    print(f"\n{'='*50}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
