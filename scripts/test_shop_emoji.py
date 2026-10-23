"""Focused Shop application-emoji tests; all Discord and SQLite state is isolated."""

import asyncio
import hashlib
import io
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from PIL import Image

from utils import shop_emoji as SE
from utils import shop_publication as pub
from utils import shop_publisher as SP


APP = "123456789012345678"
GUILD = 10
BOT = "999999999999999999"
URL1 = "https://cdn.example.com/one.png"
URL2 = "https://cdn.example.com/two.png"
PNG1 = b"final-normalized-png-one"
PNG2 = b"final-normalized-png-two"


def run(coro):
    return asyncio.run(coro)


class FakeDiscord:
    def __init__(self):
        self.application_id = APP
        self.remote = []
        self.create_calls = []
        self.delete_calls = []
        self.list_calls = 0
        self.create_error = None
        self.delete_error = None
        self.list_error = None
        self.create_hook = None
        self.auto_remote = True
        self.next_id = 1000

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def verify_application(self):
        return None

    async def bot_user_id(self):
        return BOT

    async def list_emojis(self):
        self.list_calls += 1
        if self.list_error:
            raise self.list_error
        return [dict(item) for item in self.remote]

    async def create_emoji(self, name, final_png, bot_user_id):
        self.create_calls.append((name, final_png, bot_user_id))
        if self.create_hook:
            await self.create_hook(name, final_png, bot_user_id)
        if self.create_error:
            raise self.create_error
        self.next_id += 1
        created = {"id": str(self.next_id), "name": name, "animated": False,
                   "application_id": APP, "user": {"id": BOT}}
        if self.auto_remote:
            self.remote.append(dict(created))
        return created

    async def delete_emoji(self, emoji_id):
        self.delete_calls.append(str(emoji_id))
        if self.delete_error:
            raise self.delete_error
        self.remote = [item for item in self.remote if str(item["id"]) != str(emoji_id)]


@pytest.fixture
def shop(tmp_path, monkeypatch):
    path = tmp_path / "shop-emoji.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE shop_items (id INTEGER PRIMARY KEY, guild_id INTEGER, icon_url TEXT)")
        for statement in SE.SHOP_EMOJI_SCHEMA:
            db.execute(statement)
        db.executemany("INSERT INTO shop_items(id,guild_id,icon_url) VALUES(?,?,?)",
                       [(1, GUILD, URL1), (2, GUILD, URL1), (3, GUILD, URL2),
                        (4, GUILD + 1, URL1)])
    monkeypatch.setattr(SE, "DB_PATH", str(path))
    monkeypatch.setenv("DISCORD_CLIENT_ID", APP)
    monkeypatch.setenv("DISCORD_TOKEN", "test-token-never-sent")
    SE._preparation_locks.clear()
    fake = FakeDiscord()
    monkeypatch.setattr(SE, "DiscordAPI", lambda: fake)
    images = {URL1: PNG1, URL2: PNG2}

    async def download(url):
        return images[url]

    monkeypatch.setattr(SE, "_download_and_normalize", download)
    return SimpleNamespace(path=str(path), fake=fake, images=images)


def asset_row(shop, content_hash):
    with sqlite3.connect(shop.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM shop_emoji_assets WHERE application_id=? AND content_hash=?",
            (APP, content_hash)).fetchone()
        return dict(row) if row else None


def put_asset(shop, final_png=PNG1, status="uncertain", emoji_id=None, attempt="attempt-1"):
    content_hash = SE.content_identity(final_png)
    with sqlite3.connect(shop.path) as db:
        db.execute(
            "INSERT OR REPLACE INTO shop_emoji_assets "
            "(application_id,content_hash,emoji_name,emoji_id,animated,creator_guild_id,pipeline_version,status,"
            "attempt_id,attempt_started_at,quarantine_until,last_error) VALUES(?,?,?,?,0,?,?,?,?,?,?,?)",
            (APP, content_hash, SE.deterministic_emoji_name(content_hash), emoji_id, GUILD,
             SE.PIPELINE_VERSION, status, attempt, SE._iso(),
             SE._iso(SE._utcnow() + timedelta(minutes=15)) if status == "uncertain" else None,
             "test state"))
        db.execute(
            "INSERT OR REPLACE INTO shop_emoji_url_aliases "
            "(application_id,guild_id,source_url,content_hash) VALUES(?,?,?,?)",
            (APP, GUILD, URL1, content_hash))
    return content_hash


def remote_emoji(content_hash, emoji_id):
    return {"id": str(emoji_id), "name": SE.deterministic_emoji_name(content_hash),
            "animated": False, "application_id": APP, "user": {"id": BOT}}


