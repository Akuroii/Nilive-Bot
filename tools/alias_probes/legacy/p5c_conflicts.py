import pathlib
"""LEGACY (kept as evidence, not a test): this probe documents the PRE-ROUTER behaviour of the alias system — content mutation, global bot.all_commands registration and the sibling-listener race. Those code paths no longer exist, so this file is expected to fail against the current tree; run p8 (bot), p4 (dashboard), p9 (chip UI) and p2 (static guards) instead. Its findings are written up in ALIAS_INVESTIGATION.md §1-§5 and §7.

PROBE 5c — who executes what, and the content-mutation race.

For each message: which system(s) produced an outbound action, and
what exact string Triggers._matches was handed (original vs mutated).
"""
import asyncio, sys, types
# moved into legacy/: the harness lives one level up
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeMessage, FakeGuild, FakeChannel, FakeMember, SENT_LOG)
import aiosqlite
from database import DB_PATH

GUILD = 333


async def seed(db):
    await db.execute("""
        INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
        VALUES (?, 'kick', 1, '["k","kk"]')
        ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["k","kk"]', enabled=1
    """, (GUILD,))
    for mt in ("contains", "exact", "startswith"):
        await db.execute("""
            INSERT INTO triggers (guild_id, trigger_words, response_text, response_type,
                                  match_type, fuzzy_match, fuzzy_threshold, case_sensitive,
                                  response_chance, cooldown_seconds, allowed_channels, enabled)
            VALUES (?, 'k', ?, 'text', ?, 0, 80, 0, 100, 0, '[]', 1)
        """, (GUILD, f"TRIGGER[{mt}]-FIRED", mt))
    await db.execute("""
        INSERT INTO custom_commands (guild_id, trigger, allowed_roles, actions, embed_title,
                                     embed_description, embed_color, same_channel,
                                     requires_mention, requires_reason)
        VALUES (?, 'k', '[]', '[]', 'CUSTOM-CMD-K-FIRED', '', '#ED4245', 1, 0, 0)
    """, (GUILD,))
    await db.commit()


async def main():
    fresh_db()
    await init_database()
    async with aiosqlite.connect(DB_PATH) as db:
        await seed(db)
    patch_context_send()
    bot = make_bot()
    for ext in ("cogs.moderation", "cogs.triggers", "cogs.customcommands",
                "cogs.command_aliases"):
        await bot.load_extension(ext)

    trig = bot.cogs["Triggers"]
    seen_by_triggers = []
    real_matches = trig._matches

    def spy(content, *a, **k):
        r = real_matches(content, *a, **k)
        seen_by_triggers.append((content[:24], r))
        return r
    trig._matches = spy

    invoked = []
    real_invoke = type(bot).invoke

    async def instrumented_invoke(self, ctx, /):
        if ctx.command is not None:
            invoked.append(f"PREFIX-COMMAND !{ctx.command.name}")
        return await real_invoke(self, ctx)
    bot.invoke = types.MethodType(instrumented_invoke, bot)

    async def run(label, content, guild_id=GUILD):
        seen_by_triggers.clear(); invoked.clear(); SENT_LOG.clear()
        ch = FakeChannel(guild=FakeGuild(id=guild_id))
        msg = FakeMessage(content, guild=FakeGuild(id=guild_id), channel=ch,
                          author=FakeMember(id=111, guild=FakeGuild(id=guild_id)))
        bot.dispatch("message", msg)
        await asyncio.sleep(0.5)
        sends = [s[1] for s in SENT_LOG]
        print(f"\n--- {label}: {content!r}")
        print(f"    Triggers._matches was called with: {seen_by_triggers}")
        print(f"    prefix commands invoked: {invoked or 'NONE'}")
        print(f"    outbound: {sends or 'NONE'}")
        print(f"    content after all listeners: {msg.content!r}")

    print("alias cmd params:", dict(bot.all_commands['k'].params))
    await run("A bare alias + args", "k <@111> rude")
    await run("B bare alias only", "k")
    await run("C prefixed alias", "!k <@111> rude")
    await run("D prefixed alias, other alias", "!kk <@111> rude")
    await run("E prefixed, alias is a prefix of the word", "!kick <@111> rude")
    await run("F chat mentioning the letter", "ok k?")
    await run("G other guild, same alias", "k <@111> rude", guild_id=999)

    print("\n=== what the *fixed* invocation would do (direct ctx path) ===")
    # simulate a working dispatcher: hand the already-parsed ctx to the alias command
    cmd = bot.all_commands["k"]
    ch = FakeChannel(guild=FakeGuild(id=GUILD))
    m = FakeMessage("!k", guild=FakeGuild(id=GUILD), channel=ch,
                    author=FakeMember(id=111, guild=FakeGuild(id=GUILD)))
    ctx = await bot.get_context(m)
    try:
        await cmd.callback(ctx, member=FakeMember(id=222, guild=FakeGuild(id=GUILD)),
                           reason="rude")
        print("    callback result:", SENT_LOG[:2])
    except Exception as e:
        print(f"    callback raised {type(e).__name__}: {e}")
    sc = bot.tree.get_command("kick")
    print("\n=== app_command introspection (how the callback must be called) ===")
    print("    type:", type(sc).__name__)
    print("    .cog:", sc.cog)
    print("    .callback:", sc.callback)
    print("    has __original_binding:", hasattr(sc.callback, "__original_binding"))
    print("    .checks:", sc.checks)
    print("    .params:", {n: (p.type.value if getattr(p, 'type', None) else None,
                              p.required) for n, p in sc.params.items()})

    try:
        await bot.close()
    except Exception:
        pass


asyncio.run(main())
