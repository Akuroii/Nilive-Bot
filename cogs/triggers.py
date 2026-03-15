import discord
from discord.ext import commands
import aiosqlite
from database import DB_PATH

class Triggers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    trigger TEXT,
                    response TEXT,
                    embed_title TEXT,
                    embed_color TEXT,
                    input_channel_id INTEGER,
                    output_channel_id INTEGER
                )
            """)
            await db.commit()
            cursor = await db.execute("SELECT * FROM triggers")
            all_triggers = await cursor.fetchall()

        content = message.content.lower()
        for t in all_triggers:
            _, guild_id, trigger, response, embed_title, embed_color, input_ch, output_ch = t
            if not trigger:
                continue
            if trigger.lower() not in content:
                continue
            if input_ch and message.channel.id != int(input_ch):
                continue
            target = message.channel
            if output_ch:
                ch = message.guild.get_channel(int(output_ch))
                if ch:
                    target = ch
            try:
                color_int = int(embed_color.strip("#"), 16) if embed_color else 0x5865F2
            except:
                color_int = 0x5865F2
            embed = discord.Embed(color=color_int)
            if embed_title:
                embed.title = embed_title
            if response:
                embed.description = response
            await target.send(embed=embed)
            break

async def setup(bot):
    await bot.add_cog(Triggers(bot))