def test_hashing_reuse_and_same_batch_alias_visibility(shop):
    # Model an eventually consistent list: the second URL still needs the
    # batch-local ready ID even when the next list would not include it yet.
    shop.fake.auto_remote = False
    shop.images[URL2] = PNG1
    first, second = run(SE.prepare_product_emojis(GUILD, [1, 3]))["results"]
    assert first["status"] == second["status"] == "ready"
    assert first["emoji_id"] == second["emoji_id"]
    assert len(shop.fake.create_calls) == 1
    content_hash = SE.content_identity(PNG1)
    assert first["content_hash"] == content_hash == hashlib.sha256(PNG1).hexdigest()
    assert first["emoji_name"] == SE.deterministic_emoji_name(content_hash)
    assert asset_row(shop, content_hash)["status"] == "ready"
    # Two source URLs share one final-byte asset and one Discord creation.
    with sqlite3.connect(shop.path) as db:
        assert db.execute("SELECT COUNT(*) FROM shop_emoji_assets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM shop_emoji_url_aliases").fetchone()[0] == 2


def test_application_asset_reuse_across_guild_aliases(shop):
    run(SE.prepare_product_emojis(GUILD, [1]))
    other = run(SE.prepare_product_emojis(GUILD + 1, [4]))["results"][0]
    assert other["code"] == "reused"
    assert len(shop.fake.create_calls) == 1
    with sqlite3.connect(shop.path) as db:
        assert db.execute("SELECT COUNT(*) FROM shop_emoji_url_aliases").fetchone()[0] == 2


def test_schema_migration_backfills_aliased_creator_attribution(tmp_path):
    path = tmp_path / "shop-emoji-migration.db"
    old_asset_schema = SE.SHOP_EMOJI_SCHEMA[0].replace(
        "        creator_guild_id     INTEGER,\n", "")
    assert old_asset_schema != SE.SHOP_EMOJI_SCHEMA[0]
    with sqlite3.connect(path) as db:
        db.execute(old_asset_schema)
        for statement in SE.SHOP_EMOJI_SCHEMA[1:]:
            db.execute(statement)
        for final_png in (PNG1, PNG2):
            content_hash = SE.content_identity(final_png)
            db.execute(
                "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,animated,"
                "pipeline_version,status,attempt_id,attempt_started_at,quarantine_until,last_error) "
                "VALUES (?,?,?,NULL,0,?,'ready',NULL,NULL,NULL,NULL)",
                (APP, content_hash, SE.deterministic_emoji_name(content_hash), SE.PIPELINE_VERSION))
        db.execute(
            "INSERT INTO shop_emoji_url_aliases (application_id,guild_id,source_url,content_hash) "
            "VALUES (?,?,?,?)", (APP, GUILD, URL1, SE.content_identity(PNG1)))

    async def migrate():
        async with SE.aiosqlite.connect(path) as db:
            await SE.ensure_shop_emoji_schema(db)

    run(migrate())
    with sqlite3.connect(path) as db:
        attributed = db.execute(
            "SELECT creator_guild_id FROM shop_emoji_assets WHERE application_id=? AND content_hash=?",
            (APP, SE.content_identity(PNG1))).fetchone()[0]
        unattributed = db.execute(
            "SELECT creator_guild_id FROM shop_emoji_assets WHERE application_id=? AND content_hash=?",
            (APP, SE.content_identity(PNG2))).fetchone()[0]
        assert attributed == GUILD
        assert unattributed is None
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                          "AND name='idx_shop_emoji_assets_creator'").fetchone()


def test_durable_guild_cap_counts_old_unaliased_assets(shop, monkeypatch):
    monkeypatch.setattr(SE, "GUILD_ASSET_LIMIT", 1)
    first = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert first["status"] == "ready"
    old_hash = first["content_hash"]
    assert asset_row(shop, old_hash)["creator_guild_id"] == GUILD
    with sqlite3.connect(shop.path) as db:
        db.execute("DELETE FROM shop_emoji_url_aliases WHERE application_id=? AND guild_id=?",
                   (APP, GUILD))
    blocked = run(SE.prepare_product_emojis(GUILD, [3]))["results"][0]
    assert blocked["status"] == "failed" and blocked["code"] == "guild_emoji_quota"
    assert "durable per-server limit is 1" in blocked["message"]
    assert asset_row(shop, SE.content_identity(PNG2)) is None
    assert len(shop.fake.create_calls) == 1
    assert asset_row(shop, old_hash)["status"] == "ready"
    # Ready reuse by another guild does not consume another creation slot.
    other = run(SE.prepare_product_emojis(GUILD + 1, [4]))["results"][0]
    assert other["status"] == "ready" and other["content_hash"] == old_hash
    assert len(shop.fake.create_calls) == 1


def test_guild_cap_accounting_includes_only_active_or_recovering_states(shop):
    for status in ("ready", "preparing", "uncertain", "failed", "missing"):
        put_asset(shop, final_png=("asset-" + status).encode(), status=status,
                  attempt=None if status in {"ready", "failed", "missing"} else "attempt-" + status)

    async def usage():
        async with SE.aiosqlite.connect(shop.path) as db:
            return await SE._guild_asset_usage(db, APP, GUILD)

    assert run(usage()) == 3


