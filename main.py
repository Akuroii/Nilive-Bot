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
from utils.db_config import DB_PATH
from database import (
    add_guild_owner,
    record_bot_guild_join, record_bot_guild_remove, record_bot_guild_update,
    DEV_DISCORD_IDS, TRUSTED_DISCORD_IDS, VISIBLE_OWNER_IDS,
)
from utils.guild_lifecycle import GuildLifecycleCoordinator
from utils import guild_allowlist

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

# Both entrypoint modes and aliases use one shared state; never import main for it.
from utils.command_gating import _command_cooldowns, _prune_command_cooldowns
from utils.task_lifecycle import report_task_health


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

        # SHARED GATING (2026-08-28): this whole body — enabled/disabled,
        # owner-only, role + channel black/whitelists, cooldown_seconds with
        # its bypass roles and the error_message override — now lives in
        # utils/command_gating.py, because the bare-alias path
        # (cogs/command_aliases.py) has to apply the *same* rules and had
        # already started to drift from this copy. One implementation, one
        # cooldown dict, so /kick and the alias `k` cannot disagree about
        # whether a member may run something, or double up on rate limiting.
        # Imported lazily (same reason as cogs.health below): main.py must not
        # need cogs/* at module import time.
        from utils.command_gating import evaluate_toggle_row, load_toggle_row

        row = await load_toggle_row(interaction.guild.id, cmd_name)
        now = time.time()
        allowed, msg = evaluate_toggle_row(
            row,
            guild_id=interaction.guild.id,
            command_name=cmd_name,
            member=interaction.user,
            channel_id=interaction.channel_id,
            guild_owner_id=interaction.guild.owner_id,
            cooldowns=_command_cooldowns,
            now=now,
        )
        # Pruning used to happen only when a cooldown was actually hit, which
        # meant a busy bot with mostly-unlimited commands never pruned at all.
        _prune_command_cooldowns(now)

        if not allowed and msg:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            return False

        return True
# owner_ids makes bot.is_owner() follow OWNER_ID + DEV_ID (the same trust set the dashboard
# uses) instead of whoever owns the application in the Discord Developer Portal.
bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=NeroCommandTree,
                   case_insensitive=True, owner_ids=set(TRUSTED_DISCORD_IDS))
bot.gateway_coordinator = GuildLifecycleCoordinator(bot)

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
        "cogs.wallet",
        "cogs.trade",
        "cogs.missions",
        "cogs.boost",
        "cogs.botprofile",
        "cogs.reactionroles",
        "cogs.rolereplacement",
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
        "cogs.guild_admin",
        "cogs.scheduler",
        "cogs.tagmissions",
        "cogs.tagpartners",
        "cogs.afk",
        "cogs.shop_publication_sync",
        # Load order does NOT matter for aliases any more (2026-08-28). The
        # old comment here said "MUST be last, it rewrites message.content so
        # process_commands can dispatch it" — that was wrong twice over:
        # discord.py runs process_commands before every cog on_message
        # listener, and the rewrite leaked into the listeners that ran
        # concurrently. Aliases now go through utils/message_router.py, which
        # decides one owner per message without touching the message at all.
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


_command_sync_lock = asyncio.Lock()


async def sync_startup_commands(*, force: bool = False):
    """Sync deployed definitions once; explicit developer calls can force it."""
    async with _command_sync_lock:
        if not force and getattr(bot, "_startup_commands_synced", False):
            return None
        synced = await bot.tree.sync()
        bot._startup_commands_synced = True
        return synced


@bot.event
async def on_ready():
    # IDEMPOTENT: discord.py dispatches on_ready again after a reconnect that
    # required a fresh IDENTIFY (parse_ready -> _delay_ready -> 'ready'), not
    # only on the first boot. tasks.Loop.start() raises RuntimeError("Task is
    # already launched and is not completed.") on a second call, so the old
    # unconditional rotate_status.start() here raised on every such reconnect
    # and aborted the remainder of this handler.
    print(f"Nero is online as {bot.user}")
    bot.gateway_coordinator.gateway_transport_verified = True
    # Command definitions are unchanged within a running process. Sync on the
    # first READY (including every fresh deployment), retry on failure, and
    # leave the developer !sync command as the explicit force-refresh path.
    sync_started = time.perf_counter()
    try:
        synced = await sync_startup_commands()
        if synced is not None:
            print(f"Synced {len(synced)} slash commands "
                  f"in {(time.perf_counter() - sync_started) * 1000:.0f} ms")
    except Exception as e:
        print(f"Sync error after {(time.perf_counter() - sync_started) * 1000:.0f} ms: {e}")
        traceback.print_exc()

    await bot.gateway_coordinator.reconcile_guilds()
    await guild_allowlist.run_sweep(bot)
    await backfill_guild_owners()
    # Idempotent start: safe on the first READY and on every reconnect READY.
    if not rotate_status.is_running():
        rotate_status.start()
    # Surface a dead background loop instead of letting it tick zero times in
    # silence for the life of the process (the pre-launch failure mode).
    report_task_health(bot)


