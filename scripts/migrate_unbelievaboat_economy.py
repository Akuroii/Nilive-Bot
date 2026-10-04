#!/usr/bin/env python3
"""
ONE-TIME economy migration: UnbelievaBoat balances -> this bot's primary
currency, for ONE guild only (server id 1280983591148584971).

DRY-RUN IS THE DEFAULT. Nothing is written unless BOTH flags are given:

    --execute --confirm-guild 1280983591148584971

Usage (run from the project root, on the machine that has the production DB):

    # 1) dry run -- read-only, prints CREATE/UPDATE split and
    #    current -> migration -> final for every user:
    python3 scripts/migrate_unbelievaboat_economy.py --data nilive_unbelievaboat_balances.json

    # 2) real run (only after you have reviewed the dry run):
    python3 scripts/migrate_unbelievaboat_economy.py --data nilive_unbelievaboat_balances.json \\
        --execute --confirm-guild 1280983591148584971

Optional: --report-csv PATH (write the per-user table), --summary-only.

What it does (and only this)
----------------------------
* Reads the UnbelievaBoat export. Identity key is the Discord `user_id`
  (never the username). No Discord/membership lookup of any kind: users who
  have left the server are migrated exactly like current members.
* Rules: use `total`; ignore null/missing, negative and < 10; converted
  amount = total / 10 rounded half-up with exact integer maths
  ((total + 5) // 10): 10->1, 15->2, 19->2, 20->2, 24->2, 25->3.
* ADDS the converted amount to the user's existing `economy.balance` for
  guild 1280983591148584971 (never replaces it), through the project's own
  utils.economy_safe.safe_credit() -- an additive upsert that also writes a
  transaction_ledger row. `balance` is the stable key of the guild's primary
  dynamic currency, so whatever the guild has named it is what gets credited:
  no currency is created and no name is hardcoded. `diamonds` is never touched.

Protections
-----------
* Guild isolation: the guild id is a constant in this file; every statement is
  parametrised on it; `--confirm-guild` must equal it; before/after
  fingerprints of every OTHER guild's economy/ledger rows are compared inside
  the transaction and any difference rolls everything back.
* One transaction: guard check + plan + all economy upserts + all ledger
  inserts + verification run inside ONE `BEGIN IMMEDIATE`. Any error,
  exception, mismatch or Ctrl-C -> ROLLBACK. A hard process kill leaves
  nothing behind either (an uncommitted SQLite transaction is discarded).
* One-time guard: refuses to run if any ledger row with
  source='unbelievaboat_migration' already exists for this guild. The ledger
  rows are committed atomically with the credits, so a re-run after success is
  rejected and cannot double-credit.
* Unsafe existing balances (NULL / non-integer / negative) for any affected
  user abort the run before anything is written (safe_credit would silently
  lose a credit on a NULL balance).
* Backup (execute only): before the transaction, an online-backup copy of the
  database is written to <dir of DB_PATH>/backups/pre_unbelievaboat_migration_*.db
  and verified. It is NOT registered in backup_log, so the daily backup cog's
  7-file pruning never deletes it.
* Data fingerprint: the export must match the reviewed file (515 records /
  511 eligible / 528,238 -> 52,828); anything else aborts.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import shutil
import sys
import time
import uuid

# ── Constants (the ONLY place the target is defined) ────────────────────
TARGET_GUILD_ID = 1280983591148584971
LEDGER_SOURCE = "unbelievaboat_migration"
LEDGER_REASON = "UnbelievaBoat migration (total / 10, rounded half-up)"
CURRENCY_COLUMN = "balance"          # stable key of the primary dynamic currency
MIN_TOTAL = 10
CHUNK = 300                          # IN (...) chunk size, below any SQLite limit

# The reviewed export. Content-based, so re-saving/re-formatting the file is
# fine; changing any record is not.
EXPECTED = {
    "imported": 515,
    "ignored_null": 0,
    "ignored_negative": 1,
    "ignored_below_10": 3,
    "eligible": 511,
    "original_total": 528238,
    "converted_total": 52828,
    "fingerprint": "c8bed09008cd4b1eea09c4664fe5da03813d41c520537f3b160d9f0298b3e71b",
}
EXPECTED_FILE_SHA256 = "b0d522f830e151197c8359025f68e19c28c85c6aa8c6afa16be58b8f83085472"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MigrationError(Exception):
    """Anything that must stop the run. Never leaves partial writes."""


def _p(*parts) -> None:
    text = " ".join(str(x) for x in parts)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


# ═════════════════════════════════════════════════════════════════════════
# 1. MIGRATION DATA (pure, no DB)
# ═════════════════════════════════════════════════════════════════════════

def convert(total: int) -> int:
    """total / 10 rounded to nearest, halves up, exact integer arithmetic.
    (Python's round() is banker's rounding -- 25/10 -> 2 -- so it is not used.)"""
    return (total + 5) // 10


def load_migration_data(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise MigrationError(f"cannot read data file {path!r}: {exc}")
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise MigrationError(f"data file is not valid JSON: {exc}")
    if not isinstance(data, list):
        raise MigrationError("data file must be a JSON list of records")

    seen: dict[int, int] = {}
    pairs: list[tuple[int, int | None]] = []
    eligible: list[tuple[int, int, int]] = []   # (user_id, original_total, converted)
    ignored = {"null": [], "negative": [], "below_10": []}
    dup_ids: list[int] = []

    for idx, rec in enumerate(data):
        if not isinstance(rec, dict):
            raise MigrationError(f"record #{idx} is not an object")
        raw_uid = rec.get("user_id")
        if isinstance(raw_uid, bool):
            raise MigrationError(f"record #{idx}: unusable user_id {raw_uid!r}")
        if isinstance(raw_uid, int):
            uid = raw_uid
        elif isinstance(raw_uid, str) and raw_uid.isascii() and raw_uid.isdigit():
            uid = int(raw_uid)
        else:
            raise MigrationError(f"record #{idx}: unusable user_id {raw_uid!r} "
                                 "(cannot be safely migrated)")
        if not 0 < uid < 2 ** 63:
            raise MigrationError(f"record #{idx}: user_id {uid} outside SQLite INTEGER range")
        if uid in seen:
            dup_ids.append(uid)
        seen[uid] = idx

        total = rec.get("total")
        pairs.append((uid, total if isinstance(total, int) and not isinstance(total, bool) else None))
        if total is None:
            ignored["null"].append(uid)
        elif isinstance(total, bool) or not isinstance(total, int):
            raise MigrationError(f"record #{idx} (user {uid}): non-integer total {total!r} "
                                 "(cannot be safely migrated)")
        elif total < 0:
            ignored["negative"].append(uid)
        elif total < MIN_TOTAL:
            ignored["below_10"].append(uid)
        else:
            eligible.append((uid, total, convert(total)))

    if dup_ids:
        raise MigrationError(f"duplicate/conflicting user_ids in data file: {sorted(set(dup_ids))[:10]}")

    fingerprint = hashlib.sha256(
        "\n".join(f"{u}:{'null' if t is None else t}" for u, t in sorted(pairs)).encode()
    ).hexdigest()

    result = {
        "imported": len(data),
        "ignored_null": len(ignored["null"]),
        "ignored_negative": len(ignored["negative"]),
        "ignored_below_10": len(ignored["below_10"]),
        "eligible": len(eligible),
        "original_total": sum(t for _, t, _ in eligible),
        "converted_total": sum(c for _, _, c in eligible),
        "fingerprint": fingerprint,
        "records": eligible,
        "ignored": ignored,
        "file_sha256": file_sha,
    }
    for key, want in EXPECTED.items():
        if result[key] != want:
            raise MigrationError(
                f"data does not match the reviewed export: {key} = {result[key]!r}, "
                f"expected {want!r}. Refusing to continue.")
    if file_sha != EXPECTED_FILE_SHA256:
        _p("NOTE: file bytes differ from the reviewed file (re-saved/re-formatted?) "
           "but its CONTENT fingerprint matches -> continuing.")
    return result


# ═════════════════════════════════════════════════════════════════════════
# 2. READ-ONLY DB HELPERS (all parametrised on TARGET_GUILD_ID)
# ═════════════════════════════════════════════════════════════════════════

async def _columns(db, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")   # table name is a literal
    return {r[1] for r in await cur.fetchall()}


async def _fetch_existing(db, uids: list[int]) -> dict[int, tuple]:
    out: dict[int, tuple] = {}
    for i in range(0, len(uids), CHUNK):
        chunk = uids[i:i + CHUNK]
        marks = ",".join("?" * len(chunk))
        cur = await db.execute(
            f"SELECT user_id, balance, typeof(balance), diamonds FROM economy "
            f"WHERE guild_id = ? AND user_id IN ({marks})",
            (TARGET_GUILD_ID, *chunk))
        for uid, bal, typ, dia in await cur.fetchall():
            out[int(uid)] = (bal, typ, dia)
    return out


async def build_plan(db, records: list[tuple[int, int, int]]) -> dict:
    """Read-only. Works on any connection (read-only handle or the locked
    write transaction). Returns the full plan plus blockers/warnings."""
    blockers: list[str] = []
    warnings: list[str] = []

    # schema sanity
    eco_cols = await _columns(db, "economy")
    led_cols = await _columns(db, "transaction_ledger")
    if not {"guild_id", "user_id", "balance", "diamonds"} <= eco_cols:
        raise MigrationError(f"economy table has unexpected columns: {sorted(eco_cols)}")
    if not {"guild_id", "user_id", "currency", "amount", "type", "reason", "source",
            "reversed"} <= led_cols:
        raise MigrationError(f"transaction_ledger has unexpected columns: {sorted(led_cols)}")

    # wrong-database guard
    row = await (await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM economy WHERE guild_id = ?",
        (TARGET_GUILD_ID,))).fetchone()
    guild_rows, guild_total = int(row[0]), int(row[1])
    if guild_rows == 0:
        warnings.append("target guild has NO existing economy rows in this database "
                        "(valid for a first migration: every user becomes a CREATE; "
                        "check Database/Environment above if unexpected)")
    anomalies = await (await db.execute(
        "SELECT COUNT(*) FROM economy WHERE guild_id = ? "
        "AND (balance IS NULL OR typeof(balance) != 'integer' OR balance < 0)",
        (TARGET_GUILD_ID,))).fetchone()

    # one-time guard
    n_prior = (await (await db.execute(
        "SELECT COUNT(*) FROM transaction_ledger WHERE guild_id = ? AND source = ?",
        (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchone())[0]
    if n_prior:
        blockers.append(f"ONE-TIME GUARD: {n_prior} ledger rows with source="
                        f"'{LEDGER_SOURCE}' already exist for this guild -> already migrated")

    # prior attempts that used another label
    strong = await (await db.execute(
        "SELECT id, user_id, source, reason FROM transaction_ledger "
        "WHERE guild_id = ? AND source != ? AND ("
        " lower(coalesce(source,'')) LIKE '%unbelieva%' OR lower(coalesce(reason,'')) LIKE '%unbelieva%')"
        " LIMIT 5", (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchall()
    if strong:
        blockers.append("possible earlier UnbelievaBoat import already in the ledger: "
                        + "; ".join(f"id={r[0]} user={r[1]} source={r[2]!r} reason={r[3]!r}" for r in strong))
    weak = await (await db.execute(
        "SELECT COUNT(*) FROM transaction_ledger WHERE guild_id = ? AND source != ? AND ("
        " lower(coalesce(source,'')) LIKE '%migrat%' OR lower(coalesce(reason,'')) LIKE '%migrat%'"
        " OR lower(coalesce(source,'')) LIKE '%import%' OR lower(coalesce(reason,'')) LIKE '%import%')",
        (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchone()
    if weak[0]:
        warnings.append(f"{weak[0]} existing ledger row(s) in this guild mention migrate/import "
                        "(not blocking -- review if unexpected)")

    # per-user plan
    uids = [u for u, _, _ in records]
    existing = await _fetch_existing(db, uids)
    plan = []
    for uid, original, amount in records:
        got = existing.get(uid)
        if got is None:
            plan.append(dict(uid=uid, action="CREATE", current=0, amount=amount,
                             final=amount, original=original, diamonds=None))
            continue
        bal, typ, dia = got
        if bal is None or typ != "integer":
            blockers.append(f"UNSAFE existing balance for user {uid}: value={bal!r} type={typ} "
                            "(NULL/non-integer) -- safe_credit would lose the credit")
            continue
        if bal < 0:
            blockers.append(f"NEGATIVE existing balance for user {uid}: {bal}")
            continue
        plan.append(dict(uid=uid, action="UPDATE", current=int(bal), amount=amount,
                         final=int(bal) + amount, original=original, diamonds=dia))

    # possible manual pre-credit of the same amount (non-blocking hint)
    suspects = []
    for p in plan:
        if p["amount"] >= 50:
            hit = await (await db.execute(
                "SELECT id FROM transaction_ledger WHERE guild_id = ? AND user_id = ? "
                "AND currency = 'balance' AND type = 'credit' AND amount = ? AND reversed = 0 LIMIT 1",
                (TARGET_GUILD_ID, p["uid"], p["amount"]))).fetchone()
            if hit:
                suspects.append(p["uid"])
    if suspects:
        warnings.append(f"{len(suspects)} user(s) already have a ledger credit equal to their "
                        f"converted amount (possible manual pre-credit): {suspects[:8]}")

    create_n = sum(1 for p in plan if p["action"] == "CREATE")
    update_n = sum(1 for p in plan if p["action"] == "UPDATE")
    cur_total = sum(p["current"] for p in plan)
    mig_total = sum(p["amount"] for p in plan)
    return {
        "plan": plan, "blockers": blockers, "warnings": warnings,
        "create": create_n, "update": update_n,
        "affected_current_total": cur_total, "migration_total": mig_total,
        "affected_final_total": cur_total + mig_total,
        "guild_rows": guild_rows, "guild_total_before": guild_total,
        "guild_total_after": guild_total + mig_total,
        "guild_anomaly_rows": int(anomalies[0]),
    }


async def _target_snapshot(db) -> dict:
    """Everything in the target guild we must be able to account for afterwards."""
    eco = {int(u): (b, d) for u, b, d in await (await db.execute(
        "SELECT user_id, balance, diamonds FROM economy WHERE guild_id = ?",
        (TARGET_GUILD_ID,))).fetchall()}
    led = await (await db.execute(
        "SELECT COUNT(*), TOTAL(amount), TOTAL(id) FROM transaction_ledger "
        "WHERE guild_id = ? AND source != ?", (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchone()
    return {"economy": eco, "ledger_other_sources": tuple(led)}


async def _others_fingerprint(db) -> tuple:
    """Fingerprint of EVERY OTHER guild's economy + ledger rows."""
    h = hashlib.sha256()
    cur = await db.execute(
        "SELECT guild_id, user_id, balance, diamonds FROM economy WHERE guild_id != ? "
        "ORDER BY guild_id, user_id", (TARGET_GUILD_ID,))
    n = 0
    async for row in cur:
        h.update(repr(tuple(row)).encode())
        n += 1
    led = await (await db.execute(
        "SELECT COUNT(*), TOTAL(amount), TOTAL(id), MAX(id) FROM transaction_ledger "
        "WHERE guild_id != ?", (TARGET_GUILD_ID,))).fetchone()
    return (n, h.hexdigest(), tuple(led))


