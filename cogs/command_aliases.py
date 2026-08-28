"""Command Aliases — bare-word aliases for slash commands (ProBot style).

An admin maps a short word to a slash command in the dashboard (e.g. ``k``
for ``/kick``) and members then type it as a plain message::

    k @SomeUser being rude

No prefix is involved: aliases *replace* prefixes rather than extending them.

ARCHITECTURE (rewritten 2026-08-28 — see /ALIAS_INVESTIGATION.md)
------------------------------------------------------------------
    dashboard  ->  POST /api/commands/settings/<cmd>
                ->  command_toggles.aliases (per guild) + sync flag
    bot        ->  this cog re-reads the flag, builds a PER-GUILD alias index
                ->  publishes it to utils/message_router.MessageRouter
                ->  on_message asks the router who owns the message
                ->  on "alias": bridge the slash command's parameters into a
                   throwaway commands.Command (converters included) and run it
                   through bot.get_context() + bot.invoke()

Three things this deliberately does NOT do, and why:

  * it does not mutate ``message.content``. ``Bot.dispatch`` runs
    ``process_commands`` *before* any cog ``on_message`` listener
    (discord.py 2.7.1, ``ext/commands/bot.py:237-242``), so a rewrite can
    never reach the parser — and it leaks into the other listeners, which run
    as sibling tasks and may read the text either before or after the rewrite.
  * it does not register aliases in ``bot.all_commands``. That table is
    process-global, so a per-guild setting would have applied in every server
    (and would have polluted ``!help``). The transient ``Command`` objects
    built here exist only to reuse discord.py's converter pipeline.
  * it does not decide precedence on its own. The router owns the single rule
    ``PREFIX_COMMAND > ALIAS > CUSTOM_COMMAND > TRIGGER``, so one message can
    never be executed twice and triggers/custom commands are not corrupted by
    this feature.

Permissions, enable/disable state and cooldowns run through
``utils/command_gating`` — literally the same function ``NeroCommandTree.interaction_check``
calls, on the same cooldown dict, so ``/kick`` and ``k`` cannot disagree about whether a member
may run something or how recently they last did. The parent command's own
``@app_commands.checks`` are re-applied to the wrapper.

The alias *index* is built without consulting the command tree, and the transient ``Command`` for
a word is built the first time that word is used (``_command_for``). Building both at sync time
looked tidy and was not: a parent cog that is still loading — or was just ``/reload``'ed — left the
alias permanently unregistered until the next dashboard save, with nothing to show for it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import DB_PATH
from utils.message_router import Route, get_router


# ── Compatibility wrapper ─────────────────────────────────────────
# Adapts a discord.ext.commands.Context into the minimal subset of
# discord.Interaction that the slash command callbacks in this repo actually
# use. Anything else raises AttributeError on purpose: if a future command
# reaches for a new Interaction attribute, it must fail loudly rather than
# silently do the wrong thing.
#
# Audited surface (all 93 slash commands):
#   interaction.user        -> ctx.author
#   interaction.guild       -> ctx.guild
#   interaction.channel     -> ctx.channel
#   interaction.channel_id  -> ctx.channel.id
#   interaction.permissions -> ctx.channel.permissions_for(ctx.author)
#   interaction.response.send_message() -> ctx.send()
#   interaction.response.defer()        -> no-op
#   interaction.response.is_done()      -> tracks send state
#   interaction.followup.send()         -> ctx.send()


class _PrefixResponse:
    """Mimics InteractionResponse for the message-command context."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._done = False

    async def send_message(self, content=None, *, embed=None, ephemeral=False,
                           view=None, delete_after=None, allowed_mentions=None):
        kwargs: Dict[str, Any] = {}
        if view is not None:
            kwargs["view"] = view
        if delete_after is not None:
            kwargs["delete_after"] = delete_after
        if allowed_mentions is not None:
            kwargs["allowed_mentions"] = allowed_mentions
        # `ephemeral` has no message equivalent: an alias reply is public by
        # definition. Kept in the signature so callbacks that pass it do not
        # blow up.
        await self._ctx.send(content=content, embed=embed, **kwargs)
        self._done = True

    async def defer(self, ephemeral=False, thinking=False):
        # No-op: message commands have no 3-second interaction deadline, so a
        # command that defers then uses followup.send() just sends normally.
        pass

    def is_done(self):
        return self._done


