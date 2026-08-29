"""
Phase 1 verification harness — utils/minigame_store.py

Runs against a throwaway SQLite DB (DATABASE_PATH is pointed at a temp
file BEFORE database.py is imported). Re-runnable; doubles as the seed
for the Phase 7 logic-test suite (plan §20).

Usage:  python scripts/test_minigame_store.py
"""
import asyncio
import os
import random
import sys
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="mgstore_test_")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
# database.py hard-exits without OWNER_ID — a dummy is enough for store tests
os.environ.setdefault("OWNER_ID", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.minigame_store as ms  # noqa: E402

GUILD = 111111111111111111
GUILD2 = 222222222222222222

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label} {extra}")


def section(title: str):
    print(f"\n── {title} " + "─" * max(0, 50 - len(title)))


async def test_schema():
    section("schema / ensure_tables")
    await ms.ensure_tables()
    import aiosqlite
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
    for t in ("minigame_categories", "minigame_templates", "minigame_rewards",
              "minigame_bag_state", "minigame_spawn_requests",
              "minigames_config", "minigames_log"):
        check(f"table {t} exists", t in tables)
    # v2 columns present on the legacy log
    async with aiosqlite.connect(DB_PATH) as db:
        cols = {r[1] for r in await (
            await db.execute("PRAGMA table_info(minigames_log)")).fetchall()}
    for c in ("template_id", "template_name", "category_id", "category_name",
              "game_type", "mode", "participants_json", "winners_json",
              "started_at", "ended_at", "status"):
        check(f"log column {c}", c in cols)
    # idempotent re-run
    await ms.ensure_tables()
    check("ensure_tables idempotent", True)


async def test_migration():
    section("tier→category migration")
    import aiosqlite
    from database import DB_PATH
    # The legacy table must exist for the migration to read it; seed tiers.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS minigames_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                tier TEXT NOT NULL,
                weight INTEGER DEFAULT 1,
                reward_type TEXT NOT NULL,
                reward_value TEXT NOT NULL,
                reward_duration_hours INTEGER,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for tier, w, rt, rv in (("bronze", 50, "coins", "100"),
                                ("silver", 30, "xp", "500"),
                                ("gold", 15, "diamonds", "1"),
                                ("platinum", 5, "item", "Starter Pack")):
            await db.execute(
                "INSERT INTO minigames_tiers "
                "(guild_id, tier, weight, reward_type, reward_value) "
                "VALUES (?, ?, ?, ?, ?)", (GUILD, tier, w, rt, rv))
        # duplicate tier row — migration must skip it (first wins)
        await db.execute(
            "INSERT INTO minigames_tiers "
            "(guild_id, tier, weight, reward_type, reward_value) "
            "VALUES (?, 'bronze', 99, 'coins', '999')", (GUILD,))
        await db.commit()
    await ms.run_migration()
    tree = await ms.get_categories_tree(GUILD)
    check("4 root categories created", len(tree) == 4, f"got {len(tree)}")
    bronze = next(c for c in tree if c["name"] == "Bronze")
    check("bronze weight preserved", bronze["weight"] == 50)
    check("bronze preset migrated",
          bronze["default_rewards"] == [
              {"type": "coins", "value": "100", "duration_hours": None,
               "weight": 1}], str(bronze["default_rewards"]))
    # idempotency: re-run must not duplicate
    await ms.run_migration()
    tree = await ms.get_categories_tree(GUILD)
    check("migration idempotent", len(tree) == 4, f"got {len(tree)}")
    # guild without categories is untouched by other guilds' data
    check("other guild has no categories",
          len(await ms.get_categories_tree(GUILD2)) == 0)


