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
# Booster-only rewards share the existing tracks. Their ledger identity is the
# normal ref with this prefix, so a Booster reward never collides with, or
# replaces, the Everyone reward at the same level. The ref is only a key:
# delivery reads the payload.
BOOSTER_PREFIX = "booster:"
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
SOURCE_SETLEVEL = "setlevel"
SOURCE_DASHBOARD = "dashboard"
SOURCE_BACKFILL = "legacy_backfill"
# Administrative writers that existed BEFORE `operation_id` was added to this
# table. Their rows carry no operation id, so `source` is the only surviving
# record that the crossing was an administrator's change rather than play.
# Verified against the base commit: `/setxp` wrote SOURCE_SETXP and the
# dashboard edit wrote SOURCE_DASHBOARD; message/voice XP is awarded through
# utils.reward_engine with source 'leveling', and other XP sources (missions,
# minigames, shop) use their own names. Only these two are administrative.
LEGACY_ADMIN_SOURCES = (SOURCE_SETXP, SOURCE_DASHBOARD)


def is_administrative_claim(claim: dict) -> bool:
    """True when a claim row came from an administrative XP change.

    New rows record the change's `operation_id`. Rows created before that
    column existed are recognised by their recorded `source`, and only for the
    two administrative writers that predate it. Ordinary play and the legacy
    backfill have no operation id either, and must never be treated as
    administratively created: a correction is not proof the member did not earn
    those rewards.
    """
    if str(claim.get("operation_id") or ""):
        return True
    return str(claim.get("source") or "") in LEGACY_ADMIN_SOURCES


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


def is_booster_ref(ref) -> bool:
    return str(ref or "").startswith(BOOSTER_PREFIX)


def member_is_booster(member) -> bool:
    """True only when Discord reports an active server boost on this member.

    Fails closed: no member, or an object without premium_since, is False.
    """
    return member is not None and getattr(member, "premium_since", None) is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def progress_level(xp: int) -> int:
    from utils.xp_calculator import xp_progress
    return xp_progress(max(0, int(xp or 0)))[0]


async def _persisted_level(guild_id: int, user_id: int) -> int:
    """The member's CURRENT level, read from the database the sweep writes to.

    Deliberately a XPh reading rather than a stored `levels.level` reading alone:
    `level` is a derived cache, and this module has always treated `xp_progress`
    as authoritative (a correction recomputes it rather than trusting the column).
    A missing row is level 0.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cursor.fetchone()
    return progress_level(row[0] if row else 0)


def _ops_now() -> str:
    from utils.level_ops import _now
    return _now()


def _new_fence_epoch() -> str:
    from utils.level_ops import _new_fence_epoch as epoch
    return epoch()


def _fence_expiry() -> str:
    from utils.level_ops import LOCK_TTL_SECONDS, _expiry
    return _expiry(LOCK_TTL_SECONDS)


async def _lease_problem(guild_id: int, user_id: int,
                         operation_id: str,
                         require_lease: bool = False) -> str | None:
    """Why this sweep may not make an irreversible decision right now.

    Two callers, two contracts:

    * ``require_lease=True`` - the production path (`/setxp`, `/setlevel`). The
      caller took the lease in the same transaction as the XP write, so the only
      acceptable state is "this operation still owns a live lease". A row that
      vanished (deleted, or expired and cleaned up) means the worker's authority
      is gone and it must stop: re-asserting after the fact cannot make an
      irreversible deletion safe.
    * ``require_lease=False`` - a direct caller (the regression harness, or a
      single-member tool that manages its own authority). No lease is fine. A
      lease owned by a DIFFERENT operation is not: another correction owns this
      member now, and this worker's view of the claims no longer governs.
    """
    from utils import level_ops as ops
    lease = await ops.lock_held(guild_id, user_id)
    if lease is not None:
        if str(lease.get("operation_id") or "") == str(operation_id or ""):
            return None
        return (f"operation {lease.get('operation_id')} ({lease.get('holder')}) "
                f"holds this member's lease until {lease.get('expires_at')}, so "
                f"this correction no longer has authority to reverse anything")
    if require_lease:
        return ("this operation no longer holds the member's lease (it expired "
                "or was released while the sweep was running), so it must not "
                "reverse or delete anything more")
    return None


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
        # Which administrative XP operation caused this entitlement, if any.
        # A rollback can then cancel exactly the entitlements an erroneous grant
        # created and leave the ones the member earned independently. NULL for
        # ordinary play and for every row created before this column existed -
        # those cannot be attributed, and nothing pretends otherwise.
        if "operation_id" not in columns:
            await connection.execute(
                "ALTER TABLE level_reward_claims "
                "ADD COLUMN operation_id TEXT")
        await connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_lrc_owner
            ON level_reward_claims(guild_id, user_id, status)
        """)
        # The claim path reads `level_op_locks` and writes `reward_grants`, so a
        # database created before utils/level_ops.py existed is migrated here
        # rather than failing mid-claim with "no such table".
        from utils.level_ops import ensure_tables as ensure_ops_tables
        await ensure_ops_tables(connection)
        from utils.timed_ownership import SCHEMA_STATEMENTS
        for stmt in SCHEMA_STATEMENTS:
            await connection.execute(stmt)

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


async def _definitions(db: aiosqlite.Connection, guild_id: int,
                       boosters: bool = False) -> dict:
    """One identity per (level, track, ref). Currency rows of the same
    currency at the same level sum into that identity's raw amount.

    Booster-only rows are included only when boosters=True, under a
    BOOSTER_PREFIX ref. Callers that create entitlements pass the member's
    verified boost status; the default excludes them.
    """
    identities = {}
    cursor = await db.execute("""
        SELECT level, role_id, audience FROM leveling_rewards WHERE guild_id=?
    """, (guild_id,))
    for level, role_id, audience in await cursor.fetchall():
        if not role_id:
            continue
        prefix = BOOSTER_PREFIX if audience == "boosters" else ""
        if prefix and not boosters:
            continue
        identities[(int(level), TRACK_ROLE, prefix + str(int(role_id)))] = {
            "role_id": int(role_id),
        }
    cursor = await db.execute("""
        SELECT level, currency, amount, audience FROM leveling_currency_rewards
        WHERE guild_id=?
    """, (guild_id,))
    for level, currency, amount, audience in await cursor.fetchall():
        if currency not in ("balance", "diamonds"):
            continue
        if not amount or int(amount) <= 0:
            continue
        prefix = BOOSTER_PREFIX if audience == "boosters" else ""
        if prefix and not boosters:
            continue
        key = (int(level), TRACK_CURRENCY, prefix + currency)
        slot = identities.setdefault(key, {"currency": currency, "amount": 0})
        slot["amount"] += int(amount)
    cursor = await db.execute("""
        SELECT r.level, r.item_id, r.quantity, s.name, s.type,
               s.role_id, s.duration_hours, s.xp_boost_multiplier, r.audience
        FROM leveling_shop_rewards r
        JOIN shop_items s ON s.id = r.item_id AND s.guild_id = r.guild_id
        WHERE r.guild_id = ?
    """, (guild_id,))
    for level, item_id, quantity, name, item_type, role_id, duration_hours, multiplier, audience in await cursor.fetchall():
        if item_type not in SHOP_REWARD_TYPES or not name:
            continue
        prefix = BOOSTER_PREFIX if audience == "boosters" else ""
        if prefix and not boosters:
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
        identities[(int(level), TRACK_SHOP, prefix + str(int(item_id)))] = raw
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
                           source: str, operation_id: str = None) -> None:
    fulfilled_at = now if status == "fulfilled" else None
    await db.execute("""
        INSERT OR IGNORE INTO level_reward_claims
            (guild_id, user_id, reward_level, track, reward_ref,
             payload_json, status, created_at, fulfilled_at, source,
             operation_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (guild_id, user_id, level, track, ref, payload, status, now,
          fulfilled_at, source or "", operation_id))


async def record_crossing(db: aiosqlite.Connection, guild_id: int,
                          user_id: int, old_xp: int, new_xp: int,
                          multipliers: dict | None = None, *,
                          source: str, is_booster: bool = False,
                          operation_id: str = None) -> int:
    """Insert pending rows for levels crossed by an XP increase.

    Caller owns the transaction. Eligibility is xp_progress(xp).level,
    not the stored levels.level column. Existing rows are left alone,
    so a reset and re-level cannot pay a second time.

    A level re-crossed by ORDINARY PLAY is earned again, and this is where that
    is recorded: the row keeps its identity across a correction, so a row an
    earlier administrative change created still names that change's
    `operation_id`. Leaving it there let a later correction attribute the row to
    the administrator and take back a reward the member had earned by playing
    (see `reconcile_level_drop`). Undelivered rows re-earned here lose the
    administrative attribution; delivered rows are terminal and are left alone.
    A crossing caused by an administrative change never re-attributes anything.

    Booster-only entitlements are created only when is_booster is True (the
    caller verified an active Discord boost at this crossing). There is no
    retroactive creation, and unknown status (False) creates none.
    """
    old_level = progress_level(old_xp)
    new_level = progress_level(new_xp)
    if new_level <= old_level:
        return 0
    await ensure_tables(db)
    identities = await _definitions(db, guild_id, boosters=bool(is_booster))
    administrative = (bool(str(operation_id or ""))
                      or str(source or "") in LEGACY_ADMIN_SOURCES)
    if not administrative:
        # Only rows the member can still claim: `pending`/`failed`. A `processing`
        # row is being delivered right now (its value is already committed to the
        # member) and a `fulfilled` row is terminal, so neither is re-attributed.
        # Per identity, not by level: a booster-only reward at a re-crossed level
        # was not earned by this non-boost crossing and must keep the attribution
        # of the grant that created it.
        marks = ", ".join("?" for _ in LEGACY_ADMIN_SOURCES)
        for (level, track, ref) in identities:
            if level <= old_level or level > new_level:
                continue
            await db.execute(
                f"UPDATE level_reward_claims SET operation_id=NULL, source=? "
                f"WHERE guild_id=? AND user_id=? AND reward_level=? "
                f"AND track=? AND reward_ref=? "
                f"AND status IN ('pending', 'failed') "
                f"AND ((operation_id IS NOT NULL AND operation_id != '') "
                f"     OR source IN ({marks}))",
                (source, guild_id, user_id, level, track, ref,
                 *LEGACY_ADMIN_SOURCES))
    now = _iso(_now())
    created = 0
    for (level, track, ref), raw in identities.items():
        if level <= old_level or level > new_level:
            continue
        await _insert_identity(
            db, guild_id, user_id, level, track, ref,
            _payload(track, raw, multipliers or {}),
            "pending", now, source, operation_id)
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
    legacy_marks = ", ".join("?" for _ in LEGACY_ADMIN_SOURCES)
    sql = f"""
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
          AND (substr(reward_ref, 1, 8) != 'booster:' OR ?)
          AND (
                reward_level <= ?
                OR (
                    (operation_id IS NULL OR operation_id = '')
                    AND source NOT IN ({legacy_marks})
                  )
              )
    """
    # The last predicate is the correction invariant, enforced at the one place
    # that actually pays: a reward an administrative XP change created is
    # collectable only while the member's CURRENT level reaches it. New rows
    # carry `operation_id`; rows written by the two administrative writers that
    # predate that column carry only their `source`, so `source` is honoured for
    # exactly those two names. Ordinary play and the legacy backfill have no
    # operation id and are never disabled by a correction: those rewards were
    # earned at the level they name. Nothing is rewritten here - the row is left
    # intact and becomes collectable again if the member legitimately reaches
    # that level later.
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


async def _confirm_role_added(member, role) -> bool:
    """Authoritative membership check after a role add that the cache can't confirm.

    discord.py's default (atomic) ``add_roles`` sends the request but does not
    update ``member.roles`` until the gateway echo arrives, so "not in the cache
    yet" is not evidence of a failed add. Ask Discord instead. Anything that
    cannot be confirmed (no ``fetch_member``, REST error, role still absent)
    returns False, so a genuine failure is never hidden.
    """
    fetch = getattr(getattr(member, "guild", None), "fetch_member", None)
    if fetch is None:
        return False
    try:
        fresh = await fetch(member.id)
    except Exception:
        return False
    return any(getattr(held, "id", None) == role.id
               for held in (getattr(fresh, "roles", None) or []))


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
                              level: int, role_id: int, *,
                              booster: bool = False) -> list[int]:
    """The Level-reward role ids the just-delivered (level, role_id) obsoletes.

    `level`/`role_id` are the claim being delivered; they join the fulfilled
    set so the answer is right even when this claim is being retried or when a
    later claim was already fulfilled. An empty list means "nothing to remove".

    Role progression is audience-scoped: `booster` selects which audience's
    fulfilled role claims participate. A Booster-only role therefore never
    supersedes an unrelated normal Level role, and a normal Level role never
    supersedes an unrelated Booster-only role. With no Booster-only rows
    configured the False partition is exactly the pre-audience fulfilled set,
    so legacy behaviour is unchanged.
    """
    cursor = await db.execute("""
        SELECT reward_level, reward_ref, payload_json
        FROM level_reward_claims
        WHERE guild_id=? AND user_id=? AND track=? AND status='fulfilled'
    """, (guild_id, user_id, TRACK_ROLE))
    by_level: dict[int, set[int]] = {}
    for claimed_level, ref, payload in await cursor.fetchall():
        if is_booster_ref(ref) != booster:
            continue
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


async def member_reward_role_ids(guild_id: int, user_id: int) -> set[int]:
    """Role IDs that strip_booster_roles treats as Member rewards.

    The same rule as the ``member_ids`` set there: a role from one of this
    member's own non-Booster role track claims (any status), or a role with a
    guild config row whose audience is not ``boosters``. The boost-regained
    path passes this to ``reconcile_role_progression(exclude_role_ids=...)`` so
    a restore never goes beyond what the boost loss removed. Read-only.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute("""
            SELECT reward_ref, payload_json
            FROM level_reward_claims
            WHERE guild_id=? AND user_id=? AND track=?
        """, (guild_id, user_id, TRACK_ROLE))
        claim_rows = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT role_id, audience FROM leveling_rewards WHERE guild_id=?",
            (guild_id,))
        configured = await cursor.fetchall()

    member_ids: set[int] = set()
    for ref, payload in claim_rows:
        if is_booster_ref(ref):
            continue
        try:
            member_ids.add(int(json.loads(payload)["role_id"]))
        except (TypeError, ValueError, KeyError):
            continue
    for configured_role, audience in configured:
        if audience == "boosters":
            continue
        try:
            member_ids.add(int(configured_role))
        except (TypeError, ValueError):
            continue
    return member_ids

