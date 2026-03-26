import discord
from discord.ext import commands, tasks
import asyncio
import os
import traceback
from dotenv import load_dotenv
import aiosqlite
from database import DB_PATH, add_guild_owner

load_dotenv()

print("Starting Nero bot...")
print(f"Token exists: {bool(os.getenv('DISCORD_TOKEN'))}")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

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
    """
    When Nero joins a new server, immediately grant
    the owner dashboard access for that guild.
    No restart needed.
    """
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
