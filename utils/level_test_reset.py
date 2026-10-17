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
  * `level_reward_claims`                        -> this member's rows deleted,
                                                    EXCEPT a role reward whose
                                                    Discord role could not be
                                                    taken off (see below)
  * `leveling_active_boosts`  source LIKE
    'level_claim:%'                              -> deleted (grant_xp_boost
                                                    records the claim id)
  * `temp_roles`  source = 'level_claim'
    (+ claim_id)                                 -> deleted, except for a role
                                                    whose removal failed
  * Discord roles from role-track claims         -> removed, when a member
                                                    object is supplied AND the
                                                    removal is provably safe

Prestige is per-member leveling state too, so it is in scope - but it is not
free. `levels.prestige` is the PERMANENT tier and the only way to raise it is a
Shop purchase, so zeroing it destroys something the member paid for and this
tool does not refund it. `prestige_vi_activations` is the free booster-only VI
activation, which the member can simply take again. Both are therefore left
alone unless `include_prestige=True` is passed explicitly, and while either one
survives the report says so and refuses to call the result a New Game.

Role removal: ordering and safety
---------------------------------
The Discord role is taken off BEFORE any record is deleted, and a role that
could not be taken off keeps its records. The previous shape of this tool
deleted every claim row whatever happened to the role, which could leave the
member wearing a reward role with no row anywhere describing why - no review
path and no recovery path. Now a failed removal preserves that claim row AND its
`temp_roles` row, so `/level` still shows the reward, the temp-role expiry task
can retry the removal on its own schedule, and `role_failures` names the role and
the error.

