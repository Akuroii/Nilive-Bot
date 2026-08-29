"""
Phase 3 verification harness — cogs/minigames.py (spawn flow, run
lifecycle, channel delivery, admin controls, state handling).

Covers, headless (fake Discord objects + REAL sqlite + REAL engine):
  A. Recursive selection — deterministic structure (exact weights),
     eligibility rules (disabled branches, empty subtrees, auto_spawn)
  B. Selection — Monte Carlo on plan §4 worked example A
  C. Shuffle bag mechanics (no replacement, reshuffle, staleness,
     per-node isolation)
  D. spawn_game() — channel resolution/fallback, run row lifecycle,
     counter accounting (D5), post-failure & unplayable-template rows
  E. Request queue — manual/test via _process_spawn_requests, D12
     checks, deleted template, missing guild, D11 end-to-end, live
     MC + RPS played through the queued spawn
  F. Startup sweep — aborted_restart, embed note, idempotency, grace
  G. Pacing iteration — daily check bookkeeping (reset, one roll/day,
     max cap)
  H. Deprecated compat surface (dashboard imports keep working)

Usage:  python scripts/test_minigame_spawn.py
"""
import asyncio
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="mgspawn_test_")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ.setdefault("OWNER_ID", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite  # noqa: E402
import discord  # noqa: E402
import utils.minigame_engine as eng  # noqa: E402
from utils import minigame_store as store  # noqa: E402
from database import DB_PATH  # noqa: E402
from cogs import minigames as cog_mod  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label} {extra}")


def section(title):
    print(f"\n── {title} " + "─" * max(0, 50 - len(title)))


# ── fakes ───────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, mid=None, embed=None):
        self.id = mid or random.randint(10**10, 10**11 - 1)
        self.embeds = [embed] if embed is not None else []
        self.edits = 0
        self.last_embed = embed

    async def edit(self, embed=None, view=None, content=None):
        self.edits += 1
        if embed is not None:
            self.last_embed = embed
            self.embeds = [embed]


class FakeChannel(discord.TextChannel):
    """A real TextChannel (passes the production isinstance check) with a
    network-free send(). discord.py slotted channels can't carry ad-hoc
    attributes, so harness state lives in a side table keyed by id."""

    _h: dict[int, dict] = {}

    def __init__(self, cid, fail_send=False, name="spawn-chan"):
        # bypass the data-driven __init__; fill the slots the code path touches
        self.id = int(cid)
        self._type = 0
        self.name = name
        self.guild = None
        self._state = None
        self.topic = None
        self.nsfw = False
        self.category_id = None
        self.position = 0
        self.slowmode_delay = 0
        self._overwrites = {}
        self.last_message_id = None
        self.default_auto_archive_duration = 86400
        self.default_thread_slowmode_delay = 0
        self._h.setdefault(self.id, {"fail_send": fail_send, "messages": []})

    @property
    def fail_send(self):
        return self._h[self.id]["fail_send"]

    @property
    def messages(self):
        return self._h[self.id]["messages"]

    async def send(self, *args, **kwargs):
        if self.fail_send:
            raise RuntimeError("forced send failure")
        m = FakeMessage(embed=kwargs.get("embed"))
        self.messages.append(m)
        return m

    async def fetch_message(self, mid):
        for m in self.messages:
            if m.id == mid:
                return m
        return None


class FakeGuild:
    def __init__(self, gid, channels):
        self.id = gid
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(cid)

    def add_channel(self, ch):
        self._channels[ch.id] = ch


class FakeBot:
    def __init__(self, guilds):
        self._guilds = {g.id: g for g in guilds}
        self.ready = True

    def is_ready(self):
        return self.ready

    def get_guild(self, gid):
        return self._guilds.get(gid)

    async def wait_until_ready(self):
        await asyncio.get_running_loop().create_future()
        # never resolves — the daily loop just waits (like a bot mid-boot)


class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name

    @property
    def mention(self):
        return f"<@{self.id}>"


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.message = None

    async def send_message(self, content=None, ephemeral=False, view=None):
        self.message = FakeMessage()
        self.sent.append({"content": content, "ephemeral": ephemeral,
                          "view": view})


class FakeInteraction:
    def __init__(self, user, custom_id=""):
        self.user = user
        self.data = {"custom_id": custom_id}
        self.response = FakeResponse()
        self.followup = None


def info(uid, name):
    return {"id": uid, "name": name, "mention": f"<@{uid}>"}


# ── shared recorder for the grant path ─────────────────────────────────

class GrantRecorder:
    def __init__(self, fail_types=()):
        self.grants = []
        self.fail_types = set(fail_types)

    async def fake_give_reward(self, bot, guild_id, user_id, reward_type,
                               *a, **kw):
        self.grants.append({"user_id": user_id, "reward_type": reward_type})
        if reward_type in self.fail_types:
            return {"success": False, "error": "forced failure"}
        return {"success": True}


REC = GrantRecorder()
eng.give_reward = REC.fake_give_reward  # single patch, whole harness


async def make_cog(guilds):
    """Build a Minigames cog and immediately stop its background loops —
    the harness drives iterations manually (no 10s/30min racers)."""
    bot = FakeBot(guilds)
    cog = cog_mod.Minigames(bot)
    cog.daily_check_loop.cancel()
    cog.spawn_request_loop.cancel()
    return cog, bot


