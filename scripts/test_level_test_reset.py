"""The scoped /level test reset: preview first, apply second, one member only.

Locks down `utils/level_test_reset.py` and the `/level_test_reset` admin command
against a real SQLite database with fake Discord objects. The point of this suite
is the safety contract, not the happy path:

* preview writes NOTHING (whole-database diff);
* apply touches exactly one (guild_id, user_id) - every other member's rows, all
  four reward-definition tables, the economy, the ledger and the inventory come
  out byte-identical;
* delivered currency/inventory value is reported and NEVER reversed, because it
  cannot be attributed safely;
* fulfilled currency/shop claims are NOT dropped (which would make an
  already-paid reward earnable again) unless `include_delivered` is explicit;
* a production database refuses to apply without an explicit second key;
* a non-positive or non-integer scope is refused outright;
* after a reset the member really can earn and claim the same rewards again,
  exactly once;
* a role another grant also gives is left on the member, and a role that could
  NOT be removed keeps its claim and temp-role rows - so it can never be paid a
  second time and never becomes an undocumented role;
* prestige is per-member state but is Shop-purchased, so it is only cleared on an
  explicit `include_prestige`, and the report refuses to call the result a New
  Game while any trace (prestige, delivered value, a preserved claim) survives.

Run with:
    python scripts/test_level_test_reset.py
"""
import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database

OTHER = USER + 500           # a second member whose data must never move
OTHER_GUILD = GUILD + 500    # a second guild whose data must never move
ROLE_ID = 601
# An unrelated grant's role. Deliberately NOT ROLE_ID: the scoping check
# below proves the reset deletes only level_claim rows, while section 8
# proves a role shared with another grant is left on the member.
OTHER_ROLE_ID = 602
CLEAN_USER = USER + 900    # no reward history at all
COIN_L3 = 100

FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def dump():
    """Every table, every row: the strongest 'nothing else changed' oracle."""
    from database import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    out = {}
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY 1"):
        out[table] = sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
    conn.close()
    return out


def changed_tables(before, after):
    return sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))


def leveling_cog(bot):
    from cogs.leveling import Leveling
    cog = Leveling.__new__(Leveling)
    cog.bot = bot
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}
    cog._spam_incidents = {}
    cog._spam_warn_times = {}
    return cog


def seed():
    """Two members in two guilds, both with claims, boosts and temp-role units."""
    for gid in (GUILD, OTHER_GUILD):
        execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (gid,))
        execute("INSERT INTO leveling_currency_rewards "
                "(guild_id,level,currency,amount,audience) VALUES (?,?, 'balance', ?, 'everyone')",
                (gid, 3, COIN_L3))
        execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
                "VALUES (?,?,?,'everyone')", (gid, 5, ROLE_ID))
    # `source` carries the claim id, and idx_lab_level_claim_source is unique on
    # it globally - so each seeded row needs its own, exactly like production.
    for sequence, (gid, uid) in enumerate(
            ((GUILD, USER), (GUILD, OTHER), (OTHER_GUILD, USER))):
        execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,?)",
                (gid, uid, 5000, 5, 2))
        execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,?,?)",
                (gid, uid, 777, 33))
        execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,quantity,source) "
                "VALUES (?,?, 'Potion','potion', 4, 'mission')", (gid, uid))
        execute("INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,balance_after,"
                "type,reason,source) VALUES (?,?, 'balance', ?, 777, 'credit','Level 3 reward','level_claim')",
                (gid, uid, COIN_L3))
        execute("INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,balance_after,"
                "type,reason,source) VALUES (?,?, 'balance', 5, 782, 'debit','shop purchase','shop')",
                (gid, uid))
        for level, track, ref, payload in (
                (3, "currency", "balance",
                 json.dumps({"currency": "balance", "amount": COIN_L3})),
                (5, "role", str(ROLE_ID), json.dumps({"role_id": ROLE_ID})),
                (5, "shop", "potion",
                 json.dumps({"type": "potion", "name": "Potion", "quantity": 2}))):
            execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
                    "reward_ref,payload_json,status,created_at,fulfilled_at,source) VALUES "
                    "(?,?,?,?,?,?,'fulfilled','2026-01-01T00:00:00+00:00',"
                    "'2026-01-02T00:00:00+00:00','test')",
                    (gid, uid, level, track, ref, payload))
        execute("INSERT INTO leveling_active_boosts (guild_id,user_id,multiplier,expires_at,source) "
                "VALUES (?,?,2.0,'2030-01-01T00:00:00+00:00',?)",
                (gid, uid, f"level_claim:{800000 + sequence}"))
        execute("INSERT INTO leveling_active_boosts (guild_id,user_id,multiplier,expires_at,source) "
                "VALUES (?,?,3.0,'2030-01-01T00:00:00+00:00','shop')", (gid, uid))
        execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,source,claim_id,unit_index) "
                "VALUES (?,?,?, '2030-01-01T00:00:00+00:00','level_claim', ?, 0)",
                (gid, uid, ROLE_ID, 900000 + sequence))
        execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,source) "
                "VALUES (?,?,?, '2030-01-01T00:00:00+00:00','shop')", (gid, uid, OTHER_ROLE_ID))


