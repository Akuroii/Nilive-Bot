"""
Command Aliases — Option R: per-guild message router + synthetic Context

Locked design:
- bare-only aliases (e.g. "k @User", NOT "!k")
- Per-guild alias router/index, no global leak
- Single deterministic decide() authority
- Alias wins conflicts (alias > custom > trigger > prefix)
- Exactly one executor per message via claimed set
- No registration as commands.Command
- No mutation of original Discord message
- Uses synthetic message + get_context/invoke for prefix parents,
  and PrefixInteraction wrapper for slash parents
- Advisory dashboard warnings, not hard-blocking
- Fixes for custom commands: explicit columns, enabled respected, word-boundary

Full runtime path:
Dashboard Alias input → Space → chip UI → Save → Dashboard API → DB → sync flag
→ bot sync/reload → per-guild alias router/index → message detection
→ router.decide() → alias resolution → arg parsing → synthetic message/context
→ bot.get_context()/bot.invoke() or slash callback → permission checks
→ cooldown handling → normal callback/error handling
"""

import asyncio
import discord
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView
import aiosqlite
import json
import time
import inspect
import random
from database import DB_PATH

# ── Compatibility Wrapper for slash callbacks ─────────────────────────

class _PrefixResponse:
    def __init__(self, ctx):
        self._ctx = ctx
        self._done = False

    async def send_message(self, content=None, *, embed=None, ephemeral=False):
        # ephemeral ignored for prefix path — best effort
        await self._ctx.send(content=content, embed=embed)
        self._done = True

    async def defer(self, ephemeral=False):
        pass

    def is_done(self):
        return self._done


class _PrefixFollowup:
    def __init__(self, ctx):
        self._ctx = ctx

    async def send(self, content=None, *, embed=None, ephemeral=False):
        await self._ctx.send(content=content, embed=embed)


class PrefixInteraction:
    __slots__ = ('user', 'guild', 'channel', 'channel_id', 'response', 'followup', 'permissions')

    def __init__(self, ctx: commands.Context):
        self.user = ctx.author
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.channel_id = ctx.channel.id
        self.response = _PrefixResponse(ctx)
        self.followup = _PrefixFollowup(ctx)
        try:
            self.permissions = ctx.channel.permissions_for(ctx.author)
        except Exception:
            self.permissions = ctx.permissions if hasattr(ctx, 'permissions') else None


# ── Synthetic Message (no mutation of original) ───────────────────────

class _SyntheticMessage:
    """
    Minimal Message-like object for get_context / parsing.
    Forwards unknown attrs to original to stay compatible with converters.
    """
    def __init__(self, original: discord.Message, new_content: str):
        self._orig = original
        self.content = new_content
        self.author = original.author
        self.channel = original.channel
        self.guild = original.guild
        self._state = getattr(original, '_state', None)
        self.attachments = getattr(original, 'attachments', [])
        self.mentions = getattr(original, 'mentions', [])
        self.role_mentions = getattr(original, 'role_mentions', [])
        self.channel_mentions = getattr(original, 'channel_mentions', [])
        self.id = original.id
        self.created_at = getattr(original, 'created_at', None)
        self.jump_url = getattr(original, 'jump_url', '')
        # Needed for some converters
        self.embeds = []

    def __getattr__(self, name):
        # Fallback to original for anything we didn't explicitly set
        return getattr(self._orig, name)


# ── Shared helpers ────────────────────────────────────────────────────

