"""
Command Aliases — functional message-command aliases for slash commands.

When an admin configures aliases for a slash command in the Dashboard
(e.g., alias "k" for /kick), this cog intercepts bare messages starting
with that alias and executes the same logic as the slash command.

Intended syntax:  k @User reason
NOT:              !k @User reason

Architecture:
    - An on_message listener detects bare alias usage
    - The message content is rewritten to add the bot prefix (!) so
      that discord.py's process_commands can parse and invoke it
    - Permissions, cooldowns, and toggle state are checked via the same
      check_command_toggles() function used by NeroCommandTree
    - Slash-level @app_commands.checks.has_permissions are also enforced
      via the PrefixInteraction wrapper's .permissions attribute
    - A periodic task watches for a DB flag set by the dashboard and
      re-syncs aliases when they change
"""

import asyncio
import discord
from discord.ext import commands, tasks
import aiosqlite
import json
import time
from database import DB_PATH


# ── Compatibility Wrapper ─────────────────────────────────────────
# Adapts a discord.ext.commands.Context into the minimal subset of
# discord.Interaction that slash command callbacks actually use.
#
# AUDITED ATTRIBUTES (only these are used in @app_commands.command
# callbacks across all 89 commands):
#   interaction.user          → ctx.author
#   interaction.guild         → ctx.guild
#   interaction.channel       → ctx.channel
#   interaction.channel_id    → ctx.channel.id
#   interaction.permissions   → ctx.channel.permissions_for(ctx.author)
#   interaction.response.send_message() → ctx.send()
#   interaction.response.defer()        → no-op
#   interaction.response.is_done()      → tracks send state
#   interaction.followup.send()         → ctx.send()
#
# interaction.permissions is required by @app_commands.checks.has_permissions
# which accesses interaction.permissions to check Discord-level perms
# (e.g. kick_members, ban_members, administrator).


