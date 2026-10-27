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
                              op_id: str | None = None,
                              db: aiosqlite.Connection | None = None) -> int:
    """Insert one timed holding. Call ONLY after delivery has succeeded.

    Each call creates its own record, even for an identical name: two grants
    with different expiry times are two independent holdings, and merging them
    would let one expiry destroy the other's remaining time.

    An optional caller-owned connection is joined, not committed — the same
    contract as `utils.inventory.give_item(..., db=)`.

    `op_id` records the Shop purchase operation that created this holding in
    `metadata["origin_op_id"]`. That marker is the only durable proof that a
    specific `h:<id>` key belongs to a specific operation: member, item name
    and product id are all shared when one member buys the same product twice.
    A late-delivery rollback refuses to retire a holding whose marker names a
    different operation, and refuses to touch one that records no operation at
    all (legacy rows) — those stay pending for an operator.
    """
    if not expires_at:
        raise ValueError("grant_timed_holding requires an explicit expires_at")
    if int(quantity) <= 0:
        raise ValueError("quantity must be positive")
    meta_payload = dict(metadata) if metadata else {}
    if op_id:
        meta_payload["origin_op_id"] = str(op_id)
    payload = json.dumps(meta_payload) if meta_payload else None
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


async def timed_holding_owner(entry_key) -> tuple[int, int] | None:
    """`(guild_id, user_id)` of an `h:<id>` holding, or None when it no longer exists.

    This is a MEMBER-level check only. It is NOT provenance: one member can hold
    several holdings (and several late-rollback markers) at once, so a matching
    owner says nothing about which purchase a key belongs to. Compensation paths
    must use `timed_holding_provenance()` (origin operation) or
    `_assert_retry_marker_owned()` (marker source + recorded operation) instead
    of treating this answer as proof.

    A holding that is already gone returns None (the rollback is idempotent,
    nothing left to retire).
    """
    key = str(entry_key or "")
    if not key.startswith("h:"):
        return None
    holding_id = entry_key_id(key)
    if holding_id is None:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT guild_id, user_id FROM timed_holdings WHERE id=?",
            (holding_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return int(row[0] or 0), int(row[1] or 0)


async def timed_holding_provenance(entry_key) -> dict | None:
    """Identity of an `h:<id>` holding, including the purchase that created it.

    Returns `{"guild_id", "user_id", "origin_op_id", "role_id"}` or None when
    the holding no longer exists. `origin_op_id` is the Shop purchase operation
    recorded in the holding's metadata at creation time
    (`grant_timed_holding(op_id=...)`), or None for holdings granted before that
    marker existed. `role_id` is the role the holding actually entitled, written
    to the row itself rather than taken from any caller-supplied descriptor.

    A holding's member is NOT enough to identify its purchase: one member can
    buy the same timed product repeatedly, and those holdings share guild,
    member, item name and product id. Only `origin_op_id` binds a holding to the
    operation that must be compensated. A holding without it (any row granted
    before this marker, and marks written by another grant path) cannot be
    reconciled automatically: the late-delivery rollback keeps the descriptor
    pending for manual recovery rather than expiring or deleting it. The
    purchase journal's `detail` is deliberately NOT accepted as a substitute —
    it is written only by the `pending -> delivered` transition, so an operation
    eligible for late compensation never carries it.
    """
    key = str(entry_key or "")
    if not key.startswith("h:"):
        return None
    holding_id = entry_key_id(key)
    if holding_id is None:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT guild_id, user_id, metadata, role_id FROM timed_holdings"
            " WHERE id=?",
            (holding_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        meta = json.loads(row[2]) if row[2] else {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    origin = meta.get("origin_op_id")
    return {
        "guild_id": int(row[0] or 0),
        "user_id": int(row[1] or 0),
        "origin_op_id": str(origin) if origin else None,
        # The role this holding actually entitles, as written at grant time.
        # `role_id` in a descriptor is NOT evidence; this column is.
        "role_id": int(row[3]) if row[3] is not None else None,
    }


async def _boosts_for_origin(conn, guild_id, user_id, op_id) -> list[int]:
    """Ids of live boosts this purchase operation created.

    `origin_op_id` is the only durable link between a `leveling_active_boosts`
    row and the purchase that granted it, so "is this operation compensated?"
    is answered by looking for rows that record it — not by asking whether the
    id a descriptor happens to cite still exists.
    """
    cursor = await conn.execute(
        "SELECT id FROM leveling_active_boosts"
        " WHERE guild_id=? AND user_id=? AND origin_op_id=?",
        (int(guild_id), int(user_id), str(op_id)),
    )
    return [int(r[0]) for r in await cursor.fetchall()]


async def _boost_origin_op(conn, boost_id) -> dict | None:
    """`{guild_id, user_id, origin_op_id}` for one `leveling_active_boosts` row.

    Returns None when the row no longer exists. `origin_op_id` is NULL for every
    row written by a non-Shop producer (level claim, potion, admin) and for rows
    written before the column existed; both are unprovable and must be left
    alone rather than deleted by a Shop compensation.
    """
    cursor = await conn.execute(
        "SELECT guild_id, user_id, origin_op_id FROM leveling_active_boosts"
        " WHERE id=?",
        (int(boost_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "guild_id": int(row[0] or 0),
        "user_id": int(row[1] or 0),
        "origin_op_id": str(row[2]) if row[2] else None,
    }


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


def _is_member_not_found(exc: BaseException) -> bool:
    """Return True only when Discord definitively reports the member is not in the guild (404 NotFound)."""
    try:
        import discord
        if isinstance(exc, discord.NotFound):
            return True
    except Exception:
        pass
    return False


def _is_role_not_found(exc: BaseException) -> bool:
    """Return True only when Discord definitively reports Unknown Role (404 NotFound, code 10011)."""
    try:
        import discord
        if isinstance(exc, discord.NotFound):
            return getattr(exc, "code", None) == 10011
    except Exception:
        pass
    return False


async def _resolve_guild_role(guild, role_id: int) -> tuple[object | None, bool, str | None]:
    """Resolve `role_id` on `guild`, distinguishing a confirmed deleted role from lookup failures or cache misses.

    Returns `(role_obj, role_deleted, lookup_error)`:
      * `(role, False, None)` when the role exists in the guild.
      * `(None, True, None)` when the role is confirmed deleted via Discord HTTP
        (`fetch_role` raising `discord.NotFound` with Unknown Role code `10011`,
        or `fetch_roles` returning a role list that omits `role_id`).
      * `(None, False, None)` when `guild.get_role(role_id)` is a cache miss and
        HTTP confirmation is unavailable/unconfirmed.
      * `(None, False, err_str)` when `guild` is unavailable, lacks `get_role`,
        or `get_role` / `fetch_role` / `fetch_roles` raised a lookup error
        (including Unknown Guild `10004`, permission error, timeout, rate limit,
        or transient API failure).
    """
    if guild is None or not hasattr(guild, "get_role"):
        return None, False, "guild role lookup unavailable"
    try:
        role = guild.get_role(int(role_id))
    except Exception as exc:
        return None, False, f"role lookup failed: {exc}"
    if role is not None:
        return role, False, None

    can_http_fetch = True
    try:
        import discord
        if isinstance(guild, discord.Guild):
            state = getattr(guild, "_state", None)
            if getattr(state, "http", None) is None:
                can_http_fetch = False
    except Exception:
        pass

    if can_http_fetch and hasattr(guild, "fetch_role"):
        try:
            role = await guild.fetch_role(int(role_id))
            if role is not None:
                return role, False, None
        except Exception as exc:
            if _is_role_not_found(exc):
                return None, True, None
            return None, False, f"role lookup failed: {exc}"

    if can_http_fetch and hasattr(guild, "fetch_roles"):
        try:
            roles_list = await guild.fetch_roles()
        except Exception as exc:
            return None, False, f"role lookup failed: {exc}"
        if roles_list is not None:
            for candidate in roles_list:
                try:
                    if int(getattr(candidate, "id", 0)) == int(role_id):
                        return candidate, False, None
                except (TypeError, ValueError):
                    continue
            return None, True, None

    return None, False, None


async def _late_rollback_prereqs_complete(
    op_id: str | None,
    *,
    entry_key: str | None = None,
) -> bool:
    """Return False if a linked late-delivery rollback still needs inventory or equip compensation,
    or if required rollback state is unreadable or malformed."""
    spec = None
    if op_id:
        try:
            op_row = await get_purchase(str(op_id))
        except Exception:
            return False
        mem_spec = _PENDING_DELIVERY_ROLLBACKS.get(str(op_id))
        if mem_spec is not None and not isinstance(mem_spec, dict):
            return False
        if op_row is None:
            if not isinstance(mem_spec, dict):
                return False
        else:
            raw = (
                op_row.get("_raw_delivery_rollback")
                if "_raw_delivery_rollback" in op_row
                else op_row.get("delivery_rollback")
            )
            if raw is not None and raw != "":
                try:
                    parsed = json.loads(raw)
                except Exception:
                    return False
                if not isinstance(parsed, dict):
                    return False
                spec = dict(parsed)
        if isinstance(mem_spec, dict):
            if isinstance(spec, dict):
                merged = dict(spec)
                for flag in ("inventory_rolled_back", "equip_reconciled", "role_reconciled"):
                    if mem_spec.get(flag):
                        merged[flag] = True
                spec = merged
            else:
                spec = dict(mem_spec)
    elif entry_key:
        for mem_val in _PENDING_DELIVERY_ROLLBACKS.values():
            if not isinstance(mem_val, dict):
                return False
            if (
                mem_val.get("entry_key") == entry_key
                or mem_val.get("retry_entry_key") == entry_key
            ):
                spec = dict(mem_val)
                break
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT guild_id, user_id, item_name, delivery_rollback "
                    "FROM shop_purchase_operations "
                    "WHERE delivery_rollback IS NOT NULL AND delivery_rollback != ''"
                )
                corrupt_rows: list[tuple[int | None, int | None, str | None]] = []
                matched_db_spec = None
                for row_gid, row_uid, row_iname, raw in await cur.fetchall():
                    try:
                        cand = json.loads(raw)
                    except Exception:
                        corrupt_rows.append((row_gid, row_uid, row_iname))
                        continue
                    if not isinstance(cand, dict):
                        corrupt_rows.append((row_gid, row_uid, row_iname))
                        continue
                    # A parseable object that cannot be executed (empty object,
                    # missing identity, wrong field types, unknown branch) is
                    # just as unusable as unparseable JSON: never let it retire
                    # a marker by looking like "nothing pending".
                    usable, _problem = _validate_delivery_rollback_spec(cand)
                    if not usable:
                        corrupt_rows.append((row_gid, row_uid, row_iname))
                        continue
                    if (
                        cand.get("entry_key") == entry_key
                        or cand.get("retry_entry_key") == entry_key
                    ):
                        matched_db_spec = cand
                        break
                if isinstance(matched_db_spec, dict):
                    # This marker's own operation is intact and readable; a
                    # corrupt descriptor belonging to a *different* operation
                    # must not block retiring it.
                    if isinstance(spec, dict):
                        merged = dict(matched_db_spec)
                        for flag in ("inventory_rolled_back", "equip_reconciled", "role_reconciled"):
                            if spec.get(flag) or matched_db_spec.get(flag):
                                merged[flag] = True
                        spec = merged
                    else:
                        spec = dict(matched_db_spec)
                elif corrupt_rows:
                    # Some stored descriptor is unreadable/malformed. Only allow
                    # cleanup when this marker can be positively resolved to an
                    # operation whose own descriptor is intact.
                    hid = entry_key_id(entry_key)
                    h_row = None
                    if hid is not None and str(entry_key).startswith("h:"):
                        h_cur = await db.execute(
                            "SELECT guild_id, user_id, item_name, metadata "
                            "FROM timed_holdings WHERE id=?",
                            (int(hid),),
                        )
                        h_row = await h_cur.fetchone()
                    if h_row is None:
                        return False
                    h_gid, h_uid, h_iname, h_meta_raw = h_row
                    h_op_id = None
                    if h_meta_raw:
                        try:
                            h_meta = json.loads(h_meta_raw)
                        except Exception:
                            return False
                        if isinstance(h_meta, dict):
                            h_op_id = h_meta.get("op_id")
                    if h_op_id:
                        op_cur = await db.execute(
                            "SELECT delivery_rollback FROM shop_purchase_operations "
                            "WHERE op_id=?",
                            (str(h_op_id),),
                        )
                        op_linked = await op_cur.fetchone()
                        if not op_linked:
                            return False
                        linked_raw = op_linked[0]
                        if linked_raw == "":
                            # Durable completion sentinel: nothing pending.
                            return True
                        if linked_raw is None:
                            # No durable descriptor: only an explicitly marked
                            # failed-persistence retry may stand in for it.
                            mem_linked = _PENDING_DELIVERY_ROLLBACKS.get(
                                str(h_op_id)
                            )
                            if (
                                not isinstance(mem_linked, dict)
                                or not mem_linked.get("_durable_unpersisted")
                            ):
                                return False
                            if spec is None:
                                spec = dict(mem_linked)
                        else:
                            try:
                                linked_parsed = json.loads(linked_raw)
                            except Exception:
                                return False
                            if not isinstance(linked_parsed, dict):
                                return False
                            if spec is None:
                                spec = dict(linked_parsed)
                    else:
                        for c_gid, c_uid, c_iname in corrupt_rows:
                            if (
                                int(c_gid or 0) == int(h_gid or 0)
                                and int(c_uid or 0) == int(h_uid or 0)
                                and (
                                    not c_iname
                                    or not h_iname
                                    or str(c_iname) == str(h_iname)
                                )
                            ):
                                return False
        except Exception:
            return False
    if isinstance(spec, dict):
        # A descriptor that cannot be executed (empty object, missing identity,
        # wrong type, unknown branch) must never be retired by cleanup just
        # because no usable specification could be resolved: keep the marker
        # and its row for diagnosis and manual recovery.
        valid, _problem = _validate_delivery_rollback_spec(spec)
        if not valid:
            return False
        if not spec.get("inventory_rolled_back") or not spec.get("equip_reconciled"):
            return False
    return True


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

    member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
    member_departed = False
    if member is None and hasattr(guild, "fetch_member"):
        try:
            member = await guild.fetch_member(user_id)
        except Exception as exc:
            if _is_member_not_found(exc):
                member = None
                member_departed = True
            else:
                err = f"member lookup failed: {exc}"
                return {"status": "kept", "reason": err, "error": err}
    meta = holding.get("metadata") if isinstance(holding.get("metadata"), dict) else {}
    is_late_rollback = bool(
        meta.get("late_rollback") or holding.get("source") == "shop_late_rollback"
    )
    linked_op_id = meta.get("op_id")
    if is_late_rollback or linked_op_id:
        if not await _late_rollback_prereqs_complete(linked_op_id, entry_key=entry_key):
            err = "inventory or equip rollback pending"
            return {"status": "kept", "reason": err, "error": err}
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
        if linked_op_id:
            _PENDING_DELIVERY_ROLLBACKS.pop(str(linked_op_id), None)
            try:
                await update_purchase(str(linked_op_id), delivery_rollback="")
            except Exception:
                pass
        return {"status": "removed", "reason": f"role retained ({reason})"}

    role, role_deleted, role_lookup_err = await _resolve_guild_role(guild, int(role_id))
    if role_lookup_err is not None:
        return {"status": "kept", "reason": role_lookup_err, "error": role_lookup_err}
    if is_late_rollback and not member_departed and not role_deleted and (
        member is None or role is None or not hasattr(member, "remove_roles")
    ):
        err = "member or role unavailable for late role rollback"
        return {"status": "kept", "reason": err, "error": err}
    if role_deleted and member is not None and isinstance(getattr(member, "roles", None), list):
        member.roles[:] = [
            r for r in member.roles
            if getattr(r, "id", None) != int(role_id)
        ]
    member_roles = getattr(member, "roles", None) or ()
    if (
        not member_departed
        and not role_deleted
        and member is not None
        and role is not None
        and (is_late_rollback or role in member_roles)
    ):
        try:
            await member.remove_roles(role, reason="Timed entitlement expired")
            if isinstance(getattr(member, "roles", None), list) and role in member.roles:
                member.roles.remove(role)
        except Exception as exc:
            # Keep the row: it is the only marker that this removal is pending.
            return {"status": "kept", "reason": f"Discord removal failed: {exc}"}

    # Bookkeeping BEFORE the row is deleted: `clear_equip_for_holding` is
    # idempotent, so a crash after this point simply retries, whereas deleting
    # the holding first would leave an equipped slot pointing at nothing with
    # only this row left to remember it by.
    if not is_late_rollback:
        try:
            await clear_equip_for_holding(guild_id, user_id, entry_key,
                                          role_id=role_id)
        except Exception as exc:  # pragma: no cover - defensive
            err = f"equip bookkeeping cleanup failed: {exc}"
            return {"status": "kept", "reason": err, "error": err}

    await delete_timed_holding(entry_key)
    if linked_op_id:
        _PENDING_DELIVERY_ROLLBACKS.pop(str(linked_op_id), None)
        try:
            await update_purchase(str(linked_op_id), delivery_rollback="")
        except Exception:
            pass
    return {
        "status": "removed",
        "reason": (
            "member departed"
            if member_departed
            else ("role deleted" if role_deleted else "expired")
        ),
    }


async def cleanup_expired_holdings(bot, *, limit: int = 200) -> dict:
    """One cleanup pass. Safe to re-run at any time."""
    global _LAST_BOT
    if bot is not None:
        _LAST_BOT = bot
    removed = kept = 0
    errors: list[str] = []
    try:
        rb_stats = await reconcile_delivery_rollbacks(
            bot=bot, limit=limit, handoff_to_holdings=True
        )
        if rb_stats.get("errors"):
            kept += len(rb_stats["errors"])
            errors.extend(rb_stats["errors"])
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"delivery_rollback: {exc}")
        kept += 1

    def _is_late_marker(h: dict) -> bool:
        meta = h.get("metadata") if isinstance(h.get("metadata"), dict) else {}
        return bool(meta.get("late_rollback") or h.get("source") == "shop_late_rollback")

    batch = await expired_holdings(limit=limit)
    seen_ids = {h.get("id") for h in batch}
    late_kept = 0
    for holding in batch:
        try:
            result = await expire_one(bot, holding)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{holding.get('entry_key')}: {exc}")
            kept += 1
            if _is_late_marker(holding):
                late_kept += 1
            continue
        if result["status"] == "removed":
            removed += 1
        else:
            kept += 1
            if _is_late_marker(holding):
                late_kept += 1
            if result.get("error"):
                errors.append(f"{holding.get('entry_key')}: {result['error']}")

    if limit is not None and int(limit) > 1 and late_kept > 0 and len(batch) >= int(limit):
        ordinary_in_batch = sum(1 for h in batch if not _is_late_marker(h))
        needed_ordinary = max(0, int(limit) - ordinary_in_batch)
        if needed_ordinary > 0:
            all_expired = await expired_holdings(limit=None)
            extra_ordinary = [
                h for h in all_expired
                if h.get("id") not in seen_ids and not _is_late_marker(h)
            ][:needed_ordinary]
            for holding in extra_ordinary:
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

_LAST_BOT = None
_PENDING_DELIVERY_ROLLBACKS: dict[str, dict] = {}
# Per-process attempt counter used only to keep the limited reconciliation
# window fair. Durable ordering (`updated_at`) remains the cross-process signal.
_ROLLBACK_SCAN_ATTEMPTS: dict[str, int] = {}

PURCHASE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_purchase_operations (
        op_id             TEXT PRIMARY KEY,
        guild_id          INTEGER NOT NULL,
        user_id           INTEGER NOT NULL,
        item_id           INTEGER,
        item_name         TEXT,
        currency          TEXT,
        amount            INTEGER NOT NULL DEFAULT 0,
        stock_reserved    INTEGER NOT NULL DEFAULT 0,
        charged           INTEGER NOT NULL DEFAULT 0,
        refunded          INTEGER NOT NULL DEFAULT 0,
        stock_returned    INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL,
        detail            TEXT,
        history_id        INTEGER,
        delivery_rollback TEXT,
        created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    ("delivery_rollback", "TEXT"),
    # Durable retry fairness for late-delivery rollback candidates. A process
    # restart clears `_ROLLBACK_SCAN_ATTEMPTS`, so the bounded reconciliation
    # window needs a persisted attempt count to rotate away from rows that keep
    # failing closed instead of re-selecting them forever.
    ("delivery_rollback_attempts", "INTEGER NOT NULL DEFAULT 0"),
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


_UNSET = object()


async def update_purchase(op_id: str, *, status: str | None = None,
                          detail: str | None = None,
                          stock_reserved: int | None = None,
                          charged: int | None = None,
                          refunded: int | None = None,
                          stock_returned: int | None = None,
                          history_id: int | None = None,
                          delivery_rollback: str | None = None,
                          expect_delivery_rollback=_UNSET,
                          db: aiosqlite.Connection | None = None) -> None:
    """Update one purchase operation. Returns True when exactly one row matched.

    Writing a non-empty `delivery_rollback` is conditionally guarded in SQL: it
    never overwrites the empty-string completed sentinel, so a stale invocation
    cannot turn a finished rollback back into pending work. Writing `""` (the
    sentinel itself) is unconditional and always wins.

    `expect_delivery_rollback` adds a compare-and-swap condition on the exact
    durable value the caller merged from (`None` for SQL NULL, the sentinel for
    `''`, the raw JSON string otherwise). A non-empty descriptor therefore
    cannot overwrite a *different* non-empty descriptor that landed in another
    process between the read and this write: the write matches 0 rows and the
    caller re-reads and re-merges (`_persist_delivery_rollback_progress`).
    """
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
    if delivery_rollback is not None:
        sets.append("delivery_rollback=?")
        params.append(delivery_rollback)
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
    completed_guard = ""
    if delivery_rollback not in (None, ""):
        # Database-enforced guard: a non-empty descriptor write must never
        # overwrite the durable completed sentinel (`''`). An invocation that
        # lost the race gets 0 rows back instead of resurrecting finished work.
        # Initial creation from SQL NULL still matches (only the empty-string
        # sentinel is refused), and progress updates over a live descriptor are
        # unaffected.
        completed_guard = (
            " AND (delivery_rollback IS NULL OR delivery_rollback != '')"
        )
    if expect_delivery_rollback is not _UNSET:
        # Null-safe equality (`IS`) so an expected SQL NULL is expressible.
        completed_guard += " AND delivery_rollback IS ?"
        params.append(expect_delivery_rollback)
    sql = (
        f"UPDATE shop_purchase_operations SET {', '.join(sets)} "
        f"WHERE op_id=?{where_extra}{completed_guard}"
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
        try:
            cursor = await db.execute(
                "SELECT op_id, guild_id, user_id, item_id, item_name, currency,"
                " amount, stock_reserved, charged, refunded, stock_returned,"
                " status, detail, history_id, delivery_rollback,"
                " created_at, updated_at"
                " FROM shop_purchase_operations WHERE op_id=?", (op_id,))
            row = await cursor.fetchone()
            keys = ("op_id", "guild_id", "user_id", "item_id", "item_name",
                    "currency", "amount", "stock_reserved", "charged",
                    "refunded", "stock_returned", "status", "detail",
                    "history_id", "delivery_rollback", "created_at", "updated_at")
        except Exception as exc:
            if "no such column" not in str(exc).lower():
                raise
            cursor = await db.execute(
                "SELECT op_id, guild_id, user_id, item_id, item_name, currency,"
                " amount, stock_reserved, charged, refunded, stock_returned,"
                " status, detail, history_id, created_at, updated_at"
                " FROM shop_purchase_operations WHERE op_id=?", (op_id,))
            row = await cursor.fetchone()
            keys = ("op_id", "guild_id", "user_id", "item_id", "item_name",
                    "currency", "amount", "stock_reserved", "charged",
                    "refunded", "stock_returned", "status", "detail",
                    "history_id", "created_at", "updated_at")
    if not row:
        return None
    res = dict(zip(keys, row))
    raw_dr = res.get("delivery_rollback")
    res["_raw_delivery_rollback"] = raw_dr
    if not raw_dr:
        res["delivery_rollback"] = None
    return res


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
                              op_id: str | None = None,
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
            product_id=product_id, category=category, role_id=role_id,
            op_id=op_id)
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
            "expires_at": expires_at, "role_added": role_added,
            "error": None, "uncertain": False}


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


async def record_pending_delivery_rollback(
    op_id: str,
    spec: dict,
    *,
    bot=None,
    db: aiosqlite.Connection | None = None,
) -> bool:
    """Persist a durable late-delivery rollback descriptor on the purchase row.

    Returns True when the descriptor was durably written to
    `shop_purchase_operations` in SQLite (or staged on the caller's open `db`
    transaction), or when the operation is already durably complete (the
    guarded write is then rejected and there is nothing left to persist).
    Always updates the in-memory fallback map for current-process retries, but
    never treats the in-memory copy as durable persistence.
    """
    global _LAST_BOT
    if bot is not None:
        _LAST_BOT = bot
    clean_spec = {k: v for k, v in spec.items() if not str(k).startswith("_")}
    copied = dict(clean_spec)
    _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = copied
    status, persist_err, merged = await _persist_delivery_rollback_progress(
        str(op_id), clean_spec, db=db
    )
    if merged is not None:
        _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = dict(merged)
        copied = _PENDING_DELIVERY_ROLLBACKS[str(op_id)]
    if status == "persisted":
        copied.pop("_durable_unpersisted", None)
        return True
    if status == "completed":
        # The durable `''` sentinel means another invocation already finished
        # this rollback; there is nothing left to persist and the caller's spec
        # must never resurrect the descriptor.
        _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
        return True
    copied["_durable_unpersisted"] = True
    print(
        f"[SHOP] Failed to persist durable delivery_rollback descriptor "
        f"for op_id={op_id}: {persist_err or status}"
    )
    if db is not None and status == "failed":
        # The caller stages this write inside an open transaction: raising lets
        # it roll that transaction back instead of committing a delivery whose
        # rollback descriptor was never recorded.
        raise RuntimeError(persist_err or "descriptor write failed")
    return False


def _resolve_guild_and_live_member(
    guild_id: int,
    user_id: int,
    *,
    bot=None,
    interaction=None,
):
    """Resolve the guild and live cached guild Member (preferring guild.get_member over interaction.user)."""
    guild_obj = None
    if bot is not None and hasattr(bot, "get_guild"):
        try:
            guild_obj = bot.get_guild(int(guild_id))
        except Exception:
            guild_obj = None
    if guild_obj is None and interaction is not None:
        guild_obj = getattr(interaction, "guild", None)

    target_member = None
    if guild_obj is not None and hasattr(guild_obj, "get_member"):
        try:
            target_member = guild_obj.get_member(int(user_id))
        except Exception:
            target_member = None
    return guild_obj, target_member


async def _durable_delivery_rollback_completed(op_id: str) -> bool:
    """True when the purchase row durably records a completed rollback.

    `update_purchase(..., delivery_rollback="")` stores the empty-string
    sentinel (distinct from SQL `NULL`, which means "no descriptor recorded")
    once every compensation step has finished. Any invocation still holding a
    stale specification must treat that state as authoritative.
    """
    try:
        op_row = await get_purchase(str(op_id))
    except Exception:
        return False
    if not op_row:
        return False
    raw = (
        op_row.get("_raw_delivery_rollback")
        if "_raw_delivery_rollback" in op_row
        else op_row.get("delivery_rollback")
    )
    return raw == ""


class _LateRollbackAlreadyCompleted(Exception):
    """Raised internally when the durable descriptor records a finished rollback."""


# ── Durable rollback descriptor: classification, validation, monotonic writes ─

_ROLLBACK_PROGRESS_FLAGS = (
    "inventory_rolled_back",
    "equip_reconciled",
    "role_reconciled",
)

# Identity/provenance fields of a descriptor. Once durably recorded they must
# never be erased or replaced by a stale caller that is missing the data, so the
# merge keeps the stored value and only fills holes from the incoming spec.
_ROLLBACK_IDENTITY_FIELDS = (
    "retry_entry_key",
    "boost_id",
    "boost_expires_at",
    "boost_source",
    "prior_inventory",
    "delivered_inventory",
)

# Diagnostic fields. Unlike the identity fields, these describe the LATEST
# attempt rather than the grant, so the newest observation wins: an operator
# reading a pending descriptor must see why the most recent retry refused, not
# the first failure that ever happened.
_ROLLBACK_DIAGNOSTIC_FIELDS = ("last_error", "last_error_at")

# Products delivered as a stack of `inventory_items` units (no holding row).
# Their rollback must be able to identify the unit THIS purchase delivered;
# without that evidence the only thing a rollback could do is remove a
# same-named unit that may have been granted independently.
_STACKABLE_ROLLBACK_TYPES = ("potion", "title", "custom", "shop_custom", "item")

_ROLLBACK_SUPPORTED_TYPES = (
    "role",
    "temp_role",
    "xp_boost",
    "potion",
    "title",
    "custom",
    "shop_custom",
    "item",
)


def _delivery_rollback_state(raw) -> tuple[str, dict | None]:
    """Classify one durable `delivery_rollback` value.

    Returns `(state, spec)` where state is one of:

    * `"completed"` — the empty-string sentinel, the ONLY value that means the
      rollback finished and was cleared;
    * `"absent"` — SQL NULL or a blank string (no descriptor recorded);
    * `"live"` — a non-empty JSON object (a usable descriptor);
    * `"malformed"` — unparseable JSON, a non-object, or an empty object `{}`,
      which carries no identity and cannot be executed.
    """
    if raw == "":
        return "completed", None
    if raw is None or not str(raw).strip():
        return "absent", None
    try:
        parsed = json.loads(raw)
    except Exception:
        return "malformed", None
    if not isinstance(parsed, dict) or not parsed:
        return "malformed", None
    return "live", parsed


_ENTRY_KEY_PREFIXES = ("h:", "i:")


def _canonical_entry_key_id(key) -> int | None:
    """Parse `h:<id>` / `i:<id>` and return the id only for the canonical form.

    `int()` is far too permissive for a value that is compared as a STRING
    against `timed_holdings.entry_key`/`equipped_roles.entry_key`: it accepts
    `"+1"`, `" 1"`, `"01"`, `"1_0"` (-> 10) and non-ASCII digits such as `"١"`
    (-> 1). Every one of those describes holding 1 while comparing unequal to
    the stored `"h:1"`, so a rollback could retire the holding and leave the
    equipped slot dangling on it. Only the round-trip form is accepted.

    Returns None for a malformed or non-canonical key, and for anything that is
    not `h:`/`i:`.
    """
    if not isinstance(key, str):
        return None
    for prefix in _ENTRY_KEY_PREFIXES:
        if key.startswith(prefix):
            suffix = key[len(prefix):]
            break
    else:
        return None
    if not suffix or not suffix.isascii() or not suffix.isdigit():
        return None
    try:
        parsed = int(suffix)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or str(parsed) != suffix:
        return None
    return parsed


def _bind_descriptor_to_operation(spec: dict,
                                  op_row: dict) -> tuple[bool, str | None]:
    """Reconcile a descriptor's target with the purchase operation's own row.

    This is the single ownership boundary for late-delivery compensation. Every
    branch mutates state addressed by `spec["guild_id"]`/`spec["user_id"]`, so
    those two values decide WHOSE inventory, holdings, boosts and Discord roles
    are touched. They are only ever legitimate when they are the operation's own
    columns: a descriptor that is internally consistent but names another member
    or another guild is not a rollback of this purchase, it is a request to
    modify someone else's state.

    `item_id` is checked only when both sides carry one, because it is not used
    to target any mutation (see `_validate_delivery_rollback_spec`) and older
    descriptors legitimately recorded it as absent.

    Returns `(True, None)` when the descriptor belongs to this operation, or
    `(False, reason)` so the caller can fail closed with a durable diagnostic.
    """
    if not isinstance(op_row, dict):
        return False, "purchase operation row is unavailable"
    for field in ("guild_id", "user_id"):
        try:
            descriptor_value = int(spec.get(field))
        except (TypeError, ValueError):
            return False, f"descriptor has no usable {field!r}"
        try:
            operation_value = int(op_row.get(field))
        except (TypeError, ValueError):
            return False, f"purchase operation has no usable {field!r}"
        if descriptor_value != operation_value:
            return False, (
                f"descriptor targets {field}={descriptor_value}, but purchase "
                f"operation {op_row.get('op_id')} belongs to "
                f"{field}={operation_value}; refusing to modify another "
                f"operation's state"
            )
    descriptor_item = spec.get("item_id")
    operation_item = op_row.get("item_id")
    if descriptor_item is not None and operation_item is not None:
        try:
            if int(descriptor_item) != int(operation_item):
                return False, (
                    f"descriptor names item_id={int(descriptor_item)}, but the "
                    f"purchase operation recorded item_id={int(operation_item)}"
                )
        except (TypeError, ValueError):
            return False, "descriptor has no usable 'item_id'"
    return True, None


def _validate_delivery_rollback_spec(spec) -> tuple[bool, str | None]:
    """Validate a descriptor before ANY side effect or completion claim.

    A descriptor that is not executable must be reported, never treated as
    finished and never partially guessed at; the caller leaves it in place for
    diagnosis and manual recovery.
    """
    if not isinstance(spec, dict):
        return False, "descriptor is not a JSON object"
    if not spec:
        return False, "descriptor is an empty object"
    for field in ("guild_id", "user_id"):
        if spec.get(field) is None:
            return False, f"missing required field {field!r}"
        try:
            int(spec[field])
        except (TypeError, ValueError):
            return False, f"invalid {field!r}"
    item_name = spec.get("item_name")
    if not isinstance(item_name, str) or not item_name.strip():
        return False, "missing required field 'item_name'"
    internal_type = spec.get("internal_type")
    if not isinstance(internal_type, str) or not internal_type.strip():
        return False, "missing required field 'internal_type'"
    if internal_type not in _ROLLBACK_SUPPORTED_TYPES:
        return False, f"unknown internal_type {internal_type!r}"
    for flag in _ROLLBACK_PROGRESS_FLAGS:
        if flag in spec and not isinstance(spec[flag], (bool, int)):
            return False, f"invalid progress flag {flag!r}"
    if spec.get("role_id") is not None:
        try:
            int(spec["role_id"])
        except (TypeError, ValueError):
            return False, "invalid 'role_id'"
    if "role_added" in spec and not isinstance(spec["role_added"], (bool, int)):
        return False, "invalid 'role_added'"
    entry_key = spec.get("entry_key")
    if entry_key is not None:
        if not isinstance(entry_key, str):
            return False, "invalid 'entry_key'"
        if entry_key.startswith(_ENTRY_KEY_PREFIXES):
            if _canonical_entry_key_id(entry_key) is None:
                return False, (
                    f"non-canonical 'entry_key' {entry_key!r} holding key; "
                    f"expected the exact form 'h:<holding id>' or "
                    f"'i:<inventory row id>'"
                )
    retry_entry_key = spec.get("retry_entry_key")
    if retry_entry_key is not None:
        if not isinstance(retry_entry_key, str):
            return False, "invalid 'retry_entry_key'"
        # A retry marker is always a timed holding, and it is later compared as
        # a string against stored keys, so it must be canonical too.
        if _canonical_entry_key_id(retry_entry_key) is None or not \
                retry_entry_key.startswith("h:"):
            return False, "invalid 'retry_entry_key' holding key"
    for field in _ROLLBACK_DIAGNOSTIC_FIELDS:
        if field in spec and spec[field] is not None and not isinstance(
                spec[field], str):
            return False, f"invalid {field!r}"
    for field in ("prior_inventory", "delivered_inventory"):
        value = spec.get(field)
        if value is not None and not isinstance(value, (list, tuple)):
            return False, f"invalid {field!r}"
    if internal_type == "temp_role":
        # A timed role is delivered as a `timed_holdings` row and identified by
        # `h:<holding id>` (`grant_timed_product`). Without that key the
        # compensation cannot expire/retire the holding, and the descriptor would
        # otherwise reach no branch at all.
        holding_key = spec.get("entry_key")
        holding_id = _canonical_entry_key_id(holding_key)
        if holding_id is None or not str(holding_key).startswith("h:"):
            return False, (
                "temp_role descriptor without a timed-holding 'entry_key'"
            )
    if internal_type in _STACKABLE_ROLLBACK_TYPES:
        # Two legitimate shapes, and no third:
        #   * a timed product (`entry_key` = `h:<holding id>`), rolled back by
        #     retiring its holding — its descriptor has no inventory snapshot;
        #   * a stackable grant, identified by the `delivered_inventory`
        #     snapshot `give_reward()` returned at delivery time.
        # A descriptor with neither would fall through to the stackable branch
        # and remove whatever unit happens to share the item name — possibly an
        # independent grant — while leaving a live holding in place. Keep it
        # pending for diagnosis instead of guessing.
        holding_key = spec.get("entry_key")
        holding_backed = (
            isinstance(holding_key, str) and holding_key.startswith("h:")
        )
        if not holding_backed:
            delivered = spec.get("delivered_inventory")
            if not isinstance(delivered, (list, tuple)) or len(delivered) < 2:
                return False, (
                    f"{internal_type} descriptor has neither a timed-holding "
                    f"'entry_key' nor a delivered-inventory snapshot"
                )
    if internal_type == "xp_boost":
        boost_id = spec.get("boost_id")
        if boost_id is not None:
            # A row id, not a truthy value: `True`/`1.7`/`"1"` all coerce to a
            # plausible number and would silently address whatever row happens
            # to carry that id.
            if isinstance(boost_id, bool) or not isinstance(boost_id, int):
                return False, "invalid 'boost_id'"
            if boost_id <= 0:
                return False, "invalid 'boost_id'"
        boost_exp = spec.get("boost_expires_at")
        if boost_exp is not None and not isinstance(boost_exp, str):
            return False, "invalid 'boost_expires_at'"
        if boost_id is None and not (
                isinstance(boost_exp, str) and boost_exp.strip()):
            return False, "xp_boost descriptor records no boost identity"
    return True, None


def _rollback_value_absent(value) -> bool:
    """True for a value that carries no durable information."""
    return value is None or value == "" or value == [] or value == {}


def _merge_delivery_rollback_specs(stored: dict, incoming: dict) -> dict:
    """Merge a newer durable descriptor with a (possibly stale) caller spec.

    Monotonic by construction:

    * a progress flag (`_ROLLBACK_PROGRESS_FLAGS`) that is durably `True` is
      never turned back into `False`;
    * a recorded identity/provenance value (`_ROLLBACK_IDENTITY_FIELDS`, e.g.
      `retry_entry_key`, `boost_id`) is never erased by missing/`None` incoming
      data, and a conflicting incoming identity never replaces it;
    * a field the durable descriptor lacks is filled in from the incoming spec.

    Every other key follows the same "keep the durable value unless it is
    absent" rule, so an unknown extra field can never silently drop durable
    state. This is a merge of *this operation's own* descriptor only — identity
    is separately validated against the operation by
    `_validate_delivery_rollback_spec`.
    """
    merged = dict(stored)
    for flag in _ROLLBACK_PROGRESS_FLAGS:
        merged[flag] = bool(stored.get(flag)) or bool(incoming.get(flag))
    for field in _ROLLBACK_IDENTITY_FIELDS:
        if _rollback_value_absent(merged.get(field)) and not _rollback_value_absent(
                incoming.get(field)):
            merged[field] = incoming[field]
    for field in _ROLLBACK_DIAGNOSTIC_FIELDS:
        if not _rollback_value_absent(incoming.get(field)):
            merged[field] = incoming[field]
    for field, value in incoming.items():
        if field in _ROLLBACK_PROGRESS_FLAGS or field in _ROLLBACK_IDENTITY_FIELDS:
            continue
        if field in _ROLLBACK_DIAGNOSTIC_FIELDS:
            continue
        if _rollback_value_absent(merged.get(field)):
            merged[field] = value
    return merged


async def _read_delivery_rollback_raw(op_id: str, *, db=None):
    """Return `(state, raw_value, spec)` for one purchase operation."""

    async def _query(connection):
        cursor = await connection.execute(
            "SELECT delivery_rollback FROM shop_purchase_operations WHERE op_id=?",
            (str(op_id),),
        )
        return await cursor.fetchone()

    if db is not None:
        row = await _query(db)
    else:
        async with aiosqlite.connect(DB_PATH) as connection:
            row = await _query(connection)
    if row is None:
        return "missing", None, None
    state, spec = _delivery_rollback_state(row[0])
    return state, row[0], spec


async def _persist_delivery_rollback_progress(op_id: str, spec: dict, *,
                                              db: aiosqlite.Connection | None = None,
                                              attempts: int = 4):
    """Monotonically and concurrency-safely persist rollback progress.

    Shared by every descriptor writer (`record_pending_delivery_rollback`,
    `_persist_incomplete`, `_persist_handoff` and the compensation branches), so
    no writer can regress newer progress written by another one.

    Returns `(status, error, merged_spec)` with status one of:

    * `"persisted"` — the merged descriptor is durably stored;
    * `"completed"` — the durable `''` sentinel already won; nothing written;
    * `"malformed"` — the stored value is not a usable descriptor, so writing
      would destroy diagnostic evidence; nothing written;
    * `"failed"` — the row is missing, the read failed, or the compare-and-swap
      write kept losing to concurrent writers. Nothing is claimed as durable.

    The read/merge/write sequence runs as a compare-and-swap against the exact
    value it merged from, and re-reads when another process (or another
    connection of this one) advanced the descriptor in between. A caller that
    already holds a `BEGIN IMMEDIATE` lock passes its `db` so the read and the
    write are part of that transaction.
    """
    clean = {k: v for k, v in spec.items() if not str(k).startswith("_")}
    last_error = None
    for _ in range(max(1, int(attempts))):
        try:
            state, raw, stored = await _read_delivery_rollback_raw(op_id, db=db)
        except Exception as exc:
            return "failed", f"failed to read durable descriptor: {exc}", None
        if state == "completed":
            return "completed", None, None
        if state == "missing":
            return "failed", f"purchase operation {op_id} not found", None
        if state == "malformed":
            return "malformed", (
                "stored delivery_rollback descriptor is not a usable JSON object"
            ), None
        merged = _merge_delivery_rollback_specs(stored or {}, clean)
        try:
            written = await update_purchase(
                str(op_id),
                delivery_rollback=json.dumps(merged),
                expect_delivery_rollback=raw,
                db=db,
            )
        except Exception as exc:
            return "failed", str(exc), None
        if written:
            return "persisted", None, merged
        last_error = "descriptor write lost a race with a newer durable value"
        if db is not None:
            # Inside the caller's transaction nothing else can write between our
            # read and this update, so a zero-row result means the row is gone
            # or the completion guard refused the write.
            break
    return "failed", last_error, None


async def _role_entry_key_conflict(entry_key, delivered_inv) -> str | None:
    """Why a permanent-role descriptor's `entry_key` cannot be trusted, or None.

    The producer records the equipped slot for a permanent role as
    `i:<delivered row id>` (`utils/inventory.py` builds `i:<id>` for every
    inventory row and `equip_role()` stores that same key), so the descriptor's
    key is only meaningful when it IS the delivered row. `late_key` is later
    handed to `reconcile_equip_for_expired_holding`, which acts as soon as the
    value equals the stored slot key — meaning a conflicting `i:<other row>`
    clears an unrelated equipped slot.
    """
    if entry_key is None:
        return None
    parsed = _canonical_entry_key_id(entry_key)
    if parsed is None or not str(entry_key).startswith("i:"):
        return (
            f"entry_key {entry_key!r} is not a canonical inventory key "
            f"'i:<row id>', which is what a permanent role records"
        )
    if delivered_inv is None:
        return (
            "descriptor has no delivered inventory snapshot, so entry_key "
            f"{entry_key!r} cannot be verified against the grant"
        )
    try:
        delivered_id = int(delivered_inv[0])
    except (TypeError, ValueError, IndexError):
        return "delivered_inventory has no usable row id"
    if parsed != delivered_id:
        return (
            f"entry_key {entry_key!r} addresses inventory row {parsed}, but the "
            f"delivered snapshot is row {delivered_id}"
        )
    return None


async def _delivered_role_identity(delivered_inv, guild_id, user_id,
                                   item_name, conn=None) -> int | None:
    """The role id the delivered inventory record actually entitles, or None.

    `metadata.role_id` is written by `give_reward(..., "role" | "temp_role")`
    when the row is created, so it states which role the purchase granted. A
    descriptor's own `role_id` cannot: it is a plain integer the rollback would
    otherwise hand straight to `remove_roles`. When the snapshot carries no
    role identity, fall back to the live row for the same name; if neither
    yields one the caller must fail closed rather than strip an unverified role.
    """
    meta_raw = None
    if delivered_inv is not None and len(delivered_inv) > 4:
        meta_raw = delivered_inv[4]
    if meta_raw is None:
        async def _live_meta(db):
            cur = await db.execute(
                "SELECT metadata FROM inventory_items "
                "WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, item_name),
            )
            fetched = await cur.fetchone()
            return fetched[0] if fetched else None

        if conn is not None:
            meta_raw = await _live_meta(conn)
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                meta_raw = await _live_meta(db)
    if meta_raw is None:
        return None
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    role_value = meta.get("role_id")
    if role_value is None:
        return None
    try:
        return int(role_value)
    except (TypeError, ValueError):
        return None


async def _inventory_identity_conflict(guild_id, user_id, item_name,
                                       delivered_inv, prior_inv,
                                       conn=None) -> str | None:
    """Why the descriptor's inventory identities do not describe one row, or None.

    The delivered snapshot is `(id, quantity, item_type, source, metadata,
    created_at, updated_at)` of the row this purchase created, so its first
    element is the only thing that says WHICH row may be touched. Both rollback
    helpers select the row by `item_name`, so a descriptor whose `item_name`
    (or `prior_inventory`) points at a different row would decrement, delete or
    overwrite an unrelated grant while reporting success.

    Returns None when the descriptor is consistent, or when no row carries
    `item_name` at all (the name-addressed rollback is then a no-op). A snapshot
    row that has vanished while a different row still carries the name is a
    conflict, not a no-op: the delivered grant is gone and the remaining row is
    somebody else's.
    """
    if delivered_inv is None:
        return None
    try:
        snap_id = int(delivered_inv[0])
    except (TypeError, ValueError, IndexError):
        return "delivered_inventory has no usable row id"
    if prior_inv is not None:
        try:
            prior_id = int(prior_inv[0])
        except (TypeError, ValueError, IndexError):
            return "prior_inventory has no usable row id"
        if prior_id != snap_id:
            return (
                f"prior_inventory describes row {prior_id} but the delivered "
                f"row is {snap_id}"
            )
    async def _check(db) -> str | None:
        cur = await db.execute(
            "SELECT id FROM inventory_items "
            "WHERE guild_id=? AND user_id=? AND item_name=?",
            (guild_id, user_id, item_name),
        )
        named = await cur.fetchone()
        if named is None:
            # Nothing carries this name any more, so the name-addressed rollback
            # is a no-op and cannot reach an unrelated row.
            return None
        named_id = int(named[0])
        if named_id == snap_id:
            return None
        if prior_inv is not None and named_id == prior_id:
            # The row is already back at the pre-grant state this descriptor
            # recorded (a retry whose progress flag never persisted). Both
            # rollback helpers treat that as a no-op, so it is safe.
            return None
        cur = await db.execute(
            "SELECT item_name FROM inventory_items WHERE id=?", (snap_id,))
        return named_id, await cur.fetchone()

    if conn is not None:
        checked = await _check(conn)
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            checked = await _check(db)
    if checked is None:
        return None
    named_id, snap_row = checked
    if snap_row is None:
        return (
            f"item_name {item_name!r} addresses inventory row {named_id}, but "
            f"the delivered snapshot is row {snap_id}, which no longer exists; "
            f"refusing to touch a row this purchase did not create"
        )
    return (
        f"item_name {item_name!r} addresses inventory row {named_id}, but the "
        f"delivered snapshot is row {snap_id} ({snap_row[0]!r})"
    )


async def _assert_retry_marker_owned(entry_key, guild_id, user_id, *,
                                     op_id) -> None:
    """Refuse to retire an `h:<id>` marker that this operation does not own.

    A retry marker is created *for one purchase operation* (`source =
    'shop_late_rollback'`, `metadata['op_id']`/`metadata['origin_op_id']` = that
    operation). Sharing the member is not evidence: one member can hold several
    markers and holdings at once, so a descriptor's `retry_entry_key` pointing
    at another operation's live holding would otherwise let this rollback delete
    somebody else's ownership record — and, because this runs in the role
    branch, do it after stripping a Discord role.

    All three facts are required before the caller may delete the marker, change
    equipment, remove a role, or claim completion:

    * the holding belongs to this operation's member;
    * its `source` is exactly `shop_late_rollback` (not `shop`, not any other
      grant path);
    * its recorded operation id is this `op_id`.

    A marker that no longer exists is fine: retiring it is a no-op.
    """
    key = str(entry_key or "")
    if not key.startswith("h:"):
        raise RuntimeError(f"invalid retry marker key {entry_key!r}")
    holding_id = entry_key_id(key)
    if holding_id is None:
        raise RuntimeError(f"invalid retry marker key {entry_key!r}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT guild_id, user_id, source, metadata FROM timed_holdings"
            " WHERE id=?",
            (holding_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return
    row_guild, row_user, source, meta_raw = row
    if (int(row_guild or 0), int(row_user or 0)) != (int(guild_id), int(user_id)):
        raise RuntimeError(
            f"retry marker {key} belongs to another member; refusing to retire it"
        )
    if str(source or "") != "shop_late_rollback":
        raise RuntimeError(
            f"retry marker {key} has source {source!r}, not 'shop_late_rollback';"
            f" it is not a late-rollback marker for operation {op_id} and must"
            f" not be retired as one"
        )
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:
        meta = None
    if not isinstance(meta, dict):
        raise RuntimeError(
            f"retry marker {key} has unreadable metadata; refusing to retire it"
        )
    marker_op = meta.get("origin_op_id") or meta.get("op_id")
    if str(marker_op or "") != str(op_id):
        raise RuntimeError(
            f"retry marker {key} was created for purchase operation"
            f" {marker_op!r}, not {op_id}; refusing to retire another"
            f" operation's holding"
        )


async def _ensure_role_retry_marker(
    *,
    op_id: str,
    guild_id: int,
    user_id: int,
    item_name: str,
    role_id: int,
    item_id=None,
    category=None,
) -> tuple[str, bool]:
    """Create the 1970 late-rollback retry marker exactly once, durably.

    Returns `(entry_key, created)`: `created` is False when an existing marker
    was adopted instead of a new one being inserted.

    Runs inside one `BEGIN IMMEDIATE` transaction so concurrent compensations
    (in this process or another one) cannot each create a duplicate marker for
    the same purchase operation. An existing marker recorded on the purchase
    descriptor or already present in `timed_holdings` (matched by the
    `op_id` recorded in its metadata) is adopted instead of duplicated.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        in_tx = False
        try:
            await db.execute("BEGIN IMMEDIATE")
            in_tx = True
            cur = await db.execute(
                "SELECT delivery_rollback FROM shop_purchase_operations WHERE op_id=?",
                (str(op_id),),
            )
            row = await cur.fetchone()
            db_raw = row[0] if row else None
            if db_raw == "":
                # Another invocation durably completed this rollback; do not
                # create a new retry marker for finished work.
                await db.execute("ROLLBACK")
                in_tx = False
                raise _LateRollbackAlreadyCompleted(op_id)
            if db_raw and str(db_raw).strip():
                try:
                    parsed = json.loads(db_raw)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("retry_entry_key"):
                    adopted_key = str(parsed["retry_entry_key"])
                    await db.execute("ROLLBACK")
                    in_tx = False
                    # Adopting a corrupted/foreign key must not hand this
                    # operation somebody else's holding to retire.
                    await _assert_retry_marker_owned(
                        adopted_key, guild_id, user_id, op_id=op_id)
                    return adopted_key, False

            cur = await db.execute(
                "SELECT id, metadata FROM timed_holdings "
                "WHERE guild_id=? AND user_id=? AND source='shop_late_rollback' "
                "ORDER BY id ASC",
                (int(guild_id), int(user_id)),
            )
            for h_id, h_meta_raw in await cur.fetchall():
                try:
                    h_meta = json.loads(h_meta_raw) if h_meta_raw else {}
                except Exception:
                    h_meta = {}
                if isinstance(h_meta, dict) and str(h_meta.get("op_id")) == str(op_id):
                    await db.execute("ROLLBACK")
                    in_tx = False
                    return f"h:{h_id}", False

            new_id = await grant_timed_holding(
                guild_id,
                user_id,
                item_name,
                expires_at="1970-01-01T00:00:00+00:00",
                item_type="temp_role",
                metadata={
                    "role_id": int(role_id),
                    "late_rollback": True,
                    "op_id": str(op_id),
                },
                source="shop_late_rollback",
                op_id=str(op_id),
                product_id=item_id,
                category=category,
                role_id=int(role_id),
                db=db,
            )
            await db.commit()
            in_tx = False
            return f"h:{new_id}", True
        except Exception:
            if in_tx:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
            raise


async def compensate_late_delivery(
    op_id: str,
    spec: dict | None = None,
    *,
    bot=None,
    interaction=None,
    handoff_to_holdings: bool = False,
) -> dict:
    """Compensate a delivery that completed after its purchase operation was refunded/failed.

    Enforces strict ordering and durability:
    1. Inventory/holding/boost rollback runs first and its return value is checked.
       If inventory rollback fails, compensation stops immediately without running
       equip reconciliation or `role_still_entitled` against the un-rolled-back row,
       leaving `delivery_rollback` on `shop_purchase_operations` for durable retry.
    2. For permanent roles (`internal_type == "role"`), equip reconciliation runs
       only when the member no longer owns the inventory entry after rollback
       (preserving a pre-existing equipped permanent Color on a refunded re-buy).
    3. Discord role removal resolves the live cached guild `Member` via
       `guild.get_member(user_id)` (never gating on `interaction.user.roles`),
       checks `role_still_entitled`, and retains the durable retry marker until
       removal succeeds or an independent entitlement is verified.
    """
    global _LAST_BOT
    bot_client = (
        bot
        or (getattr(interaction, "client", None) if interaction is not None else None)
        or _LAST_BOT
    )
    if bot_client is not None:
        _LAST_BOT = bot_client

    mem_spec = _PENDING_DELIVERY_ROLLBACKS.get(str(op_id))
    try:
        op_row = await get_purchase(str(op_id))
    except Exception as exc:
        return {
            "complete": False,
            "durable_persisted": False,
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
            "error": f"Failed to read purchase rollback state for {op_id}: {exc}",
        }

    if op_row is None:
        # No row to verify against: never report completion, and never act on a
        # caller-supplied specification alone.
        return {
            "complete": False,
            "durable_persisted": False,
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
            "error": f"Purchase operation {op_id} not found for late-delivery rollback",
        }

    if op_row.get("status") == PURCHASE_DELIVERED:
        _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
        return {
            "complete": False,
            "durable_persisted": False,
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
            "error": f"Cannot roll back delivered operation {op_id}",
        }

    raw_spec = (
        op_row.get("_raw_delivery_rollback")
        if "_raw_delivery_rollback" in op_row
        else op_row.get("delivery_rollback")
    )
    if raw_spec == "":
        _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
        return {
            "complete": True,
            "durable_persisted": True,
            "inventory_rolled_back": True,
            "equip_reconciled": True,
            "role_reconciled": True,
            "error": None,
        }

    db_spec = None
    if raw_spec is not None:
        try:
            db_spec = json.loads(raw_spec)
        except Exception:
            db_spec = None
        if not isinstance(db_spec, dict):
            return {
                "complete": False,
                "durable_persisted": False,
                "inventory_rolled_back": False,
                "equip_reconciled": False,
                "role_reconciled": False,
                "error": f"Malformed delivery_rollback descriptor for {op_id}",
            }
        # The durable descriptor is authoritative, so it must itself be usable
        # before anything is merged from it: a wrong-typed flag (e.g. `"yes"`)
        # must not be truthy-merged into a valid-looking `True`.
        _durable_ok, _durable_problem = _validate_delivery_rollback_spec(db_spec)
        if not _durable_ok:
            return {
                "complete": False,
                "durable_persisted": False,
                "inventory_rolled_back": False,
                "equip_reconciled": False,
                "role_reconciled": False,
                "error": (
                    f"Malformed delivery_rollback descriptor for {op_id}: "
                    f"{_durable_problem}"
                ),
            }
    else:
        # No durable descriptor at all: only an explicitly marked
        # failed-persistence retry may stand in for it, and a bare `None` means
        # there is nothing verifiable to compensate.
        if not isinstance(mem_spec, dict) or not mem_spec.get("_durable_unpersisted"):
            return {
                "complete": False,
                "durable_persisted": False,
                "inventory_rolled_back": False,
                "equip_reconciled": False,
                "role_reconciled": False,
                "error": f"Durable delivery_rollback descriptor absent for {op_id}",
            }

    if spec is None:
        spec = db_spec if isinstance(db_spec, dict) else mem_spec

    spec = dict(spec) if isinstance(spec, dict) else {}
    for source_spec in (mem_spec, db_spec):
        if not isinstance(source_spec, dict):
            continue
        for flag in ("inventory_rolled_back", "equip_reconciled", "role_reconciled"):
            if source_spec.get(flag):
                spec[flag] = True
        if not spec.get("retry_entry_key") and source_spec.get("retry_entry_key"):
            spec["retry_entry_key"] = source_spec["retry_entry_key"]
    if isinstance(db_spec, dict):
        spec.pop("_durable_unpersisted", None)
    elif isinstance(mem_spec, dict) and mem_spec.get("_durable_unpersisted"):
        spec["_durable_unpersisted"] = True

    # Validate the complete descriptor BEFORE any side effect or completion
    # claim. An empty object, a missing identity field, a wrong type, or an
    # unknown branch is reported and left in place for diagnosis — it is never
    # treated as finished work and never partially executed.
    _spec_ok, _spec_problem = _validate_delivery_rollback_spec(spec)
    if not _spec_ok:
        return {
            "complete": False,
            "durable_persisted": False,
            "inventory_rolled_back": False,
            "equip_reconciled": False,
            "role_reconciled": False,
            "error": (
                f"Malformed delivery_rollback descriptor for {op_id}: "
                f"{_spec_problem}"
            ),
        }

    guild_id = int(spec["guild_id"])
    user_id = int(spec["user_id"])
    item_id = spec.get("item_id")
    item_name = str(spec.get("item_name") or "")
    internal_type = str(spec.get("internal_type") or "")
    category = spec.get("category")
    role_id = int(spec["role_id"]) if spec.get("role_id") is not None else None
    late_key = spec.get("entry_key")
    role_added_by_delivery = bool(spec.get("role_added", True))
    prior_inv = (
        tuple(spec["prior_inventory"])
        if spec.get("prior_inventory") is not None else None
    )
    delivered_inv = (
        tuple(spec["delivered_inventory"])
        if spec.get("delivered_inventory") is not None else None
    )

    from utils.reward_engine import (
        rollback_item_inventory_grant,
        rollback_role_inventory_grant,
    )

    # Tracks a retry marker created by *this* invocation, so a completion that
    # lands while this call is still running can retire it instead of leaving an
    # orphan marker behind for finished work.
    own_retry_marker: dict = {"key": None, "created": False}

    async def _persist_incomplete(err_msg: str) -> dict:
        # A concurrent (or earlier, in another process) invocation may have
        # durably completed and cleared this rollback while this one was still
        # running on a stale specification. Never resurrect that finished
        # descriptor or write stale progress over it.
        if await _durable_delivery_rollback_completed(str(op_id)):
            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
            if own_retry_marker["created"]:
                await _retire_own_retry_marker(own_retry_marker["key"])
                own_retry_marker["created"] = False
            return {
                "complete": True,
                "durable_persisted": True,
                "inventory_rolled_back": True,
                "equip_reconciled": True,
                "role_reconciled": True,
                "error": None,
            }
        # Annotate, never fabricate. `record_pending_delivery_rollback` is the
        # only writer allowed to CREATE a descriptor: it runs immediately after
        # the delivery that makes one true. Reaching here with SQL NULL means
        # this invocation only holds a caller-supplied specification (a stale
        # local row, another process's cache), and persisting it would turn that
        # unverified guess into durable recovery state that a later
        # reconciliation pass would act on. Report the reason; leave the journal
        # alone. A descriptor this process knows it failed to persist is the one
        # exception: the write is then an attempt to recover, not an invention.
        try:
            _state, _raw, _ = await _read_delivery_rollback_raw(str(op_id))
        except Exception:
            _state = "live"  # let the writer below report the read failure
        if _state == "absent" and not spec.get("_durable_unpersisted"):
            return {
                "complete": False,
                "durable_persisted": False,
                "inventory_rolled_back": bool(spec.get("inventory_rolled_back")),
                "equip_reconciled": bool(spec.get("equip_reconciled")),
                "role_reconciled": bool(spec.get("role_reconciled")),
                "error": (
                    f"{err_msg} (no durable delivery_rollback descriptor is "
                    f"recorded for op_id={op_id}, so nothing was written)"
                ),
            }
        clean_spec = {k: v for k, v in spec.items() if not str(k).startswith("_")}
        # Record WHY this attempt stopped, in the durable descriptor itself. An
        # operator inspecting a stuck operation must not have to infer the
        # conflict from raw JSON or from a log line that may be long gone; the
        # runtime retries the same descriptor indefinitely, so the reason has to
        # travel with it. Diagnostics are latest-wins, so a retry that fails for
        # a new reason overwrites the old message instead of keeping the first.
        clean_spec["last_error"] = str(err_msg)[:1000]
        clean_spec["last_error_at"] = utcnow().isoformat()
        # Monotonic, compare-and-swap write through the shared writer: a stale
        # invocation can never lower a durable `True` flag or erase recorded
        # identity, whichever writer advanced the descriptor first.
        status, persist_err, merged = await _persist_delivery_rollback_progress(
            str(op_id), clean_spec
        )
        if status == "completed":
            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
            if own_retry_marker["created"]:
                await _retire_own_retry_marker(own_retry_marker["key"])
                own_retry_marker["created"] = False
            return {
                "complete": True,
                "durable_persisted": True,
                "inventory_rolled_back": True,
                "equip_reconciled": True,
                "role_reconciled": True,
                "error": None,
            }
        durable_persisted = status == "persisted"
        mem_copy = dict(merged if merged is not None else clean_spec)
        if spec.get("_durable_unpersisted") and not durable_persisted:
            mem_copy["_durable_unpersisted"] = True
        _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = mem_copy

        full_err = err_msg
        if not durable_persisted:
            # Nothing durable records this operation's pending rollback, so mark
            # the in-memory entry explicitly: a retry in this process may use it,
            # and `compensate_late_delivery` will refuse memory-only state that
            # lacks this marker (never treating the cache as durable recovery).
            mem_copy["_durable_unpersisted"] = True
            suffix = (
                f" (durable descriptor write failed: {persist_err})"
                if persist_err
                else f" (durable descriptor not persisted: {status})"
            )
            full_err = f"{err_msg}{suffix}"
            print(
                f"[SHOP] ERROR: Late-delivery rollback incomplete and durable "
                f"persistence failed for op_id={op_id}: {full_err}"
            )
        else:
            spec.pop("_durable_unpersisted", None)
            for flag in _ROLLBACK_PROGRESS_FLAGS:
                if merged is not None and merged.get(flag):
                    spec[flag] = True
        return {
            "complete": False,
            "durable_persisted": durable_persisted,
            "inventory_rolled_back": bool(spec.get("inventory_rolled_back")),
            "equip_reconciled": bool(spec.get("equip_reconciled")),
            "role_reconciled": bool(spec.get("role_reconciled")),
            "error": full_err,
        }

    async def _retire_own_retry_marker(entry_key: str | None) -> None:
        """Drop a retry marker this invocation created for already-finished work.

        Only called when the operation is durably complete, so the marker would
        otherwise be an orphan that cleanup would retire anyway.
        """
        if not entry_key:
            return
        try:
            await delete_timed_holding(str(entry_key))
        except Exception:
            pass

    async def _persist_handoff() -> tuple[bool, bool]:
        """Write the handoff descriptor with the durable-completion guard.

        Returns `(completed_by_other, durable_ok)`. The in-memory entry is
        marked `_durable_unpersisted` only when the write genuinely failed;
        a write rejected because the rollback is already durably complete
        drops the cache entry instead and never labels it recoverable work.
        Uses the same monotonic compare-and-swap writer as every other
        descriptor write, so a handoff can never lower newer progress or erase
        recorded identity.
        """
        clean_spec = {k: v for k, v in spec.items() if not str(k).startswith("_")}
        status, persist_err, merged = await _persist_delivery_rollback_progress(
            str(op_id), clean_spec
        )
        mem = dict(merged if merged is not None else clean_spec)
        if spec.get("_durable_unpersisted") and status != "persisted":
            mem["_durable_unpersisted"] = True
        _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = mem
        if status == "persisted":
            for flag in _ROLLBACK_PROGRESS_FLAGS:
                if mem.get(flag):
                    spec[flag] = True
            spec.pop("_durable_unpersisted", None)
            return False, True
        if status == "completed":
            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
            spec.pop("_durable_unpersisted", None)
            return True, True
        print(
            f"[SHOP] Failed to persist handoff delivery_rollback descriptor "
            f"for op_id={op_id}: {persist_err or status}"
        )
        mem["_durable_unpersisted"] = True
        _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = mem
        spec["_durable_unpersisted"] = True
        return False, False

    guild_obj, target_member = _resolve_guild_and_live_member(
        guild_id, user_id, bot=bot_client, interaction=interaction
    )
    member_departed = False
    member_lookup_error = None
    if target_member is None and guild_obj is not None and hasattr(guild_obj, "fetch_member"):
        try:
            target_member = await guild_obj.fetch_member(int(user_id))
        except Exception as exc:
            if _is_member_not_found(exc):
                target_member = None
                member_departed = True
            else:
                target_member = None
                member_lookup_error = exc
    if (
        target_member is None
        and not member_departed
        and member_lookup_error is None
        and interaction is not None
    ):
        itx_user = getattr(interaction, "user", None)
        if itx_user is not None and getattr(itx_user, "id", int(user_id)) == int(user_id):
            target_member = itx_user

    # ── Ownership boundary ───────────────────────────────────────────────
    # Shape validation cannot catch a descriptor that is internally consistent
    # but names a different guild or member, and every branch below mutates
    # state addressed by exactly those two values. Reconcile them with the
    # operation's own journal row before dispatching, so no branch can be
    # reached with a descriptor that belongs to somebody else's purchase.
    _bound, _bind_problem = _bind_descriptor_to_operation(spec, op_row)
    if not _bound:
        return await _persist_incomplete(
            f"Delivery rollback descriptor is not bound to this purchase: "
            f"{_bind_problem}"
        )

    # ── Branch 1: Timed holding (`h:<id>`) ──────────────────────────────
    if late_key and str(late_key).startswith("h:"):
        holding_id = int(str(late_key).split(":", 1)[1])
        # The key must identify THIS operation's holding. A mismatched key (a
        # corrupted descriptor, or an id belonging to another member) must not
        # expire or delete somebody else's ownership record.
        try:
            provenance = await timed_holding_provenance(str(late_key))
        except Exception as exc:
            return await _persist_incomplete(
                f"Timed holding lookup failed for {late_key}: {exc}"
            )
        holding_owner = (
            (provenance["guild_id"], provenance["user_id"])
            if provenance is not None else None
        )
        if holding_owner is not None and holding_owner != (int(guild_id), int(user_id)):
            return await _persist_incomplete(
                f"Timed holding {late_key} does not belong to this purchase's "
                f"member; refusing to retire it"
            )
        # Same member is NOT the same purchase: one member can hold several
        # timed grants of the same product at once. Expiring this key retires a
        # record (and, for a timed Color, an equipped slot) that this operation
        # may never have created, so require durable proof of the link.
        if provenance is not None:
            origin_op = provenance.get("origin_op_id")
            if origin_op is None:
                # Granted before the operation marker existed. The purchase
                # journal cannot stand in for it: `detail` is only written by the
                # `pending -> delivered` transition, and that transition makes the
                # operation `delivered`, which this function refuses to
                # compensate — so a `detail` naming this key is not a state the
                # runtime produces. Nothing durable ties the holding to this
                # purchase, and one member may hold several such rows, so this
                # fails closed for an operator instead of guessing.
                return await _persist_incomplete(
                    f"Timed holding {late_key} records no originating purchase "
                    f"operation (granted before operation provenance existed); "
                    f"cannot prove it belongs to {op_id}, so it is kept for "
                    f"manual recovery instead of being expired or deleted"
                )
            if str(origin_op) != str(op_id):
                return await _persist_incomplete(
                    f"Timed holding {late_key} was created by purchase "
                    f"operation {origin_op}, not {op_id}; refusing to expire "
                    f"or delete another operation's holding"
                )
        if holding_owner is None and not spec.get("inventory_rolled_back"):
            # The key is shaped like a holding id but no such holding exists, and
            # no earlier attempt recorded that it retired one. Completing here
            # would clear the descriptor (and could leave the op's real holding
            # and Discord role in place) without proof that anything was rolled
            # back: keep the descriptor pending for diagnosis/retry instead.
            return await _persist_incomplete(
                f"Timed holding {late_key} does not exist; cannot verify the "
                f"timed grant was rolled back"
            )
        # A descriptor's `role_id` is an integer the rollback hands straight to
        # `remove_roles`; it is not evidence of which role this holding entitled.
        # The holding row records the granted `role_id` at creation time, so a
        # descriptor naming a different role is either corrupted or forged and
        # must not strip a role this purchase never granted.
        if (
            role_id is not None
            and role_added_by_delivery
            and not spec.get("role_reconciled")
            and provenance is not None
        ):
            holding_role = provenance.get("role_id")
            if holding_role is None:
                return await _persist_incomplete(
                    f"Timed holding {late_key} records no role identity; cannot "
                    f"prove role {int(role_id)} was granted by it, so it is kept "
                    f"for manual recovery"
                )
            if int(holding_role) != int(role_id):
                return await _persist_incomplete(
                    f"Timed holding {late_key} granted role {int(holding_role)}, "
                    f"but the descriptor names role {int(role_id)}; refusing to "
                    f"remove a role this purchase did not grant"
                )

        if not spec.get("inventory_rolled_back"):
            try:
                async with aiosqlite.connect(DB_PATH) as cleanup_db:
                    cur = await cleanup_db.execute(
                        "SELECT metadata FROM timed_holdings WHERE id=?",
                        (holding_id,),
                    )
                    h_row = await cur.fetchone()
                    if h_row is not None:
                        try:
                            h_meta = json.loads(h_row[0]) if h_row[0] else {}
                        except Exception:
                            h_meta = {}
                        if not isinstance(h_meta, dict):
                            h_meta = {}
                        h_meta["op_id"] = str(op_id)
                        if role_added_by_delivery:
                            h_meta["late_rollback"] = True
                        await cleanup_db.execute(
                            "UPDATE timed_holdings "
                            "SET expires_at='1970-01-01T00:00:00+00:00', metadata=? "
                            "WHERE id=?",
                            (json.dumps(h_meta), holding_id),
                        )
                    else:
                        await cleanup_db.execute(
                            "UPDATE timed_holdings "
                            "SET expires_at='1970-01-01T00:00:00+00:00' "
                            "WHERE id=?",
                            (holding_id,),
                        )
                    await cleanup_db.commit()
                spec["inventory_rolled_back"] = True
            except Exception as exc:
                return await _persist_incomplete(f"Timed holding expiry rollback failed: {exc}")

        if not spec.get("equip_reconciled"):
            try:
                await reconcile_equip_for_expired_holding(
                    guild_id, user_id, str(late_key), role_id=role_id
                )
                spec["equip_reconciled"] = True
            except Exception as exc:
                return await _persist_incomplete(f"Timed holding equip rollback failed: {exc}")

        if handoff_to_holdings:
            if await _durable_delivery_rollback_completed(str(op_id)):
                _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                return {
                    "complete": True,
                    "durable_persisted": True,
                    "inventory_rolled_back": True,
                    "equip_reconciled": True,
                    "role_reconciled": True,
                    "error": None,
                }
            completed_by_other, durable_ok = await _persist_handoff()
            if completed_by_other:
                return {
                    "complete": True,
                    "durable_persisted": True,
                    "inventory_rolled_back": True,
                    "equip_reconciled": True,
                    "role_reconciled": True,
                    "error": None,
                }
            return {
                "complete": False,
                "durable_persisted": durable_ok,
                "inventory_rolled_back": True,
                "equip_reconciled": True,
                "role_reconciled": False,
                "handed_off": True,
                "error": None,
            }

        if not spec.get("role_reconciled"):
            if role_added_by_delivery and role_id is None and not member_departed:
                # The holding was expired, but the descriptor lost the role
                # identity: the Discord role granted with it cannot be removed,
                # so this is not reconciled — keep it pending for diagnosis.
                return await _persist_incomplete(
                    "Timed role rollback descriptor has no 'role_id'; cannot "
                    "reconcile the granted role"
                )
            try:
                if role_id is not None and role_added_by_delivery and not member_departed:
                    still_entitled = await role_still_entitled(
                        guild_id, user_id, int(role_id),
                        exclude_entry_key=str(late_key),
                        bot=bot_client, member=target_member,
                    )
                    if not still_entitled:
                        if guild_obj is None:
                            raise RuntimeError(
                                "Guild unavailable for late Discord role rollback"
                            )
                        role_obj, role_deleted, role_lookup_err = await _resolve_guild_role(
                            guild_obj, int(role_id)
                        )
                        if role_lookup_err is not None:
                            raise RuntimeError(
                                f"Role lookup failed for late Discord role rollback: {role_lookup_err}"
                            )
                        if role_deleted:
                            if target_member is not None and isinstance(getattr(target_member, "roles", None), list):
                                target_member.roles[:] = [
                                    r for r in target_member.roles
                                    if getattr(r, "id", None) != int(role_id)
                                ]
                            itx_user = getattr(interaction, "user", None) if interaction is not None else None
                            if (
                                itx_user is not None
                                and itx_user is not target_member
                                and isinstance(getattr(itx_user, "roles", None), list)
                            ):
                                itx_user.roles[:] = [
                                    r for r in itx_user.roles
                                    if getattr(r, "id", None) != int(role_id)
                                ]
                        else:
                            if member_lookup_error is not None:
                                raise RuntimeError(
                                    f"Member lookup failed for late Discord role rollback: {member_lookup_error}"
                                )
                            if role_obj is None:
                                raise RuntimeError(
                                    "Role unavailable for late Discord role rollback"
                                )
                            if target_member is None or not hasattr(target_member, "remove_roles"):
                                raise RuntimeError(
                                    "Member unavailable for late Discord role rollback"
                                )
                            await target_member.remove_roles(
                                role_obj,
                                reason="Late delivery after purchase cancellation",
                            )
                            if (
                                isinstance(getattr(target_member, "roles", None), list)
                                and role_obj in target_member.roles
                            ):
                                target_member.roles.remove(role_obj)
                            itx_user = getattr(interaction, "user", None) if interaction is not None else None
                            if (
                                itx_user is not None
                                and itx_user is not target_member
                                and isinstance(getattr(itx_user, "roles", None), list)
                                and role_obj in itx_user.roles
                            ):
                                itx_user.roles.remove(role_obj)
                await delete_timed_holding(str(late_key))
                spec["role_reconciled"] = True
            except Exception as exc:
                return await _persist_incomplete(f"Timed holding Discord role rollback failed: {exc}")

    # ── Branch 2: Permanent role / permanent Color (`internal_type == "role"`) ──
    elif internal_type == "role":
        role_guard: dict = {"outcome": None, "reason": None}

        async def _role_precheck_reason(conn) -> str | None:
            """Every identity this branch needs, checked before it mutates.

            Runs inside the durable `BEGIN IMMEDIATE` (the `tx_guard` "pre"
            phase), so it only ever executes when a durable descriptor actually
            backs this attempt — it can neither fabricate a descriptor nor
            refuse on behalf of one that was never persisted. Everything it
            verifies is verified BEFORE `rollback_role_inventory_grant`
            decrements or deletes anything, because a failure afterwards would
            leave an unrelated row already damaged while the descriptor stays
            pending for a retry.
            """
            # 1. Without a role identity nothing downstream can remove what the
            #    delivery granted, so report that gap first: it is what the
            #    operator has to supply.
            if role_added_by_delivery and role_id is None:
                return (
                    "Role rollback descriptor has no 'role_id'; cannot reconcile "
                    "the granted role"
                )
            # 2. The inventory row this descriptor addresses must be the row the
            #    purchase delivered.
            conflict = await _inventory_identity_conflict(
                guild_id, user_id, item_name, delivered_inv, prior_inv, conn=conn
            )
            if conflict is not None:
                return (
                    f"Role rollback descriptor does not describe the delivered "
                    f"inventory grant: {conflict}"
                )
            # 3. A permanent role is delivered as an inventory row, and the
            #    producer always hands that row's snapshot to the shop. Without
            #    it the name-addressed rollback cannot tell this purchase's row
            #    from an independent same-named grant, so refuse before the
            #    first mutation rather than after it.
            if delivered_inv is None:
                return (
                    f"Role rollback descriptor for {item_name!r} records no "
                    f"delivered inventory snapshot; refusing to touch an "
                    f"inventory row it cannot identify"
                )
            # 4. The equipped slot key must be this purchase's own row key.
            key_conflict = await _role_entry_key_conflict(late_key, delivered_inv)
            if key_conflict is not None:
                return f"Role rollback descriptor key is unusable: {key_conflict}"
            if role_id is not None and role_added_by_delivery:
                delivered_role = await _delivered_role_identity(
                    delivered_inv, guild_id, user_id, item_name, conn=conn
                )
                if delivered_role is None:
                    return (
                        f"Delivered inventory for {item_name!r} records no role "
                        f"identity; cannot prove role {int(role_id)} was granted "
                        f"by this purchase, so it is kept for manual recovery"
                    )
                if int(delivered_role) != int(role_id):
                    return (
                        f"Delivered inventory for {item_name!r} granted role "
                        f"{int(delivered_role)}, but the descriptor names role "
                        f"{int(role_id)}; refusing to remove a role this "
                        f"purchase did not grant"
                    )
            return None

        async def _role_inv_guard(phase, conn):
            """Re-validate durable rollback state inside `BEGIN IMMEDIATE`.

            `pre` decides whether this invocation may still touch inventory
            (fails closed on missing/malformed/delivered state, and reports
            "already_done" when a concurrent or earlier compensation durably
            recorded the rollback). `post` writes the progress flag on the same
            connection so the inventory mutation and the journal update commit
            or roll back atomically.
            """
            cur = await conn.execute(
                "SELECT status, delivery_rollback "
                "FROM shop_purchase_operations WHERE op_id=?",
                (str(op_id),),
            )
            row = await cur.fetchone()
            if row is None:
                role_guard["outcome"] = "abort"
                role_guard["reason"] = (
                    f"Purchase operation {op_id} not found for role rollback"
                )
                return "abort" if phase == "pre" else False
            db_status, db_raw = row
            if db_status == PURCHASE_DELIVERED:
                role_guard["outcome"] = "abort"
                role_guard["reason"] = (
                    f"Cannot roll back role for delivered operation {op_id}"
                )
                return "abort" if phase == "pre" else False

            if phase == "pre":
                if db_raw == "":
                    role_guard["outcome"] = "already_done"
                    spec["inventory_rolled_back"] = True
                    spec["equip_reconciled"] = True
                    spec["role_reconciled"] = True
                    return "already_done"
                if db_raw is None or not str(db_raw).strip():
                    live_mem = _PENDING_DELIVERY_ROLLBACKS.get(str(op_id))
                    if (
                        not isinstance(live_mem, dict)
                        or not live_mem.get("_durable_unpersisted")
                    ):
                        role_guard["outcome"] = "abort"
                        role_guard["reason"] = (
                            f"Durable delivery_rollback descriptor absent for {op_id}"
                        )
                        return "abort"
                    if live_mem.get("inventory_rolled_back"):
                        spec["inventory_rolled_back"] = True
                        role_guard["outcome"] = "already_done"
                        return "already_done"
                    abort_reason = await _role_precheck_reason(conn)
                    if abort_reason is not None:
                        role_guard["outcome"] = "abort"
                        role_guard["reason"] = abort_reason
                        return "abort"
                    role_guard["outcome"] = "proceed"
                    return "proceed"
                try:
                    parsed = json.loads(db_raw)
                except Exception:
                    parsed = None
                if not isinstance(parsed, dict):
                    role_guard["outcome"] = "abort"
                    role_guard["reason"] = (
                        f"Malformed delivery_rollback descriptor for {op_id}"
                    )
                    return "abort"
                for flag in (
                    "inventory_rolled_back",
                    "equip_reconciled",
                    "role_reconciled",
                ):
                    if parsed.get(flag):
                        spec[flag] = True
                if not spec.get("retry_entry_key") and parsed.get("retry_entry_key"):
                    spec["retry_entry_key"] = parsed["retry_entry_key"]
                if parsed.get("inventory_rolled_back"):
                    role_guard["outcome"] = "already_done"
                    return "already_done"
                abort_reason = await _role_precheck_reason(conn)
                if abort_reason is not None:
                    role_guard["outcome"] = "abort"
                    role_guard["reason"] = abort_reason
                    return "abort"
                role_guard["outcome"] = "proceed"
                return "proceed"

            spec["inventory_rolled_back"] = True
            clean_spec = {
                k: v for k, v in spec.items() if not str(k).startswith("_")
            }
            status, _err, _merged = await _persist_delivery_rollback_progress(
                str(op_id), clean_spec, db=conn
            )
            saved = status == "persisted"
            role_guard["outcome"] = "persisted" if saved else "post_failed"
            return saved

        if not spec.get("inventory_rolled_back"):
            inv_ok = await rollback_role_inventory_grant(
                guild_id,
                user_id,
                item_name,
                prior_inv=prior_inv,
                delivered_inv=delivered_inv,
                tx_guard=_role_inv_guard,
            )
            if not inv_ok:
                if role_guard.get("outcome") == "abort":
                    # The abort happened inside the rollback's own transaction,
                    # which has been rolled back, so nothing this attempt did
                    # survives. Record WHY it refused in the durable descriptor:
                    # otherwise the operation looks stuck for no stated reason,
                    # and the operator has to re-derive the conflict from raw
                    # JSON (or from a log line that is long gone).
                    return await _persist_incomplete(
                        role_guard.get("reason")
                        or f"Role inventory rollback aborted for {item_name!r}"
                    )
                return await _persist_incomplete(
                    f"Role inventory rollback failed for {item_name!r}"
                )
            spec["inventory_rolled_back"] = True

        # Which Discord role this purchase granted is recorded by the producer in
        # the delivered inventory row (`metadata.role_id`), not by the
        # descriptor's own `role_id` — that integer is what the code below hands
        # to `remove_roles`, and equipment reconciliation keys off the same
        # holding. Refuse before anything Discord-side or equipment-side is
        # touched when the two disagree. (Placed after the inventory step so a
        # descriptor without durable backing still fails closed on that, without
        # this check fabricating a descriptor.)
        if (
            role_id is not None
            and role_added_by_delivery
            and not spec.get("role_reconciled")
        ):
            delivered_role = await _delivered_role_identity(
                delivered_inv, guild_id, user_id, item_name
            )
            if delivered_role is None:
                return await _persist_incomplete(
                    f"Delivered inventory for {item_name!r} records no role "
                    f"identity; cannot prove role {int(role_id)} was granted by "
                    f"this purchase, so it is kept for manual recovery"
                )
            if int(delivered_role) != int(role_id):
                return await _persist_incomplete(
                    f"Delivered inventory for {item_name!r} granted role "
                    f"{int(delivered_role)}, but the descriptor names role "
                    f"{int(role_id)}; refusing to remove a role this purchase "
                    f"did not grant"
                )

        if not spec.get("equip_reconciled"):
            try:
                prior_qty = (
                    int(prior_inv[1] or 0)
                    if prior_inv is not None and len(prior_inv) > 1
                    else 0
                )
                still_owns_entry = prior_qty > 0
                if not still_owns_entry:
                    async with aiosqlite.connect(DB_PATH) as check_db:
                        cur = await check_db.execute(
                            "SELECT quantity FROM inventory_items "
                            "WHERE guild_id=? AND user_id=? AND item_name=?",
                            (guild_id, user_id, item_name),
                        )
                        rem_row = await cur.fetchone()
                    still_owns_entry = bool(rem_row and int(rem_row[0] or 0) > 0)

                if not still_owns_entry:
                    if late_key:
                        await reconcile_equip_for_expired_holding(
                            guild_id, user_id, str(late_key), role_id=role_id
                        )
                    elif role_id is not None:
                        await clear_equip_for_holding(
                            guild_id, user_id, None, role_id=role_id
                        )
                spec["equip_reconciled"] = True
            except Exception as exc:
                return await _persist_incomplete(f"Role equip reconciliation failed: {exc}")

        if not spec.get("role_reconciled"):
            if not role_added_by_delivery:
                # The delivery added nothing to remove (the member already held
                # the role), so there is nothing to reconcile.
                spec["role_reconciled"] = True
            elif role_id is None:
                # Without the role identity the branch cannot remove the role it
                # granted; claiming reconciliation here would silently leave the
                # member holding a refunded role.
                return await _persist_incomplete(
                    "Role rollback descriptor has no 'role_id'; cannot reconcile "
                    "the granted role"
                )
            elif member_departed:
                retry_entry_key = spec.get("retry_entry_key")
                if retry_entry_key:
                    try:
                        await _assert_retry_marker_owned(
                            retry_entry_key, guild_id, user_id, op_id=op_id)
                        await delete_timed_holding(retry_entry_key)
                    except Exception as exc:
                        return await _persist_incomplete(
                            f"Failed to retire retry marker for departed member: {exc}"
                        )
                    spec["retry_entry_key"] = None
                spec["role_reconciled"] = True
            else:
                retry_entry_key = spec.get("retry_entry_key")
                if not retry_entry_key:
                    try:
                        retry_entry_key, marker_created = (
                            await _ensure_role_retry_marker(
                                op_id=str(op_id),
                                guild_id=guild_id,
                                user_id=user_id,
                                item_name=item_name,
                                role_id=int(role_id),
                                item_id=item_id,
                                category=category,
                            )
                        )
                        spec["retry_entry_key"] = retry_entry_key
                        if marker_created:
                            own_retry_marker["key"] = retry_entry_key
                            own_retry_marker["created"] = True
                    except _LateRollbackAlreadyCompleted:
                        _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                        return {
                            "complete": True,
                            "durable_persisted": True,
                            "inventory_rolled_back": True,
                            "equip_reconciled": True,
                            "role_reconciled": True,
                            "error": None,
                        }
                    except Exception as exc:
                        return await _persist_incomplete(
                            f"Failed to create role rollback retry marker: {exc}"
                        )

                # Prove the marker belongs to THIS operation before anything is
                # mutated for it — before the handoff, the entitlement check, the
                # Discord role removal and the marker deletion. A descriptor whose
                # `retry_entry_key` names another operation's holding (or a
                # non-marker holding at all) must fail closed here.
                try:
                    await _assert_retry_marker_owned(
                        retry_entry_key, guild_id, user_id, op_id=op_id)
                except Exception as exc:
                    return await _persist_incomplete(
                        f"Role rollback retry marker is not this operation's: {exc}"
                    )

                if handoff_to_holdings:
                    if await _durable_delivery_rollback_completed(str(op_id)):
                        _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                        if own_retry_marker["created"]:
                            await _retire_own_retry_marker(
                                own_retry_marker["key"])
                        return {
                            "complete": True,
                            "durable_persisted": True,
                            "inventory_rolled_back": True,
                            "equip_reconciled": True,
                            "role_reconciled": True,
                            "error": None,
                        }
                    completed_by_other, durable_ok = await _persist_handoff()
                    if completed_by_other:
                        if own_retry_marker["created"]:
                            await _retire_own_retry_marker(
                                own_retry_marker["key"])
                        return {
                            "complete": True,
                            "durable_persisted": True,
                            "inventory_rolled_back": True,
                            "equip_reconciled": True,
                            "role_reconciled": True,
                            "error": None,
                        }
                    return {
                        "complete": False,
                        "durable_persisted": durable_ok,
                        "inventory_rolled_back": True,
                        "equip_reconciled": True,
                        "role_reconciled": False,
                        "handed_off": True,
                        "error": None,
                    }

                try:
                    still_entitled = await role_still_entitled(
                        guild_id,
                        user_id,
                        int(role_id),
                        exclude_entry_key=retry_entry_key,
                        bot=bot_client,
                        member=target_member,
                    )
                    if not still_entitled:
                        if guild_obj is None:
                            raise RuntimeError(
                                "Guild unavailable for late Discord role rollback"
                            )
                        role_obj, role_deleted, role_lookup_err = await _resolve_guild_role(
                            guild_obj, int(role_id)
                        )
                        if role_lookup_err is not None:
                            raise RuntimeError(
                                f"Role lookup failed for late Discord role rollback: {role_lookup_err}"
                            )
                        if role_deleted:
                            if target_member is not None and isinstance(getattr(target_member, "roles", None), list):
                                target_member.roles[:] = [
                                    r for r in target_member.roles
                                    if getattr(r, "id", None) != int(role_id)
                                ]
                            itx_user = getattr(interaction, "user", None) if interaction is not None else None
                            if (
                                itx_user is not None
                                and itx_user is not target_member
                                and isinstance(getattr(itx_user, "roles", None), list)
                            ):
                                itx_user.roles[:] = [
                                    r for r in itx_user.roles
                                    if getattr(r, "id", None) != int(role_id)
                                ]
                        else:
                            if member_lookup_error is not None:
                                raise RuntimeError(
                                    f"Member lookup failed for late Discord role rollback: {member_lookup_error}"
                                )
                            if role_obj is None:
                                raise RuntimeError(
                                    "Role unavailable for late Discord role rollback"
                                )
                            if target_member is None or not hasattr(target_member, "remove_roles"):
                                raise RuntimeError(
                                    "Member unavailable for late Discord role rollback"
                                )
                            await target_member.remove_roles(
                                role_obj,
                                reason="Late delivery after purchase cancellation",
                            )
                            if (
                                isinstance(getattr(target_member, "roles", None), list)
                                and role_obj in target_member.roles
                            ):
                                target_member.roles.remove(role_obj)
                            itx_user = getattr(interaction, "user", None) if interaction is not None else None
                            if (
                                itx_user is not None
                                and itx_user is not target_member
                                and isinstance(getattr(itx_user, "roles", None), list)
                                and role_obj in itx_user.roles
                            ):
                                itx_user.roles.remove(role_obj)
                    await _assert_retry_marker_owned(
                        retry_entry_key, guild_id, user_id, op_id=op_id)
                    await delete_timed_holding(retry_entry_key)
                    spec["retry_entry_key"] = None
                    spec["role_reconciled"] = True
                except Exception as exc:
                    return await _persist_incomplete(
                        f"Role Discord removal failed for {item_name!r}: {exc}"
                    )

    # ── Branch 3: XP Boost (`internal_type == "xp_boost"`) ──────────────
    elif internal_type == "xp_boost":
        if not spec.get("inventory_rolled_back"):
            boost_id = spec.get("boost_id")
            boost_exp = spec.get("boost_expires_at")
            try:
                async with aiosqlite.connect(DB_PATH) as cleanup_db:
                    boost_in_tx = False
                    try:
                        await cleanup_db.execute("BEGIN IMMEDIATE")
                        boost_in_tx = True
                        cur = await cleanup_db.execute(
                            "SELECT status, delivery_rollback "
                            "FROM shop_purchase_operations WHERE op_id=?",
                            (str(op_id),),
                        )
                        op_row_boost = await cur.fetchone()
                        if op_row_boost is None:
                            await cleanup_db.execute("ROLLBACK")
                            boost_in_tx = False
                            return {
                                "complete": False,
                                "durable_persisted": False,
                                "inventory_rolled_back": False,
                                "equip_reconciled": False,
                                "role_reconciled": False,
                                "error": (
                                    f"Purchase operation {op_id} not found "
                                    f"for XP boost rollback"
                                ),
                            }
                        db_status_b, db_raw_b = op_row_boost
                        if db_status_b == PURCHASE_DELIVERED:
                            await cleanup_db.execute("ROLLBACK")
                            boost_in_tx = False
                            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                            return {
                                "complete": False,
                                "durable_persisted": False,
                                "inventory_rolled_back": False,
                                "equip_reconciled": False,
                                "role_reconciled": False,
                                "error": (
                                    f"Cannot roll back XP boost for delivered "
                                    f"operation {op_id}"
                                ),
                            }
                        if db_raw_b == "":
                            await cleanup_db.execute("ROLLBACK")
                            boost_in_tx = False
                            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                            return {
                                "complete": True,
                                "durable_persisted": True,
                                "inventory_rolled_back": True,
                                "equip_reconciled": True,
                                "role_reconciled": True,
                                "error": None,
                            }

                        already_boost_rolled_back = False
                        if db_raw_b is None or not str(db_raw_b).strip():
                            live_mem_b = _PENDING_DELIVERY_ROLLBACKS.get(str(op_id))
                            if (
                                not isinstance(live_mem_b, dict)
                                or not live_mem_b.get("_durable_unpersisted")
                            ):
                                await cleanup_db.execute("ROLLBACK")
                                boost_in_tx = False
                                return {
                                    "complete": False,
                                    "durable_persisted": False,
                                    "inventory_rolled_back": False,
                                    "equip_reconciled": False,
                                    "role_reconciled": False,
                                    "error": (
                                        f"Durable delivery_rollback descriptor "
                                        f"absent for {op_id}"
                                    ),
                                }
                            already_boost_rolled_back = bool(
                                live_mem_b.get("inventory_rolled_back")
                            )
                        else:
                            try:
                                parsed_b = json.loads(db_raw_b)
                            except Exception:
                                parsed_b = None
                            if not isinstance(parsed_b, dict):
                                await cleanup_db.execute("ROLLBACK")
                                boost_in_tx = False
                                return {
                                    "complete": False,
                                    "durable_persisted": False,
                                    "inventory_rolled_back": False,
                                    "equip_reconciled": False,
                                    "role_reconciled": False,
                                    "error": (
                                        f"Malformed delivery_rollback descriptor "
                                        f"for {op_id}"
                                    ),
                                }
                            for flag in (
                                "inventory_rolled_back",
                                "equip_reconciled",
                                "role_reconciled",
                            ):
                                if parsed_b.get(flag):
                                    spec[flag] = True
                            already_boost_rolled_back = bool(
                                parsed_b.get("inventory_rolled_back")
                            )

                        if already_boost_rolled_back:
                            await cleanup_db.execute("ROLLBACK")
                            boost_in_tx = False
                        else:
                            if boost_id is not None:
                                boost_origin = await _boost_origin_op(
                                    cleanup_db, int(boost_id))
                                if boost_origin is not None:
                                    if (
                                        boost_origin["guild_id"] != int(guild_id)
                                        or boost_origin["user_id"] != int(user_id)
                                    ):
                                        raise RuntimeError(
                                            f"XP boost {int(boost_id)} belongs to "
                                            f"another member; refusing to delete it"
                                        )
                                    if boost_origin["origin_op_id"] is None:
                                        raise RuntimeError(
                                            f"XP boost {int(boost_id)} records no "
                                            f"originating purchase operation "
                                            f"(a non-Shop grant, or written before "
                                            f"provenance existed); it cannot be "
                                            f"shown to belong to {op_id}, so it is "
                                            f"kept for manual recovery"
                                        )
                                    if (
                                        str(boost_origin["origin_op_id"])
                                        != str(op_id)
                                    ):
                                        raise RuntimeError(
                                            f"XP boost {int(boost_id)} was created "
                                            f"by purchase operation "
                                            f"{boost_origin['origin_op_id']}, not "
                                            f"{op_id}; refusing to delete another "
                                            f"operation's boost"
                                        )
                                    await cleanup_db.execute(
                                        "DELETE FROM leveling_active_boosts "
                                        "WHERE id=? AND guild_id=? AND user_id=?"
                                        " AND origin_op_id=?",
                                        (int(boost_id), guild_id, user_id,
                                         str(op_id)),
                                    )
                                else:
                                    # The cited id resolves to NO row. That says
                                    # nothing about this operation's boost, which
                                    # may still be live: a descriptor with a
                                    # stale, wrong-but-plausible or foreign-looking
                                    # id used to fall through to "already gone"
                                    # and complete while the purchased boost kept
                                    # granting XP. Resolve the boost this
                                    # operation actually created instead, and let
                                    # the post-condition below decide.
                                    owned = await _boosts_for_origin(
                                        cleanup_db, guild_id, user_id, str(op_id)
                                    )
                                    if len(owned) > 1:
                                        raise RuntimeError(
                                            f"ambiguous XP boost identity for "
                                            f"op_id={op_id}: {len(owned)} live "
                                            f"boosts record this operation and the "
                                            f"descriptor's boost_id resolves to no "
                                            f"row, so none can be singled out"
                                        )
                                    if owned:
                                        await cleanup_db.execute(
                                            "DELETE FROM leveling_active_boosts "
                                            "WHERE id=? AND guild_id=? AND user_id=?"
                                            " AND origin_op_id=?",
                                            (owned[0], guild_id, user_id,
                                             str(op_id)),
                                        )
                            elif boost_exp:
                                # No recorded row id: only ever remove the boost
                                # this purchase created, identified by its
                                # provenance. A `level_claim:*` or `potion` boost
                                # that shares the same expiry string belongs to
                                # another grant and must never be deleted.
                                #
                                # `(source, expires_at)` is not a unique identity,
                                # so this path refuses to guess: exactly one match
                                # is removable, more than one is ambiguous and
                                # fails closed (descriptor kept for manual
                                # recovery) instead of deleting someone else's row.
                                # The `ORDER BY` below is therefore incidental: it
                                # never decides between two candidate rows, because
                                # more than one match deletes nothing at all.
                                boost_source = str(
                                    spec.get("boost_source") or "shop"
                                )
                                cursor = await cleanup_db.execute(
                                    "SELECT id, origin_op_id "
                                    "FROM leveling_active_boosts "
                                    "WHERE guild_id=? AND user_id=? AND expires_at=? "
                                    "AND source=? "
                                    "ORDER BY id ASC",
                                    (guild_id, user_id, str(boost_exp),
                                     boost_source),
                                )
                                b_rows = list(await cursor.fetchall())
                                owned_rows = [
                                    r for r in b_rows
                                    if str(r[1] or "") == str(op_id)
                                ]
                                if len(owned_rows) > 1:
                                    raise RuntimeError(
                                        f"ambiguous XP boost identity for "
                                        f"op_id={op_id}: {len(owned_rows)} rows "
                                        f"record this operation and the same "
                                        f"expiry, and the descriptor recorded no "
                                        f"boost_id"
                                    )
                                if len(owned_rows) == 1:
                                    await cleanup_db.execute(
                                        "DELETE FROM leveling_active_boosts "
                                        "WHERE id=? AND source=? AND origin_op_id=?",
                                        (int(owned_rows[0][0]), boost_source,
                                         str(op_id)),
                                    )
                                elif b_rows:
                                    # Rows match (member, expiry, source) but none
                                    # records this operation: an unprovable legacy
                                    # or foreign boost. Deleting by
                                    # `(source, expires_at)` would remove another
                                    # grant, so fail closed.
                                    raise RuntimeError(
                                        f"XP boost identity for op_id={op_id} is "
                                        f"unprovable: {len(b_rows)} row(s) match "
                                        f"source={boost_source!r} and the same "
                                        f"expiry but none records this purchase "
                                        f"operation, and the descriptor recorded "
                                        f"no boost_id"
                                    )
                            # Invariant, checked inside the same transaction as
                            # the delete: an operation is never fully
                            # compensated while a boost it created is still
                            # active. This is what makes every wrong-id shape
                            # fail closed instead of completing.
                            still_live = await _boosts_for_origin(
                                cleanup_db, guild_id, user_id, str(op_id)
                            )
                            if still_live:
                                raise RuntimeError(
                                    f"XP boost rollback did not retire the boost "
                                    f"this purchase created (row(s) "
                                    f"{still_live} still record op_id={op_id}); "
                                    f"refusing to report compensation"
                                )
                            spec["inventory_rolled_back"] = True
                            spec["equip_reconciled"] = True
                            spec["role_reconciled"] = True
                            clean_spec_b = {
                                k: v
                                for k, v in spec.items()
                                if not str(k).startswith("_")
                            }
                            status_b, err_b, merged_b = (
                                await _persist_delivery_rollback_progress(
                                    str(op_id), clean_spec_b, db=cleanup_db
                                )
                            )
                            if status_b != "persisted":
                                raise RuntimeError(
                                    f"Durable XP boost progress write failed for "
                                    f"op_id={op_id}: {err_b or status_b}"
                                )
                            await cleanup_db.commit()
                            boost_in_tx = False
                            _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = dict(
                                merged_b or clean_spec_b
                            )
                    except Exception:
                        if boost_in_tx:
                            try:
                                await cleanup_db.execute("ROLLBACK")
                            except Exception:
                                pass
                        raise
            except Exception as exc:
                return await _persist_incomplete(f"XP boost rollback failed: {exc}")

    # ── Branch 4: Stackable item (`internal_type in (...)`) ─────────────
    elif internal_type in _STACKABLE_ROLLBACK_TYPES:
        if not spec.get("inventory_rolled_back") and delivered_inv is None:
            # Validation already refuses this shape; repeating the check here
            # keeps the guarantee local to the mutation: without the delivered
            # snapshot there is no way to tell this purchase's unit from an
            # independently granted one, and `rollback_item_inventory_grant`
            # would remove a unit by name.
            return await _persist_incomplete(
                f"{internal_type} rollback for {item_name!r} has no delivered "
                f"inventory snapshot; refusing to guess which unit to remove"
            )
        if not spec.get("inventory_rolled_back"):
            try:
                async with aiosqlite.connect(DB_PATH) as rb_db:
                    in_tx = False
                    try:
                        await rb_db.execute("BEGIN IMMEDIATE")
                        in_tx = True
                        cur = await rb_db.execute(
                            "SELECT status, delivery_rollback "
                            "FROM shop_purchase_operations WHERE op_id=?",
                            (str(op_id),),
                        )
                        op_row_db = await cur.fetchone()
                        if op_row_db is None:
                            await rb_db.execute("ROLLBACK")
                            in_tx = False
                            return {
                                "complete": False,
                                "durable_persisted": False,
                                "inventory_rolled_back": False,
                                "equip_reconciled": False,
                                "role_reconciled": False,
                                "error": f"Purchase operation {op_id} not found for item rollback",
                            }
                        db_status, db_raw_spec = op_row_db
                        if db_status == PURCHASE_DELIVERED:
                            await rb_db.execute("ROLLBACK")
                            in_tx = False
                            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                            return {
                                "complete": False,
                                "durable_persisted": False,
                                "inventory_rolled_back": False,
                                "equip_reconciled": False,
                                "role_reconciled": False,
                                "error": f"Cannot roll back item for delivered operation {op_id}",
                            }
                        if db_raw_spec == "":
                            await rb_db.execute("ROLLBACK")
                            in_tx = False
                            _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
                            return {
                                "complete": True,
                                "durable_persisted": True,
                                "inventory_rolled_back": True,
                                "equip_reconciled": True,
                                "role_reconciled": True,
                                "error": None,
                            }

                        already_rolled_back = False
                        if db_raw_spec is None:
                            live_mem = _PENDING_DELIVERY_ROLLBACKS.get(str(op_id))
                            if not isinstance(live_mem, dict) or not live_mem.get("_durable_unpersisted"):
                                await rb_db.execute("ROLLBACK")
                                in_tx = False
                                return {
                                    "complete": False,
                                    "durable_persisted": False,
                                    "inventory_rolled_back": False,
                                    "equip_reconciled": False,
                                    "role_reconciled": False,
                                    "error": f"Durable delivery_rollback descriptor absent for {op_id}",
                                }
                            if live_mem.get("inventory_rolled_back"):
                                already_rolled_back = True
                        else:
                            try:
                                parsed_db_spec = json.loads(db_raw_spec)
                            except Exception:
                                parsed_db_spec = None
                            if not isinstance(parsed_db_spec, dict):
                                await rb_db.execute("ROLLBACK")
                                in_tx = False
                                return {
                                    "complete": False,
                                    "durable_persisted": False,
                                    "inventory_rolled_back": False,
                                    "equip_reconciled": False,
                                    "role_reconciled": False,
                                    "error": f"Malformed delivery_rollback descriptor for {op_id}",
                                }
                            for flag in ("inventory_rolled_back", "equip_reconciled", "role_reconciled"):
                                if parsed_db_spec.get(flag):
                                    spec[flag] = True
                            if parsed_db_spec.get("inventory_rolled_back"):
                                already_rolled_back = True

                        if not already_rolled_back:
                            # The snapshot's row id — not the item name —
                            # identifies the grant. Both rollback helpers select
                            # the row by name, so a descriptor naming another
                            # item would decrement (or delete) an unrelated grant
                            # while leaving this purchase's unit in place. Checked
                            # here, inside the durable transaction and before the
                            # mutation, so it can neither act on a forged name nor
                            # fabricate a descriptor where none is durable.
                            inv_conflict = await _inventory_identity_conflict(
                                guild_id, user_id, item_name, delivered_inv,
                                prior_inv, conn=rb_db,
                            )
                            if inv_conflict is not None:
                                await rb_db.execute("ROLLBACK")
                                in_tx = False
                                # Persist the refusal, not just return it: this
                                # descriptor will be retried by every
                                # reconciliation pass, so the reason has to be
                                # durable for whoever eventually has to resolve
                                # it by hand.
                                return await _persist_incomplete(
                                    "Stackable rollback descriptor does not "
                                    "describe the delivered inventory grant: "
                                    f"{inv_conflict}"
                                )
                            inv_ok = await rollback_item_inventory_grant(
                                guild_id,
                                user_id,
                                item_name,
                                quantity=1,
                                prior_inv=prior_inv,
                                delivered_inv=delivered_inv,
                                db=rb_db,
                            )
                            if not inv_ok:
                                await rb_db.execute("ROLLBACK")
                                in_tx = False
                                return await _persist_incomplete(
                                    f"Item inventory rollback failed for {item_name!r}"
                                )

                            spec["inventory_rolled_back"] = True
                            spec["equip_reconciled"] = True
                            spec["role_reconciled"] = True
                            spec.pop("_durable_unpersisted", None)
                            clean_spec = {
                                k: v for k, v in spec.items() if not str(k).startswith("_")
                            }
                            status_p, err_p, merged_p = (
                                await _persist_delivery_rollback_progress(
                                    str(op_id), clean_spec, db=rb_db
                                )
                            )
                            if status_p != "persisted":
                                raise RuntimeError(
                                    f"Durable rollback progress write failed for "
                                    f"op_id={op_id}: {err_p or status_p}"
                                )
                            await rb_db.commit()
                            in_tx = False
                            _PENDING_DELIVERY_ROLLBACKS[str(op_id)] = dict(
                                merged_p or clean_spec
                            )
                        else:
                            await rb_db.execute("ROLLBACK")
                            in_tx = False
                            spec["inventory_rolled_back"] = True
                            spec["equip_reconciled"] = True
                            spec["role_reconciled"] = True
                    except Exception:
                        if in_tx:
                            try:
                                await rb_db.execute("ROLLBACK")
                            except Exception:
                                pass
                        raise
            except Exception as exc:
                spec["inventory_rolled_back"] = False
                spec["equip_reconciled"] = False
                spec["role_reconciled"] = False
                return await _persist_incomplete(
                    f"Item inventory rollback failed for {item_name!r}: {exc}"
                )
        spec["equip_reconciled"] = True
        spec["role_reconciled"] = True
    else:
        # Defensive: every supported `internal_type` is handled by an explicit
        # branch above (and `_validate_delivery_rollback_spec` refuses a
        # `temp_role` without a usable holding key), so a validated descriptor
        # cannot land here. It must never be able to claim completion for a
        # branch this code does not know how to roll back.
        return {
            "complete": False,
            "durable_persisted": False,
            "inventory_rolled_back": bool(spec.get("inventory_rolled_back")),
            "equip_reconciled": bool(spec.get("equip_reconciled")),
            "role_reconciled": bool(spec.get("role_reconciled")),
            "error": (
                f"No delivery_rollback branch for internal_type="
                f"{internal_type!r} (op_id={op_id})"
            ),
        }

    try:
        cleared = await update_purchase(str(op_id), delivery_rollback="")
        if not cleared:
            existing_op = await get_purchase(str(op_id))
            if existing_op and existing_op.get("delivery_rollback"):
                raise RuntimeError(
                    f"Clearing delivery_rollback matched 0 rows for op_id={op_id}"
                )
    except Exception as exc:
        return await _persist_incomplete(
            f"Failed to clear delivery_rollback descriptor for {op_id}: {exc}"
        )

    _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
    return {
        "complete": True,
        "durable_persisted": True,
        "inventory_rolled_back": True,
        "equip_reconciled": True,
        "role_reconciled": True,
        "error": None,
    }


async def _record_rollback_attempt(op_id: str, *, supported: bool = True) -> None:
    """Durably count one reconciliation attempt on a rollback candidate.

    Durable (not in-memory) so a restart cannot re-select the same permanently
    failing rows forever. Deliberately does NOT touch `updated_at`, which other
    consumers read as "last durable progress". A failed counter write only costs
    fairness for this pass, so it never blocks the compensation attempt itself.
    """
    if not supported:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE shop_purchase_operations "
                "SET delivery_rollback_attempts = "
                "COALESCE(delivery_rollback_attempts, 0) + 1 "
                "WHERE op_id=?",
                (str(op_id),),
            )
            await db.commit()
    except Exception as exc:
        print(f"[SHOP] Failed to record rollback attempt for op_id={op_id}: {exc}")


async def reconcile_delivery_rollbacks(
    *,
    bot=None,
    limit: int = 100,
    handoff_to_holdings: bool = False,
) -> dict:
    """Retry any incomplete late-delivery compensations recorded in the purchase journal."""
    global _LAST_BOT
    bot_client = bot or _LAST_BOT
    if bot_client is not None:
        _LAST_BOT = bot_client

    completed = 0
    errors: list[str] = []
    candidates: dict[str, dict | None] = {}
    order: list[str] = []
    durable_attempts: dict[str, int] = {}
    attempts_supported = True

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            # Filter actionable statuses in SQL and order deterministically, so
            # the per-pass limit is never spent on non-actionable rows. The
            # durable attempt counter leads the ordering: a row that keeps
            # failing closed yields its bounded slot to rows that have been
            # attempted fewer times, and that rotation survives a restart
            # (`_ROLLBACK_SCAN_ATTEMPTS` does not). `updated_at` stays a pure
            # "last durable progress" signal for every other consumer.
            select_sql = (
                "SELECT op_id, status, delivery_rollback, "
                "COALESCE(delivery_rollback_attempts, 0) "
                "FROM shop_purchase_operations "
                "WHERE delivery_rollback IS NOT NULL AND delivery_rollback != '' "
                "AND status IN (?, ?) "
                "ORDER BY COALESCE(delivery_rollback_attempts, 0) ASC, "
                "updated_at ASC, op_id ASC"
            )
            try:
                cursor = await db.execute(select_sql,
                                          (PURCHASE_FAILED, PURCHASE_REFUNDED))
                fetched = await cursor.fetchall()
            except Exception as exc:
                if "no such column" not in str(exc).lower():
                    raise
                # Journal not migrated yet: fall back to the pre-attempt
                # ordering rather than failing the whole pass.
                attempts_supported = False
                cursor = await db.execute(
                    "SELECT op_id, status, delivery_rollback "
                    "FROM shop_purchase_operations "
                    "WHERE delivery_rollback IS NOT NULL AND delivery_rollback != '' "
                    "AND status IN (?, ?) "
                    "ORDER BY updated_at ASC, op_id ASC",
                    (PURCHASE_FAILED, PURCHASE_REFUNDED),
                )
                fetched = [tuple(row) + (0,) for row in await cursor.fetchall()]
            for row in fetched:
                op_key = str(row[0])
                raw_spec = row[2]
                parsed = None
                try:
                    parsed = json.loads(raw_spec)
                except Exception:
                    parsed = None
                if not isinstance(parsed, dict):
                    # Left as-is so the compensation reports the malformed
                    # descriptor instead of silently dropping the candidate.
                    parsed = None
                candidates[op_key] = parsed
                durable_attempts[op_key] = int(row[3] or 0)
                order.append(op_key)
        except Exception as exc:
            errors.append(f"query delivery_rollback failed: {exc}")
            return {"completed": completed, "errors": errors}

        for mem_op_id, mem_spec in list(_PENDING_DELIVERY_ROLLBACKS.items()):
            if mem_op_id in candidates:
                db_cand = candidates[mem_op_id]
                if isinstance(db_cand, dict) and isinstance(mem_spec, dict):
                    merged = dict(db_cand)
                    for flag in ("inventory_rolled_back", "equip_reconciled", "role_reconciled"):
                        if mem_spec.get(flag) or db_cand.get(flag):
                            merged[flag] = True
                    if not merged.get("retry_entry_key") and mem_spec.get("retry_entry_key"):
                        merged["retry_entry_key"] = mem_spec["retry_entry_key"]
                    candidates[mem_op_id] = merged
                elif isinstance(mem_spec, dict):
                    candidates[mem_op_id] = dict(mem_spec)
                continue
            cur = await db.execute(
                "SELECT status FROM shop_purchase_operations WHERE op_id=?",
                (str(mem_op_id),),
            )
            row = await cur.fetchone()
            if not row or row[0] == PURCHASE_DELIVERED:
                _PENDING_DELIVERY_ROLLBACKS.pop(mem_op_id, None)
            elif row[0] in (PURCHASE_FAILED, PURCHASE_REFUNDED):
                candidates[mem_op_id] = mem_spec
                order.append(mem_op_id)

    # Fair selection: rows already attempted yield to rows with fewer durable
    # attempts, so a permanently failing batch can never keep later markers out
    # of the limited window. The SQL order (durable attempts, then least
    # recently updated, then op_id) is preserved for ties by the stable sort.
    order = sorted(
        order,
        key=lambda oid: (
            int(durable_attempts.get(oid, 0)),
            _ROLLBACK_SCAN_ATTEMPTS.get(oid, 0),
        ),
    )
    selected = order[: int(limit)] if limit is not None else order
    for stale_key in list(_ROLLBACK_SCAN_ATTEMPTS):
        if stale_key not in candidates:
            _ROLLBACK_SCAN_ATTEMPTS.pop(stale_key, None)

    for op_id in selected:
        spec = candidates[op_id]
        _ROLLBACK_SCAN_ATTEMPTS[op_id] = _ROLLBACK_SCAN_ATTEMPTS.get(op_id, 0) + 1
        if handoff_to_holdings and spec and spec.get("inventory_rolled_back") and spec.get("equip_reconciled"):
            # Inventory and equip rollback already completed; `expired_holdings`
            # in `cleanup_expired_holdings` will process the `timed_holdings` marker.
            continue
        await _record_rollback_attempt(op_id, supported=attempts_supported)
        # One malformed or crashing candidate must never abort the pass: later
        # independent candidates still get their turn.
        try:
            res = await compensate_late_delivery(
                op_id,
                spec,
                bot=bot_client,
                handoff_to_holdings=handoff_to_holdings,
            )
        except Exception as exc:
            errors.append(f"{op_id}: {type(exc).__name__}: {exc}")
            print(
                f"[SHOP] Late-delivery compensation raised for op_id={op_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if res.get("complete") or res.get("handed_off"):
            completed += 1
        elif res.get("error"):
            errors.append(f"{op_id}: {res['error']}")

    return {"completed": completed, "errors": errors}


async def reconcile_purchases(*, older_than_minutes: int = 15,
                              limit: int = 100,
                              bot=None) -> dict:
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
    global _LAST_BOT
    if bot is not None:
        _LAST_BOT = bot
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

    try:
        rb = await reconcile_delivery_rollbacks(bot=bot, limit=limit)
        stats["completed"] += int(rb.get("completed") or 0)
        stats["errors"] += len(rb.get("errors") or ())
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[SHOP] Delivery rollback reconciliation failed: {exc}")
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
                                   limit: int = 100,
                                   pending_rollbacks_only: bool = False,
                                   include_pending_rollbacks: bool = False
                                   ) -> list[dict]:
    """List purchase operations filtered by status, guild, and/or user.

    `include_pending_rollbacks` widens the listing to operations whose
    `delivery_rollback` descriptor is still present. Those are the operations an
    operator most needs to find, and they are usually in status `refunded` /
    `failed` — not `uncertain` — so a status-filtered listing silently hides
    exactly the stuck work this tool exists to surface.
    `pending_rollbacks_only` narrows it to just those.
    """
    clauses: list[str] = []
    params: list = []
    if pending_rollbacks_only:
        clauses.append("delivery_rollback IS NOT NULL AND delivery_rollback != ''")
    elif include_pending_rollbacks:
        status_clause = "" if status is None else "status=? OR "
        if status is not None:
            params.append(status)
        clauses.append(
            f"({status_clause}delivery_rollback IS NOT NULL"
            f" AND delivery_rollback != '')"
        )
    elif status is not None:
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


async def rollback_retirement_evidence(op_id: str, *, db=None,
                                      op: dict | None = None) -> dict:
    """Can this pending rollback be retired safely, on local evidence alone?

    A stuck descriptor must not be cleared by assertion: this checks whether the
    operation's own delivered effects are *provably absent from the database*,
    which is what makes "nothing is left to compensate" true rather than
    assumed. It is read-only and returns the evidence either way.

    `permanent_role` is deliberately never retirable here. Proving a refunded
    role was removed requires Discord membership, which an offline SQLite tool
    cannot observe — the honest answer is "not verifiable locally", not a guess.
    """
    if op is None:
        op = await get_purchase(op_id)
    if op is None:
        return {"safe": False, "type": None,
                "reason": f"Unknown purchase operation {op_id}", "evidence": []}
    settle_problem = _retirement_status_problem(op)
    if settle_problem is not None:
        return {"safe": False, "type": None, "reason": settle_problem,
                "evidence": []}
    raw = op.get("_raw_delivery_rollback")
    if not raw:
        return {"safe": False, "type": None,
                "reason": "Operation has no pending rollback descriptor",
                "evidence": []}
    try:
        spec = json.loads(raw)
    except Exception:
        return {"safe": False, "type": None,
                "reason": "Descriptor is unreadable; treat as unrecoverable here",
                "evidence": [raw[:200]]}
    if not isinstance(spec, dict):
        return {"safe": False, "type": None,
                "reason": "Descriptor is not a JSON object", "evidence": []}

    internal_type = spec.get("internal_type")
    guild_id, user_id = int(spec.get("guild_id") or 0), int(spec.get("user_id") or 0)
    item_name = spec.get("item_name")
    evidence: list[str] = [
        f"descriptor internal_type={internal_type!r} item_name={item_name!r} "
        f"guild={guild_id} user={user_id}",
        f"last recorded refusal: {spec.get('last_error') or '(none)'}",
    ]

    async def _evidence(conn) -> dict:
        if internal_type == "xp_boost":
            live = await _boosts_for_origin(conn, guild_id, user_id, op_id)
            evidence.append(f"live boosts recording this operation: {live or 'none'}")
            if live:
                return {"safe": False, "type": internal_type,
                        "reason": (f"{len(live)} boost(s) this purchase created are "
                                   f"still active; compensate them first"),
                        "evidence": evidence}
            return {"safe": True, "type": internal_type,
                    "reason": "no boost records this operation", "evidence": evidence}

        if internal_type in ("temp_role",) or spec.get("entry_key", "").startswith("h:"):
            cursor = await conn.execute(
                "SELECT id FROM timed_holdings WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            remaining = []
            for (holding_id,) in await cursor.fetchall():
                prov = await timed_holding_provenance(f"h:{int(holding_id)}")
                if prov is not None and str(prov.get("origin_op_id")) == str(op_id):
                    remaining.append(int(holding_id))
            evidence.append(
                f"holdings recording this operation: {remaining or 'none'}")
            if remaining:
                return {"safe": False, "type": internal_type,
                        "reason": (f"holding(s) {remaining} still belong to this "
                                   f"purchase; retire them first"),
                        "evidence": evidence}
        if internal_type == "role":
            return {
                "safe": False, "type": internal_type,
                "reason": ("a permanent role's Discord membership cannot be "
                           "verified from the database; confirm on Discord and "
                           "resolve by hand, or compensate the member directly "
                           "and record it in the operation detail"),
                "evidence": evidence + [
                    "role removal requires live Discord state this tool cannot read"
                ],
            }

        # Inventory-backed types: the delivered row is the identity evidence.
        delivered = spec.get("delivered_inventory")
        if isinstance(delivered, (list, tuple)) and delivered:
            try:
                delivered_id = int(delivered[0])
            except (TypeError, ValueError):
                delivered_id = None
            if delivered_id is not None:
                cursor = await conn.execute(
                    "SELECT id, item_name, quantity FROM inventory_items"
                    " WHERE id=?", (delivered_id,))
                row = await cursor.fetchone()
                evidence.append(
                    f"delivered inventory row {delivered_id}: "
                    f"{'present ' + str(row) if row else 'absent'}")
                if row is not None:
                    return {"safe": False, "type": internal_type,
                            "reason": (f"delivered inventory row {delivered_id} "
                                       f"still exists; it has not been rolled back"),
                            "evidence": evidence}
        else:
            evidence.append(
                "descriptor records no delivered inventory snapshot: the specific "
                "grant cannot be identified, so absence cannot be proven")

        cursor = await conn.execute(
            "SELECT id, quantity, source FROM inventory_items"
            " WHERE guild_id=? AND user_id=? AND item_name=?",
            (guild_id, user_id, item_name))
        leftover = await cursor.fetchall()
        evidence.append(f"rows still carrying {item_name!r}: {leftover or 'none'}")
        if leftover:
            # Inventory rows carry no operation binding, so a row under this name
            # cannot be attributed to this purchase OR ruled out as its grant.
            # The descriptor is exactly the thing under suspicion, so its
            # snapshot cannot be used to dismiss a row that is still there.
            return {
                "safe": False, "type": internal_type,
                "reason": (
                    f"{len(leftover)} inventory row(s) still carry "
                    f"{item_name!r} and cannot be attributed to this purchase "
                    f"from the database; compensate the member by hand if "
                    f"needed, then retire"
                ),
                "evidence": evidence + [
                    "inventory rows have no origin operation, so ownership of a "
                    "surviving row cannot be proven here"
                ],
            }

        return {"safe": True, "type": internal_type,
                "reason": ("no inventory row, holding or boost attributable to "
                           "this purchase remains, so nothing is left to "
                           "compensate"),
                "evidence": evidence}

    if db is not None:
        # Inside the caller's `BEGIN IMMEDIATE`: no other writer can change the
        # rows this verdict depends on before the retirement commits.
        return await _evidence(db)
    async with aiosqlite.connect(DB_PATH) as conn:
        return await _evidence(conn)


def _retirement_status_problem(op: dict) -> str | None:
    """Why this operation's rollback may not be retired yet, or None.

    Only a settled operation (refunded or failed) can have its rollback retired.
    A `pending`/`uncertain` operation may still be inside `process_purchase`
    with a delivery in flight, and a `delivered` operation is not being
    compensated at all — retiring either would convert "nothing is missing
    right now" into a promise that nothing ever will be.
    """
    status = str(op.get("status") or "")
    if status in (PURCHASE_REFUNDED, PURCHASE_FAILED):
        return None
    return (
        f"operation status is {status!r}; only a settled (refunded or failed) "
        f"purchase can have its rollback retired — if a delivery may still be "
        f"in flight, let it finish and the runtime retry first"
    )


async def _pending_rollback_summaries() -> dict[str, str]:
    """`{op_id: reason}` for every operation whose rollback is still pending.

    The reason is the durably recorded `last_error` when present, so an operator
    does not have to reconstruct why compensation stopped from the descriptor's
    raw JSON (or from a log line that may be long gone).
    """
    out: dict[str, str] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute(
                "SELECT op_id, delivery_rollback FROM shop_purchase_operations"
                " WHERE delivery_rollback IS NOT NULL AND delivery_rollback != ''"
                " ORDER BY created_at DESC"
            )
            fetched = await cursor.fetchall()
        except Exception:
            return out
    for op_id, raw in fetched:
        try:
            parsed = json.loads(raw)
        except Exception:
            out[str(op_id)] = "descriptor is unreadable"
            continue
        if not isinstance(parsed, dict):
            out[str(op_id)] = "descriptor is not a JSON object"
            continue
        flags = [
            flag for flag in _ROLLBACK_PROGRESS_FLAGS if not parsed.get(flag)
        ]
        reason = parsed.get("last_error") or "no reason recorded"
        out[str(op_id)] = (
            f"{reason} [outstanding: {', '.join(flags) or 'none'}]"
        )
    return out


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


async def retire_pending_rollback(op_id: str, *, confirmed: bool = False,
                                  note: str | None = None) -> tuple[bool, str]:
    """Clear a pending rollback ONLY when local evidence proves it is finished.

    Refuses when `rollback_retirement_evidence` cannot demonstrate that the
    operation's own delivered effects are gone, so this can never become a
    generic "clear the descriptor" command. The evidence summary and the operator
    note are appended to the operation's `detail`, which is the audit trail a
    later reader needs; the descriptor is then set to the durable-completion
    sentinel so no reconciliation pass re-runs the compensation.
    """
    if not confirmed:
        return False, "Confirmation required (--yes)."
    operator_note = (note or "").strip()
    if not operator_note:
        return False, "An operator note recording what was verified is required."

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Re-read and re-verify inside the write transaction. The evidence is
            # only meaningful together with the descriptor it justifies: if the
            # runtime advanced or cleared the rollback in between, the operator
            # verdict no longer describes what is on disk.
            cursor = await db.execute(
                "SELECT status, delivery_rollback, detail"
                " FROM shop_purchase_operations WHERE op_id=?", (str(op_id),))
            row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return False, f"Unknown purchase operation {op_id}"
            status_now, current, existing_detail = row
            if not current:
                await db.execute("ROLLBACK")
                return False, "Operation has no pending rollback to retire"

            verdict = await rollback_retirement_evidence(
                op_id,
                db=db,
                op={"op_id": str(op_id), "status": status_now,
                    "_raw_delivery_rollback": current},
            )
            if not verdict.get("safe"):
                await db.execute("ROLLBACK")
                return False, (
                    f"Refused: {verdict.get('reason')} "
                    f"(evidence: {'; '.join(verdict.get('evidence') or [])})"
                )

            stamp = utcnow().isoformat()
            audit = (
                f"[{stamp}] Rollback retired by operator: {operator_note} | "
                f"evidence: {verdict.get('reason')} | "
                f"{'; '.join(verdict.get('evidence') or [])}"
            )
            existing = (existing_detail or "").strip()
            merged_detail = f"{existing}\n{audit}" if existing else audit
            await db.execute(
                "UPDATE shop_purchase_operations SET delivery_rollback='',"
                " detail=?, updated_at=CURRENT_TIMESTAMP WHERE op_id=?",
                (merged_detail, str(op_id)))
            await db.commit()
        except Exception as exc:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            return False, f"Failed to retire rollback: {exc}"
    _PENDING_DELIVERY_ROLLBACKS.pop(str(op_id), None)
    return True, audit


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
    # Late-delivery rollback state. A descriptor that is still present means
    # compensation is unfinished: `reconcile_delivery_rollbacks` retries it, and
    # a descriptor that cannot be reconciled with the grant (wrong role id,
    # wrong inventory row, missing provenance) is deliberately left here for an
    # operator instead of being completed.
    raw_rb = op.get("_raw_delivery_rollback", op.get("delivery_rollback"))
    if raw_rb is None:
        lines.append("Delivery Rollback: (none recorded)")
    elif raw_rb == "":
        lines.append("Delivery Rollback: completed")
    else:
        parsed_rb = None
        try:
            parsed_rb = json.loads(raw_rb)
        except Exception:
            parsed_rb = None
        if isinstance(parsed_rb, dict):
            flags = ", ".join(
                f"{flag}={bool(parsed_rb.get(flag))}"
                for flag in ("inventory_rolled_back", "equip_reconciled",
                             "role_reconciled"))
            lines.append(f"Delivery Rollback: PENDING ({flags})")
            if parsed_rb.get("retry_entry_key"):
                lines.append(
                    f"  Retry marker : {parsed_rb['retry_entry_key']}")
            last_error = parsed_rb.get("last_error")
            if last_error:
                lines.append(f"  Last error   : {last_error}")
                lines.append(
                    f"  Last attempt : {parsed_rb.get('last_error_at') or '(unknown)'}")
            else:
                lines.append(
                    "  Last error   : (none recorded; written before refusal "
                    "reasons were persisted)")
            lines.append(f"  Descriptor   : {json.dumps(parsed_rb, sort_keys=True)}")
        else:
            lines.append(f"Delivery Rollback: PENDING (unreadable) {raw_rb!r}")
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
        "--status", default=None,
        help=("Filter operations by status when listing. By default the listing "
              "shows uncertain operations AND any operation whose late-delivery "
              "rollback is still pending. Use 'any' for no status filter."))
    parser.add_argument(
        "--pending-rollbacks", action="store_true",
        help=("List only operations whose delivery_rollback descriptor is still "
              "pending (unfinished compensation, including refused ones)."))
    parser.add_argument(
        "--all-statuses", action="store_true",
        help="List operations of every status, not just uncertain/pending rollbacks.")
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
        "--check-rollback", action="store_true",
        help=("With --op-id: report whether that operation's pending "
              "late-delivery rollback can be retired safely on local evidence, "
              "without changing anything."))
    parser.add_argument(
        "--retire-rollback", action="store_true",
        help=("With --op-id --yes --note: retire a pending late-delivery rollback "
              "ONLY when the operation's own delivered effects are provably absent "
              "from the database. Records the evidence and the operator note in "
              "the operation detail."))
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
        status_filter = args.status
        if isinstance(status_filter, str) and status_filter.strip().lower() in (
                "any", "all"):
            status_filter = None
        if not args.op_id:
            if args.pending_rollbacks:
                ops = await list_purchase_operations(
                    status=None, guild_id=args.guild_id, user_id=args.user_id,
                    limit=args.limit, pending_rollbacks_only=True)
            elif args.all_statuses or status_filter is not None:
                ops = await list_purchase_operations(
                    status=status_filter, guild_id=args.guild_id,
                    user_id=args.user_id, limit=args.limit)
            else:
                # Default: the work an operator is looking for. A pending
                # rollback is usually attached to a refunded/failed operation, so
                # listing only `uncertain` hides it.
                ops = await list_purchase_operations(
                    status=PURCHASE_UNCERTAIN, guild_id=args.guild_id,
                    user_id=args.user_id, limit=args.limit,
                    include_pending_rollbacks=True)
            if args.json:
                print(json.dumps(
                    {"operations": ops,
                     "pending_rollbacks": await _pending_rollback_summaries()},
                    indent=2, default=str))
            elif not ops:
                if args.pending_rollbacks:
                    print("No operations have a pending late-delivery rollback.")
                else:
                    print("No purchase operations found for this filter.")
            else:
                if args.pending_rollbacks:
                    label = "with a pending late-delivery rollback"
                elif args.all_statuses or status_filter is not None:
                    label = f"with status={status_filter!r}"
                else:
                    label = "uncertain or with a pending late-delivery rollback"
                print(f"Found {len(ops)} operation(s) {label}:")
                pending = await _pending_rollback_summaries()
                for op in ops:
                    print(
                        f"  {op['op_id']}  guild={op['guild_id']} "
                        f"user={op['user_id']} item={op['item_name']!r} "
                        f"amount={op['amount']} {op['currency']} "
                        f"charged={op['charged']} refunded={op['refunded']} "
                        f"reserved={op['stock_reserved']} "
                        f"returned={op['stock_returned']} "
                        f"detail={op.get('detail')!r}")
                    summary = pending.get(str(op["op_id"]))
                    if summary:
                        print(f"      rollback    : PENDING — {summary}")
                if pending and not args.pending_rollbacks:
                    print(
                        f"  ({len(pending)} operation(s) with a pending "
                        f"late-delivery rollback; use --op-id <id> for the full "
                        f"evidence and reason)")
            return 0

        evidence = await inspect_purchase_operation(args.op_id)
        if evidence is None:
            if args.json:
                print(json.dumps(
                    {"error": f"Unknown operation: {args.op_id}"}, indent=2))
            else:
                print(f"Error: Unknown purchase operation {args.op_id!r}.")
            return 1

        if args.check_rollback or args.retire_rollback:
            verdict = await rollback_retirement_evidence(args.op_id)
            if args.check_rollback and not args.retire_rollback:
                if args.json:
                    print(json.dumps({"op_id": args.op_id, "verdict": verdict},
                                     indent=2, default=str))
                else:
                    print(format_purchase_inspection(evidence))
                    print()
                    print(f"Rollback retirement: "
                          f"{'SAFE' if verdict['safe'] else 'NOT SAFE'}")
                    print(f"  Why: {verdict['reason']}")
                    for line in verdict["evidence"]:
                        print(f"  evidence: {line}")
                return 0 if verdict["safe"] else 1
            if not args.yes:
                msg = ("Refusing to retire a rollback without --yes. Inspect with "
                       "--check-rollback first.")
                print(json.dumps({"success": False, "error": msg}, indent=2)
                      if args.json else f"[DRY-RUN] {msg}")
                return 2
            if not (args.note and args.note.strip()):
                msg = ("Refusing to retire a rollback without an operator --note "
                       "recording what was verified.")
                print(json.dumps({"success": False, "error": msg}, indent=2)
                      if args.json else f"Error: {msg}")
                return 2
            retired, detail = await retire_pending_rollback(
                args.op_id, confirmed=True, note=args.note)
            payload = {"success": retired, "op_id": args.op_id,
                       "reason": verdict["reason"],
                       "evidence": verdict["evidence"], "detail": detail}
            if args.json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                print(f"{'Retired' if retired else 'Not retired'} {args.op_id}: "
                      f"{detail}")
            return 0 if retired else 1

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
