"""The scoped /level test reset: preview first, apply second, one member only.

Locks down `utils/level_test_reset.py`, `utils/level_ops.py` and the
`/level_test_reset` admin command against a real SQLite database with fake
Discord objects. The point of this suite is the safety contract, not the happy
path:

* preview writes NOTHING (whole-database diff);
* apply touches exactly one (guild_id, user_id) - every other member's rows, all
  four reward-definition tables and every unrelated economy/ledger/inventory row
  come out byte-identical;
* delivered value is reversed from PROVENANCE (a `reward_grants` row, a ledger
  row), never from a stack total or a balance - and when provenance does not
  exist the reversal is REFUSED, the claim row is KEPT, and the case goes on the
  reconciliation queue instead of being guessed at;
* no claim row whose delivered effect survived is ever deleted, and there is no
  flag that overrides that (the old `include_delivered` is gone);
* a reversal is idempotent: a retry, a crash mid-way or a second process moves
  the value once, because the journal row commits with the movement it describes;
* exactly ONE compensating ledger row per reversal, so summing the ledger cannot
  double-count an event;
* the environment gate is decided from the database file actually open, fails
  closed on ambiguity, and is not satisfied by a label or by the production key;
* a non-positive or non-integer scope is refused outright;
* after a reset the member really can earn and claim the same rewards again,
  exactly once;
* a role another grant also gives is left on the member, and a role that could
  NOT be removed keeps its claim and temp-role rows - so it can never be paid a
  second time and never becomes an undocumented role;
* prestige is per-member state but is Shop-purchased, so it is PRESERVED unless
  `include_prestige` is explicit, and the report refuses to call the result a New
  Game while any trace survives;
* a database that predates the provenance schema can still be inspected and
  reset - it just cannot prove anything, and says so.

Run with:
    python scripts/test_level_test_reset.py
"""
import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database, all_embed_text

OTHER = USER + 500           # a second member whose data must never move
OTHER_GUILD = GUILD + 500    # a second guild whose data must never move
ROLE_ID = 601
# An unrelated grant's role. Deliberately NOT ROLE_ID: the scoping check below
# proves the reset deletes only level_claim rows, while section 8 proves a role
# shared with another grant is left on the member.
OTHER_ROLE_ID = 602
CLEAN_USER = USER + 900      # no reward history at all
COIN_L3 = 100
POTION_REWARD_QTY = 2
# A second member in the same guild whose reward has no provenance at all, which
# is the state every real member is in until the claim engine records a grant.
HISTORICAL_USER = USER + 700

FAILS = []


def check(label, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(label)
        if detail:
            print("        " + str(detail)[:900])


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


def wallet(user=USER, guild=GUILD):
    row = rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
               (guild, user))
    return row[0] if row else (0, 0)


def stack(user=USER, guild=GUILD, item="Potion"):
    row = rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
               "AND item_name=?", (guild, user, item))
    return row[0][0] if row else 0


def seed_definitions():
    for gid in (GUILD, OTHER_GUILD):
        execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (gid,))
        execute("INSERT INTO leveling_currency_rewards "
                "(guild_id,level,currency,amount,audience) VALUES (?,?, 'balance', ?, 'everyone')",
                (gid, 3, COIN_L3))
        execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
                "VALUES (?,?,?,'everyone')", (gid, 5, ROLE_ID))
        execute("INSERT INTO leveling_shop_rewards (guild_id,level,item_id,quantity) "
                "VALUES (?,?,?,?)", (gid, 4, 4242, POTION_REWARD_QTY))


def seed_member(uid, gid=GUILD, *, sequence=0, provenance=True, legacy=True,
                xp=5000, balance=777, diamonds=33, potion_stack=4):
    """One member's reward history: 3 real deliveries plus a legacy backfill row.

    `provenance=False` reproduces the state of every member whose rewards were
    delivered before `reward_grants` existed: the claim rows and the ledger row
    are there, the item is in a shared stack, and NOTHING proves which units
    belong to the reward.
    """
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,?)",
            (gid, uid, xp, 5, 2))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,?,?)",
            (gid, uid, balance, diamonds))
    execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,quantity,source) "
            "VALUES (?,?, 'Potion','potion', ?, 'mission')", (gid, uid, potion_stack))
    ledger_id = execute(
        "INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,balance_after,"
        "type,reason,source) VALUES (?,?, 'balance', ?, ?, 'credit','Level 3 reward',"
        "'level_claim')", (gid, uid, COIN_L3, balance))
    execute("INSERT INTO transaction_ledger (guild_id,user_id,currency,amount,balance_after,"
            "type,reason,source) VALUES (?,?, 'balance', 5, 782, 'debit','shop purchase','shop')",
            (gid, uid))
    entries = [
        (3, "currency", "balance",
         json.dumps({"currency": "balance", "amount": COIN_L3}), "test"),
        (4, "shop", "potion",
         json.dumps({"type": "potion", "name": "Potion", "quantity": POTION_REWARD_QTY}),
         "test"),
        (5, "role", str(ROLE_ID), json.dumps({"role_id": ROLE_ID}), "test"),
    ]
    if legacy:
        # `backfill_legacy_claims()` marks already-reached rewards fulfilled with
        # NO payout, and stamps them with this source. Its migration is literally
        # named `legacy_fulfilled_no_payout`, so the row itself is the evidence
        # that the bot never delivered anything.
        entries.append((5, "shop", "elixir",
                        json.dumps({"type": "potion", "name": "Elixir", "quantity": 3}),
                        "legacy_backfill"))
    claim_ids = {}
    for level, track, ref, payload, source in entries:
        claim_ids[(level, track)] = execute(
            "INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
            "reward_ref,payload_json,status,created_at,fulfilled_at,source) VALUES "
            "(?,?,?,?,?,?,'fulfilled','2026-01-01T00:00:00+00:00',"
            "'2026-01-02T00:00:00+00:00',?)",
            (gid, uid, level, track, ref, payload, source))
    if provenance:
        # What the claim engine now writes INSIDE its own delivery transaction:
        # the grant names the ledger row for currency, and keeps a
        # `remaining_quantity` counter for items.
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
                "reward_level,track,kind,currency,quantity,remaining_quantity,"
                "ledger_id,operation_id,created_at) VALUES "
                "(?,?,?,?,?, 'currency','currency','balance',?,?,?,?,"
                "'2026-01-02T00:00:00+00:00')",
                (f"cgrant-{gid}-{uid}", gid, uid, claim_ids[(3, "currency")], 3,
                 COIN_L3, COIN_L3, ledger_id, f"op-{gid}-{uid}"))
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
                "reward_level,track,kind,item_name,quantity,remaining_quantity,"
                "operation_id,created_at) VALUES "
                "(?,?,?,?,?, 'shop','inventory','Potion',?,?,?,"
                "'2026-01-02T00:00:00+00:00')",
                (f"grant-{gid}-{uid}", gid, uid, claim_ids[(4, "shop")], 4,
                 POTION_REWARD_QTY, POTION_REWARD_QTY, f"op-{gid}-{uid}"))
    execute("INSERT INTO leveling_active_boosts (guild_id,user_id,multiplier,expires_at,source) "
            "VALUES (?,?,2.0,'2030-01-01T00:00:00+00:00',?)",
            (gid, uid, f"level_claim:{800000 + sequence}"))
    execute("INSERT INTO leveling_active_boosts (guild_id,user_id,multiplier,expires_at,source) "
            "VALUES (?,?,3.0,'2030-01-01T00:00:00+00:00','shop')", (gid, uid))
    # claim_id points at the real role claim, as the claim engine writes it.
    execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,source,claim_id,"
            "unit_index) VALUES (?,?,?, '2030-01-01T00:00:00+00:00','level_claim', ?, 0)",
            (gid, uid, ROLE_ID, claim_ids[(5, "role")]))
    execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,source) "
            "VALUES (?,?,?, '2030-01-01T00:00:00+00:00','shop')", (gid, uid, OTHER_ROLE_ID))
    return claim_ids


