import json
import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# TRADE SYSTEM (E3/E4-dependent — blocked until Dark verified both live
# on Railway; that verification landed this session, so this is the
# first build against them).
#
# Schema lives in this module's own ensure_trade_table(), NOT in
# database.py's init_db() — same pattern cogs/minigames.py already
# established for its three tables. Called from cogs/trade.py's
# cog_load(), same lifecycle Minigames uses.
#
# Currency legs reuse the SAME upsert shape utils/economy_safe.py uses
# (INSERT ... ON CONFLICT DO UPDATE) but inlined here rather than
# calling safe_credit/safe_deduct directly, because those each open
# their OWN connection/transaction — a trade needs both parties' both
# currencies and every offered item validated and moved inside ONE
# BEGIN IMMEDIATE, or a partial failure could leave one side paid and
# the other not. This is the same reasoning
# cogs/leveling.py::perform_leaderboard_reset() and
# utils/economy_safe.py::safe_convert() already document for their own
# multi-step atomic writes.
#
# Item legs are inlined for the same reason — utils/inventory.py's
# give_item()/remove_item() also each open their own connection.
#
# Ledger logging happens AFTER commit (same pattern as
# utils/economy_safe.py's _log_ledger): a trade that already landed in
# `economy`/`inventory_items` must never be undone by a ledger write
# failure — the ledger is an audit trail on top of the source of
# truth, not the source of truth itself.
# ═══════════════════════════════════════════════════════════════════════

MAX_ITEM_LINES_PER_SIDE = 10


class TradeError(Exception):
    pass