class _PrefixFollowup:
    """Mimics Interaction.followup for the message-command context."""

    def __init__(self, ctx):
        self._ctx = ctx

    async def send(self, content=None, *, embed=None, ephemeral=False,
                   view=None, delete_after=None, allowed_mentions=None,
                   silent=False):
        kwargs: Dict[str, Any] = {}
        if view is not None:
            kwargs["view"] = view
        if delete_after is not None:
            kwargs["delete_after"] = delete_after
        if allowed_mentions is not None:
            kwargs["allowed_mentions"] = allowed_mentions
        await self._ctx.send(content=content, embed=embed, **kwargs)


class PrefixInteraction:
    """Thin adapter: a Context shaped like an Interaction.

    ``interaction.permissions`` is what
    ``@app_commands.checks.has_permissions`` reads (verified against
    discord.py 2.7.1 ``app_commands/checks.py`` — that check uses nothing but
    ``interaction.permissions``), and ``ctx.channel.permissions_for(author)``
    computes the same channel-resolved permissions Discord sends in the
    interaction payload.
    """

    __slots__ = ("user", "guild", "channel", "channel_id",
                 "response", "followup", "permissions", "message", "command")

    def __init__(self, ctx, command=None):
        self.user = ctx.author
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.channel_id = ctx.channel.id
        self.response = _PrefixResponse(ctx)
        self.followup = _PrefixFollowup(ctx)
        self.permissions = ctx.channel.permissions_for(ctx.author)
        self.message = ctx.message
        self.command = command


# ── Shared permission / toggle / cooldown check ──────────────────
# One implementation in utils/command_gating.py, used by the slash path
# (NeroCommandTree.interaction_check in main.py) and by the alias gate below,
# sharing one cooldown dict — two copies of a gate is how "works as a slash
# command, unrestricted as an alias" bugs start.

from utils.command_gating import check_command_toggles


async def _run_slash_checks(checks_list, interaction) -> Tuple[bool, Optional[str]]:
    """Run the parent command's ``@app_commands.checks`` against the wrapper.

    These are Discord-level requirements (e.g. ``has_permissions(kick_members=
    True)``) and are separate from the dashboard's command_toggles system, so
    both have to be applied for an alias to be equivalent to the slash
    command.
    """
    if not checks_list:
        return True, None

    for check_fn in checks_list:
        try:
            result = check_fn(interaction)
            if asyncio.iscoroutine(result):
                result = await result
            if not result:
                return False, "You don't have permission to use this command."
        except app_commands.MissingPermissions as e:
            missing = ", ".join(
                p.replace("_", " ").title() for p in e.missing_permissions
            )
            return False, f"You need the following Discord permissions: {missing}"
        except app_commands.CheckFailure as e:
            return False, str(e) or "You don't have permission to use this command."
        except Exception as e:  # never fail open on an unknown check
            return False, (f"Could not verify permissions for that command "
                           f"({type(e).__name__}: {e}). Use the slash command "
                           f"instead.")

    return True, None


class AliasGateError(commands.CommandError):
    """Raised by the per-command gate check (toggles / roles / cooldowns).

    A dedicated type so ``on_command_error`` can answer with its message
    verbatim instead of a generic "command failed" note. It is raised from a
    *check* — which discord.py runs before argument parsing — so a disabled
    command says "this command is disabled here" rather than complaining about
    missing arguments first. That is also the order the slash path uses, since
    ``NeroCommandTree.interaction_check`` runs before the callback too.
    """


# ── Slash -> message parameter bridging ──────────────────────────

_AOT = discord.AppCommandOptionType

_TYPE_TO_CONVERTER: Dict[Any, Any] = {
    _AOT.string: str,
    _AOT.integer: int,
    _AOT.number: float,
    _AOT.boolean: bool,
    _AOT.user: discord.Member,
    _AOT.role: discord.Role,
    _AOT.mentionable: Union[discord.Member, discord.Role],
    _AOT.channel: discord.abc.GuildChannel,
    _AOT.attachment: discord.Attachment,
}

_CHANNEL_TYPE_TO_CLASS: Dict[Any, Any] = {
    discord.ChannelType.text: discord.TextChannel,
    discord.ChannelType.news: discord.TextChannel,
    discord.ChannelType.voice: discord.VoiceChannel,
    discord.ChannelType.stage_voice: discord.VoiceChannel,
    discord.ChannelType.category: discord.CategoryChannel,
    discord.ChannelType.forum: discord.ForumChannel,
}
# media_gallery exists only on newer discord.py builds; look it up lazily so
# this module never fails to import on an older one.
_mg = getattr(discord.ChannelType, "media_gallery", None)
if _mg is not None:
    _CHANNEL_TYPE_TO_CLASS[_mg] = discord.ForumChannel