async def test_categories():
    section("category CRUD / tree / cycle check")
    root = await ms.create_category(GUILD2, "Bronze", weight=50)
    sub = await ms.create_category(GUILD2, "Button Games",
                                   parent_id=root["id"], weight=30)
    deep = await ms.create_category(GUILD2, "Nested", parent_id=sub["id"])
    check("created with ids", root["id"] and sub["id"] and deep["id"])

    tree = await ms.get_categories_tree(GUILD2)
    check("one root", len(tree) == 1 and tree[0]["id"] == root["id"])
    check("nested children", len(tree[0]["children"]) == 1
          and tree[0]["children"][0]["children"][0]["id"] == deep["id"])

    # counts
    t1 = await ms.create_template(GUILD2, root["id"], "A", "math",
                                  embed={"title": "x"})
    t2 = await ms.create_template(GUILD2, sub["id"], "B", "quick_click")
    t3 = await ms.create_template(GUILD2, deep["id"], "C", "wheel",
                                  auto_spawn=False)
    tree = await ms.get_categories_tree(GUILD2)
    r = tree[0]
    check("root direct counts", r["direct_templates"] == 1
          and r["direct_playable"] == 1, str(r["direct_templates"]))
    check("root subtree_playable=2 (C excluded: rotation off)",
          r["subtree_playable"] == 2, str(r["subtree_playable"]))
    s = r["children"][0]
    # sub's subtree = B (playable) + deep's C (rotation OFF) → 1
    check("sub direct counts (C excluded from subtree_playable)",
          s["direct_templates"] == 1 and s["subtree_playable"] == 1,
          f"direct={s['direct_templates']} subtree={s['subtree_playable']}")

    # rename (safe: id references)
    upd = await ms.update_category(GUILD2, root["id"], {"name": "Weekly Games"})
    check("rename ok", upd["name"] == "Weekly Games")
    check("template still attached after rename",
          (await ms.get_template(GUILD2, t1["id"]))["category_id"] == root["id"])

    # move + cycle checks
    ok_move = await ms.update_category(GUILD2, deep["id"],
                                       {"parent_id": root["id"]})
    check("move to sibling level ok", ok_move["parent_id"] == root["id"])
    try:
        await ms.update_category(GUILD2, root["id"], {"parent_id": sub["id"]})
        check("cycle: root under its own descendant rejected", False)
    except ValueError:
        check("cycle: root under its own descendant rejected", True)
    try:
        await ms.update_category(GUILD2, root["id"], {"parent_id": root["id"]})
        check("cycle: self-parent rejected", False)
    except ValueError:
        check("cycle: self-parent rejected", True)

    # delete guards
    ok, msg = await ms.delete_category(GUILD2, root["id"])
    check("delete blocked while populated", not ok and "template" in msg, msg)
    ok, msg = await ms.delete_category(GUILD2, deep["id"])
    check("delete blocked while deep still holds template C",
          not ok and "template" in msg, msg)
    await ms.delete_template(GUILD2, t3["id"])
    ok, msg = await ms.delete_category(GUILD2, deep["id"])
    check("delete empty category ok", ok, msg)
    ok, _ = await ms.delete_category(GUILD2, 999999)
    check("delete missing → error msg", not ok)


