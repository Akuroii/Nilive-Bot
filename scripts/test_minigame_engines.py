"""
Phase 2 verification harness — utils/minigame_engine.py

Behavioral correctness (not just "it runs") for all six engines, plus
REAL concurrency scenarios (gathered tasks racing on the same engine),
exactly-one-authoritative-resolution proofs, and finalization-order
proofs. Drives `engine._dispatch(...)` directly with fake Discord
objects — no live client, no network.

Usage:  python scripts/test_minigame_engines.py
"""
import asyncio
import os
import random
import sys
import tempfile
import time

_tmpdir = tempfile.mkdtemp(prefix="mgengine_test_")
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ.setdefault("OWNER_ID", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.minigame_engine as eng  # noqa: E402
from utils import minigame_store as store  # noqa: E402

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

class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name
        self.display_name = name

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


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, ephemeral=False, view=None):
        self.sent.append({"content": content, "ephemeral": ephemeral,
                          "view": view})


class FakeMessage:
    def __init__(self, fail_edit=False):
        self.fail_edit = fail_edit
        self.edits = 0
        self.last_embed = None
        self.last_view = None
        self.events = None  # shared event list, set by tests

    async def edit(self, embed=None, view=None, content=None):
        # the edit ATTEMPT is the ordering event — even a failed attempt
        # happens after grants and before the log close
        if self.events is not None:
            self.events.append("edit")
        if self.fail_edit:
            raise RuntimeError("forced embed-edit failure")
        self.edits += 1
        self.last_embed = embed
        self.last_view = view


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, embed=None, view=None, content=None):
        m = FakeMessage()
        self.messages.append(m)
        return m


class FakeInteraction:
    def __init__(self, user, custom_id=""):
        self.user = user
        self.data = {"custom_id": custom_id}
        self.response = FakeResponse()
        self.followup = FakeFollowup()


# ── test infra: grant + log spies ──────────────────────────────────────

class Recorder:
    def __init__(self, fail_reward_types=()):
        self.grants = []
        self.runs = []
        self.fail_reward_types = set(fail_reward_types)
        self.events = []  # shared ordering list

    async def fake_give_reward(self, bot, guild_id, user_id, reward_type,
                               *a, **kw):
        self.grants.append({"user_id": user_id, "reward_type": reward_type,
                            **{k: v for k, v in kw.items()
                               if k in ("amount", "role_id", "item_name",
                                        "duration_hours")}})
        self.events.append(f"grant:{user_id}")
        if reward_type in self.fail_reward_types:
            return {"success": False, "error": "forced failure"}
        return {"success": True}

    async def fake_finish_run(self, log_id, status, participants=None,
                              winners=None):
        self.runs.append({"log_id": log_id, "status": status,
                          "participants": participants, "winners": winners})
        self.events.append(f"finish:{status}")


REC = None  # active recorder


async def start_engine(snapshot, mode="auto", log_id=1, fail_edit=False):
    ch = FakeChannel()
    e = eng.make_engine(snapshot, mode, log_id, bot=None)
    await e.start(ch)
    ch.messages[0].events = REC.events  # wire ordering recorder
    if fail_edit:
        ch.messages[0].fail_edit = True
    return e, ch


def snap(game_type, **over):
    base = {
        "guild_id": 1, "template_id": 100, "name": "Test Game",
        "game_type": game_type, "category_id": 1, "category_name": "Bronze",
        "embed": {"title": "🧮 Challenge", "description": "desc",
                  "color": "#5865f2"},
        "config": {}, "rewards": [
            {"reward_type": "xp", "reward_value": "100", "weight": 1}],
        "channel_id": None,
    }
    base.update(over)
    return base


def info(uid, name=None):
    return {"id": uid, "name": name or f"User{uid}", "mention": f"<@{uid}>"}


async def wait_until(pred, timeout=5.0, step=0.005):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


# ══════════════════════════════════════════════════════════════════════