def test_guild_cap_claim_is_atomic_across_database_workers(shop, monkeypatch):
    monkeypatch.setattr(SE, "GUILD_ASSET_LIMIT", 1)

    async def worker(final_png):
        async with SE.aiosqlite.connect(shop.path) as db:
            return await SE._insert_asset_or_claim(
                db, APP, GUILD, SE.content_identity(final_png), [], BOT)

    async def concurrent():
        return await asyncio.gather(worker(PNG1), worker(PNG2), return_exceptions=True)

    outcomes = run(concurrent())
    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    claims = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    assert len(claims) == 1 and claims[0][1] is not None
    assert len(errors) == 1 and errors[0].code == "guild_emoji_quota"
    with sqlite3.connect(shop.path) as db:
        assert db.execute("SELECT COUNT(*) FROM shop_emoji_assets").fetchone()[0] == 1


def test_concurrent_preparation_has_one_claim_and_one_post(shop):
    async def concurrent():
        return await asyncio.gather(
            SE.prepare_product_emojis(GUILD, [1]),
            SE.prepare_product_emojis(GUILD, [2]))

    results = run(concurrent())
    assert len(shop.fake.create_calls) == 1
    assert all(result["results"][0]["status"] == "ready" for result in results)


def test_atomic_insert_claim_excludes_a_second_db_worker(shop):
    content_hash = SE.content_identity(PNG1)

    async def worker():
        async with SE.aiosqlite.connect(shop.path) as db:
            return await SE._insert_asset_or_claim(db, APP, GUILD, content_hash, [], BOT)

    async def concurrent():
        return await asyncio.gather(worker(), worker())

    rows = run(concurrent())
    assert sum(claimed is not None for _asset, claimed, _duplicates in rows) == 1
    assert {row[0]["attempt_id"] for row in rows} == {rows[0][0]["attempt_id"]}


def _capacity_remote(count):
    return [{"id": str(20000 + index), "name": f"other_{index}",
             "animated": index % 2 == 0, "user": {"id": BOT}}
            for index in range(count)]


def test_application_capacity_counts_animated_and_static_entries(shop):
    content_hash = put_asset(shop, status="preparing")
    shop.fake.remote = _capacity_remote(SE.APPLICATION_EMOJI_LIMIT)
    assert any(emoji["animated"] for emoji in shop.fake.remote)

    async def create():
        async with SE.aiosqlite.connect(shop.path) as db:
            asset = await SE._get_asset(db, APP, content_hash)
            return await SE._create_claimed(db, shop.fake, asset, PNG1, BOT)

    result = run(create())
    assert result["status"] == "failed" and result["code"] == "emoji_capacity"
    assert shop.fake.create_calls == []


def test_application_near_capacity_allows_one_final_creation(shop):
    content_hash = put_asset(shop, status="preparing")
    shop.fake.remote = _capacity_remote(SE.APPLICATION_EMOJI_LIMIT - 1)

    async def create():
        async with SE.aiosqlite.connect(shop.path) as db:
            asset = await SE._get_asset(db, APP, content_hash)
            return await SE._create_claimed(db, shop.fake, asset, PNG1, BOT)

    result = run(create())
    assert result["status"] == "ready" and result["code"] == "created"
    assert len(shop.fake.create_calls) == 1


def test_uncertain_post_never_retries_without_ack_and_quarantine(shop):
    shop.fake.create_error = SE.DiscordAPIError(
        "timeout", code="create_uncertain", status=504, uncertain=True)
    first = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert first["status"] == "uncertain"
    assert len(shop.fake.create_calls) == 1
    again = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert again["status"] == "uncertain" and len(shop.fake.create_calls) == 1
    blocked = run(SE.prepare_product_emojis(
        GUILD, [1], retry_uncertain=True, acknowledge_duplicate_risk=True))["results"][0]
    assert blocked["status"] == "uncertain" and len(shop.fake.create_calls) == 1


def test_reconciliation_adopts_one_late_completion_without_second_post(shop):
    shop.fake.create_error = SE.DiscordAPIError("5xx", uncertain=True)
    uncertain = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    content_hash = uncertain["content_hash"]
    shop.fake.remote = [remote_emoji(content_hash, 2001)]
    result = run(SE.reconcile_product_emojis(GUILD, [1]))["results"][0]
    assert result["status"] == "ready" and result["emoji_id"] == "2001"
    assert len(shop.fake.create_calls) == 1
    reused = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert reused["code"] == "reused" and len(shop.fake.create_calls) == 1


def test_initial_name_match_requires_provenance_before_adoption(shop):
    content_hash = SE.content_identity(PNG1)
    candidate = remote_emoji(content_hash, 2001)
    del candidate["user"]
    shop.fake.remote = [candidate]
    rejected = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert rejected["status"] == "failed" and rejected["emoji_id"] is None
    assert asset_row(shop, content_hash)["emoji_id"] is None
    assert shop.fake.create_calls == []
    shop.fake.remote = [remote_emoji(content_hash, 2001)]
    adopted = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert adopted["status"] == "ready" and adopted["emoji_id"] == "2001"
    assert shop.fake.create_calls == []