async def test_templates():
    section("template CRUD / duplicate / rewards atomicity")
    cat = (await ms.get_categories_tree(GUILD2))[0]
    t = await ms.create_template(
        GUILD2, cat["id"], "Math #37", "math",
        embed={"title": "🧮", "description": "15 × 7 = ?"},
        config={"question": "15 × 7 = ?", "answers": ["95", "105", "115", "125"],
                "correct": 1, "seconds": 30},
        rewards=[{"reward_type": "xp", "reward_value": "100", "weight": 5},
                 {"reward_type": "coins", "reward_value": "50", "weight": 3}])
    check("template saved with rewards", len(t["rewards"]) == 2)
    check("embed round-trip", t["embed"]["title"] == "🧮")
    check("config round-trip", t["config"]["correct"] == 1)

    # empty pool is valid (D11)
    t_empty = await ms.create_template(GUILD2, cat["id"], "Fun Only", "wheel",
                                       rewards=[])
    check("empty reward pool accepted (D11)",
          t_empty is not None and t_empty["rewards"] == [])

    # duplicate: auto #NN increment
    d = await ms.duplicate_template(GUILD2, t["id"])
    check("duplicate auto-name #38", d["name"] == "Math #38", d["name"])
    check("duplicate deep-copies rewards",
          [r["reward_value"] for r in d["rewards"]] == ["100", "50"])
    check("duplicate deep-copies embed/config",
          d["embed"] == t["embed"] and d["config"] == t["config"])
    d2 = await ms.duplicate_template(GUILD2, t["id"])
    check("duplicate skips taken number → #39", d2["name"] == "Math #39",
          d2["name"])

    # update: atomic rewards replacement
    u = await ms.update_template(GUILD2, t["id"], {
        "name": "Math #37 (edited)",
        "rewards": [{"reward_type": "diamonds", "reward_value": "1",
                     "weight": 10}],
    })
    check("update replaces pool atomically",
          len(u["rewards"]) == 1 and u["rewards"][0]["reward_type"] == "diamonds")
    check("update name", u["name"] == "Math #37 (edited)")

    # invalid reward type rejected (data integrity)
    try:
        await ms.update_template(GUILD2, t["id"],
                                 {"rewards": [{"reward_type": "voucher",
                                               "reward_value": "x",
                                               "weight": 1}]})
        check("invalid reward_type rejected", False)
    except ValueError:
        check("invalid reward_type rejected", True)
    # pool intact after the failed update (transactional)
    t_now = await ms.get_template(GUILD2, t["id"])
    check("pool intact after failed update",
          len(t_now["rewards"]) == 1
          and t_now["rewards"][0]["reward_type"] == "diamonds")

    # list: subtree vs direct
    sub = cat["children"][0]
    lst_direct = await ms.list_templates(GUILD2, sub["id"], subtree=False)
    check("direct list excludes subtree", all(x["category_id"] == sub["id"]
                                              for x in lst_direct))
    lst_sub = await ms.list_templates(GUILD2, sub["id"], subtree=True)
    check("subtree list includes descendants",
          len(lst_sub) >= len(lst_direct))
    check("list carries category name + has_reward_pool",
          all("category_name" in x and "has_reward_pool" in x
              for x in lst_sub))

    # delete: rewards cascade; in-flight-safe (store level)
    ok = await ms.delete_template(GUILD2, d["id"])
    check("delete template ok", ok)
    check("rewards cascade-deleted",
          (await ms.get_rewards(d["id"])) == [])
    check("deleted template gone",
          (await ms.get_template(GUILD2, d["id"])) is None)


async def test_bags():
    section("shuffle bag: no-replacement / reshuffle / staleness")
    cat = (await ms.get_categories_tree(GUILD2))[0]
    await ms.clear_bag(GUILD2, cat["id"])
    eligible = [10, 20, 30]

    seq = []
    for _ in range(6):  # 2 full cycles over 3 templates
        pick, remain = await ms.pop_bag(GUILD2, cat["id"], eligible)
        seq.append(pick)
    check("first cycle: no repeats before exhaustion",
          len(set(seq[:3])) == 3, str(seq))
    check("second cycle: no repeats, reshuffled",
          len(set(seq[3:6])) == 3, str(seq))
    check("bag emptied after 6 pops", remain == [], str(remain))

    # staleness: template 20 removed from eligibility → dropped on next pop
    await ms.rebuild_bag(GUILD2, cat["id"], [10, 20, 30])
    pick, remain = await ms.pop_bag(GUILD2, cat["id"], [10, 30])
    check("stale id dropped (20 never returned)",
          pick in (10, 30) and 20 not in remain, f"pick={pick} remain={remain}")

    pick, remain = await ms.pop_bag(GUILD2, cat["id"], [])
    check("no eligible → (None, [])", pick is None and remain == [])

    # persistence: read back the saved bag state
    bag = await ms.get_bag(GUILD2, cat["id"])
    check("bag state persisted in DB", isinstance(bag, list)
          and all(x in (10, 30) for x in bag), str(bag))