async def enforce_role_progression(member, guild_id: int, user_id: int,
                                   level: int, role_id: int,
                                   failures: list | None = None, *,
                                   booster: bool = False,
                                   preserved: list | None = None) -> list[int]:
    """Serialize entitlement checks and removals against member slot changes.

    The lock must precede the ownership read, not just the Discord removal.
    This public entry point must not be called while holding slot_lock already.
    """
    from utils.equip_engine import slot_lock
    async with slot_lock(guild_id, user_id):
        return await _enforce_role_progression_locked(
            member, guild_id, user_id, level, role_id, failures,
            booster=booster, preserved=preserved)


async def _enforce_role_progression_locked(member, guild_id: int, user_id: int,
                                   level: int, role_id: int,
                                   failures: list | None = None, *,
                                   booster: bool = False,
                                   preserved: list | None = None) -> list[int]:
    """Drop superseded Level-reward roles; return IDs actually removed.

    Call only after every required role at the highest Level is fulfilled and
    present. Removal failures are reported separately and never change reward
    definitions, claim state, or XP. A caller that omits ``failures`` retains
    the legacy raise-on-error behavior. ``booster`` scopes the superseded set
    to one audience (see superseded_role_ids).
    """
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        stale = await superseded_role_ids(
            db, guild_id, user_id, level, role_id, booster=booster)
    if not stale:
        return []
    guild = getattr(member, "guild", None)
    held = set(getattr(member, "roles", None) or [])
    removed = []
    preserved = preserved if preserved is not None else []
    for stale_id in stale:
        role = guild.get_role(stale_id) if guild is not None else None
        if role is None or role not in held:
            continue
        # A superseded Level role may also be a role the member OWNS through the
        # Shop (the same role ID configured in both is supported). This system
        # retires only its own claim; another recorded grant keeps the role.
        from utils.timed_ownership import role_removal_blocked
        # This pass retires the SUPERSEDED claims of one audience only. A claim
        # for the same role in the other audience is a separate, still-valid
        # grant (a member who keeps boosting keeps the Booster-only role).
        kept_reason = await role_removal_blocked(
            guild_id, user_id, stale_id,
            retiring="level:booster" if booster else "level:member",
            member=member)
        if kept_reason:
            preserved.append(stale_id)
            print(f"[LEVEL] Keeping superseded role {stale_id} for {user_id} "
                  f"in {guild_id}: still {kept_reason}")
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