async def main():
    await reset_database()
    import utils.level_test_reset as reset_mod
    from utils.level_test_reset import (LevelResetError, NEVER_TOUCHED_TABLES,
                                        SCOPED_REVERSAL_TABLES,
                                        inspect_member, reset_member)
    from utils.level_claims import ensure_tables
    await ensure_tables()
    seed()

    real_role = SimpleNamespace(id=ROLE_ID, name="Moon", position=5)
    guild = SimpleNamespace(id=GUILD, get_role=lambda rid: real_role if rid == ROLE_ID else None,
                            me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    held = [real_role]
    member = SimpleNamespace(id=USER, mention=f"<@{USER}>", guild=guild, roles=held,
                             premium_since=None)
    member.remove_roles = AsyncMock(
        side_effect=lambda *r, **k: [held.remove(x) for x in r if x in held])
    member.add_roles = AsyncMock(
        side_effect=lambda *r, **k: held.extend(x for x in r if x not in held))
    guild.get_member = lambda uid: member if uid == USER else None
    guild.fetch_member = AsyncMock(return_value=member)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)

    # ── 1. the preview is read-only and complete ──
    print("== 1. preview ==")
    before = dump()
    report = await reset_member(GUILD, USER)
    check("preview changed no table at all", changed_tables(before, dump()) == [],
          str(changed_tables(before, dump())))
    check("preview reports itself as a preview and did not apply",
          report["mode"] == "preview" and report["applied"] is False, report["mode"])
    plan = report["plan"]
    check("the preview names every record it would touch",
          plan["delete_claims"] == 3 and plan["delete_boosts"] == 1
          and plan["delete_temp_roles"] == 1 and plan["zero_levels_row"] is True
          and plan["remove_discord_roles"] == [ROLE_ID], str(plan))
    check("the preview only counts THIS member's level-claim rows",
          report["xp"] == 5000 and report["level"] == 5
          and len(report["boosts"]) == 1 and len(report["temp_roles"]) == 1
          and rows("SELECT COUNT(*) FROM leveling_active_boosts WHERE guild_id=? AND user_id=?",
                   (GUILD, USER))[0][0] == 2,
          f"boosts={len(report['boosts'])} temp={len(report['temp_roles'])}")
    check("the preview reports delivered currency it will NOT reverse",
          report["currency_credited"] == {"balance": COIN_L3} and report["ledger_rows"] == 1,
          str(report["currency_credited"]))
    check("the preview proves both delivered rewards, so nothing is blocked",
          plan["blocked_on_delivered_value"] is False and plan["cannot_reverse"] == []
          and plan["claims_kept"] == [] and plan["delete_claims"] == 3,
          str(plan["cannot_reverse"]))
    check("inspect_member alone is read-only too",
          changed_tables(before, dump()) == [] and (await inspect_member(GUILD, USER))["xp"] == 5000)

    # ── 2. delivered value is attributed to proof, not blocked wholesale ──
    # This is the heart of the tool: a level reward the test member already
    # collected has to be identifiable before it can be taken back, and anything
    # that cannot be identified has to be reported instead of guessed.
    print("== 2. attributing delivered value ==")
    preview = await reset_member(GUILD, USER)
    attributed = preview["plan"]
    check("the delivered currency is matched to its own append-only ledger row",
          len(attributed["reverse_currency"]) == 1
          and attributed["reverse_currency"][0]["reward_level"] == 3
          and attributed["reverse_currency"][0]["currency"] == "balance"
          and attributed["reverse_currency"][0]["amount"] == COIN_L3
          and attributed["reverse_currency"][0]["ledger_id"] is not None,
          str(attributed["reverse_currency"]))
    check("matched one-to-one, so two rewards cannot claim the same credit",
          preview["reconciliation"]["proven"]["currency"][0]["matched_on"]
              in ("currency+amount+level", "currency+amount"),
          str(preview["reconciliation"]["proven"]["currency"]))
    check("the delivered item is matched to the quantity its own claim froze",
          len(attributed["reverse_inventory"]) == 1
          and attributed["reverse_inventory"][0]["reward_level"] == 5
          and attributed["reverse_inventory"][0]["item_name"] == "Potion"
          and attributed["reverse_inventory"][0]["quantity"] == 2,
          str(attributed["reverse_inventory"]))
    check("the preview shows the wallet and the stock it would move",
          preview["wallet"] == {"balance": 777, "diamonds": 33}
          and [r["quantity"] for r in preview["inventory_candidates"]] == [4],
          f"{preview['wallet']} {preview['inventory_candidates']}")
    check("so nothing is blocked and no claim row needs keeping",
          attributed["cannot_reverse"] == [] and attributed["claims_kept"] == []
          and attributed["delete_claims"] == 3, str(attributed))
    check("and the preview still wrote nothing at all",
          changed_tables(before, dump()) == [],
          str(changed_tables(before, dump())))
    check("an inventory row from another source is a note, not a level-reward trace",
          any("another source" in n for n in preview["notes"])
          and preview["traces"] == [], str(preview["notes"]))
    check("prestige is a note too: it is not a level reward",
          any("Prestige left as it is" in n for n in preview["notes"]),
          str(preview["notes"]))

    # ── 3. the production gate ──
    print("== 3. production gate ==")
    saved_env = reset_mod.NERO_ENVIRONMENT
    reset_mod.NERO_ENVIRONMENT = "production"
    try:
        refused = None
        try:
            await reset_member(GUILD, USER, apply=True, include_delivered=True)
        except LevelResetError as exc:
            refused = str(exc)
        check("production refuses to apply without the explicit second key",
              refused is not None and "production" in refused, str(refused))
        check("the production refusal wrote nothing",
              changed_tables(before, dump()) == [],
              str(changed_tables(before, dump())))
        refused_waiver = None
        try:
            await reset_member(GUILD, USER, apply=True, include_delivered=True,
                               allow_production=True)
        except LevelResetError as exc:
            refused_waiver = str(exc)
        check("production refuses include_delivered even WITH the second key",
              refused_waiver is not None
              and "already-paid reward claimable again" in refused_waiver,
              str(refused_waiver))
        check("and that refusal wrote nothing either",
              changed_tables(before, dump()) == [],
              str(changed_tables(before, dump())))
    finally:
        reset_mod.NERO_ENVIRONMENT = saved_env

    # ── 4. the scope guard ──
    print("== 4. scope guard ==")
    for bad_guild, bad_user in ((0, USER), (GUILD, 0), (-1, USER), (GUILD, -1),
                                (None, USER), (GUILD, "abc")):
        refused = False
        try:
            await inspect_member(bad_guild, bad_user)
        except LevelResetError:
            refused = True
        check(f"a non-positive/non-integer scope is refused (guild={bad_guild!r} "
              f"user={bad_user!r})", refused)
    check("the scope guard wrote nothing", changed_tables(before, dump()) == [])

    # ── 5. apply, on staging, with the explicit delivered-value waiver ──
    print("== 5. apply ==")
    reset_mod.NERO_ENVIRONMENT = "staging"
    try:
        # The DEFAULT path: no include_delivered waiver, so every claim row that
        # survives does so because its effect survived.
        report = await reset_member(GUILD, USER, apply=True, member=member,
                                    reversed_by=999)
    finally:
        reset_mod.NERO_ENVIRONMENT = saved_env
    check("apply reports what it did", report["applied"] is True and report["mode"] == "apply")
    check("the member's XP/level is zeroed and the claim ledger is empty",
          {k: report["after"][k] for k in
           ("xp", "level", "prestige", "vi_activation", "claims", "boosts",
            "temp_roles")} == {"xp": 0, "level": 0, "prestige": 2,
                               "vi_activation": None, "claims": 0, "boosts": 0,
                               "temp_roles": 0}, str(report["after"]))
    check("every delivered level reward was reversed, so this IS a clean New Game",
          report["traces"] == [] and report["new_game_complete"] is True
          and report["unreversed"] == [] and report["claims_preserved"] == [],
          f"traces={report['traces']} unreversed={report['unreversed']}")
    check("prestige surviving is a note, not a trace - it is not a level reward",
          any("Prestige left as it is" in n for n in report["notes"]),
          str(report["notes"]))
    check("the report separates what ran from what could not",
          any("zeroed levels.xp/level" in r for r in report["ran"])
          and any(f"reversed {COIN_L3} balance" in r for r in report["ran"])
          and any("removed 2x Potion" in r for r in report["ran"])
          and report["could_not"] == [],
          f"ran={report['ran']} could_not={report['could_not']}")
    check("the reward role was removed from the member",
          report["roles_removed"] == [ROLE_ID] and not report["role_failures"]
          and real_role not in held, str(report["role_failures"]))
    check("the prestige column and the row itself survived",
          rows("SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(0, 0, 2)])

    changed = changed_tables(before, dump())
    check("only the scoped leveling stores and the three reversal stores changed",
          set(changed) == {"levels", "level_reward_claims", "leveling_active_boosts",
                           "temp_roles"} | set(SCOPED_REVERSAL_TABLES), str(changed))
    check("no reward definition, config, shop or history table was touched",
          not (set(changed) & set(NEVER_TOUCHED_TABLES)),
          str(set(changed) & set(NEVER_TOUCHED_TABLES)))
    after = dump()
    check("the wallet moved by EXACTLY the proven amount, and no further",
          rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(777 - COIN_L3, 33)],
          str(rows("SELECT balance, diamonds FROM economy WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER))))
    check("the item moved by EXACTLY the proven quantity",
          rows("SELECT quantity, source FROM inventory_items WHERE guild_id=? "
               "AND user_id=? AND item_name='Potion'", (GUILD, USER)) == [(2, "mission")],
          str(rows("SELECT quantity FROM inventory_items WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER))))
    check("the reversal is recorded through the ledger, not a silent edit",
          rows("SELECT reversed, reversed_by FROM transaction_ledger WHERE guild_id=? "
               "AND user_id=? AND source='level_claim' AND type='credit'",
               (GUILD, USER)) == [(1, 999)]
          and rows("SELECT type, amount, source FROM transaction_ledger "
                   "WHERE guild_id=? AND user_id=? AND type='reversal'",
                   (GUILD, USER)) == [("reversal", -COIN_L3, "ledger")],
          str(rows("SELECT type, amount, reversed, reversed_by FROM transaction_ledger "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))))
    # reverse_transaction() writes two rows per reversal by design: the
    # compensating debit from safe_deduct, plus its own 'reversal' marker. It also
    # flips `reversed`/`reversed_at`/`reversed_by` on the row it undoes - that is
    # the audit marker, not a rewrite of the financial fact, so every other column
    # of every pre-existing row must still be byte-identical.
    fact = (1, 2, 3, 4, 5, 6, 7, 8, 9, 13)
    ledger_before = {r[0]: r for r in before["transaction_ledger"]}
    ledger_after = {r[0]: r for r in after["transaction_ledger"]}
    check("no ledger row was deleted, and no financial column was rewritten",
          len(ledger_after) == len(ledger_before) + 2
          and set(ledger_before) <= set(ledger_after)
          and all(tuple(ledger_after[i][c] for c in fact)
                  == tuple(row[c] for c in fact)
                  for i, row in ledger_before.items()),
          f"{len(ledger_before)} -> {len(ledger_after)}")
    check("the other member's wallet and inventory are byte-identical",
          after["economy"] == sorted(
              [r for r in before["economy"] if r[1] != USER or r[0] != GUILD]
              + [(GUILD, USER, 777 - COIN_L3, 33)], key=repr)
          and rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, OTHER)) == [(777, 33)],
          str(rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, OTHER))))
    check("reward definitions, config, shop and history are byte-identical",
          all(after[t] == before[t] for t in
              ("shop_items", "leveling_currency_rewards", "leveling_rewards",
               "leveling_boost_rewards", "activity_stats", "purchase_history")))
    check("every reward definition still exists",
          rows("SELECT COUNT(*) FROM leveling_currency_rewards")[0][0] == 2
          and rows("SELECT COUNT(*) FROM leveling_rewards")[0][0] == 2)
    check("the other member in the same guild is untouched",
          rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", (GUILD, OTHER))
          == [(5000, 5)]
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                   (GUILD, OTHER)) == [(3,)])
    check("the same member in another guild is untouched",
          rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
               (OTHER_GUILD, USER)) == [(5000, 5)]
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                   (OTHER_GUILD, USER)) == [(3,)])
    check("the member's non-level-claim boost and temp-role rows survived",
          rows("SELECT source FROM leveling_active_boosts WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [("shop",)]
          and rows("SELECT source FROM temp_roles WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [("shop",)])
    check("the report lists what it reversed, with the amounts and the new totals",
          len(report["reversed_currency"]) == 1
          and report["reversed_currency"][0]["amount"] == COIN_L3
          and report["reversed_currency"][0]["new_balance"] == 777 - COIN_L3
          and report["reversed_inventory"][0]["remaining"] == 2,
          f"{report['reversed_currency']} {report['reversed_inventory']}")

    # ── 6. the reset really lets the test be repeated ──
    print("== 6. re-testing from zero ==")
    from utils.level_claims import claim_available, list_claims
    from utils.reward_engine import give_reward
    from utils.xp_calculator import xp_for_level
    check("after the reset nothing is claimable", await list_claims(GUILD, USER) == [])
    total = sum(xp_for_level(n) for n in range(1, 6))
    await give_reward(bot, GUILD, USER, "xp", amount=total, reason="retest", source="leveling")
    claims = await list_claims(GUILD, USER)
    check("re-levelling earns the same rewards again",
          {(c["reward_level"], c["track"]) for c in claims} == {(3, "currency"), (5, "role")}
          and all(c["status"] == "pending" for c in claims), str(claims))
    bal_before = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                      (GUILD, USER))[0][0]
    res = await claim_available(GUILD, USER, member=member, bot=bot)
    bal_after = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                     (GUILD, USER))[0][0]
    check("and pays them exactly once",
          bal_after - bal_before == COIN_L3 and len(res["fulfilled"]) == 2,
          f"delta={bal_after - bal_before} fulfilled={res['fulfilled']}")
    again = await claim_available(GUILD, USER, member=member, bot=bot)
    check("a second pass is a no-op", again["owned"] == 0)

    # ── 7. the admin command ──
    print("== 7. /level_test_reset command ==")
    import cogs.leveling as leveling
    cog = leveling_cog(bot)
    from discord import app_commands
    command = leveling.Leveling.level_test_reset
    checks = getattr(command.callback, "__discord_app_commands_checks__", [])
    non_admin = SimpleNamespace(
        guild=guild, guild_id=GUILD,
        user=SimpleNamespace(id=USER),
        permissions=SimpleNamespace(administrator=False))
    admin = SimpleNamespace(
        guild=guild, guild_id=GUILD,
        user=SimpleNamespace(id=USER),
        permissions=SimpleNamespace(administrator=True))
    denied = False
    try:
        for predicate in checks:
            predicate(non_admin)
    except app_commands.MissingPermissions:
        denied = True
    check("the command is administrator-gated",
          bool(checks) and denied and all(p(admin) for p in checks),
          f"checks={len(checks)} denied={denied}")
    itx = SimpleNamespace(
        user=SimpleNamespace(id=999, guild_permissions=SimpleNamespace(administrator=True)),
        guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    await command.callback(cog, itx, member, False, False)
    check("the command defers ephemerally before working",
          itx.response.defer.await_args.kwargs.get("ephemeral") is True)
    sent = itx.followup.send.await_args.kwargs
    embed = sent["embed"]
    check("without confirm it sends a preview that says nothing changed",
          sent.get("ephemeral") is True
          and "PREVIEW" in embed.title and "nothing changed" in embed.title, embed.title)
    check("the preview names the scope and the single member",
          f"`{GUILD}`" in embed.fields[0].value and f"`{USER}`" in embed.fields[0].value
          and "Never any other member" in embed.fields[0].value, embed.fields[0].value)
    check("the preview names each delivered reward it WILL reverse, and how",
          any(f.name == "Delivered level rewards this reset reverses"
              and f"L3 · {COIN_L3:,} balance" in f.value
              and "ledger row" in f.value for f in embed.fields),
          str([(f.name, f.value) for f in embed.fields]))
    check("and it states plainly what cannot be attributed to a level reward",
          any(f.name.startswith("Left alone: cannot be attributed")
              and "inventory_items" in f.value and "economy" in f.value
              for f in embed.fields),
          str([f.name for f in embed.fields]))
    check("the preview states prestige is not being reset unless asked",
          any("**not reset** (pass include_prestige)" in f.value for f in embed.fields)
          and not any("zero permanent Prestige" in f.value for f in embed.fields),
          str([f.name for f in embed.fields]))
    check("the preview warns that a failed role removal keeps its records",
          any("can never be paid twice" in f.value for f in embed.fields),
          str([f.name for f in embed.fields]))

    claims_before_command = rows(
        "SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
        (GUILD, USER))[0][0]
    other_before_command = dump()
    itx2 = SimpleNamespace(
        user=itx.user, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    os.environ.pop("ALLOW_LEVEL_TEST_RESET", None)
    await command.callback(cog, itx2, member, True, True)
    check("on production without the env key the command refuses and changes nothing",
          "refused" in (itx2.followup.send.await_args.args[0]
                        if itx2.followup.send.await_args.args else "").lower()
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                   (GUILD, USER))[0][0] == claims_before_command
          and changed_tables(other_before_command, dump()) == [],
          f"{itx2.followup.send.await_args.args} "
          f"changed={changed_tables(other_before_command, dump())}")

    itx3 = SimpleNamespace(
        user=itx.user, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    # include_delivered on production is refused even with the env key: it would
    # delete a claim row whose value is still in the member's hands.
    itx_waiver = SimpleNamespace(
        user=itx.user, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    waiver_before = dump()
    os.environ["ALLOW_LEVEL_TEST_RESET"] = "1"
    try:
        await command.callback(cog, itx_waiver, member, True, True)
        check("the command refuses include_delivered on production too",
              "already-paid reward claimable again" in
              itx_waiver.followup.send.await_args.args[0]
              and changed_tables(waiver_before, dump()) == [],
              str(itx_waiver.followup.send.await_args.args))
        # The default path needs no waiver, because everything is reversible.
        await command.callback(cog, itx3, member, True)
    finally:
        os.environ.pop("ALLOW_LEVEL_TEST_RESET", None)
    applied = itx3.followup.send.await_args.kwargs["embed"]
    check("the applied report separates what ran from what it reversed",
          any(f.name == "What ran" and "zeroed levels.xp/level" in f.value
              for f in applied.fields)
          and any(f.name == "Delivered level rewards reversed"
                  and "ledger" in f.value for f in applied.fields)
          and not any(f.name == "What could NOT be done" for f in applied.fields),
          str([(f.name, f.value) for f in applied.fields]))
    check("and it only claims a clean New Game when nothing survived",
          any(f.name == "\u2705 Clean New Game" for f in applied.fields)
          and "nothing was preserved" in (applied.footer.text or ""),
          str([(f.name, applied.footer.text) for f in applied.fields]))
    check("with both keys it applies and reports the result",
          "APPLIED" in applied.title
          and rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(0, 0)]
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(0,)], applied.title)
    after_command = dump()
    check("the other member still has their data after the command ran",
          rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
               (GUILD, OTHER)) == [(3,)]
          and rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, OTHER)) == [(5000,)]
          and set(changed_tables(other_before_command, after_command))
              <= {"levels", "level_reward_claims"} | set(SCOPED_REVERSAL_TABLES),
          f"other={rows('SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?', (GUILD, OTHER))} "
          f"changed={changed_tables(other_before_command, after_command)}")

    # ── 8. role safety, prestige, and the New Game honesty rule ──
    # Sections 1-7 cover the scoping contract. This one covers the three things a
    # reset can get WRONG in a way that silently damages a member: taking off a
    # role somebody else granted, deleting a claim record whose role is still on
    # the member (which makes the reward payable a second time), and reporting a
    # clean New Game while traces of the old one survive.
    print("== 8. role safety, prestige and New Game honesty ==")

    def claim_count(uid=USER):
        return rows("SELECT COUNT(*) FROM level_reward_claims "
                    "WHERE guild_id=? AND user_id=?", (GUILD, uid))[0][0]

    def stage(uid=USER, *, shared_role=False, vi=False, currency=False,
              shop=False, prestige=2, fresh_levels=False):
        """A known single-member state: L5, one fulfilled role reward."""
        for table in ("level_reward_claims", "temp_roles",
                      "leveling_active_boosts", "prestige_vi_activations"):
            execute(f"DELETE FROM {table} WHERE guild_id=? AND user_id=?",
                    (GUILD, uid))
        if fresh_levels:
            execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
                    "VALUES (?,?,5000,5,?)", (GUILD, uid, prestige))
        else:
            execute("UPDATE levels SET xp=5000, level=5, prestige=? "
                    "WHERE guild_id=? AND user_id=?", (prestige, GUILD, uid))
        execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
                "track,reward_ref,payload_json,status,created_at,fulfilled_at,source) "
                "VALUES (?,?,5,'role',?,?,'fulfilled',"
                "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','test')",
                (GUILD, uid, str(ROLE_ID), json.dumps({"role_id": ROLE_ID})))
        claim_id = rows("SELECT id FROM level_reward_claims WHERE guild_id=? "
                        "AND user_id=? AND track='role'", (GUILD, uid))[0][0]
        execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,"
                "source,claim_id,unit_index) VALUES (?,?,?,"
                "'2030-01-01T00:00:00+00:00','level_claim',?,0)",
                (GUILD, uid, ROLE_ID, claim_id))
        if currency:
            execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
                    "track,reward_ref,payload_json,status,created_at,fulfilled_at,"
                    "source) VALUES (?,?,3,'currency','balance',?,'fulfilled',"
                    "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','test')",
                    (GUILD, uid, json.dumps({"currency": "balance",
                                             "amount": COIN_L3})))
            # What the claim engine really leaves behind: one append-only ledger
            # row naming the reward level, and the money in the wallet. Without
            # both there is nothing to attribute and nothing to reverse.
            execute("INSERT INTO transaction_ledger (guild_id,user_id,currency,"
                    "amount,balance_after,type,reason,source) VALUES (?,?,'balance',"
                    "?,NULL,'credit','Level 3 reward','level_claim')",
                    (GUILD, uid, COIN_L3))
            execute("UPDATE economy SET balance = balance + ? "
                    "WHERE guild_id=? AND user_id=?", (COIN_L3, GUILD, uid))
        if shop:
            execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
                    "track,reward_ref,payload_json,status,created_at,fulfilled_at,"
                    "source) VALUES (?,?,5,'shop','potion',?,'fulfilled',"
                    "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','test')",
                    (GUILD, uid, json.dumps({"type": "potion", "name": "Potion",
                                             "quantity": 2})))
        if shared_role:
            execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,"
                    "source) VALUES (?,?,?,'2030-01-01T00:00:00+00:00','shop')",
                    (GUILD, uid, ROLE_ID))
        if vi:
            execute("INSERT INTO prestige_vi_activations "
                    "(guild_id,user_id,activated_at) VALUES (?,?,?)",
                    (GUILD, uid, "2026-05-01T00:00:00+00:00"))
        if real_role not in held:
            held.append(real_role)

    async def apply(uid=USER, **kw):
        reset_mod.NERO_ENVIRONMENT = "staging"
        try:
            return await reset_member(GUILD, uid, apply=True, **kw)
        finally:
            reset_mod.NERO_ENVIRONMENT = saved_env

    # 8a. a role another grant also gives must not be taken off
    stage(shared_role=True)
    shared_plan = (await inspect_member(GUILD, USER))["plan"]
    check("a role another grant also gives is kept out of the removal plan",
          shared_plan["remove_discord_roles"] == []
          and shared_plan["roles_left_on_member"] == [
              {"role_id": ROLE_ID, "also_granted_by": ["shop"]}],
          str(shared_plan))
    shared = await apply(member=member)
    check("so it stays on the member and no removal is attempted or failed",
          real_role in held and shared["roles_removed"] == []
          and shared["role_failures"] == []
          and any("still on the member" in t for t in shared["traces"]),
          f"removed={shared['roles_removed']} failures={shared['role_failures']}")
    check("only this reward's own records were cleared, the other grant's survived",
          shared["deleted"]["claims"] == 1 and claim_count() == 0
          and rows("SELECT source FROM temp_roles WHERE guild_id=? AND user_id=? "
                   "AND role_id=?", (GUILD, USER, ROLE_ID)) == [("shop",)],
          str(rows("SELECT source, role_id FROM temp_roles WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER))))

    # 8b. a FAILED removal must keep the record, and must not make it payable twice
    stage()
    member.remove_roles = AsyncMock(side_effect=RuntimeError("Missing Permissions"))
    failed = await apply(member=member)
    check("a failed role removal is reported with its reason, not swallowed",
          failed["role_failures"]
          and "Missing Permissions" in failed["role_failures"][0]["error"]
          and any("NOT removed" in c for c in failed["could_not"]),
          str(failed["role_failures"]))
    check("so its claim record survives instead of being deleted anyway",
          failed["claims_preserved"] and claim_count() == 1
          and rows("SELECT status FROM level_reward_claims WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER)) == [("fulfilled",)])
    check("and its temp-role row survives, so the expiry task can retry removal",
          rows("SELECT source FROM temp_roles WHERE guild_id=? AND user_id=? "
               "AND role_id=?", (GUILD, USER, ROLE_ID)) == [("level_claim",)])
    check("the member is still wearing it and the reset is not reported complete",
          real_role in held and failed["new_game_complete"] is False
          and failed["traces"] and failed["roles_removed"] == [],
          str(failed["traces"]))
    total = sum(xp_for_level(n) for n in range(1, 6))
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    await give_reward(bot, GUILD, USER, "xp", amount=total, reason="retest",
                      source="leveling")
    check("a preserved claim can never be paid a second time after re-levelling",
          rows("SELECT COUNT(*), MIN(status) FROM level_reward_claims "
               "WHERE guild_id=? AND user_id=? AND reward_level=5 AND track='role'",
               (GUILD, USER)) == [(1, "fulfilled")],
          str(rows("SELECT id, status FROM level_reward_claims WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER))))

    # 8c. no member object at all: the role cannot be verified as removed
    stage()
    member.remove_roles = AsyncMock(
        side_effect=lambda *r, **k: [held.remove(x) for x in r if x in held])
    no_member = await apply(member=None)
    check("without a member object the role claims are preserved, not deleted",
          no_member["role_failures"]
          and "no member object" in no_member["role_failures"][0]["error"]
          and no_member["claims_preserved"] and claim_count() == 1
          and real_role in held, str(no_member["role_failures"]))

    # 8d. prestige is per-member state, but it is paid for, so it is opt-in
    stage(vi=True, prestige=3)
    kept = await apply(member=member)
    check("prestige and the VI activation survive a default reset",
          rows("SELECT prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(3,)]
          and rows("SELECT COUNT(*) FROM prestige_vi_activations "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER)) == [(1,)])
    check("and prestige is a NOTE, not a trace that blocks the New Game verdict",
          kept["new_game_complete"] is True and kept["traces"] == []
          and any("Prestige left as it is" in n and "permanent tier 3" in n
                  for n in kept["notes"]), str(kept["notes"]))
    stage(vi=True, prestige=3)
    cleared = await apply(member=member, include_prestige=True)
    check("include_prestige zeroes the tier and drops the VI activation",
          rows("SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(0, 0, 0)]
          and rows("SELECT COUNT(*) FROM prestige_vi_activations "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER)) == [(0,)])
    check("the levels ROW survives a prestige reset - it is never deleted",
          rows("SELECT COUNT(*) FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(1,)])
    check("a preview shows the prestige values it would destroy",
          (await inspect_member(GUILD, USER, include_prestige=True))["plan"][
              "reset_prestige"] is True
          and (await inspect_member(GUILD, USER))["plan"]["reset_prestige"] is False)

    # 8e. a member with no delivered value and no prestige really does start clean
    stage(CLEAN_USER, fresh_levels=True, prestige=0)
    clean = await apply(uid=CLEAN_USER, member=member, include_prestige=True)
    check("a member with nothing left behind is reported as a complete New Game",
          clean["new_game_complete"] is True and clean["traces"] == []
          and clean["claims_preserved"] == [] and clean["role_failures"] == [],
          str(clean["traces"]))

    # 8f. delivered value is listed for manual review, proven apart from guesswork
    stage(currency=True, shop=True)
    preview = await reset_member(GUILD, USER)
    recon = preview["reconciliation"]
    # Independently aggregated from the append-only ledger, so this is an oracle
    # rather than a restatement. The total is deliberately MORE than one reward
    # definition's amount: sections 5 and 6 paid this member twice, and a number
    # read from a definition could not know that.
    ledger = rows("SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM transaction_ledger "
                  "WHERE guild_id=? AND user_id=? AND source='level_claim' "
                  "AND type='credit'", (GUILD, USER))[0]
    check("the manual-review listing proves currency from the ledger, not from a "
          "definition",
          recon["proven"]["currency_by_code"] == {"balance": ledger[0]}
          and recon["proven"]["ledger_rows"] == ledger[1]
          and ledger[0] > COIN_L3 and ledger[1] > 1
          and [c["reward_level"] for c in recon["proven"]["delivered_claims"]]
              == [3, 5], f"{recon['proven']} ledger={ledger}")
    check("and names what cannot be attributed, with the reason",
          {u["table"] for u in recon["unattributable"]}
              == {"inventory_items", "economy", "transaction_ledger"}
          and all(u["reason"] for u in recon["unattributable"])
          and "does NOT overwrite `source`" in recon["unattributable"][0]["reason"],
          str(recon["unattributable"]))
    check("with the money still in the wallet both rewards are reversible",
          preview["plan"]["cannot_reverse"] == []
          and len(preview["plan"]["reverse_currency"]) == 1
          and len(preview["plan"]["reverse_inventory"]) == 1
          and preview["traces"] == [], str(preview["plan"]["cannot_reverse"]))

    # The case that matters: the member already SPENT the reward. The ledger still
    # proves what was paid, but the wallet no longer holds it, so taking it back
    # would steal money that came from somewhere else.
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    drained = await reset_member(GUILD, USER)
    stuck = drained["plan"]["cannot_reverse"]
    check("a spent reward is reported as irreversible instead of being forced",
          len(stuck) == 1 and stuck[0]["kind"] == "currency"
          and stuck[0]["amount"] == COIN_L3 and "has been spent" in stuck[0]["note"],
          str(stuck))
    check("and the preview says which claim row that will keep",
          len(drained["plan"]["claims_kept"]) == 1
          and drained["plan"]["delete_claims"] == 2
          and any("would NOT be reversible" in t for t in drained["traces"]),
          str(drained["plan"]["claims_kept"]))
    spent = await apply(member=member)
    check("apply reverses what it can and leaves the wallet at zero, not negative",
          rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(0,)]
          and spent["reversed_inventory"] and not spent["reversed_currency"]
          and len(spent["unreversed"]) == 1, str(spent["unreversed"]))
    check("so only the un-reversed claim row survives",
          claim_count() == 1
          and rows("SELECT reward_level, track, status FROM level_reward_claims "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))
              == [(3, "currency", "fulfilled")],
          str(rows("SELECT reward_level, track, status FROM level_reward_claims "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))))
    check("and the report refuses to call that a clean New Game",
          spent["new_game_complete"] is False and spent["traces"]
          and any("could NOT be reversed" in c or "was NOT reversed" in c
                  for c in spent["could_not"]),
          str(spent["traces"]))
    check("the kept row is terminal, so it can never be claimed again",
          rows("SELECT status FROM level_reward_claims WHERE guild_id=? "
               "AND user_id=?", (GUILD, USER)) == [("fulfilled",)])
    bal_before_relevel = rows("SELECT balance FROM economy WHERE guild_id=? "
                              "AND user_id=?", (GUILD, USER))[0][0]
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    await give_reward(bot, GUILD, USER, "xp", amount=total, reason="retest",
                      source="leveling")
    again = await claim_available(GUILD, USER, member=member, bot=bot)
    check("re-levelling cannot pay the kept reward a second time",
          rows("SELECT COUNT(*), MIN(status) FROM level_reward_claims "
               "WHERE guild_id=? AND user_id=? AND reward_level=3 "
               "AND track='currency'", (GUILD, USER)) == [(1, "fulfilled")]
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER))[0][0] == bal_before_relevel,
          f"balance={rows('SELECT balance FROM economy WHERE guild_id=? AND user_id=?', (GUILD, USER))}")

    # Staging only: the explicit waiver drops the row anyway, and still says the
    # value is outstanding. On production the same call is refused (section 3).
    stage(currency=True, shop=True)
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    waived = await apply(member=member, include_delivered=True)
    check("staging's include_delivered drops the row but still reports the trace",
          waived["applied"] is True and claim_count() == 0
          and waived["new_game_complete"] is False
          and any("not reversed" in t for t in waived["traces"])
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, USER)) == [(0,)],
          str(waived["traces"]))

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LEVEL TEST RESET CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
