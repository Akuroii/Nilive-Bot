"""Level-reward entitlements. One table, one owner at a time.

pending and failed are retryable. processing is ownership and expires
after LEASE_SECONDS. fulfilled is terminal. A SELECT of pending is not
ownership: delivery happens only for rows this attempt's UPDATE reserved
under BEGIN IMMEDIATE.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import DB_PATH

LEASE_SECONDS = 60
TRACK_ROLE = "role"
TRACK_CURRENCY = "currency"
TRACK_SHOP = "shop"
TRACK_BOOST = "boost"
BOOST_REF = "xp_boost"
BACKFILL_NAME = "legacy_fulfilled_no_payout"
SHOP_REWARD_TYPES = ("custom", "title", "potion", "temp_role")
_INVENTORY_TYPE = {
    "custom": "shop_custom",
    "title": "title",
    "potion": "potion",
    "temp_role": "temp_role",
}
# Metadata only. Not part of identity, eligibility, reserve, or payout.
SOURCE_SETXP = "setxp"
SOURCE_DASHBOARD = "dashboard"
SOURCE_BACKFILL = "legacy_backfill"

_CREATE_CLAIMS = """
    CREATE TABLE IF NOT EXISTS level_reward_claims (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id               INTEGER NOT NULL,
        user_id                INTEGER NOT NULL,
        reward_level           INTEGER NOT NULL,
        track                  TEXT NOT NULL,
        reward_ref             TEXT NOT NULL,
        payload_json           TEXT NOT NULL,
        status                 TEXT NOT NULL DEFAULT 'pending',
        owner_token            TEXT,
        processing_started_at  TEXT,
        last_error             TEXT,
        created_at             TEXT NOT NULL,
        fulfilled_at           TEXT,
        source                 TEXT NOT NULL DEFAULT '',
        UNIQUE (guild_id, user_id, reward_level, track, reward_ref)
    )
