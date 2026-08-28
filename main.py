import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
import time
import sys
import traceback
from dotenv import load_dotenv

load_dotenv()

# ORDERING FIX: database.py reads OWNER_ID / DATABASE_PATH /
# NERO_ENVIRONMENT at import time (module-level, not inside a
# function) — importing it before load_dotenv() has run meant a value
# only set in a local .env file (not a real process env var) was
# silently invisible to it. Moved below load_dotenv() so both sources
# work the same way.
import aiosqlite
from database import DB_PATH, add_guild_owner

print("Starting Nero bot...")


def validate_discord_token() -> str:
    """
    CRITICAL DEBUG FIX: previously the bot called bot.start(os.getenv(...))
    directly with no validation. If DISCORD_TOKEN was missing or blank on
    Railway (unset env var, typo in var name, empty string), discord.py's
    error came back as a generic/opaque failure deep in the connection
    stack with no indication the token itself was the problem — the
    Railway logs just showed a crash with no actionable next step.

    This performs a cheap, local sanity check (present, non-empty, roughly
    the right shape) BEFORE attempting any network connection, and exits
    with a clear, actionable log line if it fails. This does NOT call
    Discord's API to verify the token is valid/unrevoked — that still
    surfaces as a normal discord.py LoginFailure on bot.start(), which is
    already a clear error. This only catches the "token isn't even set"
    class of failure, which was previously the most opaque one.
    """
    token = os.getenv("DISCORD_TOKEN")

    if not token or not token.strip():
        print("=" * 60)
        print("FATAL: DISCORD_TOKEN is missing or empty.")
        print("  -> Check Railway > your service > Variables tab.")
        print("  -> Variable name must be exactly: DISCORD_TOKEN")
        print("  -> See DEBUG_GUIDE.md for full steps.")
        print("=" * 60)
        sys.exit(1)

    token = token.strip()
    # Discord bot tokens are three dot-separated base64-ish segments and
    # are normally 59+ chars. This is a shape check only, not a validity
    # check — it catches "pasted the wrong thing" (e.g. client secret,
    # truncated token, stray quotes) without calling Discord's API.
    if token.count(".") != 2 or len(token) < 50:
        print("=" * 60)
        print("WARNING: DISCORD_TOKEN is set but doesn't look like a "
              "valid Discord bot token (unexpected length/format).")
        print("  -> Double check you copied the BOT token, not the "
              "client secret, and that no quotes/whitespace got")
        print("     included when pasting into Railway.")
        print("  -> Continuing anyway — Discord will reject it on "
              "connect if it's actually invalid.")
        print("=" * 60)
    else:
        print(f"Token check: OK (length={len(token)}, format looks valid)")

    return token


DISCORD_TOKEN = validate_discord_token()

intents = discord.Intents.all()
print(
    "Intents status: message_content="
    f"{intents.message_content}, members={intents.members}, "
    f"presences={intents.presences} (all others enabled via Intents.all())"
)

_command_cooldowns: dict[tuple, float] = {}

# MEMORY LEAK FIX (dark-fixes pass #11): _command_cooldowns is a
# module-level dict written every time a rate-limited slash command
# is used (see NeroCommandTree.interaction_check below), and nothing
# ever removed old entries — one permanent
# (guild_id, user_id, command_name) entry accumulated forever for
# every distinct combination that ever hit a cooldown-gated command,
# for the lifetime of the process. Flagged as a low-priority slow
# leak across several prior passes and deliberately left alone each
# time. Cleared now the same way as the equivalent fix in
# cogs/economy.py and cogs/triggers.py this pass: prune
# opportunistically once the dict has grown large, dropping anything
# stale enough that no realistically-configured cooldown could still
# be watching it (cooldown_seconds is dashboard-configured in
# seconds/minutes via the Commands page, never days).
_COOLDOWN_PRUNE_THRESHOLD = 5000
_COOLDOWN_MAX_AGE_SECONDS = 24 * 60 * 60


def _prune_command_cooldowns(now: float) -> None:
    if len(_command_cooldowns) < _COOLDOWN_PRUNE_THRESHOLD:
        return
    cutoff = now - _COOLDOWN_MAX_AGE_SECONDS
    stale = [k for k, ts in _command_cooldowns.items() if ts < cutoff]
    for k in stale:
        del _command_cooldowns[k]


