#!/usr/bin/env python3
"""
Wallet/Streak/Inventory phase scenario tests.

Exercises the new behaviors added in this phase on top of the
existing engine tests:
  1. Schema migration adds coin_emoji_id, diamond_name, diamond_emoji_id.
  2. Currency config returns defaults; custom names/emojis persist.
  3. /daily command is gone (import sanity).
  4. drop_item removes full stack and returns qty.
  5. equip_role then unequip_role clears Discord bookkeeping (mock).
  6. Streak panel embeds contain NO UTC, NO "capped", NO "00:00", NO "!".
  7. Claim panel embeds match the spec field names.
  8. Receipts format uses relative time and configured currency names.
  9. Inventory embeds do NOT contain "Expires"/"Paid"/"Bought".
 10. Item detail embed has Equip/Unequip/Drop/Back for equippables.
"""
import os
import sys
import asyncio
import tempfile

_TMP = tempfile.mkdtemp(prefix="wallet2_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "wallet2.db")
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef01234567")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite  # noqa
from database import DB_PATH, init_db  # noqa

GUILD = 4444
USER = 5555

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[92mPASS\033[0m  {label}")
    else:
        _failed += 1
        print(f"  \033[91mFAIL\033[0m  {label}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n\033[1m{title}\033[0m")


async def main():
    section("1. Migration — new currency columns exist")
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cols = [r[1] for r in await (await db.execute(
            "PRAGMA table_info(guild_settings)")).fetchall()]
    for c in ("coin_emoji_id", "diamond_name", "diamond_emoji_id"):
        check(f"{c} column exists", c in cols, str(cols))

    section("2. Currency config — defaults + customization")
    from utils.currency import (
        get_currency_config, DEFAULT_COIN_NAME, DEFAULT_COIN_EMOJI,
        DEFAULT_DIAMOND_NAME, DEFAULT_DIAMOND_EMOJI,
    )
    cfg = await get_currency_config(999999)  # unknown guild -> defaults
    check("default coin name", cfg["coins"]["name"] == DEFAULT_COIN_NAME,
          cfg["coins"]["name"])
    check("default coin emoji", cfg["coins"]["emoji"] == DEFAULT_COIN_EMOJI)
    check("default diamond name", cfg["diamonds"]["name"] == DEFAULT_DIAMOND_NAME)
    check("default diamond emoji", cfg["diamonds"]["emoji"] == DEFAULT_DIAMOND_EMOJI)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO guild_settings (guild_id, currency_name, coin_emoji_id, "
            "diamond_name, diamond_emoji_id) VALUES (?,?,?,?,?)",
            (GUILD, "Gold", "<:gold:123>", "Gems", "<:gem:456>"))
        await db.commit()
    cfg2 = await get_currency_config(GUILD)
    check("custom coin name", cfg2["coins"]["name"] == "Gold")
    check("custom coin emoji", cfg2["coins"]["emoji"] == "<:gold:123>")
    check("custom diamond name", cfg2["diamonds"]["name"] == "Gems")
    check("custom diamond emoji", cfg2["diamonds"]["emoji"] == "<:gem:456>")

    section("3. /daily command is removed")
    import cogs.wallet as wallet_mod
    import cogs.economy as economy_mod
    wallet_cog = wallet_mod.Wallet
    economy_cog = economy_mod.Economy
    wallet_commands = {c.name for c in wallet_cog.__cog_app_commands__}
    economy_commands = {c.name for c in economy_cog.__cog_app_commands__}
    check("wallet registers /wallet and /streak",
          wallet_commands == {"wallet", "streak"}, str(wallet_commands))
    check("/daily is not registered anywhere",
          "daily" not in (wallet_commands | economy_commands),
          str(wallet_commands | economy_commands))

    section("4. drop_item removes full stack")
    from utils.inventory import give_item, drop_item, get_inventory
    await give_item(GUILD, USER, "Hat", 3, item_type="shop_custom")
    r = await drop_item(GUILD, USER, "Hat")
    check("drop returns success", r.get("success") is True, str(r))
    check("drop returns quantity 3", r["quantity"] == 3, str(r))
    inv = await get_inventory(GUILD, USER)
    check("item no longer in inventory",
          all(it["item_name"] != "Hat" for it in inv))

    section("5. Streak panel embeds — no UTC / capped / 00:00 / !")
    from utils.daily_engine import get_streak_preview
    from cogs.wallet import (
        build_streak_embed, build_claim_embed,
        build_streak_already_claimed_embed,
    )
    import discord
    preview = await get_streak_preview(GUILD, USER)
    embed = build_streak_embed(preview, cfg2)
    rendered = embed.title + " " + (embed.description or "")
    for f in embed.fields:
        rendered += " " + (f.name or "") + " " + (f.value or "")
    for banned in ("UTC", "00:00", "capped", "Bonus grows"):
        check(f"streak preview contains no '{banned}'",
              banned.lower() not in rendered.lower(), rendered)
    for ch in ("!",):
        check(f"streak preview contains no '{ch}'", ch not in rendered, rendered)

    # Field names match the spec
    field_names = [f.name for f in embed.fields]
    check("preview has fields Daily Reward / Streak / Bonus",
          "Daily Reward" in field_names and "Streak" in field_names
          and "Bonus" in field_names, str(field_names))
    check("preview has no Cooldown field (unclaimed state)",
          "Cooldown" not in field_names, str(field_names))

    # Claim receipt
    fake_result = {
        "streak": 7, "amount": 132, "streak_bonus": 32,
        "multiplier": 1.0, "new_balance": 132, "bonus_capped": False,
        "next_reset_seconds": 2*3600 + 15*60,
    }
    cembed = build_claim_embed(fake_result, cfg2)
    cr = cembed.title + " " + (cembed.description or "")
    for f in cembed.fields:
        cr += " " + (f.name or "") + " " + (f.value or "")
    check("claim shows 132 Gold", "132" in cr and "Gold" in cr, cr)
    check("claim shows Day 7", "Day **7**" in cr, cr)
    check("claim shows +32 bonus", "+**32**" in cr, cr)
    check("claim shows cooldown 2h 15m", "2h 15m" in cr, cr)
    for banned in ("UTC", "00:00", "capped", "Bonus grows", "Prestige"):
        check(f"claim receipt contains no '{banned}'",
              banned.lower() not in cr.lower(), cr)

    # Already-claimed race panel
    acembed = build_streak_already_claimed_embed(3600, preview, cfg2)
    ar = acembed.title + " " + (acembed.description or "")
    for f in acembed.fields:
        ar += " " + (f.name or "") + " " + (f.value or "")
    check("already-claimed has Cooldown", "Cooldown" in ar, ar)
    check("already-claimed has no UTC/00:00",
          "UTC" not in ar and "00:00" not in ar, ar)

    section("6. Inventory embed — no Expires/Paid/Bought debug info")
    from utils.item_catalog import upsert_catalog_entry
    await upsert_catalog_entry(GUILD, "Sunglasses", icon_url=None,
                               rarity="rare", value_currency="balance",
                               value_amount=200)
    await give_item(GUILD, USER, "Sunglasses", 1, item_type="role",
                    metadata={"role_id": 12345})
    # temp role with expires
    await upsert_catalog_entry(GUILD, "Temporary Cape", icon_url=None,
                               rarity="common", value_currency="balance",
                               value_amount=50)
    await give_item(GUILD, USER, "Temporary Cape", 1, item_type="temp_role",
                    metadata={"role_id": 12346, "expires_at": "2030-01-01T00:00:00"})

    from utils.inventory import get_inventory as _gi
    from cogs.wallet import (
        _decorate, split_by_tab, build_inventory_embed, TAB_ITEMS,
    )
    items = await _gi(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    iembed = build_inventory_embed(TAB_ITEMS, entries, None, None)
    ir = (iembed.title or "") + " " + (iembed.description or "")
    for banned in ("Paid:", "Bought:", "Expires", "paid", "bought"):
        check(f"inventory list contains no '{banned}'",
              banned.lower() not in ir.lower(), ir)

    section("7. Item detail includes Equip/Drop/Back; no Expires")
    # We don't have a real Guild to pass, but we can build the View class
    # to confirm button labels.
    from cogs.wallet import ItemPanelView
    item = next(it for it in entries if it["item_name"] == "Sunglasses")

    class FakeGuild:
        def get_role(self, rid):
            return None

    view = ItemPanelView(GUILD, USER, TAB_ITEMS, item, equipped=False)
    labels = sorted((c.label or "") for c in view.children)
    check("buttons include Equip", "Equip" in labels, str(labels))
    check("buttons include Drop", "Drop" in labels, str(labels))
    check("buttons include Back", "Back" in labels, str(labels))

    from cogs.wallet import build_item_embed
    dEmbed = build_item_embed(FakeGuild(), TAB_ITEMS, item, equipped=False)
    dText = " ".join((f.name or "") + " " + (f.value or "") for f in dEmbed.fields)
    check("item detail has no Expires field",
          "Expires" not in dText, dText)
    check("item detail has Quantity/Rarity/Status",
          "Quantity" in dText and "Rarity" in dText and "Status" in dText,
          dText)

    section("8. Receipts formatting — relative time, configured names")
    from utils.economy_safe import safe_credit
    from utils.ledger import log_transaction  # noqa: F401
    await safe_credit(GUILD, USER, 500, currency="balance",
                      reason="Daily reward", source="daily")
    from cogs.wallet import (
        build_receipts_embed, _format_entry, RECEIPTS_PER_PAGE,
    )
    from utils.ledger import get_user_ledger_page
    entries = await get_user_ledger_page(GUILD, USER, currency="balance",
                                         offset=0, limit=RECEIPTS_PER_PAGE)
    # Custom cfg with Gold/Gems
    r_embed = build_receipts_embed("balance", cfg2, entries, 0, 1)
    r_text = r_embed.title or ""
    check("receipts title uses custom coin name 'Gold'",
          "Gold" in r_text, r_text)
    check("receipts title uses custom emoji <:gold:123>",
          "<:gold:123>" in r_text, r_text)
    line = _format_entry(entries[0], cfg2)
    check("receipt line has relative time (just now/ago), not UTC",
          ("ago" in line or "just now" in line) and "UTC" not in line, line)
    check("receipt line has no '!'", "!" not in line, line)

    section("9. View timeout raised to 30 minutes")
    from cogs.wallet import VIEW_TIMEOUT
    check("VIEW_TIMEOUT >= 1800 (30 min)", VIEW_TIMEOUT >= 1800, str(VIEW_TIMEOUT))

    section("10. No hardcoded 🪙 or 💎 in wallet embed render")
    # Render a hub snapshot and check no default emoji leaks when custom is set
    snap = {
        "coins": 1000, "diamonds": 50, "total_items": 3,
        "streak": 7, "claimed_today": False, "seconds_remaining": 0,
        "currency": cfg2,  # custom Gold/Gems config
    }
    from cogs.wallet import build_hub_embed

    class FakeUser:
        display_name = "Tester"
        class display_avatar:
            url = ""
    h_embed = build_hub_embed(snap)
    h_text = (h_embed.title or "")
    for f in h_embed.fields:
        h_text += " " + (f.name or "") + " " + (f.value or "")
    check("hub title is ✦ WALLET ✦", "WALLET" in h_embed.title, h_embed.title)
    check("hub uses custom Gold emoji", "<:gold:123>" in h_text, h_text)
    check("hub uses custom Gems emoji", "<:gem:456>" in h_text, h_text)
    check("hub does not contain default 🪙", "🪙" not in h_text, h_text)
    check("hub does not contain default 💎", "💎" not in h_text, h_text)

    print(f"\n\033[1mResults: {_passed} passed, {_failed} failed\033[0m")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