class _PrefixResponse:
    """Mimics InteractionResponse for prefix command context."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._done = False

    async def send_message(self, content=None, *, embed=None,
                           ephemeral=False):
        await self._ctx.send(content=content, embed=embed)
        self._done = True

    async def defer(self, ephemeral=False):
        # No-op: prefix commands don't have Discord's 3-second
        # interaction timeout. Commands that defer() then
        # followup.send() will just send via followup.send().
        pass

    def is_done(self):
        return self._done


class _PrefixFollowup:
    """Mimics Interaction.followup for prefix command context."""

    def __init__(self, ctx):
        self._ctx = ctx

    async def send(self, content=None, *, embed=None, ephemeral=False):
        await self._ctx.send(content=content, embed=embed)


class PrefixInteraction:
    """Thin adapter: wraps a Context to satisfy the Interaction
    interface that slash command callbacks expect.

    Only the attributes verified by audit are implemented.
    Accessing anything else will raise AttributeError — this is
    intentional: if a future command uses a new Interaction attribute,
    it will fail loudly rather than silently do the wrong thing.
    """

    __slots__ = ('user', 'guild', 'channel', 'channel_id',
                 'response', 'followup', 'permissions')

    def __init__(self, ctx):
        self.user = ctx.author
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.channel_id = ctx.channel.id
        self.response = _PrefixResponse(ctx)
        self.followup = _PrefixFollowup(ctx)
        # Required by @app_commands.checks.has_permissions which reads
        # interaction.permissions to check Discord-level perms.
        # ctx.channel.permissions_for(ctx.author) computes the same
        # channel-resolved permissions that Discord provides via the
        # Interaction payload.
        self.permissions = ctx.channel.permissions_for(ctx.author)


# ── Shared Permission / Cooldown Check ────────────────────────────
# Extracted from NeroCommandTree.interaction_check so both the slash
# command path and the prefix alias path use identical logic.

def _parse_id_set(raw_val) -> set:
    if not raw_val:
        return set()
    if isinstance(raw_val, (list, set)):
        return {int(x) for x in raw_val if str(x).isdigit()}
    try:
        parsed = json.loads(raw_val)
        if isinstance(parsed, list):
            return {int(x) for x in parsed if str(x).isdigit()}
    except Exception:
        pass
    return set()


async def check_command_toggles(
    guild_id: int, cmd_name: str,
    member: discord.Member, channel_id: int,
    cooldowns: dict, now: float,
) -> tuple[bool, str | None]:
    """Check command_toggles for enabled state, permissions, cooldowns.

    Returns (allowed, error_message).
    If allowed is False, error_message is the message to send.
    If allowed is True, error_message is None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT enabled, allowed_roles, allowed_channels, owner_only,
                   cooldown_seconds, bypass_cooldown_roles, error_message,
                   enabled_roles, disabled_roles, enabled_channels,
                   disabled_channels
            FROM command_toggles
            WHERE guild_id = ? AND command_name = ?
        """, (guild_id, cmd_name))
        row = await cursor.fetchone()

    if not row:
        return True, None

    (enabled, allowed_roles, allowed_channels, owner_only,
     cooldown_seconds, bypass_cooldown_roles, error_message,
     enabled_roles, disabled_roles, enabled_channels,
     disabled_channels) = row

    if not enabled:
        msg = (error_message
               or f"`/{cmd_name}` is currently disabled on this server.")
        return False, msg

    if owner_only and member.id != member.guild.owner_id:
        return False, "This command is restricted to the server owner."

    member_role_ids = {r.id for r in member.roles}

    # Role blacklist
    dis_roles = _parse_id_set(disabled_roles)
    if dis_roles and (member_role_ids & dis_roles):
        return False, "You don't have permission to use this command."

    # Role whitelist
    allow_roles = (_parse_id_set(enabled_roles)
                   | _parse_id_set(allowed_roles))
    if allow_roles and not (member_role_ids & allow_roles):
        return False, "You don't have permission to use this command."

    # Channel blacklist
    dis_channels = _parse_id_set(disabled_channels)
    if dis_channels and channel_id in dis_channels:
        return False, "This command can't be used in this channel."

    # Channel whitelist
    allow_channels = (_parse_id_set(enabled_channels)
                      | _parse_id_set(allowed_channels))
    if allow_channels and channel_id not in allow_channels:
        return False, "This command can't be used in this channel."

    # Cooldown — uses the SAME dict as NeroCommandTree.interaction_check
    # (imported from main._command_cooldowns) so /kick and k share one
    # cooldown state.
    if cooldown_seconds and cooldown_seconds > 0:
        bypass_roles = set()
        if bypass_cooldown_roles:
            try:
                bypass_roles = {
                    int(r) for r in json.loads(bypass_cooldown_roles)
                }
            except Exception:
                pass
        if not (member_role_ids & bypass_roles):
            key = (guild_id, member.id, cmd_name)
            last = cooldowns.get(key, 0)
            if now - last < cooldown_seconds:
                remaining = round(cooldown_seconds - (now - last), 1)
                return False, f"Slow down — try again in {remaining}s."
            cooldowns[key] = now

    return True, None


# ── Slash-Level Permission Check ──────────────────────────────────

async def _run_slash_checks(checks_list, interaction) -> tuple[bool, str | None]:
    """Run @app_commands.checks predicates against a PrefixInteraction.

    This enforces Discord-level permission requirements (e.g.
    has_permissions(ban_members=True)) that are separate from the
    dashboard command_toggles system.

    Parameters
    ----------
    checks_list : list
        The list of check predicates from the slash command's .checks
    interaction : PrefixInteraction
        The wrapper providing .permissions and other attributes

    Returns (passed, error_message).
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
        except discord.app_commands.MissingPermissions as e:
            missing = ", ".join(
                p.replace("_", " ").title() for p in e.missing_permissions
            )
            return False, f"You need the following Discord permissions: {missing}"
        except discord.app_commands.CheckFailure as e:
            return False, str(e) or "You don't have permission to use this command."

    return True, None


# ── Alias Cog ─────────────────────────────────────────────────────