@pytest.mark.parametrize("metadata", [
    {},
    {"user": None},
    {"user": {}},
    {"user": {"id": "12345"}},
    {"user": {"id": BOT}, "application_id": "12345"},
])
def test_name_matching_never_adopts_without_exact_bot_uploader_provenance(shop, metadata):
    content_hash = put_asset(shop)
    candidate = {"id": "2001", "name": SE.deterministic_emoji_name(content_hash),
                 "animated": False, **metadata}
    shop.fake.remote = [candidate]
    result = run(SE.reconcile_product_emojis(GUILD, [1]))["results"][0]
    assert result["status"] == "uncertain"
    assert result["emoji_id"] is None
    assert asset_row(shop, content_hash)["emoji_id"] is None
    assert asset_row(shop, content_hash)["status"] == "uncertain"
    assert shop.fake.create_calls == [] and shop.fake.delete_calls == []


def test_ready_bound_validation_fails_closed_on_missing_uploader(shop):
    content_hash = put_asset(shop, status="ready", emoji_id="2001", attempt=None)
    bound = remote_emoji(content_hash, 2001)
    del bound["user"]
    shop.fake.remote = [bound]
    result = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert result["status"] == "failed" and result["code"] == "bound_emoji_conflict"
    assert asset_row(shop, content_hash)["status"] == "failed"
    assert shop.fake.create_calls == []


def test_uncertain_retry_requires_elapsed_quarantine_then_is_explicit(shop):
    shop.fake.create_error = SE.DiscordAPIError("5xx", uncertain=True)
    uncertain = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    with sqlite3.connect(shop.path) as db:
        db.execute("UPDATE shop_emoji_assets SET quarantine_until=? WHERE content_hash=?",
                   (SE._iso(SE._utcnow() - timedelta(seconds=1)), uncertain["content_hash"]))
    shop.fake.create_error = None
    retried = run(SE.prepare_product_emojis(
        GUILD, [1], retry_uncertain=True, acknowledge_duplicate_risk=True))["results"][0]
    assert retried["status"] == "ready"
    assert len(shop.fake.create_calls) == 2


def test_uncertain_retry_remaps_changed_source_without_touching_old_attempt(shop):
    old_hash = put_asset(shop)
    with sqlite3.connect(shop.path) as db:
        db.execute("UPDATE shop_emoji_assets SET quarantine_until=? WHERE content_hash=?",
                   (SE._iso(SE._utcnow() - timedelta(seconds=1)), old_hash))
    old_before = asset_row(shop, old_hash)
    shop.images[URL1] = PNG2
    result = run(SE.prepare_product_emojis(
        GUILD, [1], retry_uncertain=True, acknowledge_duplicate_risk=True))["results"][0]
    new_hash = SE.content_identity(PNG2)
    assert result["code"] == "remapped_changed_source"
    assert result["status"] == "ready" and result["content_hash"] == new_hash
    assert result["remapped_from_content_hash"] == old_hash
    old_after = asset_row(shop, old_hash)
    for field in ("status", "attempt_id", "attempt_started_at", "quarantine_until", "last_error"):
        assert old_after[field] == old_before[field]
    assert old_after["status"] == "uncertain" and old_after["attempt_id"] == "attempt-1"
    assert len(shop.fake.create_calls) == 1
    assert shop.fake.create_calls[0][0] == SE.deterministic_emoji_name(new_hash)
    with sqlite3.connect(shop.path) as db:
        aliased = db.execute(
            "SELECT content_hash FROM shop_emoji_url_aliases WHERE application_id=? AND guild_id=? AND source_url=?",
            (APP, GUILD, URL1)).fetchone()[0]
        assert aliased == new_hash
    # The URL is not stuck on the old uncertain identity and needs no further POST.
    reused = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert reused["status"] == "ready" and reused["content_hash"] == new_hash
    assert len(shop.fake.create_calls) == 1


def test_late_completion_cannot_overwrite_a_newer_attempt(shop):
    content_hash = put_asset(shop, status="preparing", attempt="old-attempt")

    async def scenario():
        async with SE.aiosqlite.connect(shop.path) as db:
            asset = await SE._get_asset(db, APP, content_hash)

            async def late_hook(*_args):
                async with SE.aiosqlite.connect(shop.path) as other:
                    await other.execute(
                        "UPDATE shop_emoji_assets SET attempt_id='new-attempt',status='preparing' "
                        "WHERE application_id=? AND content_hash=?", (APP, content_hash))
                    await other.commit()

            shop.fake.create_hook = late_hook
            return await SE._create_claimed(db, shop.fake, asset, PNG1, BOT)

    result = run(scenario())
    assert result["code"] == "late_completion"
    current = asset_row(shop, content_hash)
    assert current["attempt_id"] == "new-attempt" and current["status"] == "preparing"


def test_duplicates_are_reported_without_selection_or_deletion(shop):
    content_hash = put_asset(shop)
    shop.fake.remote = [remote_emoji(content_hash, 3001), remote_emoji(content_hash, 3002)]
    result = run(SE.reconcile_product_emojis(GUILD, [1]))["results"][0]
    assert result["status"] == "uncertain"
    assert result["duplicates"] == ["3001", "3002"]
    assert asset_row(shop, content_hash)["emoji_id"] is None
    assert shop.fake.delete_calls == []


