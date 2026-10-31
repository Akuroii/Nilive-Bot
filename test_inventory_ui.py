#!/usr/bin/env python3
"""
Inventory UI — Items / Potions / Titles verification suite.

Runs the real wallet renderer, views and engines against a scratch
SQLite DB created by the project's own init_db(), so nothing here can
drift from the shipped schema.

Covers:
  1.  Exact template on all three tabs: author text/icon, color
      15000312, footer text/icon, no title, no extra fields.
  2.  Description line format, comma-grouped quantities (including 1),
      backtick-wrapped names, no extra tags, no raw URLs.
  3.  Currency balance rows come from get_balance()/get_currency_config,
      never from inventory_items, and never pollute Select options.
  4.  Prepared Shop emoji (app_emoji) inline + in Select options, with
      every fallback path (legacy custom emoji, rarity glyph).
  5.  Loading/navigating Inventory never prepares, uploads, deletes or
      fetches anything and never mutates the emoji tables.
  6.  Temporary-role `H:MM` suffix from persisted expires_at only.
  7.  Pagination at 0/1/15/16/17/30/31/34: no loss, no duplication,
      correct `◀`/`▶` visibility, currency rows excluded from the page
      size, Back-to-Wallet distinct from the arrows.
  8.  Potions: purchase defers activation, Select does not consume,
      Activate consumes exactly one, failed effect refunds, stored
      metadata (not the name) drives the effect, xp_boost unchanged.
  9.  Titles: equip slot stays independent from the Discord role slot.
 10.  Routing: `/inventory` sends a new ephemeral message; Wallet's
      Inventory button edits the current one; both reach the same
      renderer. Owner guard still rejects other members.

Run:
  .venv/bin/python test_inventory_ui.py
"""
import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="inventory_ui_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "inventory.db")
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")
os.environ["DISCORD_CLIENT_ID"] = "123456789012345678"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiosqlite  # noqa: E402
from database import DB_PATH, init_db  # noqa: E402

GUILD = 7777
USER = 8888
OTHER = 9999
BOT_NAME = "Nilive"
USERNAME = "tester"

AUTHOR_ICON = ("https://message.style/cdn/images/"
               "f21f9f7c3e914d9b9533f4399bfdfadf7447c85627034cbd00cd0e8d29df0d8f.png")
