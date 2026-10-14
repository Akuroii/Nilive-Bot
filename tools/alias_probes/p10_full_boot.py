"""PROBE 10 — the whole bot still boots, and the message path is intact.

The alias work touched main.py, two other cogs and a shared util, so this is the
"did I break something unrelated" check: every cog main.py lists must load, the
on_message listeners that are NOT part of the alias system must still see the
untouched message, and the router must not claim a message they need.

Run:  python tools/alias_probes/p10_full_boot.py
"""
import asyncio, pathlib, re, sys, types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0]))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeMessage, FakeGuild, FakeChannel, FakeMember, SENT_LOG)

import aiosqlite
from database import DB_PATH

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


def cog_list():
    src = (HERE.parents[1] / "main.py").read_text()
    start = src.index("cog_files = [")
    block = src[start:src.index("\n    ]", start)]
    return re.findall(r'"(cogs\.[a-z_0-9]+)"', block)


async def main():
    fresh_db()
    await init_database()
    patch_context_send()
    cogs = cog_list()
    print(f"\n=== 1. every cog main.py lists still loads ({len(cogs)}) ===")
    bot = make_bot()
    failed = []
    for ext in cogs:
        try:
            await bot.load_extension(ext)
        except Exception as e:
            failed.append((ext, f"{type(e).__name__}: {e}"))
    check("no cog failed to load", not failed, failed)
    check("the three message-parsing cogs are present",
          {"CommandAliases", "Triggers", "CustomCommands"} <= set(bot.cogs),
          sorted(bot.cogs))

    print("\n=== 2. load order is genuinely free ===")
    # Unload and reload the whole set in reverse. The old alias cog depended on
    # being last; the router must not depend on any order at all.
    for ext in reversed(cogs):
        try:
            await bot.unload_extension(ext)
        except Exception:
            pass
    reloaded_failed = []
    for ext in reversed(cogs):
        try:
            await bot.load_extension(ext)
        except Exception as e:
            reloaded_failed.append((ext, f"{type(e).__name__}: {e}"))
    check("reverse-order reload succeeds", not reloaded_failed, reloaded_failed)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
            VALUES (?, 'trigger_add', 1, '["zz"]')
        """, (GUILD,))
        await db.commit()
    cog = bot.cogs["CommandAliases"]
    await cog._sync_aliases()
    check("aliases register regardless of order",
          cog._table.get(GUILD, {}).get("zz") == "trigger_add", cog._table)

    print("\n=== 3. the other on_message listeners still get the real message ===")
    router = getattr(bot, "nero_router", None)
    check("the bot carries its router", router is not None)
    listener_count = len(bot.extra_events.get("on_message", []))
    check("several cogs listen to on_message", listener_count >= 4, listener_count)
    # Dispatch one fake message to *every* registered on_message listener and
    # assert each was handed the untouched text — the old implementation rewrote
    # it mid-flight, which is how the trigger listener started matching "!zz".
    seen = []

    async def probe_listener(message):
        seen.append(message.content)

    bot.add_listener(probe_listener, "on_message")
    g = FakeGuild(id=GUILD)
    g._state = types.SimpleNamespace(
        get_user=lambda uid: None, dispatch=lambda *a, **k: None,
        member_cache=__import__("discord").MemberCacheFlags.none(),
        member_cache_flags=__import__("discord").MemberCacheFlags.none(),
        http=None)
    ch = FakeChannel(guild=g)
    msg = FakeMessage("zz some words", guild=g, channel=ch,
                      author=FakeMember(id=111, guild=g))
    msg.id = 4242
    msg._state = g._state
    listeners = list(bot.extra_events.get("on_message", []))
    await asyncio.gather(*[l(msg) for l in listeners], return_exceptions=True)
    check("every listener saw the original text",
          seen and set(seen) == {"zz some words"}, seen[:3])
    check("message.content was never rewritten", msg.content == "zz some words",
          msg.content)
    check("the router claims this message for the alias, not the trigger",
          (await router.decide(msg)).route.value in ("alias", "custom_command")
          and (await router.decide(msg)).route.name == "ALIAS",
          (await router.decide(msg)).route)

    print("\n=== 4. plain chat is untouched by all of this ===")
    SENT_LOG.clear()
    plain = FakeMessage("just chatting about work", guild=g, channel=ch,
                        author=FakeMember(id=111, guild=g))
    plain.id = 4243
    plain._state = g._state
    await asyncio.gather(*[l(plain) for l in listeners], return_exceptions=True)
    check("an alias-less sentence runs no command", "Trigger added" not in
          " | ".join(str(s[1])[:60] for s in SENT_LOG),
          " | ".join(str(s[1])[:60] for s in SENT_LOG)[:140])
    dec = await router.decide(plain)
    check("…and the router says trigger/NONE for it",
          dec.route.name in ("TRIGGER", "NONE"), dec.route.name)

    print(f"\n{'='*60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", *FAILURES, sep="\n  - ")
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
    sys.exit(1 if FAILURES else 0)


asyncio.run(main())