async def reconcile_role_progression(member, guild_id: int, user_id: int, *,
                                     boosters_only: bool = False,
                                     exclude_role_ids=frozenset()) -> dict:
    """Restore earned Level roles and optionally enforce exclusivity.

    This is the shared membership-only reconciliation used by both rejoin and
    the member's Claim All path. It never writes the claim ledger or pays a
    reward. With ``remove_old_reward_role`` OFF, every fulfilled role claim is
    restored because Level roles accumulate. With it ON, only the highest
    fulfilled Level set is restored and lower fulfilled roles are removed after
    every role in that set is present. A failed/pending/processing same-Level
    claim or Discord add failure blocks replacement; unrelated roles are never
    considered.

    ``boosters_only`` (the boost-regained path in cogs/leveling.py) restores
    Booster-only Level roles and nothing else: Member-audience rows are
    skipped and no role is ever removed, with the setting ON or OFF. It still
    reads only fulfilled claims, so it cannot create an entitlement.

    ``exclude_role_ids`` only narrows the restore: a listed role is never
    added back. It cannot add a role, create a claim or change one. The
    boost-regained path passes member_reward_role_ids() here.

    Returns ``{"restored": [...], "removed": [...], "failed": [...],
    "blocked": bool}``. Failures are role IDs with an explanatory string.
    """
    await ensure_tables()
    from utils import level_ops as ops
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute("""
            SELECT id, reward_level, reward_ref, payload_json, status
            FROM level_reward_claims
            WHERE guild_id=? AND user_id=? AND track=?
        """, (guild_id, user_id, TRACK_ROLE))
        claim_rows = await cursor.fetchall()
        # A role whose reversal the journal already records as applied is gone by
        # proof. If a crash left the claim row behind, restoring the role here
        # would undo a completed reversal; the retry that finishes the operation
        # deletes the row. Open cases are kept in `open_role_claims` below so the
        # restore loop can withhold a role that is still under an unresolved
        # reversal. Read-only and tolerant: a database without the journal table
        # simply has nothing to exclude.
        try:
            cursor = await db.execute(
                "SELECT claim_id, status FROM reward_reversals WHERE guild_id=? "
                "AND user_id=? AND kind=?",
                (guild_id, user_id, TRACK_ROLE))
            journal = [(int(row[0]), str(row[1] or ""))
                       for row in await cursor.fetchall() if row[0] is not None]
            reversed_role_claims = {cid for cid, st in journal
                                    if st in (ops.STATUS_APPLIED,
                                              ops.STATUS_ALREADY)}
            # An OPEN case (outstanding/refused) says the reversal was intended
            # but its effect is not proven gone. Restoring the role from that
            # same claim would contradict the open case; a closed
            # `resolved:<decision>` is an administrator's decision and is not
            # treated as open here.
            open_role_claims = {cid for cid, st in journal
                                if st not in (ops.STATUS_APPLIED,
                                              ops.STATUS_ALREADY)
                                and not st.startswith("resolved:")}
        except aiosqlite.Error:
            reversed_role_claims = set()
            open_role_claims = set()

    result: dict = {"restored": [], "removed": [], "failed": [],
                    "blocked": False, "withheld": []}
    # Audience partitions. Level-role progression runs independently inside
    # each audience so a Booster-only role can never supersede (or be
    # superseded by) an unrelated normal Level role. Booster-only rows are
    # ignored entirely while Discord does not report an active boost: they are
    # not restored, not targeted, and not removed here.
    booster_ok = member_is_booster(member)
    partitions: dict[bool, dict[int, dict]] = {False: {}, True: {}}
    for claim_id, claimed_level, ref, payload, status in claim_rows:
        if claim_id in reversed_role_claims:
            continue
        audience = is_booster_ref(ref)
        if boosters_only and not audience:
            continue
        if audience and not booster_ok:
            continue
        level = int(claimed_level)
        group = partitions[audience].setdefault(
            level, {"role_ids": set(), "unfulfilled": False,
                    "claim_ids_by_role": {}})
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
            group["claim_ids_by_role"].setdefault(role_id, set()).add(
                int(claim_id))

    if not partitions[False] and not partitions[True]:
        return result

    from utils.xp_calculator import get_leveling_config
    exclusive_roles = bool(
        (await get_leveling_config(guild_id)).get("remove_old_reward_role"))

    guild = getattr(member, "guild", None)
    if guild is None:
        result["failed"].append((None, "Member has no guild"))
        result["blocked"] = True
        return result

    attempted_ids: set[int] = set()
    for audience in (False, True):
        by_level = partitions[audience]
        if not by_level:
            continue
        # The target is the highest Level whose role the member has actually
        # been given (fulfilled). A higher Level that is still pending/failed
        # is not a target: its role cannot be restored (it was never delivered)
        # and it must not cause the member to lose the role they already earned.
        delivered_levels = [lvl for lvl, grp in by_level.items() if grp["role_ids"]]
        if not delivered_levels:
            result["blocked"] = bool(result["failed"]) or (
                exclusive_roles and any(grp["unfulfilled"] for grp in by_level.values()))
            continue
        highest = max(delivered_levels)
        group = by_level[highest]
        keep = sorted(group["role_ids"])

        # OFF means every fulfilled Level role remains part of the member's
        # accumulated progression. For ON, restore only the highest set so an
        # exclusive progression doesn't re-grant roles that were superseded.
        restore_groups = ([(highest, group)] if exclusive_roles else
                          sorted(by_level.items()))
        held = set(getattr(member, "roles", None) or [])
        for reward_level, role_group in restore_groups:
            for role_id in sorted(role_group["role_ids"]):
                if role_id in exclude_role_ids:
                    continue
                role = guild.get_role(role_id)
                if role is None:
                    result["failed"].append((role_id, "Role no longer exists"))
                    continue
                if role in held:
                    continue
                # A role whose claim has an OPEN reversal case must not be
                # restored from that claim: the rollback/correction intended to
                # take it away and its effect is not proven gone. Suppress it
                # only when EVERY fulfilled claim for this role is in that open
                # set and no separate recorded grant (another claim, a temp_roles
                # unit, an inventory role marker) still justifies it. That keeps
                # an independent legitimate entitlement intact.
                claimants = (role_group.get("claim_ids_by_role", {})
                             .get(role_id) or set())
                open_claimants = claimants & open_role_claims
                if claimants and open_claimants == claimants:
                    try:
                        async with aiosqlite.connect(DB_PATH, timeout=10) as grant_db:
                            shared = await _role_granted_elsewhere(
                                grant_db, guild_id, user_id, role_id,
                                exclude_claim_id=min(open_claimants),
                                exclude_claim_ids=open_claimants)
                    except Exception as exc:
                        result["failed"].append(
                            (role_id, f"Entitlement check failed: {exc}"))
                        continue
                    if shared is None:
                        result["withheld"].append({
                            "role_id": role_id,
                            "claim_ids": sorted(open_claimants),
                            "reason": ("an open role-reversal case records that "
                                       "this role's effect was not reversed; it "
                                       "is not restored until the case is "
                                       "resolved")})
                        continue
                # One add attempt per role per pass: a role shared by the Member
                # and Booster partitions, or configured at several Levels, would
                # otherwise be re-added (the cache is stale after the first add).
                if role_id in attempted_ids:
                    continue
                attempted_ids.add(role_id)
                try:
                    await deliver_role(
                        member, role_id,
                        f"Level {reward_level} reward (restored)")
                    result["restored"].append(role_id)
                    held = set(getattr(member, "roles", None) or [])
                    if role not in held and not await _confirm_role_added(member, role):
                        result["failed"].append(
                            (role_id, "Role add returned without membership"))
                except Exception as exc:
                    result["failed"].append((role_id, str(exc)))

        if not exclusive_roles or boosters_only:
            result["blocked"] = result["blocked"] or bool(result["failed"])
            continue

        # Re-evaluate the member after all add attempts. Partial success must
        # never strip a lower role that is still the member's only reliable
        # reward.
        held = set(getattr(member, "roles", None) or [])
        if (result["failed"] or group["unfulfilled"] or any(
                guild.get_role(role_id) is None or guild.get_role(role_id) not in held
                for role_id in keep)):
            result["blocked"] = True
            continue

        result["removed"] += await enforce_role_progression(
            member, guild_id, user_id, highest, keep[0],
            failures=result["failed"], booster=audience)
        result["blocked"] = result["blocked"] or bool(result["failed"])
    return result


async def restore_reached_level_roles(member, guild_id: int, user_id: int) -> dict:
    """Put back the Level roles the member is still entitled to wear.

    Used only by `reconcile_level_drop` when it finds that the member
    legitimately re-reached a reward level while the sweep was running and it
    therefore KEPT that claim. The claim surviving is the entitlement; the role
    is its visible effect, and a sweep that already removed it must not leave the
    member wearing less than the ledger says.

    This is `reconcile_role_progression` verbatim - it reads FULFILLED role
    claims only, pays nothing, writes nothing, and cannot invent an entitlement.
    It lives behind this name so the "Claim All reconciles only on a member-
    initiated full pass" invariant stays scoped to the claim pass it describes
    instead of matching a correction's own repair step.
    """
    return await reconcile_role_progression(member, guild_id, user_id)


async def strip_booster_roles(member, guild_id: int, user_id: int) -> dict:
    """Remove Booster-only Level roles from a member who no longer boosts.

    Call only on a *confirmed* loss of the member's last active boost (see
    ``cogs/leveling.py::on_member_update``). Only roles that can be identified
    reliably as Booster-only Level reward roles are touched:

    * a candidate must be proven by this member's own claim ledger - a role
      track claim whose ``reward_ref`` carries ``BOOSTER_PREFIX``, which only
      exists for a reward earned while boosting. A Booster-only role somebody
      was given outside the Level system is therefore left alone;
    * a role that is also a Member reward (an ``everyone`` role claim in this
      member's ledger, or an ``audience='everyone'`` row in the guild's config)
      is kept, because it is still legitimately earned;
    * normal Level roles, unrelated roles, Prestige roles, Shop temp roles
      (a different track with its own expiry), the boost cog's own roles and
      reaction roles are never candidates.

    Fails closed: without a member object exposing ``premium_since`` the boost
    status cannot be verified, and a verification failure is never treated as a
    confirmed boost loss, so nothing is removed. A member who is still boosting
    (one or more active boosts) keeps every Booster-only role.

    The claim ledger is never written: fulfilled Booster-only claims stay
    terminal, so boosting again restores them through
    ``reconcile_role_progression`` while levels passed without a boost never
    create an entitlement and are therefore never granted retroactively.

    Returns ``{"removed": [...], "failed": [...], "not_held": [...],
    "preserved": [...], "unverified": bool, "still_boosting": bool}``; failures
    are ``(role_id, reason)`` pairs and never raise into the Discord event.
    ``preserved`` holds ``(role_id, reason)`` pairs for role ids left on the
    member because the member still owns them independently (a Shop holding, an
    unexpired ``temp_roles`` grant, or an owned role item): losing a boost retires
    the Booster-only claim, not somebody's purchase.
    """
    result: dict = {"removed": [], "failed": [], "not_held": [],
                    "preserved": [], "unverified": False,
                    "still_boosting": False}
    if member is None or not hasattr(member, "premium_since"):
        result["unverified"] = True
        return result
    if member_is_booster(member):
        result["still_boosting"] = True
        return result

    await ensure_tables()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute("""
            SELECT reward_ref, payload_json
            FROM level_reward_claims
            WHERE guild_id=? AND user_id=? AND track=?
        """, (guild_id, user_id, TRACK_ROLE))
        claim_rows = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT role_id, audience FROM leveling_rewards WHERE guild_id=?",
            (guild_id,))
        configured = await cursor.fetchall()

    booster_ids: set[int] = set()
    member_ids: set[int] = set()
    for ref, payload in claim_rows:
        try:
            role_id = int(json.loads(payload)["role_id"])
        except (TypeError, ValueError, KeyError):
            continue
        (booster_ids if is_booster_ref(ref) else member_ids).add(role_id)
    for configured_role, audience in configured:
        if audience == "boosters":
            # A config row alone is not proof that this member earned the role
            # through the Level system, so it never makes a role a candidate.
            continue
        try:
            role_id = int(configured_role)
        except (TypeError, ValueError):
            continue
        member_ids.add(role_id)

    guild = getattr(member, "guild", None)
    held = {getattr(role, "id", None)
            for role in (getattr(member, "roles", None) or [])}
    for role_id in sorted(booster_ids - member_ids):
        if role_id not in held:
            result["not_held"].append(role_id)
            continue
        role = guild.get_role(role_id) if guild is not None else None
        if role is None:
            result["failed"].append((role_id, "Role is not resolvable in the guild"))
            continue
        # The member may own this role through the Shop as well; losing a boost
        # retires the Booster-only claim, not somebody's purchase.
        from utils.timed_ownership import role_removal_blocked
        kept_reason = await role_removal_blocked(
            guild_id, user_id, role_id,
            retiring="booster", member=member)
        if kept_reason:
            result["preserved"].append((role_id, kept_reason))
            continue
        try:
            await member.remove_roles(role, reason="Server boost ended")
            result["removed"].append(role_id)
        except Exception as exc:  # deleted role, hierarchy, permissions, rate limit
            result["failed"].append((role_id, str(exc)))
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


