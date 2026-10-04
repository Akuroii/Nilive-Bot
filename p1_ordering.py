import pathlib
"""PROBE 1 — discord.py 2.7.1 dispatch semantics.

Question: does a *cog* `on_message` listener run BEFORE Bot.on_message ->
process_commands, as cogs/command_aliases.py's comment claims?

Test: register a prefix command "k" and a cog listener that mutates
message.content (the exact trick the alias cog uses). Then dispatch a real
"message" event through Bot.dispatch and see what process_commands observes.
"""
import asyncio, sys, types
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from p0_env import make_bot, FakeMessage, FakeGuild, FakeChannel, FakeMember, patch_context_send  # noqa

from discord.ext import commands


async def main():
    patch_context_send()
    bot = make_bot()
    print("discord.py:", __import__("discord").__version__)
    print("command_prefix:", bot.command_prefix, "case_insensitive:", bot.case_insensitive)

    seen = {}
    order = []

    async def k_callback(ctx):
        order.append("command-invoked")
        seen["invoked_with"] = ctx.invoked_with
        seen["content_at_invoke"] = ctx.message.content

    cmd = commands.Command(k_callback, name="k")
    bot.add_command(cmd)
    print("all_commands keys:", sorted(bot.all_commands)[:10])

    # instrument process_commands to record what content it reads first
    real_process = type(bot).process_commands

    async def instrumented(self, message, /):
        order.append("process_commands-enter")
        seen["content_at_process_commands"] = message.content
        return await real_process(self, message)

    bot.process_commands = types.MethodType(instrumented, bot)

    class AliasCogSim(commands.Cog):
        """Mimics cogs/command_aliases.py's listener exactly."""
        @commands.Cog.listener()
        async def on_message(self, message):
            if message.author.bot or not message.guild:
                return
            parts = message.content.split()
            if not parts:
                return
            if parts[0].lower() == "k":
                order.append("alias-listener-mutate")
                message.content = f"!{message.content}"

    await bot.add_cog(AliasCogSim(bot))
    print("extra_events['on_message'] =", [f.__qualname__ for f in bot.extra_events.get("on_message", [])])
    print("getattr(bot,'on_message') =", bot.on_message.__qualname__)

    msg = FakeMessage("k @someone reason", guild=FakeGuild(), channel=FakeChannel(),
                      author=FakeMember(id=111))
    bot.dispatch("message", msg)
    await asyncio.sleep(0.2)

    print("\n--- execution order ---")
    for i, step in enumerate(order, 1):
        print(f"  {i}. {step}")
    print("content seen by process_commands:", repr(seen.get("content_at_process_commands")))
    print("content after all listeners:", repr(msg.content))
    print("command invoked?", "invoked_with" in seen)

    # second scenario: what the mutation WOULD need — process_commands runs last
    print("\n--- control: manual call after mutation (i.e. what actually works) ---")
    msg2 = FakeMessage("k @someone reason")
    msg2.content = "!" + msg2.content
    await bot.process_commands(msg2)
    await asyncio.sleep(0.05)
    print("content at process_commands:", repr(msg2.content))

    await bot.close()


asyncio.run(main())
