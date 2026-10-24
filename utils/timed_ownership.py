"""
TIMED OWNERSHIP — separately identifiable temporary holdings.

Why this module exists
----------------------
`inventory_items` has UNIQUE(guild_id, user_id, item_name) and every write path
(`give_item`, `set_quantity`) upserts on that key, overwriting `item_type` and
`metadata`. That model cannot represent what the Shop now sells:

  * a permanent "Golden Key" AND an 8-hour "Golden Key" at the same time,
  * several timed copies of one name with different expiry times.

Re-keying `inventory_items` would mean rewriting trade, reward provenance
(`reward_grants`/`reward_reversals`) and the level-test-reset reversal guards,
all of which deliberately rely on "one row per name" and on `rowcount == 1`
checks to fail closed. That trade is not worth it, so timed holdings live in
their own table and `utils.inventory.get_inventory()` presents a unified view.

Design rules enforced here
--------------------------
1. ADDITIVE ONLY. No existing row is migrated, rewritten or reclassified.
2. The lifetime of a timed holding starts when it is CREATED, and callers create
   it only after delivery succeeded — never at publication or purchase attempt.
3. `expires_at` is stored as ISO-8601 UTC with an explicit offset, the same
   representation `temp_roles`, `leveling_active_boosts` and
   `temp_roles.expires_at` already use. Comparisons parse, never string-compare.
4. Read paths filter expired rows, so `/inventory` and `/rank` never show an
   expired holding even if the periodic cleanup has not run yet.
5. Cleanup is retry-safe: a row whose Discord role still has to be removed is
   NOT deleted (that row is the retry marker). Nothing else is used to remember
   outstanding work.
6. A timed expiry never revokes an independently held entitlement to the same
   Discord role — see `role_still_entitled()`.

Category identity
-----------------
A Color is a Shop product category, not a reward source. Purchases carry the
product id directly. Reward paths that only know a raw `role_id` resolve the
category through `resolve_color_category()`: exactly one enabled `type='color'`
listing for that role in that guild means "Color"; zero or several listings
means "cannot establish it", and the grant stays an ordinary role item. The
category is stored on the ownership record so it survives the listing being
edited or deleted later.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import DB_PATH

# ── Category identity ───────────────────────────────────────────────────

CATEGORY_COLOR = "color"

# The public Shop type whose grants are Colors. Temporary vs permanent is
# derived from the product's duration, never from a second type.
SHOP_TYPE_COLOR = "color"
ROLE_ITEM_TYPES = ("role", "temp_role")


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS timed_holdings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        item_name  TEXT NOT NULL,
        item_type  TEXT NOT NULL,
        quantity   INTEGER NOT NULL DEFAULT 1,
        metadata   TEXT,
        source     TEXT DEFAULT 'system',
        product_id INTEGER,
        category   TEXT,
        role_id    INTEGER,
        expires_at TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_timed_holdings_user
    ON timed_holdings(guild_id, user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_timed_holdings_expiry
    ON timed_holdings(expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_timed_holdings_role
    ON timed_holdings(guild_id, role_id)
    """,
)


async def ensure_timed_ownership_schema(db) -> None:
    """Create the timed-holding table and indexes. Additive and idempotent."""
    for statement in SCHEMA_STATEMENTS:
        await db.execute(statement)
    await db.commit()


# ── Time handling (one place, so formats cannot drift) ──────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    """Canonical stored form: ISO-8601 UTC with an explicit offset."""
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def parse_time(value) -> datetime | None:
    """Tolerant ISO parser: trailing Z, naive-as-UTC, space separator.

    Returns None for anything unparseable. Never raises — a bad stored value
    must not break a panel, a cleanup tick or a rank render.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            text = str(value).strip().replace(" ", "T", 1)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def expiry_from_hours(duration_hours) -> str | None:
    """`now + duration_hours` as the canonical stored string.

    None/0/negative/invalid means "permanent" — the caller must then use the
    existing permanent ownership path instead of this table.
    """
    try:
        hours = float(duration_hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return iso(utcnow() + timedelta(hours=hours))


def is_expired(expires_at, *, now: datetime | None = None) -> bool:
    """True when `expires_at` has passed. Unparseable counts as expired.

    Unparseable-means-expired is deliberate: a corrupt row must not become an
    eternal entitlement. It is also invisible to the UI, so nothing is lost by
    keeping it out of the member's view while cleanup removes it.
    """
    parsed = parse_time(expires_at)
    if parsed is None:
        return True
    return parsed <= (now or utcnow())


def format_remaining(expires_at, *, now: datetime | None = None) -> str | None:
    """Readable remaining lifetime: `3d 5h`, `5h 20m`, `12m`.

    None when the value is absent, invalid or already expired — the caller then
    shows no countdown at all rather than a negative or invented one.
    """
    parsed = parse_time(expires_at)
    if parsed is None:
        return None
    remaining = int((parsed - (now or utcnow())).total_seconds())
    if remaining <= 0:
        return None
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"


# ── Grant / read ────────────────────────────────────────────────────────

def _row_to_holding(row, columns) -> dict:
    data = dict(zip(columns, row))
    try:
        data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else None
    except (TypeError, ValueError):
        data["metadata"] = None
    data["entry_key"] = f"h:{data['id']}"
    data["timed"] = True
    return data


_HOLDING_COLUMNS = ("id", "guild_id", "user_id", "item_name", "item_type",
                    "quantity", "metadata", "source", "product_id", "category",
                    "role_id", "expires_at", "created_at")


async def grant_timed_holding(guild_id: int, user_id: int, item_name: str,
                              *, expires_at: str, item_type: str = "custom",
                              quantity: int = 1, metadata: dict | None = None,
                              source: str = "system",
                              product_id: int | None = None,
                              category: str | None = None,
                              role_id: int | None = None,
                              db: aiosqlite.Connection | None = None) -> int:
    """Insert one timed holding. Call ONLY after delivery has succeeded.

    Each call creates its own record, even for an identical name: two grants
    with different expiry times are two independent holdings, and merging them
    would let one expiry destroy the other's remaining time.

    An optional caller-owned connection is joined, not committed — the same
    contract as `utils.inventory.give_item(..., db=)`.
    """
    if not expires_at:
        raise ValueError("grant_timed_holding requires an explicit expires_at")
    if int(quantity) <= 0:
        raise ValueError("quantity must be positive")
    payload = json.dumps(metadata) if metadata else None
    role_value = role_id
    if role_value is None and metadata:
        try:
            role_value = int(metadata.get("role_id")) if metadata.get("role_id") else None
        except (TypeError, ValueError):
            role_value = None

    async def apply(connection):
        cursor = await connection.execute(
            """
            INSERT INTO timed_holdings
                (guild_id, user_id, item_name, item_type, quantity, metadata,
                 source, product_id, category, role_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, item_name, item_type, int(quantity), payload,
             source, product_id, category, role_value, expires_at))
        return cursor.lastrowid

    if db is not None:
        return await apply(db)

    async with aiosqlite.connect(DB_PATH) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            new_id = await apply(connection)
            await connection.commit()
            return new_id
        except Exception:
            await connection.execute("ROLLBACK")
            raise


async def get_timed_holdings(guild_id: int, user_id: int, *,
                             include_expired: bool = False,
                             category: str | None = None) -> list[dict]:
    """This member's timed holdings, expired ones excluded by default."""
    sql = (f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings "
           "WHERE guild_id=? AND user_id=?")
    params: list = [guild_id, user_id]
    if category is not None:
        sql += " AND category IS ?"
        params.append(category)
    sql += " ORDER BY id ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    holdings = [_row_to_holding(row, _HOLDING_COLUMNS) for row in rows]
    epoch_min = datetime.min.replace(tzinfo=timezone.utc)
    holdings.sort(
        key=lambda h: (parse_time(h["expires_at"]) or epoch_min, h["id"]))
    if include_expired:
        return holdings
    now = utcnow()
    return [h for h in holdings if not is_expired(h["expires_at"], now=now)]


async def get_timed_holding(entry_key: str) -> dict | None:
    """One holding by its stable UI key (`h:<id>`). None if absent/invalid."""
    try:
        holding_id = int(str(entry_key).split(":", 1)[1])
    except (IndexError, ValueError, AttributeError):
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings "
            "WHERE id=?", (holding_id,))
        row = await cursor.fetchone()
    return _row_to_holding(row, _HOLDING_COLUMNS) if row else None


async def delete_timed_holding(entry_key_or_id) -> bool:
    """Delete exactly one holding. Returns True when a row was removed."""
    raw = str(entry_key_or_id)
    try:
        holding_id = int(raw.split(":", 1)[1]) if ":" in raw else int(raw)
    except (IndexError, ValueError):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM timed_holdings WHERE id=?",
                                  (holding_id,))
        await db.commit()
        return cursor.rowcount == 1


# ── Category resolution (Color identity) ────────────────────────────────

async def resolve_color_category(guild_id: int, role_id) -> dict | None:
    """The Color product behind a raw role_id, when it can be established.

    Returns {"product_id": ..., "item_name": ...} for EXACTLY ONE enabled
    `type='color'` listing bound to that role in this guild.

    Several listings for one role share the category, but which variant was
    actually awarded is unknowable from a bare role_id — and inheriting one
    variant's expiry would be a guess. So an ambiguous role still resolves its
    CATEGORY (category is a property of the role's listings) while the product
    id is left as None. No listing at all means "not a Color".
    """
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name FROM shop_items "
            "WHERE guild_id=? AND type=? AND role_id=? AND enabled=1 "
            "ORDER BY id ASC",
            (guild_id, SHOP_TYPE_COLOR, rid))
        rows = await cursor.fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return {"product_id": rows[0][0], "item_name": rows[0][1]}
    return {"product_id": None, "item_name": None}