async def test_queue():
    section("spawn request queue")
    tpl = (await ms.list_templates(GUILD2))[0]
    rid, err = await ms.create_spawn_request(GUILD2, tpl["id"], "test",
                                             requested_by="admin@dash")
    check("request created", rid is not None and err is None, str(err))
    rid2, err2 = await ms.create_spawn_request(GUILD2, tpl["id"], "manual")
    check("duplicate in-flight rejected", rid2 is None
          and "already queued" in (err2 or ""), str(err2))
    other_tpl = (await ms.list_templates(GUILD2))[1]
    rid3, _ = await ms.create_spawn_request(GUILD2, other_tpl["id"], "manual")
    check("different template allowed", rid3 is not None)

    claim = await ms.claim_next_request(GUILD2)
    check("oldest claimed atomically", claim is not None
          and claim["id"] == min(rid, rid3), str(claim))
    in_flight = await ms.get_in_flight_requests(GUILD2)
    check("in-flight = claimed(processing) + other(pending)",
          len(in_flight) == 2
          and {p["status"] for p in in_flight} == {"processing", "pending"},
          str(in_flight))

    await ms.finish_request(claim["id"], ok=True)
    in_flight = await ms.get_in_flight_requests(GUILD2)
    check("claimed request closed", all(p["id"] != claim["id"]
                                        for p in in_flight))
    # after close, a new request for the same template is allowed
    rid4, err4 = await ms.create_spawn_request(GUILD2, tpl["id"], "test")
    check("re-queue after close allowed", rid4 is not None, str(err4))
    if rid4:
        await ms.finish_request(rid4, ok=False, error="channel deleted")
    # cleanup the leftover
    for p in await ms.get_in_flight_requests(GUILD2):
        await ms.finish_request(p["id"], ok=False, error="harness cleanup")


async def test_runs():
    section("run log: start/message/finish/history/sweep")
    tpl = (await ms.list_templates(GUILD2))[0]
    cat = (await ms.get_categories_tree(GUILD2))[0]

    # legacy v1 row for history rendering
    import aiosqlite
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO minigames_log "
            "(guild_id, event_date, tier, winner_id, winner_display_name, forced)"
            " VALUES (?, '2026-08-01', 'bronze', 42, 'LegacyUser', 1)",
            (GUILD2,))
        await db.commit()

    run1 = await ms.start_run(GUILD2, tpl["id"], tpl["name"], cat["id"],
                              cat["name"], tpl["game_type"], "auto", 555)
    await ms.set_run_message(run1, 9001)
    await ms.finish_run(
        run1, "completed",
        participants=[{"id": 1, "display_name": "P1"},
                      {"id": 2, "display_name": "P2"}],
        winners=[{"id": 1, "name": "P1", "reward_type": "xp",
                  "reward_value": "100", "status": "won"},
                 {"id": 2, "name": "P2", "reward_type": "xp",
                  "reward_value": "100", "status": "failed",
                  "error": "role above bot"}])

    # empty-pool run (D11)
    run2 = await ms.start_run(GUILD2, tpl["id"], tpl["name"], cat["id"],
                              cat["name"], "wheel", "test", 555)
    await ms.finish_run(
        run2, "completed",
        participants=[{"id": 3, "display_name": "P3"}],
        winners=[{"id": 3, "name": "P3", "reward_type": None,
                  "reward_value": None, "status": "no_reward"}])

    hist = await ms.get_history(GUILD2, limit=50)
    check("history: 3 rows newest-first", len(hist) == 3, str(len(hist)))
    # run1 is the one carrying two participants (run2 has one, legacy none)
    run1_row = next(h for h in hist
                    if len(h["participants"]) == 2)
    check("history: participants parsed",
          [p["display_name"] for p in run1_row["participants"]]
          == ["P1", "P2"])
    check("history: winners parsed incl. failure",
          [w["status"] for w in run1_row["winners"]] == ["won", "failed"])
    check("history: D11 no_reward recorded",
          any(w["status"] == "no_reward"
              for h in hist for w in h["winners"]))
    legacy = [h for h in hist if h["legacy"]]
    check("history: legacy v1 row flagged", len(legacy) == 1
          and legacy[0]["tier"] == "bronze")
    check("history: legacy winner_id preserved",
          legacy[0]["winner_id"] == 42)

    # rank-card compatibility: first winner on legacy columns
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT winner_id FROM minigames_log WHERE id = ?", (run1,))
        row = await cur.fetchone()
    check("rank-card: first winner written to winner_id",
          row and row[0] == 1, str(row))

    # open runs / sweep
    run3 = await ms.start_run(GUILD2, tpl["id"], tpl["name"], cat["id"],
                              cat["name"], "rps", "auto", 555)
    open_runs = await ms.get_open_runs(max_age_minutes=5)
    check("sweep: fresh run NOT swept (grace window)",
          all(r["id"] != run3 for r in open_runs), str(open_runs))
    # age it artificially
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE minigames_log SET started_at = "
            "datetime('now', '-1 hour') WHERE id = ?", (run3,))
        await db.commit()
    open_runs = await ms.get_open_runs(max_age_minutes=5)
    check("sweep: stale run found", any(r["id"] == run3
                                        for r in open_runs))
    await ms.finish_run(run3, "aborted_restart", participants=[], winners=[])
    open_runs = await ms.get_open_runs()
    check("sweep: closed run gone", all(r["id"] != run3
                                        for r in open_runs))


