import discord
from discord.ext import commands
from discord import app_commands
from discord.ext import tasks
import aiosqlite
import aiohttp
from database import DB_PATH

class YouTube(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_videos.start()

    def cog_unload(self):
        self.check_videos.cancel()

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS youtube_config (
                    guild_id INTEGER,
                    channel_id TEXT,
                    announce_channel_id INTEGER,
                    last_video_id TEXT,
                    message TEXT,
                    PRIMARY KEY (guild_id, channel_id)
                )
            """)
            await db.commit()

    async def get_latest_video(self, channel_id: str):
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    text = await resp.text()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(text)
            ns = {"yt": "http://www.youtube.com/xml/schemas/2015",
                  "atom": "http://www.w3.org/2005/Atom"}
            entry = root.find("atom:entry", ns)
            if entry is None:
                return None
            video_id = entry.find("yt:videoId", ns).text
            title = entry.find("atom:title", ns).text
            link = entry.find("atom:link", ns).get("href")
            author = root.find("atom:author/atom:name", ns).text
            return {"id": video_id, "title": title, "link": link, "author": author}
        except:
            return None

    @tasks.loop(minutes=10)
    async def check_videos(self):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT guild_id, channel_id, announce_channel_id, last_video_id, message FROM youtube_config")
            rows = await cursor.fetchall()
        for guild_id, yt_channel_id, announce_id, last_id, custom_msg in rows:
            video = await self.get_latest_video(yt_channel_id)
            if not video or video["id"] == last_id:
                continue
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE youtube_config SET last_video_id=? WHERE guild_id=? AND channel_id=?",
                    (video["id"], guild_id, yt_channel_id))
                await db.commit()
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(announce_id)
            if not channel:
                continue
            msg = custom_msg or "{author} just uploaded a new video!"
            msg = msg.replace("{author}", video["author"]).replace("{title}", video["title"])
            embed = discord.Embed(
                title=video["title"],
                url=video["link"],
                description=msg,
                color=discord.Color.red())
            embed.set_author(name=video["author"])
            embed.set_footer(text="YouTube")
            await channel.send(embed=embed)

    @app_commands.command(name="youtube_setup", description="Set up YouTube video announcements")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_setup(self, interaction: discord.Interaction,
                            youtube_channel_id: str,
                            announce_channel: discord.TextChannel,
                            message: str = None):
        await self.ensure_table()
        video = await self.get_latest_video(youtube_channel_id)
        if not video:
            await interaction.response.send_message(
                "Could not find that YouTube channel! Make sure you're using the Channel ID (not the name).",
                ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO youtube_config
                (guild_id, channel_id, announce_channel_id, last_video_id, message)
                VALUES (?, ?, ?, ?, ?)
            """, (interaction.guild.id, youtube_channel_id,
                  announce_channel.id, video["id"], message))
            await db.commit()
        embed = discord.Embed(title="YouTube Notifications Set Up!", color=discord.Color.red())
        embed.add_field(name="Channel", value=video["author"])
        embed.add_field(name="Announce in", value=announce_channel.mention)
        embed.add_field(name="Latest video", value=f"[{video['title']}]({video['link']})")
        embed.set_footer(text="Bot checks for new videos every 10 minutes")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="youtube_remove", description="Remove YouTube notifications")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_remove(self, interaction: discord.Interaction, youtube_channel_id: str):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM youtube_config WHERE guild_id=? AND channel_id=?",
                (interaction.guild.id, youtube_channel_id))
            await db.commit()
        await interaction.response.send_message("YouTube notifications removed!", ephemeral=True)

    @app_commands.command(name="youtube_list", description="List all YouTube channels being tracked")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_list(self, interaction: discord.Interaction):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT channel_id, announce_channel_id FROM youtube_config WHERE guild_id=?",
                (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No YouTube channels tracked!", ephemeral=True)
            return
        embed = discord.Embed(title="Tracked YouTube Channels", color=discord.Color.red())
        for yt_id, ch_id in rows:
            embed.add_field(name=f"Channel ID: {yt_id}", value=f"Announces in <#{ch_id}>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(YouTube(bot))