async def color_metadata(guild_id: int, role_id, metadata: dict | None) -> dict:
    """Metadata enriched with stable Color identity, when the role is a Color.

    Called by reward delivery so the ownership record keeps its category even
    after the listing is renamed or deleted. An unrelated role reward is
    returned unchanged — it never becomes a Color by accident.
    """
    meta = dict(metadata or {})
    if meta.get("category") == CATEGORY_COLOR:
        if meta.get("product_id") is None:
            resolved = await resolve_color_category(guild_id, role_id)
            if resolved and resolved.get("product_id") is not None:
                meta["product_id"] = resolved["product_id"]
        return meta
    resolved = await resolve_color_category(guild_id, role_id)
    if not resolved:
        return meta
    meta["category"] = CATEGORY_COLOR
    if resolved.get("product_id") is not None:
        meta["product_id"] = resolved["product_id"]
    return meta


def category_of(item: dict) -> str | None:
    """Category marker for one unified inventory entry (metadata wins)."""
    direct = item.get("category")
    if direct:
        return direct
    meta = item.get("metadata") or {}
    value = meta.get("category")
    return value if isinstance(value, str) and value else None


def is_color(item: dict) -> bool:
    return category_of(item) == CATEGORY_COLOR


def role_id_of(item: dict) -> int | None:
    """The Discord role an entry points at, if any."""
    explicit = item.get("role_id")
    if explicit not in (None, ""):
        try:
            return int(explicit)
        except (TypeError, ValueError):
            return None
    meta = item.get("metadata") or {}
    raw = meta.get("role_id")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ── Entitlements: may this role be removed? ─────────────────────────────


class EntitlementCheckError(RuntimeError):
    """Raised when a role entitlement source cannot be verified reliably."""


def _resolve_member_from_bot(bot, guild_id: int, user_id: int, member=None):
    if member is not None:
        return member
    if bot is None:
        return None
    guild = bot.get_guild(int(guild_id)) if hasattr(bot, "get_guild") else None
    if guild is None:
        return None
    return guild.get_member(int(user_id)) if hasattr(guild, "get_member") else None


def _booster_level_for(member) -> tuple[int, bool]:
    """Return `(boost_level, count_known)` for `member`.

    In discord.py, `discord.Member` exposes `premium_since` (whether the member
    has an active server boost), while `premium_subscription_count` is a
    `discord.Guild` attribute (total boosts across the whole server) rather
    than a per-member attribute. We must NEVER infer a member's boost count
    from `Guild.premium_subscription_count`.

    * If `member` is `None` or `member.premium_since` is falsy: returns
      `(0, True)` — the member is definitively not an active booster.
    * If `member` carries an explicit per-member `premium_subscription_count`
      attribute (for example from a test double or caller-supplied wrapper):
      returns `(max(1, int(raw_count)), True)`.
    * Otherwise (a live `discord.Member` with `premium_since` set and no
      per-member count attribute): returns `(1, False)` — the member is
      verified to have at least 1 active boost, while 2+ boost count cannot be
      determined from `discord.Member` alone.
    """
    if member is None or not getattr(member, "premium_since", None):
        return 0, True
    raw_count = getattr(member, "premium_subscription_count", None)
    if raw_count is not None:
        try:
            return max(1, int(raw_count or 1)), True
        except (TypeError, ValueError):
            pass
    return 1, False


async def member_managed_role_ids(guild_id: int, user_id: int, *,
                                  bot=None, member=None) -> set[int]:
    """Roles a guild-level subsystem currently entitles THIS member to wear.

    Configuration alone does not prove entitlement:
      * Prestige tier roles match only the member's effective tier (> 0) when
        Prestige is enabled.
      * Booster reward roles (`boost1_role_id`, `boost2_role_id`) require an
        active boost (`premium_since`) and sufficient boost count when the
        Boost system is enabled. When `discord.Member` does not expose a
        per-member boost count (`count_known=False`), an active booster already
        wearing `boost2_role_id` or a level-2+ `boost_color_roles` role on
        Discord retains that role rather than having it stripped on expiry/swap.
      * Self-picked Booster colour roles (`boost_color_roles`) require an
        active boost with `boost_level >= requires_boost_level` (or an active
        booster already wearing the role when per-member count is unavailable).

    Raises `EntitlementCheckError` if any required backing table/query fails so
    callers never mistake a failed query for "no entitlement exists".
    """
    managed: set[int] = set()
    member = _resolve_member_from_bot(bot, guild_id, user_id, member)

    try:
        from utils.prestige import get_effective_prestige
        prestige_enabled = True
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT enabled FROM prestige_config WHERE guild_id=?",
                (guild_id,))).fetchone()
            if row is not None and row[0] is not None and int(row[0]) == 0:
                prestige_enabled = False
        if prestige_enabled:
            effective_tier = await get_effective_prestige(
                guild_id, user_id, bot=bot, member=member)
            if int(effective_tier or 0) > 0:
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(
                        "SELECT role_id FROM prestige_roles WHERE guild_id=? AND tier=?",
                        (guild_id, int(effective_tier)))
                    for (r_id,) in await cursor.fetchall():
                        if r_id not in (None, ""):
                            try:
                                managed.add(int(r_id))
                            except (TypeError, ValueError):
                                continue
    except EntitlementCheckError:
        raise
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify Prestige role entitlement: {exc}"
        ) from exc

    boost_level, boost_count_known = _booster_level_for(member)
    if boost_level >= 1:
        held_role_ids: set[int] = set()
        for r_obj in (getattr(member, "roles", None) or ()):
            r_id_attr = getattr(r_obj, "id", r_obj)
            try:
                held_role_ids.add(int(r_id_attr))
            except (TypeError, ValueError):
                continue
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                boost_enabled = True
                cursor = await db.execute(
                    "SELECT enabled, boost1_role_id, boost2_role_id "
                    "FROM boost_config WHERE guild_id=?",
                    (guild_id,))
                row = await cursor.fetchone()
                if row:
                    enabled, b1, b2 = row
                    if enabled is not None and int(enabled) == 0:
                        boost_enabled = False
                    else:
                        if b1 not in (None, "") and boost_level >= 1:
                            try:
                                managed.add(int(b1))
                            except (TypeError, ValueError):
                                pass
                        if b2 not in (None, ""):
                            try:
                                b2_id = int(b2)
                                if boost_level >= 2 or (
                                    not boost_count_known
                                    and b2_id in held_role_ids
                                ):
                                    managed.add(b2_id)
                            except (TypeError, ValueError):
                                pass

                if boost_enabled:
                    cursor = await db.execute(
                        "SELECT role_id, requires_boost_level "
                        "FROM boost_color_roles WHERE guild_id=?",
                        (guild_id,))
                    for r_id, req_lvl in await cursor.fetchall():
                        if r_id in (None, ""):
                            continue
                        try:
                            color_role_id = int(r_id)
                        except (TypeError, ValueError):
                            continue
                        needed = max(1, int(req_lvl or 1))
                        if boost_level >= needed or (
                            not boost_count_known
                            and color_role_id in held_role_ids
                        ):
                            managed.add(color_role_id)
        except EntitlementCheckError:
            raise
        except Exception as exc:
            raise EntitlementCheckError(
                f"Failed to verify Booster role entitlement: {exc}"
            ) from exc

    return managed


async def guild_managed_role_ids(guild_id: int,
                                 user_id: int | None = None, *,
                                 bot=None, member=None) -> set[int]:
    """Per-member managed roles (Prestige / Booster) for the given member.

    Guild-wide configuration alone never proves a member is entitled to a role;
    if no member identity is supplied, returns an empty set.
    """
    uid = user_id if user_id is not None else getattr(member, "id", None)
    if uid is None:
        return set()
    return await member_managed_role_ids(
        guild_id, int(uid), bot=bot, member=member)