# ═════════════════════════════════════════════════════════════════════════
# 3. REPORTING
# ═════════════════════════════════════════════════════════════════════════

def print_report(data: dict, info: dict, *, db_path: str, env: str, currency: str | None,
                 summary_only: bool, mode: str) -> None:
    _p("=" * 78)
    _p(f"UnbelievaBoat -> economy migration   MODE: {mode}")
    _p("=" * 78)
    _p(f"Environment : {env}")
    _p(f"Database    : {db_path}")
    _p(f"Target guild: {TARGET_GUILD_ID}   (the ONLY guild that can be modified)")
    _p(f"Currency    : primary column '{CURRENCY_COLUMN}'" + (f"  -> displayed as {currency}" if currency else ""))
    _p("-" * 78)
    _p("DATA FILE")
    _p(f"  imported records        : {data['imported']}")
    _p(f"  ignored null/missing    : {data['ignored_null']}")
    _p(f"  ignored negative        : {data['ignored_negative']}  {data['ignored']['negative']}")
    _p(f"  ignored below 10        : {data['ignored_below_10']}  {data['ignored']['below_10']}")
    _p(f"  eligible                : {data['eligible']}")
    _p(f"  original total          : {data['original_total']:,}")
    _p(f"  converted total (/10)   : {data['converted_total']:,}")
    _p("  duplicate user_ids      : 0   (a duplicate would have aborted)")
    _p("-" * 78)
    if not summary_only:
        _p("PER-USER PLAN   (user_id | action | current -> +migration -> final)")
        for p in info["plan"]:
            _p(f"  {p['uid']:>19} | {p['action']:<6} | {p['current']:>12,} -> +{p['amount']:>7,} -> {p['final']:>12,}")
        _p("-" * 78)
    _p("DATABASE EFFECT (target guild only)")
    _p(f"  CREATE (new economy rows)        : {info['create']}")
    _p(f"  UPDATE (add to existing balance) : {info['update']}")
    _p(f"  affected users' current total    : {info['affected_current_total']:,}")
    _p(f"  total migration amount           : {info['migration_total']:,}")
    _p(f"  affected users' final total      : {info['affected_final_total']:,}")
    _p(f"  WHOLE-GUILD balance total  before: {info['guild_total_before']:,}  ({info['guild_rows']} rows)")
    _p(f"  WHOLE-GUILD balance total  after : {info['guild_total_after']:,}")
    _p(f"  economy rows touched             : {len(info['plan'])}   (balance column only)")
    _p(f"  ledger rows to be inserted       : {len(info['plan'])}   (source='{LEDGER_SOURCE}')")
    _p(f"  guild-wide rows with NULL/non-int/negative balance (info): {info['guild_anomaly_rows']}")
    for w in info["warnings"]:
        _p(f"  WARNING: {w}")
    if info["blockers"]:
        _p("-" * 78)
        _p("BLOCKERS -- --execute would be REFUSED:")
        for b in info["blockers"]:
            _p(f"  X {b}")
    _p("=" * 78)