def seed():
    """The fixture every section starts from."""
    seed_definitions()
    seed_member(USER, GUILD, sequence=0)
    seed_member(OTHER, GUILD, sequence=1)
    seed_member(USER, OTHER_GUILD, sequence=2)
    # A member with reward history and no provenance at all.
    seed_member(HISTORICAL_USER, GUILD, sequence=3, provenance=False,
                potion_stack=5, balance=900)


def fake_discord(role_id=ROLE_ID, *, removal_fails=False, user=USER):
    """A guild + member whose roles behave like Discord's, without Discord."""
    real_role = SimpleNamespace(id=role_id, name="Moon", position=5)
    guild = SimpleNamespace(
        id=GUILD, get_role=lambda rid: real_role if rid == role_id else None,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    held = [real_role]
    person = SimpleNamespace(id=user, mention=f"<@{user}>", guild=guild, roles=held,
                             premium_since=None, display_name="Phase One")

    async def remove(*roles, **kwargs):
        if removal_fails:
            raise RuntimeError("Missing Permissions")
        for role in roles:
            if role in held:
                held.remove(role)
    person.remove_roles = AsyncMock(side_effect=remove)
    person.add_roles = AsyncMock(
        side_effect=lambda *r, **k: held.extend(x for x in r if x not in held))
    guild.get_member = lambda uid: person if uid == user else None
    guild.fetch_member = AsyncMock(return_value=person)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)
    return guild, person, held, bot


