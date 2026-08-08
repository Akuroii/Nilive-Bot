import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import aiohttp
from database import DB_PATH
from utils.permissions import check_bot_role_position
from utils import creator_notify_engine as engine

# ═══════════════════════════════════════════════════════════════════════
# TWITCH INTEGRATION
#
# CREATOR pass 2: routes live/ended notifications through the shared
# utils/creator_notify_engine.py — previously this cog only ever sent a
# new message on going live and never posted anything (edited or new)
# when the stream ended. See that module's header for the state
# machine / concurrency / failure-mode guarantees this relies on.
#
# check_stream_live() returns a TRI-STATE result:
#   dict   -> confirmed live right now (the stream object)
#   False  -> confirmed NOT live right now (a real 200 response with an
#             empty stream list)
#   None   -> the check FAILED (bad token, rate limit, network error,
#             unexpected response) — UNKNOWN. This used to be
#             indistinguishable from "confirmed offline", meaning a
#             single failed Helix call could have looked exactly like
#             the stream ending.
# ═══════════════════════════════════════════════════════════════════════

TWITCH_COLOR = 0x9147FF
ENDED_COLOR  = 0x4E5058


async def get_twitch_token(client_id: str, client_secret: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "grant_type":    "client_credentials",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("access_token")
    except Exception as e:
        print(f"[TWITCH] Token error: {e}")
        return None


async def check_stream_live(username: str, client_id: str, token: str) -> dict | bool | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": username},
                headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    # Token expired/invalid — will be refreshed on the
                    # next tick. Unknown, not a confirmed offline read.
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json()
                streams = data.get("data", [])
                return streams[0] if streams else False
    except Exception as e:
        print(f"[TWITCH] Stream check error: {e}")
        return None


async def get_user_info(username: str, client_id: str, token: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/users",
                params={"login": username},
                headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                users = data.get("data", [])
                return users[0] if users else None
    except Exception:
        return None


def build_twitch_live_embed(username: str, stream: dict) -> discord.Embed:
    title    = stream.get("title", "")
    game     = stream.get("game_name", "")
    viewers  = stream.get("viewer_count", 0)
    twitch_url = f"https://twitch.tv/{username}"

    embed = discord.Embed(
        title=title or f"{username} is live!", url=twitch_url, color=TWITCH_COLOR)
    embed.add_field(name="Game", value=game or "Unknown")
    embed.add_field(name="Viewers", value=f"{viewers:,}")
    thumbnail = stream.get("thumbnail_url", "")
    if thumbnail:
        thumbnail = thumbnail.replace("{width}", "640").replace("{height}", "360")
        embed.set_image(url=thumbnail)
    embed.set_footer(text="🟣 Live on Twitch")
    return embed


def build_twitch_ended_embed(username: str) -> discord.Embed:
    twitch_url = f"https://twitch.tv/{username}"
    embed = discord.Embed(
        title=f"{username} was live", url=twitch_url, color=ENDED_COLOR)
    embed.set_footer(text="⚫ Stream ended")
    return embed


def _build_content(mention: str, custom_msg: str | None, username: str,
                    title: str, game: str, url: str) -> str:
    if custom_msg:
        text = (custom_msg
                .replace("{streamer}", username)
                .replace("{title}", title)
                .replace("{game}", game)
                .replace("{url}", url))
    else:
        text = f"🔴 **{username}** is now LIVE!"
    return f"{mention} {text}".strip() if mention else text


class Twitch(commands.Cog):
    def __init__(self, bot):
        self.bot    = bot
        self._token = None
        self.check_streams.start()

    def cog_unload(self):
        self.check_streams.cancel()

    async def cog_load(self):
        await engine.ensure_tables()

    async def _ensure_token(self) -> bool:
        import os
        client_id     = os.getenv("TWITCH_CLIENT_ID")
        client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not client_id or not client_secret:
            return False
        if not self._token:
            self._token = await get_twitch_token(client_id, client_secret)
        return bool(self._token)

    @tasks.loop(minutes=5)
    async def check_streams(self):
        import os
        client_id = os.getenv("TWITCH_CLIENT_ID")
        if not client_id:
            return
        if not await self._ensure_token():
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, twitch_username,
                       discord_channel_id, custom_message,
                       mention_type, ping_role_id,
                       give_role_id, role_duration_hours,
                       is_live, discord_user_id
                FROM twitch_config WHERE enabled = 1
            """)
            configs = await cursor.fetchall()

        for cfg in configs:
            (cid, guild_id, username, discord_ch_id,
             custom_msg, mention_type, ping_role_id,
             give_role_id, role_duration_hours, was_live,
             discord_user_id) = cfg

            try:
                result = await check_stream_live(username, client_id, self._token)

                if result is None:
                    # Token invalid/expired or a transient Helix
                    # failure — reset the cached token so the next tick
                    # re-fetches one, but otherwise skip this watch
                    # entirely this round. No state change.
                    self._token = None
                    continue

                guild = self.bot.get_guild(guild_id)
                is_live_now = isinstance(result, dict)

                if is_live_now:
                    if not was_live:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "UPDATE twitch_config SET is_live = 1 WHERE id = ?",
                                (cid,))
                            await db.commit()
                        if guild:
                            await self._go_live(
                                guild, cid, guild_id, username, result,
                                discord_ch_id, custom_msg, mention_type,
                                ping_role_id, give_role_id, discord_user_id)
                    else:
                        await engine.note_still_live(guild_id, "twitch", cid)
                else:
                    if was_live:
                        confirmed_offline = await engine.note_offline_tick(
                            guild_id, "twitch", cid)
                        if confirmed_offline:
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute(
                                    "UPDATE twitch_config SET is_live = 0 WHERE id = ?",
                                    (cid,))
                                await db.commit()
                            if guild:
                                await self._go_offline(
                                    guild, guild_id, cid, username,
                                    give_role_id, discord_user_id)

            except Exception as e:
                print(f"[TWITCH] Error for config {cid}: {e}")

    async def _go_live(self, guild, cid, guild_id, username, stream,
                        discord_ch_id, custom_msg, mention_type,
                        ping_role_id, give_role_id, discord_user_id=None):
        twitch_url  = f"https://twitch.tv/{username}"
        title       = stream.get("title", "")
        game        = stream.get("game_name", "")
        external_id = str(stream.get("id", ""))

        # CREATOR pass 3 (consolidated notifications): a watch linked
        # to a Creator Group posts/edits that group's single shared
        # message instead of sending its own — detecting "is it live"
        # above this point is completely unchanged either way. See
        # utils/creator_notify_engine.py's "CREATOR GROUPS" section
        # for the full design. Unlinked watches (get_watch_group
        # returns None — the default for every watch that exists
        # today) fall straight through to the exact same
        # start_live_session() call this cog always made.
        group = await engine.get_watch_group("twitch", cid)
        if group:
            tracked = await engine.start_watch_tracking(
                guild_id, "twitch", cid, group["discord_channel_id"], external_id)
            if tracked.get("started"):
                result = await engine.note_platform_live_grouped(
                    self.bot, group, "twitch", cid, "Twitch", "🟣", twitch_url)
                if not result.get("sent"):
                    print(f"[TWITCH] Group notification not sent for "
                          f"config {cid}: {result.get('reason')}")
        else:
            mention = engine.build_mention(guild, mention_type, ping_role_id)
            content = _build_content(mention, custom_msg, username, title, game, twitch_url)
            embed   = build_twitch_live_embed(username, stream)
            view    = engine.WatchNowView(twitch_url)

            result = await engine.start_live_session(
                self.bot, guild_id, "twitch", cid, discord_ch_id,
                external_id=external_id,
                content=content, embed=embed, view=view)

            if not result.get("sent") and result.get("reason") not in (
                    "already_active", "duplicate_suppressed"):
                print(f"[TWITCH] Live notification not sent for config {cid}: "
                      f"{result.get('reason')}")

        if give_role_id:
            if not discord_user_id:
                print(f"[TWITCH] give_role_id is configured for config {cid} "
                      f"({username}) but no discord_user_id is set — skipping "
                      f"role grant rather than guessing which member to give "
                      f"it to. Set the streamer's Discord user in /twitch_setup "
                      f"or the dashboard.")
            else:
                role = guild.get_role(int(give_role_id))
                if role:
                    can, warn = check_bot_role_position(guild, role)
                    if can:
                        member = guild.get_member(int(discord_user_id))
                        if member and role not in member.roles:
                            try:
                                await member.add_roles(
                                    role, reason="Streamer went live")
                            except Exception as e:
                                print(f"[TWITCH] Failed to add live role to "
                                      f"{discord_user_id} in {guild_id}: {e}")
                    else:
                        print(f"[TWITCH ROLE WARNING] {warn}")

    async def _go_offline(self, guild, guild_id, cid, username,
                           give_role_id, discord_user_id=None):
        group = await engine.get_watch_group("twitch", cid)
        if group:
            result = await engine.note_platform_offline_grouped(
                self.bot, guild_id, group["id"], "twitch", cid)
            if not result.get("updated"):
                print(f"[TWITCH] Group offline update failed for config "
                      f"{cid}: {result.get('reason')}")
            await engine.stop_watch_tracking(guild_id, "twitch", cid)
        else:
            ended_embed = build_twitch_ended_embed(username)
            view = engine.WatchNowView(f"https://twitch.tv/{username}", "Channel")
            await engine.end_live_session(
                self.bot, guild_id, "twitch", cid,
                ended_content=None, ended_embed=ended_embed, view=view)

        if give_role_id and discord_user_id:
            role = guild.get_role(int(give_role_id))
            member = guild.get_member(int(discord_user_id))
            if role and member and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Stream ended")
                except Exception as e:
                    print(f"[TWITCH] Failed to remove live role from "
                          f"{discord_user_id} in {guild_id}: {e}")

    @check_streams.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="twitch_setup", description="Set up Twitch live alerts")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_setup(self, interaction: discord.Interaction, twitch_username: str,
                            discord_channel: discord.TextChannel,
                            ping_role: discord.Role = None,
                            give_role: discord.Role = None,
                            discord_streamer: discord.Member = None,
                            custom_message: str = None):
        import os
        if not os.getenv("TWITCH_CLIENT_ID"):
            await interaction.response.send_message(
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables are not set on Railway.",
                ephemeral=True)
            return

        mention_type = "role" if ping_role else "none"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO twitch_config
                    (guild_id, twitch_username, discord_channel_id, custom_message,
                     mention_type, ping_role_id, give_role_id, discord_user_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (interaction.guild.id, twitch_username.lower(), discord_channel.id,
                  custom_message, mention_type,
                  ping_role.id if ping_role else None,
                  give_role.id if give_role else None,
                  discord_streamer.id if discord_streamer else None))
            await db.commit()

        embed = discord.Embed(title="Twitch Alerts Set Up", color=TWITCH_COLOR)
        embed.add_field(name="Streamer", value=twitch_username)
        embed.add_field(name="Posts to", value=discord_channel.mention)
        if ping_role:
            embed.add_field(name="Pings", value=ping_role.mention)
        if give_role:
            embed.add_field(name="Live Role", value=give_role.mention)
            if discord_streamer:
                embed.add_field(name="Role goes to", value=discord_streamer.mention)
            else:
                embed.add_field(
                    name="⚠️ Missing discord_streamer",
                    value="give_role won't be granted to anyone until you "
                          "also set discord_streamer (re-run /twitch_setup).",
                    inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="twitch_remove", description="Remove a Twitch alert")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_remove(self, interaction: discord.Interaction, entry_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM twitch_config WHERE id = ? AND guild_id = ?",
                (entry_id, interaction.guild.id))
            await db.commit()
        await interaction.response.send_message(f"Removed Twitch config #{entry_id}.", ephemeral=True)

    @app_commands.command(name="twitch_list", description="List Twitch alert configs")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, twitch_username, discord_channel_id, is_live, enabled
                FROM twitch_config WHERE guild_id = ?
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message("No Twitch configs set up.", ephemeral=True)
            return

        embed = discord.Embed(title="Twitch Configs", color=TWITCH_COLOR)
        for (cid, username, dch, is_live, enabled) in rows:
            status = "🔴 LIVE" if is_live else "⚫ Offline"
            active = "✅" if enabled else "❌"
            embed.add_field(name=f"#{cid} {active} — {username}", value=f"{status} → <#{dch}>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Twitch(bot))