async def level_claim_role_entitled(guild_id: int, user_id: int,
                                    role_id: int, *,
                                    bot=None, member=None) -> str | None:
    """Check if a fulfilled permanent Level role claim entitles this member to `role_id`.

    Raises `EntitlementCheckError` if a backing table/query cannot be read so
    callers never treat a failed query as absence of entitlement.
    """
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return None
    member = _resolve_member_from_bot(bot, guild_id, user_id, member)
    from utils.level_claims import TRACK_ROLE, is_booster_ref, member_is_booster
    from utils import level_ops as ops
    booster_ok = member_is_booster(member)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, reward_level, reward_ref, payload_json, status "
                "FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=? AND track=?",
                (guild_id, user_id, TRACK_ROLE))
            claim_rows = await cursor.fetchall()
            if not claim_rows:
                return None

            reversed_role_claims: set[int] = set()
            open_role_claims: set[int] = set()
            cursor = await db.execute(
                "SELECT claim_id, status FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? AND kind=?",
                (guild_id, user_id, TRACK_ROLE))
            for c_id, st_raw in await cursor.fetchall():
                if c_id is None:
                    continue
                cid = int(c_id)
                st = str(st_raw or "")
                if st in (ops.STATUS_APPLIED, ops.STATUS_ALREADY):
                    reversed_role_claims.add(cid)
                elif not st.startswith("resolved:"):
                    open_role_claims.add(cid)

            exclusive_roles = False
            cursor = await db.execute(
                "SELECT remove_old_reward_role FROM leveling_config WHERE guild_id=?",
                (guild_id,))
            cfg_row = await cursor.fetchone()
            if cfg_row and cfg_row[0]:
                exclusive_roles = True
    except EntitlementCheckError:
        raise
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify Level role claim entitlement: {exc}"
        ) from exc

    partitions: dict[bool, dict[int, dict]] = {False: {}, True: {}}
    for claim_id, claimed_level, ref, payload, status in claim_rows:
        cid = int(claim_id)
        if cid in reversed_role_claims:
            continue
        audience = is_booster_ref(ref)
        if audience and not booster_ok:
            continue
        lvl = int(claimed_level)
        group = partitions[audience].setdefault(
            lvl, {"role_ids": set(), "unfulfilled": False, "claim_ids_by_role": {}})
        if status != "fulfilled":
            group["unfulfilled"] = True
            continue
        try:
            claimed_role = int((json.loads(payload) or {}).get("role_id"))
        except (TypeError, ValueError):
            group["unfulfilled"] = True
            continue
        group["role_ids"].add(claimed_role)
        group["claim_ids_by_role"].setdefault(claimed_role, set()).add(cid)

    for audience in (False, True):
        by_level = partitions[audience]
        if not by_level:
            continue
        delivered_levels = [lvl for lvl, grp in by_level.items() if grp["role_ids"]]
        if not delivered_levels:
            continue
        highest = max(delivered_levels)
        highest_group = by_level[highest]
        if exclusive_roles and not highest_group["unfulfilled"]:
            active_groups = [(highest, highest_group)]
        else:
            active_groups = sorted(by_level.items())
        for _lvl, grp in active_groups:
            if rid not in grp["role_ids"]:
                continue
            claimants = grp["claim_ids_by_role"].get(rid, set())
            valid = claimants - open_role_claims
            if valid:
                label = "booster level role claim" if audience else "level role claim"
                return f"fulfilled {label} ({min(valid)})"
    return None


async def non_slot_role_entitlement(guild_id: int, user_id: int, role_id: int, *,
                                    bot=None, member=None) -> str | None:
    """Entitlements outside the single equip slot (Level claims, Prestige, Booster)."""
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return None
    level_reason = await level_claim_role_entitled(
        guild_id, user_id, rid, bot=bot, member=member)
    if level_reason:
        return level_reason
    if rid in await member_managed_role_ids(
            guild_id, user_id, bot=bot, member=member):
        return "member-entitled Prestige/Booster role"
    return None


async def role_still_entitled(guild_id: int, user_id: int, role_id: int,
                              *, exclude_entry_key: str | None = None,
                              bot=None, member=None) -> str | None:
    """A human-readable reason the role must stay, or None to allow removal.

    Unlike `utils.level_claims._role_granted_elsewhere` (which answers a
    reversal question about one claim), this answers "is the Discord role still
    justified for this member after THIS timed holding expires". Both are
    conservative for the same reason: revoking a role someone still earned is
    unrecoverable without their action.

    Sources consulted, in order:
      1. another unexpired timed holding for the same role,
      2. an unexpired legacy `temp_roles` row (level-claim units, older grants),
      3. a valid inventory `role` (or live non-stale `temp_role`) marker,
      4. a fulfilled permanent Level role claim (`level_reward_claims`),
      5. a per-member Prestige / Booster role the member is currently entitled to.
    """
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return None
    now = utcnow()
    member = _resolve_member_from_bot(bot, guild_id, user_id, member)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, expires_at FROM timed_holdings "
                "WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, rid))
            for holding_id, expires_at in await cursor.fetchall():
                if exclude_entry_key and str(entry_key_id(exclude_entry_key)) == str(holding_id):
                    continue
                if not is_expired(expires_at, now=now):
                    return "another unexpired timed holding"

            has_expired_temp_roles_row = False
            cursor = await db.execute(
                "SELECT id, expires_at FROM temp_roles "
                "WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, rid))
            tr_rows = await cursor.fetchall()
            for _temp_id, expires_at in tr_rows:
                if not is_expired(expires_at, now=now):
                    return "unexpired temp_roles grant"
            if tr_rows:
                has_expired_temp_roles_row = True

            cursor = await db.execute(
                "SELECT item_name, item_type, source, metadata, quantity "
                "FROM inventory_items WHERE guild_id=? AND user_id=? "
                "AND item_type IN ('role','temp_role')",
                (guild_id, user_id))
            for item_name, item_type, source, metadata, quantity in await cursor.fetchall():
                if not quantity or int(quantity) <= 0:
                    continue
                try:
                    meta = json.loads(metadata) if metadata else {}
                except (TypeError, ValueError):
                    continue
                try:
                    if int((meta or {}).get("role_id")) != rid:
                        continue
                except (TypeError, ValueError):
                    continue
                meta_exp = (meta or {}).get("expires_at")
                if item_type == "temp_role":
                    # A legacy `temp_role` inventory row is only a temporary
                    # marker. If its `temp_roles` rows have all expired, if it
                    # is an orphaned level_claim marker with no live unit in
                    # `temp_roles`, or if it lacks a valid future `expires_at`,
                    # it is stale and must not block expiry removal.
                    if has_expired_temp_roles_row:
                        continue
                    if str(source or "") == "level_claim":
                        continue
                    if meta_exp in (None, "") or is_expired(meta_exp, now=now):
                        continue
                    return f"owned temp_role item ({item_name}, {source})"
                if meta_exp not in (None, "") and is_expired(meta_exp, now=now):
                    continue
                return f"owned role item ({item_name}, {source})"
    except EntitlementCheckError:
        raise
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify role ownership entitlement: {exc}"
        ) from exc

    non_slot = await non_slot_role_entitlement(
        guild_id, user_id, rid, bot=bot, member=member)
    if non_slot:
        return non_slot
    return None


def entry_key_id(entry_key: str) -> int | None:
    """Numeric id inside an `h:<id>` / `i:<id>` key (None when malformed)."""
    try:
        return int(str(entry_key).split(":", 1)[1])
    except (IndexError, ValueError, AttributeError):
        return None


# ── Expiry cleanup ──────────────────────────────────────────────────────

async def expired_holdings(limit: int = 200, *,
                           now: datetime | None = None) -> list[dict]:
    """Holdings whose time has run out, oldest expiry first.

    Evaluated through the central `parse_time` / `is_expired` helpers rather
    than lexicographical SQL string comparison so malformed timestamps and
    valid ISO-8601 timestamps with non-UTC offsets are selected consistently
    with read-time filtering.
    """
    ref_now = now or utcnow()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings "
            "ORDER BY id ASC")
        rows = await cursor.fetchall()
    expired = []
    epoch_min = datetime.min.replace(tzinfo=timezone.utc)
    for row in rows:
        holding = _row_to_holding(row, _HOLDING_COLUMNS)
        if is_expired(holding["expires_at"], now=ref_now):
            expired.append(holding)
    expired.sort(
        key=lambda h: (parse_time(h["expires_at"]) or epoch_min, h["id"]))
    if limit is not None and int(limit) >= 0:
        return expired[:int(limit)]
    return expired


async def clear_equip_for_holding(guild_id: int, user_id: int,
                                  entry_key: str,
                                  role_id: int | None = None) -> bool:
    """Drop `equipped_roles` only when it points at this exact holding.

    `entry_key` is the identity. Rows written before this feature have no
    entry_key, so a NULL key plus a matching role_id also counts — and that
    fallback is only ever reached after `role_still_entitled()` established
    that nothing else requires the role, so it cannot clear a slot that another
    entitlement still needs.
    """
    from utils.equip_engine import get_equipped

    current = await get_equipped(guild_id, user_id)
    if not current:
        return False
    stored = current.get("entry_key")
    if stored:
        if stored != entry_key:
            return False
    else:
        if role_id is None or int(current.get("role_id") or 0) != int(role_id):
            return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM equipped_roles WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        await db.commit()
    return True


async def reconcile_equip_for_expired_holding(guild_id: int, user_id: int,
                                              entry_key: str,
                                              role_id: int | None = None) -> str | None:
    """Reconcile `equipped_roles` when an expired holding is deleted but its role stays.

    If `equipped_roles` does not point at `entry_key`, leaves it untouched.
    If it does point at `entry_key`, repoints it to another valid owned
    inventory/timed entry for the same `role_id` when one exists; otherwise
    clears the slot so `equipped_roles.entry_key` never dangles on a deleted
    holding.
    """
    from utils.equip_engine import get_equipped, record_equipped
    from utils.inventory import get_inventory

    current = await get_equipped(guild_id, user_id)
    if not current:
        return None
    stored = current.get("entry_key")
    if stored:
        if stored != entry_key:
            return stored
    else:
        if role_id is None or int(current.get("role_id") or 0) != int(role_id):
            return None

    if role_id is not None:
        rid = int(role_id)
        now = utcnow()
        has_live_temp_roles = False
        has_expired_temp_roles = False
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT expires_at FROM temp_roles "
                "WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, rid))
            tr_rows = await cursor.fetchall()
            for (tr_exp,) in tr_rows:
                if not is_expired(tr_exp, now=now):
                    has_live_temp_roles = True
                    break
            if tr_rows and not has_live_temp_roles:
                has_expired_temp_roles = True

        items = await get_inventory(guild_id, user_id, include_empty=False)
        candidates = []
        for it in items:
            if it.get("entry_key") == entry_key:
                continue
            if it.get("item_type") not in ("role", "temp_role"):
                continue
            if role_id_of(it) != rid:
                continue
            if not it.get("timed") and it.get("item_type") == "temp_role":
                meta = it.get("metadata") or {}
                meta_exp = meta.get("expires_at")
                if has_expired_temp_roles and not has_live_temp_roles:
                    continue
                if str(it.get("source") or "") == "level_claim" and not has_live_temp_roles:
                    continue
                if not has_live_temp_roles and (meta_exp in (None, "") or is_expired(meta_exp, now=now)):
                    continue
            candidates.append(it)

        epoch_min = datetime.min.replace(tzinfo=timezone.utc)
        candidates.sort(
            key=lambda it: (
                1 if it.get("timed") else 0,
                parse_time(it.get("expires_at")) or epoch_min,
                it.get("entry_key") or "",
            ))
        if candidates:
            chosen = candidates[0]
            await record_equipped(
                guild_id, user_id, chosen["item_name"],
                role_id=rid, entry_key=chosen["entry_key"])
            return chosen["entry_key"]

    await clear_equip_for_holding(guild_id, user_id, entry_key, role_id=role_id)
    return None


