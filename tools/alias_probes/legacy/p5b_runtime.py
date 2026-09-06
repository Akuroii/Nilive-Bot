import pathlib
"""LEGACY (kept as evidence, not a test): this probe documents the PRE-ROUTER behaviour of the alias system — content mutation, global bot.all_commands registration and the sibling-listener race. Those code paths no longer exist, so this file is expected to fail against the current tree; run p8 (bot), p4 (dashboard), p9 (chip UI) and p2 (static guards) instead. Its findings are written up in ALIAS_INVESTIGATION.md §1-§5 and §7.

PROBE 5b — full runtime with every on_message listener instrumented.

Records the exact interleaving of all on_message listeners for a bare
alias message, so we can see (a) whether process_commands runs first,
(b) whether the alias listener's content mutation leaks into the
other listeners (a race), and (c) which system actually executes.
"""
import asyncio, sys, types
# moved into legacy/: the harness lives one level up
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeMessage, FakeGuild, FakeChannel, FakeMember, SENT_LOG)
import aiosqlite
from database import DB_PATH

GUILD = 333
LOG = []


async def seed(db):
    await db.execute("""
        INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
        VALUES (?, 'kick', 1, '["k"]')
        ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["k"]', enabled=1
    """, (GUILD,))
    # trigger 'k' contains -> matches any message containing the letter k
    await db.execute("""
        INSERT INTO triggers (guild_id, trigger_words, response_text, response_type,
                              match_type, fuzzy_match, fuzzy_threshold, case_sensitive,
                              response_chance, cooldown_seconds, allowed_channels, enabled)
        VALUES (?, 'k', 'TRIGGER-K-CONTAINS', 'text', 'contains', 0, 80, 0, 100, 0, '[]', 1)
    """, (GUILD,))
    # trigger 'k' exact -> only matches a message that IS 'k'
    await db.execute("""
        INSERT INTO triggers (guild_id, trigger_words, response_text, response_type,
                              match_type, fuzzy_match, fuzzy_threshold, case_sensitive,
                              response_chance, cooldown_seconds, allowed_channels, enabled)
        VALUES (?, 'k', 'TRIGGER-K-EXACT', 'text', 'exact', 0, 80, 0, 100, 0, '[]', 1)
    """, (GUILD,))
    # custom command trigger 'k' (custom_commands has no enabled column)
    await db.execute("""
        INSERT INTO custom_commands (guild_id, trigger, allowed_roles, actions, embed_title,
                                     embed_description, embed_color, same_channel,
                                     requires_mention, requires_reason)
        VALUES (?, 'k', '[]', '[]', 'CUSTOM-CMD-K-FIRED', '', '#ED4245', 1, 0, 0)
    """, (GUILD,))
    await db.commit()


def dispatch(bot, content, guild_id=GUILD):
    ch = FakeChannel(guild=FakeGuild(id=guild_id))
    msg = FakeMessage(content, guild=FakeGuild(id=guild_id), channel=ch,
                      author=FakeMember(id=111, guild=FakeGuild(id=guild_id)))
    bot.dispatch("message", msg)
    return msg, ch


async def main():
    fresh_db()
    await init_database()
    async with aiosqlite.connect(DB_PATH) as db:
        await seed(db)
    patch_context_send()
    bot = make_bot()

    src = open("/home/user/Nilive-Bot/main.py").read()
    block = src.split("cog_files = [", 1)[1].split("]", 1)[0]
    cogs = [l.strip().strip(",").strip('"') for l in block.splitlines()
            if l.strip().strip(",").strip('"').startswith("cogs.")]
    for cog in cogs:
        try:
            await bot.load_extension(cog)
        except Exception as e:
            print(f"  !! {cog} failed to load: {type(e).__name__}: {e}")

    print("extra_events keys:", sorted(bot.extra_events))
    key = next((k for k in ("on_message", "message") if k in bot.extra_events), None)
    print("on_message listener count:", len(bot.extra_events.get(key, [])))

    orig = list(bot.extra_events.get(key, []))
    wrapped = []
    for i, f in enumerate(orig):
        async def w(message, i=i, f=f):
            LOG.append(f"[{i}] {f.__qualname__}: ENTER, content={message.content!r}")
            try:
                await f(message)
            except Exception as e:
                LOG.append(f"[{i}] {f.__qualname__}: RAISED {type(e).__name__}: {e}")
            LOG.append(f"[{i}] {f.__qualname__}: EXIT, content={message.content!r}")
        wrapped.append(w)
    bot.extra_events[key] = wrapped

    real_process = type(bot).process_commands

    async def instrumented(self, message, /):
        LOG.append(f"[X] Bot.on_message -> process_commands: content={message.content!r}")
        return await real_process(self, message)
    bot.process_commands = types.MethodType(instrumented, bot)

    alias_cog = bot.cogs["CommandAliases"]
    print("alias cog._registered:", alias_cog._registered)
    print("all_commands:", sorted(bot.all_commands))
    cmd = bot.all_commands.get("k")
    print("alias command params:", dict(cmd.params) if cmd else None)

    scenarios = [
        ("A: bare 'k <@111> rude'", "k <@111> rude", GUILD),
        ("B: bare 'k' only", "k", GUILD),
        ("C: explicit '!k <@111> rude'", "!k <@111> rude", GUILD),
        ("D: plain chat 'thanks for the help'", "thanks for the help", GUILD),
        ("E: bare 'k x' in guild 999 (no rows at all)", "k <@111> rude", 999),
    ]
    for label, content, gid in scenarios:
        print(f"\n=== {label} ===")
        LOG.clear(); SENT_LOG.clear()
        msg, ch = dispatch(bot, content, gid)
        await asyncio.sleep(0.8)
        for line in LOG:
            print("   ", line)
        print("    final content:", repr(msg.content))
        sends = [s for s in SENT_LOG if s[0] in ("ctx.send", "reply")]
        print("    outbound:", [str(s)[:110] for s in sends][:6] or "NONE")

    try:
        await bot.close()
    except Exception:
        pass


asyncio.run(main())