async def test_config_and_rolls():
    section("config round-trip + reward rolls")
    cfg = await ms.save_config(
        GUILD2, enabled=True, channel_id=777, min_events_per_week=3,
        max_events_per_week=9,
        global_default_rewards=[{"reward_type": "coins",
                                 "reward_value": "25", "weight": 1}])
    check("config saved", cfg["channel_id"] == 777
          and cfg["min_events_per_week"] == 3)
    check("global preset round-trip",
          cfg["global_default_rewards"][0]["reward_value"] == "25")
    cfg = await ms.save_config(GUILD2, max_events_per_week=12)
    check("partial update keeps other keys",
          cfg["channel_id"] == 777 and cfg["max_events_per_week"] == 12)
    await ms.bump_events_this_week(GUILD2)
    cfg = await ms.get_config(GUILD2)
    check("weekly counter bumped", cfg["events_this_week"] == 1)

    # rolls: empty pool → None (D11)
    check("roll on empty pool → None", ms.roll_reward([]) is None)
    pool = [{"reward_type": "coins", "reward_value": "10", "weight": 2},
            {"reward_type": "xp", "reward_value": "5", "weight": 1}]
    counts = {"coins": 0, "xp": 0}
    for _ in range(10000):
        counts[ms.roll_reward(pool)["reward_type"]] += 1
    ratio = counts["coins"] / max(1, counts["xp"])
    check("roll distribution ≈ 2:1 (relative weights)",
          1.8 < ratio < 2.2, f"coins={counts['coins']} xp={counts['xp']}")
    # independent rolls can repeat (allowed by design)
    dup = sum(1 for _ in range(500)
              if ms.roll_reward(pool) is ms.roll_reward(pool))
    check("independent rolls can produce the same reward", dup > 100,
          f"dups={dup}/500")


async def main():
    await test_schema()
    await test_migration()
    await test_categories()
    await test_templates()
    await test_bags()
    await test_queue()
    await test_runs()
    await test_config_and_rolls()
    print(f"\n{'=' * 50}\nRESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    random.seed()
    asyncio.run(main())
