import aiosqlite
import json
from database import DB_PATH

VALID_CURRENCIES = {"balance", "diamonds", "xp"}
VALID_TYPES = {"credit", "deduct", "transfer_in", "transfer_out", "reversal"}


class LedgerError(Exception):
    pass


async def log_transaction(guild_id: int, user_id: int, currency: str,
                           amount: int, balance_after: int | None,
                           type: str, reason: str = None,
                           source: str = "system",
                           related_user_id: int = None) -> int:
    if currency not in VALID_CURRENCIES:
        raise LedgerError(f"Unknown currency: {currency!r}")
    if type not in VALID_TYPES:
        raise LedgerError(f"Unknown transaction type: {type!r}")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO transaction_ledger
                (guild_id, user_id, currency, amount, balance_after,
                 type, reason, source, related_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, currency, int(amount),
              balance_after, type, reason, source, related_user_id))
        await db.commit()
        return cursor.lastrowid


async def get_user_ledger(guild_id: int, user_id: int,
                           limit: int = 50, currency: str = None) -> list[dict]:
    query = """
        SELECT id, currency, amount, balance_after, type, reason,
               source, related_user_id, reversed, reversed_at, created_at
        FROM transaction_ledger
        WHERE guild_id = ? AND user_id = ?
    """
    params: list = [guild_id, user_id]
    if currency:
        query += " AND currency = ?"
        params.append(currency)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    return [{
        "id": r[0], "currency": r[1], "amount": r[2],
        "balance_after": r[3], "type": r[4], "reason": r[5],
        "source": r[6], "related_user_id": r[7],
        "reversed": bool(r[8]), "reversed_at": r[9], "created_at": r[10],
    } for r in rows]


async def get_guild_ledger(guild_id: int, limit: int = 100,
                            currency: str = None,
                            source: str = None) -> list[dict]:
    query = """
        SELECT id, user_id, currency, amount, balance_after, type,
               reason, source, related_user_id, reversed,
               reversed_at, created_at
        FROM transaction_ledger
        WHERE guild_id = ?
    """
    params: list = [guild_id]
    if currency:
        query += " AND currency = ?"
        params.append(currency)
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    return [{
        "id": r[0], "user_id": r[1], "currency": r[2], "amount": r[3],
        "balance_after": r[4], "type": r[5], "reason": r[6],
        "source": r[7], "related_user_id": r[8],
        "reversed": bool(r[9]), "reversed_at": r[10], "created_at": r[11],
    } for r in rows]


async def reverse_transaction(ledger_id: int, guild_id: int,
                               reversed_by: int = None,
                               reason: str = "Reversed via dashboard") -> dict:
    from utils.economy_safe import safe_credit, safe_deduct, InsufficientBalance

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, currency, amount, reversed
            FROM transaction_ledger
            WHERE id = ? AND guild_id = ?
        """, (ledger_id, guild_id))
        row = await cursor.fetchone()

    if not row:
        return {"success": False, "error": "Ledger entry not found"}

    user_id, currency, amount, already_reversed = row
    if already_reversed:
        return {"success": False, "error": "Already reversed"}
    if currency not in ("balance", "diamonds"):
        return {"success": False,
                "error": f"Reversal not supported for currency '{currency}'"}

    try:
        if amount >= 0:
            await safe_deduct(guild_id, user_id, amount, currency=currency)
        else:
            await safe_credit(guild_id, user_id, -amount, currency=currency)
    except InsufficientBalance:
        return {"success": False,
                "error": "User no longer has enough balance to reverse this"}

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        bal_row = await cursor.fetchone()
        new_balance = bal_row[0] if bal_row else 0

        await db.execute("""
            UPDATE transaction_ledger
            SET reversed = 1, reversed_at = CURRENT_TIMESTAMP, reversed_by = ?
            WHERE id = ?
        """, (reversed_by, ledger_id))
        await db.commit()

    await log_transaction(
        guild_id, user_id, currency, -amount, new_balance,
        type="reversal", reason=reason, source="ledger",
    )

    return {"success": True, "new_balance": new_balance}