def test_duplicate_delete_requires_confirmation_and_fresh_id(shop):
    content_hash = put_asset(shop)
    shop.fake.remote = [remote_emoji(content_hash, 3001), remote_emoji(content_hash, 3002)]
    with pytest.raises(SE.ShopEmojiError, match="confirmation"):
        run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3002", confirm=False))
    with pytest.raises(SE.ShopEmojiError) as error:
        run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3999", confirm=True))
    assert error.value.code == "duplicate_not_confirmed"
    deleted = run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3002", confirm=True))
    assert deleted["deleted_emoji_id"] == "3002"
    assert shop.fake.delete_calls == ["3002"]
    adopted = run(SE.reconcile_product_emojis(GUILD, [1]))["results"][0]
    assert adopted["status"] == "ready" and adopted["emoji_id"] == "3001"


def test_confirmed_duplicate_delete_404_is_absence_without_retry(shop):
    content_hash = put_asset(shop, status="ready", emoji_id="3001", attempt=None)
    shop.fake.remote = [remote_emoji(content_hash, 3001), remote_emoji(content_hash, 3002)]
    shop.fake.delete_error = SE.DiscordAPIError(
        "not found", status=404, http_status=404, uncertain=False)
    result = run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3002", confirm=True))
    assert result["success"] is True and result["deleted_emoji_id"] == "3002"
    assert shop.fake.delete_calls == ["3002"]


def test_uncertain_duplicate_delete_is_never_automatically_retried(shop):
    content_hash = put_asset(shop, status="ready", emoji_id="3001", attempt=None)
    shop.fake.remote = [remote_emoji(content_hash, 3001), remote_emoji(content_hash, 3002)]
    shop.fake.delete_error = SE.DiscordAPIError(
        "timeout", status=504, http_status=None, uncertain=True)
    with pytest.raises(SE.ShopEmojiError) as error:
        run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3002", confirm=True))
    assert error.value.code == "delete_uncertain" and error.value.status == 202
    assert shop.fake.delete_calls == ["3002"]


def test_duplicate_delete_rejects_name_match_without_uploader_provenance(shop):
    content_hash = put_asset(shop, status="ready", emoji_id="3001", attempt=None)
    target = remote_emoji(content_hash, 3002)
    del target["user"]
    shop.fake.remote = [remote_emoji(content_hash, 3001), target]
    with pytest.raises(SE.ShopEmojiError) as error:
        run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3002", confirm=True))
    assert error.value.code == "duplicate_ownership_conflict"
    assert shop.fake.delete_calls == []


def test_failed_duplicate_conflict_reconciles_after_explicit_cleanup(shop):
    content_hash = put_asset(shop, status="failed", attempt=None)
    shop.fake.remote = [remote_emoji(content_hash, 3501), remote_emoji(content_hash, 3502)]
    conflict = run(SE.reconcile_product_emojis(GUILD, [1]))["results"][0]
    assert conflict["status"] == "failed" and conflict["duplicates"] == ["3501", "3502"]
    run(SE.delete_confirmed_duplicate(GUILD, content_hash, "3502", confirm=True))
    recovered = run(SE.reconcile_product_emojis(GUILD, [1]))["results"][0]
    assert recovered["status"] == "ready" and recovered["emoji_id"] == "3501"


def test_ready_bound_id_is_protected_and_confirmed_absence_can_recover(shop):
    content_hash = put_asset(shop, status="ready", emoji_id="4001", attempt=None)
    shop.fake.remote = [remote_emoji(content_hash, 4001), remote_emoji(content_hash, 4002)]
    with pytest.raises(SE.ShopEmojiError) as error:
        run(SE.delete_confirmed_duplicate(GUILD, content_hash, "4001", confirm=True))
    assert error.value.code == "bound_emoji_protected"
    shop.fake.remote = []
    recovered = run(SE.prepare_product_emojis(GUILD, [1]))["results"][0]
    assert recovered["status"] == "ready" and recovered["emoji_id"] != "4001"
    assert len(shop.fake.create_calls) == 1
    assert asset_row(shop, content_hash)["status"] == "ready"


def test_preflight_missing_and_api_failure_never_mark_asset_missing(shop):
    content_hash = put_asset(shop, status="ready", emoji_id="5001", attempt=None)
    reference = [{"id": "5001", "name": SE.deterministic_emoji_name(content_hash), "animated": False}]
    shop.fake.remote = [remote_emoji(content_hash, 5001)]
    assert run(SE.preflight_prepared_emoji_ids(GUILD, reference)) == {"checked": 1}
    shop.fake.remote = []
    with pytest.raises(SE.ShopEmojiError) as error:
        run(SE.preflight_prepared_emoji_ids(GUILD, reference))
    assert error.value.code == "product_emoji_missing"
    assert asset_row(shop, content_hash)["status"] == "ready"
    shop.fake.list_error = SE.DiscordAPIError("rate limited", status=429)
    with pytest.raises(SE.DiscordAPIError):
        run(SE.preflight_prepared_emoji_ids(GUILD, reference))
    assert asset_row(shop, content_hash)["status"] == "ready"


