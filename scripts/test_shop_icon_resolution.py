"""Shop icon URL -> explicit preparation -> Inventory/Rank regression.

Run exactly:
    PYTHONPATH=. python scripts/test_shop_icon_resolution.py

Uses a fresh scratch SQLite DB, the real Flask save/prepare endpoints, the
real Inventory entry point and the real Rank aggregation/renderer. Discord
and all source-image network I/O are blocked; preparation runs against an
in-memory fake application only. No production data or credentials are used.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_CACHE = ROOT / ".cache"
_CACHE.mkdir(exist_ok=True)
_SCRATCH = tempfile.TemporaryDirectory(prefix="shop-icon-resolution-", dir=_CACHE)
DB_FILE = Path(_SCRATCH.name) / "scratch.db"
os.environ.update(
    DATABASE_PATH=str(DB_FILE), NERO_ENVIRONMENT="test", OWNER_ID="999999999",
    SECRET_KEY="isolated-icon-test-secret-key-0123456789abcdef",
    DISCORD_CLIENT_ID="123456789012345678", DASHBOARD_DEMO_LOGIN="0",
    # No startup reconciliation task: this suite only exercises explicit
    # preparation through the fake application below.
    DISCORD_TOKEN="",
)

import aiohttp  # noqa: E402
import aiosqlite  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402

from database import DB_PATH, init_db  # noqa: E402
assert Path(DB_PATH) == DB_FILE, "Refusing to run outside the scratch DB"
import dashboard.app as dashboard  # noqa: E402
from cogs import wallet  # noqa: E402
from utils import rank_card_data, rank_card_renderer as renderer  # noqa: E402
from utils import shop_emoji as SE  # noqa: E402

APP = "123456789012345678"
OTHER_APP = "123456789012345999"
GUILD, OTHER_GUILD, USER = 8101, 8102, 8201
CSRF = "icon-test-csrf"
NAMES = ("عصير المجرات", "سهران السيرفر", "شوكولاته الجلاكسي", "عنتيل السيرفر")
URLS = tuple(f"https://assets.example.com/item-{i}.png?v=1" for i in range(4))
COLORS = ((220, 40, 40, 255), (40, 220, 40, 255), (40, 40, 220, 255), (220, 220, 40, 255))


def png_bytes(color):
    buf = io.BytesIO()
    Image.new("RGBA", (128, 128), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeApplication:
    """Only the explicit preparation test action may use this fake."""
    def __init__(self):
        self.application_id = APP
        self.remote = []
        self.next_id = 1562903222086279178

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def verify_application(self):
        return None

    async def bot_user_id(self):
        return "999999999999999999"

    async def list_emojis(self):
        return [dict(e) for e in self.remote]

    async def create_emoji(self, name, final_png, bot_user_id):
        self.next_id += 1
        emoji = {"id": str(self.next_id), "name": name, "animated": False,
                 "application_id": APP, "user": {"id": bot_user_id}}
        self.remote.append(dict(emoji))
        return emoji


class ShopIconResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_FILE) + suffix).unlink(missing_ok=True)
        await init_db()
        from utils.minigame_store import ensure_tables
        await ensure_tables()
        SE._preparation_locks.clear()
        os.environ["DISCORD_CLIENT_ID"] = APP

        # Hard network boundary: a regression may never fetch or upload.
        for target, kwargs in (
            ("aiohttp.ClientSession._request", {"side_effect": AssertionError("network blocked")}),
            ("requests.sessions.Session.request", {"side_effect": AssertionError("network blocked")}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(SE, "DiscordAPI", side_effect=AssertionError("implicit preparation blocked"))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(SE, "_download_and_normalize", AsyncMock(side_effect=AssertionError("source fetch blocked")))
        p.start()
        self.addCleanup(p.stop)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
            await db.execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,0)",
                             (GUILD, USER, 1000, 5))
            await db.execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,1000,0)",
                             (GUILD, USER))
            await db.execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                             "VALUES (?,?,'owner',1)", (GUILD, USER))
            await db.commit()

        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Icon Test", "avatar": None},
                           guild_id=GUILD, expires_at=4102444800, csrf_token=CSRF)
        self.product_ids = []
        for name in NAMES:
            r = self.client.post("/api/shop/item", json={"name": name, "type": "custom", "price": 100},
                                 headers={"X-CSRF-Token": CSRF})
            self.assertEqual(r.status_code, 200, r.get_json())
            row = await self._one("SELECT id FROM shop_items WHERE guild_id=? AND name=?", (GUILD, name))
            self.product_ids.append(row[0])
        # The user's action: add the image URL through the existing edit form.
        for pid, url in zip(self.product_ids, URLS):
            r = self.client.put(f"/api/shop/item/{pid}", json={"icon_url": url},
                                headers={"X-CSRF-Token": CSRF})
            self.assertEqual(r.status_code, 200, r.get_json())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,quantity) "
                                 "VALUES (?,?,?,'custom',1)", [(GUILD, USER, n) for n in NAMES])
            await db.commit()
        await self._explicit_prepare()

    async def _one(self, sql, args=()):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(sql, args)
            return await cur.fetchone()

    async def _exec(self, sql, args=()):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(sql, args)
            await db.commit()

    async def _explicit_prepare(self):
        fake = FakeApplication()
        images = {url: png_bytes(color) for url, color in zip(URLS, COLORS)}
        with patch.object(SE, "DiscordAPI", lambda: fake), \
             patch.object(SE, "_download_and_normalize", AsyncMock(side_effect=lambda url: images[url])):
            r = self.client.post("/api/shop-publisher/emojis/prepare",
                                 json={"product_ids": self.product_ids},
                                 headers={"X-CSRF-Token": CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(all(x["status"] == "ready" for x in r.get_json()["results"]), r.get_json())
        self.assertEqual(len(fake.remote), 4)

    async def _token(self, index):
        row = await self._one(
            "SELECT e.emoji_name,e.emoji_id,e.animated FROM shop_emoji_url_aliases a "
            "JOIN shop_emoji_assets e ON e.application_id=a.application_id AND e.content_hash=a.content_hash "
            "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=?", (APP, GUILD, URLS[index]))
        self.assertIsNotNone(row)
        return SE.build_custom_emoji(row[1], row[0], bool(row[2]))

    async def _snapshot(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
            tables = [r[0] for r in await cur.fetchall()]
            out = {}
            for table in tables:
                cur = await db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
                out[table] = await cur.fetchall()
            return out

    def _interaction(self):
        send = AsyncMock()
        return SimpleNamespace(
            # Model both discord.py 2.7.1 identity surfaces: the interaction
            # payload ID and the logged-in client ID. Production prefers the
            # former and falls back to the latter.
            application_id=int(APP),
            client=SimpleNamespace(application_id=int(APP), user=SimpleNamespace(name="Nilive")),
            user=SimpleNamespace(id=USER, name="tester", display_name="Tester"),
            guild=SimpleNamespace(id=GUILD),
            response=SimpleNamespace(send_message=send, edit_message=AsyncMock()),
            original_response=AsyncMock(return_value=object()), message=object()), send

    async def _inventory(self):
        interaction, send = self._interaction()
        await wallet.open_inventory_panel(interaction, GUILD, USER, wallet.TAB_ITEMS, first=True)
        kwargs = send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        kwargs["view"].stop()
        return kwargs["embed"]

    async def _rank(self):
        member = SimpleNamespace(id=USER, display_name="Tester", display_avatar=SimpleNamespace(url=""),
                                 joined_at=datetime(2024, 1, 1, tzinfo=timezone.utc), premium_since=None)
        return await rank_card_data.get_rank_card_data(GUILD, USER, member=member, application_id=APP)

    async def test_url_save_explicit_prepare_and_both_renderers(self):
        before = await self._snapshot()
        embed = await self._inventory()
        for i, name in enumerate(NAMES):
            token = await self._token(i)
            self.assertIn(f"{token} • ` 1 ` • ` {name} `", embed.description)
            self.assertNotIn(URLS[i], embed.description)
        data = await self._rank()
        grid = {it["item_name"]: it for it in data["inventory_grid"]}
        for i, name in enumerate(NAMES):
            self.assertEqual(grid[name]["app_emoji"], await self._token(i))
        requested = []
        async def fake_fetch(session, url):
            requested.append(url)
            return Image.new("RGBA", (96, 96), COLORS[URLS.index(url) % 4] if url in URLS else (1, 2, 3, 255))
        with patch.object(renderer, "_fetch_image", fake_fetch):
            buf = await renderer.render_rank_card(data)
            self.assertEqual(buf.read()[:8], b"\x89PNG\r\n\x1a\n")
        for i in range(4):
            token = await self._token(i)
            emoji_id = token.rsplit(":", 1)[1][:-1]
            self.assertIn(f"https://cdn.discordapp.com/emojis/{emoji_id}.png", requested)
        self.assertEqual(await self._snapshot(), before, "navigation/render changed stored state")

    async def test_inventory_uses_authenticated_bot_application_without_env(self):
        os.environ.pop("DISCORD_CLIENT_ID", None)
        before = await self._snapshot()
        embed = await self._inventory()
        for i, name in enumerate(NAMES):
            self.assertIn(f"{await self._token(i)} • ` 1 ` • ` {name} `", embed.description)
        self.assertEqual(await self._snapshot(), before)

    async def test_rank_uses_authenticated_bot_application_without_env(self):
        os.environ.pop("DISCORD_CLIENT_ID", None)
        before = await self._snapshot()
        data = await self._rank()
        grid = {it["item_name"]: it for it in data["inventory_grid"]}
        for i, name in enumerate(NAMES):
            self.assertEqual(grid[name]["app_emoji"], await self._token(i))
        self.assertEqual(await self._snapshot(), before)

    async def test_runtime_identity_wins_over_stale_dashboard_env_for_reads(self):
        os.environ["DISCORD_CLIENT_ID"] = OTHER_APP
        interaction, send = self._interaction()
        interaction.client.application_id = int(OTHER_APP)
        await wallet.open_inventory_panel(interaction, GUILD, USER, wallet.TAB_ITEMS, first=True)
        kwargs = send.await_args.kwargs
        kwargs["view"].stop()
        embed = kwargs["embed"]
        for i, name in enumerate(NAMES):
            self.assertIn(await self._token(i), embed.description)
        data = await self._rank()
        self.assertTrue(all(it["app_emoji"] for it in data["inventory_grid"]))

    async def test_rank_command_prefers_interaction_application_identity(self):
        os.environ.pop("DISCORD_CLIENT_ID", None)
        from cogs.leveling import Leveling
        interaction, _ = self._interaction()
        interaction.client.application_id = int(OTHER_APP)
        interaction.response.defer = AsyncMock()
        interaction.followup = SimpleNamespace(send=AsyncMock())
        member = SimpleNamespace(id=USER, display_name="Tester", mention=f"<@{USER}>",
                                 display_avatar=SimpleNamespace(url=""),
                                 joined_at=datetime(2024, 1, 1, tzinfo=timezone.utc), premium_since=None)
        requested = []
        async def fake_fetch(session, url):
            requested.append(url)
            return Image.new("RGBA", (96, 96), (1, 2, 3, 255))
        with patch.object(renderer, "_fetch_image", fake_fetch), \
             patch.object(rank_card_data, "get_rank_card_data",
                          wraps=rank_card_data.get_rank_card_data) as aggregate:
            await Leveling.rank.callback(object(), interaction, member)
        self.assertEqual(aggregate.await_args.kwargs["application_id"], int(APP))
        sent = interaction.followup.send.await_args.kwargs
        self.assertIn("file", sent)
        self.assertNotIn("embed", sent, "rank unexpectedly fell back instead of rendering")
        for i in range(4):
            emoji_id = (await self._token(i)).rsplit(":", 1)[1][:-1]
            self.assertIn(f"https://cdn.discordapp.com/emojis/{emoji_id}.png", requested)

    async def test_item_detail_and_drop_confirm_prefer_interaction_application_identity(self):
        os.environ.pop("DISCORD_CLIENT_ID", None)
        interaction, _ = self._interaction()
        interaction.client.application_id = int(OTHER_APP)
        await wallet.open_item_panel(interaction, GUILD, USER, wallet.TAB_ITEMS, NAMES[0])
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn(await self._token(0), kwargs["embed"].title)
        kwargs["view"].stop()
        await wallet.open_drop_confirm(interaction, GUILD, USER, wallet.TAB_ITEMS, NAMES[0])
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn(await self._token(0), kwargs["embed"].title)
        kwargs["view"].stop()

    async def test_client_application_id_fallback_when_interaction_id_absent(self):
        os.environ.pop("DISCORD_CLIENT_ID", None)
        interaction, send = self._interaction()
        del interaction.application_id
        await wallet.open_inventory_panel(interaction, GUILD, USER, wallet.TAB_ITEMS, first=True)
        kwargs = send.await_args.kwargs
        kwargs["view"].stop()
        for i, name in enumerate(NAMES):
            self.assertIn(f"{await self._token(i)} • ` 1 ` • ` {name} `", kwargs["embed"].description)
        await wallet.open_item_panel(interaction, GUILD, USER, wallet.TAB_ITEMS, NAMES[0])
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn(await self._token(0), kwargs["embed"].title)
        kwargs["view"].stop()
        await wallet.open_drop_confirm(interaction, GUILD, USER, wallet.TAB_ITEMS, NAMES[0])
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn(await self._token(0), kwargs["embed"].title)
        kwargs["view"].stop()

        from cogs.leveling import Leveling
        interaction.response.defer = AsyncMock()
        interaction.followup = SimpleNamespace(send=AsyncMock())
        member = SimpleNamespace(id=USER, display_name="Tester", mention=f"<@{USER}>",
                                 display_avatar=SimpleNamespace(url=""),
                                 joined_at=datetime(2024, 1, 1, tzinfo=timezone.utc), premium_since=None)
        async def fake_fetch(session, url):
            return Image.new("RGBA", (96, 96), (1, 2, 3, 255))
        with patch.object(renderer, "_fetch_image", fake_fetch), \
             patch.object(rank_card_data, "get_rank_card_data",
                          wraps=rank_card_data.get_rank_card_data) as aggregate:
            await Leveling.rank.callback(object(), interaction, member)
        self.assertEqual(aggregate.await_args.kwargs["application_id"], int(APP))
        self.assertIn("file", interaction.followup.send.await_args.kwargs)

    async def test_stale_equipped_title_cleanup_is_preexisting_and_emoji_state_read_only(self):
        stale_title = "لقب قديم غير مملوك"
        await self._exec("INSERT INTO equipped_titles (guild_id,user_id,item_name) VALUES (?,?,?)",
                         (GUILD, USER, stale_title))
        before = await self._snapshot()
        self.assertEqual(len(before["equipped_titles"]), 1)

        # Opening Inventory may run the pre-existing stale-title cleanup, but it
        # must not enter any Discord emoji API, preparation, reconciliation,
        # source-fetch, or upload path.
        with patch.object(SE, "DiscordAPI") as api, \
             patch.object(SE, "prepare_product_emojis", AsyncMock()) as prepare, \
             patch.object(SE, "reconcile_product_emojis", AsyncMock()) as reconcile, \
             patch.object(SE, "reconcile_uncertain_at_startup", AsyncMock()) as startup, \
             patch.object(SE, "_download_and_normalize", AsyncMock()) as download, \
             patch.object(SE, "fetch_source_image", AsyncMock()) as fetch:
            embed = await self._inventory()
        api.assert_not_called()
        prepare.assert_not_called()
        reconcile.assert_not_called()
        startup.assert_not_called()
        download.assert_not_called()
        fetch.assert_not_called()

        after = await self._snapshot()
        self.assertEqual(after["shop_emoji_assets"], before["shop_emoji_assets"])
        self.assertEqual(after["shop_emoji_url_aliases"], before["shop_emoji_url_aliases"])
        changed = {table for table in before if before[table] != after[table]}
        self.assertEqual(changed, {"equipped_titles", "inventory_activity"},
                         "only the pre-existing stale-title cleanup may write")
        self.assertEqual(after["equipped_titles"], [])
        event = await self._one(
            "SELECT event_type,item_name,item_type,quantity,source FROM inventory_activity "
            "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (GUILD, USER))
        self.assertEqual(event, ("unequipped", stale_title, "title", 1, "equipment"))
        for i, name in enumerate(NAMES):
            self.assertIn(f"{await self._token(i)} • ` 1 ` • ` {name} `", embed.description)

    async def test_missing_and_non_ready_aliases_fallback_without_mutation(self):
        await self._exec("DELETE FROM shop_emoji_url_aliases WHERE source_url=?", (URLS[0],))
        for status, url in zip(("preparing", "uncertain", "missing"), URLS[1:]):
            await self._exec("UPDATE shop_emoji_assets SET status=? WHERE content_hash="
                             "(SELECT content_hash FROM shop_emoji_url_aliases WHERE source_url=?)", (status, url))
        before = await self._snapshot()
        embed = await self._inventory()
        for name in NAMES:
            self.assertIn(f"⚪ • ` 1 ` • ` {name} `", embed.description)
        data = await self._rank()
        self.assertTrue(all(not it["app_emoji"] for it in data["inventory_grid"]))
        self.assertEqual(await self._snapshot(), before)

    async def test_stale_or_missing_catalog_still_resolves_current_listing_alias(self):
        await self._exec("UPDATE item_catalog SET icon_url=? WHERE guild_id=? AND item_name=?",
                         ("https://assets.example.com/old.png", GUILD, NAMES[0]))
        await self._exec("UPDATE item_catalog SET icon_url=NULL WHERE guild_id=? AND item_name=?", (GUILD, NAMES[1]))
        embed = await self._inventory()
        for i in (0, 1):
            self.assertIn(await self._token(i), embed.description)
        data = await self._rank()
        grid = {it["item_name"]: it for it in data["inventory_grid"]}
        self.assertEqual(grid[NAMES[0]]["app_emoji"], await self._token(0))
        self.assertEqual(grid[NAMES[0]]["icon_url"], "https://assets.example.com/old.png")
        self.assertEqual(grid[NAMES[0]]["listing_icon_url"], URLS[0])
        self.assertEqual(grid[NAMES[1]]["app_emoji"], await self._token(1))
        self.assertEqual(grid[NAMES[1]]["icon_url"], "")

    async def test_wrong_guild_or_application_alias_is_never_borrowed(self):
        await self._exec("UPDATE shop_emoji_url_aliases SET guild_id=? WHERE source_url=?", (OTHER_GUILD, URLS[0]))
        row = await self._one("SELECT content_hash FROM shop_emoji_url_aliases WHERE source_url=?", (URLS[1],))
        await self._exec("INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,animated,"
                         "creator_guild_id,pipeline_version,status) VALUES (?,?,?,?,0,?,?, 'ready')",
                         (OTHER_APP, row[0], "other_app_emoji", "1562903222086279999", GUILD, SE.PIPELINE_VERSION))
        await self._exec("UPDATE shop_emoji_url_aliases SET application_id=? WHERE source_url=?", (OTHER_APP, URLS[1]))
        before = await self._snapshot()
        embed = await self._inventory()
        for name in NAMES[:2]:
            self.assertIn(f"⚪ • ` 1 ` • ` {name} `", embed.description)
        for i in (2, 3):
            self.assertIn(await self._token(i), embed.description)
        data = await self._rank()
        grid = {it["item_name"]: it for it in data["inventory_grid"]}
        self.assertEqual(grid[NAMES[0]]["app_emoji"], "")
        self.assertEqual(grid[NAMES[1]]["app_emoji"], "")
        self.assertEqual(await self._snapshot(), before)

    async def test_ambiguous_listing_is_not_guessed(self):
        await self._exec("INSERT INTO shop_items (guild_id,name,price,type,icon_url,enabled) VALUES (?,?,100,'custom',?,1)",
                         (GUILD, NAMES[0], "https://assets.example.com/other.png"))
        await self._exec("UPDATE item_catalog SET icon_url=NULL WHERE guild_id=? AND item_name=?", (GUILD, NAMES[0]))
        before = await self._snapshot()
        embed = await self._inventory()
        self.assertIn(f"⚪ • ` 1 ` • ` {NAMES[0]} `", embed.description)
        data = await self._rank()
        item = next(it for it in data["inventory_grid"] if it["item_name"] == NAMES[0])
        self.assertEqual(item["app_emoji"], "")
        self.assertEqual(item["listing_icon_url"], "")
        self.assertEqual(await self._snapshot(), before)

    async def test_url_canonicalization_keeps_signed_query_semantics(self):
        equivalent = "https://ASSETS.EXAMPLE.COM:443/item-0.png?v=1"
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (equivalent, self.product_ids[0]))
        await self._exec("UPDATE item_catalog SET icon_url=? WHERE guild_id=? AND item_name=?", (equivalent, GUILD, NAMES[0]))
        embed = await self._inventory()
        self.assertIn(await self._token(0), embed.description)
        changed = "https://assets.example.com/item-0.png?v=2"
        await self._exec("UPDATE shop_items SET icon_url=? WHERE id=?", (changed, self.product_ids[0]))
        await self._exec("UPDATE item_catalog SET icon_url=? WHERE guild_id=? AND item_name=?", (changed, GUILD, NAMES[0]))
        embed = await self._inventory()
        self.assertIn(f"⚪ • ` 1 ` • ` {NAMES[0]} `", embed.description)


if __name__ == "__main__":
    unittest.main(verbosity=2)