@dataclass
class _ParamSpec:
    """One bridged parameter: what to parse and how."""
    name: str
    converter: Any
    required: bool = True
    default: Any = inspect.Parameter.empty
    choices: List[Tuple[str, Any]] = field(default_factory=list)
    wants_choice_object: bool = False

    @property
    def is_trailing_text(self) -> bool:
        """A plain-text param: worth "consume the rest of the line" handling.

        ``/kick member reason`` takes the whole remainder as the reason, and
        that is what members expect from ``k @user being rude`` too. discord.py
        gives exactly that behaviour to a KEYWORD_ONLY parameter.
        """
        return self.converter is str and not self.choices


class _ChoiceConverter(commands.Converter):
    """Accepts a choice's value or its display name; returns the right shape."""

    def __init__(self, choices: List[Tuple[str, Any]], as_choice_object: bool):
        self._by_value = {str(v).lower(): (n, v) for n, v in choices}
        self._by_name = {str(n).lower(): (n, v) for n, v in choices}
        self._as_choice_object = as_choice_object

    async def convert(self, ctx, argument):
        key = (argument or "").strip().lower()
        hit = self._by_value.get(key) or self._by_name.get(key)
        if hit is None:
            options = ", ".join(n for n, _ in self._by_value.values())
            raise commands.BadArgument(
                f"Invalid option `{argument}`. Expected one of: {options}")
        name, value = hit
        if self._as_choice_object:
            return app_commands.Choice(name=name, value=value)
        return value


def _slash_parameter_specs(sc) -> List[_ParamSpec]:
    """Read the parent slash command's parameters.

    ``Command._params`` (a dict of ``CommandParameter``) is used when present
    because it is the only place ``type``/``channel_types``/``choices`` live;
    ``inspect.signature`` on the raw callback is the fallback so this keeps
    working if that private layout ever changes.
    """
    specs: List[_ParamSpec] = []

    params = getattr(sc, "_params", None)
    if isinstance(params, dict) and params:
        for p in params.values():
            converter = _TYPE_TO_CONVERTER.get(getattr(p, "type", None), str)
            if getattr(p, "type", None) is _AOT.channel:
                channel_types = list(getattr(p, "channel_types", None) or [])
                if len(channel_types) == 1:
                    converter = _CHANNEL_TYPE_TO_CLASS.get(
                        channel_types[0], converter)
            raw_choices = list(getattr(p, "choices", None) or [])
            choices = [(c.name, c.value) for c in raw_choices
                       if hasattr(c, "value")]
            annotation = getattr(p, "_annotation", inspect.Parameter.empty)
            wants_obj = (getattr(annotation, "__origin__", None)
                         is app_commands.Choice or
                         annotation is app_commands.Choice)
            default = getattr(p, "default", inspect.Parameter.empty)
            specs.append(_ParamSpec(
                name=p.name,
                converter=converter,
                required=bool(getattr(p, "required", True)),
                default=default,
                choices=choices,
                wants_choice_object=wants_obj,
            ))
        return specs

    try:
        sig = inspect.signature(sc.callback)
    except (TypeError, ValueError):
        return specs
    for i, (name, param) in enumerate(sig.parameters.items()):
        if i == 0 or name in ("self", "interaction"):
            continue
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            continue
        ann = param.annotation
        if ann is inspect.Parameter.empty:
            converter = str
        else:
            converter = _TYPE_TO_CONVERTER.get(ann, ann)
        specs.append(_ParamSpec(
            name=name,
            converter=converter,
            required=param.default is inspect.Parameter.empty,
            default=param.default,
        ))
    return specs


