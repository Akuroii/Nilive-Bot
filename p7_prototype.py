import pathlib
from typing import Union
"""LEGACY (kept as evidence, not a test): this probe documents the PRE-ROUTER behaviour of the alias system — content mutation, global bot.all_commands registration and the sibling-listener race. Those code paths no longer exist, so this file is expected to fail against the current tree; run p8 (bot), p4 (dashboard), p9 (chip UI) and p2 (static guards) instead. Its findings are written up in ALIAS_INVESTIGATION.md §1-§5 and §7.

PROBE 7 — feasibility check for the recommended architecture.

Not a deliverable; a throwaway proof that these three things work on
discord.py 2.7.1 with this codebase:

  1. Register an alias as a real commands.Command whose params mirror the
     slash command (POSITIONAL_OR_KEYWORD for the leading args, KEYWORD_ONLY
     for the trailing 'consume rest' string) using the *real* Parameter API.
  2. Dispatch a BARE alias by building a synthetic message (never mutating
     the original one) + bot.get_context + bot.invoke, so listener ordering
     and other on_message listeners are irrelevant.
  3. Invoke the app-command callback correctly: sc.callback(sc.cog,
     PrefixInteraction(ctx), **kwargs).
"""
import asyncio, inspect, sys, types
# moved into legacy/: the harness lives one level up
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeMessage, FakeGuild, FakeChannel, FakeMember, SENT_LOG)
import aiosqlite
import discord
from discord.ext import commands
from database import DB_PATH

GUILD = 333


async def main():
    fresh_db()
    await init_database()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO command_toggles (guild_id, command_name, enabled, aliases, cooldown_seconds)
            VALUES (?, 'kick', 1, '["k"]', 7)
            ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["k"]', cooldown_seconds=7
        """, (GUILD,))
        await db.commit()

    patch_context_send()
    bot = make_bot()
    await bot.load_extension("cogs.moderation")

    # make FakeMember resolvable by discord.py's MemberConverter (<@id> form)
    async def fake_kick(self, *, reason=None):
        SENT_LOG.append(("MEMBER.KICKED", self.id, reason))
    FakeMember.kick = fake_kick
    def _find(self, key):
        import re
        m = re.match(r'<@!?(\d+)>$', str(key).strip())
        uid = int(m.group(1)) if m else (int(key) if str(key).isdigit() else 111)
        mem = FakeMember(id=uid, guild=self)
        mem.top_role = types.SimpleNamespace(position=5)
        mem.guild_permissions = types.SimpleNamespace(
            kick_members=True, administrator=False, ban_members=True,
            manage_roles=True, mention_everyone=False, manage_messages=True)
        mem.is_bot = lambda: False
        mem.get_top_role = lambda: mem.top_role
        mem.timed_out_until = None
        return mem
    FakeGuild.get_member = _find
    FakeGuild.get_member_named = _find
    FakeMember.guild_permissions = types.SimpleNamespace(
        kick_members=True, administrator=False, ban_members=True,
        manage_roles=True, mention_everyone=False, manage_messages=True)
    FakeMember.guild = None  # set per-instance below

    kick_sc = bot.tree.get_command("kick")
    print("slash _params:", {n: (type(c._annotation).__name__, c._annotation, c.required,
                                 c.default, c.type) for n, c in kick_sc._params.items()})

    # ── 1. build the alias command with the real Parameter API ──────────
    from cogs.command_aliases import PrefixInteraction, check_command_toggles, _run_slash_checks
    import time
    import main as mainmod

    async def alias_callback(ctx, *args, **kwargs):
        names = [n for n, p in ctx.command.params.items()]
        bound = dict(zip(names, args))
        bound.update(kwargs)
        allowed, msg = await check_command_toggles(
            guild_id=ctx.guild.id, cmd_name="kick", member=ctx.author,
            channel_id=ctx.channel.id, cooldowns=mainmod._command_cooldowns,
            now=time.time())
        if not allowed:
            await ctx.send(f"[toggles] {msg}")
            return
        inter = PrefixInteraction(ctx)
        passed, perm_msg = await _run_slash_checks(list(kick_sc.checks), inter)
        if not passed:
            await ctx.send(f"[perms] {perm_msg}")
            return
        await kick_sc.callback(kick_sc.cog, inter, **bound)
        SENT_LOG.append(("BOUND ARGS", {k: str(v)[:40] for k, v in bound.items()}))

    # derive ext.commands params straight from the slash command's own
    # CommandParameter entries (annotation + required + default), with the
    # final string-ish param made KEYWORD_ONLY = discord.py "consume rest".
    AOT = discord.AppCommandOptionType
    CONV = {
        AOT.string: str, AOT.integer: int, AOT.number: float, AOT.boolean: bool,
        AOT.user: discord.Member, AOT.channel: discord.TextChannel,
        AOT.role: discord.Role, AOT.mentionable: Union[discord.Member, discord.Role],
        AOT.attachment: discord.Attachment,
    }
    entries = list(kick_sc._params.values())
    cmd_params = {}
    for i, c in enumerate(entries):
        is_last = i == len(entries) - 1
        ann = CONV.get(c.type, str)
        kind = (inspect.Parameter.KEYWORD_ONLY
                if (is_last and ann is str)
                else inspect.Parameter.POSITIONAL_OR_KEYWORD)
        default = inspect.Parameter.empty if c.required else (
            c.default if c.default is not inspect.Parameter.empty else None)
        cmd_params[c.name] = commands.Parameter(name=c.name, kind=kind,
                                                default=default, annotation=ann)
    cmd = commands.Command(alias_callback, name="k", ignore_extra=True)
    cmd.params = cmd_params
    bot.add_command(cmd)

    @bot.event
    async def on_command_error(ctx, error):
        SENT_LOG.append(("CMD_ERROR", type(error).__name__, str(error)[:160]))
    print("alias cmd params:", {n: (p.kind, p.converter, p.required) for n, p in cmd.params.items()})

    # ── 2. bare-alias dispatch without mutating the original message ────
    guild = FakeGuild(id=GUILD)
    author = FakeMember(id=111, guild=guild)
    author.guild_permissions = types.SimpleNamespace(
        kick_members=True, administrator=False, ban_members=True,
        manage_roles=True, mention_everyone=False, manage_messages=True)
    ch = FakeChannel(guild=guild)
    original = FakeMessage("k <@111> being rude", guild=guild, channel=ch, author=author)

    synthetic = types.SimpleNamespace(
        content="!" + original.content, author=author, guild=guild, channel=ch,
        id=original.id, type=original.type, webhook_id=None, attachments=[],
        embeds=[], mentions=original.mentions, _state=original._state,
        edited_at=None, created_at=None, reference=None, message_reference=None,
    )
    ctx = await bot.get_context(synthetic)
    print("\nctx.command:", ctx.command, "| invoked_with:", ctx.invoked_with)
    await bot.invoke(ctx)
    await asyncio.sleep(0.2)
    print("outbound:", SENT_LOG)

    print("\n=== second call should hit the 7s cooldown ===")
    SENT_LOG.clear()
    synthetic2 = types.SimpleNamespace(**vars(synthetic))
    ctx2 = await bot.get_context(synthetic2)
    await bot.invoke(ctx2)
    await asyncio.sleep(0.2)
    print("outbound:", SENT_LOG)

    print("\n=== original message content untouched? ===")
    print("   ", repr(original.content), "(triggers/custom-commands still see the real text)")

    try:
        await bot.close()
    except Exception:
        pass


asyncio.run(main())