class CommandAliases(commands.Cog):
    """Intercepts bare alias messages and rewrites them so
    process_commands can dispatch them as prefix commands."""

    def __init__(self, bot):
        self.bot = bot
        # alias name → parent slash command name
        self._alias_to_parent: dict[str, str] = {}
        # set of all registered alias names (lowercase)
        self._registered: set[str] = set()

    async def cog_load(self):
        await self._sync_aliases()
        if not self._alias_sync_check.is_running():
            self._alias_sync_check.start()

    def cog_unload(self):
        if self._alias_sync_check.is_running():
            self._alias_sync_check.cancel()
        self._alias_to_parent.clear()
        self._registered.clear()

    # ── Sync Logic ────────────────────────────────────────────────

    async def _sync_aliases(self):
        """Read aliases from command_toggles and register prefix
        commands. Removes any previously registered alias commands
        first to prevent stale entries."""

        # Remove old alias commands from bot.all_commands
        for name in list(self._registered):
            self.bot.remove_command(name)
        self._registered.clear()
        self._alias_to_parent.clear()

        # Collect all known slash command names (for conflict check)
        slash_names: set[str] = set()
        for cmd in self.bot.tree.get_commands():
            slash_names.add(cmd.name)

        # Collect all existing prefix command names (sync, reload, etc.)
        existing_prefix: set[str] = set(self.bot.all_commands.keys())

        # Read all aliases from all guilds (aliases are global since
        # prefix commands are global)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT command_name, aliases FROM command_toggles
                WHERE aliases IS NOT NULL
                  AND aliases != '[]'
                  AND aliases != ''
            """)
            rows = await cursor.fetchall()

        # Build alias → parent mapping (deduplicated globally)
        alias_map: dict[str, str] = {}  # alias → parent
        for parent_name, aliases_json in rows:
            try:
                aliases = json.loads(aliases_json)
            except (json.JSONDecodeError, TypeError):
                continue
            for alias in aliases:
                alias = alias.strip().lower()
                if not alias or alias == parent_name:
                    continue
                if alias in alias_map:
                    continue  # first writer wins
                alias_map[alias] = parent_name

        # Register each alias — creates a commands.Command so that
        # process_commands can parse and invoke it after we rewrite
        # the message content.
        registered = 0
        for alias_name, parent_name in alias_map.items():
            # Conflict: existing prefix command
            if alias_name in existing_prefix:
                print(f"[ALIASES] Skip '{alias_name}' → "
                      f"'{parent_name}': conflicts with existing "
                      f"prefix command")
                continue

            # Conflict: already registered in this sync
            if alias_name in self._registered:
                print(f"[ALIASES] Skip '{alias_name}' → "
                      f"'{parent_name}': duplicate alias")
                continue

            # Find the parent slash command
            slash_cmd = self.bot.tree.get_command(parent_name)
            if not slash_cmd:
                print(f"[ALIASES] Skip '{alias_name}': parent "
                      f"'{parent_name}' not found in tree")
                continue

            # Create the prefix command
            try:
                cmd = self._make_alias_cmd(alias_name, parent_name,
                                           slash_cmd)
                self.bot.add_command(cmd)
                self._registered.add(alias_name)
                self._alias_to_parent[alias_name] = parent_name
                existing_prefix.add(alias_name)
                registered += 1
            except Exception as e:
                print(f"[ALIASES] Failed to register '{alias_name}':"
                      f" {e}")

        if registered:
            print(f"[ALIASES] Registered {registered} alias(es)")

    def _make_alias_cmd(self, alias_name: str, parent_name: str,
                        slash_cmd) -> commands.Command:
        """Create a commands.Command that delegates to a slash command.

        The command is registered with bot.add_command() so that
        process_commands can find and invoke it after the on_message
        listener rewrites the bare alias into a prefixed command.
        """
        parent = parent_name  # capture for closure
        # Capture the slash command's checks list at registration time
        # so we can enforce them at invocation time even if the tree
        # is re-synced.
        slash_checks = list(getattr(slash_cmd, 'checks', []) or [])

        async def alias_callback(ctx, **kwargs):
            # Import here to avoid circular import at module level
            from main import _command_cooldowns

            # 1. Dashboard-level checks (enabled/disabled, roles,
            #    channels, cooldowns, owner-only)
            now = time.time()
            allowed, msg = await check_command_toggles(
                guild_id=ctx.guild.id,
                cmd_name=parent,
                member=ctx.author,
                channel_id=ctx.channel.id,
                cooldowns=_command_cooldowns,
                now=now,
            )
            if not allowed:
                if msg:
                    await ctx.send(msg)
                return

            # 2. Create the Interaction wrapper (needed for both
            #    slash-level permission checks and the callback itself)
            interaction = PrefixInteraction(ctx)

            # 3. Discord-level permission checks from
            #    @app_commands.checks.has_permissions decorators
            #    on the parent slash command
            if slash_checks:
                passed, perm_msg = await _run_slash_checks(
                    slash_checks, interaction)
                if not passed:
                    await ctx.send(perm_msg)
                    return

            # 4. Look up the slash command at invocation time (not
            #    registration time) so hot-reloaded commands work.
            sc = self.bot.tree.get_command(parent)
            if not sc:
                await ctx.send(
                    f"Command `/{parent}` not found. "
                    "It may have been removed.")
                return

            # 5. Execute the slash command callback
            await sc.callback(interaction, **kwargs)

        # Create the command — process_commands will invoke it
        cmd = commands.Command(alias_callback, name=alias_name)

        # Override params with the slash command's parameters so
        # discord.ext.commands can parse arguments correctly
        try:
            from discord.ext.commands import Parameter as CmdParameter
            import inspect

            _OPTION_TYPE_MAP = {
                3: str,
                4: int,
                5: bool,
                6: discord.Member,
                7: discord.TextChannel,  # default for channel; overridden below
                8: discord.Role,
                10: float,
                11: discord.Attachment,
            }

            # Channel subtypes — when the slash command parameter has
            # channel_types constraints, use the specific channel class
            # so the ext.commands converter resolves to the right type.
            _CHANNEL_TYPE_MAP = {
                discord.ChannelType.text:    discord.TextChannel,
                discord.ChannelType.voice:   discord.VoiceChannel,
                discord.ChannelType.category: discord.CategoryChannel,
                discord.ChannelType.news:    discord.TextChannel,
                discord.ChannelType.stage_voice: discord.VoiceChannel,
                discord.ChannelType.forum:   discord.TextChannel,
            }

            cmd_params = {}
            for name, param in slash_cmd.params.items():
                opt_type = (param.type.value
                            if hasattr(param.type, 'value')
                            else param.type)
                annotation = _OPTION_TYPE_MAP.get(opt_type, str)

                # For channel parameters, refine the annotation based
                # on the channel_types constraint (e.g. CategoryChannel
                # vs TextChannel). Without this, all channel params
                # would resolve to TextChannel.
                if opt_type == 7:
                    try:
                        ch_types = param.channel_types
                        if ch_types:
                            # Use the first specified channel type
                            annotation = _CHANNEL_TYPE_MAP.get(
                                ch_types[0], discord.TextChannel)
                    except Exception:
                        pass

                if param.required:
                    default = inspect.Parameter.empty
                else:
                    default = param.default
                    if default is inspect.Parameter.empty:
                        default = None

                cmd_params[name] = CmdParameter(
                    name=name,
                    default=default,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=annotation,
                    converter=None,
                    displayed_default=None,
                    description=getattr(param, 'description', '') or '',
                    required=param.required,
                )

            cmd.params = cmd_params
        except Exception:
            pass

        # Set a description for !help
        desc = slash_cmd.description or parent_name
        cmd.help = f"Alias for /{parent_name} — {desc}"
        cmd.brief = f"Alias for /{parent_name}"

        return cmd

    # ── on_message Listener ───────────────────────────────────────
    #
    # This is the core of the bare-alias mechanism. When a user types
    # `k @User` (no prefix), this listener:
    #   1. Detects the bare alias
    #   2. Rewrites message.content to `!k @User`
    #   3. Lets bot.process_commands (which runs after all on_message
    #      listeners) dispatch it as a normal prefix command
    #
    # WHY MUTATION IS THE SAFEST APPROACH:
    #
    # Alternatives considered:
    #   a) Create a fake Message object — fragile, must replicate
    #      dozens of attributes, breaks if discord.py adds new ones
    #   b) Call bot.get_context with a copy — still requires mutating
    #      message.content since get_context reads it
    #   c) Manually parse arguments — reimplements process_commands,
    #      loses all converter/error handling infrastructure
    #   d) Use empty-string prefix — every message becomes a command
    #      attempt, catastrophic performance
    #
    # Mutation is safe here because:
    #   - This cog is loaded LAST — all other on_message listeners
    #     (triggers, customcommands, activity_engine, sticky) have
    #     already seen the original content
    #   - process_commands runs AFTER all on_message listeners
    #   - The message object is ephemeral (not persisted)
    #   - A comment in main.py warns that this cog must stay last
    #
    # This cog MUST be loaded LAST so all other on_message listeners
    # see the original message content before we rewrite it.

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not message.content:
            return

        # Extract the first word (case-insensitive)
        parts = message.content.split()
        if not parts:
            return
        first_word = parts[0].lower()

        # Check if it matches a registered alias
        if first_word not in self._registered:
            return

        # Rewrite message.content to add the bot prefix so that
        # process_commands can find and invoke the command.
        # Safe because this listener runs LAST — all other on_message
        # listeners have already seen the original content.
        message.content = f"!{message.content}"

    # ── Error Handler ─────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Handle errors from alias commands with user-friendly
        messages instead of raw tracebacks."""
        if not ctx.command or ctx.command.name not in self._registered:
            return

        # Unwrap CommandInvokeError
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"Missing argument: `{error.param.name}`.\n"
                f"Usage: `{ctx.prefix}{ctx.command.name} "
                f"[{error.param.name}]`")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(
                f"Member not found: `{error.argument}`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"Invalid argument: {error}")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                "This command can only be used in a server.")
        elif isinstance(error, commands.CheckFailure):
            # alias_callback already sent the message
            pass
        else:
            print(f"[ALIASES] Error in {ctx.prefix}"
                  f"{ctx.command.name}: {error}")

    # ── Periodic Sync Task ────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def _alias_sync_check(self):
        """Check if the dashboard has requested an alias re-sync."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT value FROM bot_settings "
                    "WHERE key = 'command_aliases_sync_needed'"
                )
                row = await cursor.fetchone()

            if row and row[0] == '1':
                # Clear the flag first
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE bot_settings SET value = '0' "
                        "WHERE key = 'command_aliases_sync_needed'"
                    )
                    await db.commit()

                # Re-sync
                await self._sync_aliases()
                print("[ALIASES] Re-synced from dashboard change")
        except Exception as e:
            print(f"[ALIASES] Sync check error: {e}")

    @_alias_sync_check.before_loop
    async def _before_sync_check(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CommandAliases(bot))
