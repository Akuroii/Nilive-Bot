"""ROLE REPLACEMENT — dashboard-configured keep/conflict role pairs.

Why this module exists
----------------------
An administrator declares one *keep* role ``A`` and one or more *conflicting*
roles ``B``/``C``. Whenever a member ends up holding both sides, the keep role
wins and the conflicting roles come off:

  * ``A`` granted while ``B`` is worn  -> remove only ``B``,
  * ``B`` granted while ``A`` is worn  -> remove only ``B`` (the newly
    conflicting role),
  * ``A`` and ``B`` granted in the same update -> ``A`` wins, ``B`` is removed.

Rules are evaluated ONLY from the roles that were ADDED by the update. A
removal therefore can never re-enter the engine, which is what keeps the
feature from cascading. Saving a rule writes configuration only — it never
walks the guild, so it cannot retroactively strip roles from members.

Ownership
---------
This feature grants nothing; it removes a role some other system may still
justify. Before every removal it asks the same authority the rest of the bot
uses — :func:`utils.timed_ownership.role_still_entitled` — plus two sources the
feature itself can cause trouble for:

  * a live ``reaction_role_expiry`` obligation (a timed reaction role still
    owed to the member), and
  * a role another *reaction-role panel* requires (removing it would trigger
    that panel's own dependent-role cascade).

If any of them still justifies the role, the role stays and the decision is
logged. That is why ``remove_old_reward_role`` (the Level-only exclusive
progression switch) is neither reused nor broadened here.

Verification
------------
Discord may refuse a removal (permissions, hierarchy) or accept it without the
member state actually changing. Every removal is confirmed against a fresh
``Guild.fetch_member`` read; an unconfirmed removal is recorded in
``role_replacement_pending`` and retried by :func:`retry_pending` until the
role is proven gone. Nothing is assumed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import discord

from database import DB_PATH

# ── Schema ───────────────────────────────────────────────────────────────

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS role_replacement_rules (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id     INTEGER NOT NULL,
        keep_role_id INTEGER NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        UNIQUE (guild_id, keep_role_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rrr_guild
    ON role_replacement_rules (guild_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS role_replacement_conflicts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        rule_id    INTEGER NOT NULL,
        role_id    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (guild_id, rule_id, role_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rrc_rule
    ON role_replacement_conflicts (rule_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS role_replacement_pending (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        role_id    INTEGER NOT NULL,
        rule_id    INTEGER,
        attempts   INTEGER NOT NULL DEFAULT 1,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (guild_id, user_id, role_id)
    )
    """,
)


async def ensure_role_replacement_schema(db) -> None:
    """Create the additive schema. Idempotent; touches no existing table."""
    for statement in SCHEMA_STATEMENTS:
        await db.execute(statement)
    await db.commit()


