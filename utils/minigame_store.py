import json
import random
import sqlite3
from datetime import datetime, timezone

import aiosqlite
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# MINIGAMES v2 — STORE (Phase 1 of the approved v2 plan)
#
# Single SQL authority for the minigames v2 domain: categories (freeform
# tree), templates, reward pools, per-category shuffle-bag state, the
# dashboard→bot spawn request queue, and the extended run log.
#
# Design (see MINIGAMES_V2_PLAN.md — the locked plan this implements):
#   - Own standalone schema with its own ensure_tables(), the same pattern
#     cogs/minigames.py, utils/mission_engine.py and utils/trade_engine.py
#     already established (isolated, idempotent, dashboard-safe).
#   - The legacy minigames_config / minigames_log / minigames_tiers tables
#     are the ones the v2 code builds on: config keeps the weekly pacing,
#     log is EXTENDED in place (no parallel table), tiers is a one-time
#     migration SOURCE (kept on disk, retired in code in Phase 3/6).
#   - Write ownership (plan §19): this store is used by BOTH processes, but
#     in practice dashboard writes categories/templates/rewards/config and
#     the request QUEUE ROWS, while the bot writes bag state, run log rows
#     and the queue claim/mark columns. No table has two writers.
#
# Conventions: async functions, aiosqlite, DB_PATH from database.py.
# Validation of user input (names, embed limits, ranges) happens in the
# API layer (Phase 4) — the store enforces only data-integrity rules
# (whitelists, positive weights, cycle-checked moves, NOT NULL legacy
# columns).
# ═══════════════════════════════════════════════════════════════════════

VALID_GAME_TYPES = ("quick_click", "wheel", "math", "colors", "emoji", "rps")
VALID_REWARD_TYPES = ("xp", "coins", "diamonds", "item", "role", "temp_role")
VALID_RUN_MODES = ("auto", "manual", "test")
VALID_REQUEST_MODES = ("manual", "test")

# Legacy minigames_log has `tier TEXT NOT NULL` and `event_date TEXT NOT
# NULL` (predates v2 and cannot be altered away in SQLite without a table
# rebuild). v2 rows fill `tier` with the template's direct category name —
# meaningful for any legacy rendering and harmless for the new columns.


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_iso() -> str:
    return _now_utc().date().isoformat()


def _now_iso() -> str:
    return _now_utc().isoformat()


def _dump(obj) -> str | None:
    return json.dumps(obj) if obj is not None else None