def build_alias_command(bot, alias: str, parent_name: str,
                        runner: Callable,
                        gate: Optional[Callable] = None) -> Optional[commands.Command]:
    """Create the throwaway command object used to parse + invoke an alias.

    It is never added to ``bot.all_commands``: it exists so that
    ``Command._parse_arguments`` (and therefore every discord.py converter and
    its error types) does the argument work, instead of a hand-rolled parser
    that would drift from prefix-command behaviour.
    """
    sc = bot.tree.get_command(parent_name)
    if sc is None:
        return None

    specs = _slash_parameter_specs(sc)
    if specs:
        params: Dict[str, commands.Parameter] = {}
        for i, spec in enumerate(specs):
            converter: Any = spec.converter
            if spec.choices:
                converter = _ChoiceConverter(spec.choices, spec.wants_choice_object)
            is_last = i == len(specs) - 1
            kind = (inspect.Parameter.KEYWORD_ONLY
                    if (is_last and spec.is_trailing_text)
                    else inspect.Parameter.POSITIONAL_OR_KEYWORD)
            default = (inspect.Parameter.empty if spec.required
                       else spec.default)
            if default is None and spec.required:
                default = inspect.Parameter.empty
            if not spec.required and default is inspect.Parameter.empty:
                default = None
            params[spec.name] = commands.Parameter(
                name=spec.name, kind=kind, default=default, annotation=converter)
    else:
        # No parameters (or an unreadable signature): pass the raw remainder
        # through as a single trailing string, which is what most of these
        # commands want anyway.
        params = {}

    cmd = commands.Command(runner, name=alias,
                           enabled=True, ignore_extra=True)
    if params:
        cmd.params = params
    cmd.extras["nero_alias_parent"] = parent_name
    cmd.extras["nero_alias_word"] = alias
    if gate is not None:
        # Checks run inside Command.can_run, i.e. before argument parsing —
        # see AliasGateError for why that ordering matters.
        cmd.add_check(gate)
    sc_desc = getattr(sc, "description", "") or parent_name
    cmd.help = f"Alias for /{parent_name} — {sc_desc}"
    cmd.brief = f"Alias for /{parent_name}"
    return cmd


# ── The cog ──────────────────────────────────────────────────────

#: seconds between sync-flag polls. A dashboard save takes at most this long to
#: take effect; it was 30s, which is longer than most people wait before
#: concluding "it doesn't work".
SYNC_INTERVAL_SECONDS = 5