class RoleReplacementError(ValueError):
    """A configuration the feature refuses to store (message is user-facing)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


# ── Small parsing helpers ────────────────────────────────────────────────

MAX_SNOWFLAKE = 2 ** 63 - 1


def normalize_role_id(value) -> int:
    """A positive Discord snowflake, or :class:`RoleReplacementError`."""
    if isinstance(value, bool):
        raise RoleReplacementError("Role IDs must be numbers.")
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit():
            raise RoleReplacementError(f"'{value}' is not a valid role ID.")
        value = int(text)
    if not isinstance(value, int):
        raise RoleReplacementError(f"'{value}' is not a valid role ID.")
    if not 0 < value <= MAX_SNOWFLAKE:
        raise RoleReplacementError(f"'{value}' is not a valid role ID.")
    return int(value)


def _normalize_role_list(values, *, label: str) -> list[int]:
    if values in (None, ""):
        return []
    if isinstance(values, (str, int)):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise RoleReplacementError(f"{label} must be a list of role IDs.")
    out: list[int] = []
    for raw in values:
        role_id = normalize_role_id(raw)
        if role_id in out:
            raise RoleReplacementError(
                f"The {label.lower()} list contains role {role_id} twice.")
        out.append(role_id)
    return out


# ── Validation ───────────────────────────────────────────────────────────

def _cycle_in(edges: dict[int, set[int]]) -> list[int] | None:
    """Return one directed cycle (as a node list) or None.

    ``edges`` maps a keep role to the roles it displaces. A cycle means two
    rules would each remove the other's keep role, so no member holding both
    could ever settle on a stable set.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in edges}
    for targets in edges.values():
        for node in targets:
            colour.setdefault(node, WHITE)
    stack: list[int] = []

    def visit(node: int) -> list[int] | None:
        colour[node] = GREY
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            state = colour.get(nxt, WHITE)
            if state == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if state == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for node in sorted(colour):
        if colour[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


async def _reaction_owned_role_ids(db, guild_id: int, role_ids,
                                   all_panels: bool = False) -> set[int]:
    """Conflicting roles that a reaction-role panel MANAGES in this guild.

    ``cogs/reactionroles.py`` writes a ``reaction_role_expiry`` row only when a
    button configured a duration, so an **untimed** reaction role leaves no
    ownership record at all - the grant exists only as the Discord role on the
    member. Such a grant can never be proven replacable, so a role a panel
    manages is refused as a conflict target at validation time rather than
    being half-honoured (and removed from a member who claimed it).

    ``all_panels=True`` widens the question from "is role X a button" to "does
    anything in this guild's reaction-role configuration reference role X",
    including ``required_role_id`` gates. Those must be refused too: removing a
    gate role triggers that panel's cascade, which would strip the member's
    dependent roles - an effect of another system caused by us.
    """
    wanted = {int(r) for r in role_ids or ()}
    if not wanted:
        return set()
    placeholders = ",".join("?" for _ in wanted)
    ordered = sorted(wanted)
    cursor = await db.execute(
        f"SELECT DISTINCT role_id FROM reaction_roles "
        f"WHERE guild_id=? AND role_id IN ({placeholders})",
        (int(guild_id), *ordered))
    found = {int(r[0]) for r in await cursor.fetchall() if r[0] is not None}
    if all_panels:
        cursor = await db.execute(
            f"SELECT DISTINCT required_role_id FROM reaction_roles "
            f"WHERE guild_id=? AND required_role_id IN ({placeholders})",
            (int(guild_id), *ordered))
        found |= {int(r[0]) for r in await cursor.fetchall() if r[0] is not None}
    return found


async def validate_rule(guild_id: int, keep_role_id, conflict_role_ids,
                        existing_rules, db,
                        *, rule_id: int | None = None) -> tuple[int, list[int]]:
    """Validate one rule against the guild's other rules.

    ``existing_rules`` is what :func:`list_rules` returns, and ``db`` an open
    connection so the reaction-role read and the configuration write share one
    snapshot. Returns ``(keep_role_id, conflict_ids)`` normalised, or raises
    :class:`RoleReplacementError` with an actionable message.

    Rejected, because they cannot be honoured predictably: an empty conflict
    list, a role on both sides of ONE rule, duplicate conflict entries, a second
    rule for the same keep role, a conflicting role managed by a reaction-role
    panel, and any directed cycle across rules.

    A CHAIN is accepted: ``A`` may displace ``B`` while ``B`` displaces ``D``
    (``A > B > D``). That is a priority ordering, not a contradiction - the
    graph stays acyclic, and a removal never re-enters the engine, so the chain
    cannot loop. Only a real directed cycle (``A > B > A``) is refused.
    """
    keep = normalize_role_id(keep_role_id)
    conflicts = _normalize_role_list(conflict_role_ids, label="Conflicting roles")

    if not conflicts:
        raise RoleReplacementError("At least one conflicting role is required.")
    if keep in conflicts:
        raise RoleReplacementError(
            f"Role {keep} cannot be both the keep role and a conflicting role.")

    others = [rule for rule in existing_rules or ()
              if rule_id is None
              or int(rule.get("id") or 0) != int(rule_id)]

    for rule in others:
        if int(rule.get("keep_role_id") or 0) == keep:
            raise RoleReplacementError(
                f"A rule for keep role {keep} already exists. "
                f"Edit that rule instead.")
    edges: dict[int, set[int]] = {}
    for rule in others:
        edges.setdefault(int(rule["keep_role_id"]), set()).update(
            int(r) for r in rule.get("conflict_role_ids") or ())
    edges.setdefault(keep, set()).update(conflicts)

    cycle = _cycle_in(edges)
    if cycle:
        path = " -> ".join(str(node) for node in cycle)
        raise RoleReplacementError(
            f"This configuration is cyclic ({path}): each rule would remove "
            f"the other's keep role. Remove one of the rules first.")

    reaction_owned = await _reaction_owned_role_ids(
        db, guild_id, conflicts, all_panels=True)
    if reaction_owned:
        listed = ", ".join(str(r) for r in sorted(reaction_owned))
        raise RoleReplacementError(
            f"Role(s) {listed} are managed by a reaction-role panel. An untimed "
            f"reaction role keeps no ownership record, so this feature cannot "
            f"prove it is safe to remove. Take those roles out of the "
            f"conflicting list.")

    return keep, conflicts


# ── Persistence ──────────────────────────────────────────────────────────

async def list_rules(guild_id: int) -> list[dict]:
    """Every rule for one guild, oldest first, with its conflicting role IDs."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, keep_role_id, enabled FROM role_replacement_rules "
            "WHERE guild_id=? ORDER BY id ASC", (int(guild_id),))
        rows = await cursor.fetchall()
        if not rows:
            return []
        rules = [{"id": int(r[0]), "keep_role_id": int(r[1]),
                  "enabled": bool(r[2]), "conflict_role_ids": []} for r in rows]
        by_id = {rule["id"]: rule for rule in rules}
        cursor = await db.execute(
            "SELECT rule_id, role_id FROM role_replacement_conflicts "
            "WHERE guild_id=? ORDER BY id ASC", (int(guild_id),))
        for rule_id, role_id in await cursor.fetchall():
            rule = by_id.get(int(rule_id))
            if rule is not None:
                rule["conflict_role_ids"].append(int(role_id))
    return rules


async def get_rule(guild_id: int, rule_id: int) -> dict | None:
    for rule in await list_rules(guild_id):
        if rule["id"] == int(rule_id):
            return rule
    return None


async def save_rule(guild_id: int, keep_role_id, conflict_role_ids, *,
                    rule_id: int | None = None, enabled: int | bool = 1) -> dict:
    """Create or replace one rule. Configuration only — never touches members.

    Runs in a single ``BEGIN IMMEDIATE`` transaction so two dashboard saves
    cannot interleave into an invalid (cyclic) pair, and so replacing the
    conflicting set is all-or-nothing.
    """
    gid = int(guild_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT id, keep_role_id, enabled FROM role_replacement_rules "
                "WHERE guild_id=?", (gid,))
            rule_rows = await cursor.fetchall()
            conflict_rows: dict[int, list[int]] = {}
            if rule_rows:
                cursor = await db.execute(
                    "SELECT rule_id, role_id FROM role_replacement_conflicts "
                    "WHERE guild_id=?", (gid,))
                for rid, role in await cursor.fetchall():
                    conflict_rows.setdefault(int(rid), []).append(int(role))
            existing = [{
                "id": int(r[0]), "keep_role_id": int(r[1]),
                "enabled": bool(r[2]),
                "conflict_role_ids": conflict_rows.get(int(r[0]), []),
            } for r in rule_rows]

            keep, conflicts = await validate_rule(
                gid, keep_role_id, conflict_role_ids, existing, db,
                rule_id=rule_id)

            now = _iso()
            if rule_id is None:
                cursor = await db.execute(
                    "INSERT INTO role_replacement_rules "
                    "(guild_id, keep_role_id, enabled, created_at, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (gid, keep, int(bool(enabled)), now, now))
                new_id = int(cursor.lastrowid)
            else:
                new_id = int(rule_id)
                cursor = await db.execute(
                    "UPDATE role_replacement_rules "
                    "SET keep_role_id=?, enabled=?, updated_at=? "
                    "WHERE id=? AND guild_id=?",
                    (keep, int(bool(enabled)), now, new_id, gid))
                if not cursor.rowcount:
                    await db.execute("ROLLBACK")
                    raise RoleReplacementError("That rule no longer exists.")
                await db.execute(
                    "DELETE FROM role_replacement_conflicts "
                    "WHERE guild_id=? AND rule_id=?", (gid, new_id))

            await db.executemany(
                "INSERT INTO role_replacement_conflicts "
                "(guild_id, rule_id, role_id, created_at) VALUES (?,?,?,?)",
                [(gid, new_id, role_id, now) for role_id in conflicts])
            await db.commit()
        except BaseException:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise
    return {"id": new_id, "keep_role_id": keep,
            "conflict_role_ids": conflicts, "enabled": bool(enabled)}


async def set_rule_enabled(guild_id: int, rule_id: int, enabled: bool) -> bool:
    """Toggle a rule. True when the row existed for this guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE role_replacement_rules SET enabled=?, updated_at=? "
            "WHERE id=? AND guild_id=?",
            (int(bool(enabled)), _iso(), int(rule_id), int(guild_id)))
        await db.commit()
        return bool(cursor.rowcount)