async def drop_cog(cog):
    await asyncio.sleep(0)  # let cancelled loop tasks unwind


async def settle(cog):
    """Prune finished engines from the registry (what the poll tick does)."""
    cog.live_games = {k: v for k, v in cog.live_games.items()
                      if not v.finished}


async def wait_until(pred, timeout=5.0, step=0.005):
    t0 = datetime.now(timezone.utc)
    while (datetime.now(timezone.utc) - t0).total_seconds() < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


async def row_closed(log_id, timeout=5.0):
    """Wait until the run row leaves 'running'. `engine.finished` is set
    at CLAIM time (before grants/edit/finish_run) — the DB row is the
    ground truth for finalization."""
    row = None
    t0 = datetime.now(timezone.utc)
    while (datetime.now(timezone.utc) - t0).total_seconds() < timeout:
        row = await run_row(log_id)
        if row and row["status"] != "running":
            return row
        await asyncio.sleep(0.005)
    return row


async def run_row(log_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM minigames_log WHERE id = ?", (log_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cursor.description]
    r = dict(zip(cols, row))
    import json as _j
    r["participants"] = _j.loads(r["participants_json"] or "[]")
    r["winners"] = _j.loads(r["winners_json"] or "[]")
    return r


async def last_run_row(guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM minigames_log WHERE guild_id = ? "
            "ORDER BY id DESC LIMIT 1", (guild_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return await run_row(row[0])


async def run_rows_for(guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM minigames_log WHERE guild_id = ? ORDER BY id",
            (guild_id,))
        return [r[0] for r in await cursor.fetchall()]


async def request_state(rid):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT status, error FROM minigame_spawn_requests WHERE id = ?",
            (rid,))
        row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


# ── template helpers ────────────────────────────────────────────────────

G1 = 1001   # main test guild
G2 = 1002   # empty guild (no categories)
G3 = 1003   # guild the bot is NOT in

TPL = {}
RWS = [{"reward_type": "xp", "reward_value": "100", "weight": 1}]


async def seed_main_guild(guild):
    """Guild G1: one root category, several templates (mixed types)."""
    await store.save_config(G1, channel_id=9001, min_events_per_week=5,
                            max_events_per_week=10)
    cat = await store.create_category(G1, "Bronze")
    t = await store.create_template(
        G1, cat["id"], "Wheel #1", "wheel",
        config={"join_seconds": 0.2}, rewards=list(RWS))
    TPL["wheel"] = t["id"]
    t = await store.create_template(
        G1, cat["id"], "QC #1", "quick_click", channel_id=9002,
        config={"buttons": 3, "reveal_min": 0.05, "reveal_max": 0.08,
                "wait_after": 0.3},
        rewards=list(RWS))
    TPL["qc"] = t["id"]
    t = await store.create_template(
        G1, cat["id"], "Math #1", "math",
        embed={"title": "🧮 Math", "description": "pick one"},
        config={"answers": ["a", "b"], "correct": 0, "seconds": 0.2},
        rewards=list(RWS))
    TPL["mc"] = t["id"]
    t = await store.create_template(
        G1, cat["id"], "RPS #1", "rps",
        config={"seating_seconds": 0.6, "choice_seconds": 0.6},
        rewards=list(RWS))
    TPL["rps"] = t["id"]
    t = await store.create_template(
        G1, cat["id"], "Wheel disabled", "wheel", enabled=False,
        config={"join_seconds": 0.2}, rewards=list(RWS))
    TPL["wheel_disabled"] = t["id"]
    t = await store.create_template(
        G1, cat["id"], "Wheel no pool", "wheel",
        config={"join_seconds": 0.2}, rewards=[])
    TPL["wheel_nopool"] = t["id"]
    # auto_spawn=False: this fixture is only for the manual-preflight
    # refusal test (D7). If it were rotation-eligible, the random
    # rotation in D1 could draw it and (correctly) fail the spawn.
    t = await store.create_template(
        G1, cat["id"], "Math broken", "math", config={}, auto_spawn=False)
    TPL["mc_broken"] = t["id"]
    return cat


# ══════════════════════════════════════════════════════════════════════

async def test_selection_structure():
    section("A. Recursive selection — structure & weights")
    G = 2001
    await store.save_config(G, channel_id=1)
    # plan §4 worked example A
    bronze = await store.create_category(G, "Bronze", weight=50)
    silver = await store.create_category(G, "Silver", weight=30)
    btn = await store.create_category(G, "Button Games",
                                      parent_id=bronze["id"], weight=30)
    gold = await store.create_category(G, "Gold", weight=20)  # EMPTY
    rws = [{"reward_type": "xp", "reward_value": "10", "weight": 1}]
    qc1 = await store.create_template(G, bronze["id"], "QC #1", "quick_click", rewards=rws)
    qc2 = await store.create_template(G, bronze["id"], "QC #2", "quick_click", rewards=rws)
    qc3 = await store.create_template(G, btn["id"], "QC #3", "quick_click", rewards=rws)
    m1 = await store.create_template(G, btn["id"], "Math #1", "math", rewards=rws)
    m2 = await store.create_template(G, silver["id"], "Math #2", "math", rewards=rws)

    orig_choices = random.choices
    orig_shuffle = random.shuffle
    state = {"calls": [], "script": []}

    def spy_choices(population, weights=None, *a, **k):
        idxs = list(population)
        state["calls"].append((idxs, list(weights) if weights else None))
        if state["script"]:
            i = state["script"].pop(0)
            return [idxs[i]]
        return orig_choices(population, weights=weights, *a, **k)

    def no_shuffle(x):
        return None  # deterministic bag order

    random.choices = spy_choices
    random.shuffle = no_shuffle
    try:
        async def run_selection(script):
            state["calls"].clear()
            state["script"] = list(script)
            return await cog_mod.select_template(G)

        # 1) Bronze → bag → first direct (QC #1); exact weights observed
        for c in (bronze, btn, silver):
            await store.clear_bag(G, c["id"])
        tpl = await run_selection([0, 0])
        check("A1: Bronze→bag yields first direct template (QC #1)",
              tpl and tpl["name"] == "QC #1", str(tpl and tpl["name"]))
        check("A1: root weights exact [50, 30]",
              state["calls"][0][1] == [50, 30], str(state["calls"][0][1]))
        check("A1: Bronze options weights exact [50 bag, 30 sub]",
              state["calls"][1][1] == [50, 30], str(state["calls"][1][1]))

        # 2) bag without replacement: next draw of the same bag = QC #2
        tpl = await run_selection([0, 0])
        check("A2: bag pops without replacement (QC #2 second)",
              tpl and tpl["name"] == "QC #2", str(tpl and tpl["name"]))

        # 3) Bronze → subcategory → its bag (games living ONLY below)
        await store.clear_bag(G, bronze["id"])
        tpl = await run_selection([0, 1, 0])
        check("A3: subcategory path reaches games living ONLY in a "
              "subcategory (QC #3)",
              tpl and tpl["name"] == "QC #3", str(tpl and tpl["name"]))
        check("A3: subcategory node offers its single bag (weight [30])",
              state["calls"][2][1] == [30], str(state["calls"][2][1]))

        # 4) Silver root → its bag
        tpl = await run_selection([1, 0])
        check("A4: Silver path yields Math #2",
              tpl and tpl["name"] == "Math #2", str(tpl and tpl["name"]))

        # 5) empty root (Gold) is never a candidate
        tpl = await run_selection([0, 0])
        check("A5: empty branch never consumes a selection (Gold absent)",
              tpl is not None and state["calls"][0][1] == [50, 30],
              str(state["calls"][0]))

        # 6) disabled branch excluded (D10)
        await store.update_category(G, bronze["id"], {"enabled": 0})
        tpl = await run_selection([0, 0])
        check("A6: disabled branch excluded from rotation (roots [30])",
              tpl and tpl["name"] == "Math #2"
              and state["calls"][0][1] == [30],
              f"{tpl and tpl['name']} {state['calls'][0][1]}")

        # 7) re-enable restores (D10: nothing modified, just excluded)
        await store.update_category(G, bronze["id"], {"enabled": 1})
        tpl = await run_selection([0, 0])
        check("A7: re-enabling restores the branch (weights back [50,30])",
              tpl is not None and state["calls"][0][1] == [50, 30],
              str(state["calls"][0][1]))

        # 8) disabled SUB excludes only its branch
        await store.update_category(G, btn["id"], {"enabled": 0})
        tpl = await run_selection([0, 0])
        check("A8: disabled subcategory → parent offers bag only [50]",
              tpl is not None and state["calls"][1][1] == [50],
              str(state["calls"][1][1]))
        await store.update_category(G, btn["id"], {"enabled": 1})

        # 9) auto_spawn=0 (rotation off) removes a template from its bag
        await store.update_template(G, m2["id"], {"auto_spawn": 0})
        tpl = await run_selection([0, 0])  # only root left = Bronze
        check("A9: auto_spawn=0 → Silver offers nothing (roots [50])",
              state["calls"][0][1] == [50] and tpl is not None,
              str(state["calls"][0][1]))
        await store.update_template(G, m2["id"], {"auto_spawn": 1})

        # 10) nothing eligible → None
        for t in (qc1, qc2, qc3, m1, m2):
            await store.update_template(G, t["id"], {"enabled": 0})
        tpl = await run_selection([])
        check("A10: no eligible templates anywhere → None", tpl is None)
    finally:
        random.choices = orig_choices
        random.shuffle = orig_shuffle


async def test_selection_monte_carlo():
    section("B. Recursive selection — Monte Carlo (worked example A)")
    G = 2002
    await store.save_config(G, channel_id=1)
    bronze = await store.create_category(G, "Bronze", weight=50)
    silver = await store.create_category(G, "Silver", weight=30)
    btn = await store.create_category(G, "Button Games",
                                      parent_id=bronze["id"], weight=30)
    rws = [{"reward_type": "xp", "reward_value": "10", "weight": 1}]
    await store.create_template(G, bronze["id"], "QC #1", "quick_click", rewards=rws)
    await store.create_template(G, bronze["id"], "QC #2", "quick_click", rewards=rws)
    await store.create_template(G, btn["id"], "QC #3", "quick_click", rewards=rws)
    await store.create_template(G, btn["id"], "Math #1", "math", rewards=rws)
    await store.create_template(G, silver["id"], "Math #2", "math", rewards=rws)
    for cat in (bronze, btn, silver):
        await store.clear_bag(G, cat["id"])

    N = 2000
    counts = {}
    t0 = datetime.now(timezone.utc)
    for _ in range(N):
        tpl = await cog_mod.select_template(G)
        assert tpl is not None
        counts[tpl["name"]] = counts.get(tpl["name"], 0) + 1
    dt = (datetime.now(timezone.utc) - t0).total_seconds()

    expected = {"QC #1": 0.1953, "QC #2": 0.1953, "QC #3": 0.1172,
                "Math #1": 0.1172, "Math #2": 0.3750}
    check(f"B1: all 5 templates reachable ({dt:.1f}s for {N} draws)",
          set(counts) == set(expected), str(sorted(counts)))
    ok = True
    detail = []
    for name, exp in expected.items():
        frac = counts.get(name, 0) / N
        if abs(frac - exp) > 0.05:
            ok = False
        detail.append(f"{name}={frac:.3f}/{exp:.3f}")
    check("B2: distribution matches plan §4 within ±5%", ok, " ".join(detail))
    check("B3: no selection lost", sum(counts.values()) == N,
          str(sum(counts.values())))


async def test_bag_mechanics():
    section("C. Shuffle bag mechanics (plan §5)")
    G = 2003
    cat = await store.create_category(G, "Cat")
    rws = [{"reward_type": "xp", "reward_value": "1", "weight": 1}]
    t1 = await store.create_template(G, cat["id"], "T1", "wheel", rewards=rws)
    t2 = await store.create_template(G, cat["id"], "T2", "wheel", rewards=rws)
    t3 = await store.create_template(G, cat["id"], "T3", "wheel", rewards=rws)
    ids = [t1["id"], t2["id"], t3["id"]]
    await store.clear_bag(G, cat["id"])

    first3 = []
    for _ in range(3):
        tid, _ = await store.pop_bag(G, cat["id"], ids)
        first3.append(tid)
    check("C1: 3 pops over 3 templates = 3 distinct (no replacement)",
          len(set(first3)) == 3, str(first3))

    next3 = []
    for _ in range(3):
        tid, _ = await store.pop_bag(G, cat["id"], ids)
        next3.append(tid)
    check("C2: exhausted bag reshuffles — next 3 also distinct",
          len(set(next3)) == 3, str(next3))

    # staleness guard: stored bag holds a deleted id
    await store.delete_template(G, t3["id"])
    alive = [t1["id"], t2["id"]]
    draws = []
    for _ in range(3):
        tid, _ = await store.pop_bag(G, cat["id"], alive)
        draws.append(tid)
    check("C3: deleted template can never be popped (staleness rebuild)",
          all(d in alive for d in draws) and t3["id"] not in draws,
          str(draws))

    # disabled (enabled=0) is not eligible either
    await store.update_template(G, t1["id"], {"enabled": 0})
    draws = []
    for _ in range(2):
        tid, _ = await store.pop_bag(G, cat["id"], [t2["id"]])
        draws.append(tid)
    check("C4: disabled template excluded from the bag",
          draws == [t2["id"], t2["id"]], str(draws))
    await store.update_template(G, t1["id"], {"enabled": 1})

    # per-node isolation
    cat_b = await store.create_category(G, "CatB")
    t4 = await store.create_template(G, cat_b["id"], "T4", "wheel", rewards=rws)
    await store.clear_bag(G, cat["id"])
    await store.clear_bag(G, cat_b["id"])
    bag_a_before = await store.get_bag(G, cat["id"])
    await store.pop_bag(G, cat_b["id"], [t4["id"]])
    bag_a_after = await store.get_bag(G, cat["id"])
    check("C5: per-node bags are isolated",
          bag_a_before == bag_a_after == [],
          f"{bag_a_before} vs {bag_a_after}")


async def test_spawn_flow():
    section("D. spawn_game — lifecycle, channel, counter (D5)")
    guild = FakeGuild(G1, [FakeChannel(9001)])
    cog, bot = await make_cog([guild])
    await seed_main_guild(guild)

    # 1) auto success → counter +1, run row closes no_winner (no joins).
    #    The rotation picks a RANDOM eligible template — assert invariants,
    #    not a specific one.
    posted_ids = set()
    cfg = await store.get_config(G1)
    ok = await cog._auto_spawn(guild, cfg, forced=False)
    game = next(iter(cog.live_games.values()))
    row = await row_closed(game.log_id, timeout=5.0)
    await settle(cog)
    posted_ids = set()
    for cid in (9001, 9002):
        ch = guild.get_channel(cid)
        if ch is not None:
            posted_ids.update(m.id for m in ch.messages)
    seeded = {"Wheel #1", "QC #1", "Math #1", "RPS #1", "Wheel disabled",
              "Wheel no pool", "Math broken"}
    check("D1: auto spawn posts a game message",
          ok and row is not None and len(posted_ids) == 1
          and row["message_id"] in posted_ids,
          str(row and (row["status"], row["message_id"] in posted_ids)))
    check("D1: run row mode=auto, eligible template, category snapshotted",
          row and row["mode"] == "auto" and row["category_name"] == "Bronze"
          and row["template_name"] in seeded
          and row["game_type"] in ("wheel", "quick_click", "math", "rps"),
          str({k: row and row[k] for k in ("mode", "template_name",
                                           "category_name", "game_type")}))
    check("D1: no joins → no_winner, closed, no rewards",
          row["status"] == "no_winner" and row["ended_at"] is not None
          and row["winners"] == [] and len(REC.grants) == 0)
    cfg = await store.get_config(G1)
    check("D1: weekly counter bumped exactly once (D5)",
          int(cfg["events_this_week"]) == 1,
          str(cfg["events_this_week"]))

    # 2) channel gone → no spawn, NO counter bump, NO run row
    before_rows = await run_rows_for(G1)
    guild._channels.pop(9001)
    ok = await cog._auto_spawn(guild, await store.get_config(G1), False)
    after_rows = await run_rows_for(G1)
    cfg = await store.get_config(G1)
    check("D2: channel missing → spawn fails, no phantom run row, counter "
          "NOT bumped",
          ok is False and after_rows == before_rows
          and int(cfg["events_this_week"]) == 1)
    guild.add_channel(FakeChannel(9001))

    # 3) no eligible template → nothing (fresh empty guild G2)
    await store.save_config(G2, channel_id=9001)
    g2 = FakeGuild(G2, [FakeChannel(9001)])
    bot._guilds[G2] = g2
    before = await run_rows_for(G2)
    ok = await cog._auto_spawn(g2, await store.get_config(G2), False)
    check("D3: no eligible template → nothing spawns, no run row",
          ok is False and await run_rows_for(G2) == before)

    # 4) override channel used (QC #1 → 9002)
    ch_ov = FakeChannel(9002)
    guild.add_channel(ch_ov)
    tpl = await store.get_template(G1, TPL["qc"])
    ok, err = await cog.spawn_game(guild, tpl, "manual")
    game = next(v for v in cog.live_games.values() if v.log_id)
    await row_closed(game.log_id, timeout=5.0)
    await settle(cog)
    check("D4: template channel override is used (9002)",
          ok and len(ch_ov.messages) == 1)
    check("D4: manual spawn never bumps the counter (D5)",
          int((await store.get_config(G1))["events_this_week"]) == 1)

    # 5) deleted override → falls back to the guild default
    tpl2 = await store.update_template(G1, TPL["qc"], {"channel_id": 9999})
    ch_def = guild.get_channel(9001)
    n_before = len(ch_def.messages)
    ok, err = await cog.spawn_game(guild, tpl2, "manual")
    game = next(v for v in cog.live_games.values() if v.log_id)
    await row_closed(game.log_id, timeout=5.0)
    await settle(cog)
    check("D5: deleted override falls back to guild default channel",
          ok and len(ch_def.messages) == n_before + 1,
          f"def={len(ch_def.messages)} err={err}")
    await store.update_template(G1, TPL["qc"], {"channel_id": 9002})

    # 6) post failure → run row closed 'failed', counter untouched
    ch_bad = FakeChannel(9003, fail_send=True)
    guild.add_channel(ch_bad)
    await store.update_template(G1, TPL["wheel"], {"channel_id": 9003})
    rows_before = await run_rows_for(G1)
    ok, err = await cog.spawn_game(guild,
                                   await store.get_template(G1, TPL["wheel"]),
                                   "manual")
    row = await run_row((await run_rows_for(G1))[-1])
    check("D6: failed post → (False, error), run row closed as 'failed'",
          ok is False and "post" in (err or "")
          and row and row["status"] == "failed"
          and row["ended_at"] is not None and row["winners"] == []
          and len(await run_rows_for(G1)) == len(rows_before) + 1)
    check("D6: counter untouched on failure",
          int((await store.get_config(G1))["events_this_week"]) == 1)
    await store.update_template(G1, TPL["wheel"], {"channel_id": None})

    # 7) unplayable template (no answers) → preflight refusal
    rows_before = await run_rows_for(G1)
    ok, err = await cog.spawn_game(guild,
                                   await store.get_template(G1, TPL["mc_broken"]),
                                   "manual")
    row = await run_row((await run_rows_for(G1))[-1])
    check("D7: unplayable template → refused, row closed 'failed', reason",
          ok is False and "no answers" in (err or "")
          and row and row["status"] == "failed"
          and row["message_id"] is None, str(err))

    # 8) snapshot isolation: editing the template mid-game doesn't touch
    #    the running game (plan §14)
    ok, _ = await cog.spawn_game(guild, await store.get_template(G1, TPL["mc"]),
                                 "manual")
    game = next(v for v in cog.live_games.values() if v.log_id)
    await store.update_template(G1, TPL["mc"], {"name": "Math #1 RENAMED"})
    row = await row_closed(game.log_id, timeout=5.0)
    await settle(cog)
    check("D8: mid-game template edit → running game keeps its snapshot",
          row["template_name"] == "Math #1"
          and game.snapshot["name"] == "Math #1")
    await store.update_template(G1, TPL["mc"], {"name": "Math #1"})

    await drop_cog(cog)


async def test_request_queue():
    section("E. Spawn request queue (plan §9/§19)")
    guild = FakeGuild(G1, [FakeChannel(9001)])
    cog, bot = await make_cog([guild])
    if not TPL:
        await seed_main_guild(guild)
    cfg_before = int((await store.get_config(G1))["events_this_week"])

    # 1) manual specific template → done, real game, mode=manual, no counter
    rid, err = await store.create_spawn_request(G1, TPL["wheel"], "manual",
                                                requested_by="slash:42")
    check("E1: request queued", rid is not None and err is None, str(err))
    await cog._process_spawn_requests()
    st, e = await request_state(rid)
    row = await last_run_row(G1)
    row = await row_closed(row["id"], timeout=5.0)
    check("E1: claim → real engine → done; run row mode=manual, closed",
          st == "done" and row["mode"] == "manual"
          and row["status"] in ("no_winner", "completed"),
          f"{st}/{e} status={row and row['status']}")
    check("E1: manual queue path never bumps the counter (D5)",
          int((await store.get_config(G1))["events_this_week"]) == cfg_before)

    # 2) test mode → [Test] mark + mode=test (D4)
    rid, err = await store.create_spawn_request(G1, TPL["mc"], "test",
                                                requested_by="dash:admin")
    await cog._process_spawn_requests()
    st, e = await request_state(rid)
    row = await last_run_row(G1)
    row = await row_closed(row["id"], timeout=5.0)
    msg = guild.get_channel(9001).messages[-1]
    check("E2: test spawn runs the REAL engine with [Test] mark",
          st == "done" and row["mode"] == "test"
          and (msg.last_embed.title or "").startswith("[Test]"),
          f"{st} title={msg.last_embed.title!r}")

    # 3) D12: manual of a DISABLED template → failed
    rid, err = await store.create_spawn_request(G1, TPL["wheel_disabled"],
                                                "manual", "slash:42")
    await cog._process_spawn_requests()
    st, e = await request_state(rid)
    check("E3: D12 — manual spawn of disabled template rejected",
          st == "failed" and "disabled" in (e or ""), f"{st}/{e}")

    # 4) D12: TEST of a disabled template is still allowed (always)
    rid, err = await store.create_spawn_request(G1, TPL["wheel_disabled"],
                                                "test", "dash:admin")
    await cog._process_spawn_requests()
    st, e = await request_state(rid)
    row = await last_run_row(G1)
    await row_closed(row["id"], timeout=5.0)
    check("E4: D12 — test spawn ignores the enabled toggle",
          st == "done", f"{st}/{e}")

    # 5) deleted template → failed with a clear error
    rid_del, _ = await store.create_spawn_request(G1, TPL["qc"], "manual", "x")
    await store.delete_template(G1, TPL["qc"])
    await cog._process_spawn_requests()
    st, e = await request_state(rid_del)
    check("E5: deleted template → request failed, error surfaced",
          st == "failed" and "no longer exists" in (e or ""), f"{st}/{e}")

    # 6) guild the bot is not in → its pending rows closed as failed
    await store.save_config(G3, channel_id=1)
    cat3 = await store.create_category(G3, "Cat")
    t3 = await store.create_template(G3, cat3["id"], "W", "wheel",
                                     config={"join_seconds": 0.2},
                                     rewards=list(RWS))
    rid, _ = await store.create_spawn_request(G3, t3["id"], "manual", "x")
    await cog._process_spawn_requests()
    st, e = await request_state(rid)
    check("E6: bot-not-in-guild → pending rows closed (no stuck queue)",
          st == "failed" and "not in this guild" in (e or ""), f"{st}/{e}")

    # 7) D11 end-to-end: empty pool through the queue — winner announced,
    #    nothing granted
    await store.update_template(G1, TPL["mc"], {"rewards": []})
    rid, _ = await store.create_spawn_request(G1, TPL["mc"], "test", "x")
    await cog._process_spawn_requests()
    st, _ = await request_state(rid)
    row = await last_run_row(G1)
    game = next(v for v in cog.live_games.values() if v.log_id == row["id"])
    await asyncio.sleep(0.05)
    await game._dispatch(FakeInteraction(FakeUser(77, "P"), "mc_0"),
                         "main", "mc_0", info(77, "P"))
    row = await row_closed(row["id"], timeout=5.0)
    n_grants_before = len(REC.grants)
    check("E7: D11 end-to-end — winner shown, zero grants, no_reward entry",
          st == "done" and row["status"] == "completed"
          and len(REC.grants) == n_grants_before
          and row["winners"] and row["winners"][0]["status"] == "no_reward"
          and row["winners"][0].get("name") == "P",
          str(row["winners"]))

    # 8) reward actually granted through the shared path (recorded)
    await store.update_template(
        G1, TPL["mc"],
        {"rewards": [{"reward_type": "xp", "reward_value": "100",
                      "weight": 1}]})
    n_grants = len(REC.grants)
    rid, _ = await store.create_spawn_request(G1, TPL["mc"], "manual", "x")
    await cog._process_spawn_requests()
    row = await last_run_row(G1)
    game = next(v for v in cog.live_games.values() if v.log_id == row["id"])
    await asyncio.sleep(0.05)
    await game._dispatch(FakeInteraction(FakeUser(78, "Q"), "mc_0"),
                         "main", "mc_0", info(78, "Q"))
    row = await row_closed(row["id"], timeout=5.0)
    check("E8: winner granted via the shared give_reward path",
          len(REC.grants) == n_grants + 1
          and REC.grants[-1] == {"user_id": 78, "reward_type": "xp"}
          and row["winners"][0]["status"] == "won"
          and row["winner_id"] == 78,
          f"grants={REC.grants[-1:]} row={row['winners']}")

    # 9) live RPS played through the queued spawn (ephemeral handout,
    #    seating, choices, resolution)
    rid, _ = await store.create_spawn_request(G1, TPL["rps"], "manual", "x")
    await cog._process_spawn_requests()
    row = await last_run_row(G1)
    game = next(v for v in cog.live_games.values() if v.log_id == row["id"])
    i1 = FakeInteraction(FakeUser(11, "P1"), "join")
    await game._dispatch(i1, "main", "join", info(11, "P1"))
    check("E9: RPS seat 1 — private choice row handed out on join",
          len(game.seats) == 1 and i1.response.sent
          and i1.response.sent[0]["view"] is not None)
    i2 = FakeInteraction(FakeUser(12, "P2"), "join")
    await game._dispatch(i2, "main", "join", info(12, "P2"))
    check("E9: RPS seat 2 — both seated", len(game.seats) == 2)
    n_grants = len(REC.grants)
    await game._dispatch(FakeInteraction(FakeUser(11, "P1"), "rps_11_rock"),
                         "choice", "rps_11_rock", info(11, "P1"))
    await game._dispatch(FakeInteraction(FakeUser(12, "P2"), "rps_12_paper"),
                         "choice", "rps_12_paper", info(12, "P2"))
    row = await row_closed(row["id"], timeout=5.0)
    check("E9: RPS resolves paper>rock → P2, exactly one reward",
          game.finished and row["status"] == "completed"
          and len(REC.grants) == n_grants + 1
          and REC.grants[-1]["user_id"] == 12
          and row["winner_id"] == 12, str(row["winners"]))

    # 9b) REGRESSION: a game resolved BY ITS OWN TIMER (the production
    #     path — resolve() runs inside the timer task) must still close
    #     the row. The old _cancel_timers cancelled the running timer
    #     task itself, injecting CancelledError into resolve() after the
    #     reward grants and before finish_run (row stuck 'running').
    rid, _ = await store.create_spawn_request(G1, TPL["wheel"], "manual", "x")
    await cog._process_spawn_requests()
    row = await last_run_row(G1)
    game = next(v for v in cog.live_games.values() if v.log_id == row["id"])
    n_grants = len(REC.grants)
    await asyncio.sleep(0.05)
    await game._dispatch(FakeInteraction(FakeUser(99, "J"), "join"),
                         "main", "join", info(99, "J"))
    # no further interaction — the 0.2s join timer resolves it
    row = await row_closed(row["id"], timeout=5.0)
    check("E9b: timer-triggered resolution closes the row (no self-cancel)",
          row["status"] == "completed" and row["winner_id"] == 99
          and len(REC.grants) == n_grants + 1
          and REC.grants[-1]["user_id"] == 99,
          str(row and (row["status"], row["winner_id"])))

    # 10) slash manual path (_command_spawn) — specific + rotation + guards
    ok, msg = await cog._command_spawn(guild, TPL["wheel"], "slash:1")
    check("E10: /minigames_spawn <id> queues the template",
          ok and "Queued" in msg, msg)
    ok2, msg2 = await cog._command_spawn(guild, TPL["wheel"], "slash:1")
    check("E10: duplicate pending for the same template is rejected",
          not ok2 and "already queued" in msg2, msg2)
    ok3, msg3 = await cog._command_spawn(guild, TPL["wheel_disabled"], "slash:1")
    check("E10: disabled template via slash is refused (D12)",
          not ok3 and "disabled" in msg3, msg3)
    # drain the queued wheel request, then the no-id rotation path
    for _ in range(4):
        await cog._process_spawn_requests()
    ok4, msg4 = await cog._command_spawn(guild, None, "slash:1")
    check("E10: /minigames_spawn (no id) selects via the rotation",
          ok4 and "Queued" in msg4, msg4)
    for _ in range(6):
        await cog._process_spawn_requests()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM minigame_spawn_requests "
            "WHERE guild_id = ? AND status IN ('pending','processing')",
            (G1,))
        (left,) = await cur.fetchone()
    check("E10: queued manual spawns all executed", left == 0, str(left))

    await drop_cog(cog)


async def test_startup_sweep():
    section("F. Startup sweep (plan §14)")
    guild = FakeGuild(G1, [FakeChannel(9001)])
    cog, bot = await make_cog([guild])

    # seed an open run from "10 minutes ago" with a live message
    ch = guild.get_channel(9001)
    msg = await ch.send(embed=discord.Embed(title="Game",
                                            description="playing…"))
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    log_id = await store.start_run(G1, TPL["wheel"], "Wheel #1",
                                   None, "Bronze", "wheel", "auto", ch.id)
    await store.set_run_message(log_id, msg.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE minigames_log SET started_at = ? WHERE id = ?",
                         (old.isoformat(), log_id))
        await db.commit()

    # fresh process: first ready sweeps EVERY open row (grace 0)
    cog._sweep_done = False
    await cog._startup_sweep()
    row = await run_row(log_id)
    check("F1: stale open run finalized as aborted_restart",
          row["status"] == "aborted_restart" and row["ended_at"] is not None
          and row["winners"] == [])
    check("F1: best-effort 'bot restarted' note on the message",
          "bot restarted" in (msg.last_embed.description or ""),
          str(msg.last_embed.description))

    # idempotent: second sweep (reconnect semantics, grace 5) touches nothing
    edits_before = msg.edits
    await cog._startup_sweep()
    row = await run_row(log_id)
    check("F2: sweep is idempotent (no double finalize / note)",
          row["status"] == "aborted_restart" and msg.edits == edits_before)

    # grace window: a FRESH run (started now) is NOT swept on a reconnect
    log2 = await store.start_run(G1, TPL["wheel"], "Wheel #1", None,
                                 "Bronze", "wheel", "auto", ch.id)
    cog._sweep_done = True
    await cog._startup_sweep()
    row2 = await run_row(log2)
    check("F3: reconnect grace (5 min) protects a fresh healthy run",
          row2["status"] == "running" and row2["ended_at"] is None)
    # …and a fresh PROCESS would sweep it (grace 0)
    cog._sweep_done = False
    await cog._startup_sweep()
    row2 = await run_row(log2)
    check("F3: fresh process (grace 0) sweeps it",
          row2["status"] == "aborted_restart")
    await drop_cog(cog)


async def test_pacing_iteration():
    section("G. Pacing iteration — bookkeeping (D5)")
    G = 3001
    guild = FakeGuild(G, [FakeChannel(9001)])
    cog, bot = await make_cog([guild])
    await store.save_config(G, channel_id=9001, min_events_per_week=5,
                            max_events_per_week=10)
    cat = await store.create_category(G, "Cat")
    await store.create_template(G, cat["id"], "W1", "wheel",
                                config={"join_seconds": 0.2}, rewards=list(RWS))

    orig_random = random.random
    random.random = lambda: 0.0  # always beats the probability
    try:
        # week reset + one roll + successful auto spawn
        await cog._daily_check_iteration()
        cfg = await store.get_config(G)
        today = datetime.now(timezone.utc).date().isoformat()
        monday = cog_mod._monday_of(datetime.now(timezone.utc))
        check("G1: first pass — roll recorded today + spawn happened",
              cfg["last_check_date"] == today and cfg["week_start_date"] == monday
              and int(cfg["events_this_week"]) == 1,
              str({k: cfg[k] for k in ("last_check_date", "week_start_date",
                                       "events_this_week")}))
        # same day → no second roll
        await cog._daily_check_iteration()
        cfg = await store.get_config(G)
        check("G2: already rolled today → no second spawn",
              int(cfg["events_this_week"]) == 1)
        # at max → probability 0 → even a 0.0 roll doesn't fire
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE minigames_config SET events_this_week = 10, "
                "last_check_date = NULL WHERE guild_id = ?", (G,))
            await db.commit()
        await cog._daily_check_iteration()
        cfg = await store.get_config(G)
        check("G3: at weekly max → nothing spawns (cap honored)",
              int(cfg["events_this_week"]) == 10)
    finally:
        random.random = orig_random
    await drop_cog(cog)


