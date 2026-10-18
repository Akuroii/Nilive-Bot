"""/xp_rollback and /level_reconcile: undo ONE recorded change, resolve ONE case.

These two commands are what replaced the deleted `include_delivered` flag, so this
suite is about the boundary between "the administrator made a mistake" and "the
member legitimately earned this". Everything here runs through the real command
callbacks and the real claim engine against a SQLite file:

* a rollback subtracts the recorded change's OWN delta from the member's CURRENT
  XP - it never restores the old snapshot, so progress earned since survives;
* only entitlements that change created AND the member no longer reaches are
  cancelled; anything still reached stays, and anything created by ordinary play
  or another change stays whatever its level;
* delivered value is reversed through the same journal-backed primitives the
  reset uses, so a retry cannot take it twice;
* entitlements that cannot be attributed to the change are listed, never guessed
  at, and never deleted;
* rolling back twice, rolling back a rollback, rolling back a reduction, or
  rolling back another member's change are all refused before anything is written;
* /level_reconcile resolves one named case, records the decision and the
  administrator, and only deletes a claim row when the delivered effect is gone -
  `value_kept` keeps the row terminal so the reward cannot be paid again;
* a rollback and a reset hold the same per-member lease, so they cannot run over
  each other, and a claim pass is frozen while either is in flight.

Run with:
    python scripts/test_xp_rollback.py
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database, member, \
    all_embed_text

OTHER = USER + 500
COIN_L3 = 100
COIN_L4 = 250
COIN_L5 = 400
COIN_L9 = 900

FAILS = []


def check(label, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(label)
        if detail:
            print("        " + str(detail)[:900])


def xp_of(user=USER):
    found = rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0] if found else (0, 0)


def balance(user=USER):
    found = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, user))
    return found[0][0] if found else 0


def claims(user=USER):
    return rows("SELECT id, reward_level, track, status, operation_id "
                "FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                "ORDER BY reward_level, id", (GUILD, user))


def mutations(user=USER):
    return rows("SELECT operation_id, source, actor_id, reason, xp_before, xp_after, "
                "delta, crossings_json, reverses_operation_id, rolled_back "
                "FROM xp_mutations WHERE guild_id=? AND user_id=? ORDER BY id",
                (GUILD, user))


def reply_text(itx):
    """Everything the administrator was sent: the string, or the whole embed.

    `all_embed_text()` covers the title, description and fields; the footer is
    where this tool puts its "how to resolve one case" instructions, so it is
    appended here rather than being invisible to the assertions.
    """
    call = itx.followup.send.await_args
    if call.args:
        return str(call.args[0])
    embed = call.kwargs.get("embed")
    if embed is None:
        return str(call.kwargs)
    footer = getattr(embed, "_footer", None)
    return all_embed_text(embed) + "\n" + str((footer or {}).get("text", ""))


def admin_interaction(person=None, guild_id=GUILD):
    who = person or SimpleNamespace(id=USER, mention=f"<@{USER}>",
                                    display_name="Admin")
    guild = SimpleNamespace(id=guild_id)
    return SimpleNamespace(
        user=who, guild=guild, guild_id=guild_id,
        permissions=SimpleNamespace(administrator=True),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()))


async def main():
    await reset_database()
    from cogs.leveling import Leveling, _apply_admin_xp, level_start_xp
    from utils import level_ops as ops
    from utils.level_claims import (SOURCE_SETLEVEL, claim_available,
                                    ensure_tables, progress_level)
    from utils.level_test_reset import LevelResetError
    await ensure_tables()
    os.environ["NERO_ENVIRONMENT"] = "production"
    os.environ["ALLOW_LEVEL_TEST_RESET"] = "1"

    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    for level, amount in ((3, COIN_L3), (4, COIN_L4), (5, COIN_L5), (9, COIN_L9)):
        execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,"
                "amount,audience) VALUES (?,?, 'balance', ?, 'everyone')",
                (GUILD, level, amount))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,3,1)",
            (GUILD, USER, level_start_xp(3)))
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,0,0)",
            (GUILD, OTHER))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, USER))

    who = member()
    who.add_roles = AsyncMock()
    who.roles = []
    who.guild = SimpleNamespace(id=GUILD, get_role=lambda rid: None,
                                me=SimpleNamespace(top_role=SimpleNamespace(position=9)))
    bot = SimpleNamespace(get_guild=lambda gid: who.guild if gid == GUILD else None)
    cog = Leveling.__new__(Leveling)
    cog.bot = bot
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}

    # The member legitimately reached L3 by playing, and claimed it. That claim is
    # the control: nothing an administrator does later may take it away.
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
            "reward_ref,payload_json,status,created_at,source) VALUES "
            "(?,?,3,'currency','balance',?,'pending',"
            "'2026-01-01T00:00:00+00:00','level_up')",
            (GUILD, USER, json.dumps({"currency": "balance", "amount": COIN_L3})))
    earned = await claim_available(GUILD, USER, member=who)
    earned_claim = claims()[0]
    check("the member's own L3 reward was delivered before any admin change",
          len(earned["fulfilled"]) == 1 and balance() == COIN_L3
          and earned_claim[3] == "fulfilled" and earned_claim[4] is None,
          str([earned["fulfilled"], balance(), earned_claim]))

    # ── the administrative mistake ───────────────────────────────────────────
    print("== 1. an erroneous grant is recorded ==")
    granted = await _apply_admin_xp(GUILD, who, level_start_xp(9), bot,
                                    source=SOURCE_SETLEVEL, actor_id=4242,
                                    reason="typo: meant level 4")
    operation = granted["operation_id"]
    check("/setlevel recorded the change with its actor, reason and delta",
          granted["old_xp"] == level_start_xp(3)
          and granted["new_xp"] == level_start_xp(9)
          and mutations()[-1][2] == 4242 and mutations()[-1][3] == "typo: meant level 4"
          and mutations()[-1][6] == level_start_xp(9) - level_start_xp(3)
          and mutations()[-1][9] == 0, str(mutations()[-1]))
    check("and named the reward levels it newly entitled",
          sorted(granted["crossings"]) == [4, 5, 9]
          and json.loads(mutations()[-1][7]) == sorted(granted["crossings"]),
          str([granted["crossings"], mutations()[-1][7]]))
    created = [c for c in claims() if c[4] == operation]
    check("every entitlement it created carries its operation id (defect G)",
          sorted(c[1] for c in created) == [4, 5, 9]
          and all(c[3] == "pending" for c in created), str(claims()))
    check("the member's own earlier claim carries no operation id",
          earned_claim[4] is None, str(earned_claim))

    # The member then claims ONE of the new rewards (L4) and keeps earning, so the
    # rollback has to handle both a delivered reward and entitlements still pending.
    l4_claim = [c[0] for c in claims() if c[1] == 4 and c[4] == operation][0]
    paid = await claim_available(GUILD, USER, member=who, claim_ids=[l4_claim])
    check("the member claimed one of the granted rewards, so it is delivered",
          len(paid["fulfilled"]) == 1 and balance() == COIN_L3 + COIN_L4
          and [c[3] for c in claims() if c[0] == l4_claim] == ["fulfilled"]
          and [c[3] for c in claims() if c[1] in (5, 9)] == ["pending", "pending"],
          str([paid["fulfilled"], balance(), claims()]))
    execute("UPDATE levels SET xp = xp + 300 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    current_xp = xp_of()[0]
    check("and then earned more XP by playing",
          current_xp == level_start_xp(9) + 300, str(current_xp))

    # ── 2. the preview ───────────────────────────────────────────────────────
    print("== 2. /xp_rollback preview ==")
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who)
    listing = all_embed_text(itx.followup.send.await_args.kwargs["embed"])
    check("with no operation id it lists this member's recorded changes",
          operation in listing and "typo: meant level 4" in listing
          and "setlevel" in listing and itx.response.defer.await_count == 1,
          listing[:400])
    check("listing wrote nothing", mutations()[-1][9] == 0 and xp_of()[0] == current_xp)

    snapshot = (rows("SELECT * FROM levels"), rows("SELECT * FROM economy"),
                claims(), rows("SELECT * FROM transaction_ledger"),
                rows("SELECT * FROM reward_reversals"))
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who, operation=operation)
    embed = itx.followup.send.await_args.kwargs["embed"]
    text = all_embed_text(embed)
    check("the preview says nothing changed",
          "PREVIEW" in (embed.title or "") and "nothing changed" in text, embed.title)
    check("the preview really wrote nothing",
          snapshot == (rows("SELECT * FROM levels"), rows("SELECT * FROM economy"),
                       claims(), rows("SELECT * FROM transaction_ledger"),
                       rows("SELECT * FROM reward_reversals")))
    target_xp = current_xp - (level_start_xp(9) - level_start_xp(3))
    check("the target is CURRENT XP minus the change's own delta, not its old "
          "snapshot",
          f"{target_xp:,}" in text and progress_level(target_xp) == 3
          and f"{level_start_xp(3):,} XP" not in text.split("target")[0],
          text[:600])
    check("the 300 XP earned since is reported as kept",
          "300 XP earned SINCE" in text and "kept" in text, text[:600])
    check("the preview names the entitlements it would cancel",
          "Entitlements this would cancel" in text and "L5" in text and "L9" in text,
          text[:800])
    check("and the delivered value it would take back",
          "would take back" in text and str(COIN_L4) in text, text[:800])
    check("the member's own L3 reward is listed as left alone",
          "not created by this operation" in text or "still earned" in text,
          text[:900])

    # ── 3. confirmation is explicit ─────────────────────────────────────────
    print("== 3. confirmation ==")
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who, operation=operation,
                                        confirm=True)
    check("confirm without a reason is refused BEFORE deferring",
          itx.response.defer.await_count == 0
          and itx.response.send_message.await_count == 1
          and "reason" in itx.response.send_message.await_args.args[0].lower()
          and itx.followup.send.await_count == 0,
          str(itx.response.send_message.await_args))
    check("and nothing was written", xp_of()[0] == current_xp
          and mutations()[-1][9] == 0)

    # ── 4. applying the rollback ────────────────────────────────────────────
    print("== 4. apply ==")
    ledger_before = rows("SELECT COUNT(*) FROM transaction_ledger WHERE guild_id=? "
                         "AND user_id=?", (GUILD, USER))[0][0]
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who, operation=operation,
                                        confirm=True, reason="admin typo, member "
                                                            "was over-granted")
    embed = itx.followup.send.await_args.kwargs["embed"]
    text = all_embed_text(embed)
    check("the applied embed says so", "APPLIED" in (embed.title or ""), embed.title)
    check("XP is now the current value minus the delta - later progress survived",
          xp_of() == (target_xp, 3), str(xp_of()))
    check("the member's own L3 reward is untouched and still delivered",
          [c for c in claims() if c[0] == earned_claim[0]]
          == [(earned_claim[0], 3, "currency", "fulfilled", None)]
          and balance() >= COIN_L3, str(claims()))
    check("the entitlements above the new level created by the change are gone",
          [c[1] for c in claims() if c[4] == operation] == []
          and [c[1] for c in claims()] == [3], str(claims()))
    check("the delivered L4 currency was taken back, and only that",
          balance() == COIN_L3, str(balance()))
    check("and the reversal wrote exactly one compensating ledger row (defect E)",
          rows("SELECT COUNT(*) FROM transaction_ledger WHERE guild_id=? AND user_id=? "
               "AND type='reversal'", (GUILD, USER))[0][0] == 1
          and rows("SELECT COUNT(*) FROM transaction_ledger WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER))[0][0] == ledger_before + 1,
          str(rows("SELECT type, amount FROM transaction_ledger WHERE user_id=?",
                   (USER,))))
    check("the journal holds one applied row for the one reversed claim",
          sorted(rows("SELECT kind, status FROM reward_reversals WHERE guild_id=? "
                      "AND user_id=?", (GUILD, USER)))
          == [("currency", "applied")],
          str(rows("SELECT claim_id, kind, status FROM reward_reversals WHERE user_id=?",
                   (USER,))))
    check("the original change is marked rolled back",
          [m[9] for m in mutations() if m[0] == operation] == [1], str(mutations()))
    rollback_row = [m for m in mutations() if m[8] == operation]
    check("and the rollback is itself a recorded operation naming what it undid",
          len(rollback_row) == 1 and rollback_row[0][1] == ops.OP_ROLLBACK
          and rollback_row[0][2] == USER          # the acting administrator
          and rollback_row[0][6] == -(level_start_xp(9) - level_start_xp(3))
          and "over-granted" in rollback_row[0][3], str(rollback_row))
    check("the rollback's operation row is in level_ops too",
          rows("SELECT kind, status FROM level_ops WHERE user_id=? AND kind=?",
               (USER, ops.OP_ROLLBACK)) == [(ops.OP_ROLLBACK, "completed")],
          str(rows("SELECT * FROM level_ops WHERE user_id=?", (USER,))))
    check("the lease was released",
          rows("SELECT COUNT(*) FROM level_op_locks") == [(0,)],
          str(rows("SELECT * FROM level_op_locks")))
    check("no other member moved", xp_of(OTHER) == (0, 0) and balance(OTHER) == 0)

    # ── 5. the refusals ─────────────────────────────────────────────────────
    print("== 5. refusals ==")
    for label, kwargs, expect in (
            ("rolling the same change back twice",
             {"operation": operation, "confirm": True, "reason": "again"},
             "already rolled back"),
            ("rolling back the rollback itself",
             {"operation": rollback_row[0][0], "confirm": True, "reason": "undo"},
             "is itself a rollback"),
            ("an operation id that does not exist",
             {"operation": "setlevel-nope", "confirm": True, "reason": "?"},
             "no recorded XP operation")):
        itx = admin_interaction()
        await Leveling.xp_rollback.callback(cog, itx, who, **kwargs)
        body = itx.followup.send.await_args.args[0]
        check(f"{label} is refused and says nothing was changed",
              "refused" in body.lower() and expect in body
              and "nothing was changed" in body, body[:300])
    check("and none of those refusals moved XP or claims",
          xp_of() == (target_xp, 3) and [c[1] for c in claims()] == [3],
          str([xp_of(), claims()]))

    # A change that LOWERED XP is not something a rollback may undo: that would be
    # a grant, and it could re-open rewards that were correctly taken away.
    lowered = await _apply_admin_xp(GUILD, who, target_xp - 100, bot,
                                    source="setxp", actor_id=4242,
                                    reason="correcting an over-award")
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who,
                                        operation=lowered["operation_id"],
                                        confirm=True, reason="undo the reduction")
    body = itx.followup.send.await_args.args[0]
    check("rolling back a reduction is refused: that would GRANT XP",
          "refused" in body.lower() and "GRANT" in body and "/setxp" in body,
          body[:300])
    check("and the reduction stands", xp_of()[0] == target_xp - 100, str(xp_of()))

    # Another member's operation is out of scope, whatever the id says.
    other_change = await _apply_admin_xp(GUILD, member(user=OTHER), 5000, bot,
                                         source="setxp", actor_id=4242,
                                         reason="other member")
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who,
                                        operation=other_change["operation_id"],
                                        confirm=True, reason="wrong member")
    body = itx.followup.send.await_args.args[0]
    check("another member's operation is refused: a rollback is scoped to one "
          "member",
          "refused" in body.lower() and "not to this member" in body, body[:300])
    check("and the other member's XP is untouched",
          xp_of(OTHER) == (5000, progress_level(5000)), str(xp_of(OTHER)))

    # ── 6. entitlements that cannot be attributed are never guessed at ───────
    print("== 6. unattributable entitlements ==")
    legacy_change = await _apply_admin_xp(GUILD, who, level_start_xp(6), bot,
                                          source="setxp", actor_id=4242,
                                          reason="pre-provenance simulation")
    execute("UPDATE level_reward_claims SET operation_id=NULL WHERE guild_id=? "
            "AND user_id=? AND operation_id=?", (GUILD, USER,
                                                 legacy_change["operation_id"]))
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(cog, itx, who,
                                        operation=legacy_change["operation_id"])
    text = all_embed_text(itx.followup.send.await_args.kwargs["embed"])
    check("a change whose rows cannot be matched says so instead of guessing",
          "Cannot be attributed to this operation" in text
          and "predate operation records" in text, text[:800])
    check("and the preview cancels nothing",
          "would cancel" not in text, text[:800])
    before_apply = claims()
    itx = admin_interaction()
    await Leveling.xp_rollback.callback(
        cog, itx, who, operation=legacy_change["operation_id"], confirm=True,
        reason="undo it anyway")
    check("applying it reduces XP but deletes no claim row it cannot attribute",
          xp_of()[0] < level_start_xp(6)
          and sorted(c[0] for c in claims()) == sorted(c[0] for c in before_apply)
          + [c[0] for c in claims() if c[0] not in
             [b[0] for b in before_apply]],
          str([xp_of(), claims(), before_apply]))
    text = all_embed_text(itx.followup.send.await_args.kwargs["embed"])
    check("and the applied report lists the rows for /level_reconcile",
          "/level_reconcile" in text, text[:900])

    # ── 7. /level_reconcile ─────────────────────────────────────────────────
    print("== 7. /level_reconcile ==")
    # A delivered reward whose value cannot be proven, so its case stays open.
    hist_user = USER + 700
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,1200,3)",
            (GUILD, hist_user))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,50,0)",
            (GUILD, hist_user))
    execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
            "quantity,source) VALUES (?,?, 'Potion','potion', 5, 'mission')",
            (GUILD, hist_user))
    hist_claim = execute(
        "INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
        "reward_ref,payload_json,status,created_at,fulfilled_at,source) VALUES "
        "(?,?,4,'shop','potion',?,'fulfilled','2026-01-01T00:00:00+00:00',"
        "'2026-01-02T00:00:00+00:00','level_claim')",
        (GUILD, hist_user, json.dumps({"type": "potion", "name": "Potion",
                                       "quantity": 2})))
    await ops.note_reversal(guild_id=GUILD, user_id=hist_user, claim_id=hist_claim,
                            kind=ops.KIND_INVENTORY, quantity=2,
                            operation_id="op-historical", status=ops.STATUS_REFUSED,
                            item_name="Potion",
                            reason="no provenance record: delivered before "
                                   "reward_grants existed")
    hist_person = SimpleNamespace(id=hist_user, mention=f"<@{hist_user}>",
                                  display_name="Historical")
    hist_member = member(user=hist_user)
    itx = admin_interaction(person=hist_person)
    await Leveling.level_reconcile.callback(cog, itx, hist_member)
    text = reply_text(itx)
    queue = await ops.outstanding_reversals(GUILD, hist_user)
    check("with no case id it lists this member's open cases with their evidence",
          len(queue) == 1 and queue[0]["reversal_id"] in text
          and "no provenance record" in text and "claim" in text
          and str(hist_claim) in text, text[:600])
    check("and it tells the administrator how to resolve one",
          "/level_reconcile" in text and "resolution" in text, text[:900])

    itx = admin_interaction(person=hist_person)
    await Leveling.level_reconcile.callback(cog, itx, hist_member,
                                            case=queue[0]["reversal_id"],
                                            resolution="effect_gone")
    check("a resolution without a note is refused before deferring",
          itx.response.defer.await_count == 0
          and "note" in itx.response.send_message.await_args.args[0].lower(),
          str(itx.response.send_message.await_args))
    itx = admin_interaction(person=hist_person)
    await Leveling.level_reconcile.callback(cog, itx, hist_member,
                                            resolution="effect_gone",
                                            note="no case id")
    check("a resolution without a case id is refused",
          itx.response.defer.await_count == 0
          and "case" in itx.response.send_message.await_args.args[0].lower(),
          str(itx.response.send_message.await_args))
    itx = admin_interaction(person=hist_person)
    await Leveling.level_reconcile.callback(cog, itx, hist_member,
                                            case="rev-doesnotexist",
                                            resolution="effect_gone", note="x")
    body = itx.followup.send.await_args.args[0]
    check("another case id cannot be resolved through this member",
          "refused" in body.lower() or "No open case" in body, body[:300])
    check("and the real case is still open",
          len(await ops.outstanding_reversals(GUILD, hist_user)) == 1)

    # `value_kept` must NOT delete the claim row: the member still has the items,
    # so re-opening the reward would pay it a second time.
    itx = admin_interaction(person=hist_person)
    await Leveling.level_reconcile.callback(
        cog, itx, hist_member, case=queue[0]["reversal_id"],
        resolution="value_kept", note="member keeps the potions, reward stays used",
        drop_claim=True)
    body = reply_text(itx)
    check("drop_claim is refused while the value is still in the member's hands",
          "refused" in str(body).lower() and "paid again" in str(body), str(body)[:400])
    check("the claim row survived, so the reward cannot be claimed twice",
          rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
               (GUILD, hist_user)) == [(1,)], str(claims(hist_user)))
    check("and the case is recorded as resolved by that administrator",
          [j[1] for j in rows("SELECT claim_id, status, reason FROM reward_reversals "
                              "WHERE user_id=?", (hist_user,))]
          == ["resolved:value_kept"]
          and str(hist_user) in rows("SELECT reason FROM reward_reversals "
                                     "WHERE user_id=?", (hist_user,))[0][0],
          str(rows("SELECT status, reason FROM reward_reversals WHERE user_id=?",
                   (hist_user,))))
    check("the journal row itself was never deleted",
          rows("SELECT COUNT(*) FROM reward_reversals WHERE user_id=?",
               (hist_user,)) == [(1,)])

    # `effect_gone` with drop_claim: the administrator states the effect is gone,
    # so the row may go and the reward becomes earnable again - on the record.
    await ops.note_reversal(guild_id=GUILD, user_id=hist_user, claim_id=hist_claim,
                            kind=ops.KIND_CURRENCY, quantity=COIN_L3,
                            operation_id="op-historical", status=ops.STATUS_OUTSTANDING,
                            currency="balance", reason="the wallet holds 50 balance")
    second_case = [q for q in await ops.outstanding_reversals(GUILD, hist_user)
                   if q["kind"] == "currency"][0]
    itx = admin_interaction(person=hist_person)
    await Leveling.level_reconcile.callback(
        cog, itx, hist_member, case=second_case["reversal_id"],
        resolution="effect_gone", note="refunded by hand in #support, ticket 55",
        drop_claim=True)
    body = reply_text(itx)
    check("with effect_gone the claim row is deleted and the decision recorded",
          "deleted" in body and "effect_gone" in body
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? "
                   "AND user_id=?", (GUILD, hist_user)) == [(0,)],
          str([body[:400], claims(hist_user)]))
    check("the resolution names the administrator and the note",
          "ticket 55" in body and f"<@{hist_user}>" in body, body[:400])
    check("and an operation row records the deletion",
          rows("SELECT kind, status FROM level_ops WHERE user_id=? AND kind=?",
               (hist_user, ops.OP_RECONCILE)) == [(ops.OP_RECONCILE, "completed")],
          str(rows("SELECT * FROM level_ops WHERE user_id=?", (hist_user,))))

    # ── 8. a correction and a claim cannot run over each other ───────────────
    print("== 8. coordination ==")
    lease_operation = ops.new_operation_id(ops.OP_ROLLBACK)
    taken = await ops.acquire_lock(GUILD, USER, holder="xp_rollback",
                                   operation_id=lease_operation)
    check("a rollback can take the member's lease", taken["ok"] is True, str(taken))
    blocked = await claim_available(GUILD, USER, member=who)
    check("Claim All is frozen while a rollback holds the lease (defect F)",
          bool(blocked.get("blocked")) and blocked["fulfilled"] == []
          and blocked["blocked_by_holder"] == "xp_rollback", str(blocked))
    itx = admin_interaction()
    await Leveling.level_test_reset.callback(cog, itx, who, confirm=True,
                                             reason="concurrent reset")
    body = itx.followup.send.await_args.args[0]
    check("and a reset for the same member is refused while the lease is held",
          "refused" in body.lower() and "another operation is running" in body,
          body[:300])
    await ops.release_lock(GUILD, USER, lease_operation)
    check("after the lease is released the claim path works again",
          not (await claim_available(GUILD, USER, member=who)).get("blocked"))

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL XP ROLLBACK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