def write_csv(path: str, plan: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "action", "current_balance", "migration_amount", "final_balance",
                    "ub_total"])
        for p in plan:
            w.writerow([p["uid"], p["action"], p["current"], p["amount"], p["final"], p["original"]])


# ═════════════════════════════════════════════════════════════════════════
# 4. BACKUP (execute only)
# ═════════════════════════════════════════════════════════════════════════

async def make_backup(aiosqlite, db_path: str) -> str:
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    fname = f"pre_unbelievaboat_migration_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.db"
    dest = os.path.join(backup_dir, fname)

    async with aiosqlite.connect(db_path) as live:
        await live.execute("PRAGMA busy_timeout = 30000")
        pre_eco = (await (await live.execute("SELECT COUNT(*) FROM economy")).fetchone())[0]
        pre_led = (await (await live.execute("SELECT COUNT(*) FROM transaction_ledger")).fetchone())[0]
        pre_t = (await (await live.execute(
            "SELECT COUNT(*) FROM economy WHERE guild_id = ?", (TARGET_GUILD_ID,))).fetchone())[0]
        async with aiosqlite.connect(dest) as dst:
            await live.backup(dst)            # same online-backup API cogs/backup.py uses

    # verify the copy (rows are never deleted from these tables, so counts can only grow)
    async with aiosqlite.connect(dest) as chk:
        ok = (await (await chk.execute("PRAGMA integrity_check")).fetchone())[0]
        b_eco = (await (await chk.execute("SELECT COUNT(*) FROM economy")).fetchone())[0]
        b_led = (await (await chk.execute("SELECT COUNT(*) FROM transaction_ledger")).fetchone())[0]
        b_t = (await (await chk.execute(
            "SELECT COUNT(*) FROM economy WHERE guild_id = ?", (TARGET_GUILD_ID,))).fetchone())[0]
    if ok != "ok" or b_eco < pre_eco or b_led < pre_led or b_t < pre_t:
        raise MigrationError(f"backup verification FAILED ({dest}): integrity={ok}, "
                             f"economy {b_eco}/{pre_eco}, ledger {b_led}/{pre_led}, guild {b_t}/{pre_t}")

    secondary = os.getenv("SECONDARY_BACKUP_DIR", "").strip()
    if secondary:
        try:
            os.makedirs(secondary, exist_ok=True)
            shutil.copy2(dest, os.path.join(secondary, fname))
            _p(f"  secondary copy written to {secondary}")
        except Exception as exc:
            _p(f"  WARNING: secondary copy failed ({exc}); primary backup is fine")
    return dest