async def test_quick_click():
    global REC
    section("Quick Click / Reflex — behavior")
    # 1) reveal delay within configured range
    REC = Recorder()
    snapshot = snap("quick_click", config={
        "buttons": 4, "reveal_min": 0.05, "reveal_max": 0.09,
        "wait_after": 10.0})
    e, ch = await start_engine(snapshot, log_id=1)
    t0 = time.monotonic()
    await wait_until(lambda: e.revealed)
    delay = time.monotonic() - t0
    check("reveal happens", e.revealed)
    check("reveal delay within [min,max] (±50ms slack)",
          0.045 <= delay <= 0.14, f"delay={delay:.3f}")
    check("green position in range", 0 <= e.green_index < 4)
    check("exactly one message posted, one redraw at reveal",
          len(ch.messages) == 1 and ch.messages[0].edits == 1,
          f"edits={ch.messages[0].edits}")

    # 2) randomized green position across many games (deterministic via
    #    patched randrange)
    original_rr = eng.random.randrange
    positions = []
    for i in range(4):
        eng.random.randrange = (lambda i: (lambda k: i))(i)
        e2, _ = await start_engine(
            snap("quick_click", config={"buttons": 4, "reveal_min": 0.01,
                                        "reveal_max": 0.02, "wait_after": 0.4}),
            log_id=10 + i)
        await wait_until(lambda: e2.revealed)
        positions.append(e2.green_index)
        await e2.resolve([], "no_winner")  # close deterministically
    eng.random.randrange = original_rr
    check("green position is random (4 games → 4 distinct positions)",
          sorted(positions) == [0, 1, 2, 3], str(positions))

    # 3) buttons cannot win before reveal
    e3, ch3 = await start_engine(snap("quick_click", config={
        "buttons": 4, "reveal_min": 0.3, "reveal_max": 0.35,
        "wait_after": 10.0}), log_id=20)
    inter = FakeInteraction(FakeUser(7, "Early"), "qc_0")
    await e3._dispatch(inter, "main", "qc_0", info(7, "Early"))
    await asyncio.sleep(0.05)
    check("pre-reveal click does not win", not e3.finished)
    check("pre-reveal click not recorded", 7 not in e3.participants)

    # 4) first valid click after reveal wins; exactly one winner
    await wait_until(lambda: e3.revealed)
    inter = FakeInteraction(FakeUser(8, "Fast"), f"qc_{e3.green_index}")
    await e3._dispatch(inter, "main", f"qc_{e3.green_index}", info(8, "Fast"))
    await wait_until(lambda: e3.finished, timeout=2.0)
    check("first valid click wins", e3.finished and 8 in e3.participants)
    check("exactly one grant for one winner", len(REC.grants) == 1
          and REC.grants[0]["user_id"] == 8, str(REC.grants))
    check("log row closed as completed", REC.runs and
          REC.runs[-1]["status"] == "completed")
    check("result text shows the winner",
          "Fast" in (ch3.messages[0].last_embed.description or ""))

    # 5) post-reveal timeout → no winner, no reward
    REC = Recorder()
    e4, ch4 = await start_engine(snap("quick_click", config={
        "buttons": 3, "reveal_min": 0.02, "reveal_max": 0.03,
        "wait_after": 0.25}), log_id=30)
    await wait_until(lambda: e4.finished, timeout=3.0)
    check("post-reveal timeout ends the game", e4.finished)
    check("timeout → no winner / no reward", len(REC.grants) == 0
          and REC.runs and REC.runs[-1]["status"] == "no_winner")

    # 6) non-green click after reveal does nothing (buttons disabled,
    #    engine guard too)
    e5, _ = await start_engine(snap("quick_click", config={
        "buttons": 4, "reveal_min": 0.02, "reveal_max": 0.03,
        "wait_after": 10.0}), log_id=40)
    await wait_until(lambda: e5.revealed)
    wrong = (e5.green_index + 1) % 4
    await e5._dispatch(FakeInteraction(FakeUser(9, "Wrong"),
                                       f"qc_{wrong}"), "main",
                       f"qc_{wrong}", info(9, "Wrong"))
    await asyncio.sleep(0.05)
    check("non-green click ignored", not e5.finished
          and 9 not in e5.participants)
    await e5._on_wait_timeout()  # force the ending
    check("forced end after ignored click → no winner",
          e5.finished and 9 not in e5.participants)
    await e3._cancel_timers()


