import aiosqlite
import json
from database import DB_PATH

VALID_CURRENCIES = {"balance", "diamonds", "xp"}
# Phase 5 / Economy v2: added convert_in/convert_out for the coins→diamonds
# exchange (see utils/economy_safe.safe_convert). Same idea as
# transfer_in/transfer_out but between two different currency columns on
# the same user, not between two users.
VALID_TYPES = {"credit", "deduct", "transfer_in", "transfer_out",
               "convert_in", "convert_out", "reversal"}


class LedgerError(Exception):
    pass


async def log_transaction(guild_id: int, user_id: int, currency: str,
                           amount: int, balance_after: int | None,
                           type: str, reason: str = None,
                           source: str = "system",
                           related_user_id: int = None, *,
                           db: aiosqlite.Connection | None = None) -> int:
    """Write a ledger row; an optional caller-owned transaction is not committed.

    Existing callers retain the standalone connection/commit behavior.
    """
    if currency not in VALID_CURRENCIES:
        raise LedgerError(f"Unknown currency: {currency!r}")
    if type not in VALID_TYPES:
        raise LedgerError(f"Unknown transaction type: {type!r}")

    async def insert(connection):
        cursor = await connection.execute("""
            INSERT INTO transaction_ledger
                (guild_id, user_id, currency, amount, balance_after,
                 type, reason, source, related_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, currency, int(amount),
              balance_after, type, reason, source, related_user_id))
        return cursor.lastrowid

    if db is not None:
        return await insert(db)
    async with aiosqlite.connect(DB_PATH) as connection:
        row_id = await insert(connection)
        await connection.commit()
        return row_id


async def get_user_ledger(guild_id: int, user_id: int,
                           limit: int = 50, currency: str = None) -> list[dict]:
    query = """
        SELECT id, user_id, currency, amount, balance_after, type, reason,
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
        "id": r[0], "user_id": r[1], "currency": r[2], "amount": r[3],
        "balance_after": r[4], "type": r[5], "reason": r[6],
        "source": r[7], "related_user_id": r[8],
        "reversed": bool(r[9]), "reversed_at": r[10], "created_at": r[11],
    } for r in rows]


