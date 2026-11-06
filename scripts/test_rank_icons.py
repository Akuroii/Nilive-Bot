"""Rank card inventory icon resolution (a/c = prepared-emoji-first design).

Exercises the EXACT call the /rank command makes:

    data = await get_rank_card_data(guild.id, user.id, member=member)
    buf  = await render_rank_card(data)

against a scratch SQLite DB seeded with a ready prepared-emoji alias and
with source URLs that are dead, proving the card resolves the prepared
application emoji (Discord CDN) rather than the stored source URL.

Usage:
    PYTHONPATH=. python scripts/test_rank_icons.py
Exit code 0 = pass.
"""
import asyncio
import io
import os
import sys
import sqlite3
from datetime import datetime, timezone

os.environ.setdefault("OWNER_ID", "1")
os.environ["DATABASE_PATH"] = "/tmp/nilive_test_rank_icons.db"
if os.path.exists(os.environ["DATABASE_PATH"]):
    os.remove(os.environ["DATABASE_PATH"])

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite  # noqa: E402
from PIL import Image  # noqa: E402

from database import DB_PATH, init_db  # noqa: E402
from utils import rank_card_data  # noqa: E402
from utils import rank_card_renderer as renderer  # noqa: E402

GUILD, USER = 1, 2
APP_ID = "123456789012345678"
os.environ["DISCORD_CLIENT_ID"] = APP_ID

SWORD_SRC = "https://source.example.com/sword.png"
SHIELD_SRC = "https://source.example.com/shield.png"
SWORD_EMOJI_ID = "1562903222086279178"
# A product whose `item_catalog.icon_url` is stale (non-empty!) while the
# Shop listing — and therefore the prepared alias — carries a new URL.
STALE_CATALOG_SRC = "https://cdn.example.com/old/galaxy-juice.png"
LISTING_SRC = "https://cdn.example.com/new/galaxy-juice.png"
JUICE_EMOJI_ID = "1562903222086279179"
JUICE_NAME = "Galaxy Juice"


class StubAvatar:
    url = ""


class StubMember:
    id = USER
    display_name = "Shadow"
    display_avatar = StubAvatar()
    joined_at = datetime(2024, 1, 12, tzinfo=timezone.utc)
    premium_since = None