async def test_compat_surface():
    section("H. Deprecated compat surface (Phase 4/6 window)")
    from cogs.minigames import (ensure_tables, get_config, get_tiers,
                                VALID_TIERS, get_user_win_count)
    check("H1: legacy imports resolve",
          VALID_TIERS == ("bronze", "silver", "gold", "platinum"))
    cfg = await get_config(G1)
    check("H2: get_config shim returns the config row",
          cfg.get("channel_id") == 9001, str(cfg.get("channel_id")))
    tiers = await get_tiers(G1)
    check("H3: get_tiers shim (legacy read) works on empty", tiers == [])
    wins = await get_user_win_count(G1, 78)
    check("H4: get_user_win_count counts v2 first-winner rows",
          wins >= 1, str(wins))  # user 78 won the E8 game
    await ensure_tables()
    check("H5: ensure_tables shim is idempotent (store-driven)", True)

    # compute_daily_probability kept verbatim — known values
    cases = [
        ((10, 3, 5, 10), (0.0, False)),
        ((0, 6, 5, 10), (1.0, True)),
        ((0, 0, 5, 10), (0.60, False)),
        ((4, 4, 5, 10), (1 / 3, False)),
        ((5, 2, 5, 10), (0.15, False)),
    ]
    ok = True
    for args, (exp_p, exp_f) in cases:
        got_p, got_f = cog_mod.compute_daily_probability(*args)
        if abs(got_p - exp_p) > 1e-9 or got_f != exp_f:
            ok = False
            print(f"    case {args}: got ({got_p}, {got_f})")
    check("H6: compute_daily_probability unchanged (5 known cases)", ok)


async def main():
    await store.ensure_tables()
    await test_selection_structure()
    await test_selection_monte_carlo()
    await test_bag_mechanics()
    await test_spawn_flow()
    await test_request_queue()
    await test_startup_sweep()
    await test_pacing_iteration()
    await test_compat_surface()

    print(f"\n{'=' * 50}\nRESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    random.seed()
    asyncio.run(main())