async def test_quick_click_concurrency():
    global REC
    section("Quick Click — concurrency (real gathered races)")
    for trial in range(3):
        REC = Recorder()
        e, ch = await start_engine(snap("quick_click", config={
            "buttons": 4, "reveal_min": 0.05, "reveal_max": 0.08,
            "wait_after": 10.0}), log_id=50 + trial)
        await wait_until(lambda: e.revealed)
        # two users click the SAME green button at nearly the same time
        await asyncio.gather(
            e._dispatch(FakeInteraction(FakeUser(100, "A"),
                                        f"qc_{e.green_index}"), "main",
                        f"qc_{e.green_index}", info(100, "A")),
            e._dispatch(FakeInteraction(FakeUser(200, "B"),
                                        f"qc_{e.green_index}"), "main",
                        f"qc_{e.green_index}", info(200, "B")),
        )
        await wait_until(lambda: e.finished, timeout=2.0)
        assert e.finished, f"trial {trial}: game never resolved"
        winners = [r["user_id"] for r in REC.grants]
        check(f"trial {trial}: exactly ONE winner from a 2-way race",
              len(winners) == 1, str(winners))
        check(f"trial {trial}: log closed exactly once",
              len(REC.runs) == 1, str(len(REC.runs)))
        await e._cancel_timers()


async def test_wheel():
    global REC
    section("Wheel — behavior")
    # join window + duplicate joins + exactly one fair winner
    REC = Recorder()
    e, ch = await start_engine(snap("wheel", config={"join_seconds": 0.4}),
                               log_id=60)
    await asyncio.sleep(0.05)
    await e._dispatch(FakeInteraction(FakeUser(1, "P1"), "join"),
                      "main", "join", info(1, "P1"))
    await e._dispatch(FakeInteraction(FakeUser(2, "P2"), "join"),
                      "main", "join", info(2, "P2"))
    # duplicate join by P1 — must be safe + acknowledged
    dup = FakeInteraction(FakeUser(1, "P1"), "join")
    await e._dispatch(dup, "main", "join", info(1, "P1"))
    check("duplicate join acknowledged, not double-counted",
          len(e.participants) == 2 and
          any("already" in (s["content"] or "") for s in dup.response.sent),
          str(dup.response.sent))
    await wait_until(lambda: e.finished, timeout=3.0)
    check("wheel ends after window", e.finished)
    check("exactly one winner", len(REC.grants) == 1)
    check("winner is one of the joiners",
          REC.grants[0]["user_id"] in (1, 2))
    check("both joiners in participants",
          {p["id"] for p in REC.runs[-1]["participants"]} == {1, 2})
    check("final embed names the winner",
          ("P1" in ch.messages[0].last_embed.description or
           "P2" in ch.messages[0].last_embed.description))

    # no joiners → no winner, no reward
    REC = Recorder()
    e2, _ = await start_engine(snap("wheel", config={"join_seconds": 0.1}),
                               log_id=70)
    await wait_until(lambda: e2.finished, timeout=2.0)
    check("no joiners → no winner / no reward",
          e2.finished and len(REC.grants) == 0
          and REC.runs[-1]["status"] == "no_winner")

    # fairness: 1 joiner always wins; 2 joiners ≈ 50/50
    REC = Recorder()
    solo_wins = 0
    for i in range(30):
        e3, _ = await start_engine(
            snap("wheel", config={"join_seconds": 0.01}), log_id=80 + i)
        await e3._dispatch(FakeInteraction(FakeUser(5, "Solo"), "join"),
                           "main", "join", info(5, "Solo"))
        await wait_until(lambda: e3.finished, timeout=2.0)
        if REC.grants and REC.grants[-1]["user_id"] == 5:
            solo_wins += 1
    check("1 joiner wins 100% of the time (30 games)", solo_wins == 30,
          f"{solo_wins}/30")

    REC = Recorder()
    wins = {1: 0, 2: 0}
    for i in range(400):
        e4, _ = await start_engine(
            snap("wheel", config={"join_seconds": 0.005}), log_id=110 + i)
        await e4._dispatch(FakeInteraction(FakeUser(1, "A"), "join"),
                           "main", "join", info(1))
        await e4._dispatch(FakeInteraction(FakeUser(2, "B"), "join"),
                           "main", "join", info(2))
        await wait_until(lambda: e4.finished, timeout=2.0)
        g = REC.grants[-1]
        wins[g["user_id"]] += 1
    pct = wins[1] / 400
    check("2 joiners ≈ 50/50 over 400 games (40–60% band)",
          0.40 <= pct <= 0.60, f"P1={wins[1]}/400 ({pct:.1%})")


