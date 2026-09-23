"""One-off verification render for the Amira Typo currency-label font and
Pearl/Shell slot colors. Reuses scripts/test_rank_integration.py's seed()
and stub member, then overrides guild_settings with one Arabic currency
name (coins slot) and one English currency name (diamonds slot) to check
both scripts render through the real pipeline. Not part of the regular
test suite -- ad hoc verification only.

Usage:
  OWNER_ID=1 python3 scripts/verify_currency_font_color.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OWNER_ID", "1")
os.environ["DATABASE_PATH"] = os.environ.get("RANK_IT_DB", "/tmp/nero_currency_verify.db")
if os.path.exists(os.environ["DATABASE_PATH"]):
    os.remove(os.environ["DATABASE_PATH"])

import aiosqlite  # noqa: E402
from database import DB_PATH  # noqa: E402
from utils.rank_card_data import get_rank_card_data  # noqa: E402
from utils.rank_card_renderer import render_rank_card  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_rank_integration import seed, StubMember, GUILD, USER  # noqa: E402


async def main():
    await seed()

    # coins slot (primary, drawn first) -> Arabic name, should get Amira
    # Typo + the icy pearl color regardless of what the name says.
    # diamonds slot (secondary) -> English name, should get Zilla Slab
    # Bold + the coral shell color.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE guild_settings SET currency_name='لؤلؤة', "
            "coin_emoji_id='🪙', diamond_name='Shellcoin Prestige', "
            "diamond_emoji_id='💠' WHERE guild_id=?", (GUILD,))
        await db.commit()

    data = await get_rank_card_data(GUILD, USER, member=StubMember())
    assert data["currency"]["coins"]["name"] == "لؤلؤة"
    assert data["currency"]["diamonds"]["name"] == "Shellcoin Prestige"
    buf = await render_rank_card(data)
    png_bytes = buf.read()
    out_path = "verify_currency_font_color_a.png"
    with open(out_path, "wb") as fh:
        fh.write(png_bytes)

    from PIL import Image
    im = Image.open(out_path)
    print(f"case A (coins=Arabic 'لؤلؤة', diamonds=English long name) -> "
          f"{out_path} size={im.size}")
    assert im.size == (1280, 853), im.size

    # Swap which slot is Arabic vs English, to confirm colors stay
    # SLOT-based (coins=pearl, diamonds=shell) and don't follow the name.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE guild_settings SET currency_name='Pearl Tokens', "
            "coin_emoji_id='🪙', diamond_name='صدفة', "
            "diamond_emoji_id='💠' WHERE guild_id=?", (GUILD,))
        await db.commit()

    data = await get_rank_card_data(GUILD, USER, member=StubMember())
    assert data["currency"]["coins"]["name"] == "Pearl Tokens"
    assert data["currency"]["diamonds"]["name"] == "صدفة"
    buf = await render_rank_card(data)
    png_bytes = buf.read()
    out_path2 = "verify_currency_font_color_b.png"
    with open(out_path2, "wb") as fh:
        fh.write(png_bytes)
    im2 = Image.open(out_path2)
    print(f"case B (coins=English 'Pearl Tokens', diamonds=Arabic 'صدفة') -> "
          f"{out_path2} size={im2.size}")
    assert im2.size == (1280, 853), im2.size

    print("VERIFICATION RENDER PASSED")


if __name__ == "__main__":
    asyncio.run(main())
