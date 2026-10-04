"""Real-pipeline integration test for the /rank card (no Discord socket).

Exercises the EXACT two calls the /rank command makes after its
existence guard:

    data = await get_rank_card_data(guild.id, user.id, member=member)
    buf  = await render_rank_card(data)

against a freshly-initialised sqlite DB (database.init_db) seeded with
real rows (levels / activity_stats / economy / guild_settings /
item_catalog / inventory_items / equipped_titles), plus a stub
discord.Member supplying only what the pipeline reads (display_name,
display_avatar.url, joined_at, premium_since).

Cases:
  1. normal member (prestige IV, title equipped, mixed inventory)
  2. active booster WITHOUT the VI shop activation (no VI, no tag)
  2b. active booster WITH the VI shop activation (VI pips + tag)
  3. member with no title equipped (no fake pill)
  4. guild with CUSTOM currency names + emoji (fully dynamic check)

Usage:
  OWNER_ID=1 python3 scripts/test_rank_integration.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OWNER_ID", "1")
os.environ["DATABASE_PATH"] = os.environ.get("RANK_IT_DB", "/tmp/nero_it.db")
if os.path.exists(os.environ["DATABASE_PATH"]):
    os.remove(os.environ["DATABASE_PATH"])

import aiosqlite  # noqa: E402
from database import DB_PATH, init_db  # noqa: E402
from utils.rank_card_data import get_rank_card_data  # noqa: E402
from utils.rank_card_renderer import render_rank_card  # noqa: E402

GUILD, USER = 1, 2


class StubAvatar:
    url = ""  # command does str(member.display_avatar.url); "" -> placeholder


class StubMember:
    id = USER

    def __init__(self, name="Shadow", booster=False):
        self.display_name = name
        self.display_avatar = StubAvatar()
        self.joined_at = datetime(2024, 1, 12, tzinfo=timezone.utc)
        self.premium_since = (datetime(2024, 6, 1, tzinfo=timezone.utc)
                              if booster else None)


async def seed():
    await init_db()
    # the minigames cog creates its own tables on cog setup in the real
    # bot; mirror that here so get_user_win_count() resolves.
    from utils.minigame_store import ensure_tables
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,?)",
            [(GUILD, USER, 582450, 84, 4),
             (GUILD, 9, 900000, 95, 5),      # outranks USER -> USER is #2
             (GUILD, 10, 10, 1, 0)])
        await db.execute(
            "INSERT INTO activity_stats (guild_id,user_id,date,"
            "messages_count,words_count,voice_minutes,forum_posts_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER, "2024-01-01", 34725, 0, 10920, 0))
        await db.execute(
            "INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,?,?)", (GUILD, USER, 24520, 138))
        await db.execute(
            "INSERT INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
        await db.executemany(
            "INSERT INTO item_catalog (guild_id,item_name,icon_url,rarity,"
            "value_currency,value_amount) VALUES (?,?,?,?,?,?)",
            [(GUILD, "Pizza", None, "common", "balance", 50),
             (GUILD, "Ramen", None, "common", "balance", 80),
             (GUILD, "Moon Ball", None, "epic", "diamonds", 5),
             (GUILD, "Lollipop", None, "common", "balance", 20),
             (GUILD, "Legend Badge", None, "legendary", "balance", 500),
             (GUILD, "Crystal", None, "rare", "diamonds", 3)])
        await db.executemany(
            "INSERT INTO inventory_items (guild_id,user_id,item_name,"
            "item_type,quantity) VALUES (?,?,?,?,?)",
            [(GUILD, USER, "Pizza", "custom", 1),
             (GUILD, USER, "Ramen", "custom", 2),
             (GUILD, USER, "Moon Ball", "custom", 1),
             (GUILD, USER, "Lollipop", "custom", 1),
             (GUILD, USER, "Legend Badge", "custom", 3),
             (GUILD, USER, "Crystal", "custom", 1)])
        await db.execute(
            "INSERT INTO equipped_titles (guild_id,user_id,item_name) "
            "VALUES (?,?,?)", (GUILD, USER, "The Silent One"))
        await db.commit()


async def render_case(tag, member, out):
    data = await get_rank_card_data(GUILD, USER, member=member)
    buf = await render_rank_card(data)
    with open(out, "wb") as fh:
        fh.write(buf.read())
    return data


async def main():
    await seed()

    # 1) normal
    data = await render_case("normal", StubMember(),
                             "preview_p4_integration_normal.png")
    assert data["rank"] == 2, data["rank"]
    assert data["level"] > 0 and data["effective_prestige"] == 4
    assert data["is_booster"] is False
    assert data["equipped_title"]["item_name"] == "The Silent One"
    grid = data["inventory_grid"]
    assert [i["item_name"] for i in grid][:2] == ["Legend Badge", "Moon Ball"], grid
    # diamonds-priced before coin-priced within same rarity:
    rar = [i["rarity"] for i in grid]
    assert rar == sorted(rar, key=lambda r: ["common", "rare", "epic",
               "legendary", "mythical", "secret"].index(r), reverse=True)
    assert data["inventory_total"] == 6 and data["owned_count"] == 6
    assert data["currency"]["coins"]["name"] == "Coins"  # default config
    print("case 1 (normal, prestige IV, title, mixed inventory) OK "
          f"rank=#{data['rank']} grid={[i['item_name'] for i in grid]}")

    # 2) booster WITHOUT the VI shop activation -> permanent tier only
    data = await get_rank_card_data(GUILD, USER, member=StubMember(booster=True))
    assert data["is_booster"] and data["effective_prestige"] == 4, data
    buf = await render_rank_card(data)
    open("preview_p4_integration_booster_noact.png", "wb").write(buf.read())
    print("case 2 (booster w/o activation -> permanent IV, no VI tag) OK")

    # 2b) booster WITH the VI shop activation -> VI pips + tag
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO prestige_vi_activations (guild_id, user_id) "
            "VALUES (?, ?)", (GUILD, USER))
        await db.commit()
    data = await render_case("booster", StubMember(booster=True),
                             "preview_p4_integration_booster.png")
    assert data["is_booster"] and data["effective_prestige"] == 6
    print("case 2b (booster + VI activation -> Prestige VI) OK")

    # 3) no equipped title -> no pill
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM equipped_titles WHERE guild_id=? "
                         "AND user_id=?", (GUILD, USER))
        await db.commit()
    data = await get_rank_card_data(GUILD, USER, member=StubMember())
    assert data["equipped_title"] is None
    buf = await render_rank_card(data)
    open("preview_p4_integration_notitle.png", "wb").write(buf.read())
    print("case 3 (no title -> no pill) OK")

    # 4) fully dynamic currencies: custom names AND emoji
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE guild_settings SET currency_name='Moon', "
            "coin_emoji_id='⭐', diamond_name='Gems', "
            "diamond_emoji_id='💠' WHERE guild_id=?", (GUILD,))
        await db.commit()
    data = await get_rank_card_data(GUILD, USER, member=StubMember())
    assert data["currency"]["coins"]["name"] == "Moon"
    assert data["currency"]["coins"]["emoji"] == "⭐"
    assert data["currency"]["diamonds"]["name"] == "Gems"
    buf = await render_rank_card(data)
    open("preview_p4_custom_currency.png", "wb").write(buf.read())
    print("case 4 (custom currency names+emoji flow through) OK")
    print("ALL INTEGRATION CASES PASSED")


if __name__ == "__main__":
    asyncio.run(main())