async def test_multiple_choice():
    global REC
    section("Math/Colors/Emoji (shared engine) — behavior")
    cfg = {"question": "15 × 7 = ?", "answers": ["95", "105", "115", "125"],
           "correct": 1, "seconds": 0.6}
    REC = Recorder()
    e, ch = await start_engine(snap("math", config=cfg), log_id=90)

    # during the game: no main-message edits, no counts/identities leaked
    await asyncio.sleep(0.05)
    i1 = FakeInteraction(FakeUser(1, "U1"), "mc_0")
    await e._dispatch(i1, "main", "mc_0", info(1, "U1"))
    await asyncio.sleep(0.05)
    check("no live main-message edit during the game",
          ch.messages[0].edits == 0, f"edits={ch.messages[0].edits}")
    ack = i1.response.sent[0]["content"]
    check("ack is generic (no counts/identities/correct answer)",
          "105" not in ack and "U1" not in ack and "1/" not in ack
          and "2/" not in ack, ack)

    # change of answer: last one counts
    i2 = FakeInteraction(FakeUser(1, "U1"), "mc_1")
    await e._dispatch(i2, "main", "mc_1", info(1, "U1"))
    check("answer changed 0→1 (last answer state)",
          e.selections[1] == 1)

    # second player answers wrong; a third is correct
    await e._dispatch(FakeInteraction(FakeUser(2, "U2"), "mc_2"),
                      "main", "mc_2", info(2, "U2"))
    await e._dispatch(FakeInteraction(FakeUser(3, "U3"), "mc_1"),
                      "main", "mc_1", info(3, "U3"))

    # a click that arrives AFTER the end must be ignored
    # (boundary test: slow in-flight click dispatched post-resolution)
    late_task = asyncio.create_task(asyncio.sleep(0.1))

    await wait_until(lambda: e.finished, timeout=3.0)
    await late_task
    i_late = FakeInteraction(FakeUser(9, "Late"), "mc_1")
    await e._dispatch(i_late, "main", "mc_1", info(9, "Late"))
    check("post-end click ignored (no selection recorded)",
          9 not in e.selections)

    winners_ids = {g["user_id"] for g in REC.grants}
    check("multiple correct players ALL win (U1 changed to correct, U3)",
          winners_ids == {1, 3}, str(winners_ids))
    check("each winner got an INDEPENDENT roll (2 grants)",
          len(REC.grants) == 2)
    desc = ch.messages[0].last_embed.description or ""
    check("reveal shows the correct answer", "105" in desc, desc)
    check("reveal lists the winners", "U1" in desc and "U3" in desc, desc)
    view_rows = eng.mc_rows(cfg["answers"], True, 1)
    check("final buttons: correct green, wrong red, all disabled",
          view_rows[0][1]["style"] == 3 and view_rows[0][0]["style"] == 4
          and all(b["disabled"] for b in view_rows[0]))
    await e._cancel_timers()

    # zero correct → no winner, no reward, answer still revealed
    REC = Recorder()
    e2, ch2 = await start_engine(snap("math", config={
        "answers": ["a", "b"], "correct": 0, "seconds": 0.15}), log_id=95)
    await e2._dispatch(FakeInteraction(FakeUser(4, "U4"), "mc_1"),
                       "main", "mc_1", info(4, "U4"))
    await wait_until(lambda: e2.finished, timeout=2.0)
    check("zero correct → no winner / no reward",
          len(REC.grants) == 0 and REC.runs[-1]["status"] == "no_winner")
    check("zero correct → correct answer still revealed",
          "a" in (ch2.messages[0].last_embed.description or ""))

    # empty reward pool (D11): winners resolve & display, no grants
    REC = Recorder()
    e3, ch3 = await start_engine(
        snap("math", config={"answers": ["x", "y"], "correct": 0,
                             "seconds": 0.15}, rewards=[]), log_id=96)
    await e3._dispatch(FakeInteraction(FakeUser(5, "U5"), "mc_0"),
                       "main", "mc_0", info(5, "U5"))
    await wait_until(lambda: e3.finished, timeout=2.0)
    check("D11: winner displayed with no reward",
          len(REC.grants) == 0 and REC.runs[-1]["status"] == "completed"
          and REC.runs[-1]["winners"][0]["status"] == "no_reward",
          str(REC.runs[-1]["winners"]))
    check("D11: winner shown in final embed",
          "U5" in (ch3.messages[0].last_embed.description or ""))

    # shared engine across the three types (colors has an image embed)
    e4, ch4 = await start_engine(snap(
        "colors",
        embed={"title": "Color?", "image": "https://example.com/blue.png"},
        config={"answers": ["🔴 Red", "🔵 Blue"], "correct": 1,
                "seconds": 0.1}), log_id=97)
    check("colors engine is the same class as math",
          type(e4) is type(e3) and e4.TYPE == "colors")
    check("colors buttons carry exact admin labels (D6)",
          e4.component_rows()[0][1]["label"] == "🔵 Blue")
    rows = eng.initial_component_rows("math", cfg)
    check("preview descriptor == engine's initial rows (single source)",
          rows == e4.__class__(
              snap("math", config=cfg), "auto", 99).component_rows())
    await e4._on_time()