async def _record_delivery_provenance(db, guild_id: int, user_id: int,
                                      row: dict) -> None:
    """Write the grant record for a reward this transaction just delivered.

    Runs inside the claim transaction, so a grant row exists if and only if the
    delivery committed - never one without the other. This is what makes a later
    correction exact instead of inferential:

    * currency -> the `transaction_ledger` row id the credit just produced, so a
      reversal targets that specific row rather than matching on amount;
    * inventory -> the item and the exact quantity granted, with a
      `remaining_quantity` that consumption decrements FIFO, so "are this
      reward's units still here" is a recorded fact rather than a guess from a
      stack total that also holds quest, shop and trade units. This includes the
      inventory marker row a `temp_role` reward writes.

    Roles and XP boosts are not recorded: a role's effect lives on the Discord
    member and in `temp_roles`, and a boost row is deleted outright by a reset.
    Neither leaves a fungible balance behind.

    Bookkeeping must never fail a delivery the member earned, so an error is
    logged and the claim still commits. The consequence is limited and safe: that
    reward has no provenance, and a later reset refuses to reverse it
    automatically instead of guessing which units are whose.
    """
    try:
        from utils.level_ops import KIND_CURRENCY, KIND_INVENTORY, record_grant
        track, payload = row["track"], row["payload"]
        if track == TRACK_CURRENCY:
            cursor = await db.execute(
                "SELECT id FROM transaction_ledger WHERE guild_id=? AND user_id=? "
                "AND currency=? AND amount=? AND type='credit' "
                "AND source='level_claim' AND reason=? ORDER BY id DESC LIMIT 1",
                (guild_id, user_id, payload["currency"], int(payload["amount"]),
                 f"Level {row['reward_level']} reward"))
            ledger_row = await cursor.fetchone()
            await record_grant(
                db, guild_id=guild_id, user_id=user_id, claim_id=row["id"],
                reward_level=row["reward_level"], track=track,
                kind=KIND_CURRENCY, quantity=int(payload["amount"]),
                currency=payload["currency"],
                ledger_id=ledger_row[0] if ledger_row else None)
        elif track == TRACK_SHOP:
            await record_grant(
                db, guild_id=guild_id, user_id=user_id, claim_id=row["id"],
                reward_level=row["reward_level"], track=track,
                kind=KIND_INVENTORY, quantity=int(payload["quantity"]),
                item_name=str(payload["name"]))
    except Exception as exc:                     # pragma: no cover - safety net
        print(f"[PROVENANCE] claim {row.get('id')} grant record failed: {exc}")


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
        # Booster-only rewards held back because the member is not verified
        # as boosting right now. Not a failure: they stay claimable later.
        "locked": 0,
        "booster_unverified": member is None,
        # Explicit reconciliation of already-fulfilled Level roles (see
        # reconcile_role_progression). Empty unless the toggle is ON and this
        # was a member-initiated full pass.
        "reconciled": {"restored": [], "removed": [], "failed": [],
                        "blocked": False},
    }
    roles = []
    temp_roles = []
    # Booster-only rows are reserved only while Discord reports an active
    # boost on the passed member. Checked here, outside the DB lock.
    booster_ok = member_is_booster(member)
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Eligibility is the member's CURRENT level, read in the same write
            # transaction as the reservation: a correction that lowers XP and
            # takes the member's lease cannot race this, and the answer cannot go
            # stale before the rows are reserved. This is what makes the
            # corrected level authoritative even if the reconciliation pass never
            # ran (crash, retry, dashboard-only writer).
            cursor = await db.execute(
                "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            xp_row = await cursor.fetchone()
            current_level = progress_level(xp_row[0] if xp_row else 0)
            result["current_level"] = current_level
            params = [token, started, guild_id, user_id, expired_before,
                      1 if booster_ok else 0, current_level,
                      *LEGACY_ADMIN_SOURCES]
            if claim_ids:
                params.extend(claim_ids)
            # A correction (New Game reset, XP rollback) may be holding this
            # member's lock while it waits on the Discord API. Delivering a reward
            # inside that window is how a claim row gets deleted by the correction
            # after its value was already paid, which makes that reward claimable
            # a second time. Checked here, inside this write transaction, so the
            # answer cannot go stale before the reservation is written - and it is
            # a database row rather than a Python lock because the dashboard is a
            # separate process over the same file.
            from utils.level_ops import _now as _ops_now
            blocker = None
            try:
                cursor = await db.execute(
                    "SELECT operation_id, holder FROM level_op_locks "
                    "WHERE guild_id=? AND user_id=? AND expires_at > ?",
                    (guild_id, user_id, _ops_now()))
                blocker = await cursor.fetchone()
            except aiosqlite.OperationalError as exc:
                # Only reachable on a database whose migration never ran. Claiming
                # an earned reward must still work; say loudly that coordination
                # is unavailable rather than failing silently.
                print(f"[LEVEL CLAIM] lock table unavailable, claiming without "
                      f"reset coordination: {exc}")
            if blocker:
                await db.execute("ROLLBACK")
                result["blocked_by_operation"] = blocker[0]
                result["blocked_by_holder"] = blocker[1]
                result["blocked"] = (
                    f"A {blocker[1]} operation ({blocker[0]}) is running for this "
                    f"member. Rewards are frozen until it finishes - try again "
                    f"in a moment.")
                return result
            await db.execute(_reserve_sql(claim_ids), params)
            owned = await _owned_rows(db, token)
            result["owned"] = len(owned)
            if not booster_ok:
                lock_sql = """
                    SELECT COUNT(*) FROM level_reward_claims
                    WHERE guild_id=? AND user_id=?
                      AND status IN ('pending', 'failed')
                      AND substr(reward_ref, 1, 8) = 'booster:'
                """
                lock_params = [guild_id, user_id]
                if claim_ids:
                    lock_sql += " AND id IN (%s)" % ",".join("?" for _ in claim_ids)
                    lock_params.extend(claim_ids)
                cursor = await db.execute(lock_sql, lock_params)
                result["locked"] = int((await cursor.fetchone())[0])
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
                    await _record_delivery_provenance(db, guild_id, user_id, row)
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
                    await _record_delivery_provenance(db, guild_id, user_id, row)
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


# ─── Correcting a level: reconcile what the higher level granted ─────────────

def _claim_brief(claim: dict) -> dict:
    """Report-safe view of one claim row: identity, not its ORM payload."""
    payload = claim.get("payload") or {}
    effect = {"currency": payload.get("currency"),
              "amount": payload.get("amount"),
              "item_name": payload.get("name"),
              "quantity": payload.get("quantity"),
              "role_id": payload.get("role_id"),
              "multiplier": payload.get("multiplier"),
              "duration_hours": payload.get("duration_hours")}
    return {"claim_id": claim["id"], "reward_level": claim["reward_level"],
            "track": claim["track"], "reward_ref": claim["reward_ref"],
            "status": claim["status"],
            "origin_operation": claim.get("operation_id"),
            "source": claim.get("source"),
            "legacy_admin": bool(claim.get("legacy_admin")),
            "effect": {k: v for k, v in effect.items() if v is not None}}


async def _role_granted_elsewhere(db, guild_id: int, user_id: int, role_id: int,
                                  *, exclude_claim_id: int,
                                  exclude_claim_ids=frozenset(),
                                  exclude_item: str | None = None) -> str | None:
    """Another recorded entitlement or grant that still justifies this role.

    A role can be the reward of several claims (the same role id configured at
    two levels), of a temp-role reward, or of an item the member owns from
    another source. Taking it off because ONE claim is being reversed would
    revoke something that is still valid, so any other grant keeps it on. This is
    deliberately conservative: an unrecorded manual role grant cannot be
    distinguished from the reward, and is reported as a residual risk rather
    than guessed at.

    A grant that a claim being reversed by this same operation wrote is not
    "another" source: its inventory marker is the effect being undone. Those
    units are recognised through `reward_grants` (which claim still owns them)
    and the marker is not offered as a reason to keep the role.
    """
    # One claim is excluded by the original caller; a caller that is deciding
    # whether a role is still justified must exclude ALL claims whose reversal
    # is unresolved, otherwise an open case would look like an independent
    # entitlement to itself.
    from utils.timed_ownership import (
        EntitlementCheckError,
        is_expired as _th_is_expired,
    )
    excluded = {int(exclude_claim_id)} | {int(c) for c in
                                          (exclude_claim_ids or ())}
    try:
        cursor = await db.execute(
            "SELECT id, track, payload_json FROM level_reward_claims "
            "WHERE guild_id=? AND user_id=? AND status='fulfilled' AND id != ?",
            (guild_id, user_id, int(exclude_claim_id)))
        claim_rows = await cursor.fetchall()
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify level_reward_claims entitlement: {exc}"
        ) from exc
    for claim_id, track, payload in claim_rows:
        if int(claim_id) in excluded:
            continue
        try:
            if int((json.loads(payload) or {}).get("role_id")) == int(role_id):
                return f"fulfilled {track} claim {claim_id}"
        except (TypeError, ValueError):
            continue

    try:
        try:
            cursor = await db.execute(
                "SELECT claim_id, source FROM temp_roles "
                "WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, int(role_id)))
            tr_rows = await cursor.fetchall()
        except aiosqlite.OperationalError as col_exc:
            if "no such column: claim_id" not in str(col_exc).lower():
                raise
            cursor = await db.execute(
                "SELECT NULL, source FROM temp_roles "
                "WHERE guild_id=? AND user_id=? AND role_id=?",
                (guild_id, user_id, int(role_id)))
            tr_rows = await cursor.fetchall()
        for claim_id, source in tr_rows:
            # A NULL claim_id is an older/manual temp-role unit: it belongs to
            # no claim, so it is always an independent grant.
            if claim_id is None or int(claim_id) not in excluded:
                return f"temp_roles grant ({source}, claim {claim_id})"
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify temp_roles entitlement: {exc}"
        ) from exc

    try:
        cursor = await db.execute(
            "SELECT item_name, source, metadata, quantity FROM inventory_items "
            "WHERE guild_id=? AND user_id=? AND item_type IN ('role','temp_role')",
            (guild_id, user_id))
        inv_rows = await cursor.fetchall()
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify inventory_items entitlement: {exc}"
        ) from exc
    for item_name, source, metadata, quantity in inv_rows:
        try:
            if int((json.loads(metadata) or {}).get("role_id")) != int(role_id):
                continue
        except (TypeError, ValueError):
            continue
        held = int(quantity or 0)
        if held <= 0:
            # An empty stack grants nothing, whatever wrote the row.
            continue
        # Attribution, never the item name and never the stack count alone. A
        # marker can be the effect of a claim this operation is itself
        # reversing; then it is not an independent grant and may not protect
        # the role from the very correction undoing it. `reward_grants` says
        # which claim each unit still belongs to: units of an excluded claim
        # are attributable to this operation, units of another claim or with no
        # claim are independent, and stack units no grant accounts for are
        # independent too (they came from somewhere this correction cannot
        # name). `exclude_item` stays as the last resort for a claim's own
        # marker whose provenance record is missing.
        own_item = bool(exclude_item and str(item_name) == str(exclude_item))
        try:
            cursor = await db.execute(
                "SELECT claim_id, remaining_quantity, unattributed "
                "FROM reward_grants WHERE guild_id=? AND user_id=? AND "
                "item_name=? AND kind='inventory'",
                (guild_id, user_id, str(item_name)))
            grants = await cursor.fetchall()
        except Exception as exc:
            raise EntitlementCheckError(
                f"Failed to verify reward_grants entitlement: {exc}"
            ) from exc
        attributable = independent = 0
        for grant_claim, remaining, unattributed in grants:
            if unattributed:
                continue        # invalidated provenance cannot attribute units
            units = max(0, int(remaining or 0))
            if grant_claim is not None and int(grant_claim) in excluded:
                attributable += units
            else:
                independent += units
        if grants and independent == 0 and held <= attributable:
            continue            # every unit is value this operation is undoing
        if own_item and independent == 0 and not grants:
            continue            # the claim's own marker, provenance unavailable
        return f"inventory '{item_name}' ({source})"

    # Timed Shop ownership (a Color or a timed Item) grants its Discord role from
    # `timed_holdings`, never from `inventory_items`, so none of the scans above
    # can see it. An unexpired holding is an independent entitlement and must
    # keep the role: no level correction buys that role back. Any query error
    # raises `EntitlementCheckError` so a transient lock or DB failure is never
    # mistaken for "no timed holding exists".
    try:
        cursor = await db.execute(
            "SELECT id, item_name, expires_at FROM timed_holdings "
            "WHERE guild_id=? AND user_id=? AND role_id=?",
            (guild_id, user_id, int(role_id)))
        holding_rows = await cursor.fetchall()
    except Exception as exc:
        raise EntitlementCheckError(
            f"Failed to verify timed_holdings entitlement: {exc}"
        ) from exc
    for holding_id, item_name, expires_at in holding_rows:
        if _th_is_expired(expires_at):
            continue
        return f"timed holding '{item_name}' (#{holding_id})"
    return None