async def ensure_trade_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                user_a     INTEGER NOT NULL,
                user_b     INTEGER NOT NULL,
                offer_a    TEXT NOT NULL,
                offer_b    TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_th_guild
            ON trade_history(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_th_users
            ON trade_history(guild_id, user_a, user_b)
        """)
        await db.commit()


def _empty_offer() -> dict:
    return {"coins": 0, "diamonds": 0, "items": {}}


def _validate_offer(offer: dict):
    if offer.get("coins", 0) < 0 or offer.get("diamonds", 0) < 0:
        raise TradeError("Offered coins/diamonds cannot be negative.")
    items = offer.get("items", {})
    if len(items) > MAX_ITEM_LINES_PER_SIDE:
        raise TradeError(f"A trade can only carry up to {MAX_ITEM_LINES_PER_SIDE} distinct items per side.")
    for name, qty in items.items():
        if qty <= 0:
            raise TradeError(f"Quantity for {name} must be positive.")
    if not (offer.get("coins") or offer.get("diamonds") or items):
        raise TradeError("An offer can't be completely empty.")


async def _read_balances_and_items(db, guild_id: int, user_id: int,
                                    item_names: list[str]) -> tuple[int, int, dict]:
    cursor = await db.execute(
        "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
        (guild_id, user_id))
    row = await cursor.fetchone()
    balance = row[0] if row else 0
    diamonds = row[1] if row else 0

    items: dict[str, int] = {}
    if item_names:
        placeholders = ",".join("?" for _ in item_names)
        cursor = await db.execute(
            f"SELECT item_name, quantity FROM inventory_items "
            f"WHERE guild_id=? AND user_id=? AND item_name IN ({placeholders})",
            (guild_id, user_id, *item_names))
        for name, qty in await cursor.fetchall():
            items[name] = qty
    return balance, diamonds, items


async def _move_currency(db, guild_id: int, frm: int, to: int,
                          coins: int, diamonds: int):
    if coins:
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, -?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance - ?
        """, (guild_id, frm, coins, coins))
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance + ?
        """, (guild_id, to, coins, coins))
    if diamonds:
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, diamonds) VALUES (?, ?, -?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET diamonds = diamonds - ?
        """, (guild_id, frm, diamonds, diamonds))
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, diamonds) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET diamonds = diamonds + ?
        """, (guild_id, to, diamonds, diamonds))


async def _move_items(db, guild_id: int, frm: int, to: int, items: dict[str, int]):
    for name, qty in items.items():
        await db.execute("""
            UPDATE inventory_items SET quantity = quantity - ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id=? AND user_id=? AND item_name=?
        """, (qty, guild_id, frm, name))
        await db.execute("""
            INSERT INTO inventory_items
                (guild_id, user_id, item_name, item_type, quantity, source, updated_at)
            VALUES (?, ?, ?, 'trade', ?, 'trade', CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id, item_name) DO UPDATE SET
                quantity   = quantity + ?,
                updated_at = CURRENT_TIMESTAMP
        """, (guild_id, to, name, qty, qty))


async def execute_trade(guild_id: int, user_a: int, offer_a: dict,
                         user_b: int, offer_b: dict,
                         reason: str = "Player trade") -> dict:
    """
    offer_a / offer_b: {"coins": int, "diamonds": int, "items": {name: qty}}
    Everything in offer_a moves from user_a to user_b, and vice versa
    for offer_b — a simultaneous two-way swap. Re-validates both
    parties' live balances/inventory INSIDE the transaction (not just
    at UI-build time), since Discord button clicks can land minutes
    after either side last had those funds/items. Fully atomic: either
    every leg lands or none do.
    """
    _validate_offer(offer_a)
    _validate_offer(offer_b)
    if user_a == user_b:
        raise TradeError("Cannot trade with yourself.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            a_bal, a_gems, a_items = await _read_balances_and_items(
                db, guild_id, user_a, list(offer_a.get("items", {}).keys()))
            b_bal, b_gems, b_items = await _read_balances_and_items(
                db, guild_id, user_b, list(offer_b.get("items", {}).keys()))

            if a_bal < offer_a.get("coins", 0):
                await db.execute("ROLLBACK")
                return {"success": False, "error": f"<@{user_a}> no longer has enough coins."}
            if a_gems < offer_a.get("diamonds", 0):
                await db.execute("ROLLBACK")
                return {"success": False, "error": f"<@{user_a}> no longer has enough diamonds."}
            for name, qty in offer_a.get("items", {}).items():
                if a_items.get(name, 0) < qty:
                    await db.execute("ROLLBACK")
                    return {"success": False, "error": f"<@{user_a}> no longer has {qty}x {name}."}

            if b_bal < offer_b.get("coins", 0):
                await db.execute("ROLLBACK")
                return {"success": False, "error": f"<@{user_b}> no longer has enough coins."}
            if b_gems < offer_b.get("diamonds", 0):
                await db.execute("ROLLBACK")
                return {"success": False, "error": f"<@{user_b}> no longer has enough diamonds."}
            for name, qty in offer_b.get("items", {}).items():
                if b_items.get(name, 0) < qty:
                    await db.execute("ROLLBACK")
                    return {"success": False, "error": f"<@{user_b}> no longer has {qty}x {name}."}

            await _move_currency(db, guild_id, user_a, user_b,
                                  offer_a.get("coins", 0), offer_a.get("diamonds", 0))
            await _move_currency(db, guild_id, user_b, user_a,
                                  offer_b.get("coins", 0), offer_b.get("diamonds", 0))
            await _move_items(db, guild_id, user_a, user_b, offer_a.get("items", {}))
            await _move_items(db, guild_id, user_b, user_a, offer_b.get("items", {}))

            now = datetime.now(timezone.utc).isoformat()
            cursor = await db.execute("""
                INSERT INTO trade_history
                    (guild_id, user_a, user_b, offer_a, offer_b, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (guild_id, user_a, user_b,
                  json.dumps(offer_a), json.dumps(offer_b), now))
            trade_id = cursor.lastrowid

            # Post-move balances, read in the same transaction so the
            # ledger's balance_after is exact, not a racy second read
            # after commit.
            new_a_bal, new_a_gems, _ = await _read_balances_and_items(db, guild_id, user_a, [])
            new_b_bal, new_b_gems, _ = await _read_balances_and_items(db, guild_id, user_b, [])

            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

    from utils.ledger import log_transaction
    try:
        if offer_a.get("coins"):
            await log_transaction(guild_id, user_a, "balance", -offer_a["coins"], new_a_bal,
                                   type="transfer_out", reason=reason, source="trade", related_user_id=user_b)
            await log_transaction(guild_id, user_b, "balance", offer_a["coins"], new_b_bal,
                                   type="transfer_in", reason=reason, source="trade", related_user_id=user_a)
        if offer_a.get("diamonds"):
            await log_transaction(guild_id, user_a, "diamonds", -offer_a["diamonds"], new_a_gems,
                                   type="transfer_out", reason=reason, source="trade", related_user_id=user_b)
            await log_transaction(guild_id, user_b, "diamonds", offer_a["diamonds"], new_b_gems,
                                   type="transfer_in", reason=reason, source="trade", related_user_id=user_a)
        if offer_b.get("coins"):
            await log_transaction(guild_id, user_b, "balance", -offer_b["coins"], new_b_bal,
                                   type="transfer_out", reason=reason, source="trade", related_user_id=user_a)
            await log_transaction(guild_id, user_a, "balance", offer_b["coins"], new_a_bal,
                                   type="transfer_in", reason=reason, source="trade", related_user_id=user_b)
        if offer_b.get("diamonds"):
            await log_transaction(guild_id, user_b, "diamonds", -offer_b["diamonds"], new_b_gems,
                                   type="transfer_out", reason=reason, source="trade", related_user_id=user_a)
            await log_transaction(guild_id, user_a, "diamonds", offer_b["diamonds"], new_a_gems,
                                   type="transfer_in", reason=reason, source="trade", related_user_id=user_b)
    except Exception as e:
        print(f"[TRADE] Ledger logging failed for trade #{trade_id} in guild {guild_id}: {e}")

    return {"success": True, "trade_id": trade_id}


async def get_trade_history(guild_id: int, user_id: int = None, limit: int = 50) -> list[dict]:
    # Self-ensure the table before reading: this is called from the
    # DASHBOARD process (dashboard/api/trade.py, dashboard/api/backups.py),
    # which never loads the trade cog, so trade_history may not have been
    # created yet on a fresh install. A guild that has simply never traded
    # has an empty history, not a 500 — same "call ensure at the top"
    # pattern the creator/mission/minigames dashboard routes already use.
    await ensure_trade_table()
    query = """
        SELECT id, user_a, user_b, offer_a, offer_b, created_at
        FROM trade_history WHERE guild_id = ?
    """
    params: list = [guild_id]
    if user_id:
        query += " AND (user_a = ? OR user_b = ?)"
        params += [user_id, user_id]
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    return [{
        "id": r[0], "user_a": r[1], "user_b": r[2],
        "offer_a": json.loads(r[3]), "offer_b": json.loads(r[4]),
        "created_at": r[5],
    } for r in rows]