async def count_user_ledger(guild_id: int, user_id: int,
                             currency: str = None) -> int:
    """
    Total ledger rows for one member, for Wallet → Receipts pagination
    (knowing the page count up front is what lets the Forward button be
    disabled on the last page instead of paging into an empty list).
    """
    query = ("SELECT COUNT(*) FROM transaction_ledger "
             "WHERE guild_id = ? AND user_id = ?")
    params: list = [guild_id, user_id]
    if currency:
        query += " AND currency = ?"
        params.append(currency)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def get_user_ledger_page(guild_id: int, user_id: int,
                                currency: str = None,
                                offset: int = 0,
                                limit: int = 8) -> list[dict]:
    """
    One page of a member's transactions, newest first.

    Separate from get_user_ledger() rather than bolting an offset onto
    it: that function is already used by the dashboard with its own
    limit semantics, and adding a parameter with a default is exactly
    how a shared helper quietly changes behaviour for an existing
    caller. Ordering adds `id DESC` as a tiebreaker because created_at
    is a second-resolution CURRENT_TIMESTAMP — several rows from one
    action (a convert writes convert_out + convert_in) share the same
    timestamp, and without the tiebreaker SQLite's row order between
    them is unspecified, which can duplicate or skip a row across a
    page boundary.
    """
    query = """
        SELECT id, user_id, currency, amount, balance_after, type, reason,
               source, related_user_id, reversed, reversed_at, created_at
        FROM transaction_ledger
        WHERE guild_id = ? AND user_id = ?
    """
    params: list = [guild_id, user_id]
    if currency:
        query += " AND currency = ?"
        params.append(currency)
    query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([max(1, int(limit)), max(0, int(offset))])

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    return [{
        "id": r[0], "user_id": r[1], "currency": r[2], "amount": r[3],
        "balance_after": r[4], "type": r[5], "reason": r[6],
        "source": r[7], "related_user_id": r[8],
        "reversed": bool(r[9]), "reversed_at": r[10], "created_at": r[11],
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
    """Undo one ledger movement: wallet, marker and compensating row in ONE commit.

    The return contract is unchanged (`{"success": True, "new_balance": …}` or
    `{"success": False, "error": …}`), but two defects in the previous shape are
    gone:

    *It was not retry-safe.* The wallet moved through `safe_deduct()`/
    `safe_credit()` on their own connection, and the `reversed = 1` marker was
    written on a SECOND connection afterwards. A crash in between left a wallet
    that had already been charged and a ledger row that still looked reversible,
    so pressing the button again charged it a second time. Here the marker is the
    guard: `UPDATE … SET reversed = 1 WHERE id = ? AND reversed = 0` inside the
    same `BEGIN IMMEDIATE` transaction, so the second caller's rowcount is 0 and
    it gets "Already reversed" with nothing moved. Two processes (the dashboard
    and the bot share this SQLite file) cannot both win.

    *It counted one movement twice.* `safe_deduct()` appends its own `deduct` row
    and this function then appended a `reversal` row for the same event, so anyone
    summing `transaction_ledger` saw the reversal twice - and `_log_ledger()`
    swallows its exceptions, so the pair could also come out as one row or none.
    A reversal now writes exactly one compensating `reversal` row, which is the
    double-entry convention: the original stays, the inverse references it, and
    the two net to zero.

    The wallet is never forced negative. Reversing a credit needs the money to
    still be there; if it has been spent this returns the same "not enough
    balance" error as before and moves nothing, marker included - the row stays
    unreversed so the case is still visible rather than silently closed.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT user_id, currency, amount
                FROM transaction_ledger
                WHERE id = ? AND guild_id = ?
            """, (ledger_id, guild_id))
            row = await cursor.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return {"success": False, "error": "Ledger entry not found"}
            user_id, currency = row[0], row[1]
            amount = int(row[2] or 0)
            if currency not in ("balance", "diamonds"):
                await db.execute("ROLLBACK")
                return {"success": False,
                        "error": f"Reversal not supported for currency '{currency}'"}

            # Claim the reversal BEFORE moving any value: this is what makes a
            # retry, and a second process, a no-op instead of a second charge.
            cursor = await db.execute("""
                UPDATE transaction_ledger
                SET reversed = 1, reversed_at = CURRENT_TIMESTAMP, reversed_by = ?
                WHERE id = ? AND guild_id = ? AND reversed = 0
            """, (reversed_by, ledger_id, guild_id))
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return {"success": False, "error": "Already reversed"}

            movement = -amount        # the compensating amount, opposite in sign
            if movement > 0:
                # Giving money back can always be applied, and must work for a
                # member whose economy row does not exist yet.
                await db.execute(f"""
                    INSERT INTO economy (guild_id, user_id, {currency})
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, user_id)
                    DO UPDATE SET {currency} = {currency} + ?
                """, (guild_id, user_id, movement, movement))
            else:
                # Taking money back only while it is still there.
                cursor = await db.execute(
                    f"UPDATE economy SET {currency} = {currency} + ? "
                    f"WHERE guild_id=? AND user_id=? AND {currency} + ? >= 0",
                    (movement, guild_id, user_id, movement))
                if cursor.rowcount != 1:
                    await db.execute("ROLLBACK")
                    return {
                        "success": False,
                        "error": "User no longer has enough balance to reverse this"}
            cursor = await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            bal_row = await cursor.fetchone()
            new_balance = int(bal_row[0] or 0) if bal_row else 0

            # Exactly ONE row for this movement, written in the same transaction.
            await db.execute("""
                INSERT INTO transaction_ledger
                    (guild_id, user_id, currency, amount, balance_after,
                     type, reason, source)
                VALUES (?, ?, ?, ?, ?, 'reversal', ?, 'ledger')
            """, (guild_id, user_id, currency, movement, new_balance, reason))
            await db.commit()
            return {"success": True, "new_balance": new_balance}
        except Exception:
            await db.execute("ROLLBACK")
            raise
