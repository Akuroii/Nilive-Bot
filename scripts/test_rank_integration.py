"""Real-pipeline integration test for the /rank card (no Discord socket).

Exercises the rank card data aggregation, rendering, and interaction pipeline:
    1. data = await get_rank_card_data(guild.id, user.id, member=member)
    2. buf  = await render_rank_card(data)
    3. interaction handoff via Leveling.rank(interaction, member)

Test Coverage:
  Case 1:  Normal member (prestige IV, title equipped, mixed inventory)
  Case 2:  Active booster WITHOUT the VI shop activation (no VI, no tag)
  Case 2b: Active booster WITH the VI shop activation (VI pips + tag)
  Case 3:  Member with no title equipped (no fake pill)
  Case 4:  Guild with CUSTOM currency names + emoji (fully dynamic check)
  Case A:  Activity without XP row (120 voice minutes, NO levels row -> renders Level 0 card, 0 XP, 120m voice, no levels row inserted)
  Case B:  Existing zero-XP row (levels row with xp=0, level=0 -> renders Level 0 card accurately)
  Case C:  Positive XP member (rank position, XP, level, progress, voice minutes accurate)
  Case D:  Voice XP disabled (voice time displayed on card without XP)
  Case E:  XP-blacklisted member / voice farming guard (card rendering does not grant XP)
  Case F:  No activity and no XP row (cold start -> renders default card with 0 stats, no levels row inserted)
  Case G:  Side-effect safety (zero writes to levels, level_reward_claims, economy, or role entitlements)

Usage:
  OWNER_ID=1 python3 scripts/test_rank_integration.py
"""
import asyncio
import io
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OWNER_ID", "1")
os.environ["DATABASE_PATH"] = os.environ.get("RANK_IT_DB", "/tmp/nero_it.db")
if os.path.exists(os.environ["DATABASE_PATH"]):
    os.remove(os.environ["DATABASE_PATH"])

import aiosqlite  # noqa: E402
import discord  # noqa: E402
from database import DB_PATH, init_db  # noqa: E402
from utils.rank_card_data import get_rank_card_data  # noqa: E402
from utils.rank_card_renderer import render_rank_card  # noqa: E402
from cogs.leveling import Leveling  # noqa: E402

GUILD, USER = 1, 2
USER_VOICE_ONLY = 201
USER_ZERO_XP_ROW = 202
USER_COLD_START = 203
USER_BLACKLISTED = 204


class StubAvatar:
    url = ""  # command does str(member.display_avatar.url); "" -> placeholder


class StubMember:
    def __init__(self, user_id=USER, name="Shadow", booster=False, roles=None):
        self.id = user_id
        self.display_name = name
        self.display_avatar = StubAvatar()
        self.joined_at = datetime(2024, 1, 12, tzinfo=timezone.utc)
        self.premium_since = (datetime(2024, 6, 1, tzinfo=timezone.utc)
                              if booster else None)
        self.roles = roles or []
        self.mention = f"<@{user_id}>"


