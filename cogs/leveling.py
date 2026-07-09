import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import time
import io
from database import DB_PATH
from utils.xp_calculator import (
    calculate_message_xp, calculate_voice_xp,
    xp_progress, get_leveling_config,
)
from utils.formatters import snapshot_user


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._xp_cooldowns: dict[tuple, float] = {}
        self._spam_tracker: dict[tuple, list[float]] = {}
        # Phase 3 / E1: voice XP is now driven by
        # cogs/activity_engine.py's activity_voice_tick event (see
        # on_activity_voice_tick below) instead of this cog running
        # its own poll loop, so there's no task to start/cancel here
        # anymore.

    # ─── SPAM DETECTION (P1 #12) ─────────────────────────
    # Frequency-based: N messages within X seconds = spam.
    # Penalizes XP instead of blocking messages (moderation.py
    # already handles actual mute/timeout enforcement).
    def _is_spamming(self, guild_id: int, user_id: int,
                      threshold: int, window_seconds: int) -> bool:
        key = (guild_id, user_id)
        now = time.time()
        times = self._spam_tracker.get(key, [])
        times = [t for t in times if now - t < window_seconds]
        times.append(now)
        self._spam_tracker[key] = times
        return len(times) >= threshold

    # ─── MESSAGE XP (Phase 3 / E1: now driven by the Activity
    # Engine's activity_message event instead of its own on_message
    # listener — word_count used to be computed here independently
    # of cogs/activity_engine.py's identical computation; now it's
    # computed once, centrally, and passed in. Everything else below
    # — the cooldown gate, spam-penalty ordering, multiplier lookup,
    # XP award, level-up announce — is unchanged from before.) ─────
    @commands.Cog.listener()
    async def on_activity_message(self, message: discord.Message,
                                   word_count: int):
        if message.author.bot or not message.guild:
            return

        config = await get_leveling_config(message.guild.id)
        if not config.get("enabled", 1):
            return

        guild_id = message.guild.id
        user_id  = message.author.id
        key      = (guild_id, user_id)
        now      = time.time()

        # P1 #12 FIX: spam detection must run on EVERY message,
        # BEFORE the XP-cooldown gate below — not after it.
        #
        # The previous ordering updated self._xp_cooldowns and
        # returned early whenever a message arrived inside the
        # cooldown window, which meant that message never reached
        # _is_spamming() at all. With the default settings
        # (xp_cooldown_seconds=30, spam_window_seconds=10,
        # spam_threshold=3), every message that could have counted
        # toward the spam threshold was filtered out by the cooldown
        # gate first — it was mathematically impossible to
        # accumulate 3 tracked messages inside a 10s window when
        # tracked messages were always >=30s apart. The anti-spam
        # feature existed in code but could never actually fire.
        #
        # Spam tracking now runs independently of the XP cooldown,
        # so rapid-fire messages get caught regardless of whether
        # they'd have earned XP anyway.
        if config.get("spam_detection_enabled", 1):
            threshold = int(config.get("spam_threshold", 3))
            window    = int(config.get("spam_window_seconds", 10))
            if self._is_spamming(guild_id, user_id, threshold, window):
                penalty = int(config.get("spam_xp_penalty", 10))
                if penalty > 0:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            INSERT INTO levels (guild_id, user_id, xp, level)
                            VALUES (?, ?, 0, 0)
                            ON CONFLICT(guild_id, user_id)
                            DO UPDATE SET xp = MAX(0, xp - ?)
                        """, (guild_id, user_id, penalty))
                        await db.commit()
                return

        cooldown = config.get("xp_cooldown_seconds", 30)
        last     = self._xp_cooldowns.get(key, 0)
        if now - last < cooldown:
            return
        self._xp_cooldowns[key] = now

        role_ids  = [r.id for r in message.author.roles]
        xp_to_add = await calculate_message_xp(
            guild_id, role_ids, word_count)

        if xp_to_add <= 0:
            return

        # Phase 3 / E2: XP granting + level-up detection + level
        # reward roles now go through the shared Reward Engine
        # instead of this cog doing its own raw INSERT/UPDATE and its
        # own inline level comparison — the exact same logic used to
        # be duplicated again below for voice XP. Announcing the
        # level-up in-channel stays here since that's leveling's own
        # UI concern, not something the engine should know about.
        from utils.reward_engine import give_reward
        result = await give_reward(
            self.bot, guild_id, user_id, "xp", amount=xp_to_add,
            reason="Message XP", source="leveling")

        if result.get("leveled_up"):
            await self._announce_levelup(
                message, result["new_level"], config)

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

    # ─── VOICE XP (Phase 3 / E1: now driven by the Activity
    # Engine's activity_voice_tick event instead of running its own
    # 60s poll loop over every guild/channel/member. The engine
    # already applies the raw disqualifiers (2+ real members present,
    # not AFK channel, not deafened) that used to live in this loop;
    # what's left here is leveling's own POLICY on top of a valid
    # tick — voice_xp_enabled, the require_unmuted choice, and the
    # actual XP math — exactly as before, just invoked once per tick
    # instead of leveling running its own duplicate poll. ──────────
    @commands.Cog.listener()
    async def on_activity_voice_tick(self, guild: discord.Guild,
                                      member: discord.Member,
                                      flags: dict):
        try:
            config = await get_leveling_config(guild.id)
            if not config.get("voice_xp_enabled", 1):
                return

            require_unmuted = config.get("voice_require_unmuted", 1)
            if require_unmuted and (flags.get("self_mute") or flags.get("mute")):
                return

            xp_per_min = config.get("voice_xp_per_minute", 3)
            xp_gain = calculate_voice_xp(1, xp_per_min)
            if xp_gain <= 0:
                return

            # Phase 3 / E2: same reward-engine handoff as message XP
            # above — no announcement on voice level-ups, matching the
            # pre-existing behavior (only chat XP announces).
            from utils.reward_engine import give_reward
            await give_reward(
                self.bot, guild.id, member.id, "xp", amount=xp_gain,
                reason="Voice XP", source="leveling")
        except Exception as e:
            print(f"[VOICE XP] Error for member {member.id} in "
                  f"guild {guild.id}: {e}")

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
