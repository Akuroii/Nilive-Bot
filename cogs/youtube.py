import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import aiohttp
import json
import os
from database import DB_PATH
from utils import creator_notify_engine as engine


RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"

VIDEO_COLOR = 0xFF0000
SHORTS_COLOR = 0xFF0050
LIVE_COLOR = 0xFF0000
ENDED_COLOR = 0x4E5058


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


# ─── CREATOR pass 2: YouTube Live detection ─────────────────────────────
#
# Tri-state, matching cogs/twitch.py's check_stream_live() exactly:
#   dict  -> confirmed live/upcoming right now (the videos.list item)
#   False -> a real response came back and the video is NOT live/upcoming
#            (no liveStreamingDetails at all, or it has an
#            actualEndTime — i.e. it's a normal video or a finished
#            broadcast that's now a VOD)
#   None  -> the check FAILED or was inconclusive: missing
#            YOUTUBE_API_KEY, non-200, network error, or an empty
#            `items` list. An empty list for a video ID we just saw in
#            the RSS feed (or are actively tracking as live) is
#            ambiguous — could be API propagation lag, could be a
#            genuinely deleted/privated video — so it's never treated
#            as a confident "not live" on its own; every call site
#            below does nothing on None and waits for the next tick.
#
# One deliberate consequence of that last rule: if a video is deleted
# or made private WHILE its live session is active, this will just
# look like a string of None results forever rather than a confirmed-
# ended transition, and the live session is left open rather than
# risking an incorrect "ended" edit off an ambiguous signal. Matches
# this engine's "never guess, fail toward doing nothing" rule
# throughout — a stale "still live" badge is a far smaller problem
# than silently closing out a session that's still genuinely live.
# Worth a follow-up (e.g. a hard cap on "how many ticks in a row can
# stay open on None before giving up") if this turns out to matter in
# practice; not built here to keep this pass's scope to what was
# asked.
async def get_video_live_status(video_id: str) -> dict | bool | None:
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "part": "snippet,liveStreamingDetails",
                        "id": video_id,
                        "key": api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items", [])
                if not items:
                    return None
                item = items[0]
                details = item.get("liveStreamingDetails")
                if not details:
                    return False
                if details.get("actualEndTime"):
                    return False
                if details.get("actualStartTime") or details.get("scheduledStartTime"):
                    return item
                return False
    except Exception as e:
        print(f"[YOUTUBE] Live status check error for {video_id}: {e}")
        return None