def _boost_is_live(expires_at) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(str(expires_at)) > _now()
    except (TypeError, ValueError):
        return False


async def reconcile_level_drop(guild_id: int, user_id: int, level: int, *,
                               operation_id: str,
                               actor_id: int | None = None,
                               reason: str | None = None,
                               member=None,
                               require_lease: bool = False) -> dict:
    """Make reward eligibility reflect a corrected (lower) level.

    This is the production correction path behind `/setxp` and `/setlevel`; it
    is not the staging reset. The member's XP has ALREADY been written, and the
    caller holds the member's `level_op_locks` lease from the same transaction,
    so no claim pass can deliver anything while this runs.

    Two obligations, handled differently:

    * **Not delivered yet.** Rows an administrative XP change created and that
      now sit above `level` are left untouched. `claim_available()` already
      refuses to reserve them (`_reserve_sql`), so they cannot be claimed; the
      row survives and becomes claimable again if the member legitimately
      reaches that level. History is kept and no identity is destroyed.
    * **Already delivered.** Rows above `level` whose `operation_id` names an
      administrative XP change are attributed and reversed through
      `utils.level_ops`: currency against the exact ledger credit, inventory
      against the exact grant's remaining units, a role off the member only when
      no other grant justifies it, a boost's own row. The claim row is deleted
      only once its effect is proven gone, which makes the reward legitimately
      earnable again later. Rows crossed by ordinary play are not touched: a
      later correction is not proof the member did not earn them.

    Every refusal is recorded (`reward_reversals`) with its reason, so
    `/level_reconcile` can show it and an administrator can decide. Nothing is
    removed on a guess, and an unresolved case keeps its claim terminal, which
    is what stops the same reward being paid twice.

    Discord role removal happens outside SQLite and cannot be atomic with the
    claim row's deletion. It is ordered first and reported per role: a failure
    keeps the claim, an effect already gone is recognised on retry, so the
    operation is retryable and reverts nothing twice. A crash between a role
    removal and the claim deletion is completed by re-running the correction.
    """
    from utils import level_ops as ops

    await ensure_tables()
    await ops.ensure_tables()
    guild_id, user_id, level = int(guild_id), int(user_id), max(0, int(level))
    report = {
        "level": level, "operation_id": operation_id,
        "requested_level": level, "effective_level": level,
        "level_raised": None, "lease_lost": None, "fenced_deferrals": None,
        "dormant": [], "in_flight": [], "kept_not_attributable": [],
        "reversed": {"currency": [], "inventory": [], "role": [], "boost": []},
        "kept_unresolved": [], "deleted_claims": [], "role_failures": [],
        # Claims whose reward level the member is STILL at or above (the level
        # moved legitimately after the correction started). They are kept, and
        # a role among them is restored, because the member is entitled to it.
        "kept_reached": [], "roles_restored": [],
    }

    # ── the level the correction is actually allowed to judge ────────────────
    # `level` is what the administrator asked for. Between the XP write and this
    # sweep the member can legitimately move UP again: only claim passes are
    # fenced by the lease, and message/voice/mission XP used not to look at it.
    # Judging against the stale number is how a sweep removed a reward the
    # member had re-earned (audit probe U). The persisted level is therefore the
    # floor, and the difference is reported instead of being silent.
    persisted_level = await _persisted_level(guild_id, user_id)
    effective = max(int(level), int(persisted_level))
    report["effective_level"] = effective
    if effective > int(level):
        report["level_raised"] = {"requested": int(level),
                                  "persisted": int(persisted_level)}
    lease_problem = await _lease_problem(guild_id, user_id, operation_id,
                                         require_lease)
    if lease_problem:
        # Another operation owns this member now, or this one's lease expired
        # and was taken. The XP write already committed, so the correct result is
        # "nothing was reversed, say why" - not a sweep that keeps deleting
        # claims on behalf of an authority it no longer has.
        report["lease_lost"] = lease_problem
        return report

    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT id, reward_level, track, reward_ref, payload_json, status, "
            "operation_id, source FROM level_reward_claims "
            "WHERE guild_id=? AND user_id=? ORDER BY reward_level, id",
            (guild_id, user_id))
        found = await cursor.fetchall()
        # Unreversed level-claim credits, for the historical claims that predate
        # `reward_grants`. Consumed one-to-one below so two rewards of the same
        # amount can never both point at the same credit.
        cursor = await db.execute(
            "SELECT id, currency, amount, reason FROM transaction_ledger "
            "WHERE guild_id=? AND user_id=? AND source='level_claim' "
            "AND type='credit' AND reversed=0 ORDER BY id", (guild_id, user_id))
        free_credits = [{"id": r[0], "currency": r[1], "amount": int(r[2] or 0),
                         "reason": r[3]} for r in await cursor.fetchall()]

    claims = []
    for (claim_id, reward_level, track, ref, payload, status,
         claim_operation, claim_source) in found:
        try:
            parsed = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            parsed = {}
        legacy_admin = bool(
            not str(claim_operation or "")
            and str(claim_source or "") in LEGACY_ADMIN_SOURCES)
        claims.append({"id": claim_id, "reward_level": int(reward_level),
                       "track": track, "reward_ref": ref, "payload": parsed,
                       "status": status, "operation_id": claim_operation,
                       "source": claim_source, "legacy_admin": legacy_admin,
                       "admin_created": is_administrative_claim({
                           "operation_id": claim_operation,
                           "source": claim_source})})

    above = [c for c in claims if c["reward_level"] > effective]
    # Every claim this pass is itself going to reverse. Such a row is not an
    # independent entitlement to the other claims in the same correction: passing
    # only the rows already in `deleted_claims` let two claims for one role cite
    # each other, so neither removal was attempted, neither row was deleted, and
    # every retry reached the same state. The set is known before the loop runs,
    # which is what makes it usable while the loop is still deciding.
    being_reversed = {
        int(c["id"]) for c in above
        if c["status"] == "fulfilled" and str(c.get("operation_id") or "")
    }
    for claim in above:
        # Re-assert authority before EACH irreversible decision, not just once at
        # the top: the sweep spans Discord round-trips, and the lease's 90 s clock
        # can expire under a slow worker (a claim pass that then takes the lease
        # supersedes this operation outright). A worker that no longer owns the
        # lease must not keep removing roles or moving value on the member's
        # behalf - the remaining claims stay untouched and are reported, so a
        # retry can finish them.
        # Renew before the check. The correction spans Discord round-trips and
        # the lease's 90 s clock runs across all of them; without this a sweep
        # that is making steady progress still loses authority part-way through,
        # and the checks below would then stop it with work left to do purely
        # because time passed. Renewal is conditional on still being the owner,
        # so a superseded worker can never extend a lease that is no longer its.
        if require_lease:
            try:
                await ops.renew_lock(guild_id, user_id, operation_id)
            except Exception:
                pass        # a failed renewal surfaces as the check below
        problem = await _lease_problem(guild_id, user_id, operation_id,
                                       require_lease)
        if problem:
            report["lease_lost"] = problem
            break
        if claim["status"] in ("pending", "failed"):
            if claim["admin_created"]:
                # Not claimable now (see _reserve_sql) and not deleted: the row
                # is the history and the future entitlement in one. Legacy
                # administrative rows are included here exactly like rows that
                # carry an operation id - their recorded source is what proves
                # an administrator created them.
                brief = _claim_brief(claim)
                if claim["legacy_admin"]:
                    brief["legacy_admin"] = True
                    brief["note"] = (
                        "created before operation ids existed; the recorded "
                        "source is an administrative writer, so the row is "
                        "dormant rather than claimable above the corrected "
                        "level")
                report["dormant"].append(brief)
        elif claim["status"] == "processing":
            # A delivery that started before the lease may still be finishing
            # outside SQLite. It is not safe to judge its effect yet; it is
            # reported, and running the correction again after the lease routine
            # picks it up. Never silently ignored.
            report["in_flight"].append(_claim_brief(claim))
        elif claim["status"] == "fulfilled":
            if not claim["operation_id"]:
                brief = _claim_brief(claim)
                if claim["legacy_admin"]:
                    brief["legacy_admin"] = True
                    brief["note"] = (
                        "delivered by a pre-operation-id administrative change; "
                        "no exact reversal record exists, so it is kept and "
                        "reported rather than guessed at")
                report["kept_not_attributable"].append(brief)
            else:
                await _reverse_correction_claim(
                    claim, report, guild_id=guild_id, user_id=user_id,
                    level=effective, operation_id=operation_id, member=member,
                    free_credits=free_credits, reason=reason,
                    reversed_by=actor_id,
                    exclude_claim_ids=being_reversed)

    delete_ids = [int(c["claim_id"]) for c in report["deleted_claims"]]
    if delete_ids:
        level_of = {int(c["claim_id"]): int(c.get("reward_level") or 0)
                    for c in report["deleted_claims"]}

        # ── STEP 1: settle every claim the member legitimately re-reached ──────
        # Last line of defence: a re-crossed reward level must keep BOTH its
        # effect and its claim, or the member loses a reward they are entitled
        # to with no row left to restore it from. So: keep the claim, undo any
        # reversal this pass already applied to it, and close its journal row as
        # an administrative fact rather than a standing reversal.
        #
        # This commits in its OWN transaction and is deliberately NOT gated on
        # lease ownership. It only gives value back, so a worker that has lost
        # its lease can still safely repair it. Keeping the repair out of the
        # deletion transaction is the point: that transaction is rolled back
        # whenever authority was lost, which is exactly when the repair is
        # needed. Rolling the repair back with it is what previously left a
        # `fulfilled` claim behind a terminal `applied` reversal with the wallet
        # already debited - unrecoverable, because nothing restores the value
        # and nothing re-pays the claim.
        reached, keep_level = await _claims_reached_again(
            guild_id, user_id, delete_ids, level_of, effective, report)
        if reached:
            report["restored_for_reached"] = await _settle_reached_reversals(
                guild_id, user_id, reached, operation_id=operation_id)
            delete_ids = [cid for cid in delete_ids if cid not in reached]
            report["deleted_claims"] = [
                c for c in report["deleted_claims"]
                if int(c["claim_id"]) not in reached]

        # ── STEP 2: delete the rest, gated on live authority ─────────────
        # Only claims still genuinely above the corrected level are deleted, and
        # only by a worker that can prove it still owns the lease.
        if delete_ids:
            async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Re-assert ownership: a worker whose lease was superseded
                    # while it waited for the write lock must not delete
                    # anything.
                    cursor = await db.execute(
                        "SELECT operation_id, fence_epoch FROM level_op_locks "
                        "WHERE guild_id=? AND user_id=? AND expires_at > ?",
                        (guild_id, user_id, _ops_now()))
                    live = await cursor.fetchone()
                    ours = bool(live) and str(live[0]) == str(operation_id)
                    if (live and not ours) or (require_lease and not ours):
                        await db.execute("ROLLBACK")
                        if live and not ours:
                            report["lease_lost"] = (
                                f"operation {live[0]} took this member's lease while "
                                f"the correction was running; no claim was deleted")
                        else:
                            report["lease_lost"] = (
                                "this operation lost the member's lease before its "
                                "claims could be deleted; no claim was deleted")
                        delete_ids = []
                    if delete_ids:
                        # Only now, with the delete set settled and authority
                        # re-proved, does the fence move. Everything that raced the
                        # sweep has committed (the write lock is ours) and is counted
                        # in `deferred`; anything after this defers.
                        await db.execute(
                            "UPDATE level_op_locks SET fence_epoch=?, expires_at=? "
                            "WHERE guild_id=? AND user_id=? AND operation_id=?",
                            (_new_fence_epoch(), _fence_expiry(), guild_id,
                             user_id, operation_id))
                    if delete_ids:
                        # A boost's remaining duration is the effect itself; the row
                        # is deleted here, in the same commit that removes the claim.
                        await db.execute(
                            f"DELETE FROM leveling_active_boosts WHERE guild_id=? "
                            f"AND user_id=? AND source IN "
                            f"({','.join('?' for _ in delete_ids)})",
                            [guild_id, user_id]
                            + [f"level_claim:{cid}" for cid in delete_ids])
                        try:
                            await db.execute(
                                "DELETE FROM temp_roles WHERE guild_id=? "
                                "AND user_id=? AND source='level_claim' "
                                "AND claim_id IN "
                                f"({','.join('?' for _ in delete_ids)})",
                                [guild_id, user_id] + delete_ids)
                        except aiosqlite.Error:
                            # `claim_id` is an added column. Fall back to the role
                            # ids this correction is removing - never to `source`
                            # alone, which would take units belonging to other
                            # claims.
                            role_ids = sorted({
                                int(c["role_id"])
                                for c in report["deleted_claims"]
                                if c.get("role_id") is not None})
                            if role_ids:
                                await db.execute(
                                    "DELETE FROM temp_roles WHERE guild_id=? "
                                    "AND user_id=? AND source='level_claim' "
                                    "AND role_id IN "
                                    f"({','.join('?' for _ in role_ids)})",
                                    [guild_id, user_id] + role_ids)
                        cursor = await db.execute(
                            "DELETE FROM level_reward_claims WHERE guild_id=? "
                            "AND user_id=? AND status='fulfilled' AND id IN "
                            f"({','.join('?' for _ in delete_ids)})",
                            [guild_id, user_id] + delete_ids)
                        if cursor.rowcount != len(delete_ids):
                            # The lease should make this impossible; if it happens,
                            # say so instead of reporting a clean result. The
                            # reversal journal still names every claim, so the case
                            # is traceable.
                            report["delete_mismatch"] = {
                                "expected": len(delete_ids),
                                "deleted": int(cursor.rowcount or 0)}
                    await db.commit()
                except Exception:
                    await db.execute("ROLLBACK")
                    raise

    # A claim kept because the member re-reached its level ORDERS that the role
    # be worn. The sweep may already have taken it off before that became
    # visible, so put the ledger's own answer back on the member. This pays
    # nothing and writes nothing: `reconcile_role_progression` restores only
    # FULFILLED role claims and refuses to invent an entitlement.
    if report["kept_reached"] and member is not None:
        try:
            restored = await restore_reached_level_roles(
                member, guild_id, user_id)
            report["roles_restored"] = list(restored.get("restored") or [])
        except Exception as exc:
            report["restore_error"] = f"{type(exc).__name__}: {exc}"
    return report