async def delete_rule(guild_id: int, rule_id: int) -> bool:
    """Delete one rule and its conflicts. True when it existed in this guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "DELETE FROM role_replacement_rules WHERE id=? AND guild_id=?",
                (int(rule_id), int(guild_id)))
            if not cursor.rowcount:
                await db.execute("ROLLBACK")
                return False
            await db.execute(
                "DELETE FROM role_replacement_conflicts "
                "WHERE guild_id=? AND rule_id=?", (int(guild_id), int(rule_id)))
            await db.commit()
        except BaseException:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise
    return True


async def load_enabled_rules(guild_id: int) -> dict[str, dict]:
    """Runtime view: role ID -> rule, split by the side of the pair.

    Read from the database on every call. That is deliberately not cached: a
    dashboard save must be visible to the bot immediately, with no restart and
    no cross-process invalidation protocol.
    """
    rules = await list_rules(guild_id)
    keep: dict[int, dict] = {}
    conflict: dict[int, list[dict]] = {}
    for rule in rules:
        if not rule["enabled"]:
            continue
        view = {"id": rule["id"], "keep_role_id": rule["keep_role_id"],
                "conflict_role_ids": tuple(rule["conflict_role_ids"])}
        keep[view["keep_role_id"]] = view
        for role_id in view["conflict_role_ids"]:
            conflict.setdefault(role_id, []).append(view)
    return {"keep": keep, "conflict": conflict}


# ── Ownership gate ───────────────────────────────────────────────────────

class _Absent:
    """Sentinel: Discord confirmed the member is not in the guild."""


MEMBER_ABSENT = _Absent()


async def _fetch_member(guild, user_id: int):
    """A fresh gateway read of the member: object, ``MEMBER_ABSENT``, or None.

    None means "could not verify" and is never read as absence or as success.
    """
    fetch = getattr(guild, "fetch_member", None)
    if not callable(fetch):
        return None
    try:
        return await fetch(int(user_id))
    except discord.NotFound:
        return MEMBER_ABSENT
    except Exception:
        return None


def _role_ids(roles) -> set[int]:
    out: set[int] = set()
    for role in roles or ():
        raw = getattr(role, "id", role)
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _parse_time(value):
    from utils.timed_ownership import parse_time
    return parse_time(value)


async def _reaction_expiry_hold(guild_id: int, user_id: int,
                                role_id: int) -> str | None:
    """A timed reaction role this member is still owed for ``role_id``."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT expires_at FROM reaction_role_expiry "
                "WHERE guild_id=? AND user_id=? AND role_id=? AND user_id != 0",
                (int(guild_id), int(user_id), int(role_id)))
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    now = utcnow()
    for (expires_at,) in rows:
        parsed = _parse_time(expires_at)
        if parsed is not None and parsed > now:
            return "a timed reaction role is still active for it"
    return None


