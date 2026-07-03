import aiosqlite
from database import DB_PATH


class InsufficientBalance(Exception):
    pass


async def safe_transfer(guild_id: int, from_user: int, to_user: int, amount: int):
    """Atomic balance transfer. Raises InsufficientBalance if sender can't cover it."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, from_user))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < amount:
                await db.execute("ROLLBACK")
                raise InsufficientBalance(f"Balance {balance} < {amount}")

            await db.execute("""
                INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, -?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance - ?
            """, (guild_id, from_user, amount, amount))
            await db.execute("""
                INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance + ?
            """, (guild_id, to_user, amount, amount))
            await db.commit()
        except InsufficientBalance:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def safe_deduct(guild_id: int, user_id: int, amount: int):
    """Atomic deduct-if-sufficient. Raises InsufficientBalance otherwise."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < amount:
                await db.execute("ROLLBACK")
                raise InsufficientBalance(f"Balance {balance} < {amount}")
            await db.execute("""
                UPDATE economy SET balance = balance - ? WHERE guild_id=? AND user_id=?
            """, (amount, guild_id, user_id))
            await db.commit()
        except InsufficientBalance:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def safe_credit(guild_id: int, user_id: int, amount: int):
    """Atomic credit, always succeeds."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance + ?
        """, (guild_id, user_id, amount, amount))
        await db.commit()


async def safe_decrement_stock(item_id: int) -> bool:
    """Atomic stock decrement. Returns False if out of stock."""
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