def test_publication_preflight_blocks_before_pending_or_update_claim(monkeypatch):
    prepared = {"blocking_errors": [], "warnings": [], "warning_token": "ack",
                "payload": {"content": "x"}, "action": {},
                "product_emoji_references": [{"id": "5001", "name": "shop", "animated": False}]}
    monkeypatch.setattr(pub, "prepare_design", lambda *args: prepared)
    monkeypatch.setattr(pub, "_product_emoji_preflight_error", lambda *args: {
        "code": "product_emoji_missing", "path": "product.emoji", "message": "missing"})

    class NeverSend:
        def validate_channel_and_permissions(self, *args):
            return None

        def create_message(self, *args):
            raise AssertionError("send must be blocked")

        def edit_message(self, *args):
            raise AssertionError("edit must be blocked")

    published = pub.post_publish(GUILD, 1, 20, warning_token="ack", rest=NeverSend())
    assert published["outcome"] == "blocked"
    prepared["blocking_errors"] = []
    monkeypatch.setattr(pub, "claim_update", lambda *args: (_ for _ in ()).throw(
        AssertionError("update must not be claimed")))
    updated = pub.update_publication(
        GUILD, {"id": 7, "design_id": 1, "channel_id": 20, "message_id": 30, "status": "published"},
        warning_token="ack", rest=NeverSend())
    assert updated["outcome"] == "blocked"


def test_supported_token_paths_and_unsupported_publication_blocker():
    product = {"id": 1, "name": "One", "enabled": 1, "app_emoji": "<:shop_one:5001>"}
    design = {
        "id": 1,
        "presentation": {"mode": "per_product", "content": "{{product.emoji}}", "embeds": [{
            "title": "{{product.emoji}}", "description": "{{product.emoji}}",
            "fields": [{"name": "{{product.emoji}}", "value": "{{product.emoji}}"}],
            "footer": {"text": "{{product.emoji}}"},
        }]},
        "products": [1], "action": {"kind": "buttons", "entries": [
            {"product_id": 1, "emoji": "{{product.emoji}}"}]},
    }
    resolved = SP.preview_design(design, {1: product}, {})
    assert resolved["content"] == "<:shop_one:5001>"
    embed = resolved["embeds"][0]
    assert embed["description"] == embed["fields"][0]["value"] == "<:shop_one:5001>"
    assert embed["title"] == embed["fields"][0]["name"] == "{{product.emoji}}"
    assert embed["footer"]["text"] == "{{product.emoji}}"
    blocked = {warning["path"] for warning in resolved["warnings"]
               if warning["code"] == "unsupported_product_emoji_surface"}
    assert blocked == {"embeds.0.title", "embeds.0.fields.0.name", "embeds.0.footer.text",
                       "action.entries.0.emoji"}
    assert "unsupported_product_emoji_surface" in pub.BLOCKING_PREVIEW_CODES


def test_unprepared_supported_and_no_context_tokens_resolve_empty_with_paths():
    product = {"id": 1, "name": "One", "enabled": 1, "app_emoji": ""}
    design = {"presentation": {"mode": "per_product", "content": "x{{product.emoji}}y",
                               "embeds": [{"description": "d{{product.emoji}}"}]},
              "products": [1], "action": {"kind": "buttons", "entries": [{"product_id": 1}]}}
    resolved = SP.preview_design(design, {1: product}, {})
    assert resolved["content"] == "xy" and resolved["embeds"][0]["description"] == "d"
    assert {warning["path"] for warning in resolved["warnings"]
            if warning["code"] == "empty_value"} == {"content", "embeds.0.description"}
    design["presentation"]["mode"] = "frame"
    frame = SP.preview_design(design, {1: product}, {})
    assert frame["content"] == "xy"
    assert any(warning["code"] == "unresolved_product_token" and warning["path"] == "content"
               for warning in frame["warnings"])


def test_reference_extraction_ignores_unsupported_and_user_typed_markup():
    resolved = SP.preview_design({
        "presentation": {"mode": "per_product", "content": "<:user_typed:9999> {{product.emoji}}",
                         "embeds": [{"title": "{{product.emoji}}", "description": "{{product.emoji}}"}]},
        "products": [1], "action": {"kind": "buttons", "entries": [{"product_id": 1}]},
    }, {1: {"id": 1, "enabled": 1, "app_emoji": "<:shop_one:5001>"}}, {})
    assert SE.product_emoji_references(resolved) == [
        {"id": "5001", "name": "shop_one", "animated": False}]


def test_normalize_static_images_to_deterministic_rgba_png():
    source = io.BytesIO()
    Image.new("RGB", (300, 100), (10, 20, 30)).save(source, format="JPEG")
    first = SE.normalize_image(source.getvalue())
    second = SE.normalize_image(source.getvalue())
    assert first == second and len(first) <= SE.MAX_EMOJI_BYTES
    with Image.open(io.BytesIO(first)) as image:
        assert image.size == (128, 128) and image.mode == "RGBA"
        assert image.format == "PNG" and getattr(image, "n_frames", 1) == 1


