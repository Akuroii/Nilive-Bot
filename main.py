import discord
from discord.ext import commands
import asyncio
import os
import traceback
from dotenv import load_dotenv

load_dotenv()

print("Starting Nero bot...")
print(f"Token exists: {bool(os.getenv('DISCORD_TOKEN'))}")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

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
    ]
    for cog in cog_files:
        try:
            await bot.load_extension(cog)
            print(f"Loaded {cog}")
        except Exception as e:
            print(f"Failed to load {cog}: {e}")
            traceback.print_exc()

@bot.event
async def on_ready():
    print(f"Nero is online as {bot.user}")
    try:
        await bot.tree.sync()
        synced = await bot.tree.fetch_commands()
        print(f"Synced {len(synced)} slash commands")
        for cmd in synced:
            print(f"  - /{cmd.name}")
    except Exception as e:
        print(f"Sync error: {e}")
        traceback.print_exc()

@bot.command()
@commands.is_owner()
async def sync(ctx):
    await bot.tree.sync()
    cmds = await bot.tree.fetch_commands()
    await ctx.send(f"Synced {len(cmds)} commands!")

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
