"""Scoped reset of ONE member's /level test state - preview first, apply second.

Why this exists
---------------
Testing `/level` end to end means crossing reward levels, claiming them, and
then wanting to do it again from zero. Nothing in the bot could do that:
`/resetxp` zeroes XP but deliberately leaves `level_reward_claims` alone (it is
a moderation correction, not a test reset), and because the claim ledger is a
one-way ratchet - `record_crossing()` uses INSERT OR IGNORE on the identity
(guild, user, level, track, ref) - re-levelling after a reset can never pay a
second time. Correct for production, useless for testing.

What it will and will not do
----------------------------
EVERY statement here is bound to one (guild_id, user_id) pair. There is no
code path that can reach a second member, a second guild, or a whole table.

Reversible here, because the level-claim writer tags them precisely:
  * `levels.xp` / `levels.level`                 -> zeroed for this member
  * `level_reward_claims`                        -> this member's rows deleted
  * `leveling_active_boosts`  source LIKE
    'level_claim:%'                              -> deleted (grant_xp_boost
                                                    records the claim id)
  * `temp_roles`  source = 'level_claim'
    (+ claim_id)                                 -> deleted
  * Discord roles from role-track claims         -> removed, when a member
                                                    object is supplied

NOT reversible here, and therefore only ever REPORTED, never touched:
  * `economy.balance` / `economy.diamonds`. Currency rewards are credited with
    `safe_credit(..., source="level_claim")`, so the ledger rows are auditable
    and the total is reported - but coins are fungible and may already have been
    spent, traded or shop-purchased with. Subtracting them can drive the wallet
    negative and would corrupt unrelated economy state, so this tool refuses to.
  * `inventory_items`. Shop rewards merge into one row per
    (guild, user, item_name) with `quantity = quantity + ?`, and `source` is
    overwritten by the most recent write, so level-claim units cannot be told
    apart from mission, minigame, shop or trade units afterwards. Deleting or
    decrementing the row would destroy unrelated inventory.
  * `transaction_ledger`. Append-only audit history; rewriting it would falsify
    the record the report points at.

Never touched at all: the four reward-definition tables (`leveling_rewards`,
`leveling_currency_rewards`, `leveling_shop_rewards`, `leveling_boost_rewards`),
`leveling_config`, `leveling_reset_config`, `leveling_leaderboard_history`,
`activity_stats`, the spam-incident tables, and every shop/economy/wallet table.

The delivered-value gate
------------------------
Deleting a `fulfilled` currency or shop claim makes that reward earnable again
while the value it delivered is still in the member's wallet or inventory - i.e.
a duplicate. So by default this tool REFUSES to delete fulfilled rows whose
effect it cannot reverse, and `apply` returns `blocked=True` with the exact
amounts to reverse by hand. Pass `include_delivered=True` only after reversing
them (or on a throwaway staging database where the value does not matter); the
report then repeats what was left behind.

Prefer staging: with `NERO_ENVIRONMENT=staging` (see database.py) this points at
`STAGING_DATABASE_PATH` and no production row exists to endanger. Against a
production database `apply` additionally requires `allow_production=True`, which
the caller must derive from an explicit admin confirmation, never from a default.
"""
from __future__ import annotations

import json

import aiosqlite

from database import DB_PATH, NERO_ENVIRONMENT

CLAIM_SOURCE = "level_claim"
BOOST_SOURCE_PREFIX = "level_claim:"
# Tracks whose delivered effect this module can reverse precisely.
AUTO_REVERSIBLE_TRACKS = ("role", "boost")
# Tracks whose delivered effect is only reportable - see the module docstring.
MANUAL_TRACKS = ("currency", "shop")
# Reward definitions and everything else that must never be touched. Listed so
# the regression test can prove the apply path issues no statement against them.
# `levels` is not here: this member's own row IS updated (xp/level zeroed), but
# never deleted, and no other member's row is reachable.
NEVER_TOUCHED_TABLES = (
    "leveling_rewards", "leveling_currency_rewards", "leveling_shop_rewards",
    "leveling_boost_rewards", "leveling_config", "leveling_reset_config",
    "leveling_leaderboard_history", "activity_stats", "economy",
    "transaction_ledger", "inventory_items", "shop_items", "purchase_history",
)


class LevelResetError(RuntimeError):
    """A refused reset. Nothing was written."""


