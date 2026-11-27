"""Per-message execution router.

WHY THIS EXISTS
---------------
Several systems in this bot react to the same message: real prefix commands
(discord.py's own ``process_commands``), slash-command *aliases* (bare words
such as ``k`` that run ``/kick``), custom commands (DB-driven ``!warn``-style
rows) and triggers (auto-responses).

discord.py 2.7.1 dispatches every ``@commands.Cog.listener()`` ``on_message``
as a **sibling task** after the client's own handler
(``discord/ext/commands/bot.py:237-242``: ``Bot.dispatch`` calls
``Client.dispatch`` — which schedules ``Bot.on_message`` -> ``process_commands``
— and only then schedules each entry of ``extra_events``). Listeners therefore
run *concurrently* and interleave at whichever ``await`` they happen to hit;
they do not run one-after-another to completion.

That makes the two "obvious" ways of coordinating them both wrong:

  * rewriting ``message.content`` in one listener so a later
    ``process_commands`` sees a prefix — impossible: ``process_commands`` has
    already run, and the mutation leaks into whichever listener is still parked
    on its DB read (measured: ``exact``/``startswith`` triggers silently stop
    matching once the alias cog is loaded, because they receive ``!k`` where
    the user typed ``k``);
  * a "handled" flag set by the first listener so the others stand down — a
    race, because "first" is not deterministic.

THE RULE (single source of truth)
---------------------------------
    PREFIX_COMMAND  >  ALIAS  >  CUSTOM_COMMAND  >  TRIGGER

At most one executor claims a message. The decision is *pure* (no mutation, no
side effects, nothing user-visible) and memoised per message id in one shared
task, so every listener awaits the same computation and therefore agrees on the
same answer regardless of scheduling order.

``Route.TRIGGER`` is deliberately the "nothing above claimed it" bucket: the
trigger engine keeps its own rich matching (contains/startswith/exact/endswith/
fuzzy, response chance, per-trigger cooldown and channel filters) because
duplicating that here would create a second implementation to keep in sync.
Triggers only ask "am I allowed to look at this message at all?".

Aliases are guild-scoped on purpose (see ``cogs/command_aliases.py``): the
previous implementation registered them into ``bot.all_commands``, which is
process-global, so an alias configured in one server worked in every server.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from database import DB_PATH


class Route(str, Enum):
    """Who is allowed to act on a message. Ordered by precedence.

    A str-Enum so members compare equal to their own value (`route == "alias"`
    works) while staying identity-comparable (`route is Route.ALIAS`), and so a
    log line or the dashboard can print the label without a lookup table.
    Declaring it here — instead of each cog deciding for itself — is the whole
    point: there is exactly one precedence order in this program.
    """

    NONE = "none"
    #: a real discord.py prefix command (!sync, !reload, …) — executed by
    #: process_commands, only reported here so the others stand down
    PREFIX_COMMAND = "prefix_command"
    #: bare alias word -> slash command
    ALIAS = "alias"
    #: DB-driven custom command, exact first-token match
    CUSTOM_COMMAND = "custom_command"
    #: no command-like system claimed the message: message-reactive systems
    #: (triggers, and anything else that reads chat text) may run
    TRIGGER = "trigger"


# Custom commands are read with an explicit column list — never ``SELECT *``.
# database.init_db() creates 16 columns while the old cogs/customcommands.py
# unpacked 14, which raised ValueError on every matching message; pinning the
# column list makes the read immune to further schema additions.
CUSTOM_COMMAND_COLUMNS = (
    "id, guild_id, trigger, allowed_roles, actions, embed_title, "
    "embed_description, embed_color, log_channel_id, same_channel, "
    "dm_member, dm_message, requires_mention, requires_reason"
)

#: Index of each column in the tuple returned by CUSTOM_COMMAND_SELECT.
CC = {
    name: i for i, name in enumerate(
        c.strip() for c in CUSTOM_COMMAND_COLUMNS.split(","))
}


def first_token(content: str) -> str:
    """The lowercased first whitespace-delimited token (no prefix stripping)."""
    if not content:
        return ""
    return content.split(maxsplit=1)[0].lower()


def strip_word(word: str) -> str:
    """Trim punctuation a user may wrap around a command word.

    ``k.``, ``(k)``, ```k``` all count as the word ``k``. Only leading/trailing
    characters are trimmed — never inner ones — so ordinary chat such as
    ``pick me up`` cannot collapse into a command word.
    """
    return word.strip("`*_~()[]{}<>\"'.,;:!?—–-")


@dataclass
class Decision:
    """The one allowed executor for a single message."""

    route: Route = Route.NONE
    #: {"alias": <canonical alias word>, "typed": <token the user typed>,
    #:  "parent": <slash command name>}
    alias: Optional[Dict[str, str]] = None
    #: the matched custom_commands row (tuple in CUSTOM_COMMAND_COLUMNS order)
    custom_command: Optional[Tuple[Any, ...]] = None
    #: reserved for extra context the winning system needs
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def claimed(self) -> bool:
        return self.route != Route.NONE


class MessageRouter:
    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {alias_word: parent_command_name} — published by
        # cogs/command_aliases.py on every sync.
        self._alias_table: Dict[int, Dict[str, str]] = {}
        # message_id -> Task[Decision]. Shared so concurrent listeners await one
        # computation instead of racing each other.
        self._decisions: "OrderedDict[int, asyncio.Task]" = OrderedDict()

    #: bounded so a busy bot cannot grow this forever
    CACHE_SIZE = 1024

    # ── alias index (owned by the alias cog, consulted for precedence) ──

    def set_alias_table(self, table: Optional[Dict[int, Dict[str, str]]],
                        *, invalidate: bool = True) -> None:
        self._alias_table = table or {}
        if invalidate:
            # A dashboard save can land between "this message was decided" and
            # "the listener that will answer it runs". Forgetting the memo means
            # the next message sees the new aliases immediately instead of
            # waiting out the cache.
            self.invalidate()

    @property
    def alias_table(self) -> Dict[int, Dict[str, str]]:
        return self._alias_table

    def alias_for(self, guild_id: Optional[int], word: str) -> Optional[str]:
        if guild_id is None or not word:
            return None
        return self._alias_table.get(guild_id, {}).get(word)

    # ── decision ───────────────────────────────────────────────────────

    async def decide(self, message) -> Decision:
        mid = getattr(message, "id", None)
        if mid is None:
            return await self._compute(message)

        task = self._decisions.get(mid)
        if task is None:
            task = self._start(mid, self._compute(message))
        return await asyncio.shield(task)

    def _start(self, message_id: int, coro) -> "asyncio.Task":
        task = asyncio.ensure_future(coro)
        self._decisions[message_id] = task
        self._trim()
        return task

    def _trim(self) -> None:
        """Keep the memo bounded without cancelling anybody's work.

        Only *finished* decisions are eligible for eviction. If everything in
        the cache is still in flight — a burst of more than CACHE_SIZE messages
        at once — the cache grows briefly instead: evicting or cancelling a
        shared decision surfaces as CancelledError inside whichever cog was
        awaiting it, which is a worse failure than a few hundred extra entries.
        """
        if len(self._decisions) <= self.CACHE_SIZE:
            return
        for mid in list(self._decisions.keys()):
            if len(self._decisions) <= self.CACHE_SIZE:
                break
            task = self._decisions[mid]
            if task.done():
                _consume(task)
                self._decisions.pop(mid, None)

    def invalidate(self, message_id: Optional[int] = None) -> None:
        """Forget memoised decisions.

        Only *finished* entries are dropped. A decision still in flight has at
        least one listener awaiting it — cancelling it would raise
        CancelledError inside that listener (and on a cog reload, that listener
        is another system's on_message, not ours), so it is left alone and its
        result simply stops being shared.
        """
        if message_id is None:
            for mid, task in list(self._decisions.items()):
                if task.done():
                    _consume(task)
                    self._decisions.pop(mid, None)
        else:
            task = self._decisions.pop(message_id, None)
            if task is not None and task.done():
                _consume(task)

    async def _compute(self, message) -> Decision:
        decision = Decision()

        if getattr(message.author, "bot", False) or message.guild is None:
            return decision

        content = message.content or ""
        if not content.strip():
            return decision

        # 1. Real prefix command? Ask discord.py's own resolver rather than
        #    reimplementing prefix/mention/callable-prefix handling.
        try:
            ctx = await self.bot.get_context(message)
        except Exception:
            ctx = None
        if ctx is not None and ctx.command is not None:
            decision.route = Route.PREFIX_COMMAND
            return decision

        first = first_token(content)
        bare = strip_word(first)

        # 2. Bare alias (ProBot-style: no prefix needed). A trailing
        #    punctuation mark ("k.") still counts as the word "k"; a leading
        #    "!" never does, because aliases deliberately do not use prefixes.
        guild_id = message.guild.id
        parent = None if first.startswith("!") else (
            self.alias_for(guild_id, first) or self.alias_for(guild_id, bare))
        if parent:
            decision.route = Route.ALIAS
            decision.alias = {
                "alias": bare or first,
                "typed": first,
                "parent": parent,
            }
            return decision

        # 3. Custom command: exact whole-token match on the trigger. Only
        #    worth a DB read for prefixed messages — custom commands have
        #    always been "!trigger" invocations, and this keeps the hot path
        #    (plain chat) free of extra queries, exactly as before.
        if first.startswith("!"):
            cmd = await self.find_custom_command(guild_id, first)
            if cmd is not None:
                decision.route = Route.CUSTOM_COMMAND
                decision.custom_command = cmd
                return decision

        # 4. Free for message-reactive systems.
        decision.route = Route.TRIGGER
        return decision

    # ── shared lookups ─────────────────────────────────────────────────

    async def find_custom_command(self, guild_id: int,
                                  first_word: str) -> Optional[Tuple[Any, ...]]:
        """Enabled custom-command row whose trigger equals ``first_word``.

        The bot prefix is accepted and ignored (``!k`` and ``k`` both match
        trigger ``k``), matching is a whole-token comparison — so trigger ``k``
        cannot swallow ``!kk`` or ``!kick`` — and disabled rows never match.
        """
        token = strip_word((first_word or "").lower()).lstrip("!").strip()
        if not token:
            return None

        rows = await self._custom_command_rows(guild_id)
        for row in rows:
            trigger = strip_word((row[CC["trigger"]] or "").lower())
            if trigger and trigger == token:
                return row
        return None

    async def _custom_command_rows(self, guild_id: int) -> List[Tuple[Any, ...]]:
        base = (f"SELECT {CUSTOM_COMMAND_COLUMNS} FROM custom_commands "
                "WHERE (guild_id = ? OR guild_id = 0)")
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                cursor = await db.execute(base + " AND enabled = 1 ORDER BY id ASC",
                                           (guild_id,))
            except aiosqlite.OperationalError:
                # Databases created before the `enabled` column existed: fall
                # back to "all rows" instead of throwing, and say so once.
                if not getattr(self, "_warned_no_enabled", False):
                    self._warned_no_enabled = True
                    print("[router] custom_commands has no `enabled` column — "
                          "run database.init_db() to migrate; treating all rows "
                          "as enabled")
                cursor = await db.execute(base + " ORDER BY id ASC", (guild_id,))
            rows = list(await cursor.fetchall())

        return rows


def _consume(task: "asyncio.Task") -> None:
    """Retrieve a task's result/exception so asyncio never logs 'never retrieved'."""
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


def get_router(bot) -> MessageRouter:
    router = getattr(bot, "nero_router", None)
    if router is None:
        router = MessageRouter(bot)
        bot.nero_router = router
    return router


__all__ = (
    "MessageRouter",
    "Decision",
    "Route",
    "get_router",
    "first_token",
    "strip_word",
    "CUSTOM_COMMAND_COLUMNS",
    "CC",
)