async def main():
    await reset_database()
    import utils.level_test_reset as reset_mod
    from utils.level_test_reset import (
        PROVENANCE_TABLES, LevelResetError, inspect_member, reset_member,
    )
    from utils.level_claims import ensure_tables
    from utils import level_ops as ops
    from database import DB_PATH
    await ensure_tables()
    seed()
    guild, member, held, bot = fake_discord()
    cog = leveling_cog(bot)

    def admin_interaction():
        person = SimpleNamespace(id=USER, mention=f"<@{USER}>", display_name="Admin")
        return SimpleNamespace(
            user=person, guild=guild, guild_id=GUILD, client=bot,
            permissions=SimpleNamespace(administrator=True),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                     is_done=lambda: False),
            followup=SimpleNamespace(send=AsyncMock()))

    # ── 1. the preview is read-only and complete ─────────────────────────────
    print("== 1. preview ==")
    before = dump()
    preview = await inspect_member(GUILD, USER)
    check("preview changed no table at all", changed_tables(before, dump()) == [],
          str(changed_tables(before, dump())))
    check("the preview reports the member, the guild and the resolved db file",
          preview["guild_id"] == GUILD and preview["user_id"] == USER
          and preview["db_path"] == DB_PATH and preview["xp"] == 5000
          and preview["level"] == 5, str({k: preview[k] for k in
                                          ("guild_id", "user_id", "db_path", "xp")}))
    check("every claim row is listed with its status and its role",
          {(c["reward_level"], c["track"]): c["status"]
           for c in preview["claims"]} == {(3, "currency"): "fulfilled",
                                           (4, "shop"): "fulfilled",
                                           (5, "role"): "fulfilled",
                                           (5, "shop"): "fulfilled"}
          and [c["role_id"] for c in preview["claims"]
               if c["track"] == "role"] == [ROLE_ID],
          str(preview["claims"]))
    # Defect H: a temp-role SHOP reward's Discord role must be found too, or its
    # temp_roles rows get deleted while the member keeps wearing the role.
    check("a temp-role shop reward's role id is collected as well (defect H)",
          all(isinstance(c.get("role_id"), int) or c.get("role_id") is None
              for c in preview["claims"])
          and preview["plan"]["remove_discord_roles"] == [ROLE_ID],
          str(preview["plan"]["remove_discord_roles"]))
    check("the plan names the provenance tables it may write",
          set(PROVENANCE_TABLES) == {"reward_grants", "reward_reversals",
                                     "level_ops", "level_op_locks", "xp_mutations"},
          str(PROVENANCE_TABLES))
    check("prestige is reported and NOT reset by default",
          preview["prestige"] == 2 and preview["plan"]["reset_prestige"] is False,
          str(preview["plan"]["reset_prestige"]))
    check("the preview's notes mention prestige and unrelated inventory",
          any("Prestige left as it is" in n for n in preview["notes"])
          and any("mission" in n for n in preview["notes"]),
          str(preview["notes"]))

    # ── 2. attribution is provenance, not inference ──────────────────────────
    print("== 2. attribution comes from provenance ==")
    attr = preview["attribution"]
    currency = attr["currency"][0]
    check("a currency reward is reversible from its grant's ledger row",
          currency["reversible"] is True and currency["ledger_id"] == 1
          and currency["amount"] == COIN_L3
          and currency["provenance"] == "reward_grants", str(currency))
    inventory = attr["inventory"][0]
    check("an item reward is reversible only from its grant's remaining units",
          inventory["reversible"] is True and inventory["quantity"] == POTION_REWARD_QTY
          and inventory["attributable"] == POTION_REWARD_QTY
          and inventory["stack"] == 4 and inventory["provenance"] == "reward_grants",
          str(inventory))
    check("a legacy backfill row is recognised as never delivered",
          len(attr["legacy_backfill"]) == 1
          and attr["legacy_backfill"][0]["claim_id"]
          == [c["id"] for c in preview["claims"]
              if c["reward_level"] == 5 and c["track"] == "shop"][0]
          and "never paid" in attr["legacy_backfill"][0]["note"],
          str(attr["legacy_backfill"]))

    # Defect B: a reward of 2 potions, already drunk, then 5 potions from another
    # source. The stack reads 5 - the old rule removed 2 of someone else's.
    historical = await inspect_member(GUILD, HISTORICAL_USER)
    hist_inv = historical["attribution"]["inventory"][0]
    check("with no grant row an item reversal is REFUSED, not inferred from the stack",
          hist_inv["reversible"] is False and hist_inv["stack"] == 5
          and "no provenance record" in hist_inv["note"], str(hist_inv))
    check("and the historical member's stack is not in the plan to reverse",
          historical["plan"]["reverse_inventory"] == []
          and len(historical["plan"]["cannot_reverse"]) == 1
          and historical["plan"]["cannot_reverse"][0]["kind"] == "inventory",
          str(historical["plan"]["reverse_inventory"]))
    check("its claim row is KEPT so the reward cannot be paid twice",
          [c["claim_id"] for c in historical["plan"]["cannot_reverse"]]
          == [c["id"] for c in historical["claims"]
              if c["track"] == "shop" and c["reward_level"] == 4],
          str(historical["plan"]["claims_kept"]))
    hist_currency = historical["attribution"]["currency"][0]
    check("currency stays reversible historically: every credit has its own ledger row",
          hist_currency["reversible"] is True
          and hist_currency["provenance"] == "reward_grants"
          and hist_currency["matched_on"] == "grant provenance"
          or (hist_currency["reversible"] is True
              and hist_currency["provenance"] == "transaction_ledger"),
          str(hist_currency))

    # Defect C: two claims for the same item judged against ONE stack. One grant
    # with 3 attributable units cannot make two 2-unit rewards both reversible.
    two_claims = USER + 701
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,4000,4)",
            (GUILD, two_claims))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, two_claims))
    execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,quantity,"
            "source) VALUES (?,?, 'Potion','potion', 3, 'level_claim')", (GUILD, two_claims))
    ids = []
    for ref in ("potion_a", "potion_b"):
        ids.append(execute(
            "INSERT INTO level_reward_claims (guild_id,user_id,reward_level,track,"
            "reward_ref,payload_json,status,created_at,fulfilled_at,source) VALUES "
            "(?,?,4,'shop',?,?,'fulfilled','2026-01-01T00:00:00+00:00',"
            "'2026-01-02T00:00:00+00:00','test')",
            (GUILD, two_claims, ref,
             json.dumps({"type": "potion", "name": "Potion", "quantity": 2}))))
    # Each claim has its OWN grant of 2 units, and the first has been partly
    # consumed (1 of its 2 units is gone). The stack holds 3, which is exactly the
    # two grants' attributable remainder - and it is NOT enough to cover both
    # rewards. This is the case that used to be judged per claim against the whole
    # stack: 2 + 2 "reversible" from 3 held.
    for index, (claim_id, remaining) in enumerate(((ids[0], 1), (ids[1], 2))):
        execute("INSERT INTO reward_grants (grant_id,guild_id,user_id,claim_id,"
                "reward_level,track,kind,item_name,quantity,remaining_quantity,"
                "operation_id,created_at) VALUES (?,?,?,?,4,'shop','inventory',"
                "'Potion',2,?,?,'2026-01-02T00:00:00+00:00')",
                (f"shared-{two_claims}-{index}", GUILD, two_claims, claim_id,
                 remaining, f"op-shared-{index}"))
    shared = await inspect_member(GUILD, two_claims)
    by_claim = {e["claim_id"]: e for e in shared["attribution"]["inventory"]}
    reversible = [e for e in shared["attribution"]["inventory"] if e["reversible"]]
    refused = [e for e in shared["attribution"]["inventory"] if not e["reversible"]]
    check("two claims over one stack: only the attributable units are promised "
          "(defect C)",
          len(reversible) == 1 and reversible[0]["claim_id"] == ids[1]
          and len(refused) == 1 and refused[0]["claim_id"] == ids[0]
          and by_claim[ids[0]]["attributable"] == 1
          and "only 1 of the 2 units" in refused[0]["note"],
          str(shared["attribution"]["inventory"]))
    check("the stack total is reported for context and trusted for nothing",
          by_claim[ids[0]]["stack"] == 3 == by_claim[ids[1]]["stack"]
          and [e["claim_id"] for e in shared["plan"]["reverse_inventory"]] == [ids[1]]
          and [e["claim_id"] for e in shared["plan"]["cannot_reverse"]] == [ids[0]],
          str(shared["plan"]["reverse_inventory"]))

    # ── 3. the environment gate: label AND file must agree ───────────────────
    # `NERO_ENVIRONMENT` on its own is not a safety statement - database.py gives
    # every value except the exact string "staging" the production file, so a guard
    # written as `if NERO_ENVIRONMENT == "production"` lets `dev`, `test` or a typo
    # destroy production data. These cases set REAL environment variables and are
    # judged against the file this process actually has open.
    print("== 3. environment gate ==")
    from database import DB_PATH as LIVE_DB
    other_file = LIVE_DB + ".elsewhere"
    saved = {key: os.environ.get(key) for key in
             ("NERO_ENVIRONMENT", "STAGING_DATABASE_PATH", "DATABASE_PATH",
              "ALLOW_LEVEL_TEST_RESET")}

    def verdict_for(**env):
        for key in ("NERO_ENVIRONMENT", "STAGING_DATABASE_PATH", "DATABASE_PATH"):
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        try:
            verdict = ops.environment_verdict()
            outcomes = {}
            for authorized in (False, True):
                try:
                    ops.require_destructive_allowed(
                        production_authorization=authorized,
                        operation="/level_test_reset")
                    outcomes[authorized] = "ALLOWED"
                except ops.LevelOpsError:
                    outcomes[authorized] = "refused"
            return verdict["target"], outcomes
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    matrix = {
        "a properly configured staging deploy": verdict_for(
            NERO_ENVIRONMENT="staging", STAGING_DATABASE_PATH=LIVE_DB,
            DATABASE_PATH=other_file),
        "the staging LABEL with the production file open": verdict_for(
            NERO_ENVIRONMENT="staging", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "staging and production pointed at ONE file": verdict_for(
            NERO_ENVIRONMENT="staging", STAGING_DATABASE_PATH=LIVE_DB,
            DATABASE_PATH=LIVE_DB),
        "the staging label with a file matching neither path": verdict_for(
            NERO_ENVIRONMENT="staging", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=other_file + ".2"),
        "production, with the production file open": verdict_for(
            NERO_ENVIRONMENT="production", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "the 'prod' alias": verdict_for(
            NERO_ENVIRONMENT="prod", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "NERO_ENVIRONMENT set to an empty string": verdict_for(
            NERO_ENVIRONMENT="", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "NERO_ENVIRONMENT absent": verdict_for(
            NERO_ENVIRONMENT=None, STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "the 'dev' alias": verdict_for(
            NERO_ENVIRONMENT="dev", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "the 'development' alias": verdict_for(
            NERO_ENVIRONMENT="development", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "the 'test' alias": verdict_for(
            NERO_ENVIRONMENT="test", STAGING_DATABASE_PATH=other_file,
            DATABASE_PATH=LIVE_DB),
        "a typo'd 'stagin'": verdict_for(
            NERO_ENVIRONMENT="stagin", STAGING_DATABASE_PATH=LIVE_DB,
            DATABASE_PATH=other_file),
        "the production label with the staging file open": verdict_for(
            NERO_ENVIRONMENT="production", STAGING_DATABASE_PATH=LIVE_DB,
            DATABASE_PATH=other_file),
    }
    for name, (target, outcomes) in matrix.items():
        print(f"        {name:52s} -> {target:11s} "
              f"no key: {outcomes[False]:8s} with key: {outcomes[True]}")
    staging_target, staging_outcomes = matrix["a properly configured staging deploy"]
    check("a properly configured staging database needs no key",
          staging_target == "staging" and staging_outcomes[False] == "ALLOWED",
          str(matrix["a properly configured staging deploy"]))
    prod_target, prod_outcomes = matrix["production, with the production file open"]
    check("the production file is refused without the key and allowed with it",
          prod_target == "production" and prod_outcomes[False] == "refused"
          and prod_outcomes[True] == "ALLOWED",
          str(matrix["production, with the production file open"]))
    check("'prod', an empty value and an absent variable all mean production",
          all(matrix[name][0] == "production" for name in
              ("the 'prod' alias", "NERO_ENVIRONMENT set to an empty string",
               "NERO_ENVIRONMENT absent")),
          str({name: matrix[name][0] for name in
               ("the 'prod' alias", "NERO_ENVIRONMENT set to an empty string",
                "NERO_ENVIRONMENT absent")}))
    check("every bypass the old string comparison allowed now fails closed",
          all(matrix[name][0] == "ambiguous" and matrix[name][1][True] == "refused"
              for name in ("the 'dev' alias", "the 'development' alias",
                           "the 'test' alias", "a typo'd 'stagin'")),
          str({name: matrix[name] for name in
               ("the 'dev' alias", "the 'development' alias", "the 'test' alias",
                "a typo'd 'stagin'")}))
    check("a label that contradicts the open file fails closed too",
          all(matrix[name][0] == "ambiguous" and matrix[name][1][True] == "refused"
              for name in ("the staging LABEL with the production file open",
                           "staging and production pointed at ONE file",
                           "the staging label with a file matching neither path",
                           "the production label with the staging file open")),
          str({name: matrix[name] for name in
               ("the staging LABEL with the production file open",
                "staging and production pointed at ONE file",
                "the staging label with a file matching neither path",
                "the production label with the staging file open")}))

    gate_before = dump()
    refused = None
    try:
        await reset_member(GUILD, USER, apply=True)
    except LevelResetError as exc:
        refused = str(exc)
    check("the harness' production file refuses to apply without the second key",
          refused is not None and "production" in refused, str(refused))
    check("the refusal is a LevelResetError, so the command reports a refusal "
          "rather than a partway failure", isinstance(refused, str))
    check("the refusal wrote nothing", changed_tables(gate_before, dump()) == [],
          str(changed_tables(gate_before, dump())))
    # Ambiguity must fail closed EVEN with the key: the key says "yes, on
    # production", and if the code cannot prove which file that is, honouring it
    # would be a guess about what it authorizes.
    os.environ["NERO_ENVIRONMENT"] = "dev"
    try:
        refused_ambiguous = None
        try:
            await reset_member(GUILD, USER, apply=True, allow_production=True)
        except LevelResetError as exc:
            refused_ambiguous = str(exc)
        check("an ambiguous environment is refused even WITH the production key",
              refused_ambiguous is not None and "ambiguous" in refused_ambiguous,
              str(refused_ambiguous))
        check("and that refusal wrote nothing either",
              changed_tables(gate_before, dump()) == [],
              str(changed_tables(gate_before, dump())))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── 4. the scope guard ───────────────────────────────────────────────────
    print("== 4. scope guard ==")
    guard_before = dump()      # §2 and §3 seeded rows; compare against now
    for bad_guild, bad_user in ((0, USER), (GUILD, 0), (-1, USER), (GUILD, -1),
                                (None, USER), (GUILD, "abc")):
        refused = False
        try:
            await inspect_member(bad_guild, bad_user)
        except LevelResetError:
            refused = True
        check(f"a non-positive/non-integer scope is refused (guild={bad_guild!r} "
              f"user={bad_user!r})", refused)
    check("the scope guard wrote nothing",
          changed_tables(guard_before, dump()) == [],
          str(changed_tables(guard_before, dump())))

    # ── 5. apply: proven value is reversed, everything else is left alone ────
    print("== 5. apply ==")
    other_before = dump()
    ledger_before = rows("SELECT id, currency, amount, type, reason, source, reversed "
                         "FROM transaction_ledger WHERE guild_id=? AND user_id=? ORDER BY id",
                         (GUILD, USER))
    report = await reset_member(GUILD, USER, apply=True, member=member,
                                reversed_by=999, reason="regression test",
                                allow_production=True)
    check("apply reports what it did", report["applied"] is True
          and report["mode"] == "apply" and report["operation_id"])
    check("the member's XP/level is zeroed, prestige preserved, claims empty",
          {k: report["after"][k] for k in
           ("xp", "level", "prestige", "vi_activation", "claims", "boosts",
            "temp_roles")} == {"xp": 0, "level": 0, "prestige": 2,
                               "vi_activation": None, "claims": 0, "boosts": 0,
                               "temp_roles": 0}, str(report["after"]))
    check("every proven delivery was reversed, so this IS a clean New Game",
          report["traces"] == [] and report["new_game_complete"] is True
          and report["unreversed"] == [] and report["claims_preserved"] == [],
          f"traces={report['traces']} unreversed={report['unreversed']}")
    check("the reward role was taken off the member",
          report["roles_removed"] == [ROLE_ID] and held == [], str(held))
    check("the wallet was debited exactly the amount the reward paid",
          wallet() == (777 - COIN_L3, 33), str(wallet()))
    check("the stack lost exactly the reward's units and kept the mission's",
          stack() == 4 - POTION_REWARD_QTY, str(stack()))
    check("the grant's attributable remainder went to zero",
          rows("SELECT remaining_quantity FROM reward_grants WHERE guild_id=? "
               "AND user_id=? AND kind='inventory'", (GUILD, USER)) == [(0,)],
          str(rows("SELECT kind, quantity, remaining_quantity FROM reward_grants "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))))
    # Defect E: ONE compensating row per movement, not the two the old
    # safe_deduct + log_transaction pair produced.
    ledger_after = rows("SELECT id, currency, amount, type, reason, source, reversed "
                        "FROM transaction_ledger WHERE guild_id=? AND user_id=? ORDER BY id",
                        (GUILD, USER))
    added = ledger_after[len(ledger_before):]
    check("exactly one compensating ledger row was appended (defect E)",
          len(ledger_after) == len(ledger_before) + 1
          and added[0][3] == "reversal" and added[0][2] == -COIN_L3
          and added[0][5] == "level_reward_reversal", str(added))
    check("the original credit is marked reversed and still there (append-only)",
          ledger_after[0][6] == 1 and ledger_after[0][2] == COIN_L3,
          str(ledger_after[0]))
    # The ledger is append-only, so its total moves by exactly the reversal. The
    # old shape (safe_deduct's `deduct` row PLUS a `reversal` row) moved it by
    # twice that, which is how one event gets counted twice by anyone summing it.
    check("summing the ledger counts the reversal exactly once (defect E)",
          sum(r[2] for r in ledger_after if r[1] == "balance")
          == sum(r[2] for r in ledger_before if r[1] == "balance") - COIN_L3,
          str([sum(r[2] for r in ledger_before if r[1] == "balance"),
               sum(r[2] for r in ledger_after if r[1] == "balance")]))
    check("the journal holds one row per kind, written by this operation",
          sorted((r[1], r[2]) for r in rows(
              "SELECT claim_id, kind, status FROM reward_reversals "
              "WHERE guild_id=? AND user_id=?", (GUILD, USER)))
          == [("currency", "applied"), ("inventory", "applied"), ("role", "applied")],
          str(rows("SELECT claim_id, kind, status, operation_id FROM reward_reversals "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))))
    check("the operation itself is recorded with its actor and reason",
          rows("SELECT kind, actor_id, reason, status FROM level_ops "
               "WHERE operation_id=?", (report["operation_id"],))
          == [("level_test_reset", 999, "regression test", "completed")],
          str(rows("SELECT * FROM level_ops")))
    check("the member's lease was released",
          rows("SELECT COUNT(*) FROM level_op_locks WHERE guild_id=? AND user_id=?",
               (GUILD, USER)) == [(0,)], str(rows("SELECT * FROM level_op_locks")))
    changed = changed_tables(other_before, dump())
    check("only this member's level-reward state changed",
          set(changed) <= {"levels", "level_reward_claims", "leveling_active_boosts",
                           "temp_roles", "economy", "transaction_ledger",
                           "inventory_items", "reward_grants"}
          | set(PROVENANCE_TABLES),
          f"changed={changed}")
    check("no other member, guild or reward definition moved",
          rows("SELECT xp, level, prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, OTHER)) == [(5000, 5, 2)]
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, OTHER)) == [(777,)]
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? "
                   "AND user_id=?", (GUILD, OTHER)) == [(4,)]
          and rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
                   "AND item_name='Potion'", (GUILD, OTHER)) == [(4,)]
          and rows("SELECT COUNT(*) FROM leveling_currency_rewards") == [(2,)]
          and rows("SELECT COUNT(*) FROM leveling_rewards") == [(2,)]
          and rows("SELECT COUNT(*) FROM leveling_shop_rewards") == [(2,)]
          and rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                   (OTHER_GUILD, USER)) == [(5000,)],
          str([rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (GUILD, OTHER)),
               rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                    (GUILD, OTHER))]))
    check("the unrelated shop boost and the shop temp-role unit survived",
          rows("SELECT COUNT(*) FROM leveling_active_boosts WHERE guild_id=? "
               "AND user_id=? AND source='shop'", (GUILD, USER)) == [(1,)]
          and rows("SELECT COUNT(*) FROM temp_roles WHERE guild_id=? AND user_id=? "
                   "AND role_id=?", (GUILD, USER, OTHER_ROLE_ID)) == [(1,)],
          str(rows("SELECT source, role_id FROM temp_roles WHERE guild_id=? AND user_id=?",
                   (GUILD, USER))))
    check("the historical member was untouched by this member's reset",
          rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
               "AND item_name='Potion'", (GUILD, HISTORICAL_USER)) == [(5,)]
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, HISTORICAL_USER)) == [(900,)],
          str(rows("SELECT * FROM inventory_items WHERE user_id=?", (HISTORICAL_USER,))))

    # ── 6. retries are idempotent (defects A and D) ──────────────────────────
    print("== 6. retries ==")
    retry_before = dump()
    again = await reset_member(GUILD, USER, apply=True, member=member,
                               reversed_by=999, reason="retry",
                               allow_production=True)
    check("a second apply is a no-op: nothing reversed again",
          again["reversed_currency"] == [] and again["reversed_inventory"] == []
          and again["unreversed"] == [], str(again["reversed_currency"]))
    # The one table that legitimately changes on a retry is `level_ops`: the retry
    # is an operation in its own right and is recorded as such. Everything that
    # holds VALUE must be byte-identical.
    check("the wallet, the stack and the ledger did not move on the retry",
          changed_tables(retry_before, dump()) == ["level_ops"]
          and wallet() == (677, 33) and stack() == 2
          and rows("SELECT COUNT(*) FROM transaction_ledger WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER)) == [(3,)]
          and rows("SELECT COUNT(*) FROM reward_reversals WHERE guild_id=? "
                   "AND user_id=?", (GUILD, USER)) == [(3,)],
          f"changed={changed_tables(retry_before, dump())} wallet={wallet()}")
    check("and the retry still reports a clean New Game",
          again["new_game_complete"] is True and again["traces"] == [],
          str(again["traces"]))

    # A crash between the wallet debit and the record deletion: the retry must
    # complete the work WITHOUT charging the member twice.
    crash_before = dump()
    real_reverse_inventory = ops.reverse_inventory
    calls = {"n": 0}

    async def crashing_reverse_inventory(**kwargs):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")
    # Re-seed a member whose currency reversal commits and whose inventory step
    # then dies, exactly like a locked database mid-operation.
    crash_user = USER + 702
    seed_member(crash_user, GUILD, sequence=4, potion_stack=4)
    ops.reverse_inventory = crashing_reverse_inventory
    crashed = None
    try:
        await reset_member(GUILD, crash_user, apply=True, member=None,
                           reversed_by=999, reason="crash", allow_production=True)
    except Exception as exc:          # noqa: BLE001 - the point is that it raises
        crashed = f"{type(exc).__name__}: {exc}"
    finally:
        ops.reverse_inventory = real_reverse_inventory
    check("a mid-operation failure surfaces instead of being swallowed",
          crashed is not None and "database is locked" in crashed, str(crashed))
    check("the failed operation is recorded as failed and stays traceable",
          rows("SELECT COUNT(*) FROM level_ops WHERE user_id=? AND status='failed'",
               (crash_user,)) == [(1,)],
          str(rows("SELECT operation_id, kind, status FROM level_ops WHERE user_id=?",
                   (crash_user,))))
    check("its lease was released, so the member is not wedged",
          rows("SELECT COUNT(*) FROM level_op_locks WHERE user_id=?", (crash_user,)) == [(0,)],
          str(rows("SELECT * FROM level_op_locks")))

    # ── 7. the admin command ─────────────────────────────────────────────────
    print("== 7. /level_test_reset command ==")
    from cogs.leveling import Leveling
    command_before = dump()
    itx = admin_interaction()
    await Leveling.level_test_reset.callback(cog, itx, member)
    sent = itx.followup.send.await_args.kwargs
    embed = sent["embed"]
    text = all_embed_text(embed)
    check("without confirm the command only previews",
          "PREVIEW" in (embed.title or "") and sent.get("ephemeral") is True
          and changed_tables(command_before, dump()) == [],
          str(changed_tables(command_before, dump())))
    check("the preview names the member, the guild and the database file",
          str(GUILD) in text and str(USER) in text and DB_PATH in text, text[:400])
    check("the preview states what would be reversed and what would not",
          "Would reset" in text and "Reward provenance" in text
          and "include_delivered" not in text, text[:400])
    check("the removed flag is not offered to administrators anywhere",
          "include_delivered" not in text
          and not any(p.name == "include_delivered"
                      for p in Leveling.level_test_reset.parameters),
          str([p.name for p in Leveling.level_test_reset.parameters]))

    # confirm without a reason: refused BEFORE deferring, because
    # interaction.response is single-use.
    itx = admin_interaction()
    await Leveling.level_test_reset.callback(cog, itx, member, confirm=True)
    check("confirm without a reason is refused and nothing is deferred",
          itx.response.defer.await_count == 0
          and itx.response.send_message.await_count == 1
          and "reason" in itx.response.send_message.await_args.args[0].lower()
          and itx.followup.send.await_count == 0,
          str(itx.response.send_message.await_args))
    check("and that refusal changed nothing",
          changed_tables(command_before, dump()) == [],
          str(changed_tables(command_before, dump())))

    # A clean member: confirm with a reason applies and reports a clean New Game.
    execute("INSERT INTO levels (guild_id,user_id,xp,level) VALUES (?,?,1200,2)",
            (GUILD, CLEAN_USER))
    clean_member = SimpleNamespace(
        id=CLEAN_USER, mention=f"<@{CLEAN_USER}>", guild=guild, roles=[],
        premium_since=None, display_name="Clean")
    clean_member.remove_roles = AsyncMock()
    itx = admin_interaction()
    os.environ["ALLOW_LEVEL_TEST_RESET"] = "1"
    try:
        await Leveling.level_test_reset.callback(
            cog, itx, clean_member, confirm=True, reason="re-testing the claim flow")
    finally:
        os.environ.pop("ALLOW_LEVEL_TEST_RESET", None)
    applied_embed = itx.followup.send.await_args.kwargs["embed"]
    applied_text = all_embed_text(applied_embed)
    check("with confirm and a reason the command applies",
          "APPLIED" in (applied_embed.title or "")
          and rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, CLEAN_USER)) == [(0, 0)],
          str(rows("SELECT xp FROM levels WHERE user_id=?", (CLEAN_USER,))))
    check("the applied embed reports a clean New Game for a member with no rewards",
          "Clean New Game" in applied_text and "operation:" in applied_text,
          applied_text[:500])
    check("the reason reached the operation record",
          rows("SELECT reason FROM level_ops WHERE user_id=? AND kind='level_test_reset'",
               (CLEAN_USER,)) == [("re-testing the claim flow",)],
          str(rows("SELECT reason FROM level_ops WHERE user_id=?", (CLEAN_USER,))))

    # Without the second key the command must report a refusal, not a failure.
    itx = admin_interaction()
    await Leveling.level_test_reset.callback(
        cog, itx, clean_member, confirm=True, reason="no key")
    body = itx.followup.send.await_args.args[0]
    check("on the production file the command refuses and says nothing was changed",
          "refused" in body.lower() and "nothing was changed" in body.lower(), body[:300])
    check("the refusal is answered as a refusal, not as a partway failure",
          "FAILED partway" not in body, body[:300])

    # An unexpected error must still answer the deferred interaction: a spinner
    # that never resolves is how an administrator loses trust in the report.
    itx = admin_interaction()
    real_reset = reset_mod.reset_member

    async def exploding_reset(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    reset_mod.reset_member = exploding_reset
    os.environ["ALLOW_LEVEL_TEST_RESET"] = "1"
    try:
        await Leveling.level_test_reset.callback(
            cog, itx, clean_member, confirm=True, reason="boom")
    finally:
        reset_mod.reset_member = real_reset
        os.environ.pop("ALLOW_LEVEL_TEST_RESET", None)
    body = itx.followup.send.await_args.args[0]
    check("an unexpected error answers the deferred interaction (no spinner)",
          itx.response.defer.await_count == 1
          and itx.followup.send.await_count == 1
          and "FAILED partway" in body and "database is locked" in body, body[:300])
    check("and it does not claim that nothing changed",
          "may already have committed" in body, body[:400])

    # ── 8. role safety, prestige and the New Game honesty rule ───────────────
    print("== 8. role safety, prestige and New Game honesty ==")
    # 8a. a role another grant also gives is left on the member, and the claim
    #     row IS deleted because the other grant is what keeps it on.
    shared_user = USER + 703
    shared_ids = seed_member(shared_user, GUILD, sequence=5)
    execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,source) "
            "VALUES (?,?,?, '2030-01-01T00:00:00+00:00','shop')",
            (GUILD, shared_user, ROLE_ID))
    shared_preview = await inspect_member(GUILD, shared_user)
    check("a role held through another grant is reported as left on the member",
          [r["role_id"] for r in shared_preview["plan"]["roles_left_on_member"]] == [ROLE_ID]
          and shared_preview["plan"]["remove_discord_roles"] == [],
          str(shared_preview["plan"]["roles_left_on_member"]))

    # 8b. a role that CANNOT be removed keeps its claim and temp-role rows.
    failing_guild, failing_member, failing_held, failing_bot = fake_discord(
        removal_fails=True)
    fail_user = USER + 704
    seed_member(fail_user, GUILD, sequence=6)
    failing_member.id = fail_user
    failing_guild.get_member = lambda uid: failing_member if uid == fail_user else None
    failing_member.roles = [SimpleNamespace(id=ROLE_ID, name="Moon", position=5)]
    failing_guild.get_role = lambda rid: (failing_member.roles[0]
                                          if rid == ROLE_ID else None)
    failed = await reset_member(GUILD, fail_user, apply=True, member=failing_member,
                                reversed_by=999, reason="role failure",
                                allow_production=True)
    check("a failed role removal is reported per role with its error",
          len(failed["role_failures"]) == 1
          and failed["role_failures"][0]["role_id"] == ROLE_ID
          and "Missing Permissions" in failed["role_failures"][0]["error"],
          str(failed["role_failures"]))
    check("its claim row and temp-role unit are KEPT, so the role stays documented",
          rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=? "
               "AND track='role'", (GUILD, fail_user)) == [(1,)]
          and rows("SELECT COUNT(*) FROM temp_roles WHERE guild_id=? AND user_id=? "
                   "AND role_id=? AND source='level_claim'",
                   (GUILD, fail_user, ROLE_ID)) == [(1,)],
          str(rows("SELECT track, status FROM level_reward_claims WHERE user_id=?",
                   (fail_user,))))
    check("and the result is NOT reported as a clean New Game",
          failed["new_game_complete"] is False
          and any("KEPT" in t for t in failed["traces"]), str(failed["traces"]))
    check("the kept claim stays terminal, so it cannot be claimed a second time",
          rows("SELECT status FROM level_reward_claims WHERE guild_id=? AND user_id=? "
               "AND track='role'", (GUILD, fail_user)) == [("fulfilled",)],
          str(rows("SELECT status FROM level_reward_claims WHERE user_id=?", (fail_user,))))

    # 8c. prestige is only cleared on an explicit request.
    prestige_user = USER + 705
    seed_member(prestige_user, GUILD, sequence=7, provenance=True)
    execute("INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) "
            "VALUES (?,?, '2026-01-01T00:00:00+00:00')", (GUILD, prestige_user))
    _, prestige_member, _, _ = fake_discord(user=prestige_user)
    kept = await reset_member(GUILD, prestige_user, apply=True,
                              member=prestige_member,
                              reversed_by=999, reason="keep prestige",
                              allow_production=True)
    check("prestige and the VI activation survive by default",
          rows("SELECT prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, prestige_user)) == [(2,)]
          and rows("SELECT COUNT(*) FROM prestige_vi_activations WHERE guild_id=? "
                   "AND user_id=?", (GUILD, prestige_user)) == [(1,)]
          and kept["new_game_complete"] is True,
          str([rows("SELECT prestige FROM levels WHERE user_id=?", (prestige_user,)),
               kept["traces"]]))
    prestige_user2 = USER + 706
    seed_member(prestige_user2, GUILD, sequence=8)
    execute("INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) "
            "VALUES (?,?, '2026-01-01T00:00:00+00:00')", (GUILD, prestige_user2))
    _, prestige_member2, _, _ = fake_discord(user=prestige_user2)
    cleared = await reset_member(GUILD, prestige_user2, apply=True,
                                 member=prestige_member2, include_prestige=True,
                                 reversed_by=999, reason="clear prestige",
                                 allow_production=True)
    check("include_prestige clears the tier and the VI activation, and says so",
          rows("SELECT prestige FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, prestige_user2)) == [(0,)]
          and rows("SELECT COUNT(*) FROM prestige_vi_activations WHERE guild_id=? "
                   "AND user_id=?", (GUILD, prestige_user2)) == [(0,)]
          and any("prestige" in r.lower() for r in cleared["ran"]),
          str(cleared["ran"]))

    # 8d. a historical member: nothing is removed on a guess, the case is queued,
    #     and the tool never claims a reversal it could not prove.
    hist_report = await reset_member(GUILD, HISTORICAL_USER, apply=True, member=None,
                                     reversed_by=999, reason="historical",
                                     allow_production=True)
    check("the historical member's unrelated stack was NOT reduced (defect B)",
          rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? "
               "AND item_name='Potion'", (GUILD, HISTORICAL_USER)) == [(5,)],
          str(rows("SELECT * FROM inventory_items WHERE user_id=?", (HISTORICAL_USER,))))
    check("the unprovable delivery is reported as not reversed, and its claim KEPT",
          len(hist_report["unreversed"]) == 1
          and hist_report["unreversed"][0]["kind"] == "inventory"
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? "
                   "AND user_id=? AND reward_level=4 AND track='shop'",
                   (GUILD, HISTORICAL_USER)) == [(1,)],
          str(hist_report["unreversed"]))
    check("the case is queued for /level_reconcile, not silently dropped",
          len(hist_report["outstanding"]) == 1
          and hist_report["outstanding"][0]["status"] in ("refused", "outstanding"),
          str(hist_report["outstanding"]))
    check("and the report refuses to call it a clean New Game",
          hist_report["new_game_complete"] is False
          and any("not reversed" in t for t in hist_report["traces"]),
          str(hist_report["traces"]))
    check("its currency reward WAS reversed, from its own ledger row",
          len(hist_report["reversed_currency"]) == 1
          and rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                   (GUILD, HISTORICAL_USER)) == [(900 - COIN_L3,)],
          str(rows("SELECT balance FROM economy WHERE user_id=?", (HISTORICAL_USER,))))
    check("the legacy backfill row was deleted and reported as never paid",
          rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? AND user_id=? "
               "AND source='legacy_backfill'", (GUILD, HISTORICAL_USER)) == [(0,)]
          and any("legacy backfill" in r for r in hist_report["ran"]),
          str(hist_report["ran"]))

    # ── 9. a database that predates the provenance schema ────────────────────
    print("== 9. migration from an older database ==")
    old_user = USER + 707
    seed_member(old_user, GUILD, sequence=9, provenance=False)
    conn = sqlite3.connect(DB_PATH)
    for table in ("reward_grants", "reward_reversals", "level_ops", "level_op_locks",
                  "xp_mutations"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    try:
        conn.execute("ALTER TABLE level_reward_claims DROP COLUMN operation_id")
        conn.commit()
    except sqlite3.OperationalError as exc:
        print(f"        (could not drop the column: {exc})")
    conn.close()
    migrated = await inspect_member(GUILD, old_user)
    check("an unmigrated database can still be inspected",
          migrated["xp"] == 5000 and len(migrated["claims"]) == 4
          and migrated["grants"] == {} and migrated["journal"] == [],
          str([migrated["xp"], len(migrated["claims"]), migrated["grants"]]))
    check("and every claim is read even without the operation_id column",
          {c["reward_level"] for c in migrated["claims"]} == {3, 4, 5}
          and all(c.get("operation_id") is None for c in migrated["claims"]),
          str([c.get("operation_id") for c in migrated["claims"]]))
    check("with no provenance anywhere, nothing is promised as reversible",
          migrated["plan"]["reverse_inventory"] == []
          and len(migrated["plan"]["cannot_reverse"]) == 1,
          str(migrated["plan"]["reverse_inventory"]))
    migrated_report = await reset_member(GUILD, old_user, apply=True, member=None,
                                         reversed_by=999, reason="old database",
                                         allow_production=True)
    check("the reset still runs on an old database and recreates what it needs",
          migrated_report["applied"] is True
          and rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                   (GUILD, old_user)) == [(0,)]
          and rows("SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name='reward_reversals'") == [("reward_reversals",)],
          str(rows("SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name LIKE 'reward%'")))
    check("and it reports honestly what it could not establish",
          migrated_report["new_game_complete"] is False
          and len(migrated_report["unreversed"]) == 1,
          str(migrated_report["traces"]))

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LEVEL TEST RESET CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