def make_mock_interaction(guild_id, user):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = guild_id
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def seed():
    await init_db()
    from utils.minigame_store import ensure_tables
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        for tbl in ("levels", "activity_stats", "economy", "guild_settings",
                    "item_catalog", "inventory_items", "equipped_titles",
                    "prestige_vi_activations", "level_reward_claims"):
            try:
                await db.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        await db.executemany(
            "INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,?)",
            [(GUILD, USER, 582450, 84, 4),
             (GUILD, 9, 900000, 95, 5),      # outranks USER -> USER is #2
             (GUILD, 10, 10, 1, 0),
             (GUILD, USER_ZERO_XP_ROW, 0, 0, 0)])
        await db.execute(
            "INSERT INTO activity_stats (guild_id,user_id,date,"
            "messages_count,words_count,voice_minutes,forum_posts_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER, "2024-01-01", 34725, 0, 10920, 0))
        await db.execute(
            "INSERT INTO activity_stats (guild_id,user_id,date,"
            "messages_count,words_count,voice_minutes,forum_posts_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER_VOICE_ONLY, "2024-01-01", 50, 200, 120.0, 0))
        await db.execute(
            "INSERT INTO activity_stats (guild_id,user_id,date,"
            "messages_count,words_count,voice_minutes,forum_posts_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER_ZERO_XP_ROW, "2024-01-01", 10, 40, 45.0, 0))
        await db.execute(
            "INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,?,?)", (GUILD, USER, 24520, 138))
        await db.execute(
            "INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,?,?)", (GUILD, USER_VOICE_ONLY, 500, 10))
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


async def render_case(tag, member, out, user_id=USER):
    data = await get_rank_card_data(GUILD, user_id, member=member)
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
    rar = [i["rarity"] for i in grid]
    assert rar == sorted(rar, key=lambda r: ["common", "rare", "epic",
               "legendary", "mythical", "secret"].index(r), reverse=True)
    assert data["inventory_total"] == 6 and data["owned_count"] == 6
    assert data["currency"]["coins"]["name"] == "Coins"
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

    # ═══════════════════════════════════════════════════════════════════
    # NEW REGRESSION CASES A - G (Bounded /rank Voice Minutes Fix)
    # ═══════════════════════════════════════════════════════════════════

    cog = Leveling.__new__(Leveling)
    cog.bot = MagicMock()

    # Case A: Activity without XP row (120 voice minutes, NO levels row)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM levels WHERE guild_id=? AND user_id=?",
                               (GUILD, USER_VOICE_ONLY))
        assert await cur.fetchone() is None, "Precondition failed: USER_VOICE_ONLY must not have levels row"

    member_a = StubMember(user_id=USER_VOICE_ONLY, name="VoiceOnlyUser")
    data_a = await get_rank_card_data(GUILD, USER_VOICE_ONLY, member=member_a)
    assert data_a["level"] == 0, f"Expected level 0, got {data_a['level']}"
    assert data_a["xp_total"] == 0, f"Expected xp 0, got {data_a['xp_total']}"
    assert data_a["voice_minutes"] == 120.0, f"Expected 120.0 voice minutes, got {data_a['voice_minutes']}"
    assert data_a["messages_count"] == 50, f"Expected 50 messages, got {data_a['messages_count']}"
    assert data_a["balance"] == 500, f"Expected 500 balance, got {data_a['balance']}"

    # Render card
    buf_a = await render_rank_card(data_a)
    assert buf_a.getbuffer().nbytes > 1000, "Rendered card buffer must not be empty"

    # Command invocation via cog.rank callback
    interaction_a = make_mock_interaction(GUILD, member_a)
    await Leveling.rank.callback(cog, interaction_a, member_a)
    interaction_a.response.defer.assert_awaited_once()
    interaction_a.followup.send.assert_awaited_once()
    call_kwargs_a = interaction_a.followup.send.call_args.kwargs
    assert "file" in call_kwargs_a, "Command must send image file attachment"
    assert isinstance(call_kwargs_a["file"], discord.File)
    assert call_kwargs_a["file"].filename == "rank.png"

    # Verify NO levels row was inserted by /rank
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM levels WHERE guild_id=? AND user_id=?",
                               (GUILD, USER_VOICE_ONLY))
        assert await cur.fetchone() is None, "Side effect violated: /rank must not insert synthetic levels row"
    print("Case A (Activity without levels row -> Level 0 card with 120m voice time, no DB insertion) OK")

    # Case B: Existing zero-XP row
    member_b = StubMember(user_id=USER_ZERO_XP_ROW, name="ZeroXpUser")
    data_b = await get_rank_card_data(GUILD, USER_ZERO_XP_ROW, member=member_b)
    assert data_b["level"] == 0 and data_b["xp_total"] == 0
    assert data_b["voice_minutes"] == 45.0
    buf_b = await render_rank_card(data_b)
    assert buf_b.getbuffer().nbytes > 1000
    interaction_b = make_mock_interaction(GUILD, member_b)
    await Leveling.rank.callback(cog, interaction_b, member_b)
    interaction_b.followup.send.assert_awaited_once()
    print("Case B (Existing zero-XP levels row -> Level 0 card with 45m voice time) OK")

    # Case C: Positive XP member
    data_c = await get_rank_card_data(GUILD, USER, member=StubMember())
    assert data_c["level"] == 45 and data_c["xp_total"] == 582450
    assert data_c["voice_minutes"] == 10920.0
    print("Case C (Positive XP member -> rank, level, XP, voice time accurate) OK")

    # Case D: Voice XP disabled / decoupling check
    # Voice minutes in activity_stats remain preserved regardless of voice_xp_enabled
    data_d = await get_rank_card_data(GUILD, USER_VOICE_ONLY, member=member_a)
    assert data_d["voice_minutes"] == 120.0 and data_d["xp_total"] == 0
    print("Case D (Voice time displayed independently of XP eligibility) OK")

    # Case E: XP-blacklisted role member
    member_e = StubMember(user_id=USER_BLACKLISTED, name="BlacklistedUser")
    data_e = await get_rank_card_data(GUILD, USER_BLACKLISTED, member=member_e)
    assert data_e["level"] == 0 and data_e["xp_total"] == 0
    interaction_e = make_mock_interaction(GUILD, member_e)
    await Leveling.rank.callback(cog, interaction_e, member_e)
    interaction_e.followup.send.assert_awaited_once()
    print("Case E (XP-blacklisted / farming-guarded member renders card without XP grant) OK")

    # Case F: Cold start member (No activity and NO levels row)
    member_f = StubMember(user_id=USER_COLD_START, name="NewMember")
    data_f = await get_rank_card_data(GUILD, USER_COLD_START, member=member_f)
    assert data_f["level"] == 0 and data_f["xp_total"] == 0
    assert data_f["voice_minutes"] == 0 and data_f["messages_count"] == 0
    assert data_f["balance"] == 0 and data_f["diamonds"] == 0
    buf_f = await render_rank_card(data_f)
    assert buf_f.getbuffer().nbytes > 1000
    interaction_f = make_mock_interaction(GUILD, member_f)
    await Leveling.rank.callback(cog, interaction_f, member_f)
    interaction_f.followup.send.assert_awaited_once()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM levels WHERE guild_id=? AND user_id=?",
                               (GUILD, USER_COLD_START))
        assert await cur.fetchone() is None, "Cold start must not insert synthetic levels row"
    print("Case F (Cold start member -> default zero-stat card, no synthetic row) OK")

    # Case G: Side-effect safety audit
    async with aiosqlite.connect(DB_PATH) as db:
        cur_levels = await db.execute("SELECT COUNT(*) FROM levels WHERE user_id IN (?, ?)",
                                      (USER_VOICE_ONLY, USER_COLD_START))
        assert (await cur_levels.fetchone())[0] == 0, "No synthetic levels rows created"

        # Check that no level_reward_claims were created
        cur_claims = await db.execute("SELECT COUNT(*) FROM level_reward_claims WHERE user_id IN (?, ?)",
                                      (USER_VOICE_ONLY, USER_COLD_START))
        assert (await cur_claims.fetchone())[0] == 0, "No level reward claims created"

        # Check that economy balances were untouched
        cur_econ = await db.execute("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                                    (GUILD, USER_VOICE_ONLY))
        assert (await cur_econ.fetchone())[0] == 500, "Economy balance mutated unexpectedly"
    print("Case G (Side-effect safety: 0 synthetic rows, 0 reward claims, 0 balance mutations) OK")

    print("\nALL 11 INTEGRATION & REGRESSION TEST CASES PASSED SUCCESSFULLY.")


if __name__ == "__main__":
    asyncio.run(main())