def test_normalization_rejects_gif_animation_pixels_size_and_format():
    assert SE.SUPPORTED_IMAGE_FORMATS == ("PNG", "JPEG", "WEBP")
    single_gif = io.BytesIO()
    Image.new("RGBA", (4, 4), "red").save(single_gif, format="GIF")
    with pytest.raises(SE.SourceImageError) as error:
        SE.normalize_image(single_gif.getvalue())
    assert error.value.code == "unsupported_source_type"
    # A supported container still may not discard frames: reject animated APNG.
    animated = io.BytesIO()
    Image.new("RGBA", (4, 4), "red").save(
        animated, format="PNG", save_all=True, append_images=[Image.new("RGBA", (4, 4), "blue")],
        duration=100, loop=0)
    with pytest.raises(SE.SourceImageError) as error:
        SE.normalize_image(animated.getvalue())
    assert error.value.code == "animated_source"
    wide = io.BytesIO()
    Image.new("RGB", (SE.MAX_IMAGE_SIDE + 1, 1)).save(wide, format="PNG")
    with pytest.raises(SE.SourceImageError) as error:
        SE.normalize_image(wide.getvalue())
    assert error.value.code == "image_pixel_limit"
    with pytest.raises(SE.SourceImageError):
        SE.normalize_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(SE.SourceImageError) as error:
        SE.normalize_image(b"x" * (SE.MAX_SOURCE_BYTES + 1))
    assert error.value.code == "source_too_large"


@pytest.mark.parametrize("url", [
    "http://example.com/image.png", "https://127.0.0.1/image.png",
    "https://10.0.0.1/image.png", "https://[::1]/image.png",
    "https://[::ffff:127.0.0.1]/image.png", "https://[64:ff9b::7f00:1]/image.png",
    "https://user:pass@example.com/image.png", "https://example.com/image.png#fragment",
    "https://example.com\\@127.0.0.1/image.png",
])
def test_unsafe_source_urls_are_rejected(url):
    with pytest.raises(SE.SourceImageError):
        SE.canonicalize_url(url)


@pytest.mark.parametrize("port", [80, 8443, 444, 65535])
def test_https_source_urls_reject_every_explicit_non_443_port(port):
    with pytest.raises(SE.SourceImageError) as error:
        SE.canonicalize_url(f"https://cdn.example.com:{port}/image.png?signature=keep")
    assert error.value.code == "invalid_source_port"


def test_https_source_urls_allow_omitted_or_explicit_443_port_only():
    omitted = SE.canonicalize_url("https://cdn.example.com/image.png?signature=keep")
    explicit = SE.canonicalize_url("https://cdn.example.com:443/image.png?signature=keep")
    assert omitted == explicit == "https://cdn.example.com/image.png?signature=keep"
    assert SE.canonicalize_url("https://8.8.8.8:443/image.png") == "https://8.8.8.8/image.png"