async def expire_one(bot, holding: dict) -> dict:
    """Retire ONE expired holding, reconciling Discord and bookkeeping.

    Idempotent by construction: every step reads current state first, and the
    holding row is deleted only once nothing is left that would need it as a
    retry marker.

    Returns {"status": "removed"|"kept"|"skipped", "reason": ...}.
    """
    guild_id = int(holding["guild_id"])
    user_id = int(holding["user_id"])
    entry_key = holding["entry_key"]
    role_id = holding.get("role_id")

    if role_id is None:
        # Nothing Discord-side to undo: the ownership record IS the whole state.
        try:
            await clear_equip_for_holding(guild_id, user_id, entry_key)
        except Exception:
            pass
        await delete_timed_holding(entry_key)
        return {"status": "removed", "reason": "no role"}

    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        # Cannot see the guild: keep the record so the removal is retried once
        # the bot is back. It is already hidden from every read path.
        return {"status": "kept", "reason": "guild unavailable"}

    member = guild.get_member(user_id)
    try:
        reason = await role_still_entitled(
            guild_id, user_id, role_id, exclude_entry_key=entry_key,
            bot=bot, member=member)
    except Exception as exc:  # pragma: no cover - defensive
        err = f"entitlement check failed: {exc}"
        return {"status": "kept", "reason": err, "error": err}

    if reason:
        # Another entitlement keeps the role. Reconcile `equipped_roles` first
        # so it never points at the deleted holding, then delete only this row.
        try:
            await reconcile_equip_for_expired_holding(
                guild_id, user_id, entry_key, role_id=role_id)
        except Exception as exc:  # pragma: no cover - defensive
            err = f"equip bookkeeping cleanup failed: {exc}"
            return {"status": "kept", "reason": err, "error": err}
        await delete_timed_holding(entry_key)
        return {"status": "removed", "reason": f"role retained ({reason})"}

    role = guild.get_role(int(role_id))
    if member is not None and role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason="Timed entitlement expired")
        except Exception as exc:
            # Keep the row: it is the only marker that this removal is pending.
            return {"status": "kept", "reason": f"Discord removal failed: {exc}"}

    # Bookkeeping BEFORE the row is deleted: `clear_equip_for_holding` is
    # idempotent, so a crash after this point simply retries, whereas deleting
    # the holding first would leave an equipped slot pointing at nothing with
    # only this row left to remember it by.
    try:
        await clear_equip_for_holding(guild_id, user_id, entry_key,
                                      role_id=role_id)
    except Exception as exc:  # pragma: no cover - defensive
        err = f"equip bookkeeping cleanup failed: {exc}"
        return {"status": "kept", "reason": err, "error": err}

    await delete_timed_holding(entry_key)
    return {"status": "removed", "reason": "expired"}


async def cleanup_expired_holdings(bot, *, limit: int = 200) -> dict:
    """One cleanup pass. Safe to re-run at any time."""
    removed = kept = 0
    errors: list[str] = []
    for holding in await expired_holdings(limit=limit):
        try:
            result = await expire_one(bot, holding)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{holding.get('entry_key')}: {exc}")
            kept += 1
            continue
        if result["status"] == "removed":
            removed += 1
        else:
            kept += 1
            if result.get("error"):
                errors.append(f"{holding.get('entry_key')}: {result['error']}")
    return {"removed": removed, "kept": kept, "errors": errors}


# ── Purchase saga journal ───────────────────────────────────────────────

PURCHASE_PENDING = "pending"
PURCHASE_DELIVERED = "delivered"
PURCHASE_REFUNDED = "refunded"
PURCHASE_UNCERTAIN = "uncertain"
PURCHASE_FAILED = "failed"

