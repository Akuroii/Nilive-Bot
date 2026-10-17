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

Prestige is NOT part of this tool's goal and is left alone by default. The goal
is to remove the traces of the LEVEL REWARDS a test member already received, not
to rebuild their account. `levels.prestige` is the PERMANENT tier and the only
way to raise it is a Shop purchase, so zeroing it destroys something the member
paid for and this tool does not refund it; `prestige_vi_activations` is the free
booster-only VI activation. Both survive a reset and are reported as a note.
`include_prestige=True` clears them for anyone who genuinely wants that too, but
surviving prestige does NOT make the result an incomplete New Game.

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

Delivered value: reversed when it is PROVABLE, reported when it is not
--------------------------------------------------------------------
This is the point of the tool - a test member should not keep the level rewards
they already collected. But "provable" is doing real work, so each kind of value
is attributed separately and only through the bot's own existing helpers. No
blanket update ever runs against a wallet or an inventory.

  * Currency (`economy.balance` / `economy.diamonds`). The claim engine credits
    with `safe_credit(..., source="level_claim", reason="Level {L} reward",
    db=db)`, so every unit a level reward paid has its own append-only
    `transaction_ledger` row naming the currency, the amount and the reward
    level. A row is matched to a claim on (currency, amount, reward level) and
    consumed one-to-one, then reversed with `utils.ledger.reverse_transaction()`
    - the same function the dashboard's reversal button uses. It marks the
    original row `reversed=1`, writes a compensating `reversal` row, and refuses
    with `InsufficientBalance` when the member has since spent the money. A
    refusal is reported with the exact amounts; nothing is guessed and the wallet
    is never forced negative.
  * Inventory (`inventory_items`). A shop reward's frozen claim payload pins the
    item name and the exact quantity granted, so the amount is provable even
    though the row is shared. Reversal uses `utils.inventory.remove_item()`,
    which refuses with `InsufficientItems` rather than going negative. Note the
    limit honestly: `give_item()`'s upsert does NOT overwrite `source`, so a row
    created by a level claim keeps `source='level_claim'` while later mission,
    minigame, shop or trade units merge into the same `quantity`. Which physical
    units are the reward's cannot be known - only how many the reward granted,
    and whether at least that many are still there.
  * `transaction_ledger` rows are never rewritten or deleted. Reversal is an
    additive mark plus a compensating row, which is what makes it auditable.

Never touched at all: the four reward-definition tables (`leveling_rewards`,
`leveling_currency_rewards`, `leveling_shop_rewards`, `leveling_boost_rewards`),
`leveling_config`, `leveling_reset_config`, `leveling_leaderboard_history`,
`activity_stats`, the spam-incident tables, `shop_items`, `purchase_history`,
and every other member's or guild's row in any table.

The claim-deletion rule
-----------------------
A claim row is deleted ONLY when its effect is gone. Deleting a `fulfilled` row
while the value it delivered is still in the wallet or inventory makes that
reward earnable again on the next crossing - a duplicate payment - so:

  * a row whose effect this reset actually reversed      -> deleted
  * a row that never delivered anything (pending/failed) -> deleted (the point)
  * a boost row, or a role row whose role was removed,
    was already absent, or is held by another grant      -> deleted
  * a row whose effect could NOT be reversed or removed  -> KEPT, and reported

A kept row stays `fulfilled`, which is terminal: `_CLAIMABLE_STATUSES` is
("pending", "failed"), so it cannot be claimed again and `record_crossing()`'s
INSERT OR IGNORE stops a re-levelled member being paid twice for it. That is
also why the reversal steps run BEFORE the delete transaction - if the
transaction fails after a reversal, the member is left with money returned and a
terminal claim row, never with a re-claimable reward.

`include_delivered=True` overrides the last bullet and drops those rows anyway.
It is refused outright on a production database, because that is exactly the
duplicate payment the rule above exists to prevent; on a throwaway staging
database it is a deliberate choice.

Is it a New Game?
-----------------
Only if no LEVEL-REWARD trace of the previous run survives. `reset_member()`
returns `new_game_complete` plus `traces`, a plain-language list of every level
reward it could not undo: value still in the wallet or inventory, a role it
refused to take off, a claim row it kept because an effect survived. A caller
must not describe the result as a full reset while `traces` is non-empty -
`/level_test_reset` prints the list.