def _parse_id_set(raw_val) -> set[int]:
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
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT enabled, allowed_roles, allowed_channels, owner_only,
                   cooldown_seconds, bypass_cooldown_roles, error_message,
                   enabled_roles, disabled_roles, enabled_channels, disabled_channels
            FROM command_toggles
            WHERE guild_id = ? AND command_name = ?
        """, (guild_id, cmd_name))
        row = await cursor.fetchone()

    if not row:
        return True, None

    (enabled, allowed_roles, allowed_channels, owner_only,
     cooldown_seconds, bypass_cooldown_roles, error_message,
     enabled_roles, disabled_roles, enabled_channels, disabled_channels) = row

    if not enabled:
        msg = error_message or f"`/{cmd_name}` is currently disabled on this server."
        return False, msg

    if owner_only and member.id != member.guild.owner_id:
        return False, "This command is restricted to the server owner."

    member_role_ids = {r.id for r in member.roles}

    dis_roles = _parse_id_set(disabled_roles)
    if dis_roles and (member_role_ids & dis_roles):
        return False, "You don't have permission to use this command."

    allow_roles = _parse_id_set(enabled_roles) | _parse_id_set(allowed_roles)
    if allow_roles and not (member_role_ids & allow_roles):
        return False, "You don't have permission to use this command."

    dis_channels = _parse_id_set(disabled_channels)
    if dis_channels and channel_id in dis_channels:
        return False, "This command can't be used in this channel."

    allow_channels = _parse_id_set(enabled_channels) | _parse_id_set(allowed_channels)
    if allow_channels and channel_id not in allow_channels:
        return False, "This command can't be used in this channel."

    if cooldown_seconds and cooldown_seconds > 0:
        bypass_roles = set()
        if bypass_cooldown_roles:
            try:
                bypass_roles = {int(r) for r in json.loads(bypass_cooldown_roles)}
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


async def _run_slash_checks(checks_list, interaction) -> tuple[bool, str | None]:
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
            missing = ", ".join(p.replace("_", " ").title() for p in e.missing_permissions)
            return False, f"You need the following Discord permissions: {missing}"
        except discord.app_commands.CheckFailure as e:
            return False, str(e) or "You don't have permission to use this command."
    return True, None


# ── Trigger matching (copied from triggers.py, kept in sync) ─────────

def _trigger_matches(content: str, trigger_words: str,
                     match_type: str, fuzzy: bool,
                     case_sensitive: bool, fuzzy_threshold: int = 80) -> bool:
    try:
        from thefuzz import fuzz
        FUZZY_AVAILABLE = True
    except ImportError:
        FUZZY_AVAILABLE = False
        fuzz = None

    words = [w.strip() for w in trigger_words.split(",") if w.strip()]
    if not case_sensitive:
        content_check = content.lower()
        words = [w.lower() for w in words]
    else:
        content_check = content

    for word in words:
        if fuzzy and FUZZY_AVAILABLE:
            ratio = fuzz.partial_ratio(word, content_check)
            if ratio >= fuzzy_threshold:
                return True
            continue
        if match_type == "contains":
            if word in content_check:
                return True
        elif match_type == "startswith":
            if content_check.startswith(word):
                return True
        elif match_type == "exact":
            if content_check.strip() == word:
                return True
        elif match_type == "endswith":
            if content_check.endswith(word):
                return True
    return False


# ── Alias Cog — Router ───────────────────────────────────────────────

class CommandAliases(commands.Cog):
    """
    Per-guild alias router with single decide() authority.
    Loaded FIRST so its on_message runs before triggers/customcommands,
    allowing it to claim messages and enforce alias > custom > trigger precedence.
    """

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {alias_lower: parent_command_name}
        self._guild_aliases: dict[int, dict[str, str]] = {}
        # global fallback guild_id 0 -> {alias: parent}
        self._global_aliases: dict[str, str] = {}
        # claimed message ids to prevent double execution
        # Use bot attribute so other cogs can check
        if not hasattr(bot, "_nero_claimed_messages"):
            bot._nero_claimed_messages = {}
        # _nero_claimed_messages: dict[message_id, timestamp]
        # For quick membership test we also keep set view, but dict allows pruning
        self._claimed_prune_threshold = 2000
        self._claimed_max_age = 300  # 5 min

    async def cog_load(self):
        await self._sync_aliases()
        if not self._alias_sync_check.is_running():
            self._alias_sync_check.start()

    def cog_unload(self):
        if self._alias_sync_check.is_running():
            self._alias_sync_check.cancel()

    # ── Claimed helpers ───────────────────────────────────────────────

    def _is_claimed(self, message_id: int) -> bool:
        return message_id in self.bot._nero_claimed_messages

    def _claim(self, message_id: int):
        self.bot._nero_claimed_messages[message_id] = time.time()
        # prune if large
        if len(self.bot._nero_claimed_messages) > self._claimed_prune_threshold:
            now = time.time()
            cutoff = now - self._claimed_max_age
            stale = [mid for mid, ts in self.bot._nero_claimed_messages.items() if ts < cutoff]
            for mid in stale:
                self.bot._nero_claimed_messages.pop(mid, None)

    # ── Sync Logic — per-guild index, no global leak ──────────────────

    async def _sync_aliases(self):
        """
        Build per-guild alias index from command_toggles.
        Guild-specific aliases do NOT leak to other guilds.
        Guild 0 is treated as global fallback, explicitly defined.
        """
        new_guild_aliases: dict[int, dict[str, str]] = {}
        new_global: dict[str, str] = {}

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT guild_id, command_name, aliases
                FROM command_toggles
                WHERE aliases IS NOT NULL
                  AND aliases != '[]'
                  AND aliases != ''
            """)
            rows = await cursor.fetchall()

        for guild_id, command_name, aliases_json in rows:
            try:
                aliases = json.loads(aliases_json)
            except Exception:
                continue
            if not isinstance(aliases, list):
                continue
            for alias in aliases:
                if not isinstance(alias, str):
                    continue
                alias_clean = alias.strip().lower()
                if not alias_clean:
                    continue
                if alias_clean == command_name.lower():
                    continue
                # Validate format: 1-32, alphanumeric + hyphens, single char allowed
                if len(alias_clean) < 1 or len(alias_clean) > 32:
                    continue
                if not alias_clean.replace("-", "").replace("_", "").isalnum():
                    # Allow hyphens/underscores, but must be alnum otherwise
                    # Original validation allowed hyphens only, but be slightly permissive
                    # Still reject if contains spaces or symbols
                    if not all(c.isalnum() or c in "-_" for c in alias_clean):
                        continue
                # Actually enforce original rule: only letters, numbers, hyphens
                # We allow underscore for backwards compat, but dashboard will enforce hyphen
                # For router, accept both to avoid breaking existing data
                if guild_id == 0:
                    if alias_clean not in new_global:
                        new_global[alias_clean] = command_name
                else:
                    if guild_id not in new_guild_aliases:
                        new_guild_aliases[guild_id] = {}
                    if alias_clean not in new_guild_aliases[guild_id]:
                        new_guild_aliases[guild_id][alias_clean] = command_name

        self._guild_aliases = new_guild_aliases
        self._global_aliases = new_global
        total_guild = sum(len(v) for v in new_guild_aliases.values())
        print(f"[ALIASES] Synced {total_guild} guild-specific alias(es) across {len(new_guild_aliases)} guild(s) + {len(new_global)} global")

    # ── Central decide() ──────────────────────────────────────────────
    # Returns decision tuple or None
    # Decision forms:
    # ("alias", parent_name, alias_used, remaining_content)
    # ("custom", row_dict)
    # ("trigger", row_dict)
    # ("prefix", None)  -> let process_commands handle
    # None -> no match

    async def decide(self, message: discord.Message):
        """
        Single deterministic authority for message routing.
        Precedence:
          Bare messages: alias > trigger
          ! messages: prefix (bot.all_commands) > custom (word-boundary) > trigger
        """
        if message.author.bot or not message.guild:
            return None
        content = message.content
        if not content or not content.strip():
            return None
        content_stripped = content.strip()
        guild_id = message.guild.id

        # Build effective alias index for this guild: guild-specific + global fallback
        # Guild-specific wins over global
        effective_aliases = {}
        if guild_id in self._guild_aliases:
            effective_aliases.update(self._guild_aliases[guild_id])
        # Global fallback only if not already present
        for alias, parent in self._global_aliases.items():
            if alias not in effective_aliases:
                effective_aliases[alias] = parent

        # Case: message starts with "!" -> potential prefix/custom/trigger
        if content_stripped.startswith("!"):
            # Extract first word after "!"
            parts = content_stripped.split()
            if not parts:
                return None
            first_token = parts[0]  # e.g. "!k"
            if len(first_token) < 2:
                return None
            first_word = first_token[1:].lower()  # after "!"
            if not first_word:
                return None

            # Prefix command check — if it's a known prefix command, let process_commands handle
            # This includes sync, reload, and any other commands.Bot commands
            if first_word in self.bot.all_commands:
                return ("prefix", None)

            # Custom command check — word-boundary aware
            custom_row = await self._find_custom_command(guild_id, content_stripped)
            if custom_row:
                return ("custom", custom_row)

            # Trigger check for ! messages
            trigger_row = await self._find_trigger(guild_id, content_stripped)
            if trigger_row:
                return ("trigger", trigger_row)

            return None

        else:
            # Bare message
            parts = content_stripped.split()
            if not parts:
                return None
            first_word = parts[0].lower()
            remaining = content_stripped[len(parts[0]):].strip()

            # Alias check
            if first_word in effective_aliases:
                parent = effective_aliases[first_word]
                return ("alias", parent, first_word, remaining)

            # Trigger check for bare messages
            trigger_row = await self._find_trigger(guild_id, content_stripped)
            if trigger_row:
                return ("trigger", trigger_row)

            return None

    # ── Custom command lookup — explicit columns, enabled respected, word-boundary ──

    async def _find_custom_command(self, guild_id: int, content_stripped: str):
        """
        Returns first matching custom command row dict or None.
        Word-boundary: !trigger must be followed by space or end of string.
        """
        content_lower = content_stripped.lower()

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, trigger, allowed_roles, actions,
                       embed_title, embed_description, embed_color,
                       log_channel_id, same_channel, dm_member, dm_message,
                       requires_mention, requires_reason, enabled
                FROM custom_commands
                WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1
                ORDER BY id ASC
            """, (guild_id,))
            rows = await cursor.fetchall()

        for row in rows:
            (cid, gid, trigger, allowed_roles, actions,
             embed_title, embed_desc, embed_color,
             log_channel_id, same_channel, dm_member, dm_message,
             requires_mention, requires_reason, enabled) = row

            if not trigger:
                continue
            trig_lower = trigger.lower().strip()
            if not trig_lower:
                continue

            # Word-boundary matching: exact "!trigger" or "!trigger " prefix
            # This prevents "!k" matching "!kick"
            if content_lower == f"!{trig_lower}":
                return {
                    "id": cid,
                    "guild_id": gid,
                    "trigger": trigger,
                    "allowed_roles": allowed_roles,
                    "actions": actions,
                    "embed_title": embed_title,
                    "embed_description": embed_desc,
                    "embed_color": embed_color,
                    "log_channel_id": log_channel_id,
                    "same_channel": same_channel,
                    "dm_member": dm_member,
                    "dm_message": dm_message,
                    "requires_mention": requires_mention,
                    "requires_reason": requires_reason,
                }
            if content_lower.startswith(f"!{trig_lower} "):
                return {
                    "id": cid,
                    "guild_id": gid,
                    "trigger": trigger,
                    "allowed_roles": allowed_roles,
                    "actions": actions,
                    "embed_title": embed_title,
                    "embed_description": embed_desc,
                    "embed_color": embed_color,
                    "log_channel_id": log_channel_id,
                    "same_channel": same_channel,
                    "dm_member": dm_member,
                    "dm_message": dm_message,
                    "requires_mention": requires_mention,
                    "requires_reason": requires_reason,
                }

        return None

    # ── Trigger lookup ────────────────────────────────────────────────

    async def _find_trigger(self, guild_id: int, content: str):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, trigger_words, response_text,
                       response_embed, response_type, match_type,
                       fuzzy_match, fuzzy_threshold, case_sensitive,
                       response_chance, cooldown_seconds,
                       allowed_channels, enabled
                FROM triggers
                WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1
                ORDER BY id ASC
            """, (guild_id,))
            rows = await cursor.fetchall()

        for row in rows:
            (tid, gid, trigger_words, response_text,
             response_embed, response_type, match_type,
             fuzzy_match, fuzzy_threshold, case_sensitive,
             response_chance, cooldown_seconds,
             allowed_channels, enabled) = row

            if not trigger_words:
                continue

            if _trigger_matches(
                content, trigger_words,
                match_type or "contains",
                bool(fuzzy_match),
                bool(case_sensitive),
                int(fuzzy_threshold) if fuzzy_threshold else 80,
            ):
                return {
                    "id": tid,
                    "guild_id": gid,
                    "trigger_words": trigger_words,
                    "response_text": response_text,
                    "response_embed": response_embed,
                    "response_type": response_type,
                    "match_type": match_type,
                    "fuzzy_match": fuzzy_match,
                    "fuzzy_threshold": fuzzy_threshold,
                    "case_sensitive": case_sensitive,
                    "response_chance": response_chance,
                    "cooldown_seconds": cooldown_seconds,
                    "allowed_channels": allowed_channels,
                    "enabled": enabled,
                }
        return None

    # ── Alias execution — synthetic context, no mutation ──────────────

    async def _handle_alias(self, message: discord.Message, parent_name: str, alias_used: str, remaining: str):
        # Claim immediately to prevent other executors
        self._claim(message.id)

        # Check if parent is a prefix command (in all_commands)
        if parent_name.lower() in self.bot.all_commands or parent_name in self.bot.all_commands:
            # Use synthetic message + get_context + invoke for prefix commands
            synthetic_content = f"!{parent_name} {remaining}".strip()
            synthetic = _SyntheticMessage(message, synthetic_content)
            try:
                ctx = await self.bot.get_context(synthetic)
                if ctx.valid:
                    await self.bot.invoke(ctx)
                else:
                    # If not valid, try with original case
                    # Maybe parent_name case differs
                    pass
            except Exception as e:
                print(f"[ALIASES] Error invoking prefix parent {parent_name}: {e}")
            return

        # Slash command path
        slash_cmd = self.bot.tree.get_command(parent_name)
        if not slash_cmd:
            # Try case-insensitive search
            for cmd in self.bot.tree.get_commands():
                if cmd.name.lower() == parent_name.lower():
                    slash_cmd = cmd
                    break
        if not slash_cmd:
            await message.channel.send(f"Command `/{parent_name}` not found. It may have been removed.")
            return

        # Permission / toggle checks
        try:
            from main import _command_cooldowns
        except ImportError:
            _command_cooldowns = {}

        now = time.time()
        allowed, err_msg = await check_command_toggles(
            guild_id=message.guild.id,
            cmd_name=parent_name,
            member=message.author,
            channel_id=message.channel.id,
            cooldowns=_command_cooldowns,
            now=now,
        )
        if not allowed:
            if err_msg:
                await message.channel.send(err_msg)
            return

        # Build temp command for arg parsing
        temp_cmd = self._build_temp_command(alias_used, slash_cmd)

        # Synthetic message for parsing: "!alias remaining"
        synthetic_content = f"!{alias_used} {remaining}".strip()
        synthetic = _SyntheticMessage(message, synthetic_content)

        # Prepare view for parsing — skip "!alias"
        view = StringView(synthetic_content)
        # Skip "!" prefix
        if not view.skip_string("!"):
            # Should not happen
            pass
        view.skip_ws()
        # Skip alias word
        view.get_word()
        view.skip_ws()

        ctx = commands.Context(
            message=synthetic,
            bot=self.bot,
            view=view,
            prefix="!",
            command=temp_cmd,
            invoked_with=alias_used,
        )
        # For converters that need args/kwargs
        ctx.args = []
        ctx.kwargs = {}

        # Parse arguments using temp command's parser
        try:
            await temp_cmd._parse_arguments(ctx)
            # ctx.kwargs now holds parsed args matching slash param names
            # ctx.args[0] is ctx itself, rest are positional — but we used POSITIONAL_OR_KEYWORD so kwargs
            parsed_kwargs = ctx.kwargs
            # Also handle positional args that might have been put in args
            # Our temp command's params are all POSITIONAL_OR_KEYWORD, so they go to args after ctx
            # Let's map positional args to param names if needed
            if ctx.args:
                # ctx.args[0] is ctx, rest are values
                # But _parse_arguments puts them in args list, not kwargs for POSITIONAL_OR_KEYWORD
                # We need to handle both
                # Actually for POSITIONAL_OR_KEYWORD, it appends to args, not kwargs
                # So we need to convert args to kwargs based on param order
                param_names = list(temp_cmd.params.keys())
                # ctx.args includes ctx at position 0, then values
                arg_values = ctx.args[1:] if len(ctx.args) > 0 else []
                for i, val in enumerate(arg_values):
                    if i < len(param_names):
                        name = param_names[i]
                        if name not in parsed_kwargs:
                            parsed_kwargs[name] = val
        except commands.MissingRequiredArgument as e:
            await message.channel.send(f"Missing argument: `{e.param.name}`. Usage: `{alias_used} [{e.param.name}]`")
            return
        except commands.MemberNotFound as e:
            await message.channel.send(f"Member not found: `{e.argument}`")
            return
        except commands.BadArgument as e:
            await message.channel.send(f"Invalid argument: {e}")
            return
        except Exception as e:
            print(f"[ALIASES] Parse error for alias {alias_used}: {e}")
            await message.channel.send(f"Error parsing arguments: {e}")
            return

        # Slash checks (has_permissions etc.)
        slash_checks = list(getattr(slash_cmd, 'checks', []) or [])
        interaction = PrefixInteraction(ctx)
        if slash_checks:
            passed, perm_msg = await _run_slash_checks(slash_checks, interaction)
            if not passed:
                await message.channel.send(perm_msg)
                return

        # Invoke slash callback
        try:
            await slash_cmd.callback(interaction, **parsed_kwargs)
        except Exception as e:
            print(f"[ALIASES] Error executing /{parent_name} via alias {alias_used}: {e}")
            # Try to send error if possible
            try:
                if not interaction.response.is_done():
                    await message.channel.send(f"Error executing command: {e}")
            except Exception:
                pass

    def _build_temp_command(self, alias_name: str, slash_cmd):
        from discord.ext.commands import Parameter as CmdParameter

        _OPTION_TYPE_MAP = {
            3: str,
            4: int,
            5: bool,
            6: discord.Member,
            7: discord.TextChannel,
            8: discord.Role,
            10: float,
            11: discord.Attachment,
        }

        _CHANNEL_TYPE_MAP = {
            discord.ChannelType.text: discord.TextChannel,
            discord.ChannelType.voice: discord.VoiceChannel,
            discord.ChannelType.category: discord.CategoryChannel,
            discord.ChannelType.news: discord.TextChannel,
            discord.ChannelType.stage_voice: discord.VoiceChannel,
            discord.ChannelType.forum: discord.TextChannel,
        }

        cmd_params = {}
        for name, param in slash_cmd.params.items():
            opt_type = param.type.value if hasattr(param.type, 'value') else param.type
            annotation = _OPTION_TYPE_MAP.get(opt_type, str)

            if opt_type == 7:
                try:
                    ch_types = getattr(param, 'channel_types', None)
                    if ch_types:
                        annotation = _CHANNEL_TYPE_MAP.get(ch_types[0], discord.TextChannel)
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

        async def dummy_callback(ctx, **kwargs):
            pass

        cmd = commands.Command(dummy_callback, name=alias_name)
        cmd.params = cmd_params
        return cmd

    # ── Custom command execution (fixed) ──────────────────────────────

    async def _handle_custom(self, message: discord.Message, row: dict):
        self._claim(message.id)

        trigger = row["trigger"]
        allowed_roles_raw = row["allowed_roles"]
        actions_raw = row["actions"]
        embed_title = row["embed_title"]
        embed_desc = row["embed_description"]
        embed_color = row["embed_color"]
        log_channel_id = row["log_channel_id"]
        same_channel = row["same_channel"]
        dm_member = row["dm_member"]
        dm_message = row["dm_message"]
        requires_mention = row["requires_mention"]
        requires_reason = row["requires_reason"]

        # Permission check
        allowed = []
        try:
            allowed = json.loads(allowed_roles_raw) if allowed_roles_raw else []
        except Exception:
            allowed = []

        if allowed:
            member_role_ids = [r.id for r in message.author.roles]
            if not any(int(r) in member_role_ids for r in allowed):
                await message.channel.send(f"{message.author.mention} You don't have permission.", delete_after=5)
                return

        parts = message.content.split()
        target_member = None
        reason = "No reason provided"

        if requires_mention:
            if not message.mentions:
                await message.channel.send(f"Usage: `!{trigger} @member reason`", delete_after=5)
                return
            target_member = message.mentions[0]
            reason_parts = parts[2:] if len(parts) > 2 else []
            reason = " ".join(reason_parts) if reason_parts else "No reason provided"
        else:
            reason_parts = parts[1:] if len(parts) > 1 else []
            reason = " ".join(reason_parts) if reason_parts else "No reason provided"

        # Execute actions (same as customcommands.py but with explicit handling)
        try:
            action_list = json.loads(actions_raw) if actions_raw else []
        except Exception:
            action_list = []

        action_errors = []
        from utils.permissions import can_moderate, check_bot_role_position

        destructive = {"ban", "kick", "remove_all_roles", "warn"}
        is_destructive = bool(destructive & set(action_list)) or any(a.startswith("timeout:") for a in action_list)
        if target_member and is_destructive:
            allowed_mod, hmsg = await can_moderate(message.author, target_member, message.guild.id)
            if not allowed_mod:
                await message.channel.send(f"{message.author.mention} {hmsg}", delete_after=6)
                return

        for action in action_list:
            try:
                if action == "ban" and target_member:
                    await target_member.ban(reason=reason)
                elif action == "kick" and target_member:
                    await target_member.kick(reason=reason)
                elif action == "warn" and target_member:
                    async with aiosqlite.connect(DB_PATH) as db:
                        from datetime import datetime
                        await db.execute("""
                            INSERT INTO warnings
                                (guild_id, user_id, moderator_id, reason, timestamp,
                                 user_display_name, moderator_display_name)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (
                            message.guild.id,
                            target_member.id,
                            message.author.id,
                            reason,
                            datetime.utcnow().isoformat(),
                            target_member.display_name,
                            message.author.display_name,
                        ))
                        await db.commit()
                elif action.startswith("timeout:") and target_member:
                    from datetime import timedelta
                    minutes = int(action.split(":")[1])
                    await target_member.timeout(timedelta(minutes=minutes), reason=reason)
                elif action == "remove_all_roles" and target_member:
                    roles_to_remove = [r for r in target_member.roles if r != message.guild.default_role and r.is_assignable()]
                    if roles_to_remove:
                        await target_member.remove_roles(*roles_to_remove)
                elif action.startswith("add_role:") and target_member:
                    role_id = int(action.split(":")[1])
                    role = message.guild.get_role(role_id)
                    if role:
                        can_assign, warn = check_bot_role_position(message.guild, role)
                        actor_ok = message.author.id == message.guild.owner_id or message.author.top_role.position > role.position
                        if not actor_ok:
                            action_errors.append(f"You don't have permission to grant @{role.name} (it's at or above your highest role).")
                        elif can_assign:
                            await target_member.add_roles(role)
                        else:
                            action_errors.append(warn)
                elif action.startswith("remove_role:") and target_member:
                    role_id = int(action.split(":")[1])
                    role = message.guild.get_role(role_id)
                    if role:
                        actor_ok = message.author.id == message.guild.owner_id or message.author.top_role.position > role.position
                        if not actor_ok:
                            action_errors.append(f"You don't have permission to remove @{role.name} (it's at or above your highest role).")
                        else:
                            await target_member.remove_roles(role)
                elif action == "delete_message":
                    try:
                        await message.delete()
                    except Exception:
                        pass
            except Exception as e:
                action_errors.append(str(e))

        try:
            color_int = int(embed_color.strip("#"), 16) if embed_color else 0xED4245
        except Exception:
            color_int = 0xED4245

        embed = discord.Embed(color=color_int)
        if embed_title:
            title = embed_title
            if target_member:
                title = title.replace("{target}", target_member.display_name)
            title = title.replace("{moderator}", message.author.display_name)
            title = title.replace("{reason}", reason)
            embed.title = title

        if embed_desc:
            desc = embed_desc
            if target_member:
                desc = desc.replace("{target}", target_member.mention)
                desc = desc.replace("{target_name}", target_member.display_name)
            desc = desc.replace("{moderator}", message.author.mention)
            desc = desc.replace("{reason}", reason)
            embed.description = desc

        if target_member:
            embed.add_field(name="Member", value=target_member.mention)
        embed.add_field(name="Moderator", value=message.author.mention)
        embed.add_field(name="Reason", value=reason)

        if action_errors:
            embed.add_field(name="Errors", value="\n".join(action_errors), inline=False)

        if same_channel:
            await message.channel.send(embed=embed)

        if log_channel_id:
            log_ch = message.guild.get_channel(int(log_channel_id))
            if log_ch:
                await log_ch.send(embed=embed)

        if dm_member and target_member and dm_message:
            try:
                dm_text = dm_message
                dm_text = dm_text.replace("{server}", message.guild.name)
                dm_text = dm_text.replace("{reason}", reason)
                dm_text = dm_text.replace("{moderator}", message.author.display_name)
                await target_member.send(dm_text)
            except Exception:
                pass

    # ── Trigger execution ─────────────────────────────────────────────

    async def _handle_trigger(self, message: discord.Message, row: dict):
        self._claim(message.id)

        tid = row["id"]
        trigger_words = row["trigger_words"]
        response_text = row["response_text"]
        response_embed = row["response_embed"]
        response_type = row["response_type"]
        match_type = row["match_type"]
        fuzzy_match = row["fuzzy_match"]
        fuzzy_threshold = row["fuzzy_threshold"]
        case_sensitive = row["case_sensitive"]
        response_chance = row["response_chance"]
        cooldown_seconds = row["cooldown_seconds"]
        allowed_channels_raw = row["allowed_channels"]

        # Channel filter
        if allowed_channels_raw:
            try:
                allowed = json.loads(allowed_channels_raw)
                if allowed and message.channel.id not in [int(c) for c in allowed]:
                    return
            except Exception:
                pass

        # Cooldown check (per guild, per trigger)
        # Use the triggers cog's cooldown tracker if available, else local
        # We'll use a local dict here to avoid coupling
        # But we need to respect cooldown — we have self._trigger_last_fired
        now = time.time()
        if not hasattr(self, "_trigger_last_fired"):
            self._trigger_last_fired = {}
        cooldown_key = (message.guild.id, tid)
        cooldown = int(cooldown_seconds) if cooldown_seconds else 0
        if cooldown > 0:
            last = self._trigger_last_fired.get(cooldown_key, 0)
            if now - last < cooldown:
                return

        # Chance check
        chance = int(response_chance) if response_chance else 100
        if chance < 100 and random.randint(1, 100) > chance:
            return

        # Send response
        try:
            if response_type == "embed" and response_embed:
                try:
                    embed_data = json.loads(response_embed)
                except Exception:
                    embed_data = {}
                color_str = embed_data.get("color", "#5865F2")
                try:
                    color_int = int(color_str.strip("#"), 16)
                except Exception:
                    color_int = 0x5865F2
                embed = discord.Embed(color=color_int)
                if embed_data.get("title"):
                    embed.title = embed_data["title"]
                if embed_data.get("description"):
                    embed.description = embed_data["description"]
                if embed_data.get("footer"):
                    embed.set_footer(text=embed_data["footer"])
                if embed_data.get("image"):
                    embed.set_image(url=embed_data["image"])
                await message.channel.send(embed=embed)

            elif response_type == "reply" and response_text:
                await message.reply(response_text, mention_author=False)

            elif response_type == "react" and response_text:
                await message.add_reaction(response_text.strip())

            elif response_text:
                await message.channel.send(response_text)

            if cooldown > 0:
                self._trigger_last_fired[cooldown_key] = now

        except Exception as e:
            print(f"[router/triggers] Error responding to trigger {tid}: {e}")

    # ── on_message — router entry point, runs FIRST ───────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot and DMs
        if message.author.bot or not message.guild:
            return
        if not message.content:
            return

        # If already claimed by router (shouldn't happen, but safety)
        if self._is_claimed(message.id):
            return

        try:
            decision = await self.decide(message)
        except Exception as e:
            print(f"[ALIASES] decide() error: {e}")
            return

        if not decision:
            return

        dtype = decision[0]

        if dtype == "prefix":
            # Let process_commands handle
            return

        if dtype == "alias":
            _, parent_name, alias_used, remaining = decision
            try:
                await self._handle_alias(message, parent_name, alias_used, remaining)
            except Exception as e:
                print(f"[ALIASES] _handle_alias error: {e}")

        elif dtype == "custom":
            _, row = decision
            try:
                await self._handle_custom(message, row)
            except Exception as e:
                print(f"[ALIASES] _handle_custom error: {e}")

        elif dtype == "trigger":
            _, row = decision
            try:
                await self._handle_trigger(message, row)
            except Exception as e:
                print(f"[ALIASES] _handle_trigger error: {e}")

    # ── Periodic sync check ───────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def _alias_sync_check(self):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("SELECT value FROM bot_settings WHERE key = 'command_aliases_sync_needed'")
                row = await cursor.fetchone()

            if row and row[0] == '1':
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE bot_settings SET value = '0' WHERE key = 'command_aliases_sync_needed'")
                    await db.commit()
                await self._sync_aliases()
                print("[ALIASES] Re-synced from dashboard change")
        except Exception as e:
            print(f"[ALIASES] Sync check error: {e}")

    @_alias_sync_check.before_loop
    async def _before_sync_check(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CommandAliases(bot))