class CommandAliases(commands.Cog):
    """Bare-word aliases for slash commands, scoped per guild."""

    SYNC_INTERVAL_SECONDS = SYNC_INTERVAL_SECONDS

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {alias word: parent command name}
        self._table: Dict[int, Dict[str, str]] = {}
        # (parent, alias) -> the transient Command, built on first use
        self._built: Dict[Tuple[str, str], commands.Command] = {}
        self.last_sync_error: Optional[str] = None
        self.last_sync_at: Optional[float] = None

    async def cog_load(self):
        await self._sync_aliases()
        if not self._alias_sync_check.is_running():
            self._alias_sync_check.start()

    def cog_unload(self):
        if self._alias_sync_check.is_running():
            self._alias_sync_check.cancel()
        try:
            get_router(self.bot).set_alias_table({})
        except Exception:
            pass
        self._table.clear()
        self._built.clear()

    # ── sync ───────────────────────────────────────────────────────────

    async def _read_alias_rows(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT guild_id, command_name, aliases, updated_at, id
                FROM command_toggles
                WHERE aliases IS NOT NULL
                  AND aliases != '[]'
                  AND aliases != ''
                ORDER BY guild_id ASC, updated_at DESC, id DESC
            """)
            return await cursor.fetchall()

    async def _sync_aliases(self) -> int:
        """Rebuild the per-guild alias index from the database.

        Raises on failure — the caller leaves the sync flag set so the next
        poll retries instead of silently consuming a half-applied change.
        """
        rows = await self._read_alias_rows()

        # guild_id -> alias -> parent. Rows are ordered newest-first, so the
        # first writer wins: if two commands in one guild claim the same word,
        # the most recently saved one owns it (the dashboard warns about this
        # while it is being created).
        per_guild: Dict[int, Dict[str, str]] = {}
        skipped: List[str] = []
        for guild_id, command_name, aliases_json, _updated, _rid in rows:
            try:
                aliases = json.loads(aliases_json)
            except (json.JSONDecodeError, TypeError):
                skipped.append(f"{guild_id}/{command_name}: malformed aliases JSON")
                continue
            if not isinstance(aliases, list):
                continue
            guild_map = per_guild.setdefault(int(guild_id), {})
            for alias in aliases:
                if not isinstance(alias, str):
                    continue
                alias = alias.strip().lower()
                if not alias or alias == command_name:
                    continue
                if alias in guild_map:
                    continue          # a newer row already owns this word
                guild_map[alias] = command_name

        # The word -> command map is all the router needs, and it is built
        # without consulting the command tree. An alias whose parent cog is
        # still loading (or was just /reload'ed out of the tree) must not
        # quietly vanish until the next dashboard save — that was a real
        # failure mode of building everything at sync time. The throwaway
        # Command that does the argument parsing is therefore built lazily,
        # the first time the word is actually used (see _command_for).
        #
        # Prefix commands are deliberately NOT filtered out of this index:
        # `!k` belongs to process_commands and bare `k` belongs to the alias,
        # which is exactly what the router does, so nothing needs dropping.
        table: Dict[int, Dict[str, str]] = {
            gid: dict(m) for gid, m in per_guild.items() if m
        }
        self._table = table
        # The tree may have changed since the last build; force a rebuild.
        self._built.clear()
        get_router(self.bot).set_alias_table(table)

        registered = sum(len(m) for m in table.values())
        self.last_sync_at = time.time()
        self.last_sync_error = "; ".join(skipped[:10]) or None
        await self._record_sync_status(registered, skipped)

        print(f"[ALIASES] {registered} alias(es) across {len(table)} guild(s)")
        for note in skipped[:10]:
            print(f"[ALIASES] skipped {note}")
        return registered

    async def _record_sync_status(self, registered: int, skipped: List[str]) -> None:
        """Expose sync problems to the dashboard instead of eating them.

        The old code's `except Exception: pass` is what let a completely dead
        feature look "ready for manual testing": every failure was invisible.
        """
        status = json.dumps({
            "at": self.last_sync_at,
            "registered": registered,
            "skipped": skipped[:20],
            "error": self.last_sync_error,
        })
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO bot_settings (key, value)
                    VALUES ('command_aliases_last_sync', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """, (status,))
                await db.commit()
        except Exception as e:
            print(f"[ALIASES] could not record sync status: {e}")

    # ── invocation ─────────────────────────────────────────────────────

    def _make_runner(self, parent_name: str) -> Callable:
        """Build the callback a transient alias Command invokes.

        Runs after the gate check, in this order:
          1. the command's own app-command checks (permissions) — not part of
             the dashboard row, so they are not in the gate
          2. the real callback, with the cog instance bound
        """
        async def runner(ctx, *args, **kwargs):
            names = list(ctx.command.params.keys())
            bound: Dict[str, Any] = dict(zip(names, args))
            bound.update(kwargs)

            # Look the command up at call time, not sync time, so a reloaded
            # cog / re-synced tree is picked up without waiting for a re-sync.
            sc = self.bot.tree.get_command(parent_name)
            if sc is None:
                await ctx.send(f"Command `/{parent_name}` not found. "
                               f"It may have been removed.")
                return

            interaction = PrefixInteraction(ctx, command=sc)

            slash_checks = list(getattr(sc, "checks", None) or [])
            passed, perm_msg = await _run_slash_checks(slash_checks, interaction)
            if not passed:
                await ctx.send(perm_msg)
                return

            try:
                # ``app_commands`` commands keep an *unbound* callback: the
                # tree itself calls ``callback(binding, interaction)``, where
                # ``binding`` is the cog the command lives on. There is no
                # ``.cog`` attribute on these objects (that belongs to the
                # prefix-command API) — reading it raises AttributeError, which
                # is exactly what killed the previous implementation.
                binding = getattr(sc, "binding", None)
                if binding is None:
                    await sc.callback(interaction, **bound)
                else:
                    await sc.callback(binding, interaction, **bound)
            except TypeError as e:
                # The usual cause is a parameter we could not bridge (e.g. a
                # custom annotation). Say so instead of dying silently.
                raise commands.CommandInvokeError(
                    TypeError(f"`{ctx.command.name}` could not be run as an "
                              f"alias: {e}")) from e
            except commands.CommandError:
                raise
            except discord.HTTPException as e:
                raise commands.CommandInvokeError(e) from e

        return runner

    def _make_gate(self, parent_name: str) -> Callable:
        """Build the check that applies this guild's dashboard row to an alias.

        Delegates to the same helper the slash path uses
        (utils/command_gating.check_command_toggles) and the same cooldown
        dict, so ``/kick`` and the alias ``k`` cannot disagree about whether a
        member may run the command, and cannot each keep their own timer.
        """

        async def gate(ctx: commands.Context) -> bool:
            from main import _command_cooldowns, _prune_command_cooldowns

            now = time.time()
            allowed, msg = await check_command_toggles(
                guild_id=ctx.guild.id,
                command_name=parent_name,
                member=ctx.author,
                channel_id=ctx.channel.id,
                cooldowns=_command_cooldowns,
                now=now,
                guild_owner_id=getattr(ctx.guild, "owner_id", None),
            )
            # The slash path prunes on every interaction; do the same here or
            # the dict grows without bound on an alias-heavy guild.
            _prune_command_cooldowns(now)
            if not allowed:
                raise AliasGateError(msg or
                                     f"`/{parent_name}` isn't available here.")
            return True

        return gate

    async def _resolve_prefix(self, message) -> str:
        """The bot's own prefix, for shaping the synthetic context.

        Never hardcoded: ``command_prefix`` is main.py's business. A callable
        prefix that returns something non-string falls back to skipping the
        prefix entirely and parsing positionally from the raw text instead.
        """
        try:
            prefixes = await self.bot.get_prefix(message)
        except Exception:
            return "!"
        if isinstance(prefixes, str):
            return prefixes
        for candidate in prefixes or []:
            if isinstance(candidate, str):
                return candidate
        return ""

    def _command_for(self, parent_name: str, alias: str) -> Optional[commands.Command]:
        """The throwaway parse/invoke command for one (parent, alias) pair.

        Built on use rather than at sync time, and rebuilt after every sync, so
        a command that is momentarily absent from the tree (cog reload) cannot
        leave a permanently dead alias behind.
        """
        key = (parent_name, alias)
        cmd = self._built.get(key)
        if cmd is not None:
            return cmd
        cmd = build_alias_command(self.bot, alias, parent_name,
                                  self._make_runner(parent_name),
                                  self._make_gate(parent_name))
        if cmd is None:
            return None
        self._built[key] = cmd
        return cmd

    async def _invoke_alias(self, message, decision) -> None:
        alias_word = decision.alias["alias"]
        parent_name = decision.alias["parent"]

        content = message.content or ""
        prefix = await self._resolve_prefix(message)
        if prefix and content.startswith(prefix):
            # Already a prefixed invocation: process_commands owns it, and the
            # router agrees (it would not have returned ALIAS).
            return

        # Build a *throwaway* message view that carries the prefix so
        # bot.get_context() returns a real Context — with a StringView already
        # positioned after prefix + command name — for discord.py's parser to
        # consume. The message the user actually sent is never touched; see the
        # module docstring for why mutating it is both useless and harmful.
        parts = content.split(None, 1)
        rest = f" {parts[1]}" if len(parts) > 1 else ""
        synthetic = _SyntheticMessage(
            content=(f"{prefix}{alias_word}{rest}" if prefix
                     else content[len(alias_word):] or " "),
            author=message.author,
            guild=message.guild,
            channel=message.channel,
            real=message,
        )

        cmd = self._command_for(parent_name, alias_word)
        if cmd is None:
            # Routed correctly, but the command behind the word is not in the
            # tree at this instant. Say so — a silent no-op is how this feature
            # looked "working" for weeks.
            print(f"[ALIASES] `/{parent_name}` not in the command tree while "
                  f"serving alias `{alias_word}`")
            await message.channel.send(
                f"`/{parent_name}` isn't loaded at the moment — try again in a "
                f"second or use the slash command.")
            return

        ctx = await self.bot.get_context(synthetic)
        if ctx.command is None:
            # Normal: aliases are not registered in all_commands. Attach the
            # transient command so invoke() has something to run.
            ctx.command = cmd
            ctx.invoked_with = alias_word
            ctx.prefix = prefix

        try:
            await self.bot.invoke(ctx)
        except Exception as e:  # anything that escaped invoke()'s own handling
            print(f"[ALIASES] error while running `{alias_word}` -> /{parent_name}: "
                  f"{type(e).__name__}: {e}")
            try:
                await message.channel.send(
                    f"Couldn't run `{alias_word}` right now "
                    f"({type(e).__name__}).")
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not message.content:
            return

        router = get_router(self.bot)
        try:
            decision = await router.decide(message)
        except Exception as e:
            # Never let the router's own failure break message handling — but
            # do say something, and stay conservative: on a router error the
            # command systems simply do not run this message.
            print(f"[ALIASES] router error: {type(e).__name__}: {e}")
            return

        if decision.route is not Route.ALIAS:
            return
        await self._invoke_alias(message, decision)

    # ── errors ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Friendly messages for alias invocations (they have no slash UI)."""
        command = ctx.command
        if command is None or not command.extras.get("nero_alias_parent"):
            return

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            # ctx.prefix is the synthetic one this cog invented to build a
            # Context; an alias is typed bare, so show it the way it is typed.
            await ctx.send(
                f"Missing argument: `{error.param.name}`.\n"
                f"Usage: `{command.name} {error.param.name} …`")
        elif isinstance(error, commands.TooManyArguments):
            await ctx.send(f"`{command.name}` got more arguments than it takes.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(f"Member not found: `{error.argument}`")
        elif isinstance(error, commands.UserNotFound):
            await ctx.send(f"User not found: `{error.argument}`")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send(f"Channel not found: `{error.argument}`")
        elif isinstance(error, commands.RoleNotFound):
            await ctx.send(f"Role not found: `{error.argument}`")
        elif isinstance(error, commands.BadUnionArgument):
            await ctx.send(f"Invalid `{error.param.name}`: `{error.argument}`")
        elif isinstance(error, commands.BadBoolArgument):
            await ctx.send("Expected `true` or `false`.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"Invalid argument: {error}")
        elif isinstance(error, AliasGateError):
            # Raised by the gate check. Note that discord.py only wraps
            # *callback* errors in CommandInvokeError (Command.invoke calls
            # prepare() outside the wrapping), so this arrives bare — treating
            # it like a generic CheckFailure here is how "disabled command"
            # answers went missing during development.
            await ctx.send(str(error))
        elif isinstance(error, commands.CheckFailure):
            pass  # the slash-check pass already produced the wording
        else:
            original = getattr(error, "original", error)
            print(f"[ALIASES] error in `{command.name}` -> "
                  f"/{command.extras['nero_alias_parent']}: "
                  f"{type(original).__name__}: {original}")

    # ── periodic sync (dashboard signals changes via bot_settings) ─────

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _alias_sync_check(self):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT value FROM bot_settings "
                    "WHERE key = 'command_aliases_sync_needed'")
                row = await cursor.fetchone()
                if not row or row[0] != "1":
                    return
                # Clear only after the sync actually succeeded: a failure
                # leaves the flag set so the next poll retries.
                await self._sync_aliases()
                await db.execute(
                    "UPDATE bot_settings SET value = '0' "
                    "WHERE key = 'command_aliases_sync_needed'")
                await db.commit()
                print("[ALIASES] re-synced from dashboard change")
        except Exception as e:
            print(f"[ALIASES] sync check failed (will retry): "
                  f"{type(e).__name__}: {e}")

    @_alias_sync_check.before_loop
    async def _before_sync_check(self):
        await self.bot.wait_until_ready()


class _SyntheticMessage:
    """Minimal message-shaped object for ``Bot.get_context``.

    ``Context.__init__`` only reads ``content``, ``author``, ``guild``,
    ``channel`` and ``_state`` off the message (verified against 2.7.1
    ``ext/commands/context.py:182-216``), and ``_parse_arguments`` also looks
    at ``attachments``; everything else — ``ctx.send``, converters that hit the
    API — goes through the *real* state object copied from the triggering
    message, so replies land in the right channel with working rate limits.
    Deliberately not a discord.py ``Message`` subclass: no cache entry, no
    lifecycle, nothing to keep in sync.
    """

    __slots__ = ("content", "author", "guild", "channel", "_state", "id",
                 "type", "attachments", "embeds", "mentions", "edited_at",
                 "created_at", "reference", "message_reference", "webhook_id",
                 "reactions", "flags")

    def __init__(self, *, content, author, guild, channel, real):
        self.content = content
        self.author = author
        self.guild = guild
        self.channel = channel
        self._state = real._state
        self.id = real.id
        self.type = real.type
        self.attachments = real.attachments
        self.embeds = real.embeds
        self.mentions = real.mentions
        self.edited_at = real.edited_at
        self.created_at = real.created_at
        self.reference = real.reference
        self.message_reference = real.message_reference
        self.webhook_id = real.webhook_id
        self.reactions = real.reactions
        self.flags = real.flags


async def setup(bot):
    await bot.add_cog(CommandAliases(bot))
