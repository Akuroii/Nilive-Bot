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
  * VI is a TEMPORARY Booster entitlement. It is never written to
    levels.prestige. An active Discord Booster has effective Prestige VI
    regardless of permanent tier; when the boost ends effective Prestige
    returns to the permanent tier. Gaining/losing VI never touches
    Coins/XP/Level/Diamonds.
  * Discord roles are cosmetic/representational only and are NEVER the
    source of Prestige or its multiplier.

Currency naming: internally the project uses ``balance`` (coins) and
``diamonds`` columns; the display name comes from guild_settings.currency_name
via cogs/shop + cogs/economy. This module never hardcodes a display name.
"""
import aiosqlite
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


async def _resolve_booster(bot, guild_id: int, user_id: int) -> bool:
    """Fall back to resolving booster status from the bot's member cache."""
    if bot is None:
        return False
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(user_id)
    return is_booster(member)


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


async def get_effective_prestige(guild_id: int, user_id: int,
                                  is_booster=None, bot=None) -> int:
    """
    Effective Prestige VI for an active booster, otherwise the permanent tier.
    is_booster (bool) lets a caller that already has the Member avoid a
    guild/member lookup; when omitted, bot is used to resolve booster status.
    """
    permanent = await get_permanent_prestige(guild_id, user_id)
    if is_booster is None:
        is_booster = await _resolve_booster(bot, guild_id, user_id)
    if is_booster:
        return BOOSTER_TIER
    return permanent


async def get_effective_prestige_multipliers(guild_id: int, user_id: int,
                                              is_booster=None, bot=None) -> dict:
    """
    {coins, diamonds} for the user's effective Prestige tier. Returns 1.0/1.0
    when Prestige is disabled globally.
    """
    config = await get_prestige_config(guild_id)
    if not config.get("enabled", 1):
        return {"coins": 1.0, "diamonds": 1.0}
    tier = await get_effective_prestige(guild_id, user_id, is_booster, bot)
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
                                        is_booster=None, bot=None) -> float:
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
                                                     is_booster, bot)
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
                    f"You need at least {min_coins:,} Coins to purchase "
                    f"Prestige {tier_label(target_tier)} (you have "
                    f"{balance:,}).")

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
            guild.id, member.id, is_booster=is_booster(member))

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