# ═════════════════════════════════════════════════════════════════════════
# 5. EXECUTE (single transaction)
# ═════════════════════════════════════════════════════════════════════════

async def execute_migration(aiosqlite, es, db_path: str, records) -> dict:
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA busy_timeout = 30000")   # wait for live bot writers
        await db.execute("BEGIN IMMEDIATE")               # exclusive write lock from here to COMMIT
        try:
            # authoritative plan, computed UNDER THE LOCK (includes the one-time guard)
            info = await build_plan(db, records)
            if info["blockers"]:
                raise MigrationError("refused under lock: " + " | ".join(info["blockers"]))
            plan = info["plan"]
            if len(plan) != EXPECTED["eligible"] or info["migration_total"] != EXPECTED["converted_total"]:
                raise MigrationError("plan size/total differs from the reviewed preview")

            before_t = await _target_snapshot(db)
            before_others = await _others_fingerprint(db)

            for p in plan:
                await es.safe_credit(TARGET_GUILD_ID, p["uid"], p["amount"],
                                     currency=CURRENCY_COLUMN, reason=LEDGER_REASON,
                                     source=LEDGER_SOURCE, db=db)

            # ── verification, still inside the transaction ──────────────
            n, s = await (await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transaction_ledger "
                "WHERE guild_id = ? AND source = ?", (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchone()
            if n != len(plan) or s != info["migration_total"]:
                raise MigrationError(f"ledger mismatch: {n} rows / {s} vs {len(plan)} / {info['migration_total']} "
                                     "(a ledger insert failed silently)")
            multi = await (await db.execute(
                "SELECT user_id FROM transaction_ledger WHERE guild_id = ? AND source = ? "
                "GROUP BY user_id HAVING COUNT(*) != 1", (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchall()
            if multi:
                raise MigrationError(f"users with != 1 migration ledger row: {multi[:5]}")

            after_t = await _target_snapshot(db)
            planned = {p["uid"]: p for p in plan}
            for uid, (bal, dia) in after_t["economy"].items():
                if uid in planned:
                    p = planned[uid]
                    if bal != p["final"]:
                        raise MigrationError(f"user {uid}: balance {bal} != expected {p['final']}")
                    old_dia = before_t["economy"].get(uid, (None, None))[1]
                    if p["action"] == "UPDATE" and dia != old_dia:
                        raise MigrationError(f"user {uid}: diamonds changed {old_dia} -> {dia}")
                    if p["action"] == "CREATE" and dia not in (0, None):
                        raise MigrationError(f"user {uid}: new row has diamonds={dia}")
                elif before_t["economy"].get(uid) != (bal, dia):
                    raise MigrationError(f"target-guild user {uid} NOT in the migration was modified")
            missing = [u for u in planned if u not in after_t["economy"]]
            if missing:
                raise MigrationError(f"planned users missing after credit: {missing[:5]}")
            if len(after_t["economy"]) != len(before_t["economy"]) + info["create"]:
                raise MigrationError("unexpected number of target-guild economy rows")
            if after_t["ledger_other_sources"] != before_t["ledger_other_sources"]:
                raise MigrationError("existing (non-migration) target-guild ledger rows changed")
            if await _others_fingerprint(db) != before_others:
                raise MigrationError("OTHER GUILD DATA CHANGED -- aborting")

            await db.commit()
        except BaseException:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        await db.close()

    # independent post-commit check on a fresh connection
    async with aiosqlite.connect(db_path) as chk:
        n, s = await (await chk.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transaction_ledger "
            "WHERE guild_id = ? AND source = ?", (TARGET_GUILD_ID, LEDGER_SOURCE))).fetchone()
        after = await _fetch_existing(chk, [p["uid"] for p in plan])
    bad = [p["uid"] for p in plan if after.get(p["uid"], (None,))[0] != p["final"]]
    if n != len(plan) or s != info["migration_total"] or bad:
        raise MigrationError(f"POST-COMMIT verification mismatch (committed!): ledger {n}/{s}, "
                             f"bad balances {bad[:5]} -- restore from the backup if needed")
    return info


# ═════════════════════════════════════════════════════════════════════════
# 6. MAIN
# ═════════════════════════════════════════════════════════════════════════

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="One-time UnbelievaBoat -> economy migration. DRY-RUN unless "
                    "--execute --confirm-guild <id> are both given.")
    ap.add_argument("--data", default="nilive_unbelievaboat_balances.json",
                    help="path to the UnbelievaBoat export (read only)")
    ap.add_argument("--execute", action="store_true",
                    help="perform the migration (also requires --confirm-guild)")
    ap.add_argument("--confirm-guild", type=int, default=None, metavar="GUILD_ID",
                    help=f"must equal {TARGET_GUILD_ID} for --execute")
    ap.add_argument("--report-csv", default=None, metavar="PATH",
                    help="also write the per-user plan to this CSV (never the DB)")
    ap.add_argument("--summary-only", action="store_true", help="omit the per-user table")
    args = ap.parse_args(argv)
    if args.execute and args.confirm_guild is None:
        ap.error(f"--execute requires --confirm-guild {TARGET_GUILD_ID}")
    return args


async def run(args) -> int:
    execute = bool(args.execute)
    if execute and args.confirm_guild != TARGET_GUILD_ID:
        _p(f"REFUSED: --confirm-guild {args.confirm_guild} != {TARGET_GUILD_ID}. Nothing was changed.")
        return 2
    if args.confirm_guild is not None and not execute:
        _p("NOTE: --confirm-guild given without --execute -> still a DRY RUN.")

    data = load_migration_data(args.data)

    # dry-run must work without the production env; --execute must not fake it
    if not os.environ.get("OWNER_ID", "").strip() and not execute:
        os.environ["OWNER_ID"] = "0"
        _p("NOTE: OWNER_ID not set; using a placeholder for this read-only dry run.")
    sys.path.insert(0, PROJECT_ROOT)
    import aiosqlite
    import database                                   # the bot's own DB_PATH resolution
    db_path, env = database.DB_PATH, database.NERO_ENVIRONMENT
    if not os.path.isfile(db_path):
        raise MigrationError(f"database file not found at {db_path!r} (nothing created, nothing changed)")
    import utils.economy_safe as es
    from utils.currency import get_currency_config

    try:
        cfg = await get_currency_config(TARGET_GUILD_ID)
        currency = f"{cfg['coins']['name']} {cfg['coins']['emoji']}"
    except Exception as exc:
        currency = f"(could not resolve: {exc})"

    # read-only handle: query_only makes any write fail at the SQL level
    async with aiosqlite.connect(db_path) as ro:
        await ro.execute("PRAGMA query_only = ON")
        info = await build_plan(ro, data["records"])

    print_report(data, info, db_path=db_path, env=env, currency=currency,
                 summary_only=args.summary_only,
                 mode="EXECUTE (pre-check)" if execute else "DRY RUN -- nothing is written")
    if args.report_csv:
        write_csv(args.report_csv, info["plan"])
        _p(f"per-user plan written to {args.report_csv}")

    if not execute:
        _p("DRY RUN complete. No database write was performed.")
        if info["blockers"]:
            _p("Result: --execute would be REFUSED (see blockers above).")
            return 2
        _p("Result: no blockers. To run for real add:  --execute --confirm-guild "
           f"{TARGET_GUILD_ID}")
        return 0

    if info["blockers"]:
        _p("ABORTED: blockers present. Nothing was changed.")
        return 2

    _p(f"EXECUTING on {db_path} ({env}) for guild {TARGET_GUILD_ID} ...")
    _p("Step 1/3: creating verified backup ...")
    backup = await make_backup(aiosqlite, db_path)
    _p(f"  backup OK: {backup}")
    _p("Step 2/3: single transaction (guard + credits + ledger + verification) ...")
    try:
        done = await execute_migration(aiosqlite, es, db_path, data["records"])
    except BaseException as exc:
        _p(f"FAILED -> ROLLED BACK, database unchanged. Reason: {exc}")
        _p(f"(backup kept at {backup})")
        return 1
    _p("Step 3/3: committed and verified on a fresh connection.")
    _p("=" * 78)
    _p("MIGRATION COMPLETE")
    _p(f"  guild                  : {TARGET_GUILD_ID}")
    _p(f"  economy rows created   : {done['create']}")
    _p(f"  economy rows updated   : {done['update']}")
    _p(f"  total records changed  : {len(done['plan'])}")
    _p(f"  total currency added   : {done['migration_total']:,}")
    _p(f"  ledger rows inserted   : {len(done['plan'])}  (source='{LEDGER_SOURCE}')")
    _p(f"  backup                 : {backup}")
    _p("  Re-running this script is now rejected by the one-time guard.")
    _p("=" * 78)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except MigrationError as exc:
        _p(f"ABORTED: {exc}")
        return 2
    except KeyboardInterrupt:
        _p("Interrupted -- any open transaction was rolled back.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
