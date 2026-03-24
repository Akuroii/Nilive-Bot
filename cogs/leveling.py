import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import time
import io
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
        self._xp_cooldowns: dict[tuple, float] = {}
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

        cooldown = config.get("xp_cooldown_seconds", 30)
        last     = self._xp_cooldowns.get(key, 0)
        if now - last < cooldown:
            return
        self._xp_cooldowns[key] = now

        word_count = len(message.content.split())
        role_ids   = [r.id for r in message.author.roles]
        xp_to_add  = await calculate_message_xp(
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

        if new_level > old_level:
            await self._announce_levelup(
                message, new_level, config)
            await check_and_award_level_rewards(
                self.bot, message.author,
                guild_id, old_level, new_level)

    async def _announce_levelup(self, message: discord.Message,
                                 new_level: int, config: dict):
        if not config.get("levelup_announce", 1):
            return
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
        for guild in self.bot.guilds:
            config = await get_leveling_config(guild.id)
            if not config.get("voice_xp_enabled", 1):
                continue
            xp_per_min = config.get("voice_xp_per_minute", 3)
            require_unmuted = config.get("voice_require_unmuted", 1)
            afk_channel_id  = guild.afk_channel.id if guild.afk_channel else None

            for channel in guild.voice_channels:
                if channel.id == afk_channel_id:
                    continue

                real_members = [
                    m for m in channel.members if not m.bot]

                if len(real_members) < 2:
                    continue

                for member in real_members:
                    if member.voice.self_deaf or member.voice.deaf:
                        continue
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

    # ─── RANK COMMAND (Pillow Image Card) ───────────────
    @app_commands.command(name="rank",
                          description="View your rank card")
    async def rank(self, interaction: discord.Interaction,
                   member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.defer()

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT xp, level FROM levels
                WHERE guild_id = ? AND user_id = ?
            """, (interaction.guild.id, member.id))
            row = await cursor.fetchone()
            if not row:
                await interaction.followup.send(
                    f"{member.mention} has no XP yet.")
                return
            xp, level = row
            rank_cursor = await db.execute("""
                SELECT COUNT(*) FROM levels
                WHERE guild_id = ? AND xp > ?
            """, (interaction.guild.id, xp))
            rank = (await rank_cursor.fetchone())[0] + 1

        lvl, current, needed = xp_progress(xp)

        try:
            import aiohttp
            from PIL import Image, ImageDraw, ImageFont, ImageFilter

            W, H = 800, 200
            card = Image.new("RGBA", (W, H), (20, 20, 30, 255))
            draw = ImageDraw.Draw(card)

            # Gradient background (violet to dark)
            for y in range(H):
                r = int(20 + (40 - 20) * y / H)
                g = int(20 + (20 - 20) * y / H)
                b = int(30 + (50 - 30) * y / H)
                draw.line([(0, y), (W, y)], fill=(r, g, b, 255))

            # Avatar
            avatar_size = 120
            avatar_x, avatar_y = 24, 40
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(
                            str(member.display_avatar.url)) as resp:
                        avatar_bytes = await resp.read()
                avatar_img = Image.open(
                    io.BytesIO(avatar_bytes)).convert("RGBA")
                avatar_img = avatar_img.resize(
                    (avatar_size, avatar_size), Image.LANCZOS)

                # Circular mask
                mask = Image.new("L", (avatar_size, avatar_size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse(
                    [0, 0, avatar_size, avatar_size], fill=255)
                avatar_img.putalpha(mask)

                # Accent ring
                ring_img = Image.new(
                    "RGBA", (avatar_size + 8, avatar_size + 8),
                    (0, 0, 0, 0))
                ring_draw = ImageDraw.Draw(ring_img)
                ring_draw.ellipse(
                    [0, 0, avatar_size + 7, avatar_size + 7],
                    outline=(124, 92, 191, 255), width=4)
                card.paste(ring_img, (avatar_x - 4, avatar_y - 4),
                           ring_img)
                card.paste(avatar_img, (avatar_x, avatar_y),
                           avatar_img)
            except Exception:
                draw.ellipse(
                    [avatar_x, avatar_y,
                     avatar_x + avatar_size,
                     avatar_y + avatar_size],
                    fill=(124, 92, 191, 255))

            text_x = avatar_x + avatar_size + 24

            # Fonts
            try:
                font_lg = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/"
                    "DejaVuSans-Bold.ttf", 28)
                font_md = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/"
                    "DejaVuSans.ttf", 20)
                font_sm = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/"
                    "DejaVuSans.ttf", 16)
            except Exception:
                font_lg = ImageFont.load_default()
                font_md = font_lg
                font_sm = font_lg

            # Name
            draw.text((text_x, 30),
                      member.display_name[:24],
                      fill=(255, 255, 255), font=font_lg)

            # Rank + Level
            draw.text((text_x, 68), f"Rank #{rank}",
                      fill=(124, 92, 191), font=font_md)
            draw.text((W - 140, 30), f"Level {lvl}",
                      fill=(255, 255, 255), font=font_lg)

            # XP text
            draw.text((text_x, 102),
                      f"{current:,} / {needed:,} XP",
                      fill=(160, 160, 160), font=font_sm)

            # Progress bar
            bar_x, bar_y = text_x, 130
            bar_w = W - text_x - 24
            bar_h = 16
            draw.rounded_rectangle(
                [bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                radius=8, fill=(50, 50, 70, 255))
            fill_w = int(bar_w * (current / needed)) if needed else bar_w
            if fill_w > 0:
                draw.rounded_rectangle(
                    [bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                    radius=8, fill=(124, 92, 191, 255))

            # Total XP
            draw.text((text_x, bar_y + bar_h + 10),
                      f"Total XP: {xp:,}",
                      fill=(120, 120, 140), font=font_sm)

            # Export
            buf = io.BytesIO()
            card.save(buf, format="PNG")
            buf.seek(0)
            file = discord.File(buf, filename="rank.png")
            await interaction.followup.send(file=file)

        except ImportError:
            # Fallback if Pillow not available
            lvl, current, needed = xp_progress(xp)
            bar_filled = int((current / needed) * 20) if needed else 20
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            embed = discord.Embed(
                title=f"Rank — {member.display_name}",
                color=0x7c5cbf)
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Rank",     value=f"#{rank}")
            embed.add_field(name="Level",    value=str(lvl))
            embed.add_field(name="Total XP", value=f"{xp:,}")
            embed.add_field(
                name=f"Progress ({current:,}/{needed:,} XP)",
                value=f"`{bar}`", inline=False)
            await interaction.followup.send(embed=embed)

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