def test_resolver_rejects_mixed_dns_answers_and_pins_original_hostname(monkeypatch):
    def mixed(*args, **kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(SE.socket, "getaddrinfo", mixed)
    with pytest.raises(SE.SourceImageError) as error:
        run(SE._PinnedResolver().resolve("cdn.example.com", 443))
    assert error.value.code == "unsafe_source_address"

    def public(*args, **kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(SE.socket, "getaddrinfo", public)
    answer = run(SE._PinnedResolver().resolve("cdn.example.com", 443))[0]
    assert answer["host"] == "8.8.8.8" and answer["hostname"] == "cdn.example.com"


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = self

    async def read(self):
        return self._body

    async def iter_chunked(self, _size):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def test_redirect_to_private_target_is_never_requested_and_no_credentials_escape(monkeypatch):
    session = FakeSession([FakeResponse(302, b"", {"Location": "https://127.0.0.1/internal"})])
    monkeypatch.setattr(SE.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(SE.aiohttp, "TCPConnector", lambda **kwargs: object())
    with pytest.raises(SE.SourceImageError):
        run(SE.fetch_source_image(URL1))
    assert len(session.calls) == 1
    assert "headers" not in session.calls[0][1] or "Authorization" not in session.calls[0][1]["headers"]


def test_redirect_to_non_443_port_is_never_requested(monkeypatch):
    session = FakeSession([FakeResponse(
        302, b"", {"Location": "https://cdn.example.com:8443/private.png"})])
    monkeypatch.setattr(SE.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(SE.aiohttp, "TCPConnector", lambda **kwargs: object())
    with pytest.raises(SE.SourceImageError) as error:
        run(SE.fetch_source_image(URL1))
    assert error.value.code == "invalid_source_port"
    assert len(session.calls) == 1


def test_download_body_limit_is_enforced_without_content_length(monkeypatch):
    session = FakeSession([FakeResponse(200, b"x" * (SE.MAX_SOURCE_BYTES + 1), {"Content-Type": "image/png"})])
    monkeypatch.setattr(SE.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(SE.aiohttp, "TCPConnector", lambda **kwargs: object())
    with pytest.raises(SE.SourceImageError) as error:
        run(SE.fetch_source_image(URL1))
    assert error.value.code == "source_too_large"


@pytest.mark.parametrize("http_status,expected_status,uncertain", [
    (400, 400, False),
    (401, 401, False),
    (403, 403, False),
    (404, 404, False),
    (429, 429, True),
    (500, 502, True),
    (503, 502, True),
])
def test_discord_definitive_and_retryable_status_semantics(http_status, expected_status, uncertain):
    client = SE.DiscordAPI(application_id=APP, token="secret")
    client._session = FakeSession([FakeResponse(http_status, b'{"message":"response"}')])
    with pytest.raises(SE.DiscordAPIError) as error:
        run(client._request("DELETE", "/applications/test/emojis/1", mutating=True))
    assert error.value.status == expected_status
    assert error.value.http_status == http_status
    assert error.value.uncertain is uncertain
    assert len(client._session.calls) == 1


@pytest.mark.parametrize("response", [
    FakeResponse(500, b'{"message":"upstream"}'),
    FakeResponse(200, b"not-json"),
])
def test_discord_bad_create_responses_are_uncertain(response):
    client = SE.DiscordAPI(application_id=APP, token="secret")
    client._session = FakeSession([response])
    with pytest.raises(SE.DiscordAPIError) as error:
        run(client.create_emoji("shop_test", PNG1, BOT))
    assert error.value.uncertain is True


def test_discord_missing_usable_create_id_is_uncertain():
    client = SE.DiscordAPI(application_id=APP, token="secret")
    client._session = FakeSession([FakeResponse(200, b"{}")])
    with pytest.raises(SE.DiscordAPIError) as error:
        run(client.create_emoji("shop_test", PNG1, BOT))
    assert error.value.uncertain is True


@pytest.mark.parametrize("user", [None, {}, {"id": "12345"}])
def test_discord_create_response_requires_exact_bot_uploader(user):
    body = {"id": "7001", "name": "shop_test", "animated": False,
            "application_id": APP}
    if user is not None:
        body["user"] = user
    client = SE.DiscordAPI(application_id=APP, token="secret")
    client._session = FakeSession([FakeResponse(200, json.dumps(body).encode())])
    with pytest.raises(SE.DiscordAPIError) as error:
        run(client.create_emoji("shop_test", PNG1, BOT))
    assert error.value.uncertain is True


def test_non_ready_enrichment_is_empty_and_never_calls_discord(shop):
    content_hash = put_asset(shop, status="uncertain")

    async def load():
        async with SE.aiosqlite.connect(shop.path) as db:
            products = [{"id": 1, "icon_url": URL1}]
            await SE.attach_prepared_emojis(db, GUILD, products)
            return products

    assert run(load())[0]["app_emoji"] == ""
    assert shop.fake.create_calls == [] and shop.fake.delete_calls == []
    with sqlite3.connect(shop.path) as db:
        db.execute("UPDATE shop_emoji_assets SET status='ready',emoji_id='5001',attempt_id=NULL "
                   "WHERE application_id=? AND content_hash=?", (APP, content_hash))
    assert run(load())[0]["app_emoji"] == f"<:{SE.deterministic_emoji_name(content_hash)}:5001>"


def test_startup_reconciliation_is_scheduled_without_blocking(shop):
    content_hash = put_asset(shop)
    shop.fake.remote = [remote_emoji(content_hash, 6001)]

    async def scenario():
        task = SE.schedule_startup_reconciliation()
        assert task is not None and not task.done()
        await task
        return task

    run(scenario())
    assert asset_row(shop, content_hash)["emoji_id"] == "6001"
    assert shop.fake.create_calls == [] and shop.fake.delete_calls == []


def test_unsupported_surface_is_a_real_publication_blocker(monkeypatch):
    design = {"id": 1, "presentation": {"mode": "per_product", "content": "",
              "embeds": [{"title": "{{product.emoji}}", "description": "safe"}]},
              "products": [1], "action": {"kind": "buttons", "entries": [{"product_id": 1}]}}
    rows = {1: {"id": 1, "enabled": 1, "app_emoji": "<:shop_one:5001>"}}
    monkeypatch.setattr(pub, "_load_design_and_rows", lambda *args: (design, rows, "Saved"))
    monkeypatch.setattr(pub.SP, "validate_design", lambda *args: [])

    async def currency(_guild_id):
        return {}

    monkeypatch.setattr(pub, "get_currency_config", currency)
    monkeypatch.setattr(pub, "run_async", lambda coro: asyncio.run(coro))
    prepared = pub.prepare_design(GUILD, 1)
    assert prepared["payload"] is None
    assert [error["code"] for error in prepared["blocking_errors"]] == [
        "unsupported_product_emoji_surface"]
    assert prepared["product_emoji_references"] == []


@pytest.mark.parametrize("failure", [asyncio.TimeoutError, aiohttp.ClientError, OSError])
def test_discord_transport_create_and_delete_failures_are_uncertain(failure):
    class FailingRequest:
        async def __aenter__(self):
            raise failure("transport failed")

        async def __aexit__(self, *args):
            return None

    class FailingSession:
        def request(self, *args, **kwargs):
            return FailingRequest()

    client = SE.DiscordAPI(application_id=APP, token="secret")
    client._session = FailingSession()
    with pytest.raises(SE.DiscordAPIError) as error:
        run(client.create_emoji("shop_test", PNG1, BOT))
    assert error.value.uncertain is True
    client._session = FailingSession()
    with pytest.raises(SE.DiscordAPIError) as error:
        run(client._request("DELETE", "/applications/test/emojis/1", mutating=True))
    assert error.value.uncertain is True
    assert error.value.code == "discord_transport_error"
