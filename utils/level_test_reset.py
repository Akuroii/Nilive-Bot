"""Scoped reset of ONE member's /level test state - preview first, apply second.

Why this exists
---------------
Testing `/level` end to end means crossing reward levels, claiming them, and then
wanting to do it again from zero. Nothing in the bot could do that: `/resetxp`
zeroes XP but deliberately leaves `level_reward_claims` alone (it is a moderation
correction, not a test reset), and because the claim ledger is a one-way ratchet -
`record_crossing()` uses INSERT OR IGNORE on the identity (guild, user, level,
track, ref) - re-levelling after a reset can never pay a second time. Correct for
production, useless for testing.

The goal is to remove the traces of the LEVEL REWARDS this member already
received. Not to rebuild their account, not to zero their economy, and not to
touch anything that is not a level reward.

Scope
-----
EVERY statement here is bound to one (guild_id, user_id) pair. There is no code
path that can reach a second member, a second guild, or a whole table.

  * `levels.xp` / `levels.level`          -> zeroed for this member (never deleted)
  * `level_reward_claims`                 -> this member's rows deleted, EXCEPT
                                             any row whose effect survived
  * `leveling_active_boosts` source LIKE
    'level_claim:%'                       -> deleted (grant_xp_boost records the
                                             claim id)
  * `temp_roles` source='level_claim'     -> deleted, except for a role whose
                                             removal failed
  * Discord roles from role-track AND
    temp-role shop claims                 -> removed, when a member object is
                                             supplied and removal is provably safe

Never touched at all: the four reward-definition tables (`leveling_rewards`,
`leveling_currency_rewards`, `leveling_shop_rewards`, `leveling_boost_rewards`),
`leveling_config`, `leveling_reset_config`, `leveling_leaderboard_history`,
`activity_stats`, the spam-incident tables, `shop_items`, `purchase_history`, and
every other member's or guild's row in any table.

Prestige is NOT part of this tool's goal and is left alone by default.
`levels.prestige` is the PERMANENT tier and the only way to raise it is a Shop
purchase, so zeroing it destroys something the member paid for and this tool does
not refund it; `prestige_vi_activations` is the free booster-only VI activation.
Both survive a reset and are reported as a note. `include_prestige=True` clears
them for anyone who genuinely wants that too, but surviving prestige does NOT make
the result an incomplete New Game.

How delivered value is reversed - provenance, not inference
-----------------------------------------------------------
An earlier version of this tool judged an inventory reward reversible when the
member's current stack was at least as large as the reward's quantity. That is not
provenance, and it lost real assets:

  A reward grants 2x Potion. The member drinks both. Later a quest grants 5x
  Potion. `inventory_items` has ONE row per (guild, user, item_name) and
  `give_item()`'s upsert does not overwrite `source`, so the stack reads 5 and the
  old rule removed 2 quest potions to "reverse" a reward that was already gone.

Three defects came out of that design and are fixed here, with the storage in
`utils/level_ops.py`:

1. **Provenance.** The claim engine now writes a `reward_grants` row inside the
   same transaction as each delivery, carrying the claim id, the exact quantity
   and a `remaining_quantity`. Every path that removes units from a stack
   (`remove_item`, `drop_item`, a trade) decrements that counter FIFO, and an
   absolute `set_quantity` write marks the stack's grants unattributed because it
   cannot say which units it replaced. So "how many of this reward's units are
   still here" is a recorded fact.
2. **A reversal journal.** `reward_reversals` has UNIQUE(guild, user, claim, kind)
   and each reversal is written in the SAME transaction as the movement it
   reverses. A retry - or a second admin clicking the same button - hits the
   constraint instead of moving value twice. Previously `remove_item()` committed
   its own transaction and the claim delete ran afterwards, so a failure in
   between left the items gone, the claim row present, and nothing recording that
   the reversal had already happened: the retry removed them again.
3. **Refuse, don't invent.** With no grant row (anything delivered before
   `reward_grants` existed) or with a grant whose attributable units were
   consumed, the reversal is recorded as `refused`/`outstanding` with the reason
   and NOTHING is removed. Those cases go to `/level_reconcile`, where an
   administrator decides with the evidence in front of them. Guessing is what
   destroyed unrelated items; an unresolved queue is recoverable.

Currency is in a better position than inventory and is reversed for historical
claims too: the claim engine credits with `safe_credit(..., source="level_claim",
reason="Level {L} reward", db=db)`, so every unit a level reward paid has its OWN
append-only `transaction_ledger` row. A grant row names that row id directly;
without one, the row is matched on (currency, amount, reward level) and consumed
one-to-one so two rewards of the same amount cannot both claim the same credit.

The reversal itself does not use `utils.ledger.reverse_transaction()`. That helper
spans three connections and two commits - debit the wallet, then mark the original
row `reversed=1`, then append a compensating row - so a crash between them leaves
a charged wallet and a ledger row that still looks reversible, and a retry charges
it again. It also writes TWO rows for ONE movement (`safe_deduct`'s `deduct` plus
its own `reversal`), which double-counts the event for anyone summing the ledger.
`utils.level_ops.reverse_currency()` does the debit, the marker, exactly one
compensating row and the journal entry in one `BEGIN IMMEDIATE` transaction, and
refuses rather than forcing the wallet negative when the money has been spent.

The claim-deletion rule
-----------------------
A claim row is deleted ONLY when its effect is gone:

  * a row whose effect this reset actually reversed      -> deleted
  * a row that never delivered anything (pending/failed) -> deleted (the point)
  * a boost row, or a role row whose role was removed,
    was already absent, or is held by another grant      -> deleted
  * a row whose effect could NOT be reversed or removed  -> KEPT, and reported

A kept row stays `fulfilled`, which is terminal: `_CLAIMABLE_STATUSES` is
("pending", "failed"), so it cannot be claimed again, and `record_crossing()`'s
INSERT OR IGNORE stops a re-levelled member being paid twice for it.

There is deliberately NO flag that overrides this. The previous
`include_delivered=True` deleted those rows anyway "for staging", which erased the
only record preventing a second payment while the value stayed in the member's
hands - and an option that is refused in production but honoured in staging is one
mistake away from being honoured in production. The staging need is served by
`/level_reconcile`, which resolves ONE named case at a time, records the decision
and the administrator who made it, and only then allows that claim row to go.

Concurrency
-----------
The reset holds a per-member lease in `level_op_locks` (a database row, not a
Python lock: the bot and the dashboard are separate processes over one SQLite
file) for its whole duration, including the Discord API calls it cannot make
transactional. `claim_available()` checks the same lease inside its own write
transaction and refuses to deliver while it is held, so a reward cannot be paid in
the window between this module's inspection and its final commit - a claim row
created there would otherwise be deleted by a reset that never saw its value.

The lease is belt and braces, not the only defence. The final transaction re-reads
the member's claims and refuses to delete a `fulfilled` row it did not inspect, so
even a writer that ignores the lease cannot produce a paid-and-forgotten reward;
the row survives, appears in `traces`, and the report says to run again.

Role removal: ordering and safety
---------------------------------
The Discord role is taken off BEFORE any record is deleted, and a role that could
not be taken off keeps its records. A failed removal used to leave the member
wearing a reward role with no row anywhere describing why - no review path and no
recovery path. Now it preserves that claim row AND its `temp_roles` row, so
`/level` still shows the reward, the temp-role expiry task can retry the removal
on its own schedule, and `role_failures` names the role and the error.

A role is only removed when its removal is provably ours to make: if this member
also holds the same role through another recorded grant (a `temp_roles` row with a
different source, or an `inventory_items` role marker written by another system)
then taking it off would revoke something else's grant. Such a role is left on the
member, the claim record IS deleted (the other grant, not this one, is keeping it
on), and the report lists it under `roles_left_on_member` with the competing
sources. This list is not exhaustive - a role granted by a system that records
nothing cannot be detected - which is why removal failures are preserved rather
than assumed away.

Policy: one Discord role ID may be owned by several systems
------------------------------------------------------------
That principle is not specific to this tool. A Discord role ID can legitimately be
configured as a Level reward, a Prestige tier role, a Booster(-only) role, a Twitch
live role, the MVP role AND a Shop item at the same time. Nothing validates those
pairs against each other, so the supported, backwards-compatible policy is that the
sharing is ALLOWED - existing guilds rely on it, and rejecting it would need a
migration and an operational review of those configurations.

What follows is an obligation on every remover: it may retire only its OWN
entitlement, and must leave the role on the member while any other source still
justifies it. Both directions are guarded:

* direction 1 - the system retires its own grant. Before `remove_roles`, these call
  `utils.timed_ownership.role_removal_blocked(guild_id, user_id, role_id,
  retiring=...)`: `utils.level_claims.enforce_role_progression` and
  `strip_booster_roles`, `utils.prestige.sync_prestige_roles`,
  `cogs.boost._handle_unboost`, the `/boostcolor` switch, `cogs.twitch._go_offline`
  and the `cogs.mvp` rollover. The helper asks whether ANY other source still grants
  the role - Shop ownership, a fulfilled Level claim, the effective Prestige tier,
  an active Booster role, a live Twitch watch, the newest MVP award - and the
  required `retiring=` argument names the GRANT the caller is itself retiring, so
  that grant is never counted against itself. One source can grant the same role
  more than once, so the argument is a grant, not a family: `level:member` /
  `level:booster` for one progression audience, `prestige:<tier>` for one tier,
  `booster:color` for the picked colour against the configured `boost1`/`boost2`
  reward role, and the bare `booster` only where the boost itself ends and every
  Booster grant really is retired. That exclusion is what keeps the check from
  retaining a role forever: the Level ledger still shows the claim whose role is
  being superseded, and the Boost config still names the role a boost loss is
  retiring. A caller that forgets `retiring=`, or names a grant that does not
  exist, fails loudly (`ValueError`) rather than silently falling back to "does
  the Shop own it?";

  Two of the sources need no exclusion, because their retiring grant is already
  invisible by the time the removal runs: the Twitch poller writes `is_live = 0`
  before `_go_offline`, and the MVP rollover writes the new winner's `mvp_history`
  row before cleaning up the previous holders. That is also why a SECOND live
  configuration of the same source - another live watch awarding the same role -
  still protects the role, while the grant being retired does not;

* direction 2 - the member unequips the Shop copy while another system still grants
  the role. That is `utils.timed_ownership.non_slot_role_entitlement`, consulted by
  `utils.equip_engine.unequip_role`, which clears the equip slot (what the member
  asked for) but keeps the role worn. It covers fulfilled Level claims, the
  effective Prestige/Booster tier, `twitch_config` (`is_live`, `give_role_id`,
  `discord_user_id`) and the MVP award (`mvp_config.mvp_role_id` plus the winner of
  the most recent `mvp_history` row - the same row the rollover reads to decide a
  cycle has elapsed).

An unreadable store is reported as a reason to KEEP the role, so a failed query can
never be read as "nobody else owns this". A table that does not exist yet is the one
exception: it means the feature has no rows for that guild, which is "not an
entitlement" rather than an unverifiable one.

Declining a removal never changes the system's own state: the Level claim stays
fulfilled, the Booster eligibility check stays authoritative, the Prestige tier is
still the effective one, the stream is still recorded as ended and the MVP award is
still the newest one. Only the Discord removal is skipped, and it is reported (a
`preserved` list, a `roles_left_on_member`-style entry, or an explicit log line
where the API has no report object).

The known limit of direction 2 is state, not intent: a source that records nothing
about whom it granted a role to cannot be consulted, and the removal-error path is
then the only evidence. Outside this policy are removals the member themselves asked
for (`cogs/reactionroles`, `cogs/customcommands`) and the destructive
`reset_member()` in this module.

Is it a New Game?
-----------------
Only if no LEVEL-REWARD trace of the previous run survives. `reset_member()`
returns `new_game_complete` plus `traces`: value still in the wallet or inventory,
a role left on the member, a claim row kept because an effect survived, a reward
delivered by a concurrent operation. A caller must not describe the result as a
full reset while `traces` is non-empty. Things that are NOT level rewards -
prestige, and any currency or item the member holds from missions, minigames, the
shop or trades - are reported under `notes` and never count against the verdict.

Environment safety
------------------
Whether this may write is decided by `utils.level_ops.environment_verdict()`,
which compares the database file actually open against the configured staging and
production paths. It does NOT trust the label: `database.py` resolves every
`NERO_ENVIRONMENT` value except the exact string "staging" to the production file,
so a guard written as `if NERO_ENVIRONMENT == "production"` lets `dev`, `test` or a
typo run a destructive reset against production. An ambiguous configuration is
refused even with the production authorization key, because ambiguous means the
code cannot tell which data it is about to destroy.
"""
from __future__ import annotations