async def _reaction_requirement_hold(guild_id: int, role_id: int,
                                     member) -> str | None:
    """Removing ``role_id`` would strip dependent reaction roles from this member."""
    held = _role_ids(getattr(member, "roles", ()))
    if not held:
        return None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT DISTINCT role_id FROM reaction_roles "
                "WHERE guild_id=? AND required_role_id=?",
                (int(guild_id), int(role_id)))
            dependent = {int(r[0]) for r in await cursor.fetchall()
                         if r and r[0] is not None}
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    remaining = dependent & held
    if remaining:
        return ("a reaction-role panel requires it for role(s) "
                f"{', '.join(str(r) for r in sorted(remaining))}")
    return None


class OwnershipUnavailable(RuntimeError):
    """An ownership source could not be read, so nothing may be removed.

    Deliberately NOT the same answer as "entitled" or "not entitled": the
    caller must keep the role AND keep the pending retry, because the question
    was never answered rather than answered "no".
    """

    def __init__(self, reason: str):
        RuntimeError.__init__(self, reason)
        self.reason = reason


async def role_removal_verdict(guild_id: int, user_id: int, role_id: int,
                               *, bot=None, member=None) -> str | None:
    """Why this role must stay, or None when Role Replacement may remove it.

    Returns a *confirmed* reason (another system still owns the role) or None.
    A source that cannot be read raises :class:`OwnershipUnavailable` instead of
    being flattened into either answer: "we could not check" is neither
    permission to remove nor proof that nobody else grants the role.
    """
    from utils.timed_ownership import role_still_entitled

    try:
        reason = await role_still_entitled(
            int(guild_id), int(user_id), int(role_id), bot=bot, member=member)
    except Exception as exc:  # includes EntitlementCheckError
        raise OwnershipUnavailable(f"ownership could not be verified ({exc})")
    if reason:
        return reason

    for check in (
        _reaction_expiry_hold(int(guild_id), int(user_id), int(role_id)),
        _reaction_requirement_hold(int(guild_id), int(role_id), member),
    ):
        try:
            reason = await check
        except Exception as exc:
            raise OwnershipUnavailable(f"ownership could not be verified ({exc})")
        if reason:
            return reason
    return None


