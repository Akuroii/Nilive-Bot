"""Scratch-only helpers for the Phase 1 regressions (no production credentials).

Import this BEFORE database/cogs/dashboard. Each test executable owns a temp DB;
reset_database() recreates only that path. External boundaries are explicit fakes.
"""
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TEMP = tempfile.TemporaryDirectory(prefix="nilive_phase1_")
DB_FILE = Path(_TEMP.name) / "test.db"
os.environ.update(
    DATABASE_PATH=str(DB_FILE), NERO_ENVIRONMENT="production",
    OWNER_ID="999999999", SECRET_KEY="phase1-scratch-only-0123456789abcdef",
    DISCORD_TOKEN="x" * 20 + "." + "x" * 20 + "." + "x" * 20,
    DASHBOARD_DEMO_LOGIN="0",
)

from database import DB_PATH, init_db  # noqa: E402
assert Path(DB_PATH) == DB_FILE, "Refuse to run against a non-test database"
GUILD, USER = 8101, 8201


def execute(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as db:
        cursor = db.execute(sql, args)
        db.commit()
        return cursor.lastrowid


def rows(sql, args=()):
    with closing(sqlite3.connect(DB_PATH)) as db:
        return db.execute(sql, args).fetchall()


async def reset_database():
    assert DB_FILE.parent == Path(_TEMP.name)
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_FILE) + suffix).unlink(missing_ok=True)
    await init_db()


def seed_member(user=USER, guild=GUILD, balance=9000, tier=3):
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,2700055,84,?)", (guild, user, tier))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,?,138)", (guild, user, balance))


def seed_listing(price=None, tier=6, stock=2, guild=GUILD, enabled=1,
                 kind="prestige", name="Prestige VI", diamonds=None):
    if price is None:
        price = 0 if tier == 6 else 2500
    return execute(
        "INSERT INTO shop_items (guild_id,name,type,price,prestige_tier,"
        "max_stock,current_stock,enabled,price_diamonds) VALUES (?,?,?,?,?,?,?,?,?)",
        (guild, name, kind, price, tier, None if stock is None else max(1, stock),
         stock, enabled, diamonds))


def member(booster=False, user=USER):
    return SimpleNamespace(
        id=user, name="PhaseOne", display_name="Phase One", mention=f"<@{user}>",
        roles=[], display_avatar=SimpleNamespace(url=""),
        joined_at=datetime(2025, 4, 12, tzinfo=timezone.utc),
        premium_since=datetime(2026, 1, 1, tzinfo=timezone.utc) if booster else None,
    )


def bot_for(person, guild=GUILD):
    server = SimpleNamespace(id=guild, owner_id=99, get_role=lambda _id: None,
                             get_member=lambda uid: person if uid == person.id else None)
    return SimpleNamespace(get_guild=lambda gid: server if gid == guild else None)


def interaction(booster=False, user=USER, guild=GUILD):
    person = member(booster, user)
    bot = bot_for(person, guild)
    return SimpleNamespace(
        user=person, guild=bot.get_guild(guild), client=bot,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def mutation_snapshot():
    """Rejection must not partially change any of these stores."""
    return {table: rows(f"SELECT * FROM {table} ORDER BY rowid") for table in (
        "levels", "economy", "shop_items", "prestige_vi_activations",
        "purchase_history", "transaction_ledger", "inventory_items", "item_catalog",
    )}


def all_embed_text(embed):
    return "\n".join([embed.title or "", embed.description or ""] +
                     [f"{field.name}\n{field.value}" for field in embed.fields])