import json

import aiosqlite

from database import DB_PATH
from utils import level_ops as ops

CLAIM_SOURCE = "level_claim"
BOOST_SOURCE_PREFIX = "level_claim:"
# Tracks whose delivered effect is undone by deleting a row this module owns.
AUTO_REVERSIBLE_TRACKS = ("role", "boost")
# Tracks whose delivered effect lives in a shared wallet/inventory row, so it is
# reversed per claim through the journal - and reported, never guessed, when the
# evidence is not there. See the module docstring.
MANUAL_TRACKS = ("currency", "shop")
# `backfill_legacy_claims()` marks every already-reached reward `fulfilled` with
# NO payout, and stamps the rows with this source (its migration is literally
# named `legacy_fulfilled_no_payout`). So for one of these rows the record itself
# says the bot never delivered anything: there is no value to reverse, and
# deleting it cannot pay a reward twice - it re-opens one the bot never paid.
LEGACY_BACKFILL_SOURCE = "legacy_backfill"
# A shop reward of this type routes its role through `temp_roles`. Its inventory
# marker row is still an item stack and still gets provenance, but its Discord
# role has to be taken off the member too - which an earlier version of this tool
# never did, while still deleting the temp_roles rows that were the only thing
# scheduled to remove it.
TEMP_ROLE_TYPE = "temp_role"
# What the compensating ledger entry says, so an operator reading the wallet
# history can see that a level test reset - not a purchase or a moderation action
# - took the value back.
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
# Tables this reset DOES write, but only through scoped helpers and only for an
# amount proven to come from a level reward. Kept separate from
# NEVER_TOUCHED_TABLES so the regression test can assert both halves: these move
# by exactly the proven amount, and nothing in the list above moves at all.
SCOPED_REVERSAL_TABLES = ("economy", "transaction_ledger", "inventory_items")
# The provenance/journal tables this reset writes. Audit rows only: they record
# what happened and never hold a member's assets.
PROVENANCE_TABLES = ("reward_grants", "reward_reversals", "level_ops",
                     "level_op_locks", "xp_mutations")


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

    Opens no transaction and writes nothing, so the preview can be run as often as
    needed - including against production - to see exactly what is there.
    """
    guild_id, user_id = _check_scope(guild_id, user_id)
    await ops.ensure_tables()
    verdict = ops.environment_verdict()
    report: dict = {
        "guild_id": guild_id,
        "user_id": user_id,
        "environment": verdict["label"],
        "target": verdict["target"],
        "db_path": verdict["db_path"],
        "xp": 0,
        "level": 0,
        "prestige": 0,
        "has_level_row": False,
        "vi_activation": None,
        "role_other_grants": {},
        "role_retention_obligations": [],
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
        "grants": {},
        "journal": [],
        "outstanding": [],
        "attribution": {"currency": [], "inventory": [], "legacy_backfill": []},
        "manual_only": [],
        "lock": None,
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

        # `source` distinguishes a legacy backfill row - which the migration
        # named `legacy_fulfilled_no_payout` marks fulfilled WITHOUT ever paying -
        # from a row the claim engine really delivered. `operation_id` names the
        # administrative XP change that created the entitlement, when there was
        # one. Both are read defensively so a database that predates either
        # column can still be inspected.
        claim_columns = "id, reward_level, track, reward_ref, status, fulfilled_at, payload_json"
        for optional in ("source", "operation_id"):
            try:
                cursor = await db.execute(
                    f"SELECT {claim_columns}, {optional} FROM level_reward_claims "
                    f"WHERE guild_id=? AND user_id=? ORDER BY reward_level, id",
                    (guild_id, user_id))
                rows_read = await cursor.fetchall()
                claim_columns += f", {optional}"
            except aiosqlite.Error:
                rows_read = None
        cursor = await db.execute(
            f"SELECT {claim_columns} FROM level_reward_claims "
            f"WHERE guild_id=? AND user_id=? ORDER BY reward_level, id",
            (guild_id, user_id))
        fetched = await cursor.fetchall()
        for claim_row in fetched:
            (claim_id, level, track, ref, status, fulfilled_at,
             payload) = claim_row[:7]
            source = claim_row[7] if len(claim_row) > 7 else None
            claim_operation = claim_row[8] if len(claim_row) > 8 else None
            try:
                frozen = json.loads(payload) if payload else {}
            except (TypeError, ValueError):
                frozen = {}
            claim = {"id": claim_id, "reward_level": level, "track": track,
                     "reward_ref": ref, "status": status,
                     "fulfilled_at": fulfilled_at, "role_id": None,
                     "source": source, "operation_id": claim_operation,
                     "legacy_backfill": str(source or "") == LEGACY_BACKFILL_SOURCE,
                     "payload": frozen if isinstance(frozen, dict) else {}}
            report["claims"].append(claim)
            report["claims_by_status"][status] = (
                report["claims_by_status"].get(status, 0) + 1)
            # A role reward AND a temp-role shop reward both put a Discord role on
            # the member. Collecting only the first left temp-role rewards wearing
            # their role forever, because this reset then deleted the `temp_roles`
            # rows that were the only thing scheduled to take it off.
            wants_role = (track == "role"
                          or (track == "shop"
                              and str(claim["payload"].get("type") or "")
                              == TEMP_ROLE_TYPE))
            if wants_role:
                try:
                    claim["role_id"] = int(claim["payload"]["role_id"])
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
                f"temp_roles ({source})")
        # An inventory role marker written by something other than a level claim
        # is the same kind of competing grant: the equip system resolves which
        # role a stack row points to from its metadata.
        cursor = await db.execute("""
            SELECT item_name, source, metadata FROM inventory_items
            WHERE guild_id=? AND user_id=? AND item_type IN ('role','temp_role')
        """, (guild_id, user_id))
        for item_name, source, metadata in await cursor.fetchall():
            try:
                role_id = int((json.loads(metadata) or {}).get("role_id"))
            except (TypeError, ValueError, KeyError):
                continue
            if str(source) != CLAIM_SOURCE:
                report["role_other_grants"].setdefault(str(role_id), set()).add(
                    f"inventory '{item_name}' ({source})")
        try:
            from utils.timed_ownership import (
                RETENTION_ITEM_TYPE, is_expired as _th_is_expired)
            cursor = await db.execute("""
                SELECT id, role_id, item_name, item_type, expires_at
                FROM timed_holdings
                WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            for (holding_id, role_id, item_name, item_type,
                 expires_at) in await cursor.fetchall():
                if role_id is None:
                    continue
                if str(item_type or "") == RETENTION_ITEM_TYPE:
                    # A durable role-retention obligation is NOT a grant: it
                    # records work the bot still owes, not something the
                    # member owns. Counting it as `role_other_grants` would
                    # tell the operator the role is legitimately held by a
                    # timed holding when the bot may in fact still owe it.
                    # Report it separately so it stays visible.
                    report["role_retention_obligations"].append({
                        "holding_id": int(holding_id),
                        "role_id": int(role_id),
                        "item_name": item_name,
                    })
                    continue
                if _th_is_expired(expires_at):
                    continue
                report["role_other_grants"].setdefault(
                    str(int(role_id)), set()
                ).add(f"timed holding '{item_name}' (#{holding_id})")
        except aiosqlite.OperationalError as exc:
            if "no such table: timed_holdings" not in str(exc).lower():
                raise
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

        # Every level-claim credit row, with its reversal flag, so each one can be
        # matched to the claim that caused it and reversed at most once.
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
        # Kept in the report: /xp_rollback re-runs the SAME attribution over a
        # SUBSET of claims (the ones one operation created) and needs the very
        # rows this inspection matched against, not a second reading of them.
        report["ledger"] = ledger

        # Inventory rows. `give_item()`'s upsert does NOT overwrite `source`, so
        # a row created by a level claim keeps that source while later mission,
        # minigame, shop or trade units merge into the same quantity. The stack
        # total is reported for context and trusted for nothing.
        cursor = await db.execute("""
            SELECT item_name, item_type, quantity, source
            FROM inventory_items
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        report["inventory_candidates"] = [
            {"item_name": r[0], "item_type": r[1], "quantity": r[2],
             "source": r[3]}
            for r in await cursor.fetchall()]

        # Provenance: what each claim actually granted and how much of it is still
        # attributable. This, not the stack total, decides a reversal.
        # Tolerant on purpose. These tables did not exist before this module, and
        # a database file swapped in under a running process (a restore, a staging
        # copy) can arrive without them. Either way the answer is the same: no
        # provenance, so nothing is claimed as reversible. Crashing here would
        # make an old member uninspectable, which is worse than reporting
        # honestly that their history cannot be proven.
        # Shop purchase grants share the ledger (utils.level_ops
        # GRANT_SOURCE_PURCHASE) but are not level rewards: this report, keyed
        # by (claim_id, kind), describes what the claim engine granted, so they
        # are excluded here and the level numbers stay what they were.
        try:
            cursor = await db.execute("""
                SELECT claim_id, kind, grant_id, quantity, remaining_quantity,
                       unattributed, unattributed_reason, item_name, currency,
                       ledger_id, reward_level, track
                FROM reward_grants WHERE guild_id=? AND user_id=?
                  AND COALESCE(grant_source, 'level_reward') <> 'shop_purchase'
                ORDER BY id
            """, (guild_id, user_id))
            grant_rows = await cursor.fetchall()
        except aiosqlite.OperationalError:
            ops.invalidate_tables_cache()
            grant_rows = []
        for r in grant_rows:
            report["grants"][(r[0], r[1])] = {
                "claim_id": r[0], "kind": r[1], "grant_id": r[2],
                "quantity": int(r[3] or 0),
                "remaining_quantity": int(r[4] or 0),
                "unattributed": bool(r[5]), "unattributed_reason": r[6],
                "item_name": r[7], "currency": r[8], "ledger_id": r[9],
                "reward_level": r[10], "track": r[11]}

        # The reversal journal: what has already been done for this member, by
        # which operation. This is what makes a retry resume instead of repeat.
        try:
            cursor = await db.execute("""
                SELECT claim_id, kind, status, quantity, reason, operation_id,
                       created_at, reversal_id
                FROM reward_reversals WHERE guild_id=? AND user_id=? ORDER BY id
            """, (guild_id, user_id))
            journal_rows = await cursor.fetchall()
        except aiosqlite.OperationalError:
            ops.invalidate_tables_cache()
            journal_rows = []
        report["journal"] = [
            {"claim_id": r[0], "kind": r[1], "status": r[2], "quantity": r[3],
             "reason": r[4], "operation_id": r[5], "created_at": r[6],
             "reversal_id": r[7]} for r in journal_rows]
        report["outstanding"] = [
            j for j in report["journal"] if j["status"] != ops.STATUS_APPLIED]

    report["lock"] = await ops.lock_held(guild_id, user_id)
    report["manual_only"] = [
        c for c in report["claims"]
        if c["status"] == "fulfilled" and c["track"] in MANUAL_TRACKS]
    report["attribution"] = _attribute(report, ledger)
    # `attribution` now has three keys, so any caller defaulting the dict must not
    # assume the old two-key shape.
    report["attribution"].setdefault("legacy_backfill", [])
    report["reconciliation"] = _reconciliation(report)
    report["plan"] = _plan(report, include_prestige=include_prestige)
    # A caller of `inspect_member()` gets the COMPLETE read-only picture, not just
    # the raw rows: `/level_test_reset`'s preview, `/level_reconcile`'s evidence
    # list and `/xp_rollback`'s preview all render these, and an inspection that
    # omitted them would let an administrator confirm a destructive step without
    # seeing what the tool already knows it cannot undo.
    report["traces"] = _traces(report, applied=False)
    report["notes"] = _notes(report, include_prestige)
    return report


def _attribute(report: dict, ledger: list) -> dict:
    """Tie each delivered level reward to the value it actually put somewhere.

    Read-only, and deliberately one-to-one: a ledger row or a grant's attributable
    remainder is consumed by the first claim that matches it, so two rewards of the
    same amount cannot both claim the same credit - which is how 2x + 2x used to be
    called reversible from a stack of 3.

    Each entry carries the verdict `apply` will act on, and the evidence for it:

    * currency  -> the grant's ledger row when provenance exists, otherwise an
      unreversed `transaction_ledger` row matched on (currency, amount, reward
      level). `reversible` also requires the wallet to still hold that much,
      because a reversal is refused once the money has been spent.
    * inventory -> reversible ONLY from a `reward_grants` row whose attributable
      remainder still covers the whole granted quantity. No grant row, or a grant
      whose units were consumed, is `refused`: the stack total proves nothing
      about which units are whose.
    """
    claims = report["claims"]
    grants = report["grants"]
    journal = {(j["claim_id"], j["kind"]): j for j in report["journal"]}
    wallet = report["wallet"]
    free = [dict(row) for row in ledger if not row["reversed"]]
    spent = [dict(row) for row in ledger if row["reversed"]]
    currency_out: list[dict] = []
    inventory_out: list[dict] = []
    legacy_out: list[dict] = []

    for claim in sorted(claims, key=lambda c: (c["reward_level"], c["id"])):
        if claim["status"] != "fulfilled":
            continue        # nothing was delivered, so nothing to reverse
        payload = claim["payload"]
        if claim.get("legacy_backfill"):
            # The migration that created this row is named
            # `legacy_fulfilled_no_payout` and the source column repeats it: the
            # bot marked an already-reached reward fulfilled precisely so it would
            # NOT be paid. There is no delivery to reverse, and reporting one as
            # "outstanding" would put a case on the reconciliation queue that no
            # evidence supports and that no administrator could ever close.
            legacy_out.append({
                "claim_id": claim["id"], "reward_level": claim["reward_level"],
                "track": claim["track"],
                "note": "marked fulfilled by the legacy backfill migration, which "
                        "never paid it (source='legacy_backfill'); no delivery "
                        "record exists, so nothing is reversed"})
            continue
        done = journal.get((claim["id"], None))
        if claim["track"] == "currency":
            code = "balance" if payload.get("currency") == "balance" else "diamonds"
            amount = int(payload.get("amount") or 0)
            grant = grants.get((claim["id"], ops.KIND_CURRENCY))
            prior = journal.get((claim["id"], ops.KIND_CURRENCY))
            entry = {
                "claim_id": claim["id"], "reward_level": claim["reward_level"],
                "currency": code, "amount": amount, "wallet": int(wallet.get(code) or 0),
                "ledger_id": None, "matched_on": None, "already_reversed": False,
                "provenance": "none",
            }
            if prior:
                entry.update(reversible=prior["status"] in (
                    ops.STATUS_APPLIED, ops.STATUS_ALREADY),
                    already_reversed=True, provenance="journal",
                    ledger_id=(grant or {}).get("ledger_id"),
                    note=f"already recorded {prior['status']} by operation "
                         f"{prior['operation_id']}")
                currency_out.append(entry)
                continue
            if grant and grant.get("ledger_id"):
                row = next((r for r in free if r["id"] == grant["ledger_id"]), None)
                if row is None:
                    row = next((r for r in spent
                                if r["id"] == grant["ledger_id"]), None)
                    entry["already_reversed"] = row is not None
                entry.update(ledger_id=grant["ledger_id"],
                             matched_on="grant provenance",
                             provenance="reward_grants")
                if row is not None and not entry["already_reversed"]:
                    free.remove(row)
            else:
                # Historical claim: no grant row, but every credit has its own
                # ledger row naming the reward level, so the amount is still
                # provable - matched one-to-one and consumed.
                want_reason = f"Level {claim['reward_level']} reward"
                match = matched_on = None
                for row in free:
                    if (row["currency"] == code and row["amount"] == amount
                            and row["reason"] == want_reason):
                        match, matched_on = row, "currency+amount+level"
                        break
                if match is None:
                    for row in free:
                        if row["currency"] == code and row["amount"] == amount:
                            match, matched_on = row, "currency+amount"
                            break
                if match is None:
                    already = next((r for r in spent
                                    if r["currency"] == code
                                    and r["amount"] == amount), None)
                    if already:
                        entry.update(already_reversed=True, reversible=True,
                                     provenance="ledger (already reversed)",
                                     ledger_id=already["id"],
                                     note="already reversed earlier")
                        currency_out.append(entry)
                        continue
                    entry.update(reversible=False, provenance="none",
                                 note="no unreversed level_claim credit matches "
                                      "this reward, so what was paid cannot be "
                                      "proven")
                    currency_out.append(entry)
                    continue
                entry.update(ledger_id=match["id"], matched_on=matched_on,
                             provenance="transaction_ledger")
                free.remove(match)
            if entry.get("already_reversed"):
                entry["reversible"] = True
                entry.setdefault("note", "already reversed earlier")
            else:
                entry["reversible"] = entry["wallet"] >= amount
                entry["note"] = ("" if entry["reversible"] else
                                 f"the wallet holds {entry['wallet']} {code}, "
                                 f"less than the {amount} this reward paid - it "
                                 f"has been spent, so reversing it would take "
                                 f"money from another source")
            currency_out.append(entry)

        elif claim["track"] == "shop":
            name = str(payload.get("name") or "")
            quantity = int(payload.get("quantity") or 0)
            grant = grants.get((claim["id"], ops.KIND_INVENTORY))
            prior = journal.get((claim["id"], ops.KIND_INVENTORY))
            stack = next((r["quantity"] for r in report["inventory_candidates"]
                          if r["item_name"] == name), 0)
            entry = {
                "claim_id": claim["id"], "reward_level": claim["reward_level"],
                "item_name": name, "quantity": quantity, "stack": int(stack or 0),
                "grant_id": (grant or {}).get("grant_id"),
                "attributable": int((grant or {}).get("remaining_quantity") or 0),
                "provenance": "reward_grants" if grant else "none",
            }
            if prior:
                entry.update(reversible=prior["status"] in (
                    ops.STATUS_APPLIED, ops.STATUS_ALREADY),
                    already_done=True,
                    note=f"already recorded {prior['status']} by operation "
                         f"{prior['operation_id']}")
            elif not grant:
                entry.update(reversible=False, note=(
                    "no provenance record: this reward was delivered before "
                    "reward_grants existed, and inventory_items merges units from "
                    "every source into one quantity, so the units in the stack "
                    "cannot be shown to be this reward's"))
            elif grant["unattributed"]:
                entry.update(reversible=False, note=(
                    "provenance was invalidated: "
                    f"{grant['unattributed_reason'] or 'an absolute quantity write'}"))
            elif entry["attributable"] < quantity:
                entry.update(reversible=False, note=(
                    f"only {entry['attributable']} of the {quantity} units this "
                    "reward granted are still attributable - the rest were used, "
                    "traded or dropped, so they cannot be taken back without "
                    "removing units from another source"))
            else:
                entry.update(reversible=True, note="")
            inventory_out.append(entry)

    return {"currency": currency_out, "inventory": inventory_out,
            "legacy_backfill": legacy_out}


def _plan(report: dict, *, include_prestige: bool = False) -> dict:
    """Exactly what `apply` would change, derived from the inspected records.

    Read-only. Three kinds of decision are visible here before anything runs:

    * a role this member also holds through another recorded grant is moved out
      of `remove_discord_roles` into `roles_left_on_member`, because taking it off
      would revoke someone else's grant;
    * delivered currency and inventory are split into what CAN be taken back
      (proven, and still there) and what CANNOT, with the evidence and the reason;
    * `claims_kept` lists the rows that would survive the reset because their
      effect survived. A role whose removal FAILS cannot be known until it is
      attempted, so `apply` adds those to `claims_preserved` itself.
    """
    claims = report["claims"]
    attribution = report.get("attribution") or {"currency": [], "inventory": []}
    others = report.get("role_other_grants") or {}
    role_ids = {r for r in report["role_ids"] if r}
    unsafe = sorted(r for r in role_ids if str(r) in others)

    money = attribution.get("currency") or []
    goods = attribution.get("inventory") or []
    legacy = attribution.get("legacy_backfill") or []
    reverse_money = [e for e in money if e["reversible"] and not e.get("already_reversed")]
    reverse_goods = [e for e in goods if e["reversible"] and not e.get("already_done")]
    stuck_money = [e for e in money if not e["reversible"]]
    stuck_goods = [e for e in goods if not e["reversible"]]

    # Rows kept because their effect survives. There is no override: deleting one
    # of these would make an already-paid reward earnable again while its value is
    # still in the member's hands. A `processing` row is kept for the same
    # reason - its delivery is still running - so the preview counts it as kept
    # instead of promising a deletion `apply` will not perform.
    kept = ({e["claim_id"] for e in stuck_money}
            | {e["claim_id"] for e in stuck_goods}
            | {c["id"] for c in claims if c["status"] == "processing"})
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
             "ledger_id": e["ledger_id"], "provenance": e["provenance"]}
            for e in reverse_money],
        "reverse_inventory": [
            {"claim_id": e["claim_id"], "reward_level": e["reward_level"],
             "item_name": e["item_name"], "quantity": e["quantity"],
             "attributable": e["attributable"], "provenance": e["provenance"]}
            for e in reverse_goods],
        "cannot_reverse": [
            dict(e, kind="currency") for e in stuck_money]
            + [dict(e, kind="inventory") for e in stuck_goods],
        "already_reversed": [
            {"claim_id": e["claim_id"], "kind": "currency",
             "reward_level": e["reward_level"]}
            for e in money if e.get("already_reversed")]
            + [{"claim_id": e["claim_id"], "kind": "inventory",
                "reward_level": e["reward_level"]}
               for e in goods if e.get("already_done")],
        "outstanding_already_recorded": report.get("outstanding") or [],
        # Deletable, and reported as deleted, because the record itself says the
        # bot never paid them. Nothing here is inferred from a stack total.
        "legacy_backfill_claims": [
            {"claim_id": e["claim_id"], "reward_level": e["reward_level"],
             "track": e["track"], "note": e["note"]} for e in legacy],
    }


def _reconciliation(report: dict) -> dict:
    """The manual-review listing: proven value apart from guesswork.

    * ``proven`` - amounts attributable to level rewards with certainty, read
      from the append-only ledger, the grant records and the frozen claim
      payloads, not from a reward definition that may have been edited since.
      Each entry carries the verdict the reset will act on.
    * ``unattributable`` - what looks related but cannot be pinned to a level
      reward, with the reason. An operator reconciling by hand works from this.
    """
    attribution = report.get("attribution") or {}
    return {
        "proven": {
            "currency_by_code": report["currency_credited"],
            "ledger_rows": report["ledger_rows"],
            "ledger_filter": ("transaction_ledger WHERE source='level_claim' "
                              "AND type='credit' AND guild_id/user_id = this member"),
            "wallet_now": report.get("wallet"),
            "currency": attribution.get("currency") or [],
            "inventory": attribution.get("inventory") or [],
            "legacy_backfill": attribution.get("legacy_backfill") or [],
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
                       "same quantity. Rewards delivered since reward_grants "
                       "exist are attributable per grant; anything older is not, "
                       "and the stack total is never treated as proof."},
            {"table": "economy",
             "reason": "coins and diamonds are fungible. The ledger proves what a "
                       "level reward credited, but once it is spent the wallet "
                       "cannot tell it apart from any other money, so a reversal "
                       "is refused rather than forced."},
            {"table": "transaction_ledger",
             "reason": "append-only audit history; rows are marked reversed and "
                       "compensated, never rewritten or deleted."},
            {"table": "reward_grants (historical)",
             "reason": "grants recorded before this schema existed have no "
                       "remaining_quantity, so how many of a reward's units "
                       "survive consumption cannot be established. Those cases are "
                       "listed as refused and resolved through /level_reconcile."},
        ],
    }


async def reset_member(guild_id: int, user_id: int, *, apply: bool = False,
                       include_prestige: bool = False,
                       allow_production: bool = False,
                       reversed_by: int | None = None,
                       reason: str | None = None,
                       operation_id: str | None = None,
                       member=None) -> dict:
    """Preview (default) or perform the scoped reset. Returns the full report.

    ``apply=False`` never writes - it returns `inspect_member()` plus the plan.
    ``apply=True`` takes the member's operation lease, removes Discord roles,
    reverses the delivered value it can PROVE through the journal, then deletes
    the records whose effects are gone in one BEGIN IMMEDIATE transaction, and
    finally reports what ran, what could not, and what is still outstanding.

    ``include_prestige`` zeroes the permanent Prestige tier and deletes the
    booster VI activation. Permanent tiers are Shop purchases and are NOT
    refunded, so this is opt-in - and prestige is not a level reward, so leaving
    it alone never makes the reset incomplete.

    ``reversed_by`` is stamped on the ledger reversal for the audit trail.
    ``operation_id`` lets a caller tie this run to an operation it already opened
    (a rollback that also resets); one is created otherwise.

    There is no override for a claim whose effect survived. `/level_reconcile`
    resolves those one at a time, on the record.
    """
    guild_id, user_id = _check_scope(guild_id, user_id)
    await ops.ensure_tables()
    operation_id = operation_id or ops.new_operation_id(ops.OP_RESET)

    if apply:
        # Decided from the database file actually open, not from the label.
        # Re-raised as LevelResetError so a caller can tell a REFUSAL (nothing was
        # written, say so plainly) from a FAILURE partway through (some steps may
        # have committed, say that instead). Conflating them would tell an
        # administrator that data may have moved when it did not.
        try:
            ops.require_destructive_allowed(
                production_authorization=allow_production,
                operation="/level_test_reset")
        except ops.LevelOpsError as exc:
            raise LevelResetError(str(exc)) from exc

    report = await inspect_member(guild_id, user_id,
                                  include_prestige=include_prestige)
    report["operation_id"] = operation_id
    report["applied"] = False
    report["roles_removed"] = []
    report["role_failures"] = []
    report["roles_deferred"] = []
    report["claims_preserved"] = []
    report["reversed_currency"] = []
    report["reversed_inventory"] = []
    report["unreversed"] = []

    if not apply:
        report["mode"] = "preview"
        report["reconciliation"] = _reconciliation(report)
        report["traces"] = _traces(report, applied=False)
        report["notes"] = _notes(report, include_prestige)
        report["new_game_complete"] = False
        return report

    # A concurrent operation on this member (another reset, a rollback, or a
    # claim pass that has not finished) means the plan above is already stale.
    lock = await ops.acquire_lock(guild_id, user_id, holder="level_test_reset",
                                  operation_id=operation_id)
    if not lock["ok"]:
        raise LevelResetError(
            f"another operation is running for this member: {lock['reason']}. "
            f"Nothing was changed - wait for it to finish or for its lease to "
            f"expire, then run the preview again.")
    try:
        await ops.open_operation(operation_id, ops.OP_RESET, guild_id, user_id,
                                 actor_id=reversed_by, reason=reason)
        # Re-inspect under the lease: the preview the admin read may predate it.
        report = await inspect_member(guild_id, user_id,
                                      include_prestige=include_prestige)
        report.update({"operation_id": operation_id, "applied": False,
                       "roles_removed": [], "role_failures": [],
                       "roles_deferred": [], "claims_preserved": [],
                       "reversed_currency": [], "reversed_inventory": [],
                       "unreversed": []})
        plan = report["plan"]
        report["roles_left_on_member"] = plan["roles_left_on_member"]
        report["mode"] = "apply"
        inspected_claim_ids = {c["id"] for c in report["claims"]}
        # A claim reserved by a claim pass whose Discord call is still running has
        # an effect this report cannot see: the role may be added AFTER this reset
        # finishes. It is inspected as `processing`, but the deletion below has no
        # status filter, so without this it would be erased and the finish would
        # leave the member wearing a role with no record - while the reset reported
        # a clean New Game. The lease rules out a NEW reservation, not a delivery
        # already in flight, so keep these rows exactly like any other effect that
        # cannot be proven gone and let the verdict say the reset is not complete.
        in_flight = [c for c in report["claims"]
                     if str(c.get("status") or "") == "processing"]
        report["in_flight_claims"] = [
            {"id": c["id"], "reward_level": c["reward_level"],
             "track": c["track"], "role_id": c.get("role_id")}
            for c in in_flight]
        in_flight_ids = {c["id"] for c in in_flight}

        # ── 1. Discord roles, while the claim rows that name them still exist ──
        # Best effort and reported per role: a failure keeps that claim row (and
        # its temp_roles row) alive below, so the member is never left wearing a
        # reward role with nothing describing why.
        for role_id in plan["remove_discord_roles"]:
            claim = next((c for c in report["claims"]
                          if c.get("role_id") == role_id), None)
            try:
                if member is None:
                    raise LevelResetError(
                        "no member object supplied - the role cannot be taken "
                        "off, so its claim record is preserved")
                role = getattr(getattr(member, "guild", None), "get_role",
                               lambda _id: None)(role_id)
                if role is None:
                    raise LevelResetError(f"role {role_id} not found in the guild")
                if role not in (getattr(member, "roles", None) or []):
                    if claim is not None and claim["id"] in in_flight_ids:
                        # The delivery that owns this claim may still add the
                        # role after this reset finishes. Nothing was removed,
                        # so nothing may be recorded as removed: writing
                        # `applied` here would be a journal row the Discord
                        # state can contradict moments later, and the next
                        # correction would trust it. The claim is already kept
                        # and reported, so the verdict stays incomplete and the
                        # decision is left to the delivery.
                        report["roles_deferred"].append(
                            {"role_id": role_id, "claim_id": claim["id"],
                             "reason": "its claim is still being delivered, so "
                                       "the role may be added after this reset; "
                                       "nothing was removed"})
                        continue
                    report["roles_removed"].append(role_id)   # already absent
                else:
                    await member.remove_roles(role, reason="level test reset")
                    report["roles_removed"].append(role_id)
                if claim is not None:
                    await ops.note_reversal(
                        guild_id=guild_id, user_id=user_id,
                        claim_id=claim["id"], kind=ops.KIND_ROLE, quantity=1,
                        operation_id=operation_id, status=ops.STATUS_APPLIED,
                        role_id=role_id,
                        reason="reward role removed from the member")
            except Exception as exc:
                report["role_failures"].append({"role_id": role_id,
                                                "error": str(exc)})

        # ── 2. currency, atomically: wallet, marker, one compensating row and
        #         the journal entry commit together ──
        for entry in plan["reverse_currency"]:
            outcome = await ops.reverse_currency(
                guild_id=guild_id, user_id=user_id,
                ledger_id=entry["ledger_id"], amount=entry["amount"],
                currency=entry["currency"], claim_id=entry["claim_id"],
                operation_id=operation_id, reversed_by=reversed_by,
                reason=RESET_REASON)
            outcome = dict(outcome, **{k: entry[k] for k in
                                       ("claim_id", "reward_level", "currency",
                                        "amount", "ledger_id", "provenance")})
            if outcome["status"] == ops.STATUS_APPLIED:
                report["reversed_currency"].append(outcome)
            elif outcome["status"] == ops.STATUS_ALREADY:
                report["reversed_currency"].append(dict(outcome, new_balance=None))
            else:
                report["unreversed"].append(dict(outcome, kind="currency"))

        # ── 3. inventory, only where provenance proves the units are this
        #         reward's, and only once per claim ──
        for entry in plan["reverse_inventory"]:
            outcome = await ops.reverse_inventory(
                guild_id=guild_id, user_id=user_id,
                claim_id=entry["claim_id"], item_name=entry["item_name"],
                quantity=entry["quantity"], operation_id=operation_id,
                reason=RESET_REASON)
            outcome = dict(outcome, **{k: entry[k] for k in
                                       ("claim_id", "reward_level", "item_name",
                                        "quantity", "provenance")})
            if outcome["status"] == ops.STATUS_APPLIED:
                report["reversed_inventory"].append(outcome)
            elif outcome["status"] == ops.STATUS_ALREADY:
                report["reversed_inventory"].append(outcome)
            else:
                report["unreversed"].append(dict(outcome, kind="inventory"))

        # Anything the plan already knew could not be reversed is reported too,
        # and recorded, so the case is on the queue rather than only in a message.
        known = {(e["claim_id"], e["kind"]) for e in report["unreversed"]}
        for entry in plan["cannot_reverse"]:
            if (entry["claim_id"], entry["kind"]) in known:
                continue
            recorded = await ops.note_reversal(
                guild_id=guild_id, user_id=user_id, claim_id=entry["claim_id"],
                kind=entry["kind"], quantity=int(
                    entry.get("amount") or entry.get("quantity") or 0),
                operation_id=operation_id,
                status=(ops.STATUS_OUTSTANDING
                        if "spent" in (entry.get("note") or "")
                        or "attributable" in (entry.get("note") or "")
                        else ops.STATUS_REFUSED),
                reason=entry.get("note"),
                currency=entry.get("currency"),
                item_name=entry.get("item_name"))
            report["unreversed"].append(dict(
                entry, error=entry.get("note") or "", recorded=recorded))

        # ── 4. the records, in one transaction ──
        failed_roles = {int(f["role_id"]) for f in report["role_failures"]}
        # A temp-role unit of an in-flight delivery is the same kind of live
        # effect: the add may just have succeeded and recorded its unit while the
        # claim is still `processing`. Keeping the claim row but deleting its unit
        # would leave a role that never expires.
        in_flight_roles = {int(c["role_id"]) for c in in_flight
                           if c.get("role_id") is not None}
        role_preserved = [c for c in report["claims"]
                          if c.get("role_id") in failed_roles]
        kept_ids = {c["id"] for c in role_preserved}
        kept_ids |= {e["claim_id"] for e in report["unreversed"]}
        kept_ids |= in_flight_ids

        # Defence behind the lease: never delete a fulfilled row this run did not
        # inspect. A reward delivered by a writer that ignored the lease would
        # otherwise be erased with its value still in the member's hands.
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            cursor = await db.execute(
                "SELECT id, reward_level, track, status FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            live = await cursor.fetchall()
        concurrent = [{"id": r[0], "reward_level": r[1], "track": r[2],
                       "status": r[3]} for r in live
                      if r[3] == "fulfilled" and r[0] not in inspected_claim_ids]
        kept_ids |= {c["id"] for c in concurrent}
        report["concurrent_claims"] = concurrent

        preserved = [c for c in report["claims"] if c["id"] in kept_ids]
        report["claims_preserved"] = [
            {"id": c["id"], "reward_level": c["reward_level"], "track": c["track"],
             "role_id": c.get("role_id"),
             "reason": ("its delivery is still in progress, so its effect "
                        "cannot be known yet - the claim row is kept and the "
                        "reset is NOT complete"
                        if c["id"] in in_flight_ids
                        else "a Discord role could not be removed"
                        if c.get("role_id") in failed_roles
                        else "its delivered effect could not be reversed")}
            for c in preserved]
        report["claims_preserved"] += [
            {"id": c["id"], "reward_level": c["reward_level"], "track": c["track"],
             "role_id": None,
             "reason": "it was delivered by a concurrent operation while this "
                       "reset was running, so its effect was never inspected"}
            for c in concurrent]

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
                # Temp-role units, EXCEPT for a role still on the member: keeping
                # the row lets the temp-role expiry task retry the removal.
                sql = ("DELETE FROM temp_roles "
                       "WHERE guild_id=? AND user_id=? AND source=?")
                args = [guild_id, user_id, CLAIM_SOURCE]
                keep_roles = failed_roles | in_flight_roles
                if keep_roles:
                    sql += (f" AND role_id NOT IN "
                            f"({','.join('?' * len(keep_roles))})")
                    args += sorted(keep_roles)
                cursor = await db.execute(sql, args)
                counts["temp_roles"] = cursor.rowcount
                # The entitlement ledger - what makes the next crossing earnable
                # again, which is the point of the tool - minus every row whose
                # effect is still live. record_crossing() is INSERT OR IGNORE on
                # the reward identity, so a surviving row is also what stops a
                # re-levelled member being paid a second time.
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
                {"id": c["id"], "reward_level": c["reward_level"],
                 "track": c["track"], "status": c["status"]}
                for c in after["claims"]],
            "outstanding": after["outstanding"],
        }
        # The journal and the open-case queue are read from the re-inspection: the
        # rows this run just wrote are the ones an administrator has to see, and
        # the command embed reads them at the top level of the report.
        report["journal"] = after["journal"]
        report["outstanding"] = after["outstanding"]
        report["reconciliation"] = _reconciliation(after)
        # The notes describe what is there NOW, so they read the re-inspection
        # rather than the snapshot taken before the reversals: quoting a
        # pre-reset stack quantity as "left alone" would misreport the state an
        # administrator is about to rely on.
        report["inventory_candidates"] = after["inventory_candidates"]
        report["wallet"] = after["wallet"]
        report["ran"] = _ran(report, counts, include_prestige)
        report["could_not"] = _could_not(report)
        report["traces"] = _traces(report, applied=True)
        report["notes"] = _notes(report, include_prestige)
        # The verdict is about LEVEL REWARD traces only: prestige and any value the
        # member holds from other systems are notes, not failures.
        report["new_game_complete"] = bool(report["applied"]) and not report["traces"]
        await ops.finish_operation(
            operation_id,
            "completed" if report["new_game_complete"] else "partial",
            {"reversed_currency": len(report["reversed_currency"]),
             "reversed_inventory": len(report["reversed_inventory"]),
             "unreversed": len(report["unreversed"]),
             "claims_deleted": counts.get("claims", 0),
             "claims_preserved": len(report["claims_preserved"]),
             "traces": report["traces"]})
        return report
    except Exception as exc:
        try:
            await ops.finish_operation(operation_id, "failed",
                                       {"error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        raise
    finally:
        await ops.release_lock(guild_id, user_id, operation_id)


ROLLBACK_REASON = "level reward rollback"
# A rollback cancels an entitlement only when the member no longer reaches the
# reward level on the XP they keep. Anything still reached stays claimable and
# stays delivered - taking that away would remove a reward the member legitimately
# earned, which is the one thing this tool must never do.
_CLAIMABLE = ("pending", "failed")


async def rollback_xp_operation(guild_id: int, user_id: int, *,
                                operation_id: str, apply: bool = False,
                                reversed_by: int | None = None,
                                reason: str | None = None,
                                member=None) -> dict:
    """Undo ONE recorded administrative XP change, and only what it caused.

    This is NOT a snapshot restore. Restoring `xp_before` would erase every point
    the member earned between the mistake and now, so the rollback subtracts the
    change's own delta from the member's CURRENT XP:

        target_xp = max(0, current_xp - mutation.delta)

    Later progress survives by construction, and if the member's XP has since
    fallen below the delta the result clamps at 0 instead of going negative or
    reaching back for the old snapshot.

    What it removes is decided by provenance, in this order:

    * entitlements (claim rows) that carry THIS operation's id and sit ABOVE the
      target level - the member no longer reaches them, and they exist only
      because of the change being undone. Unclaimed ones are cancelled. Delivered
      ones are reversed through the same journal-backed primitives the reset uses,
      and the row is deleted only when the reversal actually applied.
    * entitlements at or below the target level are LEFT ALONE even if this
      operation created them: the member still reaches that level on their own
      remaining XP, so the reward is legitimately theirs.
    * entitlements that do NOT carry this operation's id are left alone whatever
      their level. They were earned by ordinary play or by another change, and a
      level number is not proof of who created them. When the operation's
      `crossings` cannot be matched to rows this way (every change made before the
      operation_id column existed), nothing is cancelled: the cases are listed for
      `/level_reconcile` instead of being guessed at.

    Prestige, the economy outside level rewards, other members and reward
    definitions are never touched, exactly as in `reset_member()`.
    """
    from utils.level_claims import _role_granted_elsewhere, progress_level
    guild_id, user_id = _check_scope(guild_id, user_id)
    await ops.ensure_tables()

    mutation = await ops.get_xp_mutation(operation_id)
    if mutation is None:
        raise LevelResetError(
            f"no recorded XP operation `{operation_id}`. /xp_rollback works from "
            f"the durable rows in `xp_mutations`; run it with no operation to "
            f"list this member's recorded changes.")
    if (int(mutation["guild_id"]) != guild_id
            or int(mutation["user_id"]) != user_id):
        raise LevelResetError(
            f"operation `{operation_id}` belongs to guild "
            f"{mutation['guild_id']} / member {mutation['user_id']}, not to this "
            f"member in this guild - refused: a rollback is scoped to one member.")
    if mutation["reverses_operation_id"]:
        raise LevelResetError(
            f"`{operation_id}` is itself a rollback of "
            f"`{mutation['reverses_operation_id']}` - roll that change back "
            f"instead, so the audit trail stays one level deep.")
    if mutation["rolled_back"]:
        raise LevelResetError(
            f"operation `{operation_id}` is already rolled back. Nothing is "
            f"undone twice: the first rollback's own row records what happened. "
            f"If that rollback reported a reward role it could not remove, the "
            f"claim row and its open case are listed by `/level_reconcile` for "
            f"this member - remove the role in Discord, then resolve the case "
            f"with `resolution: effect_gone drop_claim: True`. Re-running this "
            f"rollback is refused so the XP delta is never subtracted twice.")
    delta = int(mutation["delta"])
    if delta <= 0:
        raise LevelResetError(
            f"operation `{operation_id}` did not raise XP (delta {delta}), so "
            f"rolling it back would GRANT {abs(delta)} XP and could re-open "
            f"rewards that were correctly taken away. Use /setxp for a deliberate "
            f"grant - it previews what it would entitle.")

    report = await inspect_member(guild_id, user_id)
    current_xp = int(report["xp"])
    earned_since = current_xp - int(mutation["xp_after"])
    clamped = current_xp < delta
    target_xp = max(0, current_xp - delta)
    target_level = progress_level(target_xp)

    def brief(claim: dict) -> dict:
        return {"claim_id": claim["id"], "reward_level": claim["reward_level"],
                "track": claim["track"], "reward_ref": claim["reward_ref"],
                "status": claim["status"], "role_id": claim.get("role_id"),
                "legacy_backfill": bool(claim.get("legacy_backfill"))}

    caused_ids: set[int] = set()
    for claim in report["claims"]:
        if str(claim.get("operation_id") or "") == str(operation_id):
            caused_ids.add(claim["id"])
    caused = [c for c in report["claims"] if c["id"] in caused_ids]
    above = [c for c in caused if int(c["reward_level"]) > target_level]
    cancel = [c for c in above if c["status"] in _CLAIMABLE]
    delivered = [c for c in above if c["status"] == "fulfilled"]
    # A claim this operation created whose delivery was still in progress when the
    # rollback inspected it. Its effect (a Discord role add, an inventory grant)
    # cannot be known yet, so it is neither cancelled nor reversed: the row is
    # kept and the operation is not called complete. Reporting it here - rather
    # than leaving it out of every list because it is neither `pending` nor
    # `fulfilled` - is what stops a later delivery from appearing as a clean
    # rollback after it already paid a reward above the target level.
    in_flight = [c for c in above if c["status"] == "processing"]
    still_earned = [c for c in caused if int(c["reward_level"]) <= target_level]
    independent = [c for c in report["claims"] if c["id"] not in caused_ids]

    # ── attribute ONLY the delivered rewards this rollback may take back ──
    # The same one-to-one matcher the reset uses, over a filtered claim list and
    # the ledger rows naming those reward levels, so a credit belonging to another
    # reward can never be consumed by this one.
    attribution = {"currency": [], "inventory": [], "legacy_backfill": []}
    if delivered:
        wanted = {f"Level {c['reward_level']} reward" for c in delivered}
        subset = dict(report, claims=delivered)
        attribution = _attribute(
            subset, [r for r in report.get("ledger") or []
                     if r.get("reason") in wanted])
    cannot = ([dict(e, kind=ops.KIND_CURRENCY)
               for e in attribution["currency"] if not e.get("reversible")]
              + [dict(e, kind=ops.KIND_INVENTORY)
                 for e in attribution["inventory"] if not e.get("reversible")])
    # A legacy-backfill row says the bot never paid it, so there is no value to
    # reverse; it is cancelled like an unclaimed entitlement and reported as such.
    legacy_cancel = [e for e in attribution["legacy_backfill"]]

    unattributable = []
    if mutation["crossings"] and not caused:
        unattributable = [
            dict(brief(c), note="this operation's recorded crossings "
                                f"({', '.join(str(x) for x in mutation['crossings'])}) "
                                "cannot be matched to claim rows, because the rows "
                                "predate the operation_id column; they are NOT "
                                "cancelled automatically")
            for c in independent if int(c["reward_level"]) > target_level]

    # Roles this rollback may take off, and roles it must leave on because another
    # recorded entitlement still grants them. This reuses the SAME predicate the
    # correction engine uses (`utils.level_claims._role_granted_elsewhere`) rather
    # than a second, weaker check: it names another fulfilled claim, a
    # `temp_roles` row from a different source, or an inventory role marker. A
    # role kept for that reason is deliberately retained, not failed.
    role_claims: dict[int, dict] = {}
    for claim in delivered:
        if claim.get("role_id") and not claim.get("legacy_backfill"):
            role_claims.setdefault(int(claim["role_id"]), claim)
    remove_discord_roles: list[int] = []
    retained_roles: list[dict] = []
    if role_claims:
        # Every delivered claim for this role that THIS rollback is about to
        # reverse, not just the one representative picked above. Another claim
        # for the same role created by the same operation is not an independent
        # entitlement: counting it as one made two rows justify each other, so
        # the role stayed on the member and both rows survived while the XP was
        # rolled back - a reward above the target level with no independent
        # source. `_role_granted_elsewhere` may only name a grant that this run
        # leaves in place.
        being_reversed: dict[int, set[int]] = {}
        for claim in delivered:
            if claim.get("role_id") is None:
                continue
            being_reversed.setdefault(int(claim["role_id"]), set()).add(
                int(claim["id"]))
        async with aiosqlite.connect(DB_PATH, timeout=10) as grant_db:
            for role_id, claim in sorted(role_claims.items()):
                shared = await _role_granted_elsewhere(
                    grant_db, guild_id, user_id, role_id,
                    exclude_claim_id=int(claim["id"]),
                    exclude_claim_ids=being_reversed.get(role_id, set()),
                    exclude_item=(claim["payload"].get("name")
                                  if claim["track"] == "shop" else None))
                if shared:
                    retained_roles.append({"role_id": role_id,
                                           "also_granted_by": [shared]})
                else:
                    remove_discord_roles.append(role_id)

    plan = {
        "target_xp": target_xp, "target_level": target_level,
        "removed_xp": current_xp - target_xp, "delta": delta,
        "earned_since": earned_since, "clamped": clamped,
        "cancel_claims": [brief(c) for c in cancel],
        "in_flight_claims": [brief(c) for c in in_flight],
        "legacy_cancel_claims": legacy_cancel,
        "reverse_currency": [e for e in attribution["currency"]
                             if e.get("reversible")],
        "reverse_inventory": [e for e in attribution["inventory"]
                              if e.get("reversible")],
        "cannot_reverse": cannot,
        "kept_still_earned": [brief(c) for c in still_earned],
        "kept_independent": [brief(c) for c in independent],
        "unattributable": unattributable,
        "remove_discord_roles": remove_discord_roles,
        "roles_left_on_member": retained_roles,
    }
    report.update({
        "mode": "preview" if not apply else "apply",
        "kind": "rollback", "mutation": mutation, "plan": plan,
        "applied": False, "operation_id": None,
        "roles_removed": [], "role_failures": [], "claims_preserved": [],
        "roles_left_on_member": list(retained_roles),
        "reversed_currency": [], "reversed_inventory": [], "unreversed": [],
        "cancelled_claims": [], "deleted": {},
    })
    if not apply:
        report["reconciliation"] = _reconciliation(report)
        report["notes"] = _notes(report, False)
        return report

    # ── apply ────────────────────────────────────────────────────────────────
    # A rollback is a supported production operation: undoing an administrative
    # mistake is exactly what production needs it for, and refusing it there
    # would leave /setxp - which cannot cancel an entitlement - as the only tool.
    # What makes it safe is not an environment flag but the scope: one recorded
    # operation, one member, confirm plus a reason, the per-member lease, and
    # value only ever moved through provenance.
    rollback_operation_id = ops.new_operation_id(ops.OP_ROLLBACK)
    lock = await ops.acquire_lock(guild_id, user_id, holder="xp_rollback",
                                  operation_id=rollback_operation_id)
    if not lock["ok"]:
        raise LevelResetError(
            f"another operation is running for this member: {lock['reason']}. "
            f"Nothing was changed - wait for it to finish or for its lease to "
            f"expire, then run the preview again.")
    report["operation_id"] = rollback_operation_id
    try:
        await ops.open_operation(rollback_operation_id, ops.OP_ROLLBACK, guild_id,
                                 user_id, actor_id=reversed_by,
                                 reason=f"rolls back {operation_id}: {reason or ''}")
        # Re-read under the lease and rebuild the plan from THAT state, so every
        # decision below is made from the rows this operation is allowed to
        # change. The preview the admin read may predate the lease.
        preview = await rollback_xp_operation(
            guild_id, user_id, operation_id=operation_id, apply=False)
        report = dict(preview, mode="apply", kind="rollback", applied=False,
                      operation_id=rollback_operation_id, roles_removed=[],
                      role_failures=[], claims_preserved=[],
                      reversed_currency=[], reversed_inventory=[],
                      unreversed=[], cancelled_claims=[], deleted={})
        plan = report["plan"]
        current_xp = int(report["xp"])
        caused_ids = {c["id"] for c in report["claims"]
                      if str(c.get("operation_id") or "") == str(operation_id)}

        # ── 1. Discord roles first, while the claim rows naming them exist ──
        # `plan["remove_discord_roles"]` was built by the preview immediately
        # above, after `_role_granted_elsewhere` found no other entitlement for
        # each role. Roles that predicate retained are not in it and stay on the
        # member, reported under `roles_left_on_member`.
        failed_roles: set[int] = set()
        removed_role_claim_ids: set[int] = set()
        for role_id in plan["remove_discord_roles"]:
            group = [c for c in report["claims"]
                     if c.get("role_id") is not None
                     and int(c["role_id"]) == int(role_id)
                     and c.get("status") == "fulfilled"
                     and c["id"] in caused_ids
                     and not c.get("legacy_backfill")]
            try:
                if member is None:
                    raise LevelResetError(
                        "no member object supplied - the role cannot be taken "
                        "off, so its claim record is preserved")
                role = getattr(getattr(member, "guild", None), "get_role",
                               lambda _id: None)(role_id)
                if role is None:
                    raise LevelResetError(f"role {role_id} not found in the guild")
                if role not in (getattr(member, "roles", None) or []):
                    report["roles_removed"].append(role_id)      # already absent
                else:
                    await member.remove_roles(
                        role, reason=f"xp rollback {operation_id}")
                    report["roles_removed"].append(role_id)
                # One journal row per claim whose role effect is now gone. These
                # claims join `delete_ids` below: leaving a fulfilled role claim
                # behind would let a rejoin's `reconcile_role_progression()`
                # restore the role the rollback just removed.
                for claim in group:
                    await ops.note_reversal(
                        guild_id=guild_id, user_id=user_id,
                        claim_id=claim["id"], kind=ops.KIND_ROLE, quantity=1,
                        operation_id=rollback_operation_id,
                        status=ops.STATUS_APPLIED, role_id=role_id,
                        reason=f"reward role removed by the rollback of "
                               f"{operation_id}")
                    removed_role_claim_ids.add(int(claim["id"]))
            except Exception as exc:
                failed_roles.add(int(role_id))
                # A failed Discord removal is a REAL open case, not a note in a
                # transient reply. The role is still on the member and its claim
                # is kept, so `reward_reversals` must remember that the effect
                # was not reversed: `/level_reconcile` lists exactly those rows,
                # and the operation below is recorded `partial` instead of
                # `completed`. A re-run of this rollback cannot retry the
                # removal (the XP delta must never be subtracted twice); the
                # supported path is: take the role off in Discord, then resolve
                # the open case with `effect_gone` + `drop_claim: True`.
                for claim in group:
                    await ops.note_reversal(
                        guild_id=guild_id, user_id=user_id,
                        claim_id=claim["id"], kind=ops.KIND_ROLE, quantity=1,
                        operation_id=rollback_operation_id,
                        status=ops.STATUS_OUTSTANDING, role_id=role_id,
                        reason=(f"Discord refused the rollback's role removal: "
                                f"{exc}. The role is still on the member and the "
                                f"claim row is kept; resolve this case with "
                                f"/level_reconcile after removing the role."))
                    report["unreversed"].append({
                        "claim_id": claim["id"],
                        "reward_level": claim["reward_level"],
                        "kind": ops.KIND_ROLE, "role_id": role_id,
                        "status": ops.STATUS_OUTSTANDING,
                        "reason": f"Discord refused the role removal: {exc}"})
                report["role_failures"].append({
                    "role_id": role_id, "error": str(exc),
                    "claim_ids": [c["id"] for c in group],
                    "open_case": True})

        # ── 2. delivered value, through the same journal-backed primitives ──
        for entry in plan["reverse_currency"]:
            outcome = await ops.reverse_currency(
                guild_id=guild_id, user_id=user_id,
                ledger_id=entry["ledger_id"], amount=entry["amount"],
                currency=entry["currency"], claim_id=entry["claim_id"],
                operation_id=rollback_operation_id, reversed_by=reversed_by,
                reason=ROLLBACK_REASON)
            outcome = dict(outcome, **{k: entry[k] for k in
                                       ("claim_id", "reward_level", "currency",
                                        "amount", "ledger_id", "provenance")})
            if outcome["status"] in (ops.STATUS_APPLIED, ops.STATUS_ALREADY):
                report["reversed_currency"].append(outcome)
            else:
                report["unreversed"].append(dict(outcome, kind="currency"))
        for entry in plan["reverse_inventory"]:
            outcome = await ops.reverse_inventory(
                guild_id=guild_id, user_id=user_id,
                claim_id=entry["claim_id"], item_name=entry["item_name"],
                quantity=entry["quantity"],
                operation_id=rollback_operation_id, reason=ROLLBACK_REASON)
            outcome = dict(outcome, **{k: entry[k] for k in
                                       ("claim_id", "reward_level", "item_name",
                                        "quantity", "provenance")})
            if outcome["status"] in (ops.STATUS_APPLIED, ops.STATUS_ALREADY):
                report["reversed_inventory"].append(outcome)
            else:
                report["unreversed"].append(dict(outcome, kind="inventory"))
        known = {(e["claim_id"], e["kind"]) for e in report["unreversed"]}
        for entry in plan["cannot_reverse"]:
            if (entry["claim_id"], entry["kind"]) in known:
                continue
            recorded = await ops.note_reversal(
                guild_id=guild_id, user_id=user_id, claim_id=entry["claim_id"],
                kind=entry["kind"],
                quantity=int(entry.get("amount") or entry.get("quantity") or 0),
                operation_id=rollback_operation_id,
                status=(ops.STATUS_OUTSTANDING
                        if "spent" in (entry.get("note") or "")
                        or "attributable" in (entry.get("note") or "")
                        else ops.STATUS_REFUSED),
                reason=entry.get("note"), currency=entry.get("currency"),
                item_name=entry.get("item_name"))
            report["unreversed"].append(dict(
                entry, error=entry.get("note") or "", recorded=recorded))

        # ── 3. XP, the entitlements whose effects are gone, and the audit mark,
        #         in ONE transaction ──
        # Splitting the XP write from the claim deletes would leave a member who
        # lost the rewards but kept the level (or the reverse) if the process died
        # in between.
        reversed_ok = ({e["claim_id"] for e in report["reversed_currency"]}
                       | {e["claim_id"] for e in report["reversed_inventory"]}
                       | {e["claim_id"] for e in plan["legacy_cancel_claims"]}
                       | removed_role_claim_ids)
        delete_ids = {c["claim_id"] for c in plan["cancel_claims"]}
        delete_ids |= {c["id"] for c in report["claims"]
                       if c["id"] in caused_ids and c["id"] in reversed_ok}
        # A role that could not be taken off keeps its claim row and its temp-role
        # unit, whatever else the rollback decided about that claim.
        failed_claim_ids = {c["id"] for c in report["claims"]
                            if c.get("role_id") in failed_roles}
        delete_ids -= failed_claim_ids
        retained_role_ids = {int(r["role_id"])
                             for r in plan.get("roles_left_on_member") or []}
        retained_claim_ids = {c["id"] for c in report["claims"]
                              if c.get("role_id") is not None
                              and int(c["role_id"]) in retained_role_ids
                              and c["id"] in caused_ids}
        in_flight_ids = {c["claim_id"]
                         for c in plan.get("in_flight_claims") or []}
        still_earned_ids = {int(c["id"]) for c in still_earned}
        report["in_flight_claims"] = list(plan.get("in_flight_claims") or [])
        report["claims_preserved"] = [
            dict(brief(c), reason=("its delivery was still in progress when the "
                                   "rollback ran, so its effect cannot be known "
                                   "yet - the claim row is kept and this "
                                   "rollback is NOT complete"
                                   if c["id"] in in_flight_ids
                                   else "a Discord role could not be removed"
                                   if c["id"] in failed_claim_ids
                                   else "the role is also granted by another "
                                        "entitlement, so it was deliberately "
                                        "kept"
                                   if c["id"] in retained_claim_ids
                                   else "the member still reaches this level, "
                                        "so the reward is legitimately theirs"
                                   if c["id"] in still_earned_ids
                                   else "its delivered effect could not be "
                                        "reversed, so it stays terminal"))
            for c in report["claims"]
            if c["id"] in caused_ids and c["id"] not in delete_ids]

        counts: dict = {}
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
                row = await cursor.fetchone()
                live_xp = int(row[0] or 0) if row else 0
                if live_xp != current_xp:
                    # XP moved since the inspection (a message award, a voice
                    # tick, another admin). Subtract the delta from the LIVE
                    # value so the member keeps what they earned in the meantime;
                    # never write the stale target over newer progress.
                    target_xp = max(0, live_xp - delta)
                    plan["target_xp"] = target_xp
                    plan["target_level"] = progress_level(target_xp)
                    plan["removed_xp"] = live_xp - target_xp
                    plan["xp_moved_during_rollback"] = {
                        "inspected": current_xp, "live": live_xp}
                cursor = await db.execute("""
                    UPDATE levels SET xp=?, level=? WHERE guild_id=? AND user_id=?
                """, (target_xp, plan["target_level"], guild_id, user_id))
                counts["levels"] = cursor.rowcount
                if delete_ids:
                    sql = ("DELETE FROM level_reward_claims WHERE guild_id=? "
                           "AND user_id=? AND status IN ('pending','failed') "
                           f"AND id IN ({','.join('?' * len(delete_ids))})")
                    args = [guild_id, user_id] + sorted(delete_ids)
                    cursor = await db.execute(sql, args)
                    counts["cancelled_claims"] = cursor.rowcount
                    # A delivered row is only deleted once its effect is gone.
                    sql = ("DELETE FROM level_reward_claims WHERE guild_id=? "
                           "AND user_id=? AND status='fulfilled' "
                           f"AND id IN ({','.join('?' * len(delete_ids))})")
                    cursor = await db.execute(sql, args)
                    counts["reversed_claims"] = cursor.rowcount
                    sql = ("DELETE FROM leveling_active_boosts WHERE guild_id=? "
                           "AND user_id=? AND source LIKE ?")
                    for claim_id in sorted(delete_ids):
                        cursor = await db.execute(
                            sql, (guild_id, user_id,
                                  f"{BOOST_SOURCE_PREFIX}{claim_id}"))
                        counts["boosts"] = counts.get("boosts", 0) + cursor.rowcount
                    # Temp-role units belonging to the claims being removed. A
                    # role whose removal failed keeps its unit row, so the expiry
                    # task can retry the removal on its own schedule and the
                    # member is never left wearing an undocumented reward role.
                    sql = ("DELETE FROM temp_roles WHERE guild_id=? AND user_id=? "
                           "AND source=?"
                           + (f" AND role_id NOT IN "
                              f"({','.join('?' * len(failed_roles))})"
                              if failed_roles else "")
                           + f" AND claim_id IN "
                             f"({','.join('?' * len(delete_ids))})")
                    args = ([guild_id, user_id, CLAIM_SOURCE]
                            + sorted(failed_roles) + sorted(delete_ids))
                    try:
                        cursor = await db.execute(sql, args)
                        counts["temp_roles"] = cursor.rowcount
                    except aiosqlite.Error:
                        # `claim_id` is an added column; on a database that never
                        # got the migration, fall back to the role ids this
                        # rollback is removing - never to the source alone, which
                        # would take units belonging to other claims.
                        role_ids = sorted(
                            {int(c["role_id"]) for c in report["claims"]
                             if c["id"] in delete_ids and c.get("role_id")}
                            - failed_roles)
                        counts["temp_roles"] = 0
                        if role_ids:
                            cursor = await db.execute(
                                "DELETE FROM temp_roles WHERE guild_id=? "
                                "AND user_id=? AND source=? AND role_id IN "
                                f"({','.join('?' * len(role_ids))})",
                                [guild_id, user_id, CLAIM_SOURCE] + role_ids)
                            counts["temp_roles"] = cursor.rowcount
                await ops.mark_rolled_back(operation_id, db=db)
                await ops.record_xp_mutation(
                    guild_id=guild_id, user_id=user_id, source=ops.OP_ROLLBACK,
                    actor_id=reversed_by, reason=reason,
                    xp_before=current_xp, xp_after=target_xp,
                    level_before=report["level"],
                    level_after=plan["target_level"],
                    reverses_operation_id=operation_id,
                    operation_id=rollback_operation_id, db=db)
                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

        report["applied"] = True
        report["deleted"] = counts
        report["cancelled_claims"] = [c for c in plan["cancel_claims"]
                                      if c["claim_id"] in delete_ids]
        after = await inspect_member(guild_id, user_id)
        report["after"] = {"xp": after["xp"], "level": after["level"],
                           "claims": len(after["claims"]),
                           "wallet": after["wallet"]}
        report["outstanding"] = await ops.outstanding_reversals(guild_id, user_id)
        report["journal"] = await ops.reversal_rows(guild_id, user_id)
        await ops.finish_operation(
            rollback_operation_id,
            # A role the rollback could not take off is an outstanding reversal
            # even if the role's claims could not be journalled (no member object,
            # or a role that disappeared from the guild). Never record
            # `completed` while `role_failures` says otherwise - and a delivery
            # that was still in flight when the rollback ran is the same kind of
            # open case: its effect may land above the target level afterwards.
            "completed" if not report["unreversed"]
            and not report.get("role_failures")
            and not report.get("in_flight_claims") else "partial",
            {"rolled_back": operation_id, "target_xp": target_xp,
             "cancelled": counts.get("cancelled_claims", 0),
             "reversed_claims": counts.get("reversed_claims", 0),
             "reversed_currency": len(report["reversed_currency"]),
             "reversed_inventory": len(report["reversed_inventory"]),
             "unreversed": len(report["unreversed"]),
             "in_flight": len(report.get("in_flight_claims") or []),
             "role_failures": len(report.get("role_failures") or []),
             "open_role_cases": sum(
                 1 for f in report.get("role_failures") or [] if f.get("open_case"))})
        return report
    except Exception as exc:
        try:
            await ops.finish_operation(
                rollback_operation_id, "failed",
                {"error": f"{type(exc).__name__}: {exc}",
                 "rolled_back": operation_id})
        except Exception:
            pass
        raise
    finally:
        await ops.release_lock(guild_id, user_id, rollback_operation_id)


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
        if entry.get("status") == ops.STATUS_ALREADY:
            ran.append(f"L{entry['reward_level']} {entry['amount']} "
                       f"{entry['currency']} was already reversed - no money "
                       f"moved again")
            continue
        ran.append(
            f"reversed {entry['amount']} {entry['currency']} from L"
            f"{entry['reward_level']} (ledger {entry['ledger_id']} -> "
            f"{entry.get('new_balance')}, one compensating row)")
    for entry in report["reversed_inventory"]:
        if entry.get("status") == ops.STATUS_ALREADY:
            ran.append(f"L{entry['reward_level']} {entry['quantity']}x "
                       f"{entry['item_name']} was already reversed - no items "
                       f"moved again")
            continue
        ran.append(f"removed {entry['quantity']}x {entry['item_name']} from L"
                   f"{entry['reward_level']} ({entry.get('remaining')}x left in "
                   f"the stack, provenance {entry.get('provenance')})")
    if report["roles_removed"]:
        ran.append(f"removed Discord role(s) {report['roles_removed']}")
    ran.append(f"deleted level_reward_claims rows ({counts.get('claims', 0)})")
    legacy = (report.get("plan") or {}).get("legacy_backfill_claims") or []
    if legacy:
        ran.append(
            f"of those, {len(legacy)} were legacy backfill row(s) the migration "
            f"marked fulfilled WITHOUT paying (source='legacy_backfill'), so "
            f"nothing was reversed for them and no value moved")
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
        what = (f"{entry.get('amount')} {entry.get('currency')}"
                if entry.get("kind") == "currency"
                else f"{entry.get('quantity')}x {entry.get('item_name')}")
        could_not.append(
            f"L{entry['reward_level']} {entry.get('kind')} reward ({what}) was "
            f"NOT reversed [{entry.get('status')}]: "
            f"{entry.get('error') or entry.get('note') or entry.get('reason')}. "
            f"Its claim record was KEPT and the case was written to "
            f"reward_reversals, so it cannot be claimed again and "
            f"/level_reconcile can resolve it.")
    for entry in report.get("roles_deferred") or []:
        could_not.append(
            f"role {entry['role_id']} was NOT removed: claim {entry['claim_id']} "
            f"is still being delivered, so the role may be added after this "
            f"reset. Its claim row was kept and this reset is not complete; run "
            f"the reset again once the delivery has finished.")
    for claim in report.get("concurrent_claims") or []:
        could_not.append(
            f"claim {claim['id']} (L{claim['reward_level']} {claim['track']}) was "
            f"delivered while this reset was running, so it was NOT deleted. "
            f"Run the preview again to include it.")
    return could_not


def _traces(report: dict, *, applied: bool) -> list[str]:
    """Level-reward traces this reset does not undo. Non-empty = not a New Game.

    Prestige and value from other systems are deliberately absent - see
    `_notes()`. The goal is a clean level test, not a rebuilt account.
    """
    # `claims_preserved` and `unreversed` only exist once something has been
    # applied, so a preview reads them as empty rather than crashing: the preview
    # branch below is what makes an inspection honest about what apply would leave
    # behind.
    traces = []
    for claim in report.get("claims_preserved") or []:
        traces.append(
            f"claim {claim['id']} (L{claim['reward_level']} {claim['track']}) "
            f"was KEPT because {claim['reason']}.")
    for entry in report.get("unreversed") or []:
        what = (f"{entry.get('amount')} {entry.get('currency')} still in the wallet"
                if entry.get("kind") == "currency"
                else f"{entry.get('quantity')}x {entry.get('item_name')} in the "
                     f"inventory, not proven to be this reward's")
        traces.append(
            f"L{entry['reward_level']} reward not reversed "
            f"[{entry.get('status')}]: {what}.")
    for left in report.get("roles_left_on_member") or []:
        traces.append(
            f"reward role {left['role_id']} is still on the member, because "
            f"{', '.join(left['also_granted_by'])} also grants it.")
    if not applied and (report.get("plan") or {}).get("cannot_reverse"):
        for entry in report["plan"]["cannot_reverse"]:
            traces.append(
                f"L{entry['reward_level']} {entry['kind']} reward would NOT be "
                f"reversible: {entry.get('note')}.")
    # An open reversal case is a trace whether this run wrote it or an earlier
    # one did: the effect is not proven gone, so the reset is not a clean New
    # Game. Cases already listed above are not repeated.
    seen = {c.get("claim_id") for c in report.get("claims_preserved") or []}
    seen |= {e.get("claim_id") for e in report.get("unreversed") or []}
    for entry in report.get("outstanding") or []:
        if entry.get("claim_id") in seen:
            continue
        traces.append(
            f"an operation left claim {entry['claim_id']} "
            f"{entry['kind']} unresolved ({entry['status']}): "
            f"{entry['reason']}.")
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
