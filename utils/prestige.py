"""
Finalized Prestige system.

This module is the SINGLE source of truth for Prestige state, the
permanent/effective tier calculation, the per-tier currency multipliers,
the Shop purchase path, and the cosmetic Discord-role sync. It replaces the
obsolete XP/level-gated "/prestige reset" mechanic that used to live in
utils/xp_calculator.py (which is intentionally no longer wired to any
command).

Design rules (locked with product):
  * Exactly 6 tiers: I → VI.
  * Permanent progression is I → V only, strictly sequential, no skipping.
  * A user has ONE permanent Prestige state (stored on levels.prestige,
    clamped to 0..5). It is never an Inventory item and is never duplicated.
  * I–V are purchased through the Shop using Coins. Purchasing resets the
    user's Coins balance to 0; Level, XP and Diamonds are untouched.
  * VI is a FREE Booster-only Shop activation, never written to levels.prestige.
    It has no stock, level, role or balance requirement. Expiry removes VI;
    a later boost requires a new explicit free activation. The permanent tier,
    all balances and inventory stay untouched. Activation is session-bound.
  * Discord roles are cosmetic/representational only and are NEVER the
    source of Prestige or its multiplier.

Currency naming: internally the project uses ``balance`` (coins) and
``diamonds`` columns; the display name comes from guild_settings.currency_name
via cogs/shop + cogs/economy. This module never hardcodes a display name.
"""
import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH

MAX_PERMANENT_TIER = 5   # Prestige V is the highest purchasable permanent tier
BOOSTER_TIER = 6         # Prestige VI (temporary Booster entitlement)
TIERS = (1, 2, 3, 4, 5, 6)

# Locked defaults, used to seed a guild's config on first read so a fresh
# install already has a complete, valid set of multipliers.
DEFAULT_TIER_MULTIPLIERS = {
    1: {"coins": 1.0, "diamonds": 1.0},
    2: {"coins": 1.0, "diamonds": 1.0},
    3: {"coins": 1.0, "diamonds": 1.0},
    4: {"coins": 1.0, "diamonds": 1.0},
    5: {"coins": 1.1, "diamonds": 1.0},
    6: {"coins": 1.1, "diamonds": 1.2},
}

_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}


class PrestigeError(Exception):
    """Raised on any invalid Prestige operation; message is user-facing."""


def tier_label(tier: int) -> str:
    return _ROMAN.get(int(tier), str(tier))


def is_booster(member) -> bool:
    """A member is a booster iff Discord reports premium_since (None = not)."""
    return bool(member is not None and getattr(member, "premium_since", None))


async def _resolve_member(bot, guild_id: int, user_id: int, *, raise_not_found=False):
    """Use the live guild cache, falling back to a Discord member fetch."""
    if bot is None:
        return None
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is None and hasattr(guild, "fetch_member"):
        try:
            member = await guild.fetch_member(user_id)
        except Exception as exc:
            if raise_not_found:
                from discord import NotFound
                if isinstance(exc, NotFound):
                    raise
            member = None
    return member


async def _resolve_booster(bot, guild_id: int, user_id: int) -> bool:
    return is_booster(await _resolve_member(bot, guild_id, user_id))


# ── Config ──────────────────────────────────────────────────────────────