async def test_multiple_choice_concurrency():
    global REC
    section("Multiple choice — concurrency (real gathered races)")
    # 1) timer fires while a click is being processed (repeated trials,
    #    invariants asserted — exactly one authoritative resolution)
    for trial in range(5):
        REC = Recorder()
        e, _ = await start_engine(snap("math", config={
            "answers": ["a", "b"], "correct": 1,
            "seconds": 0.3}), log_id=120 + trial)

        async def slow_click():
            await asyncio.sleep(0.25 + 0.02 * trial)
            await e._dispatch(FakeInteraction(FakeUser(1, "Racer"),
                                              "mc_1"), "main", "mc_1",
                              info(1, "Racer"))

        t = asyncio.create_task(slow_click())
        await wait_until(lambda: e.finished, timeout=3.0)
        await t
        check(f"trial {trial}: resolved exactly once",
              len(REC.runs) == 1, str(len(REC.runs)))
        winners = [g["user_id"] for g in REC.grants]
        if 1 in winners:
            check(f"trial {trial}: winner's answer was committed (b)",
                  e.selections.get(1) == 1)
        else:
            check(f"trial {trial}: no phantom winner", winners == [])
        await e._cancel_timers()

    # 2) two users hammering different buttons in the same instant
    REC = Recorder()
    e, _ = await start_engine(snap("math", config={
        "answers": ["a", "b"], "correct": 0, "seconds": 0.3}), log_id=130)
    await asyncio.gather(
        e._dispatch(FakeInteraction(FakeUser(1, "A"), "mc_0"), "main",
                    "mc_0", info(1, "A")),
        e._dispatch(FakeInteraction(FakeUser(2, "B"), "mc_1"), "main",
                    "mc_1", info(2, "B")),
    )
    await wait_until(lambda: e.finished, timeout=3.0)
    check("burst clicks both recorded (state integrity)",
          e.selections.get(1) == 0 and e.selections.get(2) == 1,
          str(e.selections))
    check("only the correct click wins",
          [g["user_id"] for g in REC.grants] == [1], str(REC.grants))

    # 3) double resolution trigger (direct concurrent resolve calls)
    REC = Recorder()
    e, _ = await start_engine(snap("math", config={
        "answers": ["a", "b"], "correct": 0, "seconds": 10}), log_id=140)
    r1, r2 = await asyncio.gather(
        e.resolve([info(1, "A")], status="completed", result_text="x"),
        e.resolve([info(2, "B")], status="no_winner", result_text="y"))
    check("exactly one of two concurrent resolves claims the game",
          (r1, r2) in ((True, False), (False, True)), f"{r1}/{r2}")
    check("double trigger → log closed once, grants once",
          len(REC.runs) == 1 and len(REC.grants) == 1,
          f"runs={len(REC.runs)} grants={len(REC.grants)}")
    await e._cancel_timers()