@bot.event
async def on_resumed():
    print("[GATEWAY] Session resumed successfully; reconciling guild state...")
    bot.gateway_coordinator.gateway_transport_verified = True
    await bot.gateway_coordinator.reconcile_guilds()


@bot.event
async def on_disconnect():
    print("[GATEWAY] Disconnected from Discord Gateway; invalidating authorization...")
    await bot.gateway_coordinator.on_disconnect_async()


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)


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
        msg = ("Something went wrong running that command. "
               "The error has been logged.")
        if interaction.response.is_done():
            # CONFIRMED BUG FIX: commands that had already deferred (or
            # otherwise responded) hit `is_done() == True` here, and the
            # user-facing reply was silently swallowed. The first
            # response slot is consumed at that point, so the message
            # must go out through the followup webhook instead.
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"Joined new guild: {guild.name} ({guild.id})")
    # Optional allowlist (utils/guild_allowlist.py): off unless ALLOWED_GUILD_IDS is set.
    # False means the bot just left an unapproved server, so nothing is recorded for it.
    if not await guild_allowlist.handle_join(guild):
        return
    async def _do_join():
        icon_key = getattr(guild.icon, "key", None) if hasattr(guild.icon, "key") else (str(guild.icon) if guild.icon else None)
        await record_bot_guild_join(
            guild.id, guild.name, icon_key,
            guild.owner_id or 0, getattr(guild, "member_count", 0) or 0,
        )
        await add_guild_owner(guild.id, guild.owner_id)
        for _owner_id in VISIBLE_OWNER_IDS:
            await add_guild_owner(guild.id, _owner_id)
    await bot.gateway_coordinator.execute_mutation(_do_join)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    if getattr(guild, "unavailable", False):
        print(f"[GATEWAY] Guild {guild.id} is temporarily unavailable; preserving in database.")
        return
    print(f"Removed from guild: {guild.name} ({guild.id})")
    async def _do_remove():
        await record_bot_guild_remove(guild.id)
    await bot.gateway_coordinator.execute_mutation(_do_remove)


@bot.event
async def on_guild_unavailable(guild: discord.Guild):
    print(f"[GATEWAY] Guild {guild.id} is temporarily unavailable due to Discord outage; preserving in database.")


@bot.event
async def on_guild_available(guild: discord.Guild):
    print(f"[GATEWAY] Guild {guild.id} is back online; updating metadata.")
    icon_key = getattr(guild.icon, "key", None) if hasattr(guild.icon, "key") else (str(guild.icon) if guild.icon else None)
    async def _do_update():
        await record_bot_guild_update(guild.id, guild.name, icon_key)
    await bot.gateway_coordinator.execute_mutation(_do_update)


@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    icon_key = getattr(after.icon, "key", None) if hasattr(after.icon, "key") else (str(after.icon) if after.icon else None)
    async def _do_update():
        await record_bot_guild_update(after.id, after.name, icon_key)
    await bot.gateway_coordinator.execute_mutation(_do_update)


async def backfill_guild_owners():
    """
    Backfills owner access rows in dashboard_users for installed guilds.
    Guild synchronization to bot_guilds is handled authoritatively by
    bot.gateway_coordinator.reconcile_guilds().
    """
    for guild in bot.guilds:
        if guild_allowlist.decide(guild_allowlist.get_config(), guild.id) == guild_allowlist.LEAVE:
            continue  # unapproved server in enforce mode: no access rows for it
        if guild.owner_id:
            await add_guild_owner(guild.id, guild.owner_id)
        for _owner_id in VISIBLE_OWNER_IDS:
            await add_guild_owner(guild.id, _owner_id)
    print(f"✅ Backfilled owner access for {len(bot.guilds)} installed guilds")


def dev_only():
    """!sync / !reload can break the running bot: developer(s) only, not the bot owner."""
    async def predicate(ctx):
        return ctx.author.id in DEV_DISCORD_IDS
    return commands.check(predicate)


@bot.command()
@dev_only()
async def sync(ctx):
    started = time.perf_counter()
    cmds = await sync_startup_commands(force=True)
    elapsed_ms = (time.perf_counter() - started) * 1000
    await ctx.send(f"Synced {len(cmds)} commands in {elapsed_ms:.0f} ms!")


@bot.command()
@dev_only()
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