async def _seed_tier_rows(db, guild_id: int) -> None:
    """Insert the default 6 rows for a guild that has none yet (lazy seed)."""
    for tier in TIERS:
        d = DEFAULT_TIER_MULTIPLIERS[tier]
        await db.execute(
            """
            INSERT OR IGNORE INTO prestige_tiers
                (guild_id, tier, coins_multiplier, diamonds_multiplier)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, tier, d["coins"], d["diamonds"]),
        )


async def get_prestige_config(guild_id: int) -> dict:
    """
    Returns {guild_id, enabled, tiers: {tier: {coins, diamonds}}}.
    Seeds the default tier rows once (only when a guild has no rows yet),
    so reads never write after the first time.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        enabled_row = await (
            await db.execute(
                "SELECT enabled FROM prestige_config WHERE guild_id = ?",
                (guild_id,),
            )
        ).fetchone()
        enabled = enabled_row[0] if enabled_row and enabled_row[0] is not None else 1

        count_row = await (
            await db.execute(
                "SELECT COUNT(*) FROM prestige_tiers WHERE guild_id = ?",
                (guild_id,),
            )
        ).fetchone()
        if (count_row[0] if count_row else 0) == 0:
            await _seed_tier_rows(db, guild_id)
            await db.commit()

        cursor = await db.execute(
            """
            SELECT tier, coins_multiplier, diamonds_multiplier
            FROM prestige_tiers WHERE guild_id = ? ORDER BY tier ASC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()

    tiers = {
        r[0]: {"coins": r[1], "diamonds": r[2]}
        for r in rows
    }
    return {"guild_id": guild_id, "enabled": enabled, "tiers": tiers}


async def set_prestige_multipliers(guild_id: int, enabled=None,
                                    tiers: dict | None = None) -> None:
    """
    Admin/dashboard write path. ``enabled`` toggles the whole system; tiers is
    a {tier: {coins, diamonds}} mapping. Both are optional so a single
    dashboard save can update either without clobbering the other.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if enabled is not None:
            await db.execute(
                """
                INSERT INTO prestige_config (guild_id, enabled, min_level)
                VALUES (?, ?, 0)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, 1 if enabled else 0),
            )
        if tiers:
            for tier, m in tiers.items():
                coins = float(m.get("coins", 1.0))
                diamonds = float(m.get("diamonds", 1.0))
                await db.execute(
                    """
                    INSERT INTO prestige_tiers
                        (guild_id, tier, coins_multiplier, diamonds_multiplier)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, tier) DO UPDATE SET
                        coins_multiplier = excluded.coins_multiplier,
                        diamonds_multiplier = excluded.diamonds_multiplier
                    """,
                    (guild_id, int(tier), coins, diamonds),
                )
        await db.commit()


# ── State ───────────────────────────────────────────────────────────────

async def get_permanent_prestige(guild_id: int, user_id: int) -> int:
    """
    The user's ONE permanent Prestige tier, 0 (none) .. V (5).
    levels.prestige values above 5 are NOT rewritten/destroyed — they are
    safely clamped to 5 for every read/eligibility decision so legacy data
    from the old unbounded reset mechanic degrades gracefully instead of
    producing an invalid state.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT prestige FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
    prestige = row[0] if row and row[0] else 0
    return int(min(max(prestige, 0), MAX_PERMANENT_TIER))


def _utc_timestamp(value):
    """SQLite legacy UTC timestamps and Discord aware datetimes, without guessing."""
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


async def has_vi_activation(guild_id: int, user_id: int) -> bool:
    """Stored-state inspection only; use get_effective_prestige for entitlement."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT 1 FROM prestige_vi_activations WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))).fetchone()
    return row is not None


async def _reconcile_vi_activation(db, guild_id, user_id, member, *, observed_at=None):
    # Missing context/fetch failure is NOT evidence of expiry. A bare boolean
    # cannot prove that this is the same boost session as the activation.
    if member is None or getattr(member, "id", None) != user_id:
        return False
    observed_at = observed_at or datetime.now(timezone.utc)
    row = await (await db.execute(
        "SELECT activated_at FROM prestige_vi_activations WHERE guild_id=? AND user_id=?",
        (guild_id, user_id))).fetchone()
    if row is None:
        return False
    # The cache may have advanced while SQLite was being awaited.
    guild = getattr(member, "guild", None)
    if guild is not None:
        if guild.id != guild_id:
            return False
        member = guild.get_member(user_id) or member
    if getattr(member, "id", None) != user_id or not hasattr(member, "premium_since"):
        return False
    started = _utc_timestamp(member.premium_since)
    now = datetime.now(timezone.utc)
    if member.premium_since is not None and (started is None or started > now):
        return False  # malformed/unverifiable context is not confirmed expiry
    activated = _utc_timestamp(row[0])
    if (started is not None and activated is not None
            and started <= activated <= now):
        return True
    # Preserve a cycle created after this observation began, even if it was
    # committed before SELECT. A later fresh lookup can reconcile it. Invalid
    # future timestamps are not legitimate cycles.
    if activated is not None and observed_at < activated <= now:
        return False
    # Compare-and-delete also protects replacement between SELECT and DELETE.
    await db.execute(
        "DELETE FROM prestige_vi_activations WHERE guild_id=? AND user_id=? AND activated_at IS ?",
        (guild_id, user_id, row[0]))
    return False


async def expire_vi_activation(guild_id, user_id, *, before):
    """Confirmed guild departure: revoke only activations predating the event."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT activated_at FROM prestige_vi_activations WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))).fetchone()
        if row is not None:
            stamp = _utc_timestamp(row[0])
            if stamp is None or stamp <= before:
                await db.execute(
                    "DELETE FROM prestige_vi_activations WHERE guild_id=? AND user_id=? AND activated_at IS ?",
                    (guild_id, user_id, row[0]))
                await db.commit()


async def get_effective_prestige(guild_id: int, user_id: int,
                                  is_booster=None, bot=None, *, member=None) -> int:
    """VI requires activation in the CURRENT boost session. Unknown context
    returns permanent state without destructive cleanup. Legacy boolean hints
    never establish session identity; callers should pass member or bot.
    Verified expiry/session change removes stale activation, not permanent tier.
    """
    observed_at = datetime.now(timezone.utc)
    permanent = await get_permanent_prestige(guild_id, user_id)
    if member is None:
        member = await _resolve_member(bot, guild_id, user_id)
    else:
        guild = getattr(member, "guild", None)
        if guild is not None:
            if guild.id != guild_id:
                return permanent
            current = guild.get_member(user_id)
            if current is None:
                # An event/interaction snapshot may predate a new session.
                # Without a cache entry, refresh rather than trusting it.
                try:
                    current = await guild.fetch_member(user_id)
                except Exception:
                    return permanent  # unknown: neither grant nor erase VI
            member = current
    async with aiosqlite.connect(DB_PATH) as db:
        active = await _reconcile_vi_activation(db, guild_id, user_id, member, observed_at=observed_at)
        await db.commit()
    return BOOSTER_TIER if active else permanent


async def get_effective_prestige_multipliers(guild_id: int, user_id: int,
                                              is_booster=None, bot=None, *, member=None) -> dict:
    """
    {coins, diamonds} for the user's effective Prestige tier. Returns 1.0/1.0
    when Prestige is disabled globally.
    """
    config = await get_prestige_config(guild_id)
    if not config.get("enabled", 1):
        return {"coins": 1.0, "diamonds": 1.0}
    tier = await get_effective_prestige(guild_id, user_id, is_booster, bot, member=member)
    tiers = config.get("tiers", {})
    t = tiers.get(tier) or DEFAULT_TIER_MULTIPLIERS.get(tier)
    if not t:
        return {"coins": 1.0, "diamonds": 1.0}
    return {
        "coins": float(t.get("coins", 1.0)),
        "diamonds": float(t.get("diamonds", 1.0)),
    }


async def get_prestige_earn_multiplier(guild_id: int, user_id: int,
                                        currency: str,
                                        is_booster=None, bot=None, *, member=None) -> float:
    """
    Multiplier for one currency at earn-time. currency is the project's
    internal column name: 'balance' → coins, 'diamonds' → diamonds.
    Anything unrecognised returns 1.0. Never changes deductions/transfers.
    """
    if currency == "balance":
        key = "coins"
    elif currency == "diamonds":
        key = "diamonds"
    else:
        return 1.0
    mults = await get_effective_prestige_multipliers(guild_id, user_id,
                                                     is_booster, bot, member=member)
    return float(mults.get(key, 1.0))


# ── Purchase (Shop I–V) ─────────────────────────────────────────────────

async def purchase_prestige(guild_id: int, user_id: int, target_tier: int,
                             min_coins: int, *, item_name: str = "Prestige",
                             item_id=None, display_name: str | None = None) -> dict:
    """
    Atomically purchase one permanent Prestige tier (I–V).

    Enforced on the backend (never trusted to the UI):
      * tier must be 1..5;
      * Prestige must be enabled;
      * tier must be exactly the next sequential tier (no skip, no re-buy);
      * the user must have at least ``min_coins`` (the shop item's coin price).

    On success: Coins balance is set to 0; Level/XP/Diamonds are untouched;
    a purchase_history + transaction_ledger row are written. Prestige is NOT
    written to inventory_items and is NOT delivered through the reward engine.

    Returns a dict describing the purchase; raises PrestigeError (user-facing
    message) on any validation failure.
    """
    target_tier = int(target_tier)
    if target_tier not in (1, 2, 3, 4, 5):
        raise PrestigeError("Invalid Prestige tier.")

    # Resolved BEFORE the write transaction opens: the display name comes
    # from the guild's currency config, and reading it on a second
    # connection while this one holds a write lock is avoidable.
    from utils.currency import get_currency_config, currency_name_for
    balance_name = currency_name_for(
        await get_currency_config(guild_id), "coins")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cfg_row = await (
                await db.execute(
                    "SELECT enabled FROM prestige_config WHERE guild_id = ?",
                    (guild_id,),
                )
            ).fetchone()
            enabled = cfg_row[0] if cfg_row and cfg_row[0] is not None else 1
            if not enabled:
                await db.execute("ROLLBACK")
                raise PrestigeError("Prestige is not enabled on this server.")

            level_row = await (
                await db.execute(
                    "SELECT prestige FROM levels WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            permanent = int(level_row[0] if level_row and level_row[0] else 0)
            permanent = int(min(max(permanent, 0), MAX_PERMANENT_TIER))

            if target_tier == permanent:
                await db.execute("ROLLBACK")
                raise PrestigeError(
                    f"You already own Prestige {tier_label(target_tier)}.")
            if permanent >= MAX_PERMANENT_TIER:
                await db.execute("ROLLBACK")
                raise PrestigeError(
                    f"You've reached the maximum permanent Prestige "
                    f"({tier_label(MAX_PERMANENT_TIER)}).")
            if target_tier != permanent + 1:
                next_tier = permanent + 1
                await db.execute("ROLLBACK")
                raise PrestigeError(
                    f"You must purchase Prestige {tier_label(next_tier)} next "
                    f"(you're at Prestige {tier_label(permanent)}).")

            bal_row = await (
                await db.execute(
                    "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            ).fetchone()
            balance = bal_row[0] if bal_row and bal_row[0] else 0
            if balance < min_coins:
                await db.execute("ROLLBACK")
                raise PrestigeError(
                    f"You need at least {min_coins:,} {balance_name} to "
                    f"purchase Prestige {tier_label(target_tier)} (you "
                    f"have {balance:,}).")

            old_balance = balance

            # Reset the Coins balance to 0. Level/XP/Diamonds untouched.
            await db.execute(
                "UPDATE economy SET balance = 0 WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )

            # Set the permanent Prestige tier. An existing levels row keeps
            # its xp/level (the ON CONFLICT only updates prestige).
            await db.execute(
                """
                INSERT INTO levels (guild_id, user_id, xp, level, prestige)
                VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    prestige = excluded.prestige
                """,
                (guild_id, user_id, target_tier),
            )

            # purchase_history.item_id is NOT NULL; default to 0 when the
            # caller didn't supply a real shop item id.
            if item_id is None:
                item_id = 0
            await db.execute(
                """
                INSERT INTO purchase_history
                    (guild_id, user_id, user_display_name,
                     item_id, item_name, price_paid, currency_paid)
                VALUES (?, ?, ?, ?, ?, ?, 'balance')
                """,
                (guild_id, user_id, display_name or "",
                 item_id, item_name, old_balance),
            )

            await db.commit()
        except PrestigeError:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise

    # Ledger is written after commit (the project's established pattern —
    # safe_credit/safe_deduct do the same), so we never log within an open
    # write transaction.
    try:
        from utils.ledger import log_transaction
        await log_transaction(
            guild_id, user_id, "balance", -old_balance, 0,
            type="deduct",
            reason=f"Prestige purchase: {tier_label(target_tier)}",
            source="shop",
        )
    except Exception as e:
        print(f"[PRESTIGE] Ledger write failed for purchase: {e}")

    return {
        "success": True,
        "old_tier": permanent,
        "new_tier": target_tier,
        "old_balance": old_balance,
        "new_balance": 0,
        "item_name": item_name,
    }


# ── Booster-only Prestige VI (Shop activation) ───────────────────────────

async def activate_booster_prestige(guild_id: int, user_id: int,
                                     min_coins: int, *,
                                     item_name: str = "Prestige VI",
                                     item_id=None,
                                     display_name: str | None = None,
                                     is_booster=None, bot=None) -> dict:
    """Free, unmetered VI activation for a verified current Booster.

    Legacy caller price/name/boolean arguments confer no authority. There are
    no balance, stock, level or role requirements. Only canonical listing and
    current boost identity matter. State + zero-value receipt commit together.
    No economy, stock, inventory or financial-ledger writes occur here.
    """
    from utils.shop_validation import (
        ShopValidationError, integer, text, prestige_terms,
    )

    try:
        guild_id = integer(guild_id, "Server", minimum=1)
        user_id = integer(user_id, "Member", minimum=1)
        item_id = integer(item_id, "Shop listing", minimum=1)
    except ShopValidationError as exc:
        raise PrestigeError(str(exc)) from exc

    # Resolve external Discord state before taking SQLite's write lock. A
    # caller-supplied boolean never authorizes a purchase, even with no bot.
    member = await _resolve_member(bot, guild_id, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            listing = await (await db.execute(
                "SELECT id,name,type,enabled,price,price_diamonds,prestige_tier "
                "FROM shop_items WHERE id=? AND guild_id=?",
                (item_id, guild_id),
            )).fetchone()
            if not listing or listing["enabled"] != 1:
                raise PrestigeError("Shop item not found or disabled on this server.")
            if listing["type"] != "prestige":
                raise PrestigeError("This listing is not a Prestige item.")
            item_name = text(listing["name"], "Shop item name", required=True)
            min_coins, tier = prestige_terms(
                listing["price"], listing["prestige_tier"], listing["price_diamonds"])
            if tier != BOOSTER_TIER:
                raise PrestigeError("This listing is not Prestige VI.")
            guild = bot.get_guild(guild_id) if bot is not None else None
            if guild is not None:
                member = guild.get_member(user_id) or member
            if not (member is not None and getattr(member, "id", None) == user_id
                    and getattr(member, "premium_since", None)):
                raise PrestigeError("Prestige VI is a Booster-only tier.")

            cfg = await (await db.execute(
                "SELECT enabled FROM prestige_config WHERE guild_id=?", (guild_id,),
            )).fetchone()
            if cfg is not None and cfg[0] == 0:
                raise PrestigeError("Prestige is not enabled on this server.")
            started = _utc_timestamp(member.premium_since)
            now = datetime.now(timezone.utc)
            if started is None or started > now:
                raise PrestigeError("Current Booster status could not be verified.")
            if await _reconcile_vi_activation(db, guild_id, user_id, member):
                raise PrestigeError("Prestige VI is already activated on your account.")
            await db.execute(
                "INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) VALUES (?,?,?)",
                (guild_id, user_id, now.isoformat(timespec="microseconds")))
            await db.execute(
                "INSERT INTO purchase_history (guild_id,user_id,user_display_name,"
                "item_id,item_name,price_paid,currency_paid) VALUES (?,?,?,?,?,?,'balance')",
                (guild_id, user_id, display_name or "", item_id, item_name, 0))
            await db.commit()
        except BaseException as exc:
            await db.rollback()
            if isinstance(exc, PrestigeError):
                raise
            if isinstance(exc, ShopValidationError):
                raise PrestigeError(str(exc)) from exc
            if isinstance(exc, Exception):
                import logging
                logging.getLogger(__name__).exception("VI purchase rolled back")
                raise PrestigeError("Purchase could not be completed. No coins were spent. Please try again.") from exc
            raise  # cancellation must also roll back, but must not be swallowed

    return {"success": True, "new_tier": BOOSTER_TIER,
            "price_paid": 0, "item_name": item_name}


# ── Cosmetic roles (representation only, never the source of truth) ──────

async def get_prestige_roles(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, tier, role_id FROM prestige_roles
            WHERE guild_id = ? ORDER BY tier ASC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
    return [{"id": r[0], "tier": r[1], "role_id": r[2]} for r in rows]


async def sync_prestige_roles(bot, guild, member, effective_tier=None) -> None:
    """
    Cosmetic only. Ensures the member wears exactly the configured role for
    their effective Prestige tier and no other configured prestige role.
    Never grants Prestige state or multipliers — those derive from the DB +
    Discord booster status (see get_effective_prestige).
    """
    if bot is None or guild is None or member is None:
        return
    if effective_tier is None:
        effective_tier = await get_effective_prestige(
            guild.id, member.id, member=member)

    roles = await get_prestige_roles(guild.id)
    role_by_tier = {r["tier"]: r["role_id"] for r in roles}
    desired_role_id = role_by_tier.get(int(effective_tier))

    from utils.permissions import check_bot_role_position

    # Remove any configured prestige role that no longer matches.
    for tier, role_id in role_by_tier.items():
        role = guild.get_role(int(role_id))
        if not role or role not in member.roles:
            continue
        if int(tier) != int(effective_tier):
            try:
                await member.remove_roles(role, reason="Prestige tier changed")
            except Exception as e:
                print(f"[PRESTIGE] Failed to remove tier {tier} role: {e}")

    # Grant the desired role if missing and allowed.
    if desired_role_id:
        role = guild.get_role(int(desired_role_id))
        if role and role not in member.roles:
            can_assign, warning = check_bot_role_position(guild, role)
            if can_assign:
                try:
                    await member.add_roles(role, reason="Prestige tier")
                except Exception as e:
                    print(f"[PRESTIGE] Failed to add tier role: {e}")
            else:
                print(f"[PRESTIGE ROLE WARNING] {warning}")