class NeroCommandTree(discord.app_commands.CommandTree):
    """
    CRASH FIX: the bot was wired up via `@bot.tree.check`, but
    discord.py's `CommandTree` has no `.check()` decorator (that only
    exists on `commands.Bot` for prefix commands) — this raised
    `AttributeError: 'CommandTree' object has no attribute 'check'`
    at import time, before the bot ever attempted to log in. The
    dashboard process is separate (start.sh runs it after the bot,
    regardless of whether the bot crashed), which is why the
    dashboard looked fully healthy while the bot itself never came
    online. The correct hook for a tree-wide app-command check is
    overriding `interaction_check` on a `CommandTree` subclass and
    passing it to `commands.Bot(tree_cls=...)`. All gating logic
    below (per-guild enable/disable, owner-only, role/channel
    restrictions, cooldowns) is unchanged from the old function.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.command is None:
            return True

        cmd_name = interaction.command.qualified_name

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT enabled, allowed_roles, allowed_channels, owner_only,
                       cooldown_seconds, bypass_cooldown_roles, error_message,
                       enabled_roles, disabled_roles, enabled_channels, disabled_channels
                FROM command_toggles
                WHERE guild_id = ? AND command_name = ?
            """, (interaction.guild.id, cmd_name))
            row = await cursor.fetchone()

        if not row:
            return True

        (enabled, allowed_roles, allowed_channels, owner_only,
         cooldown_seconds, bypass_cooldown_roles, error_message,
         enabled_roles, disabled_roles, enabled_channels, disabled_channels) = row

        if not enabled:
            msg = error_message or f"`/{cmd_name}` is currently disabled on this server."
            await interaction.response.send_message(msg, ephemeral=True)
            return False

        if owner_only and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "This command is restricted to the server owner.", ephemeral=True)
            return False

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

        member_role_ids = {r.id for r in interaction.user.roles}

        # Role Blacklist: if member has ANY disabled role, block
        dis_roles = _parse_id_set(disabled_roles)
        if dis_roles and (member_role_ids & dis_roles):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True)
            return False

        # Role Whitelist: enabled_roles or legacy allowed_roles
        allow_roles = _parse_id_set(enabled_roles) | _parse_id_set(allowed_roles)
        if allow_roles and not (member_role_ids & allow_roles):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True)
            return False

        # Channel Blacklist: if invoked in disabled channel, block
        dis_channels = _parse_id_set(disabled_channels)
        if dis_channels and interaction.channel_id in dis_channels:
            await interaction.response.send_message(
                "This command can't be used in this channel.", ephemeral=True)
            return False

        # Channel Whitelist: enabled_channels or legacy allowed_channels
        allow_channels = _parse_id_set(enabled_channels) | _parse_id_set(allowed_channels)
        if allow_channels and interaction.channel_id not in allow_channels:
            await interaction.response.send_message(
                "This command can't be used in this channel.", ephemeral=True)
            return False

        if cooldown_seconds and cooldown_seconds > 0:
            bypass_roles = set()
            if bypass_cooldown_roles:
                try:
                    bypass_roles = {int(r) for r in json.loads(bypass_cooldown_roles)}
                except Exception:
                    pass
            member_role_ids = {r.id for r in interaction.user.roles}
            if not (member_role_ids & bypass_roles):
                key = (interaction.guild.id, interaction.user.id, cmd_name)
                now = time.time()
                last = _command_cooldowns.get(key, 0)
                if now - last < cooldown_seconds:
                    remaining = round(cooldown_seconds - (now - last), 1)
                    await interaction.response.send_message(
                        f"Slow down — try again in {remaining}s.", ephemeral=True)
                    return False
                _command_cooldowns[key] = now
                _prune_command_cooldowns(now)

        return True


bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=NeroCommandTree, case_insensitive=True)

_status_index = 0