PURCHASE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_purchase_operations (
        op_id          TEXT PRIMARY KEY,
        guild_id       INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        item_id        INTEGER,
        item_name      TEXT,
        currency       TEXT,
        amount         INTEGER NOT NULL DEFAULT 0,
        stock_reserved INTEGER NOT NULL DEFAULT 0,
        charged        INTEGER NOT NULL DEFAULT 0,
        refunded       INTEGER NOT NULL DEFAULT 0,
        stock_returned INTEGER NOT NULL DEFAULT 0,
        status         TEXT NOT NULL,
        detail         TEXT,
        history_id     INTEGER,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_ops_status
    ON shop_purchase_operations(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_ops_user
    ON shop_purchase_operations(guild_id, user_id)
    """,
)


# Effect flags added after the first schema version. They let a compensation
# that crashed half-way (money returned, stock not yet) be finished later
# instead of being repeated or abandoned. PRAGMA-guarded so the migration is
# idempotent and touches no existing value.
_PURCHASE_EXTRA_COLUMNS = (
    ("refunded", "INTEGER NOT NULL DEFAULT 0"),
    ("stock_returned", "INTEGER NOT NULL DEFAULT 0"),
)


async def ensure_purchase_journal_schema(db) -> None:
    for statement in PURCHASE_SCHEMA:
        await db.execute(statement)
    cursor = await db.execute("PRAGMA table_info(shop_purchase_operations)")
    present = {row[1] for row in await cursor.fetchall()}
    for name, ddl in _PURCHASE_EXTRA_COLUMNS:
        if name not in present:
            await db.execute(
                f"ALTER TABLE shop_purchase_operations ADD COLUMN {name} {ddl}")
    await db.commit()


async def open_purchase(guild_id: int, user_id: int, *, item_id: int | None,
                        item_name: str, currency: str, amount: int,
                        db: aiosqlite.Connection | None = None) -> str:
    """Write the recovery anchor BEFORE any money or stock moves.

    A crash after this row exists is diagnosable and refundable; a crash before
    it is a purchase that never started, which is the safe side.
    """
    op_id = uuid.uuid4().hex
    sql = (
        "INSERT INTO shop_purchase_operations "
        "(op_id, guild_id, user_id, item_id, item_name, currency, amount,"
        " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (op_id, guild_id, user_id, item_id, item_name, currency,
              int(amount or 0), PURCHASE_PENDING)
    if db is not None:
        await db.execute(sql, params)
    else:
        async with aiosqlite.connect(DB_PATH) as connection:
            await connection.execute(sql, params)
            await connection.commit()
    return op_id


async def update_purchase(op_id: str, *, status: str | None = None,
                          detail: str | None = None,
                          stock_reserved: int | None = None,
                          charged: int | None = None,
                          refunded: int | None = None,
                          stock_returned: int | None = None,
                          history_id: int | None = None,
                          db: aiosqlite.Connection | None = None) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if detail is not None:
        sets.append("detail=?")
        params.append(detail)
    if stock_reserved is not None:
        sets.append("stock_reserved=?")
        params.append(int(stock_reserved))
    if charged is not None:
        sets.append("charged=?")
        params.append(int(charged))
    if refunded is not None:
        sets.append("refunded=?")
        params.append(int(refunded))
    if stock_returned is not None:
        sets.append("stock_returned=?")
        params.append(int(stock_returned))
    if history_id is not None:
        sets.append("history_id=?")
        params.append(int(history_id))
    if not sets:
        return False
    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(op_id)
    where_extra = ""
    if status == PURCHASE_DELIVERED:
        where_extra = (
            " AND status IN (?, ?, ?)"
            " AND COALESCE(refunded, 0) = 0"
            " AND COALESCE(stock_returned, 0) = 0"
        )
        params.extend((PURCHASE_PENDING, PURCHASE_UNCERTAIN, PURCHASE_FAILED))
    elif status == PURCHASE_UNCERTAIN:
        where_extra = (
            " AND status IN (?, ?)"
            " AND COALESCE(refunded, 0) = 0"
            " AND COALESCE(stock_returned, 0) = 0"
        )
        params.extend((PURCHASE_PENDING, PURCHASE_UNCERTAIN))
    sql = (
        f"UPDATE shop_purchase_operations SET {', '.join(sets)} "
        f"WHERE op_id=?{where_extra}"
    )
    if db is not None:
        cursor = await db.execute(sql, params)
        return cursor.rowcount == 1
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.rowcount == 1


async def get_purchase(op_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT op_id, guild_id, user_id, item_id, item_name, currency,"
            " amount, stock_reserved, charged, refunded, stock_returned,"
            " status, detail, history_id, created_at, updated_at"
            " FROM shop_purchase_operations WHERE op_id=?", (op_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    keys = ("op_id", "guild_id", "user_id", "item_id", "item_name", "currency",
            "amount", "stock_reserved", "charged", "refunded", "stock_returned",
            "status", "detail", "history_id", "created_at", "updated_at")
    return dict(zip(keys, row))


def is_ambiguous_discord_role_error(exc: BaseException, *,
                                    member=None, role=None) -> bool:
    """Return True when a `member.add_roles()` exception leaves delivery uncertain.

    A role grant is ONLY a known pre-application failure (`False`) when:
      1. the target role is not currently present on `member.roles`, AND
      2. Discord returned a deterministic client/permission rejection before
         applying the role (`discord.Forbidden`, `discord.NotFound`, or a
         `discord.HTTPException` with a 4xx status other than 429).

    Any timeout, transport/connection error, 5xx/429 response, unknown
    exception, or state where the role is observed on `member.roles` is
    ambiguous (`True`): the HTTP request may have succeeded on Discord's side,
    so the caller must park the purchase as `uncertain` and never auto-refund.
    """
    if member is not None and role is not None:
        try:
            if role in (getattr(member, "roles", None) or ()):
                return True
        except Exception:
            return True

    import discord
    if isinstance(exc, (discord.Forbidden, discord.NotFound)):
        return False
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            return False
    return True


async def grant_timed_product(bot, guild_id: int, user_id: int, *,
                              item_name: str, duration_hours,
                              item_type: str = "shop_custom",
                              metadata: dict | None = None,
                              source: str = "shop",
                              product_id: int | None = None,
                              category: str | None = None,
                              role_id: int | None = None,
                              reason: str = "Timed reward") -> dict:
    """Deliver a timed holding. Call ONLY after the charge/reservation is safe.

    Returns {"success": bool, "entry_key": str|None, "expires_at": str|None,
             "error": str|None, "uncertain": bool}.

    `expires_at` is computed HERE, at the moment of delivery, never from the
    listing's publication time or from an abandoned purchase attempt — that is
    the whole point of "the lifetime starts when delivery succeeds".

    A role-backed holding adds the Discord role before the ownership row is
    written. If the role cannot be added, nothing is persisted and the caller
    can compensate cleanly. If the role IS added (or `add_roles` fails with an
    ambiguous timeout/transport error) or the row write fails after adding the
    role, the result is reported as `uncertain` rather than failed: the member
    may hold a role the bot has no record of, and refunding then would hand out
    a free colour.
    """
    from utils.permissions import check_bot_role_position

    expires_at = expiry_from_hours(duration_hours)
    if expires_at is None:
        return {"success": False, "entry_key": None, "expires_at": None,
                "error": "A timed product needs a positive duration.",
                "uncertain": False}

    meta = dict(metadata or {})
    if category:
        meta["category"] = category
    if role_id is not None:
        meta["role_id"] = int(role_id)
    if product_id is not None:
        meta["product_id"] = int(product_id)

    role_added = False
    guild = None
    member = None
    if role_id is not None:
        if bot is None:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": "Bot is offline; the role cannot be granted.",
                    "uncertain": False}
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": "Bot is not in that guild.", "uncertain": False}
        member = guild.get_member(int(user_id))
        role = guild.get_role(int(role_id))
        if member is None or role is None:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": "Member or role not found.", "uncertain": False}
        can_assign, warning = check_bot_role_position(guild, role)
        if not can_assign:
            return {"success": False, "entry_key": None, "expires_at": None,
                    "error": warning, "uncertain": False}
        if role not in member.roles:
            try:
                await member.add_roles(role, reason=reason)
                role_added = True
            except Exception as exc:
                ambiguous = is_ambiguous_discord_role_error(
                    exc, member=member, role=role)
                return {"success": False, "entry_key": None,
                        "expires_at": None,
                        "error": f"Discord error assigning role: {exc}",
                        "uncertain": ambiguous}

    try:
        holding_id = await grant_timed_holding(
            guild_id, user_id, item_name, expires_at=expires_at,
            item_type=item_type, metadata=meta, source=source,
            product_id=product_id, category=category, role_id=role_id)
    except Exception as exc:
        return {"success": False, "entry_key": None, "expires_at": None,
                "error": f"Could not record ownership: {exc}",
                # The role may already be on the member: this must be
                # reconciled, not blindly refunded.
                "uncertain": role_added}

    entry_key = f"h:{holding_id}"
    if role_id is not None:
        # Perform the guarded single-slot Discord role swap and update
        # `equipped_roles`, keeping ownership of any previously worn role and
        # never stripping a role the member still holds through a non-slot
        # entitlement (Level claim, Prestige, Booster) or the same `role_id`.
        try:
            from utils.equip_engine import swap_and_record_equipped
            await swap_and_record_equipped(
                bot, guild_id, user_id,
                item_name=item_name, role_id=int(role_id),
                entry_key=entry_key, guild=guild, member=member,
                reason=f"Replaced by timed role: {item_name}")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[TIMED] equip swap/bookkeeping failed for {entry_key}: {exc}")
            try:
                from utils.equip_engine import record_equipped
                await record_equipped(guild_id, user_id, item_name,
                                      role_id=int(role_id), entry_key=entry_key)
            except Exception:
                pass

    return {"success": True, "entry_key": entry_key,
            "expires_at": expires_at, "error": None, "uncertain": False}


async def claim_purchase_transition(op_id: str, *,
                                    from_statuses: tuple[str, ...],
                                    to_status: str,
                                    detail: str | None = None,
                                    history_id: int | None = None,
                                    require_uncompensated: bool | None = None,
                                    db: aiosqlite.Connection | None = None) -> bool:
    """Atomically move one purchase operation between states.

    The `WHERE status IN (...)` clause is what makes compensation
    exactly-once: two concurrent retries (a double click, a restart mid-recovery)
    cannot both win the transition, so a refund or a stock restore can never be
    applied twice. Returns True only for the caller that won.

    Whenever `to_status in (PURCHASE_DELIVERED, PURCHASE_UNCERTAIN)` (or
    `require_uncompensated=True`), the SQL `WHERE` clause also enforces
    `COALESCE(refunded, 0) = 0 AND COALESCE(stock_returned, 0) = 0` so an
    operation that has already begun or completed compensation can never
    transition to `delivered` or revert to `uncertain`.
    """
    if require_uncompensated is None:
        require_uncompensated = (
            to_status in (PURCHASE_DELIVERED, PURCHASE_UNCERTAIN)
        )
    marks = ",".join("?" for _ in from_statuses)
    guard_sql = (
        " AND COALESCE(refunded, 0) = 0 AND COALESCE(stock_returned, 0) = 0"
        if require_uncompensated else ""
    )
    set_cols = ["status=?", "detail=?", "updated_at=CURRENT_TIMESTAMP"]
    set_params: list = [to_status, detail]
    if history_id is not None:
        set_cols.append("history_id=?")
        set_params.append(int(history_id))
    sql = (
        f"UPDATE shop_purchase_operations SET {', '.join(set_cols)} "
        f"WHERE op_id=? AND status IN ({marks}){guard_sql}"
    )
    params = (*set_params, op_id, *from_statuses)
    if db is not None:
        cursor = await db.execute(sql, params)
        return cursor.rowcount == 1
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.rowcount == 1


# ── Compensation ────────────────────────────────────────────────────────

async def restore_stock(item_id, *,
                        db: aiosqlite.Connection | None = None) -> bool:
    """Give one unit back to a stock-limited listing.

    Mirrors `utils.economy_safe.safe_decrement_stock()`: an unlimited listing
    has nothing to give back, and `max_stock` (not the current value) decides
    whether the counter is meaningful at all.

    When `db` is provided, joins the caller's open transaction without
    committing so the stock restoration and journal marker commit atomically.
    """
    if item_id is None:
        return False

    async def _apply(conn: aiosqlite.Connection) -> bool:
        cursor = await conn.execute(
            "SELECT max_stock FROM shop_items WHERE id=?", (item_id,))
        row = await cursor.fetchone()
        if not row or not row[0]:
            return False
        await conn.execute(
            "UPDATE shop_items SET current_stock = current_stock + 1 WHERE id=?",
            (item_id,))
        return True

    if db is not None:
        return await _apply(db)
    async with aiosqlite.connect(DB_PATH) as conn:
        applied = await _apply(conn)
        if applied:
            await conn.commit()
        return applied


async def refund_purchase(op_id: str) -> dict:
    """Return what a failed purchase took, at most once per effect.

    Each effect (`safe_credit` + `refunded=1`, and `restore_stock` +
    `stock_returned=1`) executes inside its own `BEGIN IMMEDIATE` SQLite
    transaction so a crash or error after the balance/stock write rolls back
    both the effect and the journal flag, eliminating duplicate refunds or
    stock restores on retry.

    Returns {"refunded": bool, "stock_returned": bool, "complete": bool,
             "error": str|None}.
    """
    op = await get_purchase(op_id)
    if op is None:
        return {"refunded": False, "stock_returned": False,
                "complete": False, "error": "Unknown purchase operation"}
    if op["status"] == PURCHASE_DELIVERED:
        # Delivered means the member got the goods; anything after that is a
        # support decision, never an automatic refund.
        return {"refunded": False, "stock_returned": False,
                "complete": False,
                "error": "Cannot refund a delivered purchase operation"}

    error = None
    if op["charged"] and not op["refunded"]:
        from utils.economy_safe import safe_credit
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT status, charged, refunded "
                    "FROM shop_purchase_operations WHERE op_id=?",
                    (op_id,))
                cur_row = await cursor.fetchone()
                if not cur_row:
                    await db.execute("ROLLBACK")
                    return {"refunded": False, "stock_returned": False,
                            "complete": False,
                            "error": "Unknown purchase operation"}
                cur_status, cur_charged, cur_refunded = cur_row
                if cur_status == PURCHASE_DELIVERED:
                    await db.execute("ROLLBACK")
                    return {"refunded": False, "stock_returned": False,
                            "complete": False,
                            "error": "Cannot refund a delivered purchase operation"}
                if cur_charged and not cur_refunded:
                    try:
                        credit_res = await safe_credit(
                            op["guild_id"], op["user_id"],
                            int(op["amount"] or 0),
                            currency=op["currency"] or "balance",
                            reason=f"Refund: {op['item_name']}",
                            source="shop_refund", db=db)
                    except TypeError as type_exc:
                        if "db" not in str(type_exc):
                            raise
                        credit_res = await safe_credit(
                            op["guild_id"], op["user_id"],
                            int(op["amount"] or 0),
                            currency=op["currency"] or "balance",
                            reason=f"Refund: {op['item_name']}",
                            source="shop_refund")
                    if isinstance(credit_res, dict) and not credit_res.get("success", True):
                        raise RuntimeError(
                            credit_res.get("error") or "safe_credit failed")
                    try:
                        await update_purchase(op_id, refunded=1, db=db)
                    except TypeError as type_exc:
                        if "db" not in str(type_exc):
                            raise
                        await update_purchase(op_id, refunded=1)
                    await db.commit()
                    op["refunded"] = 1
                else:
                    await db.execute("ROLLBACK")
                    op["refunded"] = int(bool(cur_refunded))
            except Exception as exc:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                error = f"refund failed: {exc}"

    if op["stock_reserved"] and not op["stock_returned"]:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT status, stock_reserved, stock_returned "
                    "FROM shop_purchase_operations WHERE op_id=?",
                    (op_id,))
                cur_row = await cursor.fetchone()
                if not cur_row:
                    await db.execute("ROLLBACK")
                    return {"refunded": bool(op["refunded"]),
                            "stock_returned": False,
                            "complete": False,
                            "error": "Unknown purchase operation"}
                cur_status, cur_reserved, cur_returned = cur_row
                if cur_status == PURCHASE_DELIVERED:
                    await db.execute("ROLLBACK")
                    return {"refunded": bool(op["refunded"]),
                            "stock_returned": False,
                            "complete": False,
                            "error": "Cannot refund a delivered purchase operation"}
                if cur_reserved and not cur_returned:
                    try:
                        await restore_stock(op["item_id"], db=db)
                    except TypeError as type_exc:
                        if "db" not in str(type_exc):
                            raise
                        await restore_stock(op["item_id"])
                    try:
                        await update_purchase(op_id, stock_returned=1, db=db)
                    except TypeError as type_exc:
                        if "db" not in str(type_exc):
                            raise
                        await update_purchase(op_id, stock_returned=1)
                    await db.commit()
                    op["stock_returned"] = 1
                else:
                    await db.execute("ROLLBACK")
                    op["stock_returned"] = int(bool(cur_returned))
            except Exception as exc:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                error = error or f"stock restore failed: {exc}"

    complete = (bool(op["refunded"] or not op["charged"])
                and bool(op["stock_returned"] or not op["stock_reserved"])
                and error is None)
    return {"refunded": bool(op["refunded"]),
            "stock_returned": bool(op["stock_returned"]),
            "complete": complete, "error": error}


async def reconcile_purchases(*, older_than_minutes: int = 15,
                              limit: int = 100) -> dict:
    """Finish compensations and quarantine purchases that never resolved.

    Two buckets:

    * `failed` / `refunded` operations with an outstanding effect flag —
      completed here, idempotently.
    * `pending` operations older than `older_than_minutes` — the process died
      between the charge and the delivery result. The money may or may not have
      moved and the goods may or may not exist, so the row is parked as
      `uncertain` for a human decision. It is NOT auto-refunded: that is exactly
      the "don't refund when delivery is uncertain" rule.
    """
    stats = {"completed": 0, "uncertain": 0, "errors": 0}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT op_id FROM shop_purchase_operations"
            " WHERE status IN (?, ?)"
            "   AND (refunded = 0 AND charged = 1"
            "        OR stock_returned = 0 AND stock_reserved = 1)"
            " LIMIT ?",
            (PURCHASE_FAILED, PURCHASE_REFUNDED, limit))
        rows = [r[0] for r in await cursor.fetchall()]
    for op_id in rows:
        try:
            result = await refund_purchase(op_id)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[SHOP] Compensation failed for {op_id}: {exc}")
            stats["errors"] += 1
            continue
        if result["complete"]:
            stats["completed"] += 1
        else:
            stats["errors"] += 1

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shop_purchase_operations SET status=?, detail=?,"
            " updated_at=CURRENT_TIMESTAMP"
            " WHERE status=? AND created_at <= datetime('now', ?)",
            (PURCHASE_UNCERTAIN,
             "Timed out before a delivery result; needs review",
             PURCHASE_PENDING,
             f"-{int(older_than_minutes)} minutes"))
        await db.commit()
        stats["uncertain"] = cursor.rowcount or 0
    return stats


# ── Uncertain purchase inspection & operator recovery ───────────────────

async def list_purchase_operations(*, status: str | None = PURCHASE_UNCERTAIN,
                                   guild_id: int | None = None,
                                   user_id: int | None = None,
                                   limit: int = 100) -> list[dict]:
    """List purchase operations filtered by status, guild, and/or user."""
    clauses: list[str] = []
    params: list = []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if guild_id is not None:
        clauses.append("guild_id=?")
        params.append(int(guild_id))
    if user_id is not None:
        clauses.append("user_id=?")
        params.append(int(user_id))
    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT op_id, guild_id, user_id, item_id, item_name, currency,"
            " amount, stock_reserved, charged, refunded, stock_returned,"
            " status, detail, history_id, created_at, updated_at"
            f" FROM shop_purchase_operations{where_sql}"
            " ORDER BY created_at DESC, op_id ASC LIMIT ?",
            params)
        rows = await cursor.fetchall()
    keys = ("op_id", "guild_id", "user_id", "item_id", "item_name", "currency",
            "amount", "stock_reserved", "charged", "refunded", "stock_returned",
            "status", "detail", "history_id", "created_at", "updated_at")
    return [dict(zip(keys, row)) for row in rows]


async def inspect_purchase_operation(op_id: str) -> dict | None:
    """Gather read-only diagnostic evidence for one purchase operation.

    Performs zero mutations. Collects the journal record alongside correlated
    shop listing state, purchase history, wallet balance, transaction ledger
    entries, inventory items, timed holdings, legacy `temp_roles`, and equipped
    role state so an operator can verify whether delivery happened before
    choosing a resolution.
    """
    op = await get_purchase(op_id)
    if op is None:
        return None
    guild_id = int(op["guild_id"])
    user_id = int(op["user_id"])
    item_id = op["item_id"]
    item_name = op["item_name"]
    history_id = op["history_id"]

    evidence: dict = {
        "operation": op,
        "shop_item": None,
        "purchase_history": None,
        "member_wallet": None,
        "matching_inventory_items": [],
        "matching_timed_holdings": [],
        "matching_temp_roles": [],
        "equipped_role": None,
        "recent_ledger_entries": [],
    }

    role_id_candidate: int | None = None
    async with aiosqlite.connect(DB_PATH) as db:
        if item_id is not None:
            try:
                cursor = await db.execute(
                    "SELECT id, name, type, role_id, duration_hours,"
                    " max_stock, current_stock, enabled, price, price_diamonds"
                    " FROM shop_items WHERE id=?",
                    (item_id,))
                row = await cursor.fetchone()
                if row:
                    cols = ("id", "name", "item_type", "role_id",
                            "duration_hours", "max_stock", "current_stock",
                            "enabled", "price", "price_diamonds")
                    evidence["shop_item"] = dict(zip(cols, row))
                    if row[3] not in (None, ""):
                        try:
                            role_id_candidate = int(row[3])
                        except (TypeError, ValueError):
                            role_id_candidate = None
            except aiosqlite.Error:
                pass

        if history_id is not None:
            try:
                cursor = await db.execute(
                    "SELECT id, guild_id, user_id, item_id, item_name,"
                    " price_paid, currency_paid, expires_at, purchased_at"
                    " FROM purchase_history WHERE id=?",
                    (history_id,))
                row = await cursor.fetchone()
                if row:
                    cols = ("id", "guild_id", "user_id", "item_id",
                            "item_name", "price_paid", "currency_paid",
                            "expires_at", "purchased_at")
                    evidence["purchase_history"] = dict(zip(cols, row))
            except aiosqlite.Error:
                pass

        try:
            cursor = await db.execute(
                "SELECT balance, diamonds FROM economy"
                " WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            if row:
                evidence["member_wallet"] = {
                    "balance": int(row[0] or 0),
                    "diamonds": int(row[1] or 0),
                }
        except aiosqlite.Error:
            pass

        try:
            cursor = await db.execute(
                "SELECT id, item_name, item_type, quantity, source, metadata,"
                " created_at, updated_at FROM inventory_items"
                " WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            for r in await cursor.fetchall():
                inv_id, i_name, i_type, qty, src, meta_raw, c_at, u_at = r
                try:
                    meta = json.loads(meta_raw) if meta_raw else {}
                except (TypeError, ValueError):
                    meta = {}
                meta_pid = meta.get("product_id")
                meta_rid = meta.get("role_id")
                matches = (
                    (item_name and i_name == item_name)
                    or (item_id is not None and str(meta_pid) == str(item_id))
                    or (role_id_candidate is not None
                        and meta_rid not in (None, "")
                        and str(meta_rid) == str(role_id_candidate))
                )
                if matches:
                    evidence["matching_inventory_items"].append({
                        "entry_key": f"i:{inv_id}",
                        "id": inv_id,
                        "item_name": i_name,
                        "item_type": i_type,
                        "quantity": qty,
                        "source": src,
                        "metadata": meta,
                        "created_at": c_at,
                        "updated_at": u_at,
                    })
        except aiosqlite.Error:
            pass

        try:
            cursor = await db.execute(
                f"SELECT {', '.join(_HOLDING_COLUMNS)} FROM timed_holdings"
                " WHERE guild_id=? AND user_id=? ORDER BY id ASC",
                (guild_id, user_id))
            for r in await cursor.fetchall():
                h = _row_to_holding(r, _HOLDING_COLUMNS)
                matches = (
                    (item_name and h["item_name"] == item_name)
                    or (item_id is not None
                        and h["product_id"] is not None
                        and int(h["product_id"]) == int(item_id))
                    or (role_id_candidate is not None
                        and h["role_id"] is not None
                        and int(h["role_id"]) == int(role_id_candidate))
                )
                if matches:
                    h["expired"] = is_expired(h["expires_at"])
                    evidence["matching_timed_holdings"].append(h)
        except aiosqlite.Error:
            pass

        if role_id_candidate is not None:
            try:
                cursor = await db.execute(
                    "SELECT id, role_id, expires_at FROM temp_roles"
                    " WHERE guild_id=? AND user_id=? AND role_id=?",
                    (guild_id, user_id, role_id_candidate))
                for tr_id, tr_rid, tr_exp in await cursor.fetchall():
                    evidence["matching_temp_roles"].append({
                        "id": tr_id,
                        "role_id": tr_rid,
                        "expires_at": tr_exp,
                        "expired": is_expired(tr_exp),
                    })
            except aiosqlite.Error:
                pass

        try:
            cursor = await db.execute(
                "SELECT id, currency, amount, balance_after, reason, source,"
                " created_at FROM transaction_ledger"
                " WHERE guild_id=? AND user_id=?"
                " ORDER BY id DESC LIMIT 10",
                (guild_id, user_id))
            cols = ("id", "currency", "amount", "balance_after", "reason",
                    "source", "created_at")
            evidence["recent_ledger_entries"] = [
                dict(zip(cols, r)) for r in await cursor.fetchall()
            ]
        except aiosqlite.Error:
            pass

    try:
        from utils.equip_engine import get_equipped
        evidence["equipped_role"] = await get_equipped(guild_id, user_id)
    except Exception:
        pass

    return evidence


async def resolve_purchase_operation(op_id: str, *, action: str,
                                     confirmed: bool = False,
                                     note: str | None = None) -> dict:
    """Resolve an `uncertain` (or incomplete `failed`) purchase operation.

    Requires `confirmed=True` so no caller or CLI invocation can mutate state
    without explicit operator confirmation.

    Supported actions:
      * `"refund"`: atomically refunds any charged currency (`refunded=0`) and
        restores any reserved stock (`stock_returned=0`), then transitions the
        operation to `refunded` while preserving the audit trail in `detail`.
      * `"delivered"` (or `"mark_delivered"`): marks the operation as
        `delivered` once the operator has verified (or manually completed)
        delivery, refusing if partial compensation has already occurred.
    """
    normalized = (action or "").strip().lower()
    if normalized in ("mark_delivered", "deliver"):
        normalized = "delivered"
    elif normalized in ("compensate", "refunded"):
        normalized = "refund"

    if normalized not in ("refund", "delivered"):
        return {
            "success": False,
            "op_id": op_id,
            "action": action,
            "status": None,
            "error": (f"Invalid resolution action {action!r}; "
                      "choose 'refund' or 'delivered'."),
        }

    if not op_id or not str(op_id).strip():
        return {
            "success": False,
            "op_id": op_id,
            "action": normalized,
            "status": None,
            "error": "Operation ID must be a non-empty string.",
        }

    if not confirmed:
        return {
            "success": False,
            "op_id": op_id,
            "action": normalized,
            "status": None,
            "error": ("Confirmation required: pass confirmed=True "
                      "(or --yes on the CLI) to apply a resolution."),
        }

    op = await get_purchase(op_id)
    if op is None:
        return {
            "success": False,
            "op_id": op_id,
            "action": normalized,
            "status": None,
            "error": f"Unknown purchase operation: {op_id}",
        }

    current_status = op["status"]
    if current_status not in (PURCHASE_UNCERTAIN, PURCHASE_FAILED):
        return {
            "success": False,
            "op_id": op_id,
            "action": normalized,
            "status": current_status,
            "error": (f"Cannot resolve operation in status {current_status!r}; "
                      "only 'uncertain' or 'failed' operations may be resolved."),
        }

    operator_note = (note or "").strip()

    if normalized == "delivered":
        audit_suffix = "Operator resolved as delivered"
        if operator_note:
            audit_suffix += f" ({operator_note})"
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT status, refunded, stock_returned, detail "
                    "FROM shop_purchase_operations WHERE op_id=?",
                    (op_id,))
                cur_row = await cursor.fetchone()
                if not cur_row:
                    await db.execute("ROLLBACK")
                    return {
                        "success": False,
                        "op_id": op_id,
                        "action": normalized,
                        "status": None,
                        "error": f"Unknown purchase operation: {op_id}",
                    }
                cur_status, cur_refunded, cur_stock_returned, cur_detail = cur_row
                if cur_status not in (PURCHASE_UNCERTAIN, PURCHASE_FAILED):
                    await db.execute("ROLLBACK")
                    return {
                        "success": False,
                        "op_id": op_id,
                        "action": normalized,
                        "status": cur_status,
                        "error": (
                            f"Cannot resolve operation in status {cur_status!r}; "
                            "only 'uncertain' or 'failed' operations may be resolved."
                        ),
                    }
                if int(cur_refunded or 0) != 0 or int(cur_stock_returned or 0) != 0:
                    await db.execute("ROLLBACK")
                    return {
                        "success": False,
                        "op_id": op_id,
                        "action": normalized,
                        "status": cur_status,
                        "error": (
                            "Cannot mark operation as delivered because "
                            "compensation has already been partially or fully "
                            f"applied (refunded={int(cur_refunded or 0)}, "
                            f"stock_returned={int(cur_stock_returned or 0)})."
                        ),
                    }
                live_detail = cur_detail or ""
                merged_detail = (f"{live_detail} | {audit_suffix}"
                                 if live_detail else audit_suffix)
                won = await claim_purchase_transition(
                    op_id,
                    from_statuses=(PURCHASE_UNCERTAIN, PURCHASE_FAILED),
                    to_status=PURCHASE_DELIVERED,
                    detail=merged_detail,
                    require_uncompensated=True,
                    db=db,
                )
                if not won:
                    await db.execute("ROLLBACK")
                    updated = await get_purchase(op_id)
                    return {
                        "success": False,
                        "op_id": op_id,
                        "action": normalized,
                        "status": updated["status"] if updated else None,
                        "error": "Concurrent state change prevented transition to delivered.",
                    }
                await db.commit()
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        updated = await get_purchase(op_id)
        return {
            "success": True,
            "op_id": op_id,
            "action": normalized,
            "status": PURCHASE_DELIVERED,
            "refunded": False,
            "stock_returned": False,
            "operation": updated,
            "error": None,
        }

    # normalized == "refund"
    comp = await refund_purchase(op_id)
    if not comp["complete"]:
        updated = await get_purchase(op_id)
        return {
            "success": False,
            "op_id": op_id,
            "action": normalized,
            "status": updated["status"] if updated else current_status,
            "refunded": comp["refunded"],
            "stock_returned": comp["stock_returned"],
            "operation": updated,
            "error": comp["error"] or "Compensation did not complete.",
        }

    audit_suffix = "Operator resolved via refund"
    if operator_note:
        audit_suffix += f" ({operator_note})"
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status, charged, refunded, stock_reserved, "
                "stock_returned, detail "
                "FROM shop_purchase_operations WHERE op_id=?",
                (op_id,))
            cur_row = await cursor.fetchone()
            if not cur_row:
                await db.execute("ROLLBACK")
                return {
                    "success": False,
                    "op_id": op_id,
                    "action": normalized,
                    "status": None,
                    "error": f"Unknown purchase operation: {op_id}",
                }
            (cur_status, cur_charged, cur_refunded,
             cur_reserved, cur_returned, cur_detail) = cur_row
            if cur_status not in (PURCHASE_UNCERTAIN, PURCHASE_FAILED):
                await db.execute("ROLLBACK")
                updated = await get_purchase(op_id)
                return {
                    "success": False,
                    "op_id": op_id,
                    "action": normalized,
                    "status": cur_status,
                    "refunded": bool(cur_refunded),
                    "stock_returned": bool(cur_returned),
                    "operation": updated,
                    "error": (
                        f"Cannot finalize refund in status {cur_status!r}; "
                        "only 'uncertain' or 'failed' operations may be resolved."
                    ),
                }
            if (cur_charged and not cur_refunded) or (cur_reserved and not cur_returned):
                await db.execute("ROLLBACK")
                updated = await get_purchase(op_id)
                return {
                    "success": False,
                    "op_id": op_id,
                    "action": normalized,
                    "status": cur_status,
                    "refunded": bool(cur_refunded),
                    "stock_returned": bool(cur_returned),
                    "operation": updated,
                    "error": "Compensation flags are incomplete; cannot finalize refund.",
                }
            live_detail = cur_detail or ""
            merged_detail = (f"{live_detail} | {audit_suffix}"
                             if live_detail else audit_suffix)
            won = await claim_purchase_transition(
                op_id,
                from_statuses=(PURCHASE_UNCERTAIN, PURCHASE_FAILED),
                to_status=PURCHASE_REFUNDED,
                detail=merged_detail,
                db=db,
            )
            if not won:
                await db.execute("ROLLBACK")
                updated = await get_purchase(op_id)
                return {
                    "success": False,
                    "op_id": op_id,
                    "action": normalized,
                    "status": updated["status"] if updated else None,
                    "refunded": bool(cur_refunded),
                    "stock_returned": bool(cur_returned),
                    "operation": updated,
                    "error": "Concurrent state change prevented transition to refunded.",
                }
            await db.commit()
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise
    updated = await get_purchase(op_id)
    return {
        "success": True,
        "op_id": op_id,
        "action": normalized,
        "status": updated["status"] if updated else PURCHASE_REFUNDED,
        "refunded": comp["refunded"],
        "stock_returned": comp["stock_returned"],
        "operation": updated,
        "error": None,
    }


def format_purchase_inspection(evidence: dict) -> str:
    """Render an operator-readable summary of `inspect_purchase_operation`."""
    op = evidence["operation"]
    lines = [
        f"Operation ID   : {op['op_id']}",
        f"Status         : {op['status']}",
        f"Guild / User   : guild={op['guild_id']} user={op['user_id']}",
        f"Item           : id={op['item_id']} name={op['item_name']!r}",
        f"Charge         : {op['amount']} {op['currency']} "
        f"(charged={op['charged']}, refunded={op['refunded']})",
        f"Stock          : reserved={op['stock_reserved']}, "
        f"returned={op['stock_returned']}",
        f"History ID     : {op['history_id']}",
        f"Created / Upd  : {op.get('created_at')} / {op.get('updated_at')}",
        f"Detail         : {op.get('detail') or '(none)'}",
    ]
    wallet = evidence.get("member_wallet")
    if wallet is not None:
        lines.append(
            f"Member Wallet  : balance={wallet['balance']}, "
            f"diamonds={wallet['diamonds']}")
    shop_item = evidence.get("shop_item")
    if shop_item is not None:
        lines.append(
            f"Shop Listing   : type={shop_item['item_type']} "
            f"role_id={shop_item['role_id']} "
            f"duration_hours={shop_item['duration_hours']} "
            f"stock={shop_item['current_stock']}/{shop_item['max_stock']}")
    inv = evidence.get("matching_inventory_items") or []
    lines.append(f"Matching Inv   : {len(inv)} row(s)")
    for row in inv:
        lines.append(
            f"  - {row['entry_key']} {row['item_name']!r} "
            f"type={row['item_type']} qty={row['quantity']} "
            f"source={row['source']} meta={row['metadata']}")
    holdings = evidence.get("matching_timed_holdings") or []
    lines.append(f"Timed Holdings : {len(holdings)} row(s)")
    for h in holdings:
        lines.append(
            f"  - {h['entry_key']} {h['item_name']!r} role_id={h['role_id']} "
            f"expires_at={h['expires_at']} expired={h.get('expired')}")
    temp_roles = evidence.get("matching_temp_roles") or []
    if temp_roles:
        lines.append(f"Temp Roles     : {len(temp_roles)} row(s)")
        for tr in temp_roles:
            lines.append(
                f"  - id={tr['id']} role_id={tr['role_id']} "
                f"expires_at={tr['expires_at']} expired={tr['expired']}")
    eq = evidence.get("equipped_role")
    if eq:
        lines.append(
            f"Equipped Role  : entry_key={eq.get('entry_key')} "
            f"item_name={eq.get('item_name')!r} role_id={eq.get('role_id')}")
    return "\n".join(lines)


def _swap_db_path(target_path: str) -> dict:
    """Point database-backed modules at `target_path`, returning prior state."""
    import os
    import sys
    prior = {"env": os.environ.get("DATABASE_PATH"), "modules": {}}
    os.environ["DATABASE_PATH"] = target_path
    for mod_name in (
        "database",
        "utils.timed_ownership",
        "utils.economy_safe",
        "utils.ledger",
        "utils.equip_engine",
        "utils.inventory",
        "utils.reward_engine",
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "DB_PATH"):
            prior["modules"][mod_name] = getattr(mod, "DB_PATH")
            setattr(mod, "DB_PATH", target_path)
    return prior


def _restore_db_path(prior: dict) -> None:
    import os
    import sys
    if prior["env"] is None:
        os.environ.pop("DATABASE_PATH", None)
    else:
        os.environ["DATABASE_PATH"] = prior["env"]
    for mod_name, old_val in prior["modules"].items():
        mod = sys.modules.get(mod_name)
        if mod is not None:
            setattr(mod, "DB_PATH", old_val)


async def _cli_async(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description=(
            "Inspect and safely resolve uncertain/failed shop purchase "
            "operations (read-only by default). Note: this tool inspects "
            "SQLite tables only; live Discord role membership must be "
            "verified by the operator before resolving as 'delivered' or 'refund'."
        ))
    parser.add_argument(
        "--db", default=None,
        help="Path to the SQLite database file (or set DATABASE_PATH).")
    parser.add_argument(
        "--op-id", help="Specific purchase operation ID to inspect or resolve.")
    parser.add_argument(
        "--status", default=PURCHASE_UNCERTAIN,
        help="Filter operations by status when listing (default: uncertain).")
    parser.add_argument(
        "--guild-id", type=int, default=None,
        help="Filter operations by guild_id when listing.")
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Filter operations by user_id when listing.")
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max operations to list (default: 50).")
    parser.add_argument(
        "--resolve", choices=("refund", "delivered"), default=None,
        help="Resolution action for --op-id ('refund' or 'delivered').")
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm and execute the --resolve state transition.")
    parser.add_argument(
        "--note", default=None,
        help="Operator note recorded in the operation detail (required with --resolve --yes).")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON output.")
    args = parser.parse_args(argv)

    if args.resolve and not args.op_id:
        parser.error("--resolve requires --op-id.")

    target_db = (args.db or os.environ.get("DATABASE_PATH") or "").strip()
    if not target_db:
        msg = ("Refusing to run without an explicit database target: "
               "pass --db <path> or set DATABASE_PATH.")
        if args.json:
            print(json.dumps({"success": False, "error": msg}, indent=2))
        else:
            print(f"Error: {msg}")
        return 2

    if not os.path.isfile(target_db):
        msg = f"Database file does not exist: {target_db}"
        if args.json:
            print(json.dumps({"success": False, "error": msg}, indent=2))
        else:
            print(f"Error: {msg}")
        return 2

    prior_db = _swap_db_path(target_db)
    try:
        if not args.op_id:
            ops = await list_purchase_operations(
                status=args.status, guild_id=args.guild_id,
                user_id=args.user_id, limit=args.limit)
            if args.json:
                print(json.dumps({"operations": ops}, indent=2, default=str))
            elif not ops:
                print(f"No purchase operations found with status={args.status!r}.")
            else:
                print(f"Found {len(ops)} operation(s) with status={args.status!r}:")
                for op in ops:
                    print(
                        f"  {op['op_id']}  guild={op['guild_id']} "
                        f"user={op['user_id']} item={op['item_name']!r} "
                        f"amount={op['amount']} {op['currency']} "
                        f"charged={op['charged']} refunded={op['refunded']} "
                        f"reserved={op['stock_reserved']} "
                        f"returned={op['stock_returned']} "
                        f"detail={op.get('detail')!r}")
            return 0

        evidence = await inspect_purchase_operation(args.op_id)
        if evidence is None:
            if args.json:
                print(json.dumps(
                    {"error": f"Unknown operation: {args.op_id}"}, indent=2))
            else:
                print(f"Error: Unknown purchase operation {args.op_id!r}.")
            return 1

        if not args.resolve:
            if args.json:
                print(json.dumps(evidence, indent=2, default=str))
            else:
                print(format_purchase_inspection(evidence))
            return 0

        if not args.yes:
            msg = ("Refusing to mutate without --yes. Inspect the evidence above "
                   f"and re-run with --op-id {args.op_id} --resolve {args.resolve} "
                   "--yes --note '<reason>'.")
            if args.json:
                print(json.dumps(
                    {"success": False, "error": msg, "inspection": evidence},
                    indent=2, default=str))
            else:
                print(format_purchase_inspection(evidence))
                print(f"\n[DRY-RUN] {msg}")
            return 2

        if not (args.note and args.note.strip()):
            msg = ("Refusing to resolve without an operator --note recording "
                   "what was verified.")
            if args.json:
                print(json.dumps(
                    {"success": False, "error": msg, "inspection": evidence},
                    indent=2, default=str))
            else:
                print(f"Error: {msg}")
            return 2

        res = await resolve_purchase_operation(
            args.op_id, action=args.resolve, confirmed=True, note=args.note)
        if args.json:
            print(json.dumps(res, indent=2, default=str))
        else:
            if res["success"]:
                print(f"Resolved {args.op_id}: action={res['action']} -> status={res['status']}")
            else:
                print(f"Resolution failed for {args.op_id}: {res['error']}")
        return 0 if res["success"] else 1
    finally:
        _restore_db_path(prior_db)


def main(argv: list[str] | None = None) -> int:
    import asyncio
    return asyncio.run(_cli_async(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