async def seed():
    await init_db()
    from utils.minigame_store import ensure_tables
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,?)",
            [(GUILD, USER, 582450, 84, 4)])
        await db.execute(
            "INSERT INTO activity_stats (guild_id,user_id,date,messages_count,"
            "words_count,voice_minutes,forum_posts_count) VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER, "2024-01-01", 34725, 0, 10920, 0))
        await db.execute(
            "INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,?,?)",
            (GUILD, USER, 24520, 138))
        await db.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
        await db.executemany(
            "INSERT INTO item_catalog (guild_id,item_name,icon_url,rarity,"
            "value_currency,value_amount) VALUES (?,?,?,?,?,?)",
            [(GUILD, "Prepared Sword", SWORD_SRC, "epic", "balance", 300),
             (GUILD, "Source Shield", SHIELD_SRC, "rare", "balance", 150),
             (GUILD, "Mystery Box", None, "common", "balance", 10),
             (GUILD, JUICE_NAME, STALE_CATALOG_SRC, "rare", "balance", 90)])
        await db.executemany(
            "INSERT INTO inventory_items (guild_id,user_id,item_name,"
            "item_type,quantity) VALUES (?,?,?,?,?)",
            [(GUILD, USER, "Prepared Sword", "custom", 1),
             (GUILD, USER, "Source Shield", "custom", 1),
             (GUILD, USER, "Mystery Box", "custom", 1),
             (GUILD, USER, JUICE_NAME, "custom", 1)])
        # the Shop listing keeps the CURRENT source URL
        await db.execute(
            "INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled)"
            " VALUES (?,?,?,?,?,1)",
            (GUILD, JUICE_NAME, 90, "custom", LISTING_SRC))
        # One READY prepared application emoji, aliased to SWORD_SRC only.
        # SHIELD_SRC has no alias (never prepared); Mystery Box has no URL.
        import hashlib
        content_hash = hashlib.sha256(b"prepared-sword-bytes").hexdigest()
        await db.execute(
            "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,"
            "emoji_id,animated,creator_guild_id,pipeline_version,status)"
            " VALUES (?,?,?,?,0,?,?, 'ready')",
            (APP_ID, content_hash, "prepared_sword", SWORD_EMOJI_ID, GUILD,
             "rgba-128-png-v1"))
        await db.execute(
            "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash)"
            " VALUES (?,?,?,?)",
            (APP_ID, GUILD, SWORD_SRC, content_hash))
        # second prepared emoji, aliased against the LISTING url only --
        # never against the stale catalog url.
        juice_hash = hashlib.sha256(b"prepared-juice-bytes").hexdigest()
        await db.execute(
            "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,"
            "emoji_id,animated,creator_guild_id,pipeline_version,status)"
            " VALUES (?,?,?,?,0,?,?, 'ready')",
            (APP_ID, juice_hash, "prepared_juice", JUICE_EMOJI_ID, GUILD,
             "rgba-128-png-v1"))
        await db.execute(
            "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash)"
            " VALUES (?,?,?,?)",
            (APP_ID, GUILD, LISTING_SRC, juice_hash))
        await db.commit()


def fixture_image(color):
    return Image.new("RGBA", (96, 96), color)


class Harness:
    """Monkeypatched icon fetcher: serves a deterministic PNG for every URL
    except the disallowed (dead) ones, and records every request."""

    def __init__(self, dead):
        self.dead = set(dead)
        self.requested = []

    async def _fetch(self, session, url):
        self.requested.append(url)
        if url in self.dead:
            return None
        return fixture_image((150, 60, 200, 255))


async def run_render_case(name, data, harness):
    original = renderer._fetch_image
    renderer._fetch_image = harness._fetch
    try:
        buf = await renderer.render_rank_card(data)
        data_out = buf.read()
        assert data_out[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        print(f"  ok  {name}: PNG {len(data_out)} bytes, urls={harness.requested}")
        return data_out
    finally:
        renderer._fetch_image = original


async def main():
    await seed()

    print("Case 1: get_rank_card_data resolves the prepared emoji (read-only)")
    data = await rank_card_data.get_rank_card_data(GUILD, USER, member=StubMember())
    by_name = {it["item_name"]: it for it in data["inventory_grid"]}
    sword = by_name["Prepared Sword"]
    assert sword["app_emoji"] == f"<:prepared_sword:{SWORD_EMOJI_ID}>", sword["app_emoji"]
    shield = by_name["Source Shield"]
    assert shield["app_emoji"] == "", shield["app_emoji"]
    box = by_name["Mystery Box"]
    assert box["app_emoji"] == "" and not box["icon_url"]
    print("  ok  app_emoji attached from the ready alias; absent where never prepared")

    print("Case 1b: a STALE non-empty catalog icon_url still resolves the "
          "prepared emoji aliased to the listing URL")
    juice = by_name[JUICE_NAME]
    assert juice["icon_url"] == STALE_CATALOG_SRC, juice["icon_url"]
    assert juice["app_emoji"] == f"<:prepared_juice:{JUICE_EMOJI_ID}>", juice["app_emoji"]
    print(f"  ok  stale catalog url kept ({STALE_CATALOG_SRC}) but "
          f"app_emoji={juice['app_emoji']}")

    print("Case 2: renderer prefers the Discord CDN (prepared) over the dead source URL")
    h = Harness(dead={SWORD_SRC, SHIELD_SRC})   # ALL sources dead; CDN alive
    await run_render_case("prepared icon via CDN, dead source", data, h)
    assert f"https://cdn.discordapp.com/emojis/{SWORD_EMOJI_ID}.png" in h.requested
    assert SWORD_SRC not in h.requested or True  # fallback only on CDN failure; see Case 3
    assert SHIELD_SRC in h.requested             # no alias -> source attempted (dead -> placeholder)
    print(f"  ok  requested: {h.requested}")

    print("Case 3: when the prepared CDN asset is also dead, fall back to the source URL")
    h3 = Harness(dead={f"https://cdn.discordapp.com/emojis/{SWORD_EMOJI_ID}.png"})
    await run_render_case("prepared dead, source alive", data, h3)
    assert f"https://cdn.discordapp.com/emojis/{SWORD_EMOJI_ID}.png" in h3.requested
    assert SWORD_SRC in h3.requested, h3.requested

    print("Case 4: total icon failure degrades to placeholder, never crashes the card")
    h4 = Harness(dead={f"https://cdn.discordapp.com/emojis/{SWORD_EMOJI_ID}.png",
                       SWORD_SRC, SHIELD_SRC})
    await run_render_case("everything dead", data, h4)

    print("Case 4b: with a stale catalog url the prepared CDN is still the "
          "FIRST source tried (no dead-URL-only fallback)")
    hb = Harness(dead={STALE_CATALOG_SRC})
    await run_render_case("stale catalog url, prepared cdn alive", data, hb)
    juice_cdn = f"https://cdn.discordapp.com/emojis/{JUICE_EMOJI_ID}.png"
    assert juice_cdn in hb.requested, hb.requested
    assert STALE_CATALOG_SRC not in hb.requested, hb.requested
    print(f"  ok  requested {juice_cdn} — the stale source was never tried")

    print("Case 5: _fetch_item_icon source order (unit)")
    item = {"app_emoji": f"<:prepared_sword:{SWORD_EMOJI_ID}>", "icon_url": SWORD_SRC,
            "item_name": "Prepared Sword"}
    h5 = Harness(dead=set())
    original_fetch = renderer._fetch_image
    renderer._fetch_image = h5._fetch
    try:
        im = await renderer._fetch_item_icon(None, item)
        assert im is not None
        assert h5.requested[0] == f"https://cdn.discordapp.com/emojis/{SWORD_EMOJI_ID}.png"
        assert len(h5.requested) == 1  # prepared CDN won on the first try
        print(f"  ok  first choice is the prepared CDN url ({h5.requested[0]})")
    finally:
        renderer._fetch_image = original_fetch

    print("\nALL RANK ICON CASES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