@tasks.loop(minutes=5)
async def rotate_status():
    global _status_index
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT text, type FROM status_messages
            WHERE enabled = 1
            ORDER BY position ASC
        """)
        messages = await cursor.fetchall()
    if not messages:
        return
    _status_index = _status_index % len(messages)
    text, status_type = messages[_status_index]
    _status_index += 1
    type_map = {
        "playing":   discord.ActivityType.playing,
        "watching":  discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }
    activity_type = type_map.get(status_type, discord.ActivityType.playing)
    await bot.change_presence(
        activity=discord.Activity(type=activity_type, name=text))


async def load_cogs():
    cog_files = [
        "cogs.activity_engine",
        "cogs.mvp",
        "cogs.moderation",
        "cogs.leveling",
        "cogs.economy",
        "cogs.trade",
        "cogs.missions",
        "cogs.boost",
        "cogs.botprofile",
        "cogs.reactionroles",
        "cogs.tickets",
        "cogs.embedbuilder",
        "cogs.sticky",
        "cogs.youtube",
        "cogs.triggers",
        "cogs.customcommands",
        "cogs.welcome",
        "cogs.shop",
        "cogs.auditlog",
        "cogs.twitch",
        "cogs.events",
        "cogs.minigames",
        "cogs.report",
        "cogs.health",
        "cogs.backup",
        "cogs.scheduler",
        "cogs.tagmissions",
        "cogs.tagpartners",
        # MUST be last: its on_message listener rewrites bare alias
        # messages (e.g. "k @User" → "!k @User") so that
        # process_commands can dispatch them. All other on_message
        # listeners must see the original content first.
        "cogs.command_aliases",
    ]
    bot.loaded_cogs = []
    bot.failed_cogs = []
    for cog in cog_files:
        try:
            await bot.load_extension(cog)
            print(f"  Loaded {cog}")
            bot.loaded_cogs.append(cog)
        except Exception as e:
            print(f"  Failed to load {cog}: {e}")
            traceback.print_exc()
            bot.failed_cogs.append({"cog": cog, "error": str(e)})


@bot.event
async def on_ready():
    print(f"Nero is online as {bot.user}")
    try:
        await bot.tree.sync()
        synced = await bot.tree.fetch_commands()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Sync error: {e}")
        traceback.print_exc()
    await backfill_guild_owners()
    rotate_status.start()


@bot.event
async def on_error(event_method, *args, **kwargs):
    tb = traceback.format_exc()
    print(f"[ON_ERROR] in {event_method}:\n{tb}")
    try:
        from cogs.health import record_error
        await record_error(f"event:{event_method}", tb)
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction,
                                error: discord.app_commands.AppCommandError):
    if isinstance(error, (discord.app_commands.MissingPermissions,
                           discord.app_commands.CheckFailure)):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True)
        return

    tb = "".join(traceback.format_exception(
        type(error), error, error.__traceback__))
    print(f"[APP_COMMAND_ERROR] {interaction.command}:\n{tb}")
    try:
        from cogs.health import record_error
        cmd_name = interaction.command.qualified_name if interaction.command else "unknown"
        await record_error(f"command:/{cmd_name}", tb)
    except Exception:
        pass

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong running that command. "
                "The error has been logged.", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"Joined new guild: {guild.name} ({guild.id})")
    await add_guild_owner(guild.id, guild.owner_id)


async def backfill_guild_owners():
    """
    Grants each guild's REAL Discord owner (guild.owner_id) a
    dashboard_users 'owner' row, for every guild the bot is ALREADY
    in — on_guild_join above only covers guilds joined after this
    deploy. Called from on_ready, which re-fires on every reconnect;
    safe to re-run every time since add_guild_owner()'s INSERT OR
    IGNORE is idempotent against idx_du_unique(guild_id, user_id).
    """
    for guild in bot.guilds:
        if guild.owner_id:
            await add_guild_owner(guild.id, guild.owner_id)
    print(f"✅ Backfilled real owner access for {len(bot.guilds)} guilds")


@bot.command()
@commands.is_owner()
async def sync(ctx):
    await bot.tree.sync()
    cmds = await bot.tree.fetch_commands()
    await ctx.send(f"Synced {len(cmds)} commands!")


@bot.command()
@commands.is_owner()
async def reload(ctx, cog: str):
    try:
        await bot.reload_extension(f"cogs.{cog}")
        await ctx.send(f"Reloaded cogs.{cog}")
    except Exception as e:
        await ctx.send(f"Failed: {e}")


async def main():
    try:
        print("Initializing database...")
        from database import init_db
        await init_db()
        print("Loading cogs...")
        await load_cogs()
        print("Attempting Discord connection...")
        await bot.start(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("=" * 60)
        print("FATAL: Discord rejected DISCORD_TOKEN (LoginFailure).")
        print("  -> The token is set but Discord says it's invalid.")
        print("  -> It may have been regenerated/revoked in the")
        print("     Developer Portal — copy a fresh token and update")
        print("     the Railway env var, then redeploy.")
        print("  -> See DEBUG_GUIDE.md for full steps.")
        print("=" * 60)
        sys.exit(1)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    print("Running main...")
    asyncio.run(main())