async def _claims_reached_again(guild_id: int, user_id: int,
                                delete_ids: list, level_of: dict,
                                effective: int, report: dict):
    """Which of these claims the member is legitimately at or above again.

    Read in one transaction: the persisted level is the floor, so a level that
    moved up while the sweep was reversing is honoured rather than corrected
    away. Reports the difference instead of being silent about it.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            row = await cursor.fetchone()
            keep_level = max(int(effective), progress_level(row[0] if row else 0))
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    if keep_level > int(effective):
        report["level_raised"] = {
            "requested": int(effective), "persisted": int(keep_level),
            "detected": "inside the settle transaction"}
    reached = [cid for cid in delete_ids if level_of.get(cid, 0) <= keep_level]
    for cid in reached:
        entry = next((c for c in report["deleted_claims"]
                      if int(c["claim_id"]) == cid), None)
        if entry is not None:
            report["kept_reached"].append(dict(
                entry,
                reason=("the member legitimately re-reached "
                        f"Level {level_of.get(cid)} while the correction was "
                        "running, so the reward is still earned and both its "
                        "effect and its claim row are kept")))
    return reached, keep_level


async def _settle_reached_reversals(guild_id: int, user_id: int,
                                    reached: list, *,
                                    operation_id: str) -> list[dict]:
    """Give back what this pass already took from claims it is now keeping.

    A claim the member has legitimately re-reached is KEPT, so any reversal this
    sweep already applied to it was a mistake it must undo - otherwise the member
    keeps the claim row (and can never be paid again, because `fulfilled` is
    terminal) while the wallet was debited and the reversal journal says
    `applied`. That combination is an unrecoverable reward: nothing restores the
    value and nothing re-pays it.

    Runs in its own transaction and commits on its own, deliberately NOT gated on
    lease ownership. It only gives value back, so a worker that has lost its
    lease can still safely repair; and keeping it out of the deletion
    transaction is the point, because that transaction is rolled back whenever
    authority was lost - which previously discarded the repair exactly when it
    was needed (a lease expiring during a Discord round-trip).

    Idempotent by journal status: only an `applied`/`already_reversed` row still
    has value to give back, and the row is closed to `resolved:level_reached` as
    it is settled, so a retry finds nothing to do. Never debits or credits twice.
    """
    if not reached:
        return []
    from utils import level_ops as ops
    settled: list[dict] = []
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in reached)
            cursor = await db.execute(
                "SELECT reversal_id, claim_id, kind, currency, item_name, "
                "quantity, status FROM reward_reversals WHERE guild_id=? "
                f"AND user_id=? AND claim_id IN ({placeholders}) "
                "AND status IN (?, ?) ORDER BY id",
                (guild_id, user_id, *reached, ops.STATUS_APPLIED,
                 ops.STATUS_ALREADY))
            rows_found = await cursor.fetchall()
            for (rev_id, claim_id, kind, currency, item_name, quantity,
                 status) in rows_found:
                quantity = int(quantity or 0)
                note = {"claim_id": int(claim_id), "kind": kind,
                        "status": status}
                if kind == ops.KIND_CURRENCY and currency and quantity > 0:
                    from utils.economy_safe import safe_credit
                    await safe_credit(
                        guild_id, user_id, quantity, currency,
                        reason=(f"level correction: Level reward re-earned by "
                                f"the member, reversal {rev_id} undone"),
                        source="level_correction_restore", db=db)
                    note["credited"] = {currency: quantity}
                elif kind == ops.KIND_INVENTORY and item_name and quantity > 0:
                    from utils.inventory import give_item
                    await give_item(
                        guild_id, user_id, item_name, quantity,
                        source="level_correction_restore", db=db)
                    note["returned"] = {item_name: quantity}
                # A role has no fungible value to hand back: it is restored from
                # the surviving claim by `restore_reached_level_roles` below, and
                # closing the row here is what lets that restore see the claim.
                await db.execute(
                    "UPDATE reward_reversals SET status=?, reason=reason || ? "
                    "WHERE reversal_id=? AND status IN (?, ?)",
                    ("resolved:level_reached",
                     " | the member re-reached this reward level during the "
                     "correction, so the reversal was undone and the claim and "
                     "its effect are kept", rev_id, ops.STATUS_APPLIED,
                     ops.STATUS_ALREADY))
                settled.append(note)
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return settled


async def _reverse_correction_claim(claim: dict, report: dict, *,
                                    guild_id: int, user_id: int, level: int,
                                    operation_id: str, member,
                                    free_credits: list,
                                    reason: str | None = None,
                                    reversed_by: int | None = None,
                                    require_lease: bool = False,
                                    exclude_claim_ids=frozenset()) -> None:
    """Reverse ONE delivered reward, or record precisely why it was kept.

    `exclude_claim_ids` is every claim this correction pass is itself reversing;
    a role justification may not name one of them (see `_reverse_correction_role`).
    """
    from utils import level_ops as ops

    claim_id = int(claim["id"])
    payload = claim["payload"]
    origin = claim.get("operation_id")
    why = (f"created by administrative XP change {origin} while the member was "
           f"above Level {level}")
    if reason:
        # The administrator's own words travel into every journal row this
        # reversal writes; the operation record keeps them too, but the case
        # queue is read on its own.
        why += f" ({reason})"

    async def unresolved(kind: str, reason_text: str, *, detail=None) -> None:
        await ops.note_reversal(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id, kind=kind,
            quantity=int((detail or {}).get("quantity") or 1),
            operation_id=operation_id, status=ops.STATUS_OUTSTANDING,
            reason=f"{why}; {reason_text}",
            currency=(detail or {}).get("currency"),
            item_name=(detail or {}).get("item_name"),
            role_id=(detail or {}).get("role_id"))
        report["kept_unresolved"].append(dict(
            _claim_brief(claim), kind=kind, reason=reason_text))

    if claim["track"] == TRACK_CURRENCY:
        code = "balance" if payload.get("currency") == "balance" else "diamonds"
        amount = int(payload.get("amount") or 0)
        detail = {"currency": code, "amount": amount, "quantity": amount}
        if amount <= 0:
            await unresolved(ops.KIND_CURRENCY, "the frozen payload has no "
                                                 "positive amount", detail=detail)
            return
        grant = await ops.grant_for_claim(guild_id, user_id, claim_id,
                                          ops.KIND_CURRENCY)
        ledger_id = (grant or {}).get("ledger_id")
        if ledger_id is None:
            # Historical delivery: every level-claim credit has its own ledger
            # row. Match it one-to-one, exactly as the reset's attribution does.
            want = f"Level {claim['reward_level']} reward"
            match = next((r for r in free_credits
                          if r["currency"] == code and r["amount"] == amount
                          and r["reason"] == want), None)
            if match is None:
                match = next((r for r in free_credits
                              if r["currency"] == code
                              and r["amount"] == amount), None)
            if match is not None:
                free_credits.remove(match)
                ledger_id = match["id"]
        if ledger_id is None:
            await unresolved(
                ops.KIND_CURRENCY,
                "no unreversed level_claim credit can be matched to this "
                "reward, so what was paid cannot be proven", detail=detail)
            return
        outcome = await ops.reverse_currency(
            guild_id=guild_id, user_id=user_id, ledger_id=int(ledger_id),
            amount=amount, currency=code, claim_id=claim_id,
            operation_id=operation_id, reversed_by=reversed_by,
            reason="level correction: reward above the corrected level")
        if outcome["status"] in (ops.STATUS_APPLIED, ops.STATUS_ALREADY):
            report["reversed"]["currency"].append(dict(
                detail, claim_id=claim_id, reward_level=claim["reward_level"],
                ledger_id=int(ledger_id), status=outcome["status"]))
            report["deleted_claims"].append(dict(
                _claim_brief(claim), role_id=None))
        else:
            report["kept_unresolved"].append(dict(
                _claim_brief(claim), kind=ops.KIND_CURRENCY,
                reason=outcome.get("reason") or outcome["status"]))
        return

    if claim["track"] == TRACK_SHOP:
        item_name = str(payload.get("name") or "")
        quantity = int(payload.get("quantity") or 0)
        detail = {"item_name": item_name, "quantity": quantity}
        if not item_name or quantity <= 0:
            await unresolved(ops.KIND_INVENTORY,
                             "the frozen payload has no item or quantity",
                             detail=detail)
            return
        is_temp_role = str(payload.get("type") or "") == "temp_role"
        role_id = payload.get("role_id") if is_temp_role else None
        if is_temp_role:
            role_outcome = await _reverse_correction_role(
                claim, report, guild_id=guild_id, user_id=user_id,
                role_id=role_id, level=level, operation_id=operation_id,
                member=member, exclude_item=item_name,
                require_lease=require_lease,
                exclude_claim_ids=exclude_claim_ids)
            if not role_outcome:
                return              # already reported: the role keeps it
        outcome = await ops.reverse_inventory(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id,
            item_name=item_name, quantity=quantity, operation_id=operation_id,
            reason="level correction: reward above the corrected level")
        if outcome["status"] in (ops.STATUS_APPLIED, ops.STATUS_ALREADY):
            report["reversed"]["inventory"].append(dict(
                detail, claim_id=claim_id, reward_level=claim["reward_level"],
                status=outcome["status"]))
            report["deleted_claims"].append(dict(
                _claim_brief(claim), role_id=role_id))
        else:
            report["kept_unresolved"].append(dict(
                _claim_brief(claim), kind=ops.KIND_INVENTORY,
                reason=outcome.get("reason") or outcome["status"]))
        return

    if claim["track"] == TRACK_BOOST:
        detail = {"multiplier": payload.get("multiplier"),
                  "duration_hours": payload.get("duration_hours"),
                  "quantity": 1}
        previous = await ops.existing_reversal(
            guild_id, user_id, claim_id, ops.KIND_BOOST)
        if previous and previous["status"] in (ops.STATUS_APPLIED,
                                               ops.STATUS_ALREADY):
            report["reversed"]["boost"].append(dict(
                detail, claim_id=claim_id, reward_level=claim["reward_level"],
                status=previous["status"]))
            report["deleted_claims"].append(dict(
                _claim_brief(claim), role_id=None))
            return
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            cursor = await db.execute(
                "SELECT expires_at FROM leveling_active_boosts "
                "WHERE guild_id=? AND user_id=? AND source=?",
                (guild_id, user_id, f"level_claim:{claim_id}"))
            boost_row = await cursor.fetchone()
        if not _boost_is_live(boost_row[0] if boost_row else None):
            await unresolved(
                ops.KIND_BOOST,
                "the boost the reward granted has already elapsed (or was "
                "removed), so its value cannot be taken back; the claim stays "
                "terminal so the same reward cannot be granted twice",
                detail=detail)
            return
        await ops.note_reversal(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id,
            kind=ops.KIND_BOOST, quantity=1, operation_id=operation_id,
            status=ops.STATUS_APPLIED,
            reason=f"{why}; the remaining boost duration was removed")
        report["reversed"]["boost"].append(dict(
            detail, claim_id=claim_id, reward_level=claim["reward_level"],
            status=ops.STATUS_APPLIED))
        report["deleted_claims"].append(dict(_claim_brief(claim), role_id=None))
        return

    if claim["track"] == TRACK_ROLE:
        removed = await _reverse_correction_role(
            claim, report, guild_id=guild_id, user_id=user_id,
            role_id=payload.get("role_id"), level=level,
            operation_id=operation_id, member=member,
            require_lease=require_lease,
            exclude_claim_ids=exclude_claim_ids)
        if removed:
            report["deleted_claims"].append(dict(
                _claim_brief(claim), role_id=payload.get("role_id")))
        return

    await unresolved(ops.KIND_ROLE,
                     f"unknown reward track {claim['track']!r}")


async def _reverse_correction_role(claim: dict, report: dict, *,
                                   guild_id: int, user_id: int, role_id,
                                   level: int, operation_id: str, member,
                                   exclude_item: str | None = None,
                                   require_lease: bool = False,
                                   exclude_claim_ids=frozenset()) -> bool:
    """Take a reward role off the member, or record why it stays.

    `exclude_claim_ids` names the claims this same correction pass is reversing.
    None of them is an independent entitlement to the role, so none may justify
    keeping it: otherwise two claims for one role cite each other, no removal is
    attempted, and neither row is ever deleted.

    Returns True only when the role's effect is gone (removed now, already
    absent, or never held) AND that outcome is on the journal. A Discord API
    failure is not a reversal: the claim stays terminal, the case is recorded,
    and a later run retries the removal. `True` means the caller may delete the
    claim row - never before this point.

    A journal row that says `applied`/`already_reversed` is NOT proof on its own.
    A reset that ran while this claim was still `processing` could record the
    role as removed when it was not on the member yet, and the delivery could
    add it afterwards; the row then stays terminal while the role is live. The
    recorded outcome is therefore re-checked against the member. If the role IS
    worn the row is treated as stale and the removal is attempted again - unless
    another recorded grant still justifies the role, in which case the claim may
    be deleted and that other grant keeps the role documented. With no member to
    check, a settled row is left exactly as it was.
    """
    from utils import level_ops as ops

    claim_id = int(claim["id"])
    detail = {"role_id": int(role_id) if role_id is not None else None,
              "quantity": 1}
    if role_id is None:
        await ops.note_reversal(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id,
            kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
            status=ops.STATUS_OUTSTANDING, role_id=None,
            reason="the frozen payload names no role, so its effect cannot be "
                   "identified")
        report["kept_unresolved"].append(dict(
            _claim_brief(claim), kind=ops.KIND_ROLE,
            reason="the frozen payload names no role"))
        return False
    role_id = int(role_id)

    prior = await ops.existing_reversal(guild_id, user_id, claim_id,
                                        ops.KIND_ROLE)
    settled = bool(prior and prior["status"] in (ops.STATUS_APPLIED,
                                                 ops.STATUS_ALREADY))

    # Is the role actually on the member right now? The journal is a record of
    # what an earlier run BELIEVED it did; only the member says what is true.
    role = None
    held: list = []
    if member is not None:
        guild = getattr(member, "guild", None)
        role = guild.get_role(role_id) if guild is not None else None
        # A list, not a set: discord.py Role objects are hashable, but test
        # doubles and other embedders may pass plain objects whose equality is
        # identity.
        held = list(getattr(member, "roles", None) or [])
    worn = role is not None and role in held

    if settled and (member is None or not worn):
        # The journal and the member agree: there is no live effect to undo.
        return True

    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            shared = await _role_granted_elsewhere(
                db, guild_id, user_id, role_id, exclude_claim_id=claim_id,
                # No claim this correction pass is reversing is a separate
                # entitlement - neither one it has already decided to delete nor one
                # it has not reached yet. Counting either as one lets two claims for
                # one role cite each other, so no removal is attempted and neither
                # row is ever deleted. Genuinely independent grants (a claim at or
                # below the corrected level, a temp-role unit, an inventory marker)
                # are not in this set and still keep the role.
                exclude_claim_ids=({int(c["claim_id"])
                                    for c in report.get("deleted_claims") or []}
                                   | {int(c) for c in exclude_claim_ids or ()}),
                exclude_item=exclude_item)
    except Exception as exc:
        report["role_failures"].append({"claim_id": claim_id,
                                        "role_id": role_id,
                                        "error": str(exc)})
        await ops.note_reversal(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id,
            kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
            status=ops.STATUS_OUTSTANDING, role_id=role_id,
            reason=f"could not verify whether another entitlement still grants "
                   f"the role ({exc}); the role is retained and the reversal "
                   f"remains open for retry")
        report["kept_unresolved"].append(dict(
            _claim_brief(claim), kind=ops.KIND_ROLE,
            reason=f"could not verify other role entitlements: {exc}"))
        return False
    if shared:
        if settled:
            # The journal says this claim's reversal was applied AND another
            # recorded grant still owns the role, so deleting the claim row
            # leaves the role documented rather than orphaned.
            return True
        await ops.note_reversal(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id,
            kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
            status=ops.STATUS_OUTSTANDING, role_id=role_id,
            reason=f"the role is also granted by {shared}, so removing it would "
                   f"revoke a separate entitlement")
        report["kept_unresolved"].append(dict(
            _claim_brief(claim), kind=ops.KIND_ROLE,
            reason=f"the role is also granted by {shared}"))
        return False

    if member is None:
        await ops.note_reversal(
            guild_id=guild_id, user_id=user_id, claim_id=claim_id,
            kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
            status=ops.STATUS_OUTSTANDING, role_id=role_id,
            reason="no member object was available, so the Discord role could "
                   "not be removed; the claim stays terminal and the removal can "
                   "be retried")
        report["kept_unresolved"].append(dict(
            _claim_brief(claim), kind=ops.KIND_ROLE,
            reason="no member object available for the Discord role removal"))
        return False

    if worn:
        # ── lease maintenance across the Discord round-trip ────────────────
        # The lease's 90 s clock is wall-clock time and this await is the one
        # place in the sweep that can spend it. Renewing only at the top of the
        # claim loop is not enough: the expiry can land between iterations, and
        # specifically while Discord is answering this call. Renew first so an
        # ordinary round-trip does not cost the sweep its authority.
        try:
            await ops.renew_lock(guild_id, user_id, operation_id)
        except Exception:
            pass        # a failed renewal is caught by the re-check below
        try:
            await member.remove_roles(
                role, reason=f"Level correction: Level {claim['reward_level']} "
                             f"reward is above the corrected level")
        except Exception as exc:
            report["role_failures"].append({"claim_id": claim_id,
                                            "role_id": role_id,
                                            "error": str(exc)})
            await ops.note_reversal(
                guild_id=guild_id, user_id=user_id, claim_id=claim_id,
                kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
                status=ops.STATUS_OUTSTANDING, role_id=role_id,
                reason=f"Discord refused the role removal: {exc}")
            report["kept_unresolved"].append(dict(
                _claim_brief(claim), kind=ops.KIND_ROLE,
                reason=f"Discord refused the role removal: {exc}"))
            return False
        # The role is off the member. Re-assert authority BEFORE recording that
        # as terminal, because a terminal row is forever: if the lease expired
        # while Discord was working, this worker can no longer delete the claim
        # - and a surviving `fulfilled` claim behind a terminal `applied` row is
        # unrecoverable (`reconcile_role_progression` refuses to restore it, and
        # Claim All never re-pays a fulfilled row). An OPEN case is retried and
        # resolved; a terminal one just closes the door. Record what is true.
        lost = await _lease_problem(guild_id, user_id, operation_id,
                                    require_lease)
        if lost:
            report["lease_lost"] = lost
            await ops.note_reversal(
                guild_id=guild_id, user_id=user_id, claim_id=claim_id,
                kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
                status=ops.STATUS_OUTSTANDING, role_id=role_id,
                reason="the role was removed but the lease expired before the "
                       "claim could be finalised, so the removal is recorded as "
                       "open rather than terminal: a retry completes it, and an "
                       "open case can be resolved by hand")
            report["kept_unresolved"].append(dict(
                _claim_brief(claim), kind=ops.KIND_ROLE,
                reason="the role was removed but authority was lost before the "
                       "claim could be finalised, so the reversal is open"))
            return False
    # role is None (deleted in the guild) or not held: there is no live effect.
    await ops.note_reversal(
        guild_id=guild_id, user_id=user_id, claim_id=claim_id,
        kind=ops.KIND_ROLE, quantity=1, operation_id=operation_id,
        status=ops.STATUS_APPLIED, role_id=role_id,
        reason="reward role removed from the member / already absent")
    report["reversed"]["role"].append(dict(
        detail, claim_id=claim_id, reward_level=claim["reward_level"],
        status=ops.STATUS_APPLIED))
    return True


def claim_is_dormant(claim) -> bool:
    """A row an administrative XP change created, now above the member's level.

    Reward eligibility follows the member's CORRECTED level. When an administrator
    reduces a level, the entitlements the higher level created stop being
    collectable immediately - even before (or without) the reconciliation pass,
    and even if the process that applied the change died. The row is never deleted
    here: it is history, and legitimately crossing that level again makes the very
    same row claimable again, at most once.

    Rows an administrative change did NOT create (ordinary play) are deliberately
    excluded: they were earned at the level they name, and a later correction is
    not proof that the member did not earn them. `record_crossing()` writes the
    operation id for the current administrative writers; rows written by the two
    administrative writers that predate that column are recognised by their
    recorded `source` (`is_administrative_claim`). Everything else - ordinary
    play, the legacy backfill, and rows with an unknown/empty source - keeps the
    old (claimable) behaviour and is reported as unattributable by the
    correction rather than guessed at.
    """
    if not claim.get("above_level"):
        return False
    # `admin_created` is kept as an explicit override for callers/reports that
    # already classified the row; otherwise classify from the row itself.
    return bool(claim.get("admin_created")
                or is_administrative_claim(claim))


async def list_claims(guild_id: int, user_id: int) -> list[dict]:
    await ensure_tables()
    # Same expiry rule claim_available()'s reservation uses: a 'processing' row
    # whose lease ran out (or never recorded a start) is recoverable, so report
    # it as 'pending'. Read-only: reservation/ownership logic is untouched.
    expired_before = _iso(_now() - timedelta(seconds=LEASE_SECONDS))
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute("""
            SELECT id, reward_level, track, reward_ref, payload_json,
                   status, last_error, created_at, fulfilled_at, source,
                   processing_started_at, operation_id
            FROM level_reward_claims
            WHERE guild_id=? AND user_id=?
            ORDER BY reward_level, id
        """, (guild_id, user_id))
        found = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        level_row = await cursor.fetchone()
    current_level = progress_level(level_row[0] if level_row else 0)
    rows = []
    for (claim_id, level, track, ref, payload, status, error,
         created_at, fulfilled_at, source, started_at,
         claim_operation) in found:
        stale = (status == "processing"
                 and (started_at is None or started_at < expired_before))
        rows.append({
            "id": claim_id,
            "reward_level": level,
            "track": track,
            "reward_ref": ref,
            "payload": json.loads(payload),
            "status": "pending" if stale else status,
            "last_error": error,
            # History, for the /level Stats view: when the crossing was recorded,
            # when it was delivered, and which path recorded it. Read-only —
            # nothing in the claim engine reads these back.
            "created_at": created_at,
            "fulfilled_at": fulfilled_at,
            "source": source,
            "stale_processing": stale,
            # Eligibility input, not a status change: which administrative XP
            # change (if any) created this entitlement, and whether it now sits
            # above the member's level. `claim_is_dormant()` is the single
            # predicate the card, the button and the correction all read.
            # `legacy_admin` marks the pre-operation-id rows that are still
            # administrative because of their recorded source.
            "operation_id": claim_operation,
            "admin_created": is_administrative_claim({
                "operation_id": claim_operation, "source": source}),
            "legacy_admin": bool(
                not str(claim_operation or "")
                and str(source or "") in LEGACY_ADMIN_SOURCES),
            "current_level": current_level,
            "above_level": int(level) > int(current_level),
        })
    return rows


async def current_level_definitions(guild_id: int, level: int) -> list[dict]:
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        identities = await _definitions(db, guild_id, boosters=True)
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


async def next_reward_level(guild_id: int, level: int, *,
                            boosters: bool = False) -> int | None:
    """The lowest configured Level Reward milestone above `level`, or None.

    Read-only presentation support for the /level card heading. It reuses
    `_definitions()` - the exact identity set `record_crossing()` turns into
    entitlements - so a level that could never produce a claim (deleted Shop
    product, zero amount, multiplier <= 1, quantity < 1) is not reported as a
    milestone, and the four reward tables cannot drift apart from the heading.

    `boosters` carries the member's verified boost status (`member_is_booster`,
    which fails closed), so Booster-only milestones are skipped for a member
    who is not a verified booster: the card never aims at a reward the member
    cannot claim. Nothing here creates, updates, reserves or pays a claim, and
    no eligibility rule is applied beyond that existing audience filter.
    """
    await ensure_tables()
    async with aiosqlite.connect(DB_PATH) as db:
        identities = await _definitions(db, guild_id, boosters=bool(boosters))
    ahead = [reward_level for (reward_level, _track, _ref) in identities
             if int(reward_level) > int(level)]
    return min(ahead) if ahead else None