A role is only removed when its removal is provably ours to make: if this member
also holds the same role through another recorded grant (`temp_roles` row with a
different source - mission, booster, shop, an admin's own grant) then taking it
off would revoke something else gave them. Such a role is left on the member, the
claim record IS deleted (the other grant, not this one, is keeping it on), and
the report lists it under `roles_left_on_member` with the competing sources.

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

Is it a New Game?
-----------------
Only if nothing from the previous run survives. `reset_member()` therefore
returns `new_game_complete` plus `traces`, a plain-language list of everything it
could not or would not undo: prestige left in place, delivered currency or
inventory value still in the wallet, a role it refused to take off, a claim row
it preserved because the removal failed. A caller must not describe the result as
a full reset while `traces` is non-empty - `/level_test_reset` prints the list.

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


async def inspect_member(guild_id: int, user_id: int, *,
                         include_prestige: bool = False) -> dict:
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
        "prestige": 0,
        "has_level_row": False,
        "vi_activation": None,
        "role_other_grants": {},
        "roles_left_on_member": [],
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
        # `prestige` was added by an ALTER TABLE migration, so an unmigrated
        # database can still be inspected: read it separately and tolerate its
        # absence rather than failing the whole preview.
        cursor = await db.execute(
            "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        row = await cursor.fetchone()
        if row:
            report["has_level_row"] = True
            report["xp"], report["level"] = int(row[0] or 0), int(row[1] or 0)
            try:
                cursor = await db.execute(
                    "SELECT prestige FROM levels WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
                prow = await cursor.fetchone()
                report["prestige"] = int(prow[0] or 0) if prow else 0
            except aiosqlite.Error:
                report["prestige_column_missing"] = True

        # The free booster-only Prestige VI activation, if this member has one.
        try:
            cursor = await db.execute("""
                SELECT activated_at FROM prestige_vi_activations
                WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            vrow = await cursor.fetchone()
            report["vi_activation"] = vrow[0] if vrow else None
        except aiosqlite.Error:
            pass

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
                     "fulfilled_at": fulfilled_at, "role_id": None}
            report["claims"].append(claim)
            report["claims_by_status"][status] = (
                report["claims_by_status"].get(status, 0) + 1)
            if track == "role":
                try:
                    claim["role_id"] = int(json.loads(payload)["role_id"])
                    report["role_ids"].append(claim["role_id"])
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

        # Every OTHER recorded grant of a role to this member. If one of them
        # names the same role a level reward gave, taking that role off would
        # revoke something else's grant, so the reset leaves it on and says so.
        cursor = await db.execute("""
            SELECT role_id, source FROM temp_roles
            WHERE guild_id=? AND user_id=? AND source IS NOT ?
        """, (guild_id, user_id, CLAIM_SOURCE))
        for role_id, source in await cursor.fetchall():
            report["role_other_grants"].setdefault(str(role_id), set()).add(
                str(source))
        report["role_other_grants"] = {
            k: sorted(v) for k, v in report["role_other_grants"].items()}

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
    report["plan"] = _plan(report, include_prestige=include_prestige)
    return report


def _plan(report: dict, *, include_prestige: bool = False) -> dict:
    """Exactly what `apply` would change, derived from the inspected records.

    Read-only. A role this member also holds through another recorded grant is
    moved out of `remove_discord_roles` and into `roles_left_on_member`, because
    taking it off would revoke someone else's grant. Claim rows preserved after a
    FAILED removal cannot be known until the removal is attempted, so `apply`
    reports those separately in `claims_preserved`.
    """
    claims = report["claims"]
    blocked = bool(report["manual_only"])
    others = report.get("role_other_grants") or {}
    role_ids = {r for r in report["role_ids"] if r}
    unsafe = sorted(r for r in role_ids if str(r) in others)
    return {
        "zero_levels_row": report["has_level_row"],
        # The flag alone decides this: clearing prestige is an explicit choice,
        # and the preview above shows the values it would destroy.
        "reset_prestige": bool(include_prestige),
        "prestige_now": report.get("prestige", 0),
        "vi_activation_now": report.get("vi_activation"),
        "delete_claims": len(claims),
        "delete_boosts": len(report["boosts"]),
        "delete_temp_roles": len(report["temp_roles"]),
        "remove_discord_roles": sorted(r for r in role_ids if str(r) not in others),
        "roles_left_on_member": [
            {"role_id": r, "also_granted_by": others[str(r)]} for r in unsafe],
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


def _reconciliation(report: dict) -> dict:
    """The manual-review listing for value this tool will not reverse itself.

    Two halves, kept strictly apart because acting on the wrong one is how
    unrelated economy data gets destroyed:

    * ``proven`` - amounts attributable to level rewards with certainty, read
      from the append-only ledger the claim engine wrote at delivery time, not
      from a reward definition that may have been edited since.
    * ``unattributable`` - what looks related but cannot be pinned to a level
      reward, with the reason. An operator reconciling by hand works from this.
    """
    delivered = [
        {"claim_id": c["id"], "reward_level": c["reward_level"],
         "track": c["track"], "reward_ref": c["reward_ref"],
         "fulfilled_at": c["fulfilled_at"]}
        for c in report["claims"]
        if c["status"] == "fulfilled" and c["track"] in MANUAL_TRACKS]
    return {
        "proven": {
            "currency_by_code": report["currency_credited"],
            "ledger_rows": report["ledger_rows"],
            "ledger_filter": ("transaction_ledger WHERE source='level_claim' "
                              "AND type='credit' AND guild_id/user_id = this member"),
            "delivered_claims": delivered,
        },
        "unattributable": [
            {"table": "inventory_items",
             "reason": "shop rewards merge into ONE row per "
                       "(guild, user, item_name) with quantity = quantity + ?, "
                       "and `source` is overwritten by the latest write, so "
                       "level-claim units cannot be separated from mission, "
                       "minigame, shop or trade units afterwards",
             "candidates": report["inventory_candidates"]},
            {"table": "economy",
             "reason": "coins and diamonds are fungible and may already have "
                       "been spent, traded or shop-purchased with; the ledger "
                       "total above is the only provable amount, and subtracting "
                       "it can drive the wallet negative"},
            {"table": "transaction_ledger",
             "reason": "append-only audit history; rewriting it would falsify "
                       "the very record this report points at"},
        ],
    }


async def reset_member(guild_id: int, user_id: int, *, apply: bool = False,
                       include_delivered: bool = False,
                       include_prestige: bool = False,
                       allow_production: bool = False,
                       member=None) -> dict:
    """Preview (default) or perform the scoped reset. Returns the full report.

    ``apply=False`` never writes - it returns `inspect_member()` plus the plan.
    ``apply=True`` removes Discord roles first, then runs the reversible part in
    one BEGIN IMMEDIATE transaction, then reports what ran and what could not.

    ``include_delivered`` also drops fulfilled currency/shop claim rows. Their
    delivered value stays in the wallet/inventory, so this is only safe after
    reversing that value by hand or on a throwaway database - see the docstring.
    ``include_prestige`` also zeroes the permanent Prestige tier and deletes the
    booster VI activation. Permanent tiers are Shop purchases and are NOT
    refunded, so this is opt-in and never implied by a reset.
    """
    guild_id, user_id = _check_scope(guild_id, user_id)
    report = await inspect_member(guild_id, user_id,
                                  include_prestige=include_prestige)
    report["applied"] = False
    report["blocked"] = False
    report["roles_removed"] = []
    report["role_failures"] = []
    report["claims_preserved"] = []
    report["reconciliation"] = _reconciliation(report)

    if not apply:
        report["mode"] = "preview"
        report["traces"] = _traces(report, applied=False,
                                   include_prestige=include_prestige)
        report["new_game_complete"] = False
        return report

    if NERO_ENVIRONMENT == "production" and not allow_production:
        raise LevelResetError(
            "NERO_ENVIRONMENT=production: refusing to apply a test reset "
            "without an explicit confirmation (allow_production). Run the "
            "preview first, or use a staging database.")

    plan = report["plan"]
    report["roles_left_on_member"] = plan["roles_left_on_member"]
    if plan["blocked_on_delivered_value"] and not include_delivered:
        report["blocked"] = True
        report["mode"] = "blocked"
        report["traces"] = _traces(report, applied=False,
                                   include_prestige=include_prestige)
        report["new_game_complete"] = False
        return report
    report["mode"] = "apply"

    # Discord roles FIRST, while the claim rows that name them still exist, and
    # only the ones this reset provably owns. A failure here is not swallowed:
    # it keeps that claim row (and its temp_roles row) alive below, so the member
    # never ends up wearing an undocumented reward role.
    for role_id in plan["remove_discord_roles"]:
        try:
            if member is None:
                raise LevelResetError(
                    "no member object supplied - the role cannot be taken off, "
                    "so its claim record is preserved")
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

    failed_roles = {int(f["role_id"]) for f in report["role_failures"]}
    preserved = [c for c in report["claims"]
                 if c.get("role_id") in failed_roles]
    preserved_ids = [c["id"] for c in preserved]
    report["claims_preserved"] = [
        {"id": c["id"], "reward_level": c["reward_level"], "track": c["track"],
         "role_id": c["role_id"]} for c in preserved]

    counts = {}
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # One member's level-claim XP boosts. Untouched by a role failure:
            # a boost is not a role and its removal cannot half-fail.
            cursor = await db.execute("""
                DELETE FROM leveling_active_boosts
                WHERE guild_id=? AND user_id=? AND source LIKE ?
            """, (guild_id, user_id, BOOST_SOURCE_PREFIX + "%"))
            counts["boosts"] = cursor.rowcount
            # Temp-role units, EXCEPT for a role still on the member: keeping the
            # row lets the temp-role expiry task retry the removal on its own.
            sql = ("DELETE FROM temp_roles "
                   "WHERE guild_id=? AND user_id=? AND source=?")
            args = [guild_id, user_id, CLAIM_SOURCE]
            if failed_roles:
                sql += f" AND role_id NOT IN ({','.join('?' * len(failed_roles))})"
                args += sorted(failed_roles)
            cursor = await db.execute(sql, args)
            counts["temp_roles"] = cursor.rowcount
            # The entitlement ledger - what makes the next crossing earnable
            # again, which is the point of the tool. Rows preserved above stay:
            # record_crossing() is INSERT OR IGNORE on the reward identity, so a
            # surviving row is also what stops a re-levelled member being paid a
            # second time for a role they never gave back.
            sql = ("DELETE FROM level_reward_claims "
                   "WHERE guild_id=? AND user_id=?")
            args = [guild_id, user_id]
            if preserved_ids:
                sql += f" AND id NOT IN ({','.join('?' * len(preserved_ids))})"
                args += preserved_ids
            cursor = await db.execute(sql, args)
            counts["claims"] = cursor.rowcount
            # Zero, never delete: the row is read by systems outside leveling.
            sql = ("UPDATE levels SET xp=0, level=0, prestige=0"
                   if plan["reset_prestige"] else
                   "UPDATE levels SET xp=0, level=0")
            cursor = await db.execute(
                sql + " WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            counts["levels"] = cursor.rowcount
            if plan["reset_prestige"]:
                cursor = await db.execute("""
                    DELETE FROM prestige_vi_activations
                    WHERE guild_id=? AND user_id=?
                """, (guild_id, user_id))
                counts["vi_activations"] = cursor.rowcount
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

    report["applied"] = True
    report["deleted"] = counts
    after = await inspect_member(guild_id, user_id,
                                 include_prestige=include_prestige)
    report["after"] = {
        "xp": after["xp"], "level": after["level"],
        "prestige": after.get("prestige", 0),
        "vi_activation": after.get("vi_activation"),
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
    report["ran"] = _ran(report, counts, include_prestige)
    report["could_not"] = _could_not(report)
    report["traces"] = _traces(report, applied=True,
                               include_prestige=include_prestige)
    report["new_game_complete"] = bool(report["applied"]) and not report["traces"]
    return report


def _ran(report: dict, counts: dict, include_prestige: bool) -> list[str]:
    """What actually executed, with the row counts SQLite reported."""
    ran = []
    if counts.get("levels"):
        ran.append(f"zeroed levels.xp/level ({counts['levels']} row)"
                   + (", prestige tier" if include_prestige else ""))
    if include_prestige:
        ran.append(f"deleted prestige VI activation(s) "
                   f"({counts.get('vi_activations', 0)})")
    ran.append(f"deleted level_reward_claims rows ({counts.get('claims', 0)})")
    ran.append(f"deleted level-claim XP boosts ({counts.get('boosts', 0)})")
    ran.append(f"deleted level-claim temp-role units "
               f"({counts.get('temp_roles', 0)})")
    if report["roles_removed"]:
        ran.append(f"removed Discord role(s) {report['roles_removed']}")
    return ran


def _could_not(report: dict) -> list[str]:
    """What did NOT run, and why - never reported as a success."""
    could_not = []
    for failure in report["role_failures"]:
        could_not.append(
            f"role {failure['role_id']} was NOT removed: {failure['error']}. "
            f"Its claim record and temp-role row were kept so the removal can be "
            f"reviewed or retried.")
    for left in report.get("roles_left_on_member") or []:
        could_not.append(
            f"role {left['role_id']} was left on the member: also granted by "
            f"{', '.join(left['also_granted_by'])}. Removing it would revoke "
            f"another system's grant, so only this reward's record was cleared.")
    for claim in report["claims_preserved"]:
        could_not.append(
            f"claim {claim['id']} (L{claim['reward_level']} role "
            f"{claim['role_id']}) was preserved, not deleted.")
    for item in report["reconciliation"]["unattributable"]:
        could_not.append(
            f"{item['table']}: not reversed - {item['reason']}.")
    return could_not


def _traces(report: dict, *, applied: bool, include_prestige: bool) -> list[str]:
    """Everything from the previous run this reset does not undo.

    Non-empty means the result must not be described as a full New Game. Each
    entry says what survived and what to do about it.
    """
    traces = []
    prestige = report.get("prestige", 0)
    vi = report.get("vi_activation")
    if not include_prestige and (prestige or vi is not None):
        detail = f"permanent tier {prestige}" if prestige else "no permanent tier"
        if vi is not None:
            detail += f", Prestige VI activation from {vi}"
        traces.append(
            f"Prestige was NOT reset ({detail}). Permanent tiers are Shop "
            f"purchases and this tool does not refund them, so prestige is only "
            f"cleared when include_prestige is passed explicitly.")
    if report["claims_preserved"]:
        ids = ", ".join(str(c["id"]) for c in report["claims_preserved"])
        traces.append(
            f"{len(report['claims_preserved'])} claim record(s) kept because a "
            f"Discord role could not be removed (claim id {ids}). The member is "
            f"still wearing that reward role.")
    if report.get("roles_left_on_member"):
        traces.append(
            f"{len(report['roles_left_on_member'])} reward role(s) are still on "
            f"the member because another grant also gives them.")
    for claim in report["manual_only"]:
        traces.append(
            f"L{claim['reward_level']} {claim['track']} reward "
            f"(ref {claim['reward_ref']}) was delivered"
            + (" and its claim record is being dropped while the value stays in "
               "the wallet/inventory - reverse it by hand first"
               if applied or report.get("mode") == "apply" else
               " - its value cannot be reversed by this tool")
            + ".")
    if report["currency_credited"]:
        totals = ", ".join(f"{code}={amount}"
                           for code, amount in sorted(
                               report["currency_credited"].items()))
        traces.append(
            f"Currency credited by level claims is still in the wallet: {totals} "
            f"across {report['ledger_rows']} ledger row(s).")
    if report["inventory_candidates"]:
        traces.append(
            f"{len(report['inventory_candidates'])} inventory row(s) may hold "
            f"level-reward units and cannot be attributed - see "
            f"`reconciliation`.")
    return traces
