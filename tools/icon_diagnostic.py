#!/usr/bin/env python3
"""READ-ONLY production diagnosis for Shop product-icon preparation.

Runs INSIDE the deployed container (or against a copied DB) and answers one
question per product: *where exactly does the prepared emoji disappear?*

Usage
-----
    # inside the main Nilive-Bot service shell
    OWNER_ID=0 python3 tools/icon_diagnostic.py --guild-id <GUILD_ID> \
        [--user-id <MEMBER_ID>]

    # the guild id is auto-detected from the four product names if omitted
    OWNER_ID=0 python3 tools/icon_diagnostic.py --user-id <MEMBER_ID>

Guarantees
----------
* SQLite is opened with ``file:...?mode=ro`` (uri=True). Every connection in
  this script — including the one handed to the production resolver
  ``utils.shop_emoji.attach_prepared_emojis`` — is read-only, so a write
  attempt would raise instead of mutating production.
* No DDL, no migration, no Discord API call, no network I/O at all.
* Companion (optional, run separately after this script):
      curl -sS -H "Authorization: Bot $DISCORD_TOKEN" \
        https://discord.com/api/v10/applications/$DISCORD_CLIENT_ID/emojis \
        | python3 -c 'import json,sys;d=json.load(sys.stdin);print({e["id"]:e["name"] for e in d.get("items",[])})'
  That request also prints the IDs this script reports under "emoji_id".
  It never prints the token itself.
* URLs are redacted to scheme://host/path (query strings — which can carry
  signed tokens — are never printed).

This script only reports. It changes nothing.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Make the repository importable no matter where the script is invoked from,
# so `from database import DB_PATH` resolves to the DEPLOYED code, not a copy.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_NAMES = ("سهران السيرفر", "عنتيل السيرفر", "عصير المجرات", "شوكولاته الجلاكسي")

# Real columns as created by database.init_db() + utils.shop_emoji.
EXPECTED_SCHEMA = {
    "shop_items": ["id", "guild_id", "name", "icon_url", "enabled", "type"],
    "item_catalog": ["guild_id", "item_name", "icon_url", "rarity"],
    "inventory_items": ["guild_id", "user_id", "item_name", "quantity", "item_type"],
    # These two are the ONLY tables the icon pipeline depends on; a missing
    # column here is a real finding, hence the strict check.
    "shop_emoji_url_aliases": [
        "application_id", "guild_id", "source_url", "content_hash",
        "recovery_ack_required", "recovery_not_before", "created_at", "updated_at",
    ],
    "shop_emoji_assets": [
        "application_id", "content_hash", "emoji_name", "emoji_id", "animated",
        "creator_guild_id", "pipeline_version", "status", "attempt_id",
        "attempt_started_at", "quarantine_until", "last_error", "created_at", "updated_at",
    ],
}


# ── helpers ──────────────────────────────────────────────────────────────
def redact(raw):
    """Never echo a query string / fragment (may hold signed credentials)."""
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
    except Exception:
        return "<unparseable>"
    if parts.query or parts.fragment:
        return f"{parts.scheme}://{parts.netloc}{parts.path}?<redacted {len(parts.query)} chars>"
    return raw


def hr(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def sub(title):
    print("\n" + "-" * 78)
    print(title)
    print("-" * 78)


def qi(conn, sql, params=()):
    """Read-only query; tolerates a table/column that does not exist yet."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"  [query error] {exc.__class__.__name__}: {exc}  (sql: {sql[:90]}…)")
        return []


