"""PROBE 6 — one decision per message, shared by every sibling listener.

Why this exists: discord.py runs every cog's ``on_message`` as a *sibling task*
(``Client.dispatch`` → ``_run_event`` → the extra events are scheduled after
``process_commands``), so the alias listener, the trigger listener and the
custom-command listener all see the same message with no ordering guarantee.
Before the router each of them sniffed ``message.content`` independently, which
is how one message could run two systems, and how the alias listener's content
rewrite made the other two see a different message than the user sent
(``legacy/p5b_runtime.py`` has the recorded interleaving).

Run:  python tools/alias_probes/p6_race.py
"""
import asyncio, os, sys, types, pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0]))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeMessage, FakeGuild, FakeChannel, FakeMember, SENT_LOG)

import aiosqlite
from database import DB_PATH
from utils.message_router import MessageRouter, Route, get_router

GUILD = 333
FAILURES = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          f"{('  -> ' + str(detail)[:130]) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def fake_message(bot, content, mid, guild_id=GUILD):
    g = FakeGuild(id=guild_id)
    g._state = types.SimpleNamespace(
        get_user=lambda uid: None, dispatch=lambda *a, **k: None,
        member_cache=__import__("discord").MemberCacheFlags.none(),
        member_cache_flags=__import__("discord").MemberCacheFlags.none(),
        http=None)
    ch = FakeChannel(guild=g)
    # the app command carries @checks.has_permissions(manage_guild=True); p0_env's
    # default fake namespace does not define it, which correctly fails the alias
    # check closed — so give this channel the permission the check asks for.
    ch.permissions_for = lambda obj: types.SimpleNamespace(
        kick_members=True, ban_members=True, manage_guild=True, manage_roles=True,
        mention_everyone=False, manage_messages=True)
    author = FakeMember(id=111, guild=g)
    m = FakeMessage(content, guild=g, channel=ch, author=author)
    m.id = mid
    m._state = g._state
    return m


async def main():
    fresh_db()
    await init_database()
    patch_context_send()
    bot = make_bot()
    router = get_router(bot)

    computes = []
    real_compute = MessageRouter._compute

    async def counted_compute(self, message):
        computes.append(getattr(message, "id", None))
        await asyncio.sleep(0.01)          # force the siblings to actually race
        return await real_compute(self, message)

    MessageRouter._compute = counted_compute

    print("\n=== 1. five listeners, one message: one computation, one answer ===")
    router.set_alias_table({})
    msg = fake_message(bot, "hello there", 9001)
    decisions = await asyncio.gather(*[router.decide(msg) for _ in range(5)])
    check("_compute ran once for five concurrent awaits",
          computes.count(9001) == 1, computes)
    check("all five got the identical object",
          all(d is decisions[0] for d in decisions))
    check("and the same route", len({d.route for d in decisions}) == 1,
          [d.route for d in decisions])

    print("\n=== 2. no bleed between messages ===")
    computes.clear()
    a = fake_message(bot, "hello there", 9002)
    b = fake_message(bot, "hello there", 9003)
    da, db = await asyncio.gather(router.decide(a), router.decide(b))
    check("two message ids -> two computations", len(computes) == 2, computes)
    check("both still agree on the outcome", da.route is db.route, (da.route, db.route))

    print("\n=== 3. a dashboard save mid-flight is not stuck behind the memo ===")
    computes.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO triggers (guild_id, trigger_words, response_text,"
                         " response_type, match_type, fuzzy_match, fuzzy_threshold,"
                         " case_sensitive, response_chance, cooldown_seconds,"
                         " allowed_channels, enabled) "
                         "VALUES (?, 'kick', 'TRIGGER-REPLY', 'text', 'exact', 0, 80, 0,"
                         " 100, 0, '[]', 1)", (GUILD,))
        await db.commit()
    router.set_alias_table({})
    msg = fake_message(bot, "kick", 9004)
    first = await router.decide(msg)
    check("with no aliases, the trigger owns the message",
          first.route is Route.TRIGGER, first.route)
    router.set_alias_table({GUILD: {"kick": "kick"}})
    again = await router.decide(msg)
    check("set_alias_table invalidates the memo (save lands immediately)",
          again is not first and again.route is Route.ALIAS,
          (first.route, again.route))
    check("and it cost exactly one recomputation", len(computes) == 2, computes)

    print("\n=== 4. an in-flight decision is never cancelled underneath a listener ===")
    router.set_alias_table({GUILD: {"kick": "kick"}})
    msg = fake_message(bot, "kick", 9005)
    slow_started = asyncio.ensure_future(router.decide(msg))
    await asyncio.sleep(0)                       # task created, still running
    router.invalidate()                          # e.g. cog_unload mid-burst
    check("the awaiting listener is not cancelled", not slow_started.cancelled())
    outcome = await slow_started
    check("…and still gets a decision", outcome.route is Route.ALIAS, outcome.route)

    print("\n=== 5. the memo stays bounded without evicting live work ===")
    router._decisions.clear()
    for i in range(router.CACHE_SIZE + 40):
        t = bot.loop.create_task(asyncio.sleep(0, result=None))
        router._decisions[10000 + i] = t
    await asyncio.sleep(0.05)          # let them all finish
    router._trim()
    done_count = sum(1 for t in router._decisions.values() if t.done())
    check("finished entries are trimmed down to CACHE_SIZE",
          len(router._decisions) <= router.CACHE_SIZE,
          {"size": len(router._decisions), "done": done_count})
    router._decisions.clear()
    inflight = [bot.loop.create_task(asyncio.sleep(5, result="x")) for _ in range(20)]
    for i, t in enumerate(inflight):
        router._decisions[50000 + i] = t
    router._trim()
    check("a burst of in-flight decisions grows the cache instead of cancelling",
          len(router._decisions) == 20 and not any(t.cancelled() for t in inflight),
          len(router._decisions))
    for t in inflight:
        t.cancel()

    print("\n=== 6. with the real cogs loaded, exactly one system acts ===")
    # The exact collision the dashboard used to refuse: alias word `kick` in
    # /trigger_add, and an exact trigger for the word `kick`.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
            VALUES (?, 'trigger_add', 1, '["kick"]')
        """, (GUILD,))
        await db.commit()
    for ext in ("cogs.triggers", "cogs.customcommands", "cogs.command_aliases"):
        await bot.load_extension(ext)
    computes.clear()
    SENT_LOG.clear()
    msg = fake_message(bot, "kick hello now", 9006)
    # exactly what Client.dispatch iterates, in the order it will call them
    listeners = list(getattr(bot, "extra_events", {}).get("on_message", []))
    check("three cogs listen for on_message", len(listeners) == 3, len(listeners))
    await asyncio.gather(*[l(msg) for l in listeners])
    out = " | ".join(str(s[1])[:70] for s in SENT_LOG)
    check("the router computed once for all three listeners",
          computes.count(9006) == 1, computes)
    check("the alias ran the slash command", "Trigger added" in out, out[:140])
    check("the trigger stayed silent for that message", "TRIGGER-REPLY" not in out,
          out[:140])
    check("exactly one system answered", out.count("Trigger added") == 1
          and "CUSTOM" not in out, out[:140])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT trigger_words, response_text FROM triggers "
                              "WHERE trigger_words='hello'")
        added = await cur.fetchall()
    check("…and the command it ran really executed", added == [("hello", "now")], added)

    print(f"\n{'='*60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", *FAILURES, sep="\n  - ")
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
    sys.exit(1 if FAILURES else 0)


asyncio.run(main())
