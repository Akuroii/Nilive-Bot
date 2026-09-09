#!/usr/bin/env python3
"""
Wallet system — verification suite.

Runs the real engines against a scratch SQLite DB created by the
project's own init_db(), so schema drift between this test and
database.py is impossible.

Covers:
  1.  init_db() creates equipped_titles and is idempotent (runs twice).
  2.  Streak claim credits coins and writes a ledger row (source='daily').
  3.  A second claim on the same UTC day is rejected (shared guard).
  4.  /streak, /daily and the Wallet button share ONE claim guard.
  5.  Streak continues (+1) across consecutive days, resets after a gap.
  6.  Bonus caps at day 7 while the streak COUNT keeps climbing.
  7.  get_streak_state reports a broken chain as streak 0, not the stale count.
  8.  get_streak_preview never consumes a claim.
  9.  Title equip / unequip, and independence from the role slot.
  10. Equipping a title you don't own is rejected.
  11. cleanup_unowned_title clears a title that was removed from inventory.
  12. Potion use consumes exactly one copy and grants the XP boost.
  13. Using the last potion leaves quantity 0 and cannot be used again.
  14. A misconfigured potion is rejected WITHOUT being consumed.
  15. A potion whose effect grant fails is refunded (no silent loss).
  16. Receipts pagination: no duplicate/skipped rows across pages.
  17. Receipts are currency-filtered and per-user isolated.
  18. Inventory tab split routes each item_type to exactly one tab.
  19. Quantities are aggregated per item, not one row per copy.
  20. Concurrent double-claim: only one succeeds.

Run:
  .venv/bin/python scripts/test_wallet.py
"""
import os
import sys
import asyncio
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="wallet_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "wallet.db")
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import aiosqlite  # noqa: E402
from database import DB_PATH, init_db  # noqa: E402