"""

_CREATE_MIGRATIONS = """
    CREATE TABLE IF NOT EXISTS level_claim_migrations (
        name       TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def progress_level(xp: int) -> int:
    from utils.xp_calculator import xp_progress
    return xp_progress(max(0, int(xp or 0)))[0]


async def ensure_tables(db: aiosqlite.Connection | None = None) -> None:
    async def apply(connection):
        await connection.execute(_CREATE_CLAIMS)
        await connection.execute(_CREATE_MIGRATIONS)
        cursor = await connection.execute("PRAGMA table_info(level_reward_claims)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "source" not in columns:
            await connection.execute(
                "ALTER TABLE level_reward_claims "
                "ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        await connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_lrc_owner
            ON level_reward_claims(guild_id, user_id, status)
        """)

    if db is not None:
        await apply(db)
        return
    async with aiosqlite.connect(DB_PATH) as connection:
        await apply(connection)
        await connection.commit()


async def snapshot_multipliers(guild_id: int, user_id: int,
                               member=None, bot=None) -> dict:
    """Earn-time prestige multipliers, frozen onto a new entitlement.

    Looked up before the claim/XP write lock so a prestige read cannot
    deadlock on that BEGIN IMMEDIATE. Failures grant the raw amount.
    """
    try:
        from utils.prestige import get_prestige_earn_multiplier
        coins = await get_prestige_earn_multiplier(
            guild_id, user_id, "balance", member=member, bot=bot)
        diamonds = await get_prestige_earn_multiplier(
            guild_id, user_id, "diamonds", member=member, bot=bot)
        return {"balance": float(coins), "diamonds": float(diamonds)}
    except Exception as e:
        print(f"[LEVEL CLAIM] multiplier lookup failed; snapshotting raw "
              f"(guild={guild_id} user={user_id}): {e}")
        return {"balance": 1.0, "diamonds": 1.0}


def _freeze_amount(amount: int, currency: str, multipliers: dict) -> int:
    mult = float((multipliers or {}).get(currency, 1.0) or 1.0)
    final = int(round(int(amount) * mult))
    if final == 0 and int(amount) > 0:
        return int(amount)
    return final


async def _definitions(db: aiosqlite.Connection, guild_id: int) -> dict:
    """One identity per (level, track, ref). Currency rows of the same
    currency at the same level sum into that identity's raw amount."""
    identities = {}
    cursor = await db.execute("""
        SELECT level, role_id FROM leveling_rewards WHERE guild_id=?
    """, (guild_id,))
    for level, role_id in await cursor.fetchall():
        if not role_id:
            continue
        identities[(int(level), TRACK_ROLE, str(int(role_id)))] = {
            "role_id": int(role_id),
        }
    cursor = await db.execute("""
        SELECT level, currency, amount FROM leveling_currency_rewards
        WHERE guild_id=?
    """, (guild_id,))
    for level, currency, amount in await cursor.fetchall():
        if currency not in ("balance", "diamonds"):
            continue
        if not amount or int(amount) <= 0:
            continue
        key = (int(level), TRACK_CURRENCY, currency)
        slot = identities.setdefault(key, {"currency": currency, "amount": 0})
        slot["amount"] += int(amount)
    cursor = await db.execute("""
        SELECT r.level, r.item_id, r.quantity, s.name, s.type,
               s.role_id, s.duration_hours, s.xp_boost_multiplier
        FROM leveling_shop_rewards r
        JOIN shop_items s ON s.id = r.item_id AND s.guild_id = r.guild_id
        WHERE r.guild_id = ?
    """, (guild_id,))
    for level, item_id, quantity, name, item_type, role_id, duration_hours, multiplier in await cursor.fetchall():
        if item_type not in SHOP_REWARD_TYPES or not name:
            continue
        if not quantity or int(quantity) < 1:
            continue
        raw = {
            "item_id": int(item_id),
            "name": name,
            "type": item_type,
            "quantity": int(quantity),
        }
        if item_type == "temp_role":
            if not role_id or not duration_hours or int(duration_hours) <= 0:
                continue
            raw["role_id"] = int(role_id)
            raw["duration_hours"] = int(duration_hours)
        elif item_type == "potion":
            if not multiplier or float(multiplier) <= 1 or not duration_hours:
                continue
            from utils.potion_engine import EFFECT_XP_BOOST, build_metadata
            raw["metadata"] = build_metadata(
                EFFECT_XP_BOOST, float(multiplier), int(duration_hours))
        identities[(int(level), TRACK_SHOP, str(int(item_id)))] = raw
    cursor = await db.execute("""
        SELECT level, multiplier, duration_hours
        FROM leveling_boost_rewards WHERE guild_id=?
    """, (guild_id,))
    for level, multiplier, duration_hours in await cursor.fetchall():
        if not multiplier or float(multiplier) <= 1:
            continue
        if not duration_hours or int(duration_hours) < 1:
            continue
        identities[(int(level), TRACK_BOOST, BOOST_REF)] = {
            "multiplier": float(multiplier),
            "duration_hours": int(duration_hours),
        }
    return identities


def _payload(track: str, raw: dict, multipliers: dict | None) -> str:
    if track == TRACK_ROLE:
        return json.dumps({"role_id": int(raw["role_id"])})
    if track == TRACK_SHOP:
        # Frozen at entitlement creation. Claim delivery must not re-read
        # shop_items, so price, stock, and purchase gates are omitted.
        body = {
            "item_id": int(raw["item_id"]),
            "name": raw["name"],
            "type": raw["type"],
            "quantity": int(raw["quantity"]),
        }
        if raw["type"] == "temp_role":
            body["role_id"] = int(raw["role_id"])
            body["duration_hours"] = int(raw["duration_hours"])
        if raw.get("metadata"):
            body["metadata"] = raw["metadata"]
        return json.dumps(body)
    if track == TRACK_BOOST:
        # Frozen at entitlement creation. Claim delivery must not re-read
        # leveling_boost_rewards.
        return json.dumps({
            "multiplier": float(raw["multiplier"]),
            "duration_hours": int(raw["duration_hours"]),
        })
    amount = int(raw["amount"])
    if multipliers is not None:
        amount = _freeze_amount(amount, raw["currency"], multipliers)
    return json.dumps({"currency": raw["currency"], "amount": amount})


async def _insert_identity(db, guild_id, user_id, level, track, ref,
                           payload: str, status: str, now: str,
                           source: str) -> None:
    fulfilled_at = now if status == "fulfilled" else None
    await db.execute("""
        INSERT OR IGNORE INTO level_reward_claims
            (guild_id, user_id, reward_level, track, reward_ref,
             payload_json, status, created_at, fulfilled_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (guild_id, user_id, level, track, ref, payload, status, now,
          fulfilled_at, source or ""))


async def record_crossing(db: aiosqlite.Connection, guild_id: int,
                          user_id: int, old_xp: int, new_xp: int,
                          multipliers: dict | None = None, *,
                          source: str) -> int:
    """Insert pending rows for levels crossed by an XP increase.

    Caller owns the transaction. Eligibility is xp_progress(xp).level,
    not the stored levels.level column. Existing rows are left alone,
    so a reset and re-level cannot pay a second time. source is stored
    once and is not read by reserve or payout.
    """
    old_level = progress_level(old_xp)
    new_level = progress_level(new_xp)
    if new_level <= old_level:
        return 0
    await ensure_tables(db)
    identities = await _definitions(db, guild_id)
    now = _iso(_now())
    created = 0
    for (level, track, ref), raw in identities.items():
        if level <= old_level or level > new_level:
            continue
        await _insert_identity(
            db, guild_id, user_id, level, track, ref,
            _payload(track, raw, multipliers or {}),
            "pending", now, source)
        created += 1
    return created


async def backfill_legacy_claims() -> int:
    """One-shot: already-reached rewards become fulfilled. No payout."""
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT 1 FROM level_claim_migrations WHERE name=?",
                (BACKFILL_NAME,))
            if await cursor.fetchone():
                await db.commit()
                return 0
            cursor = await db.execute("SELECT DISTINCT guild_id FROM levels")
            guilds = [row[0] for row in await cursor.fetchall()]
            defs = {guild: await _definitions(db, guild) for guild in guilds}
            cursor = await db.execute(
                "SELECT guild_id, user_id, xp FROM levels")
            members = await cursor.fetchall()
            now = _iso(_now())
            inserted = 0
            for guild_id, user_id, xp in members:
                level = progress_level(xp)
                for (reward_level, track, ref), raw in defs.get(guild_id, {}).items():
                    if reward_level > level:
                        continue
                    await _insert_identity(
                        db, guild_id, user_id, reward_level, track, ref,
                        _payload(track, raw, None), "fulfilled", now,
                        SOURCE_BACKFILL)
                    inserted += 1
            await db.execute(
                "INSERT INTO level_claim_migrations (name, applied_at) VALUES (?, ?)",
                (BACKFILL_NAME, now))
            await db.commit()
            return inserted
        except Exception:
            await db.execute("ROLLBACK")
            raise


def _reserve_sql(claim_ids: list[int] | None) -> str:
    sql = """
        UPDATE level_reward_claims
        SET status='processing',
            owner_token=?,
            processing_started_at=?,
            last_error=NULL
        WHERE guild_id=? AND user_id=?
          AND status != 'fulfilled'
          AND (
                status IN ('pending', 'failed')
                OR (
                    status='processing'
                    AND (processing_started_at IS NULL
                         OR processing_started_at < ?)
                )
              )
    """
    if claim_ids:
        marks = ",".join("?" for _ in claim_ids)
        sql += f" AND id IN ({marks})"
    return sql


async def _owned_rows(db, token: str) -> list[dict]:
    cursor = await db.execute("""
        SELECT id, reward_level, track, reward_ref, payload_json
        FROM level_reward_claims
        WHERE owner_token=? AND status='processing'
        ORDER BY reward_level, id
    """, (token,))
    rows = []
    for claim_id, level, track, ref, payload in await cursor.fetchall():
        rows.append({
            "id": claim_id,
            "reward_level": level,
            "track": track,
            "reward_ref": ref,
            "payload": json.loads(payload),
            "owner_token": token,
        })
    return rows


async def _mark(db, claim_id: int, token: str, status: str,
                error: str | None = None) -> bool:
    now = _iso(_now())
    if status == "fulfilled":
        cursor = await db.execute("""
            UPDATE level_reward_claims
            SET status='fulfilled', fulfilled_at=?, owner_token=NULL,
                processing_started_at=NULL, last_error=NULL
            WHERE id=? AND owner_token=? AND status='processing'
        """, (now, claim_id, token))
    else:
        await db.execute("""
            UPDATE level_reward_claims
            SET status='failed', last_error=?, owner_token=NULL,
                processing_started_at=NULL
            WHERE id=? AND owner_token=? AND status='processing'
        """, ((error or "delivery failed")[:500], claim_id, token))
    changed = await db.execute("SELECT changes()")
    count = (await changed.fetchone())[0]
    return int(count or 0) == 1


async def deliver_role(member, role_id: int, reason: str) -> None:
    """Existing level-reward role add. Already-present is not a second reward."""
    from utils.permissions import check_bot_role_position

    guild = getattr(member, "guild", None)
    if guild is None:
        raise RuntimeError("Member has no guild")
    role = guild.get_role(int(role_id))
    if role is None:
        raise RuntimeError("Role not found")
    can_assign, warning = check_bot_role_position(guild, role)
    if not can_assign:
        raise RuntimeError(warning or "Role is above the bot")
    await member.add_roles(role, reason=reason)


# ─── Level reward role progression ─────────────────────────────────────
# A guild configures this on the Leveling page as `remove_old_reward_role`:
#
#   OFF (0, the default) — Level reward roles accumulate, exactly as they
#   always did: reaching Level 10 leaves the Level 5 role in place.
#
#   ON (1) — the progression is exclusive: when a higher Level reward role is
#   successfully delivered, the superseded lower ones are removed, so the
#   member wears the role of the highest level they have reached.
#
# The rule is evaluated on the claim ledger, never on live configuration:
# `level_reward_claims` is already the frozen record of what a member is
# entitled to and what was actually delivered, so a superseded role is one
# from a FULFILLED role claim at a LOWER reward_level. That keeps the whole
# feature inside the existing claim-based path: no new table, no reward
# engine, no change to claim identity or entitlement.
#
# Three things are deliberately NOT done here:
#   * other tracks are never touched (currency, shop, boost, temp roles are
#     not part of the progression and keep their claim semantics);
#   * roles that do not appear in a fulfilled role claim are never touched,
#     so manually granted / other-system roles survive;
#   * a role that is ALSO the reward of the highest fulfilled level is never
#     removed (a role id configured at two levels stays put).
async def superseded_role_ids(db, guild_id: int, user_id: int,
                              level: int, role_id: int) -> list[int]:
    """The Level-reward role ids the just-delivered (level, role_id) obsoletes.

    `level`/`role_id` are the claim being delivered; they join the fulfilled
    set so the answer is right even when this claim is being retried or when a
    later claim was already fulfilled. An empty list means "nothing to remove".
    """
    cursor = await db.execute("""
        SELECT reward_level, payload_json FROM level_reward_claims
        WHERE guild_id=? AND user_id=? AND track=? AND status='fulfilled'
    """, (guild_id, user_id, TRACK_ROLE))
    by_level: dict[int, set[int]] = {}
    for claimed_level, payload in await cursor.fetchall():
        try:
            claimed_role = int(json.loads(payload)["role_id"])
        except (TypeError, ValueError, KeyError):
            continue
        by_level.setdefault(int(claimed_level), set()).add(claimed_role)
    by_level.setdefault(int(level), set()).add(int(role_id))
    highest = max(by_level)
    keep = by_level[highest]
    remove: set[int] = set()
    for claimed_level, role_ids in by_level.items():
        if claimed_level < highest:
            remove |= role_ids
    return sorted(remove - keep)


async def enforce_role_progression(member, guild_id: int, user_id: int,
                                   level: int, role_id: int,
                                   failures: list | None = None) -> list[int]:
    """Drop superseded Level-reward roles; return IDs actually removed.

    Call only after every required role at the highest Level is fulfilled and
    present. Removal failures are reported separately and never change reward
    definitions, claim state, or XP. A caller that omits ``failures`` retains
    the legacy raise-on-error behavior.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        stale = await superseded_role_ids(db, guild_id, user_id, level, role_id)
    if not stale:
        return []
    guild = getattr(member, "guild", None)
    held = set(getattr(member, "roles", None) or [])
    removed = []
    for stale_id in stale:
        role = guild.get_role(stale_id) if guild is not None else None
        if role is None or role not in held:
            continue
        try:
            await member.remove_roles(
                role, reason=f"Replaced by the Level {level} reward role")
            removed.append(stale_id)
        except Exception as exc:
            if failures is None:
                raise
            failures.append((stale_id, str(exc)))
    return removed


async def reconcile_role_progression(member, guild_id: int, user_id: int) -> dict:
    """Restore earned Level roles and optionally enforce exclusivity.

    This is the shared membership-only reconciliation used by both rejoin and
    the member's Claim All path. It never writes the claim ledger or pays a
    reward. With ``remove_old_reward_role`` OFF, every fulfilled role claim is
    restored because Level roles accumulate. With it ON, only the highest
    fulfilled Level set is restored and lower fulfilled roles are removed after
    every role in that set is present. A failed/pending/processing same-Level
    claim or Discord add failure blocks replacement; unrelated roles are never
    considered.

    Returns ``{"restored": [...], "removed": [...], "failed": [...],
    "blocked": bool}``. Failures are role IDs with an explanatory string.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute("""
            SELECT reward_level, payload_json, status FROM level_reward_claims
            WHERE guild_id=? AND user_id=? AND track=?
        """, (guild_id, user_id, TRACK_ROLE))
        claim_rows = await cursor.fetchall()

    result: dict = {"restored": [], "removed": [], "failed": [],
                    "blocked": False}
    by_level: dict[int, dict] = {}
    for claimed_level, payload, status in claim_rows:
        level = int(claimed_level)
        group = by_level.setdefault(level, {"role_ids": set(), "unfulfilled": False})
        if status != "fulfilled":
            group["unfulfilled"] = True
        try:
            role_id = int(json.loads(payload)["role_id"])
        except (TypeError, ValueError, KeyError):
            group["unfulfilled"] = True
            result["failed"].append((None, f"Invalid role claim payload at Level {level}"))
            continue
        if status == "fulfilled":
            group["role_ids"].add(role_id)

    if not by_level:
        return result

    from utils.xp_calculator import get_leveling_config
    exclusive_roles = bool(
        (await get_leveling_config(guild_id)).get("remove_old_reward_role"))
    # The target is the highest Level whose role the member has actually been
    # given (fulfilled). A higher Level that is still pending/failed is not a
    # target: its role cannot be restored (it was never delivered) and it must
    # not cause the member to lose the role they already earned.
    delivered_levels = [lvl for lvl, grp in by_level.items() if grp["role_ids"]]
    if not delivered_levels:
        result["blocked"] = bool(result["failed"]) or (
            exclusive_roles and any(grp["unfulfilled"] for grp in by_level.values()))
        return result
    highest = max(delivered_levels)
    group = by_level[highest]
    keep = sorted(group["role_ids"])

    guild = getattr(member, "guild", None)
    if guild is None:
        result["failed"].append((None, "Member has no guild"))
        result["blocked"] = True
        return result

    # OFF means every fulfilled Level role remains part of the member's
    # accumulated progression. For ON, restore only the highest set so an
    # exclusive progression doesn't re-grant roles that were superseded.
    restore_groups = ([(highest, group)] if exclusive_roles else
                     sorted(by_level.items()))
    held = set(getattr(member, "roles", None) or [])
    for reward_level, role_group in restore_groups:
        for role_id in sorted(role_group["role_ids"]):
            role = guild.get_role(role_id)
            if role is None:
                result["failed"].append((role_id, "Role no longer exists"))
                continue
            if role in held:
                continue
            try:
                await deliver_role(
                    member, role_id,
                    f"Level {reward_level} reward (restored)")
                result["restored"].append(role_id)
                held = set(getattr(member, "roles", None) or [])
                if role not in held:
                    result["failed"].append(
                        (role_id, "Role add returned without membership"))
            except Exception as exc:
                result["failed"].append((role_id, str(exc)))

    if not exclusive_roles:
        result["blocked"] = bool(result["failed"])
        return result

    # Re-evaluate the member after all add attempts. Partial success must never
    # strip a lower role that is still the member's only reliable reward.
    held = set(getattr(member, "roles", None) or [])
    if (result["failed"] or group["unfulfilled"] or any(
            guild.get_role(role_id) is None or guild.get_role(role_id) not in held
            for role_id in keep)):
        result["blocked"] = True
        return result

    result["removed"] = await enforce_role_progression(
        member, guild_id, user_id, highest, keep[0],
        failures=result["failed"])
    result["blocked"] = bool(result["failed"])
    return result

async def _grant_inventory(db, guild_id: int, user_id: int, payload: dict,
                          metadata: dict | None = None) -> None:
    from utils.inventory import give_item
    item_type = _INVENTORY_TYPE[payload["type"]]
    await give_item(
        guild_id, user_id, payload["name"],
        quantity=int(payload["quantity"]),
        item_type=item_type,
        metadata=metadata if metadata is not None else payload.get("metadata"),
        source="level_claim", db=db)


async def _ensure_temp_role_units(claim_id: int, guild_id: int, user_id: int,
                                  role_id: int, quantity: int,
                                  duration_hours: int) -> str:
    """Write exactly `quantity` expiry rows for this claim. A retry inserts none."""
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT unit_index, expires_at FROM temp_roles "
                "WHERE claim_id=? ORDER BY unit_index",
                (claim_id,))
            existing = await cursor.fetchall()
            have = {row[0] for row in existing}
            if existing and existing[0][1]:
                expires_at = existing[0][1]
            else:
                expires_at = (
                    _now() + timedelta(hours=int(duration_hours))
                ).isoformat()
            for index in range(int(quantity)):
                if index in have:
                    continue
                await db.execute("""
                    INSERT OR IGNORE INTO temp_roles
                        (guild_id, user_id, role_id, expires_at, source,
                         claim_id, unit_index)
                    VALUES (?, ?, ?, ?, 'level_claim', ?, ?)
                """, (guild_id, user_id, int(role_id), expires_at,
                      claim_id, index))
            await db.commit()
            return expires_at
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def claim_available(guild_id: int, user_id: int, *,
                          member=None, bot=None,
                          claim_ids: list[int] | None = None) -> dict:
    """Reserve, deliver, and finalize. The losing attempt delivers nothing."""
    await ensure_tables()
    token = uuid.uuid4().hex
    now = _now()
    started = _iso(now)
    expired_before = _iso(now - timedelta(seconds=LEASE_SECONDS))
    result = {
        "owner_token": token,
        "owned": 0,
        "fulfilled": [],
        "failed": [],
        "delivered_currency": 0,
        "delivered_roles": 0,
        "delivered_shop": 0,
        "delivered_boost": 0,
        # Explicit reconciliation of already-fulfilled Level roles (see
        # reconcile_role_progression). Empty unless the toggle is ON and this
        # was a member-initiated full pass.
        "reconciled": {"restored": [], "removed": [], "failed": [],
                        "blocked": False},
    }
    roles = []
    temp_roles = []
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            params = [token, started, guild_id, user_id, expired_before]
            if claim_ids:
                params.extend(claim_ids)
            await db.execute(_reserve_sql(claim_ids), params)
            owned = await _owned_rows(db, token)
            result["owned"] = len(owned)
            for row in owned:
                if row["track"] == TRACK_ROLE:
                    roles.append(row)
                    continue
                if row["track"] == TRACK_SHOP and row["payload"].get("type") == "temp_role":
                    temp_roles.append(row)
                    continue
                await db.execute(f"SAVEPOINT claim_{row['id']}")
                try:
                    if row["track"] == TRACK_CURRENCY:
                        from utils.economy_safe import safe_credit
                        payload = row["payload"]
                        await safe_credit(
                            guild_id, user_id, int(payload["amount"]),
                            currency=payload["currency"],
                            reason=f"Level {row['reward_level']} reward",
                            source="level_claim", db=db)
                    elif row["track"] == TRACK_SHOP:
                        await _grant_inventory(db, guild_id, user_id, row["payload"])
                    elif row["track"] == TRACK_BOOST:
                        from utils.xp_calculator import grant_xp_boost
                        payload = row["payload"]
                        await grant_xp_boost(
                            guild_id, user_id, float(payload["multiplier"]),
                            int(payload["duration_hours"]),
                            source=f"level_claim:{row['id']}", db=db)
                    else:
                        raise RuntimeError(f"unknown claim track {row['track']}")
                    if not await _mark(db, row["id"], token, "fulfilled"):
                        raise RuntimeError("lost claim ownership before fulfillment")
                    await db.execute(f"RELEASE SAVEPOINT claim_{row['id']}")
                    result["fulfilled"].append(row["id"])
                    if row["track"] == TRACK_CURRENCY:
                        result["delivered_currency"] += 1
                    elif row["track"] == TRACK_BOOST:
                        result["delivered_boost"] += 1
                    else:
                        result["delivered_shop"] += 1
                except Exception as e:
                    await db.execute(
                        f"ROLLBACK TO SAVEPOINT claim_{row['id']}")
                    if await _mark(db, row["id"], token, "failed", str(e)):
                        result["failed"].append(row["id"])
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

    # Read once per pass: `remove_old_reward_role` is a guild setting (default
    # OFF). With it OFF, claim delivery never removes roles; this Claim All path
    # reconciles fulfilled roles only when the setting is ON, while the member-
    # join callback separately restores all accumulated fulfilled roles when
    # OFF. The setting gates the post-delivery enforcement below and this
    # full-pass reconciliation.
    exclusive_roles = False
    if roles or (member is not None and claim_ids is None):
        from utils.xp_calculator import get_leveling_config
        exclusive_roles = bool(
            (await get_leveling_config(guild_id)).get("remove_old_reward_role"))

    for row in roles:
        try:
            if member is None:
                raise RuntimeError("Member not available for role delivery")
            await deliver_role(
                member, row["payload"]["role_id"],
                f"Level {row['reward_level']} reward")
            # Role replacement is deliberately deferred until every role
            # claim at the highest Level has been delivered. Removing a lower
            # role here would be unsafe when another same-Level add later fails.
        except Exception as e:
            async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    if await _mark(db, row["id"], token, "failed", str(e)):
                        result["failed"].append(row["id"])
                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise
            continue
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if await _mark(db, row["id"], token, "fulfilled"):
                    result["fulfilled"].append(row["id"])
                    result["delivered_roles"] += 1
                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

    for row in temp_roles:
        payload = row["payload"]
        try:
            if member is None:
                raise RuntimeError("Member not available for role delivery")
            await deliver_role(
                member, int(payload["role_id"]),
                f"Level {row['reward_level']} reward")
            expires_at = await _ensure_temp_role_units(
                row["id"], guild_id, user_id, int(payload["role_id"]),
                int(payload["quantity"]), int(payload["duration_hours"]))
        except Exception as e:
            async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    if await _mark(db, row["id"], token, "failed", str(e)):
                        result["failed"].append(row["id"])
                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise
            continue
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(f"SAVEPOINT claim_{row['id']}")
                try:
                    await _grant_inventory(
                        db, guild_id, user_id, payload,
                        metadata={
                            "role_id": int(payload["role_id"]),
                            "expires_at": expires_at,
                            "duration_hours": int(payload["duration_hours"]),
                        })
                    if not await _mark(db, row["id"], token, "fulfilled"):
                        raise RuntimeError("lost claim ownership before fulfillment")
                    await db.execute(f"RELEASE SAVEPOINT claim_{row['id']}")
                    result["fulfilled"].append(row["id"])
                    result["delivered_shop"] += 1
                except Exception as e:
                    await db.execute(f"ROLLBACK TO SAVEPOINT claim_{row['id']}")
                    if await _mark(db, row["id"], token, "failed", str(e)):
                        result["failed"].append(row["id"])
                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

    # Explicit reconciliation on the member's own Claim All pass (never on a
    # message, never on a timer, never in the background). Requires a member and
    # a full pass, and runs there only when exclusivity is ON. The member-join
    # listener also calls the shared reconciliation: with the setting OFF it
    # restores all fulfilled roles because the progression accumulates.
    #
    # FUTURE — member blacklist (still under construction, NOT
    # `leveling_blacklist_roles`, which is only a role-based XP opt-out; see
    # LEVELING_FOLLOWUP_AUDIT.md §5): a blacklisted member must not have roles
    # this reconciliation would hand back, and the blacklist check belongs
    # here as well as in front of the delivery loops above.
    if exclusive_roles and member is not None and claim_ids is None:
        result["reconciled"] = await reconcile_role_progression(
            member, guild_id, user_id)
    return result


async def list_claims(guild_id: int, user_id: int) -> list[dict]:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, reward_level, track, reward_ref, payload_json,
                   status, last_error, created_at, fulfilled_at, source
            FROM level_reward_claims
            WHERE guild_id=? AND user_id=?
            ORDER BY reward_level, id
        """, (guild_id, user_id))
        found = await cursor.fetchall()
    rows = []
    for (claim_id, level, track, ref, payload, status, error,
         created_at, fulfilled_at, source) in found:
        rows.append({
            "id": claim_id,
            "reward_level": level,
            "track": track,
            "reward_ref": ref,
            "payload": json.loads(payload),
            "status": status,
            "last_error": error,
            # History, for the /level Stats view: when the crossing was recorded,
            # when it was delivered, and which path recorded it. Read-only —
            # nothing in the claim engine reads these back.
            "created_at": created_at,
            "fulfilled_at": fulfilled_at,
            "source": source,
        })
    return rows


async def current_level_definitions(guild_id: int, level: int) -> list[dict]:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        identities = await _definitions(db, guild_id)
    rows = []
    for (reward_level, track, ref), raw in sorted(identities.items()):
        if reward_level != level:
            continue
        rows.append({
            "reward_level": reward_level,
            "track": track,
            "reward_ref": ref,
            "payload": json.loads(_payload(track, raw, None)),
        })
    return rows
