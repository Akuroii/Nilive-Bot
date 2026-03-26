import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import aiohttp
import json
from database import DB_PATH
from utils.permissions import check_bot_role_position


async def get_twitch_token(client_id: str,
                            client_secret: str) -> str | None:
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


async def check_stream_live(username: str, client_id: str,
                             token: str) -> dict | None:
    """Returns stream data if live, None if offline."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": username},
                headers={
                    "Client-ID":     client_id,
                    "Authorization": f"Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                streams = data.get("data", [])
                return streams[0] if streams else None
    except Exception as e:
        print(f"[TWITCH] Stream check error: {e}")
        return None


async def get_user_info(username: str, client_id: str,
                         token: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/users",
                params={"login": username},
                headers={
                    "Client-ID":     client_id,
                    "Authorization": f"Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                users = data.get("data", [])
                return users[0] if users else None
    except Exception:
        return None


class Twitch(commands.Cog):
    def __init__(self, bot):
        self.bot    = bot
        self._token = None
        self.check_streams.start()

    def cog_unload(self):
        self.check_streams.cancel()

    async def _ensure_token(self) -> bool:
        import os
        client_id     = os.getenv("TWITCH_CLIENT_ID")
        client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not client_id or not client_secret:
            return False
        if not self._token:
            self._token = await get_twitch_token(
                client_id, client_secret)
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
                       embed_data, ping_role_id,
                       give_role_id, role_duration_hours,
                       is_live
                FROM twitch_config WHERE enabled = 1
            """)
            configs = await cursor.fetchall()

        for cfg in configs:
            (cid, guild_id, username, discord_ch_id,
             custom_msg, embed_data_str, ping_role_id,
             give_role_id, role_duration_hours,
             was_live) = cfg

            try:
                stream = await check_stream_live(
                    username, client_id, self._token)
                is_live_now = stream is not None

                # State unchanged
                if bool(is_live_now) == bool(was_live):
                    continue

                # Update is_live state
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        UPDATE twitch_config
                        SET is_live = ?
                        WHERE id = ?
                    """, (int(is_live_now), cid))
                    await db.commit()

                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                # Went live
                if is_live_now:
                    await self._handle_went_live(
                        guild, username, stream,
                        discord_ch_id, custom_msg,
                        embed_data_str, ping_role_id,
                        give_role_id, role_duration_hours)

                # Went offline
                else:
                    await self._handle_went_offline(
                        guild, username, give_role_id)

            except Exception as e:
                print(f"[TWITCH] Error for config {cid}: {e}")

    async def _handle_went_live(self, guild, username, stream,
                                  discord_ch_id, custom_msg,
                                  embed_data_str, ping_role_id,
                                  give_role_id, role_duration_hours):
        channel = guild.get_channel(int(discord_ch_id))
        if not channel:
            return

        title    = stream.get("title", "")
        game     = stream.get("game_name", "")
        viewers  = stream.get("viewer_count", 0)
        twitch_url = f"https://twitch.tv/{username}"

        content = ""
        if ping_role_id:
            role = guild.get_role(int(ping_role_id))
            if role:
                content = role.mention + " "

        if custom_msg:
            content += (custom_msg
                        .replace("{streamer}", username)
                        .replace("{title}", title)
                        .replace("{game}", game)
                        .replace("{url}", twitch_url))
        else:
            content += f"🔴 **{username}** is now LIVE!"

        # Build embed
        try:
            if embed_data_str:
                embed_data = json.loads(embed_data_str)
                color_str  = embed_data.get("color", "#9147FF")
            else:
                embed_data = {}
                color_str  = "#9147FF"

            try:
                color_int = int(color_str.strip("#"), 16)
            except Exception:
                color_int = 0x9147FF

            embed = discord.Embed(
                title=title or f"{username} is live!",
                url=twitch_url,
                color=color_int)
            embed.add_field(name="Game",    value=game or "Unknown")
            embed.add_field(name="Viewers", value=f"{viewers:,}")
            embed.add_field(name="Watch",   value=twitch_url,
                            inline=False)

            # Thumbnail from stream
            thumbnail = stream.get("thumbnail_url", "")
            if thumbnail:
                thumbnail = thumbnail.replace(
                    "{width}", "640").replace("{height}", "360")
                embed.set_image(url=thumbnail)

            await channel.send(content=content, embed=embed)
        except Exception as e:
            print(f"[TWITCH] Send error: {e}")
            await channel.send(content)

        # Give live role
        if give_role_id:
            import os
            client_id = os.getenv("TWITCH_CLIENT_ID")
            user_info = await get_user_info(
                username, client_id, self._token)
            if user_info:
                for member in guild.members:
                    if not member.bot:
                        role = guild.get_role(int(give_role_id))
                        if role:
                            can, warn = check_bot_role_position(
                                guild, role)
                            if can:
                                try:
                                    await member.add_roles(
                                        role,
                                        reason="Streamer went live")
                                    break
                                except Exception:
                                    pass

    async def _handle_went_offline(self, guild, username,
                                    give_role_id):
        """Remove live role when streamer goes offline."""
        if not give_role_id:
            return
        role = guild.get_role(int(give_role_id))
        if not role:
            return
        for member in guild.members:
            if role in member.roles:
                try:
                    await member.remove_roles(
                        role, reason="Stream ended")
                except Exception:
                    pass

    @check_streams.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="twitch_setup",
                          description="Set up Twitch live alerts")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_setup(
            self, interaction: discord.Interaction,
            twitch_username: str,
            discord_channel: discord.TextChannel,
            ping_role: discord.Role = None,
            give_role: discord.Role = None,
            custom_message: str = None):
        import os
        if not os.getenv("TWITCH_CLIENT_ID"):
            await interaction.response.send_message(
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET "
                "environment variables are not set on Railway.",
                ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO twitch_config
                    (guild_id, twitch_username,
                     discord_channel_id, custom_message,
                     ping_role_id, give_role_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (
                interaction.guild.id,
                twitch_username.lower(),
                discord_channel.id,
                custom_message,
                ping_role.id if ping_role else None,
                give_role.id if give_role else None,
            ))
            await db.commit()

        embed = discord.Embed(
            title="Twitch Alerts Set Up",
            color=0x9147FF)
        embed.add_field(name="Streamer",
                        value=twitch_username)
        embed.add_field(name="Posts to",
                        value=discord_channel.mention)
        if ping_role:
            embed.add_field(name="Pings",      value=ping_role.mention)
        if give_role:
            embed.add_field(name="Live Role",  value=give_role.mention)
        await interaction.response.send_message(
            embed=embed, ephemeral=True)

    @app_commands.command(name="twitch_remove",
                          description="Remove a Twitch alert")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_remove(self, interaction: discord.Interaction,
                             entry_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                DELETE FROM twitch_config
                WHERE id = ? AND guild_id = ?
            """, (entry_id, interaction.guild.id))
            await db.commit()
        await interaction.response.send_message(
            f"Removed Twitch config #{entry_id}.",
            ephemeral=True)

    @app_commands.command(name="twitch_list",
                          description="List Twitch alert configs")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, twitch_username,
                       discord_channel_id, is_live, enabled
                FROM twitch_config
                WHERE guild_id = ?
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No Twitch configs set up.", ephemeral=True)
            return

        embed = discord.Embed(title="Twitch Configs",
                              color=0x9147FF)
        for (cid, username, dch, is_live, enabled) in rows:
            status = "🔴 LIVE" if is_live else "⚫ Offline"
            active = "✅" if enabled else "❌"
            embed.add_field(
                name=f"#{cid} {active} — {username}",
                value=f"{status} → <#{dch}>",
                inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Twitch(bot))
