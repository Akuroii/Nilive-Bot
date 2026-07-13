import aiosqlite
from database import DB_PATH

VALID_CURRENCIES = {"balance", "diamonds"}


class InsufficientBalance(Exception):
    pass


def _check_currency(currency: str) -> str:
    if currency not in VALID_CURRENCIES:
        raise ValueError(f"Unknown currency column: {currency!r}")
    return currency


async def _log_ledger(guild_id: int, user_id: int, currency: str,
                       amount: int, balance_after: int, type: str,
                       reason: str, source: str,
                       related_user_id: int = None):
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


# ── Phase 5 / Economy v2 ────────────────────────────────────────────────
# Converts coins into diamonds at a configurable coins-per-diamond rate.
# Atomic (BEGIN IMMEDIATE): the coin deduction and diamond credit either
# both land or neither does — same transactional guard pattern as
# safe_transfer above, just moving value between two currency columns on
# the same user instead of between two users on the same currency.
#
# Only the coins that convert cleanly are spent (coin_amount is floored
# to the nearest multiple of `rate`), so a user converting 1,250 coins at
# a 500:1 rate gets 2 diamonds and keeps the leftover 250 coins rather
# than losing them to rounding.
async def safe_convert(guild_id: int, user_id: int, coin_amount: int,
                        rate: int, reason: str = "Currency conversion",
                        source: str = "convert") -> dict:
    if coin_amount <= 0:
        raise ValueError("coin_amount must be positive")
    if rate <= 0:
        raise ValueError("rate must be positive")

    diamonds_gained = coin_amount // rate
    if diamonds_gained <= 0:
        raise ValueError(
            f"Need at least {rate} coins to convert 1 diamond "
            f"(current rate: {rate}:1)")

    coins_spent = diamonds_gained * rate

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < coins_spent:
                await db.execute("ROLLBACK")
                raise InsufficientBalance(f"Balance {balance} < {coins_spent}")

            await db.execute("""
                UPDATE economy SET balance = balance - ?
                WHERE guild_id=? AND user_id=?
            """, (coins_spent, guild_id, user_id))
            await db.execute("""
                INSERT INTO economy (guild_id, user_id, diamonds)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET diamonds = diamonds + ?
            """, (guild_id, user_id, diamonds_gained, diamonds_gained))

            new_balance_row = await (await db.execute(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))).fetchone()
            new_diamonds_row = await (await db.execute(
                "SELECT diamonds FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))).fetchone()
            await db.commit()
        except InsufficientBalance:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    new_balance  = new_balance_row[0] if new_balance_row else 0
    new_diamonds = new_diamonds_row[0] if new_diamonds_row else 0

    await _log_ledger(guild_id, user_id, "balance", -coins_spent,
                       new_balance, "convert_out",
                       f"{reason} ({rate}:1 rate)", source)
    await _log_ledger(guild_id, user_id, "diamonds", diamonds_gained,
                       new_diamonds, "convert_in",
                       f"{reason} ({rate}:1 rate)", source)

    return {
        "coins_spent": coins_spent,
        "diamonds_gained": diamonds_gained,
        "new_balance": new_balance,
        "new_diamonds": new_diamonds,
    }


async def get_guild_exchange_rate(guild_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT diamond_exchange_rate FROM guild_settings WHERE guild_id=?",
            (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else 500