def columns(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


# ── 1. environment / provenance ──────────────────────────────────────────
def environment_report(db_path):
    hr("1. RUNTIME + DATABASE PROVENANCE")
    print(f"python                  : {sys.version.split()[0]}")
    print(f"sqlite3 (driver)        : {sqlite3.sqlite_version}")
    print(f"cwd                     : {os.getcwd()}")
    print(f"NERO_ENVIRONMENT        : {os.getenv('NERO_ENVIRONMENT', 'production')!r}")
    print(f"DATABASE_PATH env       : {os.getenv('DATABASE_PATH', '<unset>')!r}")
    print(f"STAGING_DATABASE_PATH   : {os.getenv('STAGING_DATABASE_PATH', '<unset>')!r}")
    print(f"resolved database.DB_PATH: {db_path}")
    print(f"DISCORD_CLIENT_ID set   : {bool(os.getenv('DISCORD_CLIENT_ID', '').strip())}")
    print(f"DISCORD_TOKEN set       : {bool(os.getenv('DISCORD_TOKEN', '').strip())} (value never printed)")

    path = Path(db_path)
    if path.exists():
        stat = path.stat()
        print(f"db file size            : {stat.st_size:,} bytes")
        print(f"db file mtime           : {__import__('datetime').datetime.utcfromtimestamp(stat.st_mtime)}Z")
    else:
        print(f"!! DB FILE DOES NOT EXIST AT {db_path}")

    # A staging DB would make every "missing alias" meaningless.
    for extra in ("/app/data/nero.db", "/app/data/nero_staging.db"):
        if extra != db_path and Path(extra).exists():
            print(f"note: another DB also exists: {extra} "
                  f"({Path(extra).stat().st_size:,} bytes)")


def schema_report(conn):
    hr("2. ACTUAL SCHEMA (does it match the deployed code?)")
    for table, expected in EXPECTED_SCHEMA.items():
        actual = columns(conn, table)
        if not actual:
            print(f"\n{table}: ** TABLE MISSING **")
            continue
        missing = [c for c in expected if c not in actual]
        print(f"\n{table}: {len(actual)} columns")
        print(f"  present : {', '.join(actual)}")
        if missing:
            print(f"  !! MISSING vs deployed code: {', '.join(missing)}")

    sub("shop_emoji_* DDL as stored in this database file")
    rows = qi(conn, "SELECT name, sql FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'shop_emoji%'")
    if not rows:
        print("  NO shop_emoji_* TABLES EXIST IN THIS FILE")
    for name, sql in rows:
        print(f"\n  -- {name}")
        print("  " + (sql or "").replace("\n", "\n  "))


# ── 2. per-product database trace ────────────────────────────────────────
def canonicalize(raw, canonicalize_url, SourceImageError):
    """Use the APPLICATION's own canonicalizer — never a re-implementation."""
    try:
        return canonicalize_url(raw), None
    except SourceImageError as exc:
        return None, f"{exc.code}: {exc}"
    except Exception as exc:  # noqa: BLE001 - report, never crash the audit
        return None, f"{exc.__class__.__name__}: {exc}"


def product_trace(conn, names, guild_id, app_candidates, canonicalize_url, SourceImageError):
    hr("3. PER-PRODUCT DATABASE TRACE")

    aliases_all = qi(conn, "SELECT application_id, guild_id, source_url, content_hash, "
                           "created_at FROM shop_emoji_url_aliases")
    assets_all = { (r[0], r[1]): r for r in qi(
        conn, "SELECT application_id, content_hash, status, emoji_id, emoji_name, animated, "
              "creator_guild_id, pipeline_version, last_error, created_at, updated_at "
              "FROM shop_emoji_assets") }
    print(f"totals: {len(aliases_all)} alias row(s), {len(assets_all)} asset row(s) "
          f"in this database file")

    verdicts = []
    for name in names:
        sub(f"PRODUCT: {name!r}")

        listings = qi(conn, "SELECT id, guild_id, name, icon_url, enabled, type "
                            "FROM shop_items WHERE name=?", (name,))
        if not listings:
            print("  shop_items           : NO ROW with this exact name (any guild)")
        for row in listings:
            print(f"  shop_items           : id={row[0]} guild_id={row[1]} enabled={row[4]} "
                  f"type={row[5]!r}  -> guild matches --guild-id: {row[1] == guild_id}")
            print(f"      icon_url (raw)   : {redact(row[3])!r}")
            if row[3]:
                print(f"      sha256(url)      : {__import__('hashlib').sha256(row[3].encode()).hexdigest()[:16]}…")
        same_guild = [r for r in listings if r[1] == guild_id]
        if len(listings) > 1:
            print(f"  !! {len(listings)} shop_items rows share this name "
                  f"(find_listing_icon_urls REQUIRES exactly ONE -> returns nothing)")

        catalog = qi(conn, "SELECT guild_id, item_name, icon_url, rarity FROM item_catalog "
                           "WHERE guild_id=? AND item_name=?", (guild_id, name))
        if not catalog:
            print("  item_catalog         : NO ROW for (this guild, this name)")
        for row in catalog:
            print(f"  item_catalog         : rarity={row[3]!r}")
            print(f"      icon_url (raw)   : {redact(row[2])!r}")

        inventory = qi(conn, "SELECT quantity, item_type FROM inventory_items "
                             "WHERE guild_id=? AND item_name=?", (guild_id, name))
        if inventory:
            print(f"  inventory_items      : {sum(r[0] or 0 for r in inventory)} owned "
                  f"across {len(inventory)} row(s), types={sorted({r[1] for r in inventory})}")
        else:
            print("  inventory_items      : (no row for this guild / name)")

        # ── the URLs the app would look up ──
        raw_urls = []
        if same_guild and same_guild[0][3]:
            raw_urls.append(("listing shop_items.icon_url", same_guild[0][3]))
        if catalog and catalog[0][2]:
            raw_urls.append(("catalog item_catalog.icon_url", catalog[0][2]))
        if not raw_urls:
            print("\n  FIRST FAILING CONDITION: no image URL stored anywhere "
                  "for this product (nothing was ever prepared against a URL)")
            verdicts.append((name, "no stored icon_url", None))
            continue

        canonical = {}
        for label, raw in raw_urls:
            canon, err = canonicalize(raw, canonicalize_url, SourceImageError)
            print(f"\n  canonical({label})")
            print(f"      canonical form   : {redact(canon)!r}" if canon else f"      !! INVALID: {err}")
            if canon:
                canonical.setdefault(canon, []).append(label)

        if not canonical:
            print("\n  FIRST FAILING CONDITION: every stored URL fails the app's own "
                  "canonicalize_url() -> production would silently break out before "
                  "ever querying the aliases")
            verdicts.append((name, "stored URL not canonicalizable", None))
            continue
        if len(canonical) > 1:
            print("  !! listing URL and catalog URL canonicalize to DIFFERENT strings")
        else:
            print("  listing and catalog canonicalize to the SAME string"
                  if len(raw_urls) == 2 else "  only one URL source present")

        # ── alias lookup, exactly production's predicate ──
        for canon, labels in canonical.items():
            for app_id in app_candidates:
                hits = qi(conn,
                          "SELECT a.guild_id, a.source_url, a.content_hash, a.recovery_ack_required, "
                          "a.recovery_not_before, a.created_at, a.updated_at "
                          "FROM shop_emoji_url_aliases a "
                          "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=?",
                          (app_id, guild_id, canon))
                print(f"\n  alias WHERE application_id={app_id} guild_id={guild_id} "
                      f"AND source_url=<canonical of {labels[0]}>  -> {len(hits)} row(s)")
                for glm, src, chash, ack, nbf, cad, uad in hits:
                    asset = assets_all.get((app_id, chash))
                    print(f"      content_hash     : {chash}")
                    print(f"      recovery flags   : ack_required={ack} not_before={nbf}")
                    print(f"      created/updated  : {cad} / {uad}")
                    if not asset:
                        print("      !! ASSET ROW MISSING for this hash "
                              "(alias is dangling; ON DELETE RESTRICT implies the asset "
                              "was deleted outside normal flow)")
                        continue
                    (_a, _h, status, emoji_id, ename, animated, creator, pver, lasterr,
                     _ca, _ua) = asset
                    print(f"      asset.status     : {status!r}   <- production requires 'ready'")
                    print(f"      asset.emoji_id   : {emoji_id!r}")
                    print(f"      asset.emoji_name : {ename!r}  animated={bool(animated)}")
                    print(f"      creator_guild_id : {creator}  (== this guild: {creator == guild_id})")
                    print(f"      pipeline_version : {pver!r}")
                    print(f"      asset.last_error : {lasterr!r}")

            # Is the same URL aliased under ANOTHER application or guild?
            others = qi(conn, "SELECT application_id, guild_id FROM shop_emoji_url_aliases "
                              "WHERE source_url=?", (canon,))
            foreign_apps = {a for a, _g in others} - set(app_candidates)
            foreign_guilds = {g for _a, g in others} - {guild_id}
            if foreign_apps:
                print(f"  !! same canonical URL is aliased under OTHER application_id(s): "
                      f"{sorted(foreign_apps)}  <-- application-id mismatch")
            if foreign_guilds:
                print(f"  !! same canonical URL is aliased under OTHER guild_id(s): "
                      f"{sorted(foreign_guilds)}  <-- guild-id mismatch")
            if not others:
                print("  no alias for this canonical URL exists under ANY application/guild "
                      "-> this exact source_url was never prepared")

        # ── near-miss detector: aliases in this guild whose canonical form
        # differs only slightly (the URL-canonicalization-mismatch hypothesis)
        guild_aliases = [(a, g, s) for a, g, s, *_ in aliases_all if g == guild_id]
        print(f"\n  aliases in this guild (any app): {len(guild_aliases)}")
        for app_id, _g, stored in guild_aliases:
            sc, _err = canonicalize(stored, canonicalize_url, SourceImageError)
            tag = "MATCH" if sc in canonical else "     "
            print(f"      {tag} app={app_id} stored={redact(stored)!r}")

        verdicts.append((name, "see detail above", canonical))

    return verdicts


# ── 3. Rank-side trace using the real resolver ───────────────────────────
def _renderer_candidates(item):
    """Byte-for-byte copy of _fetch_item_icon()'s candidate construction.

    This is the ONLY way to know which sources the production
    "tried N source(s)" warning counted.
    """
    from utils.emoji import emoji_cdn_url, parse_emoji_input

    prepared = (item.get("app_emoji") or "").strip()
    parsed = parse_emoji_input(prepared) if prepared else None
    candidates = []
    if parsed:
        candidates.append(emoji_cdn_url(parsed[0], animated=parsed[2]))
    for key in ("listing_icon_url", "icon_url"):
        raw = (item.get(key) or "").strip()
        if not raw:
            continue
        if raw.lower().startswith(("http://", "https://")):
            candidates.append(raw)
        else:
            token = parse_emoji_input(raw)
            if token:
                candidates.append(emoji_cdn_url(token[0], animated=token[2]))
    return list(dict.fromkeys(candidates))


async def _probe_resolver(db_path, guild_id, user_id, app_id):
    """Build the Rank card's grid payload, then resolve it with the real code."""
    import aiosqlite

    from utils.item_catalog import get_catalog_entry
    from utils.inventory import get_inventory_entries
    from utils.shop_emoji import attach_prepared_emojis, find_listing_icon_urls

    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as adb:
        items = await get_inventory_entries(guild_id, user_id)
        grid = []
        for it in items:
            if it["item_type"] in ("role", "temp_role"):
                continue
            cat = await get_catalog_entry(guild_id, it["item_name"])
            grid.append({"item_name": it["item_name"],
                         "icon_url": (cat.get("icon_url") or "").strip(),
                         "listing_icon_url": ""})
        found = await find_listing_icon_urls(
            adb, guild_id, [g["item_name"] for g in grid])
        for g in grid:
            g["listing_icon_url"] = found.get(g["item_name"], "")

        # Pass 1 (catalog URL) then pass 2 (listing URL), exactly as
        # utils/rank_card_data.py::get_rank_card_data() does. Production wraps
        # both in `except Exception: pass`, which hides the real error -- here
        # we surface it.
        pass1_error = pass2_error = None
        try:
            await attach_prepared_emojis(adb, guild_id, grid, application_id=app_id)
        except Exception as exc:  # noqa: BLE001
            pass1_error = exc
        unresolved = [g for g in grid if not (g.get("app_emoji") or "").strip()]
        if unresolved:
            listing_products = [{"icon_url": g["listing_icon_url"]} for g in unresolved]
            try:
                await attach_prepared_emojis(adb, guild_id, listing_products,
                                             application_id=app_id)
            except Exception as exc:  # noqa: BLE001
                pass2_error = exc
            for g, lp in zip(unresolved, listing_products):
                g["app_emoji"] = (lp.get("app_emoji") or "").strip()
        return grid, pass1_error, pass2_error


def rank_trace(db_path, guild_id, user_id, app_candidates):
    """Call the DEPLOYED resolver with a READ-ONLY connection."""
    if not user_id:
        print("\n(skipping Rank resolver trace: pass --user-id <MEMBER_ID>)")
        return

    import asyncio

    async def _run():
        for app_id in app_candidates:
            print(f"\n  --- resolver run with application_id={app_id} ---")
            grid, err1, err2 = await _probe_resolver(db_path, guild_id, user_id, app_id)
            if err1 is not None:
                print(f"  !! pass 1 RAISED (swallowed in production): "
                      f"{err1.__class__.__name__}: {err1}")
            if err2 is not None:
                print(f"  !! pass 2 RAISED (swallowed in production): "
                      f"{err2.__class__.__name__}: {err2}")
            if not grid:
                print("  (no non-role items in this member's inventory)")
            for g in grid:
                cands = _renderer_candidates(g)
                print(f"    {g['item_name']!r}")
                print(f"      app_emoji        : {g.get('app_emoji') or '<EMPTY>'}")
                print(f"      catalog icon_url : {redact(g['icon_url']) or '<empty>'}")
                print(f"      listing icon_url : {redact(g['listing_icon_url']) or '<empty>'}")
                if not cands:
                    print("      renderer will try: 0 source(s) -> placeholder tile, NO warning")
                else:
                    tag = ("   <-- matches a 'tried 1 source(s)' warning"
                           if len(cands) == 1 else "")
                    print(f"      renderer will try: {len(cands)} source(s){tag}")
                    for url in cands:
                        print(f"         -> {redact(url)}")

    asyncio.run(_run())


# ── main ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guild-id", type=int, default=None)
    ap.add_argument("--user-id", type=int, default=None,
                    help="member whose Rank card fails (enables the resolver trace)")
    ap.add_argument("--names", default=",".join(DEFAULT_NAMES),
                    help="comma-separated exact product names")
    ap.add_argument("--all-guilds", action="store_true",
                    help="do not filter by guild when locating the products")
    args = ap.parse_args()

    names = [n.strip() for n in args.names.split(",") if n.strip()]

    print("READ-ONLY Shop icon diagnostic — no writes, no Discord calls, no network.")
    print("Nothing in this run can modify the database: every connection uses mode=ro.")

    # importing the app module gives us the DEPLOYED DB path resolution
    try:
        from database import DB_PATH
    except SystemExit:
        print("\nFATAL: importing database.py requires OWNER_ID to be set "
              "(it calls sys.exit when missing).\n"
              "Re-run as:  OWNER_ID=0 python3 tools/icon_diagnostic.py …")
        return 2
    from utils.shop_emoji import (SourceImageError, canonicalize_url,
                                  configured_application_id)

    db_path = str(DB_PATH)
    environment_report(db_path)
    if not Path(db_path).exists():
        return 2

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        schema_report(conn)

        # application ids: (a) what the BOT process authenticates as, (b) the
        # dashboard env default that preparation used.
        configured = configured_application_id()
        app_candidates = []
        try:
            import discord  # noqa: F401  (present in the image)
            print("\nnote: the bot's authenticated application_id is available at runtime as "
                  "client.application_id / interaction.application_id. This script cannot "
                  "read a live client, so it uses DISCORD_CLIENT_ID and any ids actually "
                  "present in the alias table.")
        except Exception:
            pass
        if configured:
            app_candidates.append(configured)
        for (app_id,) in qi(conn, "SELECT DISTINCT application_id FROM shop_emoji_url_aliases"):
            if app_id not in app_candidates:
                app_candidates.append(app_id)
        hr("APPLICATION IDS BEING TESTED")
        print(f"DISCORD_CLIENT_ID (dashboard/preparation default): {configured or '<UNSET>'}")
        for extra in app_candidates[1:]:
            print(f"application_id found in alias table            : {extra}")
        if not app_candidates:
            print("!! no application id available at all -> every alias lookup returns nothing")

        guild_id = args.guild_id
        if guild_id is None:
            marks = ",".join("?" for _ in names)
            rows = qi(conn, f"SELECT DISTINCT guild_id FROM shop_items WHERE name IN ({marks})",
                      tuple(names))
            guild_ids = [r[0] for r in rows]
            print(f"\nauto-detected guild_id(s) for the four names: {guild_ids}")
            if len(guild_ids) == 1:
                guild_id = guild_ids[0]
            else:
                print("!! cannot auto-detect a single guild; re-run with --guild-id")
                return 2
        print(f"\nUSING guild_id = {guild_id}")

        product_trace(conn, names, guild_id, app_candidates, canonicalize_url, SourceImageError)
        rank_trace(db_path, guild_id, args.user_id, app_candidates)
    finally:
        conn.close()

    hr("DONE — nothing was written")
    print("Next: re-run the companion curl (see this script's docstring) to confirm whether "
          "each reported emoji_id still exists in the application. That is the ONLY "
          "remaining check this offline script cannot perform.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