def _check_scope(guild_id, user_id) -> tuple[int, int]:
    """The one guard that makes a mass reset unrepresentable."""
    try:
        guild_id, user_id = int(guild_id), int(user_id)
    except (TypeError, ValueError):
        raise LevelResetError("guild_id and user_id must both be integers")
    if guild_id <= 0 or user_id <= 0:
        raise LevelResetError(
            f"refusing to reset a non-positive scope (guild={guild_id} "
            f"user={user_id})")
    return guild_id, user_id


async def inspect_member(guild_id: int, user_id: int) -> dict:
    """Read-only: every record and side effect this reset would touch.

    Opens no transaction and writes nothing, so the preview can be run as often
    as needed - including against production - to see exactly what is there.
    """
    guild_id, user_id = _check_scope(guild_id, user_id)
    report: dict = {
        "guild_id": guild_id,
        "user_id": user_id,
        "environment": NERO_ENVIRONMENT,
        "xp": 0,
        "level": 0,
        "has_level_row": False,
        "claims": [],
        "claims_by_status": {},
        "boosts": [],
        "temp_roles": [],
        "role_ids": [],
        "currency_credited": {},
        "ledger_rows": 0,
        "inventory_candidates": [],
        "manual_only": [],
    }
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cursor.fetchone()
        if row:
            report["has_level_row"] = True
            report["xp"], report["level"] = int(row[0] or 0), int(row[1] or 0)

        cursor = await db.execute("""
            SELECT id, reward_level, track, reward_ref, status, fulfilled_at,
                   payload_json
            FROM level_reward_claims
            WHERE guild_id=? AND user_id=?
            ORDER BY reward_level, id
        """, (guild_id, user_id))
        for (claim_id, level, track, ref, status, fulfilled_at,
             payload) in await cursor.fetchall():
            claim = {"id": claim_id, "reward_level": level, "track": track,
                     "reward_ref": ref, "status": status,
                     "fulfilled_at": fulfilled_at}
            report["claims"].append(claim)
            report["claims_by_status"][status] = (
                report["claims_by_status"].get(status, 0) + 1)
            if track == "role":
                try:
                    report["role_ids"].append(int(json.loads(payload)["role_id"]))
                except (TypeError, ValueError, KeyError):
                    pass

        # XP boosts granted by a level claim carry the claim id in `source`.
        cursor = await db.execute("""
            SELECT id, multiplier, expires_at, source
            FROM leveling_active_boosts
            WHERE guild_id=? AND user_id=? AND source LIKE ?
        """, (guild_id, user_id, BOOST_SOURCE_PREFIX + "%"))
        report["boosts"] = [
            {"id": r[0], "multiplier": r[1], "expires_at": r[2], "source": r[3]}
            for r in await cursor.fetchall()]

        # Temp-role units written by a level claim.
        cursor = await db.execute("""
            SELECT id, role_id, expires_at, claim_id
            FROM temp_roles
            WHERE guild_id=? AND user_id=? AND source=?
        """, (guild_id, user_id, CLAIM_SOURCE))
        report["temp_roles"] = [
            {"id": r[0], "role_id": r[1], "expires_at": r[2], "claim_id": r[3]}
            for r in await cursor.fetchall()]

        # Currency a level claim credited. Read from the append-only ledger, so
        # this is what was ACTUALLY paid, not what a definition says today.
        cursor = await db.execute("""
            SELECT currency, COALESCE(SUM(amount), 0), COUNT(*)
            FROM transaction_ledger
            WHERE guild_id=? AND user_id=? AND source=? AND type='credit'
            GROUP BY currency
        """, (guild_id, user_id, CLAIM_SOURCE))
        for currency, total, count in await cursor.fetchall():
            report["currency_credited"][currency] = int(total or 0)
            report["ledger_rows"] += int(count or 0)

        # Inventory rows that MIGHT hold level-claim units. Reported as
        # candidates only - see the docstring for why they cannot be attributed.
        wanted = {c["reward_ref"] for c in report["claims"] if c["track"] == "shop"}
        if wanted:
            cursor = await db.execute("""
                SELECT item_name, item_type, quantity, source
                FROM inventory_items
                WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            report["inventory_candidates"] = [
                {"item_name": r[0], "item_type": r[1], "quantity": r[2],
                 "source": r[3]}
                for r in await cursor.fetchall()]

    report["manual_only"] = [
        c for c in report["claims"]
        if c["status"] == "fulfilled" and c["track"] in MANUAL_TRACKS]
    report["plan"] = _plan(report)
    return report


def _plan(report: dict) -> dict:
    """Exactly what `apply` would change, derived from the inspected records."""
    claims = report["claims"]
    blocked = bool(report["manual_only"])
    return {
        "zero_levels_row": report["has_level_row"],
        "delete_claims": len(claims),
        "delete_boosts": len(report["boosts"]),
        "delete_temp_roles": len(report["temp_roles"]),
        "remove_discord_roles": sorted({r for r in report["role_ids"] if r}),
        "blocked_on_delivered_value": blocked,
        "blocked_claims": [
            {"id": c["id"], "reward_level": c["reward_level"], "track": c["track"]}
            for c in report["manual_only"]],
        "reported_not_reversed": {
            "currency": report["currency_credited"],
            "ledger_rows": report["ledger_rows"],
            "inventory_candidates": report["inventory_candidates"],
        },
    }


async def reset_member(guild_id: int, user_id: int, *, apply: bool = False,
                       include_delivered: bool = False,
                       allow_production: bool = False,
                       member=None) -> dict:
    """Preview (default) or perform the scoped reset. Returns the full report.

    ``apply=False`` never writes - it returns `inspect_member()` plus the plan.
    ``apply=True`` runs the reversible part in one BEGIN IMMEDIATE transaction.
    """
    guild_id, user_id = _check_scope(guild_id, user_id)
    report = await inspect_member(guild_id, user_id)
    report["applied"] = False
    report["blocked"] = False
    report["roles_removed"] = []
    report["role_failures"] = []

    if not apply:
        report["mode"] = "preview"
        return report

    if NERO_ENVIRONMENT == "production" and not allow_production:
        raise LevelResetError(
            "NERO_ENVIRONMENT=production: refusing to apply a test reset "
            "without an explicit confirmation (allow_production). Run the "
            "preview first, or use a staging database.")

    plan = report["plan"]
    if plan["blocked_on_delivered_value"] and not include_delivered:
        report["blocked"] = True
        report["mode"] = "blocked"
        return report
    report["mode"] = "apply"

    # Discord roles first, while the claim rows that name them still exist.
    # Best effort and reported per role: a missing permission must not silently
    # leave the member wearing a reward role from a run that no longer exists.
    for role_id in plan["remove_discord_roles"]:
        try:
            if member is None:
                raise LevelResetError("no member object supplied")
            role = getattr(getattr(member, "guild", None), "get_role",
                           lambda _id: None)(role_id)
            if role is None:
                raise LevelResetError(f"role {role_id} not found in the guild")
            if role not in (getattr(member, "roles", None) or []):
                report["roles_removed"].append(role_id)   # already absent
                continue
            await member.remove_roles(role, reason="level test reset")
            report["roles_removed"].append(role_id)
        except Exception as exc:
            report["role_failures"].append({"role_id": role_id, "error": str(exc)})

    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # One member's level-claim XP boosts and temp-role units.
            await db.execute("""
                DELETE FROM leveling_active_boosts
                WHERE guild_id=? AND user_id=? AND source LIKE ?
            """, (guild_id, user_id, BOOST_SOURCE_PREFIX + "%"))
            await db.execute("""
                DELETE FROM temp_roles
                WHERE guild_id=? AND user_id=? AND source=?
            """, (guild_id, user_id, CLAIM_SOURCE))
            # The entitlement ledger. This is what makes the next crossing
            # earnable again - the point of the tool.
            await db.execute("""
                DELETE FROM level_reward_claims
                WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            # Zero, never delete: the row also carries prestige and is read by
            # systems outside leveling.
            await db.execute("""
                UPDATE levels SET xp=0, level=0
                WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

    report["applied"] = True
    after = await inspect_member(guild_id, user_id)
    report["after"] = {
        "xp": after["xp"], "level": after["level"],
        "claims": len(after["claims"]), "boosts": len(after["boosts"]),
        "temp_roles": len(after["temp_roles"]),
    }
    # The value this tool deliberately left in place, repeated after the reset so
    # the admin cannot miss it.
    report["left_behind"] = {
        "currency": report["currency_credited"],
        "ledger_rows": report["ledger_rows"],
        "inventory_candidates": report["inventory_candidates"],
    }
    return report
