import aiosqlite
from database import DB_PATH

VALID_CURRENCIES = {"balance", "diamonds"}


class InsufficientBalance(Exception):
    pass


def _check_currency(currency: str) -> str:
    # Column names can't be parameterized as SQL placeholders, so this
    # whitelist check is what keeps the f-strings below safe — currency
    # is always an internal constant passed by calling code (never raw
    # user input), but the check stays here as a hard guarantee rather
    # than trusting every call site to only ever pass a literal.
    if currency not in VALID_CURRENCIES:
        raise ValueError(f"Unknown currency column: {currency!r}")
    return currency


async def _log_ledger(guild_id: int, user_id: int, currency: str,
                       amount: int, balance_after: int, type: str,
                       reason: str, source: str,
                       related_user_id: int = None):
    """
    Phase 3 / E3: best-effort ledger write. Every economy_safe op that
    mutates a balance calls this immediately after committing the
    balance change itself, so the ledger is always a record of what
    already happened — never a gate on whether it happens. A ledger
    write failure (e.g. transient disk issue) is logged and swallowed
    rather than raised, so a logging problem can never roll back or
    block a real economy transaction.
    """
    try:
        from utils.ledger import log_transaction
        await log_transaction(
            guild_id, user_id, currency, amount, balance_after,
            type=type, reason=reason, source=source,
            related_user_id=related_user_id)
    except Exception as e:
        print(f"[LEDGER] Failed to log transaction "
              f"(guild={guild_id} user={user_id} currency={currency}): {e}")


async def safe_transfer(guild_id: int, from_user: int, to_user: int,
                         amount: int, currency: str = "balance",
                         reason: str = "Transfer", source: str = "system"):
    """Atomic balance transfer. Raises InsufficientBalance if sender can't cover it."""
    currency = _check_currency(currency)
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, from_user))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < amount:
                await db.execute("ROLLBACK")
                raise InsufficientBalance(f"Balance {balance} < {amount}")

            await db.execute(f"""
                INSERT INTO economy (guild_id, user_id, {currency}) VALUES (?, ?, -?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET {currency} = {currency} - ?
            """, (guild_id, from_user, amount, amount))
            await db.execute(f"""
                INSERT INTO economy (guild_id, user_id, {currency}) VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET {currency} = {currency} + ?
            """, (guild_id, to_user, amount, amount))

            from_row = await (await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, from_user))).fetchone()
            to_row = await (await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, to_user))).fetchone()

            await db.commit()
        except InsufficientBalance:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    # Phase 3 / E3: log both legs of the transfer, cross-referenced
    # via related_user_id so the ledger can reconstruct the pair.
    await _log_ledger(guild_id, from_user, currency, -amount,
                       from_row[0] if from_row else None,
                       "transfer_out", reason, source,
                       related_user_id=to_user)
    await _log_ledger(guild_id, to_user, currency, amount,
                       to_row[0] if to_row else None,
                       "transfer_in", reason, source,
                       related_user_id=from_user)


async def safe_deduct(guild_id: int, user_id: int, amount: int,
                       currency: str = "balance",
                       reason: str = "Deduction", source: str = "system"):
    """Atomic deduct-if-sufficient. Raises InsufficientBalance otherwise."""
    currency = _check_currency(currency)
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < amount:
                await db.execute("ROLLBACK")
                raise InsufficientBalance(f"Balance {balance} < {amount}")
            await db.execute(f"""
                UPDATE economy SET {currency} = {currency} - ? WHERE guild_id=? AND user_id=?
            """, (amount, guild_id, user_id))
            new_row = await (await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))).fetchone()
            await db.commit()
        except InsufficientBalance:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    await _log_ledger(guild_id, user_id, currency, -amount,
                       new_row[0] if new_row else None,
                       "deduct", reason, source)


async def safe_credit(guild_id: int, user_id: int, amount: int,
                       currency: str = "balance",
                       reason: str = "Credit", source: str = "system"):
    """Atomic credit, always succeeds."""
    currency = _check_currency(currency)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
            INSERT INTO economy (guild_id, user_id, {currency}) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET {currency} = {currency} + ?
        """, (guild_id, user_id, amount, amount))
        new_row = await (await db.execute(
            f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))).fetchone()
        await db.commit()

    await _log_ledger(guild_id, user_id, currency, amount,
                       new_row[0] if new_row else None,
                       "credit", reason, source)


async def safe_admin_deduct(guild_id: int, user_id: int, amount: int,
                             currency: str = "balance",
                             reason: str = "Admin deduction",
                             source: str = "admin") -> int:
    """
    Admin-only deduct variant: clamps to zero instead of raising
    InsufficientBalance, matching the existing /removecoins behavior
    (an admin can always zero someone out, even below their current
    balance). Returns the new balance. Still atomic and still logged
    to the ledger like every other economy_safe op.
    """
    currency = _check_currency(currency)
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            old_balance = row[0] if row else 0
            await db.execute(f"""
                INSERT INTO economy (guild_id, user_id, {currency}) VALUES (?, ?, 0)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET {currency} = MAX(0, {currency} - ?)
            """, (guild_id, user_id, amount))
            new_row = await (await db.execute(
                f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))).fetchone()
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

    new_balance = new_row[0] if new_row else 0
    actually_removed = old_balance - new_balance
    await _log_ledger(guild_id, user_id, currency, -actually_removed,
                       new_balance, "deduct", reason, source)
    return new_balance


async def get_balance(guild_id: int, user_id: int,
                       currency: str = "balance") -> int:
    currency = _check_currency(currency)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {currency} FROM economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cursor.fetchone()
    return row[0] if row else 0


async def safe_decrement_stock(item_id: int) -> bool:
    """
    Atomic stock claim. Returns False if the item is out of stock
    (or doesn't exist). Unlimited-stock items (max_stock NULL/0)
    always succeed and are left untouched.

    Used by shop.process_purchase() to claim a unit of stock BEFORE
    attempting the balance deduction, so two simultaneous purchases
    of the last unit can't both succeed. If the balance deduction
    that follows fails, the caller is responsible for calling back
    to increment current_stock by 1 to release the claim.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT max_stock, current_stock FROM shop_items WHERE id=?",
                (item_id,))
            row = await cursor.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return False
            max_stock, curr_stock = row
            if max_stock and curr_stock is not None and curr_stock <= 0:
                await db.execute("ROLLBACK")
                return False
            if max_stock:
                await db.execute(
                    "UPDATE shop_items SET current_stock = current_stock - 1 WHERE id=?",
                    (item_id,))
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise
