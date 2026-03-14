import discord
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

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
    ]
    for cog in cog_files:
        try:
            await bot.load_extension(cog)
            print(f"Loaded {cog}")
        except Exception as e:
            print(f"Failed to load {cog}: {e}")

@bot.event
async def on_ready():
    print(f"Nero is online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Sync error: {e}")

async def main():
    from database import init_db
    await init_db()
    await load_cogs()
    await bot.start(os.getenv("DISCORD_TOKEN"))

asyncio.run(main())