def _load(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ── SCHEMA ──────────────────────────────────────────────────────────────

async def ensure_tables():
    """
    Idempotent: create the v2 tables (and the legacy tables if this
    process ran before the bot's cog_load ever did — same defensive
    pattern dashboard/api/minigames.py relies on), extend the legacy log
    and config tables with guarded ALTERs, then run the one-time
    tier→category migration.

    Safe to await from BOTH processes on every startup/query — every
    statement is IF NOT EXISTS or guard-checked.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Legacy tables — verbatim shape of cogs/minigames.py's schema so
        # v2 works even if the dashboard is opened before the bot has
        # completed a cog_load (see dashboard/api/minigames.py header).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigames_config (
                guild_id            INTEGER PRIMARY KEY,
                enabled             INTEGER DEFAULT 1,
                channel_id          INTEGER,
                min_events_per_week INTEGER DEFAULT 5,
                max_events_per_week INTEGER DEFAULT 10,
                events_this_week    INTEGER DEFAULT 0,
                week_start_date     TEXT,
                last_check_date     TEXT,
                claim_seconds       INTEGER DEFAULT 300,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigames_log (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id           INTEGER NOT NULL,
                event_date         TEXT NOT NULL,
                tier               TEXT NOT NULL,
                channel_id         INTEGER,
                message_id         INTEGER,
                winner_id          INTEGER,
                winner_display_name TEXT,
                forced             INTEGER DEFAULT 0,
                fired_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgl_guild
            ON minigames_log(guild_id, event_date)
        """)

        # v2 tables (plan §2).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigame_categories (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id             INTEGER NOT NULL,
                parent_id            INTEGER,
                name                 TEXT NOT NULL,
                weight               INTEGER NOT NULL DEFAULT 1,
                sort_order           INTEGER NOT NULL DEFAULT 0,
                emoji                TEXT,
                color                TEXT,
                default_rewards_json TEXT,
                enabled              INTEGER NOT NULL DEFAULT 1,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgc_guild
            ON minigame_categories(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgc_parent
            ON minigame_categories(guild_id, parent_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigame_templates (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER NOT NULL,
                category_id   INTEGER NOT NULL,
                name          TEXT NOT NULL,
                game_type     TEXT NOT NULL,
                enabled       INTEGER NOT NULL DEFAULT 1,
                auto_spawn    INTEGER NOT NULL DEFAULT 1,
                embed_json    TEXT NOT NULL,
                config_json   TEXT NOT NULL DEFAULT '{}',
                channel_id    INTEGER,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgt_guild_cat
            ON minigame_templates(guild_id, category_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigame_rewards (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id    INTEGER NOT NULL,
                sort_order     INTEGER NOT NULL DEFAULT 0,
                reward_type    TEXT NOT NULL,
                reward_value   TEXT NOT NULL,
                duration_hours INTEGER,
                weight         INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mgr_template
            ON minigame_rewards(template_id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigame_bag_state (
                guild_id     INTEGER NOT NULL,
                category_id  INTEGER NOT NULL,
                bag_json     TEXT NOT NULL,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, category_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigame_spawn_requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER NOT NULL,
                template_id   INTEGER NOT NULL,
                mode          TEXT NOT NULL,
                requested_by  TEXT,
                requested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at  TIMESTAMP,
                status        TEXT NOT NULL DEFAULT 'pending',
                error         TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_msr_pending
            ON minigame_spawn_requests(status, guild_id)
        """)

        # Guarded ALTERs on the legacy tables (plan §2 / §3): only add a
        # column if PRAGMA table_info says it is missing, so re-runs are
        # no-ops.
        log_cols = {r[1] for r in await (
            await db.execute("PRAGMA table_info(minigames_log)")).fetchall()}
        log_additions = (
            ("template_id", "INTEGER"),
            ("template_name", "TEXT"),
            ("category_id", "INTEGER"),
            ("category_name", "TEXT"),
            ("game_type", "TEXT"),
            ("mode", "TEXT DEFAULT 'auto'"),
            ("participants_json", "TEXT"),
            ("winners_json", "TEXT"),
            ("started_at", "TIMESTAMP"),
            ("ended_at", "TIMESTAMP"),
            ("status", "TEXT DEFAULT 'completed'"),
        )
        for col, coltype in log_additions:
            if col not in log_cols:
                await db.execute(f"ALTER TABLE minigames_log ADD COLUMN {col} {coltype}")

        cfg_cols = {r[1] for r in await (
            await db.execute("PRAGMA table_info(minigames_config)")).fetchall()}
        if "global_default_rewards_json" not in cfg_cols:
            await db.execute(
                "ALTER TABLE minigames_config "
                "ADD COLUMN global_default_rewards_json TEXT")

        await db.commit()

    # One-time tier→category migration (idempotent; cheap early exits).
    await run_migration()


# ── MIGRATION (one-time, plan §3) ───────────────────────────────────────

async def run_migration(guild_id: int | None = None):
    """
    Seed the freeform category tree from the legacy fixed tiers: for each
    guild that has minigames_tiers rows but NO minigame_categories rows,
    create one root category per distinct tier (same weight/enabled) with
    that tier's single reward as a one-row default-reward preset.

    Idempotent: once a guild has categories the check short-circuits, and
    the tiers table is only ever READ. The table itself is kept on disk
    (never dropped) for rollback safety — plan §3.

    Failure safety: each guild's inserts commit as ONE transaction. If
    anything fails mid-guild the connection closes without committing
    and SQLite rolls the whole guild back — the DB is left in its clean
    pre-migration state for that guild, and the next startup retries
    (plan §3.4 rollback guarantee; verified by
    scripts/test_minigame_migration.py). A failed guild never aborts the
    migration of other guilds — it is logged loudly instead.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if guild_id is not None:
            guild_ids = [guild_id]
        else:
            try:
                cursor = await db.execute(
                    "SELECT DISTINCT guild_id FROM minigames_tiers")
                guild_ids = [r[0] for r in await cursor.fetchall()]
            except sqlite3.OperationalError:
                # minigames_tiers does not exist (fresh DB where the legacy
                # cog never created it) — nothing to migrate.
                return

        for g in guild_ids:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM minigame_categories WHERE guild_id = ?",
                (g,))
            (has_cats,) = await cursor.fetchone()
            if has_cats:
                continue  # already migrated (or hand-built categories exist)

            try:
                cursor = await db.execute("""
                    SELECT tier, weight, reward_type, reward_value,
                           reward_duration_hours, enabled
                    FROM minigames_tiers
                    WHERE guild_id = ?
                    ORDER BY id ASC
                """, (g,))
                tiers = await cursor.fetchall()
            except sqlite3.OperationalError:
                continue  # legacy table absent — nothing to migrate
            if not tiers:
                continue

            try:
                seen = set()
                for tier, weight, rtype, rval, rdur, enabled in tiers:
                    if tier in seen:
                        # Legacy schema allowed duplicate tier rows; the
                        # first wins, the rest are skipped (warned, not
                        # lost — they remain in the kept minigames_tiers
                        # table).
                        print(f"[MINIGAMES] migration: skipping duplicate "
                              f"tier '{tier}' for guild {g}")
                        continue
                    seen.add(tier)
                    # Canonical reward-row shape (the keys the UI and
                    # _insert_rewards read; duration only when the legacy
                    # row had one — temp roles). A LIST of rows: the
                    # column is a reward-pool JSON array.
                    preset = [{
                        "reward_type": rtype,
                        "reward_value": rval,
                        "weight": 1,
                    }]
                    if rdur:
                        preset[0]["duration_hours"] = int(rdur)
                    # NOTE: `int(enabled or 1)` was a latent bug — a
                    # DISABLED legacy tier (enabled=0) would have been
                    # migrated as enabled, because `0 or 1` is 1.
                    # (Caught by the realistic-fixture migration suite.)
                    await db.execute("""
                        INSERT INTO minigame_categories
                            (guild_id, parent_id, name, weight, sort_order,
                             default_rewards_json, enabled)
                        VALUES (?, NULL, ?, ?, ?, ?, ?)
                    """, (g, tier.title(), max(1, int(weight or 1)),
                          len(seen) - 1, _dump(preset), 1 if enabled else 0))
                await db.commit()
                print(f"[MINIGAMES] migration: seeded {len(seen)} categories "
                      f"from legacy tiers for guild {g}")
            except Exception as exc:  # noqa: BLE001 — deliberate, see above
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                # Explicit rollback (and the connection close, which
                # rolls back anything left open) guarantee this guild is
                # left in its clean pre-migration state — no partial
                # state, and the next startup retries from scratch.
                print(f"[MINIGAMES] migration ERROR for guild {g}: {exc!r} "
                      f"— nothing committed for this guild; it will retry "
                      f"on the next startup. Fix the cause and restart.")


# ── CONFIG (weekly pacing + presets) ────────────────────────────────────

async def get_config(guild_id: int) -> dict:
    """Full guild config incl. parsed preset. Mirrors the legacy shape so
    the existing pacing loop can consume it unchanged (Phase 3)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM minigames_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    if row:
        cols = [d[0] for d in cursor.description]
        cfg = dict(zip(cols, row))
    else:
        cfg = {
            "guild_id": guild_id, "enabled": 1, "channel_id": None,
            "min_events_per_week": 5, "max_events_per_week": 10,
            "events_this_week": 0, "week_start_date": None,
            "last_check_date": None, "claim_seconds": 300,
        }
    cfg["global_default_rewards"] = _load(
        cfg.get("global_default_rewards_json"), [])
    return cfg


async def save_config(guild_id: int, **fields) -> dict:
    """
    Upsert a PARTIAL config (only the keys passed). Keys accepted:
    enabled, channel_id, min_events_per_week, max_events_per_week,
    global_default_rewards (list). events_this_week / week_start_date /
    last_check_date are bot-owned pacing state and are not accepted here.
    """
    allowed = ("enabled", "channel_id", "min_events_per_week",
               "max_events_per_week")
    cols, values = [], []
    for key in allowed:
        if key in fields:
            cols.append(key)
            values.append(fields[key])
    if "global_default_rewards" in fields:
        cols.append("global_default_rewards_json")
        values.append(_dump(fields["global_default_rewards"]))
    if not cols:
        return await get_config(guild_id)

    now = _now_iso()
    # NOTE: the DO UPDATE SET clause carries its OWN bindings on top of the
    # INSERT VALUES — supply the value tuple twice.
    sets = ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ?"
    placeholders = ", ".join("?" * len(cols))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
            INSERT INTO minigames_config (guild_id, {", ".join(cols)}, updated_at)
            VALUES (?, {placeholders}, ?)
            ON CONFLICT(guild_id) DO UPDATE SET {sets}
        """, (guild_id, *values, now) + tuple(values) + (now,))
        await db.commit()
    return await get_config(guild_id)


async def bump_events_this_week(guild_id: int) -> None:
    """Bot-owned weekly counter increment (D5: automatic successes only)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE minigames_config
            SET events_this_week = events_this_week + 1,
                updated_at = ?
            WHERE guild_id = ?
        """, (_now_iso(), guild_id))
        await db.commit()


async def mark_week(guild_id: int, week_start_date: str,
                    last_check_date: str, events_this_week: int | None = None) -> None:
    """Bot-owned pacing state writes (weekly reset / daily roll bookkeeping)."""
    if events_this_week is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE minigames_config
                SET week_start_date = ?, last_check_date = ?,
                    events_this_week = ?, updated_at = ?
                WHERE guild_id = ?
            """, (week_start_date, last_check_date, events_this_week,
                  _now_iso(), guild_id))
            await db.commit()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE minigames_config
                SET week_start_date = ?, last_check_date = ?, updated_at = ?
                WHERE guild_id = ?
            """, (week_start_date, last_check_date, _now_iso(), guild_id))
            await db.commit()


# ── CATEGORIES (freeform tree, plan §4/§13) ─────────────────────────────

async def create_category(guild_id: int, name: str, parent_id: int | None = None,
                          weight: int = 1, emoji: str | None = None,
                          color: str | None = None,
                          default_rewards: list | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("category name must not be empty")
    if parent_id is not None:
        parent = await get_category(guild_id, parent_id)
        if not parent:
            raise ValueError("parent category not found")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM minigame_categories "
            "WHERE guild_id = ? AND parent_id IS ?", (guild_id, parent_id))
        (sort_order,) = await cursor.fetchone()
        cursor = await db.execute("""
            INSERT INTO minigame_categories
                (guild_id, parent_id, name, weight, sort_order, emoji, color,
                 default_rewards_json, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (guild_id, parent_id, name, max(1, int(weight or 1)), sort_order,
              emoji, color, _dump(default_rewards or [])))
        await db.commit()
        new_id = cursor.lastrowid
    return await get_category(guild_id, new_id)


async def get_category(guild_id: int, category_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM minigame_categories WHERE id = ? AND guild_id = ?",
            (category_id, guild_id))
        row = await cursor.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cursor.description]
    cat = dict(zip(cols, row))
    cat["default_rewards"] = _load(cat.pop("default_rewards_json"), [])
    return cat


def _category_row_to_dict(cols, row) -> dict:
    cat = dict(zip(cols, row))
    cat["default_rewards"] = _load(cat.pop("default_rewards_json"), [])
    # Read-time normalization: very early migration builds (and any
    # hand-edited rows) may carry the legacy `type`/`value` keys.
    # Canonical shape is reward_type/reward_value — the UI and the
    # reward validator both read the canonical keys.
    norm = []
    for r in cat["default_rewards"]:
        if isinstance(r, dict) and "reward_type" not in r and "type" in r:
            r = dict(r)
            r["reward_type"] = r.pop("type")
            if "reward_value" not in r and "value" in r:
                r["reward_value"] = r.pop("value")
        norm.append(r)
    cat["default_rewards"] = norm
    return cat


async def get_categories_tree(guild_id: int) -> list[dict]:
    """
    Full nested tree for one guild. Each node:
      id, parent_id, name, weight, enabled, emoji, color, sort_order,
      default_rewards (list), direct_templates (count),
      direct_playable (enabled+auto_spawn count at this node),
      subtree_playable (playable templates anywhere below, incl. direct).
    Nested under 'children' (sorted by sort_order, id).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM minigame_categories WHERE guild_id = ? "
            "ORDER BY sort_order ASC, id ASC", (guild_id,))
        cols = [d[0] for d in cursor.description]
        cats = [_category_row_to_dict(cols, r) for r in await cursor.fetchall()]
        cursor = await db.execute("""
            SELECT id, category_id, enabled, auto_spawn
            FROM minigame_templates WHERE guild_id = ?
        """, (guild_id,))
        templates = await cursor.fetchall()

    by_id = {c["id"]: c for c in cats}
    for c in cats:
        c["children"] = []
        c["direct_templates"] = 0
        c["direct_playable"] = 0

    for tid, cat_id, enabled, auto in templates:
        node = by_id.get(cat_id)
        if not node:
            continue  # orphaned row (shouldn't happen) — ignore, not hide
        node["direct_templates"] += 1
        if enabled and auto:
            node["direct_playable"] += 1

    roots = []
    for c in cats:
        if c["parent_id"] is not None and c["parent_id"] in by_id:
            by_id[c["parent_id"]]["children"].append(c)
        else:
            roots.append(c)

    def subtree_playable(node):
        total = node["direct_playable"]
        for child in node["children"]:
            total += subtree_playable(child)
        node["subtree_playable"] = total
        return total

    for r in roots:
        subtree_playable(r)
    return roots


async def get_category_subtree_ids(guild_id: int, category_id: int) -> list[int]:
    """All descendant category ids (BFS) — for subtree template queries."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, parent_id FROM minigame_categories WHERE guild_id = ?",
            (guild_id,))
        edges = await cursor.fetchall()
    children = {}
    for cid, pid in edges:
        children.setdefault(pid, []).append(cid)
    out, queue = [], [category_id]
    while queue:
        cur = queue.pop(0)
        out.extend(children.get(cur, []))
        queue.extend(children.get(cur, []))
    return out


async def update_category(guild_id: int, category_id: int,
                          fields: dict) -> dict | None:
    """
    Partial update. Keys: name, weight, parent_id (MOVE — cycle-checked),
    emoji, color, default_rewards (list), enabled.
    Moving a category under its own descendant (or itself) raises
    ValueError (plan §17 cycle check).
    """
    cat = await get_category(guild_id, category_id)
    if not cat:
        return None

    if "parent_id" in fields:
        new_parent = fields["parent_id"]
        if new_parent is not None:
            if new_parent == category_id:
                raise ValueError("a category cannot be its own parent")
            descendants = set(await get_category_subtree_ids(guild_id, category_id))
            if new_parent in descendants:
                raise ValueError("cannot move a category under its own descendant")
            if not await get_category(guild_id, new_parent):
                raise ValueError("parent category not found")

    sets, args = [], []
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise ValueError("category name must not be empty")
        sets.append("name = ?"); args.append(name)
    if "weight" in fields:
        sets.append("weight = ?"); args.append(max(1, int(fields["weight"] or 1)))
    if "parent_id" in fields:
        sets.append("parent_id = ?"); args.append(fields["parent_id"])
    if "emoji" in fields:
        sets.append("emoji = ?"); args.append(fields["emoji"])
    if "color" in fields:
        sets.append("color = ?"); args.append(fields["color"])
    if "default_rewards" in fields:
        sets.append("default_rewards_json = ?")
        args.append(_dump(fields["default_rewards"] or []))
    if "enabled" in fields:
        sets.append("enabled = ?"); args.append(int(bool(fields["enabled"])))
    if not sets:
        return cat
    args += (category_id, guild_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE minigame_categories SET {', '.join(sets)} "
            "WHERE id = ? AND guild_id = ?", args)
        await db.commit()
    return await get_category(guild_id, category_id)


async def delete_category(guild_id: int, category_id: int) -> tuple[bool, str]:
    """
    (ok, message). Refuses (409-equivalent) while the category has direct
    templates or subcategories — plan §10/§16: nothing is silently moved
    or cascade-deleted.
    """
    cat = await get_category(guild_id, category_id)
    if not cat:
        return False, "category not found"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM minigame_categories "
            "WHERE guild_id = ? AND parent_id = ?", (guild_id, category_id))
        (n_children,) = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM minigame_templates "
            "WHERE guild_id = ? AND category_id = ?", (guild_id, category_id))
        (n_templates,) = await cursor.fetchone()
    if n_children or n_templates:
        parts = []
        if n_templates:
            parts.append(f"{n_templates} template(s)")
        if n_children:
            parts.append(f"{n_children} subcategor{'y' if n_children == 1 else 'ies'}")
        return False, (f"cannot delete — move or delete its "
                       f"{', '.join(parts)} first")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM minigame_categories WHERE id = ? AND guild_id = ?",
            (category_id, guild_id))
        # Its bags can no longer be reached — drop them (state, not data).
        await db.execute("""
            DELETE FROM minigame_bag_state
            WHERE guild_id = ? AND category_id = ?
        """, (guild_id, category_id))
        await db.commit()
    return True, "deleted"


# ── TEMPLATES ───────────────────────────────────────────────────────────

async def _insert_rewards(db, template_id: int, rewards: list | None):
    """Insert reward rows (empty list is valid — D11). Raises ValueError
    on an invalid reward_type (data integrity; user-facing validation is
    the API layer's job)."""
    for i, row in enumerate(rewards or []):
        rtype = row.get("reward_type") or row.get("type")
        if rtype not in VALID_REWARD_TYPES:
            raise ValueError(f"invalid reward_type: {rtype!r}")
        value = row.get("reward_value")
        if value is None and "value" in row:
            value = row["value"]
        if value is None or str(value) == "":
            raise ValueError("reward_value is required for every reward row")
        duration = row.get("duration_hours")
        if duration is not None:
            duration = int(duration)
            if duration <= 0:
                raise ValueError("duration_hours must be positive")
        await db.execute("""
            INSERT INTO minigame_rewards
                (template_id, sort_order, reward_type, reward_value,
                 duration_hours, weight)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (template_id, i, rtype, str(value), duration,
              max(1, int(row.get("weight") or 1))))


async def create_template(guild_id: int, category_id: int, name: str,
                          game_type: str, enabled: bool = True,
                          auto_spawn: bool = True,
                          embed: dict | None = None,
                          config: dict | None = None,
                          channel_id: int | None = None,
                          rewards: list | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("template name must not be empty")
    if game_type not in VALID_GAME_TYPES:
        raise ValueError(f"game_type must be one of: {', '.join(VALID_GAME_TYPES)}")
    if not await get_category(guild_id, category_id):
        raise ValueError("category not found")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO minigame_templates
                (guild_id, category_id, name, game_type, enabled, auto_spawn,
                 embed_json, config_json, channel_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, category_id, name, game_type, int(bool(enabled)),
              int(bool(auto_spawn)), _dump(embed or {}),
              _dump(config or {}), channel_id, _now_iso()))
        await _insert_rewards(db, cursor.lastrowid, rewards)
        await db.commit()
        new_id = cursor.lastrowid
    return await get_template(guild_id, new_id)


async def get_template(guild_id: int, template_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM minigame_templates WHERE id = ? AND guild_id = ?",
            (template_id, guild_id))
        row = await cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        tpl = dict(zip(cols, row))
        rewards = await _fetch_rewards(db, template_id)
    tpl["embed"] = _load(tpl.pop("embed_json"), {})
    tpl["config"] = _load(tpl.pop("config_json"), {})
    tpl["rewards"] = rewards
    return tpl


async def _fetch_rewards(db, template_id: int) -> list[dict]:
    cursor = await db.execute("""
        SELECT id, sort_order, reward_type, reward_value,
               duration_hours, weight
        FROM minigame_rewards WHERE template_id = ?
        ORDER BY sort_order ASC, id ASC
    """, (template_id,))
    rows = await cursor.fetchall()
    return [{
        "id": r[0], "sort_order": r[1], "reward_type": r[2],
        "reward_value": r[3], "duration_hours": r[4], "weight": r[5],
    } for r in rows]


async def list_templates(guild_id: int, category_id: int | None = None,
                         include_disabled: bool = True,
                         subtree: bool = True) -> list[dict]:
    """
    Templates for a category (direct or its subtree when subtree=True)
    or the whole guild when category_id is None. Each row includes a
    rewards summary (count + has_pool) and category name for the tree UI.
    """
    cat_ids = None
    if category_id is not None:
        cat_ids = {category_id}
        if subtree:
            cat_ids |= set(await get_category_subtree_ids(guild_id, category_id))
    async with aiosqlite.connect(DB_PATH) as db:
        query = """
            SELECT t.*, c.name AS category_name,
                   (SELECT COUNT(*) FROM minigame_rewards r
                     WHERE r.template_id = t.id) AS reward_count
            FROM minigame_templates t
            JOIN minigame_categories c ON c.id = t.category_id
            WHERE t.guild_id = ?
        """
        args: list = [guild_id]
        if cat_ids is not None:
            query += " AND t.category_id IN ({})".format(
                ",".join("?" * len(cat_ids)))
            args += sorted(cat_ids)
        if not include_disabled:
            query += " AND t.enabled = 1"
        query += " ORDER BY t.name ASC, t.id ASC"
        cursor = await db.execute(query, args)
        cols = [d[0] for d in cursor.description]
        out = []
        for row in await cursor.fetchall():
            tpl = dict(zip(cols, row))
            tpl["embed"] = _load(tpl.pop("embed_json"), {})
            tpl["config"] = _load(tpl.pop("config_json"), {})
            tpl["has_reward_pool"] = int(tpl.pop("reward_count")) > 0
            out.append(tpl)
    return out


async def update_template(guild_id: int, template_id: int,
                          fields: dict) -> dict | None:
    """
    Partial update. Keys: name, category_id, game_type, enabled,
    auto_spawn, embed, config, channel_id, rewards (list — replaces the
    whole pool ATOMICALLY: delete + reinsert in one transaction, so a
    failure cannot leave a half-pool).
    """
    tpl = await get_template(guild_id, template_id)
    if not tpl:
        return None
    if "game_type" in fields and fields["game_type"] not in VALID_GAME_TYPES:
        raise ValueError(f"game_type must be one of: {', '.join(VALID_GAME_TYPES)}")
    if "category_id" in fields and fields["category_id"] is not None:
        if not await get_category(guild_id, fields["category_id"]):
            raise ValueError("category not found")

    sets, args = [], []
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise ValueError("template name must not be empty")
        sets.append("name = ?"); args.append(name)
    if "category_id" in fields:
        sets.append("category_id = ?"); args.append(fields["category_id"])
    if "game_type" in fields:
        sets.append("game_type = ?"); args.append(fields["game_type"])
    if "enabled" in fields:
        sets.append("enabled = ?"); args.append(int(bool(fields["enabled"])))
    if "auto_spawn" in fields:
        sets.append("auto_spawn = ?"); args.append(int(bool(fields["auto_spawn"])))
    if "embed" in fields:
        sets.append("embed_json = ?"); args.append(_dump(fields["embed"] or {}))
    if "config" in fields:
        sets.append("config_json = ?"); args.append(_dump(fields["config"] or {}))
    if "channel_id" in fields:
        sets.append("channel_id = ?"); args.append(fields["channel_id"])
    sets.append("updated_at = ?"); args.append(_now_iso())
    args += (template_id, guild_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE minigame_templates SET {', '.join(sets)} "
            "WHERE id = ? AND guild_id = ?", args)
        if "rewards" in fields:
            await db.execute(
                "DELETE FROM minigame_rewards WHERE template_id = ?",
                (template_id,))
            await _insert_rewards(db, template_id, fields["rewards"])
        await db.commit()
    return await get_template(guild_id, template_id)


async def delete_template(guild_id: int, template_id: int) -> bool:
    """
    Safe while a run is in progress (the live game holds its own snapshot —
    plan §14). Rewards cascade; the template's bag entry self-heals via the
    staleness guard on the next pop.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM minigame_templates WHERE id = ? AND guild_id = ?",
            (template_id, guild_id))
        await db.execute(
            "DELETE FROM minigame_rewards WHERE template_id = ?", (template_id,))
        await db.commit()
        return cursor.rowcount > 0


async def duplicate_template(guild_id: int, template_id: int,
                             new_name: str | None = None) -> dict | None:
    """
    Deep copy: embed, config, rewards, toggles, channel. Name: an explicit
    new_name wins; otherwise a trailing ' #NN' is auto-incremented
    ('Math #37' → 'Math #38', reusing the next free number) or ' (copy)'
    is appended.
    """
    src = await get_template(guild_id, template_id)
    if not src:
        return None
    name = (new_name or "").strip() or await _auto_copy_name(
        guild_id, src["category_id"], src["name"])
    return await create_template(
        guild_id, src["category_id"], name, src["game_type"],
        enabled=bool(src["enabled"]), auto_spawn=bool(src["auto_spawn"]),
        embed=src["embed"], config=src["config"],
        channel_id=src["channel_id"], rewards=src["rewards"])


async def _auto_copy_name(guild_id: int, category_id: int, name: str) -> str:
    import re
    m = re.search(r"#(\d+)\s*$", name)
    if m:
        start = int(m.group(1))
        existing = set()
        for other in await get_sibling_names(guild_id, category_id):
            om = re.search(r"#(\d+)\s*$", other)
            if om:
                existing.add(int(om.group(1)))
        n = start + 1
        while n in existing:
            n += 1
        return f"{name[:m.start()]}#{n}"
    return f"{name} (copy)"


async def get_sibling_names(guild_id: int, category_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT name FROM minigame_templates "
            "WHERE guild_id = ? AND category_id = ?", (guild_id, category_id))
        return [r[0] for r in await cursor.fetchall()]


async def get_direct_playable_ids(guild_id: int, category_id: int) -> list[int]:
    """
    Template ids DIRECTLY in this category that are playable
    (enabled=1 AND auto_spawn=1) — the recursive selector's bag source
    (plan §4/§5). One query, ids only.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM minigame_templates "
            "WHERE guild_id = ? AND category_id = ? AND enabled = 1 "
            "AND auto_spawn = 1 ORDER BY id ASC",
            (guild_id, category_id))
        return [r[0] for r in await cursor.fetchall()]


# ── REWARDS (pools + independent rolls) ─────────────────────────────────

async def get_rewards(template_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        return await _fetch_rewards(db, template_id)


def roll_reward(rewards: list[dict]) -> dict | None:
    """
    One INDEPENDENT weighted roll (plan §7). Returns the chosen row or
    None for an empty pool (D11: empty pool is valid — callers simply
    grant nothing and record status 'no_reward'). Weights are relative;
    never normalized.
    """
    if not rewards:
        return None
    weights = [max(1, int(r.get("weight") or 1)) for r in rewards]
    return random.choices(rewards, weights=weights, k=1)[0]


# ── SHUFFLE BAGS (plan §5) ──────────────────────────────────────────────

async def get_bag(guild_id: int, category_id: int) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT bag_json FROM minigame_bag_state "
            "WHERE guild_id = ? AND category_id = ?",
            (guild_id, category_id))
        row = await cursor.fetchone()
    return _load(row[0], []) if row else []


async def _save_bag(guild_id: int, category_id: int, bag: list[int]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO minigame_bag_state
                (guild_id, category_id, bag_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, category_id) DO UPDATE SET
                bag_json = excluded.bag_json,
                updated_at = excluded.updated_at
        """, (guild_id, category_id, _dump(bag), _now_iso()))
        await db.commit()


async def pop_bag(guild_id: int, category_id: int,
                  eligible_ids: list[int]) -> tuple[int | None, list[int]]:
    """
    Draw the next template id from this category's bag:
      - no eligible ids → (None, []) (caller: nothing to spawn here)
      - empty/stale bag → rebuilt by shuffling the eligible ids
      - stale entries (ids no longer eligible) are dropped BEFORE the pop
      - pop is without replacement; the remainder is persisted so the
        sequence survives bot restarts (plan §5, §19 — bot is the only
        writer of this table).
    Returns (template_id, remaining_bag).
    """
    eligible = [t for t in eligible_ids]
    if not eligible:
        return None, []
    eligible_set = set(eligible)

    bag = [t for t in await get_bag(guild_id, category_id) if t in eligible_set]
    if not bag:
        bag = list(eligible)
        random.shuffle(bag)
    pick = bag.pop(0)
    await _save_bag(guild_id, category_id, bag)
    return pick, bag


async def rebuild_bag(guild_id: int, category_id: int,
                      eligible_ids: list[int]) -> list[int]:
    """Force a fresh shuffled bag (admin/testing hook + explicit rebuild)."""
    bag = list(eligible_ids)
    random.shuffle(bag)
    await _save_bag(guild_id, category_id, bag)
    return bag


async def clear_bag(guild_id: int, category_id: int) -> None:
    await _save_bag(guild_id, category_id, [])


# ── SPAWN REQUEST QUEUE (plan §9/§19) ───────────────────────────────────

async def create_spawn_request(guild_id: int, template_id: int, mode: str,
                               requested_by: str | None = None
                               ) -> tuple[int | None, str | None]:
    """
    (request_id, error). mode ∈ {manual, test}. Rejects a duplicate
    pending/processing request for the same template ("already queued" —
    plan §16 abuse guard). The caller (API layer) is responsible for the
    D12 permission checks (test always; manual requires enabled).
    """
    if mode not in VALID_REQUEST_MODES:
        return None, f"mode must be one of: {', '.join(VALID_REQUEST_MODES)}"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT COUNT(*) FROM minigame_spawn_requests
            WHERE guild_id = ? AND template_id = ?
              AND status IN ('pending', 'processing')
        """, (guild_id, template_id))
        (in_flight,) = await cursor.fetchone()
        if in_flight:
            return None, "a spawn for this template is already queued"
        cursor = await db.execute("""
            INSERT INTO minigame_spawn_requests
                (guild_id, template_id, mode, requested_by, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (guild_id, template_id, mode, requested_by))
        await db.commit()
        return cursor.lastrowid, None


async def claim_next_request(guild_id: int) -> dict | None:
    """
    Atomically claim the oldest pending request for the guild
    (plan §19: UPDATE … WHERE status='pending' with a rowcount check —
    idempotent even if the poller ever double-fires).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id FROM minigame_spawn_requests
            WHERE guild_id = ? AND status = 'pending'
            ORDER BY id ASC LIMIT 1
        """, (guild_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        claim = await db.execute("""
            UPDATE minigame_spawn_requests
            SET status = 'processing'
            WHERE id = ? AND status = 'pending'
        """, (row[0],))
        await db.commit()
        if claim.rowcount == 0:
            return None  # lost a race — someone else claimed it
        cursor = await db.execute(
            "SELECT id, template_id, mode, requested_by FROM "
            "minigame_spawn_requests WHERE id = ?", (row[0],))
        c = await cursor.fetchone()
        return {"id": c[0], "template_id": c[1], "mode": c[2],
                "requested_by": c[3]}


async def finish_request(request_id: int, ok: bool,
                         error: str | None = None,
                         message_id: int | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if ok:
            # The spawned message id lives on the run log row (set by the
            # spawn path) — the request row just closes here.
            await db.execute("""
                UPDATE minigame_spawn_requests
                SET status = 'done', processed_at = ?, error = NULL
                WHERE id = ?
            """, (_now_iso(), request_id))
        else:
            await db.execute("""
                UPDATE minigame_spawn_requests
                SET status = 'failed', processed_at = ?, error = ?
                WHERE id = ?
            """, (_now_iso(), (error or "unknown error")[:500], request_id))
        await db.commit()


async def get_in_flight_requests(guild_id: int) -> list[dict]:
    """pending + processing requests — the UI's "queued / spawning…" view."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, template_id, mode, requested_at, status
            FROM minigame_spawn_requests
            WHERE guild_id = ? AND status IN ('pending', 'processing')
            ORDER BY id ASC
        """, (guild_id,))
        return [{"id": r[0], "template_id": r[1], "mode": r[2],
                 "requested_at": r[3], "status": r[4]}
                for r in await cursor.fetchall()]


# ── RUN LOG (extended minigames_log, plan §11/§15) ──────────────────────

async def start_run(guild_id: int, template_id: int | None,
                    template_name: str, category_id: int | None,
                    category_name: str, game_type: str, mode: str,
                    channel_id: int | None) -> int:
    """
    Open the run row BEFORE the message is sent (so a send failure still
    leaves an auditable row). Fills the legacy NOT NULL columns:
    event_date = today, tier = the template's direct category name
    (meaningful for legacy rendering). started_at is set for the
    restart-sweep (plan §14).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO minigames_log
                (guild_id, event_date, tier, channel_id, mode,
                 template_id, template_name, category_id, category_name,
                 game_type, status, started_at, forced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """, (guild_id, _today_iso(), category_name or template_name or "-",
              channel_id, mode, template_id, template_name, category_id,
              category_name, game_type, _now_iso(),
              int(mode in ("manual", "test"))))
        await db.commit()
        return cursor.lastrowid


async def set_run_message(log_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE minigames_log SET message_id = ? WHERE id = ?",
            (message_id, log_id))
        await db.commit()


async def finish_run(log_id: int, status: str,
                     participants: list[dict] | None = None,
                     winners: list[dict] | None = None) -> None:
    """
    Close the run. status ∈ completed | no_winner | aborted_restart.
    winners rows: {id, name, reward_type, reward_value, status} where
    status ∈ won | failed | no_reward (D11). The FIRST winner's id/name is
    also written to the legacy winner_id / winner_display_name columns —
    utils/rank_card_data.py and legacy UIs keep working (plan §2 note).
    """
    winners = winners or []
    first_winner = next((w for w in winners if w.get("status") == "won"), None)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE minigames_log
            SET status = ?,
                participants_json = ?,
                winners_json = ?,
                ended_at = ?,
                winner_id = ?,
                winner_display_name = ?
            WHERE id = ?
        """, (status, _dump(participants) if participants is not None else None,
              _dump(winners), _now_iso(),
              first_winner.get("id") if first_winner else None,
              first_winner.get("name") if first_winner else None,
              log_id))
        await db.commit()


async def get_history(guild_id: int, limit: int = 50,
                      category_id: int | None = None) -> list[dict]:
    """New + legacy rows, newest first. v1 rows (template_id NULL) are
    flagged legacy=True and returned as stored."""
    query = "SELECT * FROM minigames_log WHERE guild_id = ?"
    args: list = [guild_id]
    if category_id is not None:
        query += " AND category_id = ?"
        args.append(category_id)
    query += " ORDER BY COALESCE(started_at, fired_at) DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, args)
        cols = [d[0] for d in cursor.description]
        out = []
        for row in await cursor.fetchall():
            r = dict(zip(cols, row))
            r["legacy"] = r.get("template_id") is None
            r["participants"] = _load(r.get("participants_json"), [])
            r["winners"] = _load(r.get("winners_json"), [])
            r.pop("participants_json", None)
            r.pop("winners_json", None)
            out.append(r)
    return out


async def get_open_runs(max_age_minutes: int = 5) -> list[dict]:
    """
    Runs still 'running' (ended_at NULL, status running) older than
    max_age_minutes — the bot's startup sweep finalizes these as
    aborted_restart (plan §14). The age cutoff gives a live bot a grace
    window so the sweep never touches a run that is still healthy.
    """
    cutoff = (_now_utc().timestamp() - max_age_minutes * 60)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, guild_id, channel_id, message_id, mode, template_id,
                   started_at
            FROM minigames_log
            WHERE ended_at IS NULL AND status = 'running'
        """)
        rows = await cursor.fetchall()
    out = []
    for r in rows:
        started = None
        if r[6]:
            try:
                started = datetime.fromisoformat(r[6])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
            except ValueError:
                started = None
        # No parseable started_at → treat as stale (safe: it is by
        # definition not a healthy in-memory run).
        if started is None or started.timestamp() <= cutoff:
            out.append({"id": r[0], "guild_id": r[1], "channel_id": r[2],
                        "message_id": r[3], "mode": r[4], "template_id": r[5]})
    return out
