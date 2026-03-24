import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import aiohttp
import json
from database import DB_PATH


RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


async def fetch_latest_video(channel_id: str) -> dict | None:
    url = RSS_URL.format(channel_id)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(
                    total=10)) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()

        import re
        video_id_match = re.search(
            r'<yt:videoId>(.+?)</yt:videoId>', text)
        title_match    = re.search(
            r'<title>(.+?)</title>', text)
        link_match     = re.search(
            r'<link rel="alternate" href="(.+?)"/>', text)

        if not video_id_match:
            return None

        # Skip channel title (first <title> match)
        titles = re.findall(r'<title>(.+?)</title>', text)
        title  = titles[1] if len(titles) > 1 else "New Video"

        return {
            "id":    video_id_match.group(1),
            "title": title,
            "url":   (link_match.group(1) if link_match
                      else f"https://youtu.be/{video_id_match.group(1)}"),
        }
    except Exception as e:
        print(f"[YOUTUBE] Fetch error: {e}")
        return None


async def extract_channel_id(url: str) -> str | None:
    """
    Extracts YouTube channel ID from various URL formats.
    Also handles @handle URLs by scraping.
    """
    import re
    # Direct channel ID
    match = re.search(r'channel/([A-Za-z0-9_-]{24})', url)
    if match:
        return match.group(1)
    # Try scraping for @handle
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
        match = re.search(r'"channelId":"([A-Za-z0-9_-]{24})"', text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


class YouTube(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_videos.start()

    def cog_unload(self):
        self.check_videos.cancel()

    @tasks.loop(minutes=10)
    async def check_videos(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, youtube_channel_id,
                       youtube_channel_url, discord_channel_id,
                       custom_message, embed_data,
                       ping_role_id, last_video_id
                FROM youtube_config
                WHERE enabled = 1
                  AND youtube_channel_id IS NOT NULL
            """)
            configs = await cursor.fetchall()

        for cfg in configs:
            (cid, guild_id, yt_channel_id, yt_url,
             discord_ch_id, custom_msg, embed_data_str,
             ping_role_id, last_video_id) = cfg
            try:
                video = await fetch_latest_video(yt_channel_id)
                if not video:
                    continue
                if video["id"] == last_video_id:
                    continue

                # Update last video ID
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        UPDATE youtube_config
                        SET last_video_id = ?
                        WHERE id = ?
                    """, (video["id"], cid))
                    await db.commit()

                guild   = self.bot.get_guild(guild_id)
                channel = guild.get_channel(int(discord_ch_id)) if guild else None
                if not channel:
                    continue

                # Build message
                content = ""
                if ping_role_id:
                    role = guild.get_role(int(ping_role_id))
                    if role:
                        content = role.mention + " "

                if custom_msg:
                    content += (custom_msg
                                .replace("{title}", video["title"])
                                .replace("{url}", video["url"]))
                else:
                    content += f"📺 New video: **{video['title']}**\n{video['url']}"

                # Build embed if configured
                if embed_data_str:
                    try:
                        embed_data = json.loads(embed_data_str)
                        color_str  = embed_data.get("color", "#FF0000")
                        try:
                            color_int = int(
                                color_str.strip("#"), 16)
                        except Exception:
                            color_int = 0xFF0000
                        embed = discord.Embed(
                            title=embed_data.get(
                                "title", video["title"]),
                            url=video["url"],
                            color=color_int)
                        if embed_data.get("description"):
                            embed.description = (
                                embed_data["description"]
                                .replace("{title}", video["title"])
                                .replace("{url}", video["url"]))
                        embed.add_field(
                            name="Watch Now",
                            value=video["url"])
                        await channel.send(content=content,
                                           embed=embed)
                    except Exception:
                        await channel.send(content)
                else:
                    await channel.send(content)

            except Exception as e:
                print(f"[YOUTUBE] Error for config {cid}: {e}")

    @check_videos.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="youtube_setup",
                          description="Add a YouTube channel to watch")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_setup(
            self, interaction: discord.Interaction,
            youtube_url: str,
            discord_channel: discord.TextChannel,
            ping_role: discord.Role = None,
            custom_message: str = None):
        await interaction.response.defer(ephemeral=True)

        yt_channel_id = await extract_channel_id(youtube_url)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO youtube_config
                    (guild_id, youtube_channel_url,
                     youtube_channel_id, discord_channel_id,
                     custom_message, ping_role_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (
                interaction.guild.id,
                youtube_url,
                yt_channel_id,
                discord_channel.id,
                custom_message,
                ping_role.id if ping_role else None,
            ))
            await db.commit()

        embed = discord.Embed(
            title="YouTube Notifications Set Up",
            color=0xFF0000)
        embed.add_field(name="YouTube URL",  value=youtube_url)
        embed.add_field(name="Posts to",     value=discord_channel.mention)
        if ping_role:
            embed.add_field(name="Pings", value=ping_role.mention)
        if not yt_channel_id:
            embed.add_field(
                name="⚠️ Warning",
                value="Could not extract channel ID. "
                      "Notifications may not work.",
                inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="youtube_remove",
                          description="Remove a YouTube notification")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_remove(self, interaction: discord.Interaction,
                              entry_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                DELETE FROM youtube_config
                WHERE id = ? AND guild_id = ?
            """, (entry_id, interaction.guild.id))
            await db.commit()
        await interaction.response.send_message(
            f"Removed YouTube config #{entry_id}.",
            ephemeral=True)

    @app_commands.command(name="youtube_list",
                          description="List YouTube notification configs")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, youtube_channel_url,
                       discord_channel_id, enabled
                FROM youtube_config
                WHERE guild_id = ?
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No YouTube configs set up.", ephemeral=True)
            return

        embed = discord.Embed(title="YouTube Configs",
                              color=0xFF0000)
        for (cid, url, dch, enabled) in rows:
            status = "✅" if enabled else "❌"
            embed.add_field(
                name=f"#{cid} {status}",
                value=f"URL: {url[:40]}\nChannel: <#{dch}>",
                inline=False)
        await interaction.response.send_message(embed=embed,
                                                ephemeral=True)


async def setup(bot):
    await bot.add_cog(YouTube(bot))