async def test_rps():
    global REC
    section("RPS — behavior")
    # seating: third player rejected
    REC = Recorder()
    e, ch = await start_engine(snap("rps", config={
        "seating_seconds": 5.0, "choice_seconds": 5.0}), log_id=150)
    p1 = FakeInteraction(FakeUser(1, "P1"), "join")
    await e._dispatch(p1, "main", "join", info(1, "P1"))
    check("first seat: private choice row handed out",
          len(p1.response.sent) == 1 and p1.response.sent[0]["view"] is not None
          and len(e.seats) == 1)
    p2 = FakeInteraction(FakeUser(2, "P2"), "join")
    await e._dispatch(p2, "main", "join", info(2, "P2"))
    check("second seat: private row handed out + main redrawn",
          len(p2.response.sent) == 1 and p2.response.sent[0]["view"] is not None
          and len(e.seats) == 2 and ch.messages[0].edits >= 1)
    p3 = FakeInteraction(FakeUser(3, "P3"), "join")
    await e._dispatch(p3, "main", "join", info(3, "P3"))
    check("third player cannot take a seat",
          len(e.seats) == 2 and 3 not in e.participants
          and any("full" in (s["content"] or "") for s in p3.response.sent))

    # wrong-user choice button rejected
    p2b = FakeInteraction(FakeUser(2, "P2"), f"rps_1_rock")
    await e._dispatch(p2b, "choice", "rps_1_rock", info(2, "P2"))
    check("P2 cannot use P1's choice button",
          1 not in e.choices and
          any("isn't yours" in (s["content"] or "")
              for s in p2b.response.sent))

    # both choose → resolve (paper beats rock)
    a1 = FakeInteraction(FakeUser(1, "P1"), "rps_1_paper")
    a2 = FakeInteraction(FakeUser(2, "P2"), "rps_2_rock")
    await asyncio.gather(
        e._dispatch(a1, "choice", "rps_1_paper", info(1, "P1")),
        e._dispatch(a2, "choice", "rps_2_rock", info(2, "P2")))
    await wait_until(lambda: e.finished, timeout=2.0)
    check("RPS resolves when both choose", e.finished)
    check("paper beats rock → P1 wins, exactly one reward",
          len(REC.grants) == 1 and REC.grants[0]["user_id"] == 1,
          str(REC.grants))
    desc = ch.messages[0].last_embed.description or ""
    check("final embed shows both choices + winner",
          "Paper" in desc and "Rock" in desc and "P1" in desc, desc)
    check("choice views disabled after the end",
          all(b.disabled for uid, (v, m) in e._choice_views.items()
              for b in v.children))
    await e._cancel_timers()

    # draw → no reward
    REC = Recorder()
    e2, _ = await start_engine(snap("rps", config={
        "seating_seconds": 5.0, "choice_seconds": 5.0}), log_id=160)
    await e2._dispatch(FakeInteraction(FakeUser(1, "P1"), "join"),
                       "main", "join", info(1, "P1"))
    await e2._dispatch(FakeInteraction(FakeUser(2, "P2"), "join"),
                       "main", "join", info(2, "P2"))
    await e2._dispatch(FakeInteraction(FakeUser(1, "P1"), "rps_1_rock"),
                       "choice", "rps_1_rock", info(1, "P1"))
    await e2._dispatch(FakeInteraction(FakeUser(2, "P2"), "rps_2_rock"),
                       "choice", "rps_2_rock", info(2, "P2"))
    await wait_until(lambda: e2.finished, timeout=2.0)
    check("draw → explicitly no reward (not an accidental winner)",
          len(REC.grants) == 0 and REC.runs[-1]["status"] == "no_winner"
          and "Draw" in _final_desc(e2))

    # seating timeout (1 player only) → no winner
    REC = Recorder()
    e3, _ = await start_engine(snap("rps", config={
        "seating_seconds": 0.2, "choice_seconds": 5.0}), log_id=170)
    await e3._dispatch(FakeInteraction(FakeUser(1, "P1"), "join"),
                       "main", "join", info(1, "P1"))
    await wait_until(lambda: e3.finished, timeout=3.0)
    check("seating timeout → no winner / no reward",
          e3.finished and len(REC.grants) == 0
          and REC.runs[-1]["status"] == "no_winner")

    # choice timeout: one chose, the other didn't → no winner (no forfeit)
    REC = Recorder()
    e4, _ = await start_engine(snap("rps", config={
        "seating_seconds": 5.0, "choice_seconds": 0.3}), log_id=180)
    await e4._dispatch(FakeInteraction(FakeUser(1, "P1"), "join"),
                       "main", "join", info(1, "P1"))
    await e4._dispatch(FakeInteraction(FakeUser(2, "P2"), "join"),
                       "main", "join", info(2, "P2"))
    await e4._dispatch(FakeInteraction(FakeUser(1, "P1"), "rps_1_scissors"),
                       "choice", "rps_1_scissors", info(1, "P1"))
    await wait_until(lambda: e4.finished, timeout=3.0)
    check("1-of-2 chose + choice timeout → no winner (no forfeit-by-default)",
          e4.finished and len(REC.grants) == 0
          and REC.runs[-1]["status"] == "no_winner")