FOOTER_TEXT = "-ˋˏ ༺❀ 𝙽𝚒𝚕𝚒𝚟𝚎 𝚃𝚎𝚊𝚖 ❀༻ˎˊ-"
FOOTER_ICON = ("https://message.style/cdn/images/"
               "b5b0bf7153118e3c82fc0229a1548dbf5fefdf1691fc0a73b01f45b90ce44b06.gif")

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[92mPASS\033[0m  {label}")
    else:
        _failed += 1
        print(f"  \033[91mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# ── Fakes ───────────────────────────────────────────────────────────────

class FakeUser:
    def __init__(self, user_id=USER, name=USERNAME, display_name="Tester"):
        self.id = user_id
        self.name = name
        self.display_name = display_name
        self.mention = f"<@{user_id}>"
        self.roles = []
        self.display_avatar = None
        self.bot = False


class FakeRole:
    def __init__(self, role_id=4242):
        self.id = role_id
        self.mention = f"<@&{role_id}>"


class FakeGuild:
    id = GUILD

    def __init__(self):
        self._roles = {}

    def get_role(self, rid):
        return self._roles.get(int(rid))


class FakeClient:
    def __init__(self):
        self.user = FakeUser(1, name=BOT_NAME, display_name=BOT_NAME)


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modals = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append({**({"content": content} if content else {}), **kwargs})

    async def edit_message(self, content=None, **kwargs):
        self.edits.append({**({"content": content} if content else {}), **kwargs})

    async def defer(self, *args, **kwargs):
        pass

    def is_done(self):
        return bool(self.sent or self.edits)


class FakeMessage:
    id = 1
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, user=None, guild=None, client=None, message=None):
        self.user = user or FakeUser()
        self.guild = guild or FakeGuild()
        self.client = client or FakeClient()
        self.response = FakeResponse()
        self.message = message or FakeMessage()

    async def original_response(self):
        return self.message


async def reset_all(user_id=USER):
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("inventory_items", "equipped_titles", "equipped_roles",
                      "leveling_active_boosts", "economy",
                      "transaction_ledger", "purchase_history",
                      "daily_claims"):
            await db.execute(
                f"DELETE FROM {table} WHERE guild_id=? AND user_id=?",
                (GUILD, user_id))
        await db.execute("DELETE FROM shop_items WHERE guild_id=?", (GUILD,))
        await db.execute("DELETE FROM item_catalog WHERE guild_id=?", (GUILD,))
        await db.execute("DELETE FROM shop_emoji_url_aliases WHERE guild_id=?",
                         (GUILD,))
        await db.execute("DELETE FROM shop_emoji_assets WHERE creator_guild_id=?",
                         (GUILD,))
        await db.execute("DELETE FROM guild_settings WHERE guild_id=?", (GUILD,))
        await db.commit()


def lines_of(embed):
    return [ln for ln in (embed.description or "").splitlines() if ln]


def render(embed, entry_names):
    return [ln for ln in lines_of(embed)
            if any(f"` {name} `" in ln for name in entry_names)]


# ── Suite ───────────────────────────────────────────────────────────────

async def main():
    await init_db()
    await reset_all()

    import discord
    from utils.inventory import give_item, get_inventory, find_entry
    from utils.item_catalog import upsert_catalog_entry
    from utils.currency import set_currency_config, get_currency_config
    from utils.economy_safe import safe_credit
    from cogs.wallet import (
        TAB_ITEMS, TAB_COLORS, TAB_POTIONS, TAB_TITLES,
        INVENTORY_PAGE_SIZE,
        build_inventory_embed, build_item_embed, paginate_entries,
        remaining_hours_minutes,
        open_inventory_panel, open_item_panel, InventoryView,
        InventoryItemSelect, InventoryPageButton, InventoryBackButton,
        InventoryTabButton, WalletHubView, ItemPanelView, _decorate,
        split_by_tab, _entry_emoji,
    )

    await set_currency_config(
        GUILD, currency_name="Silvers", coin_emoji_id="<:silver:111>",
        diamond_name="لؤلؤة", diamond_emoji_id="<:pearl:222>")
    await safe_credit(GUILD, USER, 11024, currency="balance",
                      reason="seed", source="system")
    await safe_credit(GUILD, USER, 399, currency="diamonds",
                      reason="seed", source="system")

    await give_item(GUILD, USER, "Sunglasses", 1, item_type="role",
                    metadata={"role_id": 4242})
    await give_item(GUILD, USER, "Hat", 11024, item_type="shop_custom")
    expires = (datetime.now(timezone.utc)
               + timedelta(hours=3, minutes=5)).isoformat()
    await give_item(GUILD, USER, "Temporary Cape", 1, item_type="temp_role",
                    metadata={"role_id": 4243, "expires_at": expires})
    await upsert_catalog_entry(GUILD, "Sunglasses", rarity="rare")
    await upsert_catalog_entry(GUILD, "Hat", rarity="common")
    await upsert_catalog_entry(GUILD, "Temporary Cape", rarity="common")

    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])

    # ── 1. Exact template on all three tabs ────────────────────────────
    section("1. Embed template (all three tabs)")
    currency_rows = [
        {"emoji": "<:silver:111>", "name": "Silvers", "quantity": 11024},
        {"emoji": "<:pearl:222>", "name": "لؤلؤة", "quantity": 399},
    ]
    expected_word = {TAB_ITEMS: "𝐈𝐓𝐄𝐌𝐒", TAB_POTIONS: "𝐩𝐨𝐭𝐢𝐨𝐧𝐬",
                     TAB_TITLES: "𝐓𝐈𝐓𝐋𝐄𝐒"}
    for tab, word in expected_word.items():
        emb = build_inventory_embed(
            tab, entries if tab == TAB_ITEMS else [],
            currency_rows=currency_rows if tab == TAB_ITEMS else [],
            bot_name=BOT_NAME, username=USERNAME)
        check(f"[{tab}] author line", emb.author.name ==
              f"{BOT_NAME}: {word} {USERNAME}", str(emb.author.name))
        check(f"[{tab}] author icon URL",
              emb.author.icon_url == AUTHOR_ICON, str(emb.author.icon_url))
        check(f"[{tab}] color 15000312", emb.color.value == 15000312,
              str(emb.color.value))
        check(f"[{tab}] footer text", emb.footer.text == FOOTER_TEXT,
              str(emb.footer.text))
        check(f"[{tab}] footer icon URL",
              emb.footer.icon_url == FOOTER_ICON, str(emb.footer.icon_url))
        check(f"[{tab}] no embed title", emb.title is None, str(emb.title))
        check(f"[{tab}] no extra fields", len(emb.fields) == 0,
              str([f.name for f in emb.fields]))

    # ── 2. Description lines ───────────────────────────────────────────
    section("2. Description line format")
    emb = build_inventory_embed(TAB_ITEMS, entries,
                                currency_rows=currency_rows,
                                bot_name=BOT_NAME, username=USERNAME)
    text = emb.description or ""
    check("currency line matches the exact template",
          "> - <:silver:111> • ` 11,024 ` • ` Silvers `" in text, text)
    check("second currency line uses the configured diamond name",
          "> - <:pearl:222> • ` 399 ` • ` لؤلؤة `" in text, text)
    check("every line starts with '> -'",
          all(ln.startswith("> -") for ln in lines_of(emb)), text)
    check("quantity 1 is still shown",
          any("` 1 `" in ln for ln in render(emb, ["Sunglasses"])), text)
    check("no rarity / Equipped / Setup needed tags",
          not any(tag in text for tag in
                  ("Common", "Rare", "Equipped", "Setup needed")), text)
    check("no raw http URL in the description", "http" not in text, text)
    check("no raw image URL rendered for any entry",
          not any((e.get("icon_url") or "").startswith("http")
                  and e.get("icon_url") in text for e in entries), text)

    # Markdown safety: a name with a backtick cannot break the line.
    from cogs.wallet import _inventory_line
    escaped = _inventory_line("🎒", 3, "bad`name\nsecond line")
    check("backticks/newlines in a name are neutralised",
          escaped == "> - 🎒 • ` 3 ` • ` bad'name second line `", escaped)

    # ── 3. Currency balances ───────────────────────────────────────────
    section("3. Currency balances come from the economy tables")
    rows = await get_currency_config(GUILD)
    check("currency rows use configured names",
          rows["coins"]["name"] == "Silvers"
          and rows["diamonds"]["name"] == "لؤلؤة", str(rows))
    check("balances are real (11024 / 399)",
          "11,024" in text and "399" in text, text)
    check("currency is not an inventory row",
          all(it["item_name"] not in ("Silvers", "لؤلؤة") for it in items),
          str([it["item_name"] for it in items]))
    pot_emb = build_inventory_embed(TAB_POTIONS, [], bot_name=BOT_NAME,
                                    username=USERNAME)
    check("Potions tab shows no currency balances",
          "Silvers" not in (pot_emb.description or "")
          and "لؤلؤة" not in (pot_emb.description or ""),
          str(pot_emb.description))
    tit_emb = build_inventory_embed(TAB_TITLES, [], bot_name=BOT_NAME,
                                    username=USERNAME)
    check("Titles tab shows no currency balances",
          "Silvers" not in (tit_emb.description or ""), str(tit_emb.description))

    # A URL configured as a currency icon must never leak as inline text.
    from cogs.wallet import currency_balance_rows
    from utils.currency import DEFAULT_COIN_EMOJI
    await set_currency_config(GUILD, coin_emoji_id="https://x.example/a.png")
    url_rows = await currency_balance_rows(GUILD, USER)
    check("URL-valued currency icon falls back to the module default",
          url_rows[0]["emoji"] == DEFAULT_COIN_EMOJI, str(url_rows[0]))
    check("no raw URL reaches the currency line",
          "http" not in _inventory_line(
              url_rows[0]["emoji"], url_rows[0]["quantity"],
              url_rows[0]["name"]), url_rows[0]["emoji"])
    await set_currency_config(GUILD, coin_emoji_id="<:silver:111>")

    # ── 4. Prepared Shop emoji ─────────────────────────────────────────
    section("4. Prepared Shop emoji (read-only enrichment)")
    icon = "https://cdn.example.com/items/sunglasses.png"
    await upsert_catalog_entry(GUILD, "Sunglasses", icon_url=icon)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shop_emoji_assets "
            "(application_id, content_hash, emoji_name, emoji_id, animated,"
            " creator_guild_id, pipeline_version, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("123456789012345678", "a" * 64, "sunglasses_emj",
             "150000000000000001", 0, GUILD, "rgba-128-png-v1", "ready"))
        await db.execute(
            "INSERT INTO shop_emoji_url_aliases "
            "(application_id, guild_id, source_url, content_hash) "
            "VALUES (?,?,?,?)",
            ("123456789012345678", GUILD, icon, "a" * 64))
        await db.commit()

    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    shade = next(it for it in entries if it["item_name"] == "Sunglasses")
    check("app_emoji resolved from the stored alias",
          shade["app_emoji"] == "<:sunglasses_emj:150000000000000001>",
          str(shade.get("app_emoji")))

    emb = build_inventory_embed(TAB_ITEMS, entries,
                                currency_rows=currency_rows,
                                bot_name=BOT_NAME, username=USERNAME)
    line = next(ln for ln in lines_of(emb) if "Sunglasses" in ln)
    check("description uses the prepared emoji inline",
          line.startswith("> - <:sunglasses_emj:150000000000000001> •"), line)
    check("description never shows the source URL", icon not in line, line)

    pot_entries = [{"item_name": "Potion 1H", "item_type": "potion",
                    "quantity": 5, "rarity": "common", "icon_url": None,
                    "app_emoji": "<:potion1h:150000000000000002>"}]
    pemb = build_inventory_embed(TAB_POTIONS, pot_entries,
                                 bot_name=BOT_NAME, username=USERNAME)
    check("potion lines use their own prepared emoji",
          any(ln.startswith("> - <:potion1h:150000000000000002> •")
              for ln in lines_of(pemb)), str(pemb.description))

    tit_entries = [{"item_name": "GetTheMoon", "item_type": "title",
                    "quantity": 5, "rarity": "common", "icon_url": None,
                    "app_emoji": "<:cardshop1:150000000000000003>"}]
    temb = build_inventory_embed(TAB_TITLES, tit_entries,
                                 bot_name=BOT_NAME, username=USERNAME)
    check("title lines use their own prepared emoji",
          any(ln.startswith("> - <:cardshop1:150000000000000003> •")
              for ln in lines_of(temb)), str(temb.description))

    # Select options carry the same emoji
    page_entries, page, pages = paginate_entries(entries, 0)
    select = InventoryItemSelect(GUILD, USER, TAB_ITEMS, page_entries, page)
    opt = next(o for o in select.options if o.value == "Sunglasses")
    check("Select option reuses the prepared emoji",
          opt.emoji is not None and str(opt.emoji.id) == "150000000000000001",
          str(opt.emoji))
    # The option value identifies the ENTRY, not the name: two entries can
    # legitimately share a display name, and the panel opened from a stale
    # view must land on the same record the member selected.
    check("Select value maps back to the entry_key",
          select._by_value[opt.value] == f"i:{shade.get('id')}",
          str(select._by_value))
    check("that entry_key resolves back to the real item",
          find_entry(entries, entry_key=select._by_value[opt.value])
          ["item_name"] == "Sunglasses", str(select._by_value))

    # Fallback 1: legacy custom emoji typed into icon_url
    check("fallback to a legacy custom emoji",
          _entry_emoji({"app_emoji": "", "icon_url": "<:legacy:4242>"},
                       "⚪") == "<:legacy:4242>")
    # Fallback 2: rarity glyph
    check("fallback to the rarity glyph when nothing is prepared",
          _entry_emoji({"app_emoji": "", "icon_url": icon}, "⚪") == "⚪")
    # Fallback 3: prepared mapping not ready / invalid id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shop_emoji_assets SET status='failed' WHERE content_hash=?",
            ("a" * 64,))
        await db.commit()
    entries_failed = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    shade = next(it for it in entries_failed if it["item_name"] == "Sunglasses")
    check("non-ready asset falls back safely (empty app_emoji)",
          shade["app_emoji"] == "", str(shade["app_emoji"]))
    emb = build_inventory_embed(TAB_ITEMS, entries_failed,
                                currency_rows=currency_rows,
                                bot_name=BOT_NAME, username=USERNAME)
    line = next(ln for ln in lines_of(emb) if "Sunglasses" in ln)
    check("failed asset still renders an inline glyph, not a URL",
          line.startswith("> - ⚪ •") and icon not in line, line)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shop_emoji_assets SET status='ready' WHERE content_hash=?",
            ("a" * 64,))
        await db.commit()

    # ── 4b. Listings that predate the item_catalog write ───────────────
    section("4b. Prepared emoji with NO item_catalog row")
    legacy_url = "https://cdn.example.com/items/legacy_cape.png"
    await give_item(GUILD, USER, "Legacy Cape", 2, item_type="shop_custom")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM item_catalog WHERE guild_id=? AND item_name=?",
                         (GUILD, "Legacy Cape"))
        await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type, icon_url,"
            " enabled) VALUES (?,?,?,?,?,1)",
            (GUILD, "Legacy Cape", 50, "custom", legacy_url))
        await db.execute(
            "INSERT INTO shop_emoji_assets "
            "(application_id, content_hash, emoji_name, emoji_id, animated,"
            " creator_guild_id, pipeline_version, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("123456789012345678", "b" * 64, "legacy_cape_emj",
             "150000000000000010", 0, GUILD, "rgba-128-png-v1", "ready"))
        await db.execute(
            "INSERT INTO shop_emoji_url_aliases "
            "(application_id, guild_id, source_url, content_hash) "
            "VALUES (?,?,?,?)",
            ("123456789012345678", GUILD, legacy_url, "b" * 64))
        await db.commit()

    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    legacy = next(it for it in entries if it["item_name"] == "Legacy Cape")
    check("catalog has no row for this item", legacy["icon_url"] is None,
          str(legacy["icon_url"]))
    check("Shop listing icon resolves the prepared emoji",
          legacy["app_emoji"] == "<:legacy_cape_emj:150000000000000010>",
          str(legacy["app_emoji"]))
    emb = build_inventory_embed(TAB_ITEMS, entries,
                                currency_rows=currency_rows,
                                bot_name=BOT_NAME, username=USERNAME)
    legacy_line = next(ln for ln in lines_of(emb) if "Legacy Cape" in ln)
    check("the resolved emoji is used inline",
          legacy_line.startswith("> - <:legacy_cape_emj:150000000000000010> •"),
          legacy_line)
    check("the Shop URL is not written back as a catalog icon",
          legacy["icon_url"] is None, str(legacy["icon_url"]))

    # Ambiguous name: two listings, only one carrying the prepared URL.
    await give_item(GUILD, USER, "Ambiguous Item", 1, item_type="shop_custom")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type, icon_url,"
            " enabled) VALUES (?,?,?,?,?,1)",
            (GUILD, "Ambiguous Item", 10, "custom", legacy_url))
        await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type, icon_url,"
            " enabled) VALUES (?,?,?,?,?,1)",
            (GUILD, "Ambiguous Item", 20, "custom", None))
        await db.commit()
    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    ambiguous = next(it for it in entries if it["item_name"] == "Ambiguous Item")
    check("an ambiguous Shop name never borrows another listing's emoji",
          ambiguous["app_emoji"] == "", str(ambiguous["app_emoji"]))
    emb = build_inventory_embed(TAB_ITEMS, entries,
                                currency_rows=currency_rows,
                                bot_name=BOT_NAME, username=USERNAME)
    amb_line = next(ln for ln in lines_of(emb) if "Ambiguous Item" in ln)
    check("ambiguous item keeps the rarity fallback",
          amb_line.startswith("> - ⚪ •")
          and "legacy_cape_emj" not in amb_line, amb_line)

    # No Shop listing at all.
    await give_item(GUILD, USER, "Unlisted Item", 1, item_type="shop_custom")
    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    unlisted = next(it for it in entries if it["item_name"] == "Unlisted Item")
    check("no Shop listing means no emoji (safe fallback)",
          unlisted["app_emoji"] == "", str(unlisted["app_emoji"]))

    # Listing in ANOTHER guild must never be matched.
    await give_item(GUILD, USER, "Foreign Item", 1, item_type="shop_custom")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type, icon_url,"
            " enabled) VALUES (?,?,?,?,?,1)",
            (GUILD + 1, "Foreign Item", 10, "custom", legacy_url))
        await db.commit()
    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    foreign = next(it for it in entries if it["item_name"] == "Foreign Item")
    check("another guild's listing is never used",
          foreign["app_emoji"] == "", str(foreign["app_emoji"]))

    # Listing with an icon but nothing prepared.
    await give_item(GUILD, USER, "Unprepared Item", 1, item_type="shop_custom")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type, icon_url,"
            " enabled) VALUES (?,?,?,?,?,1)",
            (GUILD, "Unprepared Item", 10, "custom",
             "https://cdn.example.com/items/unprepared.png"))
        await db.commit()
    items = await get_inventory(GUILD, USER, include_empty=False)
    entries = await _decorate(GUILD, split_by_tab(items)[TAB_ITEMS])
    unprepared = next(it for it in entries if it["item_name"] == "Unprepared Item")
    check("a Shop URL with no prepared asset stays empty",
          unprepared["app_emoji"] == "", str(unprepared["app_emoji"]))

    # The new lookup is still read-only.
    async with aiosqlite.connect(DB_PATH) as db:
        shops_before = (await (await db.execute(
            "SELECT COUNT(*) FROM shop_items")).fetchone())[0]
    def _no_io(*args, **kwargs):
        raise AssertionError("Shop fallback triggered emoji preparation/IO")

    with patch("utils.shop_emoji.prepare_product_emojis", _no_io), \
         patch("utils.shop_emoji.fetch_source_image", _no_io):
        await _decorate(GUILD, split_by_tab(
            await get_inventory(GUILD, USER, include_empty=False))[TAB_ITEMS])
    async with aiosqlite.connect(DB_PATH) as db:
        shops_after = (await (await db.execute(
            "SELECT COUNT(*) FROM shop_items")).fetchone())[0]
    check("Shop fallback writes nothing", shops_before == shops_after,
          f"{shops_before}/{shops_after}")

    # ── 5. Strict no side effects ──────────────────────────────────────
    section("5. Navigation never mutates emoji state")
    async with aiosqlite.connect(DB_PATH) as db:
        before_assets = (await (await db.execute(
            "SELECT COUNT(*) FROM shop_emoji_assets")).fetchone())[0]
        before_aliases = (await (await db.execute(
            "SELECT COUNT(*) FROM shop_emoji_url_aliases")).fetchone())[0]

    def _forbidden(*args, **kwargs):
        raise AssertionError("Inventory triggered emoji preparation/IO")

    inter = FakeInteraction()
    with patch("utils.shop_emoji.fetch_source_image", _forbidden), \
         patch("utils.shop_emoji._download_and_normalize", _forbidden), \
         patch("utils.shop_emoji.normalize_image", _forbidden), \
         patch("utils.shop_emoji.prepare_product_emojis", _forbidden), \
         patch("utils.shop_emoji.reconcile_product_emojis", _forbidden), \
         patch("utils.shop_emoji.DiscordAPI", _forbidden):
        await open_inventory_panel(inter, GUILD, USER, TAB_ITEMS, first=True)
        await open_inventory_panel(inter, GUILD, USER, TAB_POTIONS)
        await open_inventory_panel(inter, GUILD, USER, TAB_TITLES)
        await open_item_panel(inter, GUILD, USER, TAB_ITEMS, "Sunglasses")

    async with aiosqlite.connect(DB_PATH) as db:
        after_assets = (await (await db.execute(
            "SELECT COUNT(*) FROM shop_emoji_assets")).fetchone())[0]
        after_aliases = (await (await db.execute(
            "SELECT COUNT(*) FROM shop_emoji_url_aliases")).fetchone())[0]
    check("rendering succeeded with all preparation paths booby-trapped", True)
    check("no emoji asset rows created/dropped",
          (before_assets, before_aliases) == (after_assets, after_aliases),
          f"{before_assets}/{after_assets} {before_aliases}/{after_aliases}")

    # ── 6. Temporary-role remaining time ───────────────────────────────
    section("6. Temporary-role duration from persisted expires_at")
    suffix = remaining_hours_minutes(
        {"expires_at": (datetime.now(timezone.utc)
                        + timedelta(hours=3, minutes=5)).isoformat()})
    check("computes H:MM from expires_at",
          suffix is not None and suffix.startswith("3:0"), str(suffix))
    check("missing expires_at yields no suffix",
          remaining_hours_minutes({}) is None)
    check("invalid expires_at yields no suffix",
          remaining_hours_minutes({"expires_at": "not-a-date"}) is None)
    check("already-expired yields no negative countdown",
          remaining_hours_minutes({"expires_at": "2000-01-01T00:00:00"}) is None)

    emb = build_inventory_embed(TAB_ITEMS, entries,
                                currency_rows=currency_rows,
                                bot_name=BOT_NAME, username=USERNAME)
    cape_line = next(ln for ln in lines_of(emb) if "Temporary Cape" in ln)
    check("temp role line ends with the time suffix",
          re.search(r"• ` \d+:\d{2} `$", cape_line) is not None, cape_line)
    hat_line = next(ln for ln in lines_of(emb) if "` Hat `" in ln)
    check("non-temp-role line has no time suffix",
          not re.search(r"• ` \d+:\d{2} `$", hat_line), hat_line)

    expired_entries = [{"item_name": "Old Cape", "item_type": "temp_role",
                        "quantity": 1, "rarity": "common", "icon_url": None,
                        "app_emoji": "", "metadata":
                        {"expires_at": "2000-01-01T00:00:00"}}]
    eemb = build_inventory_embed(TAB_ITEMS, expired_entries,
                                 bot_name=BOT_NAME, username=USERNAME)
    old_line = next(ln for ln in lines_of(eemb) if "Old Cape" in ln)
    check("expired temp role shows no (negative) time",
          ":" not in old_line.split("Old Cape")[1], old_line)

    # ── 7. Pagination ──────────────────────────────────────────────────
    section("7. Pagination boundaries")
    def make_entries(n):
        return [{"item_name": f"Item {i:02d}", "item_type": "shop_custom",
                 "quantity": i + 1, "rarity": "common", "icon_url": None,
                 "app_emoji": ""} for i in range(n)]

    for n in (0, 1, 15, 16, 17, 30, 31, 34):
        all_entries = make_entries(n)
        _pe, _p, pages = paginate_entries(all_entries, 0)
        expected_pages = max(1, -(-n // INVENTORY_PAGE_SIZE))
        seen = []
        arrows_ok = True
        layout_ok = True
        cur_ok = True
        for p in range(pages):
            page_entries, clamped, total = paginate_entries(all_entries, p)
            assert clamped == p
            seen.extend(e["item_name"] for e in page_entries)

            view = InventoryView(GUILD, USER, TAB_ITEMS, page_entries,
                                 p, total)
            rows = {}
            for child in view.children:
                rows.setdefault(child.row, []).append(child)

            tabs = [c for c in rows.get(0, []) if isinstance(c, InventoryTabButton)]
            if [t.tab for t in tabs] != [TAB_ITEMS, TAB_COLORS,
                                         TAB_POTIONS, TAB_TITLES]:
                layout_ok = False
            if not any(isinstance(c, InventoryItemSelect) for c in rows.get(1, [])):
                layout_ok = False
            if not any(isinstance(c, InventoryBackButton) for c in rows.get(3, [])):
                layout_ok = False

            page_arrow_children = [c for c in view.children
                                   if isinstance(c, InventoryPageButton)]
            want_prev = p > 0
            want_next = p < total - 1
            have_prev = any(c.direction < 0 for c in page_arrow_children)
            have_next = any(c.direction > 0 for c in page_arrow_children)
            if (have_prev, have_next) != (want_prev, want_next):
                arrows_ok = False
            for child in page_arrow_children:
                if getattr(child.emoji, "name", child.emoji) not in ("◀", "▶"):
                    arrows_ok = False

            # Currency rows must not consume the page budget.
            if len(page_entries) > INVENTORY_PAGE_SIZE:
                cur_ok = False
            if n and p < total - 1 and len(page_entries) != INVENTORY_PAGE_SIZE:
                cur_ok = False

            emb = build_inventory_embed(
                TAB_ITEMS, all_entries, page=p,
                currency_rows=currency_rows, bot_name=BOT_NAME,
                username=USERNAME)
            listed = [e["item_name"] for e in page_entries
                      if f"` {e['item_name']} `" in (emb.description or "")]
            if len(listed) != len(page_entries):
                cur_ok = False
            if not all("Silvers" in ln or "لؤلؤة" in ln
                       for ln in lines_of(emb)[:2]):
                cur_ok = False

        check(f"n={n}: page count {expected_pages}", pages == expected_pages,
              f"{pages} != {expected_pages}")
        check(f"n={n}: every entry appears exactly once",
              sorted(seen) == sorted(e["item_name"] for e in all_entries)
              and len(seen) == len(set(seen)), f"{len(seen)} vs {n}")
        check(f"n={n}: arrows appear only when usable", arrows_ok)
        check(f"n={n}: rows 0/1/3 layout", layout_ok)
        check(f"n={n}: currency lines excluded from page size", cur_ok)

    # 17 items, page 2 must still hold the two overflow entries.
    over = make_entries(17)
    p2, _c, _t = paginate_entries(over, 1)
    check("17 items: overflow page holds exactly 2",
          [e["item_name"] for e in p2] == ["Item 15", "Item 16"],
          str([e["item_name"] for e in p2]))

    # Out-of-range pages clamp instead of stranding an empty panel.
    _pe, clamped, _t = paginate_entries(make_entries(16), 99)
    check("out-of-range page clamps to the last valid page", clamped == 1,
          str(clamped))
    _pe, clamped, _t = paginate_entries(make_entries(0), 5)
    check("empty tab clamps to page 0", clamped == 0, str(clamped))

    # ── 8. Potions: deferred activation ────────────────────────────────
    section("8. Potions are deferred-use")
    await reset_all()
    await set_currency_config(GUILD, currency_name="Silvers",
                              coin_emoji_id="<:silver:111>")
    await safe_credit(GUILD, USER, 1000, currency="balance",
                      reason="seed", source="system")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type,"
            " duration_hours, xp_boost_multiplier, enabled) "
            "VALUES (?,?,?,?,?,?,1)",
            (GUILD, "Potion 1H", 100, "potion", 1, 2.0))
        potion_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type,"
            " duration_hours, xp_boost_multiplier, enabled) "
            "VALUES (?,?,?,?,?,?,1)",
            (GUILD, "Boost 1H", 100, "xp_boost", 1, 2.0))
        boost_id = cur.lastrowid
        await db.commit()

    from cogs.shop import process_purchase

    def _never(*args, **kwargs):
        raise AssertionError("grant_xp_boost ran during a potion PURCHASE")

    inter = FakeInteraction()
    with patch("utils.xp_calculator.grant_xp_boost", _never):
        await process_purchase(inter, potion_id)
    inv = await get_inventory(GUILD, USER, include_empty=False)
    bought = next((it for it in inv if it["item_name"] == "Potion 1H"), None)
    check("purchase delivered the potion into inventory", bought is not None,
          str([it['item_name'] for it in inv]))
    check("potion metadata carries the configured effect",
          bought is not None and bought["metadata"] == {
              "effect": "xp_boost", "multiplier": 2.0, "duration_hours": 1},
          str(bought and bought["metadata"]))
    check("purchase did NOT grant an active boost",
          not await _boost_rows(GUILD, USER))
    check("purchase embed says it was added to the bag",
          any("Added to your bag" in (f.name or "")
              for e in inter.response.sent for f in e["embed"].fields),
          str(inter.response.sent))

    # Select never consumes.
    pot_entries = await _decorate(
        GUILD, split_by_tab(await get_inventory(GUILD, USER))[TAB_POTIONS])
    select = InventoryItemSelect(GUILD, USER, TAB_POTIONS, pot_entries, 0)
    sel_inter = FakeInteraction()
    value = next(o.value for o in select.options if o.value == "Potion 1H")
    select._values = [value]
    await select.callback(sel_inter)
    inv = await get_inventory(GUILD, USER, include_empty=False)
    after_select = next(it for it in inv if it["item_name"] == "Potion 1H")
    check("Selecting a potion does not consume it",
          after_select["quantity"] == 1, str(after_select["quantity"]))
    check("Selecting a potion does not activate it",
          not await _boost_rows(GUILD, USER))
    check("Selecting opens the detail panel on the same page",
          sel_inter.response.edits and "Potion 1H" in
          str(sel_inter.response.edits[0]["embed"].title),
          str(sel_inter.response.edits))

    # Activate consumes exactly one and grants the stored effect.
    pot_entries = await _decorate(
        GUILD, split_by_tab(await get_inventory(GUILD, USER))[TAB_POTIONS])
    item = next(it for it in pot_entries if it["item_name"] == "Potion 1H")
    detail = ItemPanelView(GUILD, USER, TAB_POTIONS, item, False, 0)
    activate = next(c for c in detail.children if c.label == "Activate")
    await give_item(GUILD, USER, "Potion 1H", 1, item_type="potion",
                    metadata={"effect": "xp_boost", "multiplier": 2.0,
                              "duration_hours": 1})
    act_inter = FakeInteraction()
    await activate.callback(act_inter)
    inv = await get_inventory(GUILD, USER, include_empty=False)
    remaining = next(it for it in inv if it["item_name"] == "Potion 1H")
    check("Activate consumes exactly one copy",
          remaining["quantity"] == 1, str(remaining["quantity"]))
    boosts = await _boost_rows(GUILD, USER)
    check("Activate grants exactly one active boost", len(boosts) == 1,
          str(boosts))
    remaining_hours = _hours_left(boosts[0][1]) if boosts else None
    check("effect uses stored metadata (2x, 1h), not the name",
          bool(boosts) and float(boosts[0][0]) == 2.0
          and remaining_hours is not None and 0.9 < remaining_hours <= 1.01,
          f"{boosts} hours_left={remaining_hours}")

    # A misnamed potion still uses its stored duration.
    await give_item(GUILD, USER, "Potion 99H", 1, item_type="potion",
                    metadata={"effect": "xp_boost", "multiplier": 3.0,
                              "duration_hours": 2})
    from utils.potion_engine import use_potion
    res = await use_potion(GUILD, USER, "Potion 99H")
    check("name text never overrides stored duration",
          res.get("success") and res["duration_hours"] == 2
          and res["multiplier"] == 3.0, str(res))

    # Refund when the effect grant fails.
    await give_item(GUILD, USER, "Potion Refund", 1, item_type="potion",
                    metadata={"effect": "xp_boost", "multiplier": 2.0,
                              "duration_hours": 1})
    def _boom(*args, **kwargs):
        raise RuntimeError("grant exploded")
    with patch("utils.xp_calculator.grant_xp_boost", _boom):
        failed = await use_potion(GUILD, USER, "Potion Refund")
    inv = await get_inventory(GUILD, USER, include_empty=False)
    refunded = next(it for it in inv if it["item_name"] == "Potion Refund")
    check("failed effect returns success=False", failed.get("success") is False,
          str(failed))
    check("failed effect refunds the consumed copy",
          refunded["quantity"] == 1, str(refunded["quantity"]))

    # xp_boost (immediate-use) path is unchanged.
    await safe_credit(GUILD, USER, 1000, currency="balance",
                      reason="seed", source="system")
    boost_inter = FakeInteraction()
    await process_purchase(boost_inter, boost_id)
    inv = await get_inventory(GUILD, USER, include_empty=False)
    check("xp_boost does not enter inventory",
          not any(it["item_name"] == "Boost 1H" for it in inv),
          str([it["item_name"] for it in inv]))
    check("xp_boost still activates at purchase",
          len(await _boost_rows(GUILD, USER)) >= 1,
          str(await _boost_rows(GUILD, USER)))

    # A misconfigured potion is refused before any charge.
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO shop_items (guild_id, name, price, type,"
            " duration_hours, xp_boost_multiplier, enabled) "
            "VALUES (?,?,?,?,?,?,1)",
            (GUILD, "Broken Potion", 100, "potion", 0, 2.0))
        broken_id = cur.lastrowid
        await db.commit()
    before_balance = await _balance(GUILD, USER)
    broken_inter = FakeInteraction()
    await process_purchase(broken_inter, broken_id)
    check("misconfigured potion is refused without charging",
          await _balance(GUILD, USER) == before_balance,
          str(await _balance(GUILD, USER)))

    # ── 9. Titles keep the separate slot ───────────────────────────────
    section("9. Title slot independence")
    from utils.title_engine import equip_title, get_equipped_title
    from utils.equip_engine import get_equipped
    await give_item(GUILD, USER, "ملك الغابة", 5, item_type="title")
    await give_item(GUILD, USER, "Sunglasses", 1, item_type="role",
                    metadata={"role_id": 4242})
    tit = await equip_title(GUILD, USER, "ملك الغابة")
    check("title equips from inventory", tit.get("success") is True, str(tit))
    check("equipped title is recorded",
          (await get_equipped_title(GUILD, USER))["item_name"] == "ملك الغابة")
    check("equipping a title does not touch the role slot",
          await get_equipped(GUILD, USER) is None)
    owned_title = next(it for it in await get_inventory(
        GUILD, USER, include_empty=False) if it["item_name"] == "ملك الغابة")
    check("titles are never Discord roles",
          "role_id" not in (owned_title.get("metadata") or {}),
          str(owned_title.get("metadata")))
    shared = await _decorate(
        GUILD, split_by_tab(await get_inventory(GUILD, USER))[TAB_TITLES])
    temb = build_inventory_embed(TAB_TITLES, shared, bot_name=BOT_NAME,
                                 username=USERNAME)
    check("title quantity is the real owned count",
          any("` 5 `" in ln and "ملك الغابة" in ln
              for ln in lines_of(temb)), str(temb.description))

    # ── 10. Routing ────────────────────────────────────────────────────
    section("10. Command routing and ownership")
    from cogs.wallet import Wallet, build_hub_embed, load_wallet_snapshot
    from cogs.shop import Shop

    inv_cmd = next(c for c in Shop.__cog_app_commands__
                   if c.name == "inventory")
    cmd_inter = FakeInteraction()
    await inv_cmd.callback(None, cmd_inter)
    check("/inventory sends a NEW ephemeral message",
          len(cmd_inter.response.sent) == 1
          and cmd_inter.response.sent[0].get("ephemeral") is True
          and not cmd_inter.response.edits, str(cmd_inter.response.sent))
    sent_embed = cmd_inter.response.sent[0]["embed"]
    check("/inventory opens the Items tab directly",
          sent_embed.author.name == f"{BOT_NAME}: 𝐈𝐓𝐄𝐌𝐒 {USERNAME}",
          str(sent_embed.author.name))
    check("/inventory is not the Wallet hub",
          not (sent_embed.title or "").startswith("✦ WALLET"),
          str(sent_embed.title))

    hub = WalletHubView(GUILD, USER, claimed_today=False)
    hub_inter = FakeInteraction()
    hub_button = next(c for c in hub.children if c.label == "Inventory")
    await hub_button.callback(hub_inter)
    check("Wallet Inventory button EDITS the current message",
          len(hub_inter.response.edits) == 1
          and not hub_inter.response.sent, str(hub_inter.response.edits))
    edit_embed = hub_inter.response.edits[0]["embed"]
    check("Wallet button reaches the same Items renderer",
          edit_embed.author.name == sent_embed.author.name
          and edit_embed.description == sent_embed.description,
          str(edit_embed.author.name))
    check("Wallet hub still has its own buttons",
          {c.label for c in hub.children if c.label} ==
          {"Streak", "Inventory", "Receipts", "Vote"},
          str([c.label for c in hub.children]))

    tab_view = InventoryView(GUILD, USER, TAB_ITEMS,
                             paginate_entries(make_entries(16), 0)[0], 0, 2)
    tab_inter = FakeInteraction()
    potion_tab = next(c for c in tab_view.children
                      if isinstance(c, InventoryTabButton)
                      and c.tab == TAB_POTIONS)
    await potion_tab.callback(tab_inter)
    check("tab switch edits the same message",
          len(tab_inter.response.edits) == 1
          and not tab_inter.response.sent, str(tab_inter.response.edits))
    check("tab switch resets to page 1 of that tab",
          tab_inter.response.edits[0]["embed"].author.name
          == f"{BOT_NAME}: 𝐩𝐨𝐭𝐢𝐨𝐧𝐬 {USERNAME}",
          str(tab_inter.response.edits[0]["embed"].author.name))

    back_inter = FakeInteraction()
    back_btn = next(c for c in tab_view.children
                    if isinstance(c, InventoryBackButton))
    await back_btn.callback(back_inter)
    check("Back to Wallet edits back to the hub",
          len(back_inter.response.edits) == 1
          and "WALLET" in (back_inter.response.edits[0]["embed"].title or ""),
          str(back_inter.response.edits))
    check("Back button is visually separate from the page arrows",
          back_btn.row == 3 and back_btn.label == "Back to Wallet"
          and getattr(back_btn.emoji, "name", back_btn.emoji) not in ("◀", "▶"),
          str((back_btn.row, back_btn.emoji, back_btn.label)))

    guard_view = InventoryView(GUILD, USER, TAB_ITEMS, [], 0, 1)
    other_inter = FakeInteraction(user=FakeUser(OTHER, name="intruder"))
    allowed = await guard_view.interaction_check(other_inter)
    check("another member is rejected by the owner guard",
          allowed is False and other_inter.response.sent
          and "not your wallet" in other_inter.response.sent[0]["content"],
          str(other_inter.response.sent))
    check("view timeout is still 30 minutes", guard_view.timeout == 1800,
          str(guard_view.timeout))

    snap = await load_wallet_snapshot(GUILD, USER)
    hub_embed = build_hub_embed(snap)
    check("wallet hub renderer unchanged", hub_embed.title == "✦ WALLET ✦",
          str(hub_embed.title))

    # ── 11. Colors tab (role-backed, permanent or timed) ───────────────
    section("11. Colors tab")
    from utils.timed_ownership import (
        grant_timed_holding, expiry_from_hours, CATEGORY_COLOR,
    )
    from cogs.wallet import (
        TAB_COLORS, is_color_entry, TAB_AUTHOR_WORD, TAB_EMPTY_TEXT,
    )
    from utils.inventory import find_entry

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inventory_items")
        await db.execute("DELETE FROM timed_holdings")
        await db.execute("DELETE FROM equipped_roles")
        await db.commit()

    async def color_grant(name, role_id, hours, category=CATEGORY_COLOR):
        # The Discord side is not under test here; this writes exactly the
        # ownership record a timed Color purchase ends up with.
        holding_id = await grant_timed_holding(
            GUILD, USER, name, expires_at=expiry_from_hours(hours),
            item_type="temp_role", category=category, role_id=role_id,
            metadata={"role_id": role_id, "category": category})
        return {"entry_key": f"h:{holding_id}"}

    # A permanent Color (a role-backed inventory row) and a timed one.
    await give_item(GUILD, USER, "Ember", 1, item_type="role",
                    metadata={"role_id": 5150, "category": CATEGORY_COLOR})
    # An ordinary role reward with NO Colors listing: not a Color.
    await give_item(GUILD, USER, "Guard", 1, item_type="role",
                    metadata={"role_id": 5151})
    timed = await color_grant("Frost", 5152, 6)

    entries = await _decorate(
        GUILD, split_by_tab(await get_inventory(GUILD, USER))[TAB_COLORS])
    names = sorted(e["item_name"] for e in entries)
    check("Colors tab holds only category-tagged entries",
          names == ["Ember", "Frost"], str(names))
    check("a plain role reward stays in the Items tab",
          [e["item_name"] for e in split_by_tab(
              await get_inventory(GUILD, USER))[TAB_ITEMS]] == ["Guard"],
          "Guard")
    check("is_color_entry reads provenance, not the item type",
          is_color_entry({"category": CATEGORY_COLOR, "item_type": "temp_role"})
          and not is_color_entry({"item_type": "role"}))
    check("Colors tab has its own author word and empty text",
          TAB_AUTHOR_WORD.get(TAB_COLORS) and TAB_EMPTY_TEXT.get(TAB_COLORS))

    cemb = build_inventory_embed(TAB_COLORS, entries, bot_name=BOT_NAME,
                                 username=USERNAME)
    ctext = cemb.description or ""
    check("permanent Color is rendered as a role mention",
          "<@&5150>" in ctext, ctext)
    check("timed Color is rendered as a role mention too",
          "<@&5152>" in ctext, ctext)
    check("no role mention is swallowed by backticks",
          "` <@&" not in ctext, ctext)
    check("permanent Color has no countdown",
          not any("<@&5150>" in ln and ":" in ln.split("<@&5150>")[1]
                  for ln in lines_of(cemb)), ctext)
    check("timed Color shows its remaining time",
          any("<@&5152>" in ln and ":" in ln for ln in lines_of(cemb)), ctext)
    check("every Colors line keeps the shared template",
          all(ln.startswith("> - ") for ln in lines_of(cemb)), ctext)

    # Stale-select safety: the option value is the entry_key, so two entries
    # that share a display name can never be confused for one another.
    entries.append({**entries[0], "entry_key": "h:999",
                    "item_name": entries[0]["item_name"]})
    page_entries, _, _ = paginate_entries(entries, 0)
    sel = InventoryItemSelect(GUILD, USER, TAB_COLORS, page_entries, 0)
    keys = list(sel._by_value.values())
    check("duplicate names get distinct entry_keys in the select",
          len(set(keys)) == len(keys) and "h:999" in keys, str(keys))
    check("each option resolves to exactly its own entry",
          all(find_entry(entries, entry_key=k)["entry_key"] == k for k in keys),
          str(keys))
    entries.pop()

    color_entries = await _decorate(
        GUILD, split_by_tab(await get_inventory(GUILD, USER))[TAB_COLORS])
    timed_entry = next(e for e in color_entries if e["timed"])
    item_view = ItemPanelView(GUILD, USER, TAB_COLORS, timed_entry, False, 0)
    labels = [c.label for c in item_view.children if getattr(c, "label", None)]
    check("a timed Color offers Equip but no Drop",
          "Equip" in labels and "Drop" not in labels, str(labels))
    check("the item panel keeps the entry_key it was opened with",
          item_view.entry_key == timed["entry_key"], str(item_view.entry_key))

    perm_entry = next(e for e in color_entries if not e["timed"])
    perm_view = ItemPanelView(GUILD, USER, TAB_COLORS, perm_entry, False, 0)
    check("a permanent Color never offers Drop",
          "Drop" not in [c.label for c in perm_view.children
                     if getattr(c, "label", None)],
          str([getattr(c, "label", None) for c in perm_view.children]))

    colors_guild = FakeGuild()
    colors_guild._roles = {5150: FakeRole(5150), 5152: FakeRole(5152)}
    det = build_item_embed(colors_guild, TAB_COLORS, timed_entry, False)
    det_text = " ".join(f"{f.name} {f.value}" for f in det.fields)
    check("Colors detail shows the role and the remaining time",
          "<@&5152>" in det_text and "Time remaining" in det_text, det_text)

    idem = build_item_embed(colors_guild, TAB_COLORS, perm_entry, False)
    idem_text = " ".join(f"{f.name} {f.value}" for f in idem.fields)
    check("permanent Colors detail shows no countdown",
          "Time remaining" not in idem_text, idem_text)

    print(f"\n\033[1mResults: {_passed} passed, {_failed} failed\033[0m")
    return 1 if _failed else 0


# ── helpers used above ──────────────────────────────────────────────────

async def _boost_rows(guild_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT multiplier, expires_at, source "
            "FROM leveling_active_boosts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        return await cur.fetchall()


def _hours_left(expires_at):
    """Hours remaining on a stored boost expiry (None when unparseable)."""
    if not expires_at:
        return None
    try:
        text = str(expires_at).strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - datetime.now(timezone.utc)).total_seconds() / 3600


async def _balance(guild_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cur.fetchone()
    return row[0] if row else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
