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
  exactly once.

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
                "VALUES (?,?,?, '2030-01-01T00:00:00+00:00','shop')", (gid, uid, ROLE_ID))


async def main():
    await reset_database()
    import utils.level_test_reset as reset_mod
    from utils.level_test_reset import (LevelResetError, NEVER_TOUCHED_TABLES,
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
    check("the preview flags the block on already-delivered value",
          plan["blocked_on_delivered_value"] is True and len(plan["blocked_claims"]) == 2
          and {b["track"] for b in plan["blocked_claims"]} == {"currency", "shop"},
          str(plan["blocked_claims"]))
    check("inspect_member alone is read-only too",
          changed_tables(before, dump()) == [] and (await inspect_member(GUILD, USER))["xp"] == 5000)

    # ── 2. the delivered-value gate ──
    print("== 2. delivered-value gate ==")
    blocked = await reset_member(GUILD, USER, apply=True, allow_production=True)
    check("apply is blocked while delivered value cannot be reversed",
          blocked["blocked"] is True and blocked["mode"] == "blocked"
          and blocked["applied"] is False, str(blocked["mode"]))
    check("a blocked apply wrote nothing", changed_tables(before, dump()) == [],
          str(changed_tables(before, dump())))
    check("the block explains which claim rows it will not drop",
          blocked["plan"]["blocked_claims"][0]["track"] == "currency",
          str(blocked["plan"]["blocked_claims"]))

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
        check("the production refusal wrote nothing", changed_tables(before, dump()) == [],
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
        report = await reset_member(GUILD, USER, apply=True, include_delivered=True,
                                    member=member)
    finally:
        reset_mod.NERO_ENVIRONMENT = saved_env
    check("apply reports what it did", report["applied"] is True and report["mode"] == "apply")
    check("the member's XP/level is zeroed and the claim ledger is empty",
          report["after"] == {"xp": 0, "level": 0, "claims": 0, "boosts": 0,
                              "temp_roles": 0}, str(report["after"]))
    check("the reward role was removed from the member",
          report["roles_removed"] == [ROLE_ID] and not report["role_failures"]
          and real_role not in held, str(report["role_failures"]))
    check("the prestige column and the row itself survived",
          rows("SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(0, 0, 2)])

    changed = changed_tables(before, dump())
    check("only the four scoped stores changed",
          set(changed) == {"levels", "level_reward_claims", "leveling_active_boosts",
                           "temp_roles"}, str(changed))
    check("no reward definition, config, economy, ledger or inventory table was touched",
          not (set(changed) & set(NEVER_TOUCHED_TABLES)),
          str(set(changed) & set(NEVER_TOUCHED_TABLES)))
    after = dump()
    check("the economy, ledger and inventory rows are byte-identical",
          all(after[t] == before[t] for t in
              ("economy", "transaction_ledger", "inventory_items", "shop_items",
               "leveling_currency_rewards", "leveling_rewards", "activity_stats")))
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
    check("the value it could not reverse is repeated in the report",
          report["left_behind"]["currency"] == {"balance": COIN_L3}
          and report["left_behind"]["ledger_rows"] == 1
          and len(report["left_behind"]["inventory_candidates"]) == 1,
          str(report["left_behind"]))

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
    check("the preview warns about the delivered value it will not reverse",
          any("NOT reversed" in f.value for f in embed.fields),
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
    os.environ["ALLOW_LEVEL_TEST_RESET"] = "1"
    try:
        await command.callback(cog, itx3, member, True, True)
    finally:
        os.environ.pop("ALLOW_LEVEL_TEST_RESET", None)
    applied = itx3.followup.send.await_args.kwargs["embed"]
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
              <= {"levels", "level_reward_claims"},
          f"other={rows('SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?', (GUILD, OTHER))} "
          f"changed={changed_tables(other_before_command, after_command)}")

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LEVEL TEST RESET CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