Things that are NOT level rewards - prestige, and any currency or item the member
holds from missions, minigames, the shop or trades - are reported separately
under `notes` and never count against the verdict. The goal is a clean level
test, not a rebuilt account.

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
# Tracks whose delivered effect is undone by deleting a row this module owns.
AUTO_REVERSIBLE_TRACKS = ("role", "boost")
# Tracks whose delivered effect lives in a shared wallet/inventory row, so it is
# reversed per claim through the bot's own helpers - and reported, never guessed,
# when those helpers refuse. See the module docstring.
MANUAL_TRACKS = ("currency", "shop")
# Shop rewards of this type never reach inventory_items: the claim engine routes
# them to `temp_roles` instead (utils/level_claims.py, the `type == "temp_role"`
# branch), so including them here would try to take items out of an inventory
# that was never given them.
NON_INVENTORY_SHOP_TYPES = ("temp_role",)
# What the compensating ledger entry says. `reverse_transaction()` writes it on
# the `reversal` row, so an operator reading the wallet history can see that a
# level test reset - not a purchase or a moderation action - took the value back.
RESET_REASON = "Level test reset: level reward reversed"
# Reward definitions and everything else that must never be touched. Listed so
# the regression test can prove the apply path issues no statement against them.
# `levels` is not here: this member's own row IS updated (xp/level zeroed), but
# never deleted, and no other member's row is reachable.
NEVER_TOUCHED_TABLES = (
    "leveling_rewards", "leveling_currency_rewards", "leveling_shop_rewards",
    "leveling_boost_rewards", "leveling_config", "leveling_reset_config",
    "leveling_leaderboard_history", "activity_stats", "shop_items",
    "purchase_history",
)
# Tables this reset DOES write, but only through the bot's own scoped helpers and
# only for an amount proven to come from a level reward. Kept separate from
# NEVER_TOUCHED_TABLES so the regression test can assert both halves: these move
# by exactly the proven amount, and nothing in the list above moves at all.
SCOPED_REVERSAL_TABLES = ("economy", "transaction_ledger", "inventory_items")


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
                         include_prestige: bool = False,
                         include_delivered: bool = False) -> dict:
    """Read-only: every record and side effect this reset would touch.

    Opens no transaction and writes nothing, so the preview can be run as often
    as needed - including against production - to see exactly what is there. The
    two flags are passed through to `_plan()` so the preview describes the apply
    the caller actually asked for.
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
        "wallet": {"balance": 0, "diamonds": 0},
        "attribution": {"currency": [], "inventory": []},
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
            try:
                frozen = json.loads(payload) if payload else {}
            except (TypeError, ValueError):
                frozen = {}
            claim = {"id": claim_id, "reward_level": level, "track": track,
                     "reward_ref": ref, "status": status,
                     "fulfilled_at": fulfilled_at, "role_id": None,
                     "payload": frozen if isinstance(frozen, dict) else {}}
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

        # Every level-claim credit row, with its reversal flag, so each one can
        # be matched to the claim that caused it and reversed at most once.
        cursor = await db.execute("""
            SELECT id, currency, amount, reason, reversed
            FROM transaction_ledger
            WHERE guild_id=? AND user_id=? AND source=? AND type='credit'
            ORDER BY id
        """, (guild_id, user_id, CLAIM_SOURCE))
        ledger = [{"id": r[0], "currency": r[1], "amount": int(r[2] or 0),
                   "reason": r[3], "reversed": bool(r[4])}
                  for r in await cursor.fetchall()]

        # The wallet as it stands, per currency: the number that decides whether
        # a reversal is safe or would eat money from another source.
        cursor = await db.execute("""
            SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        wallet = await cursor.fetchone()
        report["wallet"] = {"balance": int(wallet[0] or 0) if wallet else 0,
                            "diamonds": int(wallet[1] or 0) if wallet else 0}

        # Inventory rows. `give_item()`'s upsert does NOT overwrite `source`, so
        # a row created by a level claim keeps that source while later units from
        # other systems merge into the same quantity - reported, not trusted.
        cursor = await db.execute("""
            SELECT item_name, item_type, quantity, source
            FROM inventory_items
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        report["inventory_candidates"] = [
            {"item_name": r[0], "item_type": r[1], "quantity": r[2],
             "source": r[3]}
            for r in await cursor.fetchall()]
        held = {row["item_name"]: int(row["quantity"] or 0)
                for row in report["inventory_candidates"]}
        row_source = {row["item_name"]: row["source"]
                      for row in report["inventory_candidates"]}

        report["attribution"] = _attribute(report["claims"], ledger, held,
                                           row_source, report["wallet"])

    report["manual_only"] = [
        c for c in report["claims"]
        if c["status"] == "fulfilled" and c["track"] in MANUAL_TRACKS]
    report["reconciliation"] = _reconciliation(report)
    report["plan"] = _plan(report, include_prestige=include_prestige,
                           include_delivered=include_delivered)
    return report


def _attribute(claims: list, ledger: list, held: dict, row_source: dict,
               wallet: dict) -> dict:
    """Tie each delivered level reward to the value it actually put somewhere.

    Read-only, and deliberately one-to-one: a ledger row is consumed by the first
    claim that matches it, so two rewards of the same amount cannot both claim the
    same credit. Matching is on the frozen claim payload - what the reward really
    granted at claim time - never on a definition that may have been edited since.

    Each entry says whether the value can be taken back, so `apply` never has to
    guess:

    * currency  -> the matching `transaction_ledger` row, or why there is none.
      `reversible` additionally requires the wallet to still hold that much,
      because `reverse_transaction()` refuses (correctly) once it has been spent.
    * inventory -> the item name and the exact quantity granted, plus how many
      the member holds now. Which physical units they are cannot be known -
      quantities from missions, minigames, the shop and trades merge into one row
      - so `reversible` only asserts that at least the granted amount is present.
    """
    free = [dict(row) for row in ledger if not row["reversed"]]
    spent = [dict(row) for row in ledger if row["reversed"]]
    currency_out, inventory_out = [], []
    for claim in sorted(claims, key=lambda c: (int(c["reward_level"]),
                                               int(c["id"]))):
        if claim["status"] != "fulfilled":
            continue          # nothing was delivered, so there is nothing to undo
        payload = claim.get("payload") or {}
        if claim["track"] == "currency":
            code = str(payload.get("currency") or "")
            amount = int(payload.get("amount") or 0)
            want_reason = f"Level {claim['reward_level']} reward"
            match = matched_on = None
            for row in free:
                if row["currency"] == code and row["amount"] == amount \
                        and row["reason"] == want_reason:
                    match, matched_on = row, "currency+amount+level"
                    break
            if match is None:
                for row in free:
                    if row["currency"] == code and row["amount"] == amount:
                        match, matched_on = row, "currency+amount"
                        break
            already = None
            if match is None:
                # The credit may have been reversed earlier, by the dashboard or a
                # previous run of this tool. That counts as "effect already gone".
                for row in spent:
                    if row["currency"] == code and row["amount"] == amount:
                        already = row
                        break
            entry = {
                "claim_id": claim["id"], "reward_level": claim["reward_level"],
                "currency": code, "amount": amount,
                "ledger_id": (match or already or {}).get("id"),
                "matched_on": matched_on,
                "already_reversed": bool(already),
                "wallet": int(wallet.get(code) or 0),
            }
            if already:
                entry["reversible"] = True
                entry["note"] = "already reversed earlier"
            elif match is None:
                entry["reversible"] = False
                entry["note"] = ("no unreversed level_claim credit matches this "
                                 "reward, so what was paid cannot be proven")
            else:
                entry["reversible"] = entry["wallet"] >= amount
                entry["note"] = ("" if entry["reversible"] else
                                 f"the wallet holds {entry['wallet']} {code}, "
                                 f"less than the {amount} this reward paid - it "
                                 f"has been spent, so reversing it would take "
                                 f"money from another source")
            if match:
                free.remove(match)
            currency_out.append(entry)
        elif claim["track"] == "shop":
            if str(payload.get("type") or "") in NON_INVENTORY_SHOP_TYPES:
                continue      # routed to temp_roles, never to inventory_items
            name = str(payload.get("name") or "")
            quantity = int(payload.get("quantity") or 0)
            have = int(held.get(name, 0))
            inventory_out.append({
                "claim_id": claim["id"], "reward_level": claim["reward_level"],
                "item_name": name, "quantity": quantity, "held": have,
                "row_source": row_source.get(name),
                "reversible": quantity > 0 and have >= quantity,
                "note": ("" if quantity > 0 and have >= quantity else
                         f"the member holds {have}x {name}, fewer than the "
                         f"{quantity}x this reward granted - some were used or "
                         f"traded, so the reward's units cannot be taken back "
                         f"without touching units from another source"),
            })
    return {"currency": currency_out, "inventory": inventory_out}


def _plan(report: dict, *, include_prestige: bool = False,
          include_delivered: bool = False) -> dict:
    """Exactly what `apply` would change, derived from the inspected records.

    Read-only. Three kinds of decision are visible here before anything runs:

    * a role this member also holds through another recorded grant is moved out
      of `remove_discord_roles` into `roles_left_on_member`, because taking it off
      would revoke someone else's grant;
    * delivered currency and inventory are split into what CAN be taken back
      (proven amount still present) and what CANNOT, with the reason;
    * `claims_kept` lists the rows that would survive the reset because their
      effect survived. A role whose removal FAILS cannot be known until it is
      attempted, so `apply` adds those to `claims_preserved` itself.
    """
    claims = report["claims"]
    attribution = report.get("attribution") or {"currency": [], "inventory": []}
    others = report.get("role_other_grants") or {}
    role_ids = {r for r in report["role_ids"] if r}
    unsafe = sorted(r for r in role_ids if str(r) in others)

    money = attribution["currency"]
    goods = attribution["inventory"]
    reverse_money = [e for e in money if e["reversible"]]
    reverse_goods = [e for e in goods if e["reversible"]]
    stuck_money = [e for e in money if not e["reversible"]]
    stuck_goods = [e for e in goods if not e["reversible"]]

    # Rows kept because their effect survives. include_delivered drops exactly
    # these (and only these) - a role still on the member is never dropped, since
    # that is the case with no recovery path at all.
    kept = set()
    if not include_delivered:
        kept = {e["claim_id"] for e in stuck_money} | \
               {e["claim_id"] for e in stuck_goods}
    return {
        "zero_levels_row": report["has_level_row"],
        # The flag alone decides this: clearing prestige is an explicit choice,
        # and the preview above shows the values it would destroy.
        "reset_prestige": bool(include_prestige),
        "prestige_now": report.get("prestige", 0),
        "vi_activation_now": report.get("vi_activation"),
        "delete_claims": len(claims) - len(kept),
        "claims_kept": sorted(kept),
        "delete_boosts": len(report["boosts"]),
        "delete_temp_roles": len(report["temp_roles"]),
        "remove_discord_roles": sorted(r for r in role_ids if str(r) not in others),
        "roles_left_on_member": [
            {"role_id": r, "also_granted_by": others[str(r)]} for r in unsafe],
        "reverse_currency": [
            {"claim_id": e["claim_id"], "reward_level": e["reward_level"],
             "currency": e["currency"], "amount": e["amount"],
             "ledger_id": e["ledger_id"],
             "already_reversed": e.get("already_reversed", False)}
            for e in reverse_money],
        "reverse_inventory": [
            {"claim_id": e["claim_id"], "reward_level": e["reward_level"],
             "item_name": e["item_name"], "quantity": e["quantity"]}
            for e in reverse_goods],
        "cannot_reverse": [
            dict(e, kind="currency") for e in stuck_money]
            + [dict(e, kind="inventory") for e in stuck_goods],
        # Kept for the embed's wording; it now means "some delivered value cannot
        # be undone", not "the whole reset is refused".
        "blocked_on_delivered_value": bool(stuck_money or stuck_goods),
        "blocked_claims": [
            {"id": e["claim_id"], "reward_level": e["reward_level"],
             "track": e["kind"]} for e in
            [dict(x, kind="currency") for x in stuck_money]
            + [dict(x, kind="inventory") for x in stuck_goods]],
        "reported_not_reversed": {
            "currency": report["currency_credited"],
            "ledger_rows": report["ledger_rows"],
            "inventory_candidates": report["inventory_candidates"],
        },
    }


def _reconciliation(report: dict) -> dict:
    """The manual-review listing: proven value apart from guesswork.

    * ``proven`` - amounts attributable to level rewards with certainty, read
      from the append-only ledger and the frozen claim payloads, not from a
      reward definition that may have been edited since. Each entry carries the
      verdict the reset will act on.
    * ``unattributable`` - what looks related but cannot be pinned to a level
      reward, with the reason. An operator reconciling by hand works from this.
    """
    attribution = report.get("attribution") or {"currency": [], "inventory": []}
    return {
        "proven": {
            "currency_by_code": report["currency_credited"],
            "ledger_rows": report["ledger_rows"],
            "ledger_filter": ("transaction_ledger WHERE source='level_claim' "
                              "AND type='credit' AND guild_id/user_id = this member"),
            "wallet_now": report.get("wallet"),
            "currency": attribution["currency"],
            "inventory": attribution["inventory"],
            "delivered_claims": [
                {"claim_id": c["id"], "reward_level": c["reward_level"],
                 "track": c["track"], "reward_ref": c["reward_ref"],
                 "fulfilled_at": c["fulfilled_at"]}
                for c in report["claims"]
                if c["status"] == "fulfilled" and c["track"] in MANUAL_TRACKS],
        },
        "unattributable": [
            {"table": "inventory_items",
             "reason": "give_item()'s upsert adds to one row per "
                       "(guild, user, item_name) and does NOT overwrite `source`, "
                       "so a row created by a level claim keeps that source while "
                       "mission, minigame, shop and trade units merge into the "
                       "same quantity. How many units the reward granted is "
                       "provable; which physical units they are is not."},
            {"table": "economy",
             "reason": "coins and diamonds are fungible. The ledger above proves "
                       "what a level reward credited, but once it is spent the "
                       "wallet cannot tell it apart from any other money, so a "
                       "reversal is refused rather than forced."},
            {"table": "transaction_ledger",
             "reason": "append-only audit history; rows are marked reversed and "
                       "compensated, never rewritten or deleted."},
        ],
    }


async def reset_member(guild_id: int, user_id: int, *, apply: bool = False,
                       include_delivered: bool = False,
                       include_prestige: bool = False,
                       allow_production: bool = False,
                       reversed_by: int | None = None,
                       member=None) -> dict:
    """Preview (default) or perform the scoped reset. Returns the full report.

    ``apply=False`` never writes - it returns `inspect_member()` plus the plan.
    ``apply=True`` removes Discord roles, reverses the delivered value it can
    prove, then deletes the records whose effects are gone in one BEGIN IMMEDIATE
    transaction, and finally reports what ran and what could not.

    ``include_delivered`` additionally drops claim rows whose value could NOT be
    reversed. That makes an already-paid reward earnable again, so it is refused
    outright on a production database and is only for a throwaway staging one.
    ``include_prestige`` zeroes the permanent Prestige tier and deletes the
    booster VI activation. Permanent tiers are Shop purchases and are NOT
    refunded, so this is opt-in - and prestige is not a level reward, so leaving
    it alone never makes the reset incomplete.
    ``reversed_by`` is stamped on the ledger reversal for the audit trail.
    """
    guild_id, user_id = _check_scope(guild_id, user_id)
    report = await inspect_member(
        guild_id, user_id, include_prestige=include_prestige,
        include_delivered=include_delivered)
    report["applied"] = False
    report["roles_removed"] = []
    report["role_failures"] = []
    report["claims_preserved"] = []
    report["reversed_currency"] = []
    report["reversed_inventory"] = []
    report["unreversed"] = []
    report["reconciliation"] = _reconciliation(report)

    if not apply:
        report["mode"] = "preview"
        report["traces"] = _traces(report, applied=False)
        report["notes"] = _notes(report, include_prestige)
        report["new_game_complete"] = False
        return report

    if NERO_ENVIRONMENT == "production" and not allow_production:
        raise LevelResetError(
            "NERO_ENVIRONMENT=production: refusing to apply a test reset "
            "without an explicit confirmation (allow_production). Run the "
            "preview first, or use a staging database.")
    if include_delivered and NERO_ENVIRONMENT == "production":
        raise LevelResetError(
            "include_delivered on a production database would delete claim rows "
            "whose delivered value could not be reversed, which makes an "
            "already-paid reward claimable again. Refused - reverse that value "
            "by hand first, or use a staging database.")

    plan = report["plan"]
    report["roles_left_on_member"] = plan["roles_left_on_member"]
    report["mode"] = "apply"

    # ── 1. Discord roles, while the claim rows that name them still exist ──
    # Best effort and reported per role: a failure keeps that claim row (and its
    # temp_roles row) alive below, so the member is never left wearing a reward
    # role with nothing describing why.
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

    # ── 2. currency, through the bot's own reversal helper ──
    # `reverse_transaction()` is the dashboard's reversal path: it marks the
    # original ledger row, writes a compensating `reversal` row, refuses a row
    # already reversed, and refuses with InsufficientBalance once the money has
    # been spent. Using it means no second economy implementation and no way to
    # force a wallet negative.
    from utils.ledger import reverse_transaction
    for entry in plan["reverse_currency"]:
        if entry.get("already_reversed"):
            report["reversed_currency"].append(dict(entry, new_balance=None))
            continue
        outcome = await reverse_transaction(
            entry["ledger_id"], guild_id, reversed_by=reversed_by,
            reason=RESET_REASON)
        if outcome.get("success"):
            report["reversed_currency"].append(
                dict(entry, new_balance=outcome.get("new_balance")))
        elif outcome.get("error") == "Already reversed":
            report["reversed_currency"].append(dict(entry, new_balance=None))
        else:
            report["unreversed"].append(dict(
                entry, kind="currency", error=outcome.get("error")))

    # ── 3. inventory, through the bot's own removal helper ──
    # `remove_item()` raises InsufficientItems instead of going negative, which
    # is the same refusal shape as the wallet: the reward's units are only taken
    # back when at least that many are still there.
    from utils.inventory import InsufficientItems, remove_item
    for entry in plan["reverse_inventory"]:
        try:
            remaining = await remove_item(
                guild_id, user_id, entry["item_name"], entry["quantity"])
            report["reversed_inventory"].append(dict(entry, remaining=remaining))
        except InsufficientItems as exc:
            report["unreversed"].append(dict(entry, kind="inventory",
                                             error=str(exc)))
        except Exception as exc:
            report["unreversed"].append(dict(entry, kind="inventory",
                                             error=f"{type(exc).__name__}: {exc}"))
    # Anything the plan already knew could not be reversed is reported too, so
    # `unreversed` is the complete list whatever the reason.
    known = {(e["claim_id"], e["kind"]) for e in report["unreversed"]}
    for entry in plan["cannot_reverse"]:
        if (entry["claim_id"], entry["kind"]) not in known:
            report["unreversed"].append(dict(entry, error=entry.get("note") or ""))

    # ── 4. the records, in one transaction ──
    failed_roles = {int(f["role_id"]) for f in report["role_failures"]}
    role_preserved = [c for c in report["claims"] if c.get("role_id") in failed_roles]
    kept_ids = {c["id"] for c in role_preserved}
    if not include_delivered:
        kept_ids |= {e["claim_id"] for e in report["unreversed"]
                     if e.get("kind") in MANUAL_TRACKS}
    preserved = [c for c in report["claims"] if c["id"] in kept_ids]
    report["claims_preserved"] = [
        {"id": c["id"], "reward_level": c["reward_level"], "track": c["track"],
         "role_id": c.get("role_id"),
         "reason": ("a Discord role could not be removed"
                    if c.get("role_id") in failed_roles
                    else "its delivered value could not be reversed")}
        for c in preserved]

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
            # again, which is the point of the tool - minus every row whose
            # effect is still live. record_crossing() is INSERT OR IGNORE on the
            # reward identity, so a surviving row is also what stops a re-levelled
            # member being paid a second time.
            sql = ("DELETE FROM level_reward_claims "
                   "WHERE guild_id=? AND user_id=?")
            args = [guild_id, user_id]
            if kept_ids:
                sql += f" AND id NOT IN ({','.join('?' * len(kept_ids))})"
                args += sorted(kept_ids)
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
        "wallet": after["wallet"],
        "claims_left": [
            {"id": c["id"], "reward_level": c["reward_level"], "track": c["track"],
             "status": c["status"]} for c in after["claims"]],
    }
    report["ran"] = _ran(report, counts, include_prestige)
    report["could_not"] = _could_not(report)
    report["traces"] = _traces(report, applied=True)
    report["notes"] = _notes(report, include_prestige)
    # The verdict is about LEVEL REWARD traces only: prestige and any value the
    # member holds from other systems are notes, not failures.
    report["new_game_complete"] = bool(report["applied"]) and not report["traces"]
    return report


def _ran(report: dict, counts: dict, include_prestige: bool) -> list[str]:
    """What actually executed, with the row counts and the amounts moved."""
    ran = []
    if counts.get("levels"):
        ran.append(f"zeroed levels.xp/level ({counts['levels']} row)"
                   + (", prestige tier" if include_prestige else ""))
    if include_prestige:
        ran.append(f"deleted prestige VI activation(s) "
                   f"({counts.get('vi_activations', 0)})")
    for entry in report["reversed_currency"]:
        ran.append(
            f"reversed {entry['amount']} {entry['currency']} from L"
            f"{entry['reward_level']} (ledger {entry['ledger_id']}"
            + (", already reversed earlier)" if entry.get("already_reversed")
               else f" -> {entry.get('new_balance')})"))
    for entry in report["reversed_inventory"]:
        ran.append(f"removed {entry['quantity']}x {entry['item_name']} from L"
                   f"{entry['reward_level']} "
                   f"({entry['remaining']}x left)")
    if report["roles_removed"]:
        ran.append(f"removed Discord role(s) {report['roles_removed']}")
    ran.append(f"deleted level_reward_claims rows ({counts.get('claims', 0)})")
    ran.append(f"deleted level-claim XP boosts ({counts.get('boosts', 0)})")
    ran.append(f"deleted level-claim temp-role units "
               f"({counts.get('temp_roles', 0)})")
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
    for entry in report["unreversed"]:
        what = (f"{entry['amount']} {entry['currency']}"
                if entry.get("kind") == "currency"
                else f"{entry.get('quantity')}x {entry.get('item_name')}")
        could_not.append(
            f"L{entry['reward_level']} {entry.get('kind')} reward ({what}) was "
            f"NOT reversed: {entry.get('error') or entry.get('note')}. "
            f"Its claim record was kept, so it cannot be claimed again.")
    # The three unattributable tables are deliberately NOT listed here: this is a
    # list of actions that failed, not of general caveats. They stay in
    # `reconciliation`, which is where an operator reconciling by hand reads them.
    return could_not


def _traces(report: dict, *, applied: bool) -> list[str]:
    """Level-reward traces this reset does not undo. Non-empty = not a New Game.

    Prestige and value from other systems are deliberately absent - see
    `_notes()`. The goal is a clean level test, not a rebuilt account.
    """
    traces = []
    if report["claims_preserved"]:
        for claim in report["claims_preserved"]:
            traces.append(
                f"claim {claim['id']} (L{claim['reward_level']} {claim['track']}) "
                f"was KEPT because {claim['reason']}.")
    for entry in report["unreversed"]:
        what = (f"{entry['amount']} {entry['currency']} still in the wallet"
                if entry.get("kind") == "currency"
                else f"{entry.get('quantity')}x {entry.get('item_name')} still in "
                     f"the inventory")
        traces.append(
            f"L{entry['reward_level']} reward not reversed: {what}.")
    for left in report.get("roles_left_on_member") or []:
        traces.append(
            f"reward role {left['role_id']} is still on the member, because "
            f"{', '.join(left['also_granted_by'])} also grants it.")
    if not applied and report["plan"].get("cannot_reverse"):
        for entry in report["plan"]["cannot_reverse"]:
            traces.append(
                f"L{entry['reward_level']} {entry['kind']} reward would NOT be "
                f"reversible: {entry.get('note')}.")
    return traces


def _notes(report: dict, include_prestige: bool) -> list[str]:
    """Context that is NOT a level-reward trace, so it never blocks the verdict."""
    notes = []
    prestige = report.get("prestige", 0)
    vi = report.get("vi_activation")
    if not include_prestige and (prestige or vi is not None):
        detail = f"permanent tier {prestige}" if prestige else "no permanent tier"
        if vi is not None:
            detail += f", Prestige VI activation from {vi}"
        notes.append(
            f"Prestige left as it is ({detail}). Prestige is not a level reward "
            f"and permanent tiers are Shop purchases this tool does not refund, "
            f"so it is only cleared when include_prestige is passed.")
    other_items = [
        row for row in report["inventory_candidates"]
        if row["source"] != CLAIM_SOURCE]
    if other_items:
        notes.append(
            f"{len(other_items)} inventory row(s) come from another source and "
            f"were left alone: "
            + ", ".join(f"{r['item_name']} x{r['quantity']} ({r['source']})"
                        for r in other_items[:10]) + ".")
    return notes
