import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import time
from database import DB_PATH
from utils.xp_calculator import (
    calculate_message_xp, calculate_voice_xp,
    xp_progress, check_and_award_level_rewards,
    get_leveling_config,
)
from utils.formatters import snapshot_user


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Anti-spam: tracks last XP award time per (guild_id, user_id)
        self._xp_cooldowns: dict[tuple, float] = {}
        # Voice XP: tracks when each user joined voice
        self._voice_join_times: dict[tuple, float] = {}
        self.voice_xp_task.start()

    def cog_unload(self):
        self.voice_xp_task.cancel()

    # ─── MESSAGE XP ─────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        config = await get_leveling_config(message.guild.id)
        if not config.get("enabled", 1):
            return

        guild_id = message.guild.id
        user_id  = message.author.id
        key      = (guild_id, user_id)
        now      = time.time()

        # Anti-spam cooldown (Rule 4 equivalent for messages)
        cooldown = config.get("xp_cooldown_seconds", 30)
        last     = self._xp_cooldowns.get(key, 0)
        if now - last < cooldown:
            return
        self._xp_cooldowns[key] = now

        # Calculate XP with anti-inflation multiplier (Rule 3)
        word_count    = len(message.content.split())
        role_ids      = [r.id for r in message.author.roles]
        xp_to_add     = await calculate_message_xp(
            guild_id, role_ids, word_count)

        if xp_to_add <= 0:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT xp, level FROM levels
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            row = await cursor.fetchone()
            old_xp    = row[0] if row else 0
            old_level = row[1] if row else 0
            new_xp    = old_xp + xp_to_add
            new_level, _, _ = xp_progress(new_xp)
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = ?, level = ?
            """, (guild_id, user_id, new_xp, new_level,
                  new_xp, new_level))
            await db.commit()

        # Level up announcement
        if new_level > old_level:
            await self._announce_levelup(
                message, new_level, config)
            await check_and_award_level_rewards(
                self.bot, message.author,
                guild_id, old_level, new_level)

    async def _announce_levelup(self, message: discord.Message,
                                 new_level: int, config: dict):
        channel_id = config.get("levelup_channel_id")
        channel    = (message.guild.get_channel(int(channel_id))
                      if channel_id else message.channel)
        if not channel:
            return
        custom_msg = config.get("levelup_message")
        if custom_msg:
            text = (custom_msg
                    .replace("{user}", message.author.mention)
                    .replace("{level}", str(new_level))
                    .replace("{name}", message.author.display_name))
            await channel.send(text)
        else:
            embed = discord.Embed(
                description=(f"🎉 {message.author.mention} reached "
                             f"**Level {new_level}**!"),
                color=0x7c5cbf)
            await channel.send(embed=embed)

    # ─── VOICE XP TASK (Rule 4 — Voice Farming Guard) ───
    @tasks.loop(seconds=60)
    async def voice_xp_task(self):
        """
        Awards voice XP every 60 seconds.
        Rule 4 guards: alone, deafened, AFK channel, muted.
        """
        for guild in self.bot.guilds:
            config = await get_leveling_config(guild.id)
            if not config.get("voice_xp_enabled", 1):
                continue
            xp_per_min = config.get("voice_xp_per_minute", 3)
            require_unmuted = config.get("voice_require_unmuted", 1)
            afk_channel_id  = guild.afk_channel.id if guild.afk_channel else None

            for channel in guild.voice_channels:
                # Skip AFK channel (Rule 4)
                if channel.id == afk_channel_id:
                    continue

                # Get non-bot members
                real_members = [
                    m for m in channel.members if not m.bot]

                # No XP if alone in channel (Rule 4)
                if len(real_members) < 2:
                    continue

                for member in real_members:
                    # No XP if deafened (Rule 4)
                    if member.voice.self_deaf or member.voice.deaf:
                        continue
                    # No XP if muted (configurable Rule 4)
                    if require_unmuted:
                        if member.voice.self_mute or member.voice.mute:
                            continue

                    xp_gain = calculate_voice_xp(1, xp_per_min)
                    if xp_gain <= 0:
                        continue

                    async with aiosqlite.connect(DB_PATH) as db:
                        cursor = await db.execute("""
                            SELECT xp, level FROM levels
                            WHERE guild_id = ? AND user_id = ?
                        """, (guild.id, member.id))
                        row = await cursor.fetchone()
                        old_xp    = row[0] if row else 0
                        old_level = row[1] if row else 0
                        new_xp    = old_xp + xp_gain
                        new_level, _, _ = xp_progress(new_xp)
                        await db.execute("""
                            INSERT INTO levels (guild_id, user_id, xp, level)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(guild_id, user_id)
                            DO UPDATE SET xp = ?, level = ?
                        """, (guild.id, member.id, new_xp, new_level,
                              new_xp, new_level))
                        await db.commit()

                    # Also update voice_sessions for MVP
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            INSERT INTO voice_sessions
                                (guild_id, user_id, join_time)
                            VALUES (?, ?, ?)
                            ON CONFLICT(guild_id, user_id)
                            DO UPDATE SET join_time = join_time
                        """, (guild.id, member.id, time.time()))
                        await db.commit()

                    if new_level > old_level:
                        await check_and_award_level_rewards(
                            self.bot, member,
                            guild.id, old_level, new_level)

    @voice_xp_task.before_loop
    async def before_voice_task(self):
        await self.bot.wait_until_ready()

    # ─── RANK COMMAND ───────────────────────────────────
    @app_commands.command(name="rank",
                          description="View your rank and XP")
    async def rank(self, interaction: discord.Interaction,
                   member: discord.Member = None):
        member = member or interaction.user
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT xp, level FROM levels
                WHERE guild_id = ? AND user_id = ?
            """, (interaction.guild.id, member.id))
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(
                    f"{member.mention} has no XP yet.",
                    ephemeral=True)
                return
            xp, level = row
            rank_cursor = await db.execute("""
                SELECT COUNT(*) FROM levels
                WHERE guild_id = ? AND xp > ?
            """, (interaction.guild.id, xp))
            rank = (await rank_cursor.fetchone())[0] + 1

        lvl, current, needed = xp_progress(xp)
        bar_filled = int((current / needed) * 20) if needed else 20
        bar = "█" * bar_filled + "░" * (20 - bar_filled)

        embed = discord.Embed(
            title=f"Rank — {member.display_name}",
            color=0x7c5cbf)
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Rank",  value=f"#{rank}")
        embed.add_field(name="Level", value=str(lvl))
        embed.add_field(name="Total XP", value=f"{xp:,}")
        embed.add_field(
            name=f"Progress ({current:,}/{needed:,} XP)",
            value=f"`{bar}`",
            inline=False)
        await interaction.response.send_message(embed=embed)

    # ─── LEADERBOARD ────────────────────────────────────
    @app_commands.command(name="leaderboard",
                          description="View the XP leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                WHERE guild_id = ?
                ORDER BY xp DESC LIMIT 10
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No XP data yet.", ephemeral=True)
            return

        embed = discord.Embed(title="⭐ XP Leaderboard",
                              color=0x7c5cbf)
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, xp, level) in enumerate(rows, 1):
            medal  = medals[i-1] if i <= 3 else f"#{i}"
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            embed.add_field(
                name=f"{medal} {name}",
                value=f"Level {level} • {xp:,} XP",
                inline=False)
        await interaction.response.send_message(embed=embed)

    # ─── SET XP (admin) ─────────────────────────────────
    @app_commands.command(name="setxp",
                          description="Set XP for a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setxp(self, interaction: discord.Interaction,
                    member: discord.Member, xp: int):
        new_level, _, _ = xp_progress(xp)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = ?, level = ?
            """, (interaction.guild.id, member.id, xp, new_level,
                  xp, new_level))
            await db.commit()
        await interaction.response.send_message(
            f"Set {member.mention}'s XP to {xp:,} (Level {new_level}).",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