async def role_removal_reason(guild_id: int, user_id: int, role_id: int,
                              *, bot=None, member=None) -> str | None:
    """Keep-reason for display and for callers that must never remove.

    Flattens :class:`OwnershipUnavailable` into a keep reason, which is the
    safe answer whenever the caller's only options are keep or remove.
    """
    try:
        return await role_removal_verdict(guild_id, user_id, role_id,
                                          bot=bot, member=member)
    except OwnershipUnavailable as exc:
        return exc.reason


# ── Pending (unconfirmed) removals ───────────────────────────────────────

async def _record_pending(guild_id: int, user_id: int, role_id: int, *,
                          rule_id: int | None, error: str) -> None:
    now = _iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO role_replacement_pending
                (guild_id, user_id, role_id, rule_id, attempts, last_error,
                 created_at, updated_at)
            VALUES (?,?,?,?,1,?,?,?)
            ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET
                attempts   = role_replacement_pending.attempts + 1,
                last_error = excluded.last_error,
                rule_id    = excluded.rule_id,
                updated_at = excluded.updated_at
            """,
            (int(guild_id), int(user_id), int(role_id), rule_id,
             str(error)[:500], now, now))
        await db.commit()


async def _clear_pending(guild_id: int, user_id: int, role_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM role_replacement_pending "
            "WHERE guild_id=? AND user_id=? AND role_id=?",
            (int(guild_id), int(user_id), int(role_id)))
        await db.commit()
        return bool(cursor.rowcount)


async def pending_removals(*, guild_id: int | None = None,
                           limit: int = 200) -> list[dict]:
    sql = ("SELECT id, guild_id, user_id, role_id, rule_id, attempts, "
           "last_error FROM role_replacement_pending")
    params: list = []
    if guild_id is not None:
        sql += " WHERE guild_id=?"
        params.append(int(guild_id))
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(int(limit))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
    return [{"id": int(r[0]), "guild_id": int(r[1]), "user_id": int(r[2]),
             "role_id": int(r[3]), "rule_id": (int(r[4]) if r[4] is not None else None),
             "attempts": int(r[5] or 0), "last_error": r[6]} for r in rows]


async def clear_pending_for_member(guild_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM role_replacement_pending WHERE guild_id=? AND user_id=?",
            (int(guild_id), int(user_id)))
        await db.commit()
        return int(cursor.rowcount or 0)


# ── Removal ──────────────────────────────────────────────────────────────

async def _decide_removal(bot, guild_id: int, user_id: int, role_id: int,
                          member, *, rule_id: int | None,
                          keep_role_id: int | None) -> dict | None:
    """Terminal outcome for this removal, or None when it should go ahead.

    Every decision is made against ``member`` exactly as Discord reported it a
    moment ago, never against the event snapshot: the snapshot is a statement
    about the past, and an equip, a level claim or another listener may have
    changed the truth since. Both halves of the rule are checked - the
    conflicting role must still be held AND its keep role must still be held -
    so a stale event cannot strip a role the member is no longer meant to lose.
    """
    held = _role_ids(getattr(member, "roles", ()))
    if int(role_id) not in held:
        # Already gone (a duplicate event, or another system removed it).
        await _clear_pending(guild_id, user_id, role_id)
        return {"status": "already_absent", "role_id": int(role_id)}

    if keep_role_id is None:
        # The rule that justified this removal cannot be resolved. Nothing
        # justifies removing a role any more, so leave the member alone.
        await _clear_pending(guild_id, user_id, role_id)
        return {"status": "keep_absent", "role_id": int(role_id),
                "reason": "the rule no longer has a keep role"}
    if int(keep_role_id) not in held:
        await _clear_pending(guild_id, user_id, role_id)
        return {"status": "keep_absent", "role_id": int(role_id),
                "reason": f"the keep role {int(keep_role_id)} is no longer held"}

    try:
        reason = await role_removal_verdict(guild_id, user_id, role_id,
                                            bot=bot, member=member)
    except OwnershipUnavailable as exc:
        # Could not tell whether somebody else still owns this role. Never
        # remove on an unanswered question, and never drop the retry either:
        # the next sweep re-asks and may legitimately complete the removal.
        await _record_pending(guild_id, user_id, role_id,
                              rule_id=rule_id, error=exc.reason)
        return {"status": "unverified", "role_id": int(role_id),
                "reason": exc.reason}
    except Exception as exc:  # defensive: never treat a crash as permission
        reason = f"ownership could not be verified ({exc})"
        await _record_pending(guild_id, user_id, role_id,
                              rule_id=rule_id, error=reason)
        return {"status": "unverified", "role_id": int(role_id),
                "reason": reason}
    if reason:
        # Some other system still justifies this role. That is a decision, not
        # a failure: removal is not permitted, so no retry marker is kept.
        await _clear_pending(guild_id, user_id, role_id)
        return {"status": "kept", "role_id": int(role_id), "reason": reason}
    return None


async def _remove_role_verified(bot, guild, user_id: int, role_id: int, *,
                                rule_id: int | None,
                                keep_role_id: int | None) -> dict:
    """Remove one conflicting role, verified against authoritative state.

    The event snapshot is deliberately NOT trusted: Discord can redeliver an
    event, and another listener may have acted on the same update already. The
    member is fetched twice - once to decide, once to confirm - and a failed
    read is reported (and retried) instead of being read as success or absence.

    The decision is taken while holding the member's ``slot_lock``, the same
    lock every Inventory/Equip mutation takes, and against a member read *after*
    the lock was acquired. Without that, an equip could grant and record the
    role between the ownership check and the removal, and this feature would
    strip a role the member had just legitimately acquired.
    """
    guild_id = int(guild.id)
    role = guild.get_role(int(role_id)) if hasattr(guild, "get_role") else None
    if role is None:
        # The configured role was deleted from the guild: there is nothing to
        # remove and no obligation could ever be satisfied. Drop any marker.
        await _clear_pending(guild_id, user_id, role_id)
        return {"status": "missing_role", "role_id": int(role_id)}

    # Guild-level geometry, not member state: safe to answer before the lock.
    from utils.permissions import check_bot_role_position
    can_remove, why = check_bot_role_position(guild, role)
    if not can_remove:
        await _record_pending(guild_id, user_id, role_id,
                              rule_id=rule_id, error=why)
        return {"status": "blocked", "role_id": int(role_id), "reason": why}

    from utils.equip_engine import slot_lock
    async with slot_lock(guild_id, user_id):
        current = await _fetch_member(guild, user_id)
        if current is MEMBER_ABSENT:
            await _clear_pending(guild_id, user_id, role_id)
            return {"status": "member_absent", "role_id": int(role_id)}
        if current is None:
            reason = "could not read the member's current roles"
            await _record_pending(guild_id, user_id, role_id,
                                  rule_id=rule_id, error=reason)
            return {"status": "unverified", "role_id": int(role_id),
                    "reason": reason}

        # Re-decide against what the member holds NOW, inside the lock, so the
        # ownership gate cannot be overtaken by an equip that already recorded
        # its entitlement.
        decided = await _decide_removal(bot, guild_id, user_id, role_id,
                                        current, rule_id=rule_id,
                                        keep_role_id=keep_role_id)
        if decided is not None:
            return decided

        try:
            await current.remove_roles(
                role, reason="Role replacement: conflicting role")
        except Exception as exc:
            await _record_pending(guild_id, user_id, role_id,
                                  rule_id=rule_id, error=str(exc))
            return {"status": "failed", "role_id": int(role_id),
                    "reason": str(exc)}

    # Confirmation happens outside the lock: nothing else should queue behind a
    # Discord round-trip just to read the result back.

    confirmed = await _fetch_member(guild, user_id)
    if confirmed is MEMBER_ABSENT:
        await _clear_pending(guild_id, user_id, role_id)
        return {"status": "removed", "role_id": int(role_id)}
    if confirmed is None:
        reason = "could not verify the role was removed"
        await _record_pending(guild_id, user_id, role_id,
                              rule_id=rule_id, error=reason)
        return {"status": "unverified", "role_id": int(role_id),
                "reason": reason}
    if int(role_id) in _role_ids(getattr(confirmed, "roles", ())):
        reason = "Discord still reports the role on the member"
        await _record_pending(guild_id, user_id, role_id,
                              rule_id=rule_id, error=reason)
        return {"status": "unconfirmed", "role_id": int(role_id),
                "reason": reason}
    await _clear_pending(guild_id, user_id, role_id)
    return {"status": "removed", "role_id": int(role_id)}


# ── Event processing ─────────────────────────────────────────────────────

async def process_member_update(bot, before, after) -> dict:
    """Evaluate one role change against this guild's rules.

    Only ADDED roles are considered, so a removal (ours or anyone else's) can
    never trigger further rule processing. Returns a summary used for logging
    and by the regression tests.
    """
    result: dict = {"evaluated": False, "removed": [], "kept": [],
                    "blocked": [], "failed": [], "unconfirmed": [],
                    "events": []}
    guild = getattr(after, "guild", None)
    if guild is None or getattr(after, "bot", False):
        return result

    before_ids = _role_ids(getattr(before, "roles", ()))
    after_ids = _role_ids(getattr(after, "roles", ()))
    added = after_ids - before_ids
    if not added:
        return result

    rules = await load_enabled_rules(int(guild.id))
    if not rules["keep"] and not rules["conflict"]:
        return result
    result["evaluated"] = True

    # Which roles this update displaces.
    #
    # A role this feature is about to remove must never act as a keep role:
    # with A > B > D, adding B while A and D are held has to remove B and leave
    # D alone, because B is being displaced by A before its own rule could
    # apply. So displacement is settled FIRST - a role arriving as somebody
    # else's conflict loses its keep-role licence in the same pass - and only
    # the keep roles that survive that are allowed to displace anything.
    #
    # Every step is a set operation over the whole update, never a walk in id
    # order, so the outcome cannot depend on which role the loop happens to
    # visit first.
    targets: dict[int, dict] = {}
    while True:
        displaced = set(targets)

        # Keep roles that are still allowed to act, held and arrived alike.
        active = {role_id for role_id in after_ids
                  if role_id in rules["keep"] and role_id not in displaced}

        # 1. A conflicting role that just arrived is displaced by a keep role
        #    the member already holds - or one arriving with it.
        newly = set()
        for role_id in added:
            if role_id in displaced:
                continue
            for rule in rules["conflict"].get(role_id, ()):
                if rule["keep_role_id"] in active:
                    newly.add(role_id)
                    targets.setdefault(role_id, rule)
                    break

        # 2. Only now may a keep role act, and never if step 1 just displaced
        #    it. This is what stops B from removing D in the A > B > D chain.
        still_active = {role_id for role_id in active
                        if role_id not in displaced and role_id not in newly}
        for role_id in added:
            rule = rules["keep"].get(role_id)
            if not rule or role_id not in still_active:
                continue
            for conflict in rule["conflict_role_ids"]:
                if conflict in after_ids and conflict not in targets:
                    targets[conflict] = rule
                    newly.add(conflict)

        if not newly:
            break

    for role_id in sorted(targets):
        rule = targets[role_id]
        outcome = await _remove_role_verified(
            bot, guild, int(after.id), role_id, rule_id=rule.get("id"),
            keep_role_id=rule.get("keep_role_id"))
        result["events"].append(outcome)
        status = outcome["status"]
        if status == "removed":
            result["removed"].append(role_id)
        elif status == "kept":
            result["kept"].append(outcome)
        elif status == "blocked":
            result["blocked"].append(outcome)
        elif status in ("failed", "unverified", "unconfirmed"):
            result["failed"].append(outcome)
    return result


def describe(result: dict) -> list[str]:
    """Human-readable log lines for a :func:`process_member_update` result."""
    lines: list[str] = []
    for event in result.get("events") or ():
        status = event.get("status")
        role_id = event.get("role_id")
        if status == "removed":
            lines.append(f"[ROLE REPLACEMENT] Removed conflicting role "
                         f"{role_id} (verified absent).")
        elif status == "kept":
            lines.append(f"[ROLE REPLACEMENT] Kept role {role_id}: still "
                         f"{event.get('reason')}.")
        elif status == "keep_absent":
            lines.append(f"[ROLE REPLACEMENT] Kept role {role_id}: "
                         f"{event.get('reason')}.")
        elif status in ("blocked", "failed", "unverified", "unconfirmed"):
            lines.append(f"[ROLE REPLACEMENT] Could not remove role {role_id} "
                         f"({status}): {event.get('reason')}. Retry recorded.")
    return lines


# ── Retry sweep ──────────────────────────────────────────────────────────

async def _retry_justification(guild_id: int, row: dict) -> tuple[str, int | None]:
    """Is the pending removal still what the CURRENT configuration asks for?

    A pending row is a reminder of an intention, not a standing order: the
    administrator may have disabled the rule, deleted it, or edited the
    conflicting role away since it was written - and the dashboard promises
    that deleting a rule leaves members' roles alone. A row that names no rule,
    or a rule that no longer exists, is treated conservatively: with nothing to
    justify it, the member keeps the role.

    Returns ``(verdict, keep_role_id)`` where verdict is one of ``apply``,
    ``not_configured`` or ``unknown`` (a read that failed: keep waiting, never
    remove on a guess).
    """
    rule_id = row.get("rule_id")
    if rule_id is None:
        return "not_configured", None
    try:
        rule = await get_rule(guild_id, int(rule_id))
    except Exception as exc:
        print(f"[ROLE REPLACEMENT] rule lookup failed for rule {rule_id} "
              f"in guild {guild_id}: {exc}")
        return "unknown", None
    if rule is None:
        return "not_configured", None
    if not rule.get("enabled"):
        return "not_configured", None
    if int(row["role_id"]) not in {int(r) for r in rule["conflict_role_ids"]}:
        return "not_configured", None
    return "apply", int(rule["keep_role_id"])


async def retry_pending(bot, *, limit: int = 200) -> dict:
    """Retry removals Discord never confirmed.

    A row is deleted only once the role is proven gone, the member left, the
    configured role no longer exists, the current configuration no longer asks
    for the removal, or another system now justifies the role.
    """
    summary = {"checked": 0, "removed": 0, "cleared": 0, "pending": 0}
    for row in await pending_removals(limit=limit):
        summary["checked"] += 1
        guild = bot.get_guild(row["guild_id"]) if hasattr(bot, "get_guild") else None
        if guild is None:
            summary["pending"] += 1
            continue
        verdict, keep_role_id = await _retry_justification(row["guild_id"], row)
        if verdict == "not_configured":
            # Disabled / deleted / edited away: the dashboard's promise that
            # retiring a rule strands nobody takes precedence.
            await _clear_pending(row["guild_id"], row["user_id"], row["role_id"])
            summary["cleared"] += 1
            continue
        if verdict == "unknown":
            summary["pending"] += 1
            continue
        try:
            outcome = await _remove_role_verified(
                bot, guild, row["user_id"], row["role_id"],
                rule_id=row["rule_id"], keep_role_id=keep_role_id)
        except Exception as exc:
            print(f"[ROLE REPLACEMENT] retry failed for role {row['role_id']} "
                  f"/ user {row['user_id']} in {row['guild_id']}: {exc}")
            summary["pending"] += 1
            continue
        status = outcome.get("status")
        if status == "removed":
            summary["removed"] += 1
        elif status in ("already_absent", "member_absent", "missing_role",
                        "kept", "keep_absent"):
            summary["cleared"] += 1
        else:
            summary["pending"] += 1
            print(f"[ROLE REPLACEMENT] still pending for role {row['role_id']} "
                  f"/ user {row['user_id']} in {row['guild_id']}: "
                  f"{outcome.get('reason')}")
    return summary