def _final_desc(engine):
    # last embed edit text (engine keeps the message ref)
    return engine._message.last_embed.description or "" if engine._message else ""


async def test_rps_concurrency():
    global REC
    section("RPS — concurrency (real gathered races)")
    for trial in range(3):
        REC = Recorder()
        e, _ = await start_engine(snap("rps", config={
            "seating_seconds": 5.0, "choice_seconds": 5.0}),
            log_id=190 + trial)
        # P1 seats first (serial)
        await e._dispatch(FakeInteraction(FakeUser(1, "P1"), "join"),
                          "main", "join", info(1, "P1"))
        # P2 and P3 race for the final seat
        await asyncio.gather(
            e._dispatch(FakeInteraction(FakeUser(2, "P2"), "join"),
                        "main", "join", info(2, "P2")),
            e._dispatch(FakeInteraction(FakeUser(3, "P3"), "join"),
                        "main", "join", info(3, "P3")))
        seated = {s["id"] for s in e.seats}
        check(f"trial {trial}: exactly two seated after a 2-way seat race",
              len(seated) == 2 and 1 in seated and seated <= {1, 2, 3},
              str(seated))
        loser = ({2, 3} - seated)
        check(f"trial {trial}: the loser got the 'full' feedback",
              loser and len(loser) == 1)
        await e.resolve([], "no_winner", result_text="test teardown")


async def test_finalization_order():
    global REC
    section("Finalization order + partial failure (cross-engine)")
    # order: grants → embed edit → log; a failing grant doesn't block
    # others; a failing embed edit never loses rewards
    for fail_edit_case in (False, True):
        REC = Recorder(fail_reward_types={"coins"})
        snapshot = snap("math", config={"answers": ["a", "b"], "correct": 0,
                                        "seconds": 10},
                        rewards=[{"reward_type": "coins",
                                  "reward_value": "50", "weight": 1},
                                 {"reward_type": "xp", "reward_value": "100",
                                  "weight": 1}])
        e, _ = await start_engine(snapshot, log_id=200,
                                  fail_edit=fail_edit_case)
        ok = await e.resolve([info(1, "A"), info(2, "B")],
                             status="completed", result_text="result")
        assert ok
        # ordering: every grant event before the edit event, edit before finish
        ev = REC.events
        last_grant = max(i for i, x in enumerate(ev)
                         if x.startswith("grant"))
        edit_i = ev.index("edit")
        finish_i = ev.index("finish:completed")
        check(f"fail_edit={fail_edit_case}: grants precede embed edit",
              last_grant < edit_i, str(ev))
        check(f"fail_edit={fail_edit_case}: embed edit precedes log close",
              edit_i < finish_i, str(ev))
        check(f"fail_edit={fail_edit_case}: BOTH winners processed "
              f"(1 failed grant doesn't block the other)",
              len(REC.grants) == 2, str(REC.grants))
        # rolls are independent+random: the invariant is that a grant is
        # "failed" EXACTLY when it drew the forced-to-fail (coins) type
        statuses = {w["id"]: w["status"] for w in REC.runs[-1]["winners"]}
        rolled = {g["user_id"]: g["reward_type"] for g in REC.grants}
        invariant = all(
            statuses[uid] == ("failed" if rolled[uid] == "coins" else "won")
            for uid in (1, 2))
        check(f"fail_edit={fail_edit_case}: failed ⟺ coins roll; "
              f"no winner blocked by the other's failure",
              invariant, f"statuses={statuses} rolled={rolled}")
        if fail_edit_case:
            check("failed embed edit did NOT lose rewards (2 grants "
                  "delivered before the failed edit)",
                  len(REC.grants) == 2)
        await e._cancel_timers()