GUILD = 1111
USER = 2222
OTHER = 3333

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[92mPASS\033[0m  {label}")
    else:
        _failed += 1
        print(f"  \033[91mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


def section(title: str):
    print(f"\n\033[1m{title}\033[0m")


async def reset_user(user_id=USER):
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("daily_claims", "economy", "inventory_items",
                      "equipped_titles", "equipped_roles",
                      "transaction_ledger", "leveling_active_boosts"):
            await db.execute(
                f"DELETE FROM {table} WHERE guild_id=? AND user_id=?",
                (GUILD, user_id))
        await db.commit()


async def set_claim_date(date_str: str, streak: int, user_id=USER):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO daily_claims
                (guild_id, user_id, last_claim_date, streak_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                last_claim_date = excluded.last_claim_date,
                streak_count    = excluded.streak_count
        """, (GUILD, user_id, date_str, streak))
        await db.commit()


async def main():
    from datetime import datetime, timezone, timedelta

    section("1. Schema / migrations")
    await init_db()
    await init_db()  # idempotency
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='equipped_titles'")
        exists = await cursor.fetchone()
        cursor = await db.execute("PRAGMA table_info(equipped_titles)")
        cols = [c[1] for c in await cursor.fetchall()]
    check("init_db() runs twice without error (idempotent)", True)
    check("equipped_titles table created", bool(exists))
    check("equipped_titles has the expected columns",
          set(cols) == {"guild_id", "user_id", "item_name", "equipped_at"},
          str(cols))

    from utils.daily_engine import (
        perform_streak_claim, get_streak_state, get_streak_preview,
        get_streak_bonus, DailyAlreadyClaimed, STREAK_BONUS_CAP_DAYS,
        format_remaining,
    )
    from utils.economy_safe import get_balance
    from utils.ledger import get_user_ledger_page, count_user_ledger

    section("2. Streak claim — crediting and ledger")
    await reset_user()
    result = await perform_streak_claim(None, GUILD, USER)
    balance = await get_balance(GUILD, USER)
    check("claim returns a positive amount", result["amount"] > 0,
          str(result))
    check("balance equals the credited amount", balance == result["amount"],
          f"{balance} vs {result['amount']}")
    check("first claim is streak day 1", result["streak"] == 1)
    ledger = await get_user_ledger_page(GUILD, USER, currency="balance")
    check("claim wrote exactly one ledger row", len(ledger) == 1)
    check("ledger row is sourced 'daily'",
          ledger and ledger[0]["source"] == "daily",
          str(ledger[:1]))
    check("ledger amount matches the credit",
          ledger and ledger[0]["amount"] == result["amount"])

    section("3. Same-day re-claim is rejected")
    rejected = False
    seconds = None
    try:
        await perform_streak_claim(None, GUILD, USER)
    except DailyAlreadyClaimed as e:
        rejected = True
        seconds = e.seconds_remaining
    check("second claim raises DailyAlreadyClaimed", rejected)
    check("remaining seconds are within one UTC day",
          seconds is not None and 0 < seconds <= 86400, str(seconds))
    check("balance unchanged by the rejected claim",
          await get_balance(GUILD, USER) == balance)
    check("format_remaining renders hours+minutes",
          format_remaining(67320) == "18h 42m", format_remaining(67320))
    check("format_remaining handles sub-hour", format_remaining(2520) == "42m")

    section("4. Shared guard across all three entry points")
    # /streak, /daily and the Wallet button are all the same call; the
    # guard lives in daily_claims, so any second attempt must fail.
    blocked = 0
    for _ in range(3):
        try:
            await perform_streak_claim(None, GUILD, USER)
        except DailyAlreadyClaimed:
            blocked += 1
    check("all further entry-point attempts blocked", blocked == 3)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transaction_ledger "
            "WHERE guild_id=? AND user_id=? AND source='daily'",
            (GUILD, USER))
        daily_rows = (await cursor.fetchone())[0]
    check("still exactly one daily credit in the ledger", daily_rows == 1,
          str(daily_rows))

    section("5. Streak continuation and reset")
    await reset_user()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    await set_claim_date(yesterday, 4)
    r = await perform_streak_claim(None, GUILD, USER)
    check("claiming the day after continues the streak", r["streak"] == 5,
          str(r["streak"]))

    await reset_user()
    old = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    await set_claim_date(old, 9)
    r = await perform_streak_claim(None, GUILD, USER)
    check("claiming after a gap resets to day 1", r["streak"] == 1,
          str(r["streak"]))

    section("6. Bonus caps at day 7, count does not")
    await reset_user()
    await set_claim_date(yesterday, 30)
    r = await perform_streak_claim(None, GUILD, USER)
    bonus_at_31 = await get_streak_bonus(GUILD, 31)
    bonus_at_cap = await get_streak_bonus(GUILD, STREAK_BONUS_CAP_DAYS)
    check("streak count keeps climbing past 7", r["streak"] == 31,
          str(r["streak"]))
    check("bonus is capped at the day-7 value", bonus_at_31 == bonus_at_cap,
          f"{bonus_at_31} vs {bonus_at_cap}")
    check("claim reports bonus_capped", r["bonus_capped"] is True)

    section("7. get_streak_state reports a broken chain honestly")
    await reset_user()
    await set_claim_date(old, 9)   # 3 days ago -> chain broken
    state = await get_streak_state(GUILD, USER)
    check("broken chain reports current streak 0", state["streak"] == 0,
          str(state))
    check("stored count is still preserved", state["stored_streak"] == 9)
    check("not marked as claimed today", state["claimed_today"] is False)

    await set_claim_date(yesterday, 3)
    state = await get_streak_state(GUILD, USER)
    check("live chain reports the real streak", state["streak"] == 3)

    section("8. Preview never consumes a claim")
    await reset_user()
    before = await get_balance(GUILD, USER)
    preview = await get_streak_preview(GUILD, USER)
    after = await get_balance(GUILD, USER)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM daily_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
        claim_rows = (await cursor.fetchone())[0]
    check("preview did not credit anything", before == after == 0)
    check("preview did not write a claim row", claim_rows == 0)
    check("preview shows next streak as 1 for a new member",
          preview["next_streak"] == 1, str(preview["next_streak"]))
    check("preview exposes the configured daily range",
          preview["daily_min"] == 100 and preview["daily_max"] == 300,
          f"{preview['daily_min']}-{preview['daily_max']}")

    from utils.inventory import give_item, get_inventory, remove_item
    from utils.title_engine import (
        equip_title, unequip_title, get_equipped_title, cleanup_unowned_title,
    )

    section("9. Titles — equip / unequip, independent of the role slot")
    await reset_user()
    await give_item(GUILD, USER, "The Silent One", 1, item_type="title")
    await give_item(GUILD, USER, "Nightfall", 1, item_type="title")
    # Simulate an equipped ROLE item, the other slot.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO equipped_roles (guild_id, user_id, item_name, role_id)
            VALUES (?, ?, 'Flame', 4242)
        """, (GUILD, USER))
        await db.commit()

    r = await equip_title(GUILD, USER, "The Silent One")
    check("equipping an owned title succeeds", r.get("success") is True, str(r))
    check("equipped title is readable",
          (await get_equipped_title(GUILD, USER))["item_name"] == "The Silent One")

    from utils.equip_engine import get_equipped
    role_slot = await get_equipped(GUILD, USER)
    check("equipping a title did NOT disturb the role slot",
          role_slot and role_slot["item_name"] == "Flame", str(role_slot))

    await equip_title(GUILD, USER, "Nightfall")
    check("equipping a second title replaces the first (one slot)",
          (await get_equipped_title(GUILD, USER))["item_name"] == "Nightfall")

    r = await unequip_title(GUILD, USER, "Nightfall")
    check("unequip succeeds", r.get("success") is True)
    check("no title equipped afterwards",
          await get_equipped_title(GUILD, USER) is None)
    check("role slot STILL untouched after title unequip",
          (await get_equipped(GUILD, USER))["item_name"] == "Flame")

    r = await unequip_title(GUILD, USER, "Nightfall")
    check("unequipping an already-unequipped title is rejected cleanly",
          r.get("success") is False and "error" in r, str(r))

    section("10. Titles — ownership is enforced")
    r = await equip_title(GUILD, USER, "A Title I Never Bought")
    check("equipping an unowned title is rejected",
          r.get("success") is False, str(r))
    await give_item(GUILD, USER, "Just An Item", 1, item_type="shop_custom")
    r = await equip_title(GUILD, USER, "Just An Item")
    check("equipping a non-title item is rejected",
          r.get("success") is False, str(r))

    section("11. Stale equipped title is cleaned up")
    await equip_title(GUILD, USER, "The Silent One")
    await remove_item(GUILD, USER, "The Silent One", 1)
    await cleanup_unowned_title(GUILD, USER)
    check("title no longer owned is auto-unequipped",
          await get_equipped_title(GUILD, USER) is None)

    from utils.potion_engine import (
        use_potion, build_metadata, EFFECT_XP_BOOST, is_usable,
        get_active_effects,
    )

    section("12. Potions — use consumes one copy and grants the effect")
    await reset_user()
    await give_item(GUILD, USER, "Elixir", 3, item_type="potion",
                    metadata=build_metadata(EFFECT_XP_BOOST, 2.0, 4))
    r = await use_potion(GUILD, USER, "Elixir")
    check("use succeeds", r.get("success") is True, str(r))
    check("exactly one copy consumed", r["remaining"] == 2, str(r.get("remaining")))
    effects = await get_active_effects(GUILD, USER)
    check("an active XP effect now exists", len(effects) == 1, str(effects))
    check("effect carries the configured multiplier",
          effects and effects[0]["multiplier"] == 2.0)
    check("effect is sourced 'potion'",
          effects and effects[0]["source"] == "potion", str(effects[:1]))

    from utils.xp_calculator import get_active_boost_multiplier
    check("the existing XP pipeline sees the boost",
          await get_active_boost_multiplier(GUILD, USER) == 2.0)

    section("13. Using the last potion")
    await use_potion(GUILD, USER, "Elixir")
    r = await use_potion(GUILD, USER, "Elixir")
    check("last copy consumed leaves remaining 0", r["remaining"] == 0, str(r))
    r = await use_potion(GUILD, USER, "Elixir")
    check("using an empty stack is rejected", r.get("success") is False, str(r))
    inv = await get_inventory(GUILD, USER, include_empty=False)
    check("empty potion no longer listed in inventory",
          all(it["item_name"] != "Elixir" for it in inv), str(inv))

    section("14. Misconfigured potion is rejected, not consumed")
    await give_item(GUILD, USER, "Broken Brew", 2, item_type="potion",
                    metadata={"effect": "xp_boost"})  # no multiplier/duration
    check("is_usable() rejects the bad metadata",
          is_usable({"effect": "xp_boost"}) is False)
    r = await use_potion(GUILD, USER, "Broken Brew")
    check("using it is rejected", r.get("success") is False, str(r))
    inv = await get_inventory(GUILD, USER, include_empty=False)
    qty = next(it["quantity"] for it in inv if it["item_name"] == "Broken Brew")
    check("nothing was consumed", qty == 2, str(qty))

    section("15. Failed effect grant refunds the potion")
    import utils.xp_calculator as xpcalc
    await give_item(GUILD, USER, "Cursed Vial", 1, item_type="potion",
                    metadata=build_metadata(EFFECT_XP_BOOST, 3.0, 2))
    original = xpcalc.grant_xp_boost

    async def exploding_grant(*a, **kw):
        raise RuntimeError("simulated DB failure")

    xpcalc.grant_xp_boost = exploding_grant
    try:
        r = await use_potion(GUILD, USER, "Cursed Vial")
    finally:
        xpcalc.grant_xp_boost = original
    check("failure is reported to the caller", r.get("success") is False, str(r))
    inv = await get_inventory(GUILD, USER, include_empty=False)
    qty = next((it["quantity"] for it in inv
                if it["item_name"] == "Cursed Vial"), 0)
    check("the consumed copy was refunded", qty == 1, str(qty))
    meta = next((it["metadata"] for it in inv
                 if it["item_name"] == "Cursed Vial"), None)
    check("refund preserved the effect metadata",
          meta and meta.get("multiplier") == 3.0, str(meta))

    section("16. Receipts pagination integrity")
    await reset_user()
    from utils.economy_safe import safe_credit
    for i in range(23):
        await safe_credit(GUILD, USER, 10 + i, currency="balance",
                          reason=f"row {i}", source="test")
    total = await count_user_ledger(GUILD, USER, currency="balance")
    check("count matches what was written", total == 23, str(total))

    seen = []
    page = 0
    while True:
        rows = await get_user_ledger_page(
            GUILD, USER, currency="balance",
            offset=page * 8, limit=8)
        if not rows:
            break
        seen.extend(r["id"] for r in rows)
        page += 1
    check("paging visits every row exactly once",
          len(seen) == 23 and len(set(seen)) == 23,
          f"{len(seen)} rows, {len(set(seen))} unique")
    check("rows come back newest-first",
          seen == sorted(seen, reverse=True))
    check("page count is 3 for 23 rows at 8/page", page == 3, str(page))

    section("17. Receipts isolation — per currency and per user")
    await safe_credit(GUILD, USER, 5, currency="diamonds",
                      reason="gem", source="test")
    await safe_credit(GUILD, OTHER, 999, currency="balance",
                      reason="someone else", source="test")
    coins = await count_user_ledger(GUILD, USER, currency="balance")
    gems = await count_user_ledger(GUILD, USER, currency="diamonds")
    check("coin history excludes diamond rows", coins == 23, str(coins))
    check("diamond history excludes coin rows", gems == 1, str(gems))
    other_rows = await get_user_ledger_page(GUILD, OTHER, currency="balance")
    check("another member's history is separate",
          len(other_rows) == 1 and other_rows[0]["user_id"] == OTHER)
    check("wallet owner never sees the other member's row",
          all(r["user_id"] == USER for r in
              await get_user_ledger_page(GUILD, USER, currency="balance",
                                         limit=50)))
    await reset_user(OTHER)

    section("18. Inventory tab routing")
    from cogs.wallet import split_by_tab, TAB_ITEMS, TAB_POTIONS, TAB_TITLES
    await reset_user()
    await give_item(GUILD, USER, "Glasses", 1, item_type="shop_custom")
    await give_item(GUILD, USER, "Hat", 2, item_type="shop_custom")
    await give_item(GUILD, USER, "Flame", 1, item_type="role",
                    metadata={"role_id": 55})
    await give_item(GUILD, USER, "Elixir", 4, item_type="potion",
                    metadata=build_metadata(EFFECT_XP_BOOST, 2.0, 3))
    await give_item(GUILD, USER, "The Silent One", 1, item_type="title")
    inv = await get_inventory(GUILD, USER, include_empty=False)
    tabs = split_by_tab(inv)
    names = {k: sorted(i["item_name"] for i in v) for k, v in tabs.items()}
    check("Items tab holds items + role items",
          names[TAB_ITEMS] == ["Flame", "Glasses", "Hat"], str(names[TAB_ITEMS]))
    check("Potions tab holds only potions",
          names[TAB_POTIONS] == ["Elixir"], str(names[TAB_POTIONS]))
    check("Titles tab holds only titles",
          names[TAB_TITLES] == ["The Silent One"], str(names[TAB_TITLES]))
    check("every item lands in exactly one tab",
          sum(len(v) for v in tabs.values()) == len(inv))

    section("19. Quantities aggregate per item")
    hat = next(i for i in inv if i["item_name"] == "Hat")
    check("two Hats are one row with quantity 2", hat["quantity"] == 2)
    await give_item(GUILD, USER, "Hat", 3, item_type="shop_custom")
    inv = await get_inventory(GUILD, USER, include_empty=False)
    hat = next(i for i in inv if i["item_name"] == "Hat")
    check("buying more stacks rather than adding rows", hat["quantity"] == 5)
    check("still a single Hat row",
          sum(1 for i in inv if i["item_name"] == "Hat") == 1)

    section("20. Concurrent double-claim")
    await reset_user()
    results = await asyncio.gather(
        perform_streak_claim(None, GUILD, USER),
        perform_streak_claim(None, GUILD, USER),
        return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict)]
    blocked = [r for r in results if isinstance(r, DailyAlreadyClaimed)]
    check("exactly one concurrent claim succeeded", len(ok) == 1, str(results))
    check("the other was rejected as already-claimed", len(blocked) == 1,
          str(results))
    if ok:
        check("balance reflects only one credit",
              await get_balance(GUILD, USER) == ok[0]["amount"])

    print(f"\n\033[1mResults: {_passed} passed, {_failed} failed\033[0m")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
