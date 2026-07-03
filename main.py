import discord
from discord.ext import commands, tasks
import asyncio
import os
import json          # ← NEW
import time          # ← NEW
import traceback
from dotenv import load_dotenv
import aiosqlite
from database import DB_PATH, add_guild_owner

load_dotenv()

print("Starting Nero bot...")
print(f"Token exists: {bool(os.getenv('DISCORD_TOKEN'))}")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ── COMMAND TOGGLE ENFORCEMENT ────────────────────────────────   ← NEW BLOCK
_command_cooldowns: dict[tuple, float] = {}

@bot.tree.check
async def global_command_gate(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or interaction.command is None:
        return True

    cmd_name = interaction.command.qualified_name

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT enabled, allowed_roles, allowed_channels, owner_only,
                   cooldown_seconds, bypass_cooldown_roles, error_message
            FROM command_toggles
            WHERE guild_id = ? AND command_name = ?
        """, (interaction.guild.id, cmd_name))
        row = await cursor.fetchone()

    if not row:
        return True

    (enabled, allowed_roles, allowed_channels, owner_only,
     cooldown_seconds, bypass_cooldown_roles, error_message) = row

    if not enabled:
        msg = error_message or f"`/{cmd_name}` is currently disabled on this server."
        await interaction.response.send_message(msg, ephemeral=True)
        return False

    if owner_only and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(
            "This command is restricted to the server owner.", ephemeral=True)
        return False

    if allowed_roles:
        try:
            role_ids = {int(r) for r in json.loads(allowed_roles)}
        except Exception:
            role_ids = set()
        if role_ids:
            member_role_ids = {r.id for r in interaction.user.roles}
            if not member_role_ids & role_ids:
                await interaction.response.send_message(
                    "You don't have permission to use this command.", ephemeral=True)
                return False

    if allowed_channels:
        try:
            channel_ids = {int(c) for c in json.loads(allowed_channels)}
        except Exception:
            channel_ids = set()
        if channel_ids and interaction.channel_id not in channel_ids:
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

    return True
# ── END OF NEW BLOCK ──────────────────────────────────────────

# ↓↓↓ YOUR ORIGINAL CODE CONTINUES BELOW (unchanged) ↓↓↓

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
        "cogs.mvp",
        "cogs.moderation",
        "cogs.leveling",
        "cogs.economy",
        "cogs.boost",
        "cogs.reactionroles",
        "cogs.tickets",
        "cogs.embedbuilder",
        "cogs.sticky",
        "cogs.roleplay",
        "cogs.youtube",
        "cogs.triggers",
        "cogs.customcommands",
        "cogs.welcome",
        "cogs.shop",
        "cogs.auditlog",
        "cogs.twitch",
        "cogs.events",
    ]
    for cog in cog_files:
        try:
            await bot.load_extension(cog)
            print(f"  Loaded {cog}")
        except Exception as e:
            print(f"  Failed to load {cog}: {e}")
            traceback.print_exc()


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
    rotate_status.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"Joined new guild: {guild.name} ({guild.id})")
    await add_guild_owner(guild.id)


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
        print("Starting bot...")
        await bot.start(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()

print("Running main...")
asyncio.run(main())