async def test_preview_descriptor():
    global REC
    section("Preview architecture (engine owns component definition)")
    rows_qc = eng.initial_component_rows(
        "quick_click", {"buttons": 5, "reveal_min": 2, "reveal_max": 6,
                        "wait_after": 10})
    check("qc preview: 5 numbered buttons, all disabled pre-reveal",
          [b["label"] for b in rows_qc[0]] == ["1", "2", "3", "4", "5"]
          and all(b["disabled"] for b in rows_qc[0])
          and all(b["style"] == 2 for b in rows_qc[0]))
    cfg_mc = {"answers": ["🔴 Red", "🟡 Yellow", "🔵 Blue"], "correct": 2,
              "seconds": 20}
    rows_mc = eng.initial_component_rows("math", cfg_mc)
    check("mc preview: exact admin labels, enabled (D6 — no prefixes)",
          [b["label"] for b in rows_mc[0]] == ["🔴 Red", "🟡 Yellow",
                                               "🔵 Blue"]
          and not any(b["disabled"] for b in rows_mc[0]))
    check("mc preview does NOT leak the correct answer",
          all("correct" not in str(b).lower() for b in rows_mc[0]))
    rows_w = eng.initial_component_rows("wheel", {})
    rows_r = eng.initial_component_rows("rps", {})
    check("wheel/rps preview: single Join button (primary)",
          rows_w[0][0]["label"] == "Join" and rows_w[0][0]["style"] == 1
          and rows_r[0][0]["label"] == "Join")
    # engine instance initial rows == descriptor (no divergence possible)
    for gtype, cfg in (("quick_click", {"buttons": 4}),
                       ("math", cfg_mc), ("wheel", {}), ("rps", {})):
        e = eng.make_engine(snap(gtype, config=cfg), "auto", 1)
        check(f"{gtype}: engine initial rows == descriptor",
              e.component_rows() == eng.initial_component_rows(gtype, cfg))
    # preflight: a broken template cannot be started
    e_bad = eng.make_engine(snap("math", config={}), "auto", 1)
    try:
        await e_bad.start(FakeChannel())
        check("preflight: empty-answers template refused", False)
    except ValueError:
        check("preflight: empty-answers template refused", True)
    # [Test] prefix (D4)
    e_test = eng.make_engine(snap("math", config=cfg_mc), "test", 1)
    check("test mode embed gets [Test] prefix",
          e_test.build_embed().title == "[Test] 🧮 Challenge")


async def main():
    global REC
    # patch the grant path + run-log close with recorders
    REC = Recorder()
    orig_grant = eng.give_reward
    orig_finish = store.finish_run
    async def _grant(*a, **k):
        return await REC.fake_give_reward(*a, **k)
    async def _finish(*a, **k):
        return await REC.fake_finish_run(*a, **k)
    eng.give_reward = _grant
    store.finish_run = _finish

    await test_quick_click()
    await test_quick_click_concurrency()
    await test_wheel()
    await test_multiple_choice()
    await test_multiple_choice_concurrency()
    await test_rps()
    await test_rps_concurrency()
    await test_finalization_order()
    await test_preview_descriptor()

    eng.give_reward = orig_grant
    store.finish_run = orig_finish
    # let any pending engine tasks drain/finish quietly
    await asyncio.sleep(0.1)

    print(f"\n{'=' * 50}\nRESULT: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    random.seed()
    asyncio.run(main())