async def is_short(video_id: str) -> bool:
    """
    Unofficial-but-reliable Short classification: youtube.com/shorts/{id}
    200s for a real Short and redirects to /watch?v={id} for anything
    else. Fail-safe on any error — returns False (not a Short), which
    falls through to the normal Video path. A real upload silently
    getting misfiled as a Short (wrong channel, wrong message) is a
    worse failure than an actual Short occasionally posting as a
    regular video.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"https://www.youtube.com/shorts/{video_id}",
                    allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
    except Exception as e:
        print(f"[YOUTUBE] Shorts check error for {video_id}: {e}")
        return False


def _video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def _thumbnail_url(video_id: str) -> str:
    # Always-available CDN thumbnail, no API call needed — every valid
    # video ID has one, unlike maxresdefault.jpg which 404s for a lot
    # of older/lower-res uploads.
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _apply_placeholders(text: str, title: str, url: str) -> str:
    return text.replace("{title}", title).replace("{url}", url)


def build_video_embed(video: dict) -> discord.Embed:
    embed = discord.Embed(
        title=video["title"], url=video["url"], color=VIDEO_COLOR)
    embed.set_image(url=_thumbnail_url(video["id"]))
    embed.set_footer(text="📺 New YouTube video")
    return embed


def build_short_embed(video: dict) -> discord.Embed:
    embed = discord.Embed(
        title=video["title"], url=video["url"], color=SHORTS_COLOR)
    embed.set_image(url=_thumbnail_url(video["id"]))
    embed.set_footer(text="🩳 New YouTube Short")
    return embed


def build_live_embed(video_id: str, item: dict) -> discord.Embed:
    snippet = item.get("snippet", {}) or {}
    title = snippet.get("title") or "Live now"
    channel_title = snippet.get("channelTitle") or ""
    thumbs = snippet.get("thumbnails") or {}
    thumb = None
    for size in ("maxres", "standard", "high", "medium", "default"):
        candidate = thumbs.get(size, {}).get("url")
        if candidate:
            thumb = candidate
            break
    if not thumb:
        thumb = _thumbnail_url(video_id)

    embed = discord.Embed(title=title, url=_video_url(video_id), color=LIVE_COLOR)
    if channel_title:
        embed.add_field(name="Channel", value=channel_title)
    embed.set_image(url=thumb)
    embed.set_footer(text="🔴 Live on YouTube")
    return embed


def build_live_ended_embed(video_id: str, title: str = "") -> discord.Embed:
    # No cheap way to recover the original live title at end-of-stream
    # time without either an extra API call (spends quota just for a
    # cosmetic detail) or storing it separately at start time — same
    # tradeoff cogs/twitch.py already accepts for its own ended embed
    # (it uses the static configured username, not the dynamic stream
    # title, either). "Stream ended" plus the still-working link
    # covers the useful part; not worth the extra complexity for the
    # rest.
    embed = discord.Embed(
        title=title or "Stream ended", url=_video_url(video_id), color=ENDED_COLOR)
    embed.set_footer(text="⚫ Stream ended")
    return embed


class YouTube(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_videos.start()

    def cog_unload(self):
        self.check_videos.cancel()

    async def cog_load(self):
        await engine.ensure_tables()

    @tasks.loop(minutes=10)
    async def check_videos(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, youtube_channel_id,
                       youtube_channel_url, discord_channel_id,
                       custom_message, embed_data,
                       ping_role_id, last_video_id,
                       video_mention_type,
                       shorts_enabled, shorts_discord_channel_id,
                       shorts_custom_message, shorts_mention_type,
                       shorts_mention_role_id,
                       live_enabled, live_discord_channel_id,
                       live_custom_message, live_mention_type,
                       live_mention_role_id, live_video_id
                FROM youtube_config
                WHERE enabled = 1
                  AND youtube_channel_id IS NOT NULL
            """)
            configs = await cursor.fetchall()

        for cfg in configs:
            (cid, guild_id, yt_channel_id, yt_url, discord_ch_id,
             custom_msg, embed_data_str, ping_role_id, last_video_id,
             video_mention_type,
             shorts_enabled, shorts_ch_id, shorts_msg,
             shorts_mention_type, shorts_role_id,
             live_enabled, live_ch_id, live_msg,
             live_mention_type, live_role_id, live_video_id) = cfg

            guild = self.bot.get_guild(guild_id)

            # CREATOR pass 3 (consolidated notifications): looked up
            # once per config and reused by both phases below — a
            # watch linked to a Creator Group posts/edits that group's
            # single shared message instead of sending its own; an
            # unlinked watch (group is None — the default for every
            # watch that exists today) falls straight through to the
            # exact same start_live_session()/end_live_session() calls
            # this cog always made. See utils/creator_notify_engine.py's
            # "CREATOR GROUPS" section for the full design.
            group = await engine.get_watch_group("youtube", cid)

            try:
                # ── Phase A: is a video we're already tracking as live
                # still live? Only spends an API call when there's
                # something to track AND Live is still turned on for
                # this watch — an admin flipping live_enabled off
                # mid-broadcast just freezes the open session rather
                # than closing it (same "don't guess" reasoning as the
                # tri-state contract above).
                if live_video_id and live_enabled:
                    result = await get_video_live_status(live_video_id)
                    if result is None:
                        pass
                    elif isinstance(result, dict):
                        await engine.note_still_live(guild_id, "youtube", cid)
                    else:
                        confirmed_offline = await engine.note_offline_tick(
                            guild_id, "youtube", cid)
                        if confirmed_offline:
                            if group:
                                result = await engine.note_platform_offline_grouped(
                                    self.bot, guild_id, group["id"], "youtube", cid)
                                if not result.get("updated"):
                                    print(f"[YOUTUBE] Group offline update "
                                          f"failed for config {cid}: "
                                          f"{result.get('reason')}")
                                await engine.stop_watch_tracking(
                                    guild_id, "youtube", cid)
                            elif guild:
                                await engine.end_live_session(
                                    self.bot, guild_id, "youtube", cid,
                                    ended_content=None,
                                    ended_embed=build_live_ended_embed(live_video_id),
                                    view=engine.WatchNowView(
                                        _video_url(live_video_id), "Watch on YouTube"))
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute("""
                                    UPDATE youtube_config
                                    SET live_video_id = NULL
                                    WHERE id = ?
                                """, (cid,))
                                await db.commit()

                # ── Phase B: has a new video shown up since we last
                # checked the RSS feed? RSS only ever gives the single
                # latest upload, same as before this pass — this is
                # unchanged in shape from the original cog.
                video = await fetch_latest_video(yt_channel_id)
                if not video or video["id"] == last_video_id:
                    continue

                new_id = video["id"]
                handled = False

                if live_enabled and guild:
                    live_result = await get_video_live_status(new_id)
                    if isinstance(live_result, dict):
                        if group:
                            tracked = await engine.start_watch_tracking(
                                guild_id, "youtube", cid,
                                group["discord_channel_id"], new_id)
                            if tracked.get("started"):
                                result = await engine.note_platform_live_grouped(
                                    self.bot, group, "youtube", cid,
                                    "YouTube", "🔴", video["url"])
                                if result.get("sent"):
                                    async with aiosqlite.connect(DB_PATH) as db:
                                        await db.execute("""
                                            UPDATE youtube_config
                                            SET live_video_id = ?
                                            WHERE id = ?
                                        """, (new_id, cid))
                                        await db.commit()
                                else:
                                    print(f"[YOUTUBE] Group notification "
                                          f"not sent for config {cid}: "
                                          f"{result.get('reason')}")
                        else:
                            target_ch_id = live_ch_id or discord_ch_id
                            mention = engine.build_mention(
                                guild, live_mention_type or "none", live_role_id)
                            default_text = f"🔴 New live stream: **{video['title']}**"
                            text = (_apply_placeholders(live_msg, video["title"], video["url"])
                                    if live_msg else default_text)
                            content = f"{mention} {text}".strip() if mention else text

                            result = await engine.start_live_session(
                                self.bot, guild_id, "youtube", cid, target_ch_id,
                                external_id=new_id, content=content,
                                embed=build_live_embed(new_id, live_result),
                                view=engine.WatchNowView(video["url"], "Watch on YouTube"))

                            if result.get("sent"):
                                # Only claim live_video_id when WE
                                # actually started the session — if
                                # another live session for this same
                                # watch is already open
                                # (already_active), leave the existing
                                # live_video_id alone so Phase A keeps
                                # tracking the RIGHT video next tick
                                # instead of getting silently
                                # redirected onto this new one.
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.execute("""
                                        UPDATE youtube_config
                                        SET live_video_id = ?
                                        WHERE id = ?
                                    """, (new_id, cid))
                                    await db.commit()
                            elif result.get("reason") not in (
                                    "already_active", "duplicate_suppressed"):
                                print(f"[YOUTUBE] Live notification not sent "
                                      f"for config {cid}: {result.get('reason')}")
                        handled = True

                if not handled and shorts_enabled and guild and await is_short(new_id):
                    target_ch_id = shorts_ch_id or discord_ch_id
                    channel = guild.get_channel(int(target_ch_id)) if target_ch_id else None
                    if channel:
                        mention = engine.build_mention(
                            guild, shorts_mention_type or "none", shorts_role_id)
                        default_text = f"🩳 New Short: **{video['title']}**"
                        text = (_apply_placeholders(shorts_msg, video["title"], video["url"])
                                if shorts_msg else default_text)
                        content = f"{mention} {text}".strip() if mention else text
                        try:
                            await channel.send(content=content, embed=build_short_embed(video))
                        except Exception as e:
                            print(f"[YOUTUBE] Shorts send error for config {cid}: {e}")
                    handled = True

                if not handled and guild:
                    channel = guild.get_channel(int(discord_ch_id)) if discord_ch_id else None
                    if channel:
                        mention = engine.build_mention(
                            guild, video_mention_type or "role", ping_role_id)
                        default_text = f"📺 New video: **{video['title']}**\n{video['url']}"
                        text = (_apply_placeholders(custom_msg, video["title"], video["url"])
                                if custom_msg else default_text)
                        content = f"{mention} {text}".strip() if mention else text

                        embed = None
                        if embed_data_str:
                            try:
                                embed_data = json.loads(embed_data_str)
                                color_str  = embed_data.get("color", "#FF0000")
                                try:
                                    color_int = int(color_str.strip("#"), 16)
                                except Exception:
                                    color_int = 0xFF0000
                                embed = discord.Embed(
                                    title=embed_data.get("title", video["title"]),
                                    url=video["url"],
                                    color=color_int)
                                if embed_data.get("description"):
                                    embed.description = _apply_placeholders(
                                        embed_data["description"], video["title"], video["url"])
                                embed.add_field(name="Watch Now", value=video["url"])
                            except Exception:
                                embed = None
                        try:
                            if embed:
                                await channel.send(content=content, embed=embed)
                            else:
                                await channel.send(content)
                        except Exception as e:
                            print(f"[YOUTUBE] Video send error for config {cid}: {e}")

                # Every branch above (live, short, or plain video)
                # advances last_video_id — this is what stops a stream
                # that just ended from ALSO posting as a "new video"
                # once YouTube turns it into a VOD under the same ID,
                # and what stops a Short from being reprocessed as a
                # plain upload on the next tick.
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        UPDATE youtube_config
                        SET last_video_id = ?
                        WHERE id = ?
                    """, (new_id, cid))
                    await db.commit()

            except Exception as e:
                print(f"[YOUTUBE] Error for config {cid}: {e}")

    @check_videos.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="youtube_setup",
                          description="Add a YouTube channel to watch (Video notifications)")
    @app_commands.checks.has_permissions(administrator=True)
    async def youtube_setup(
            self, interaction: discord.Interaction,
            youtube_url: str,
            discord_channel: discord.TextChannel,
            ping_role: discord.Role = None,
            custom_message: str = None):
        await interaction.response.defer(ephemeral=True)
        await engine.ensure_tables()

        yt_channel_id = await extract_channel_id(youtube_url)
        video_mention_type = "role" if ping_role else "none"

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO youtube_config
                    (guild_id, youtube_channel_url,
                     youtube_channel_id, discord_channel_id,
                     custom_message, ping_role_id,
                     video_mention_type, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                interaction.guild.id,
                youtube_url,
                yt_channel_id,
                discord_channel.id,
                custom_message,
                ping_role.id if ping_role else None,
                video_mention_type,
            ))
            await db.commit()

        embed = discord.Embed(
            title="YouTube Notifications Set Up",
            color=0xFF0000)
        embed.add_field(name="YouTube URL",  value=youtube_url)
        embed.add_field(name="Posts to",     value=discord_channel.mention)
        if ping_role:
            embed.add_field(name="Pings", value=ping_role.mention)
        embed.add_field(
            name="Shorts & Live",
            value="Configure independently from the dashboard's Creator Hub page.",
            inline=False)
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
                       discord_channel_id, enabled,
                       live_enabled, live_video_id, shorts_enabled
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
        for (cid, url, dch, enabled, live_enabled,
             live_video_id, shorts_enabled) in rows:
            status = "✅" if enabled else "❌"
            extras = []
            if live_enabled:
                extras.append("🔴 LIVE" if live_video_id else "Live: armed")
            if shorts_enabled:
                extras.append("🩳 Shorts on")
            extra_str = f"\n{' · '.join(extras)}" if extras else ""
            embed.add_field(
                name=f"#{cid} {status}",
                value=f"URL: {url[:40]}\nChannel: <#{dch}>{extra_str}",
                inline=False)
        await interaction.response.send_message(embed=embed,
                                                ephemeral=True)


async def setup(bot):
    await bot.add_cog(YouTube(bot))
