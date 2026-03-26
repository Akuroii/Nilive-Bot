import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
import json
import random
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso
from utils.permissions import check_bot_role_position


async def get_currency_name(guild_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT currency_name FROM guild_settings
            WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else "Coins"


async def give_reward(guild: discord.Guild,
                       member: discord.Member,
                       reward_type: str,
                       reward_value: str,
                       duration_hours: int = None):
    """
    Awards an event reward to a member.
    reward_type: 'coins', 'xp', 'role', 'temp_role'
    """
    from database import DB_PATH
    guild_id = guild.id
    user_id  = member.id

    if reward_type == "coins":
        amount = int(reward_value)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO economy (guild_id, user_id, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET balance = balance + ?
            """, (guild_id, user_id, amount, amount))
            await db.commit()

    elif reward_type == "xp":
        amount = int(reward_value)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = xp + ?
            """, (guild_id, user_id, amount, amount))
            await db.commit()

    elif reward_type in ("role", "temp_role"):
        role_id = int(reward_value)
        role    = guild.get_role(role_id)
        if role:
            can, warn = check_bot_role_position(guild, role)
            if can:
                try:
                    await member.add_roles(
                        role, reason="Event reward")
                except Exception:
                    pass
                if reward_type == "temp_role" and duration_hours:
                    expires_at = (
                        datetime.now(timezone.utc) +
                        timedelta(hours=duration_hours)).isoformat()
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            INSERT INTO temp_roles
                                (guild_id, user_id, role_id,
                                 expires_at, source)
                            VALUES (?, ?, ?, ?, 'event')
                        """, (guild_id, user_id,
                              role_id, expires_at))
                        await db.commit()
            else:
                print(f"[EVENTS] {warn}")


class ButtonRaceView(discord.ui.View):
    """First N users to click win."""

    def __init__(self, event_id: int, max_winners: int,
                 reward_type: str, reward_value: str,
                 reward_duration: int = None):
        super().__init__(timeout=300)
        self.event_id        = event_id
        self.max_winners     = max_winners
        self.reward_type     = reward_type
        self.reward_value    = reward_value
        self.reward_duration = reward_duration
        self.winners:  list[int] = []
        self.finished: bool      = False

    @discord.ui.button(label="🏁 Claim Reward!",
                       style=discord.ButtonStyle.green,
                       custom_id="event_claim")
    async def claim(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        if self.finished:
            await interaction.response.send_message(
                "This event has ended!", ephemeral=True)
            return
        if interaction.user.id in self.winners:
            await interaction.response.send_message(
                "You already claimed this reward!",
                ephemeral=True)
            return

        self.winners.append(interaction.user.id)
        snap = snapshot_user(interaction.user)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO event_winners
                    (event_id, guild_id, user_id,
                     user_display_name)
                VALUES (?, ?, ?, ?)
            """, (self.event_id, interaction.guild.id,
                  interaction.user.id,
                  snap["display_name"]))
            await db.commit()

        await give_reward(
            interaction.guild,
            interaction.user,
            self.reward_type,
            self.reward_value,
            self.reward_duration)

        currency = await get_currency_name(interaction.guild.id)
        if self.reward_type == "coins":
            reward_str = (f"**{int(self.reward_value):,}** "
                          f"{currency}")
        elif self.reward_type == "xp":
            reward_str = f"**{int(self.reward_value):,}** XP"
        else:
            reward_str = "your reward"

        await interaction.response.send_message(
            f"🎉 You won {reward_str}! "
            f"({len(self.winners)}/{self.max_winners})",
            ephemeral=True)

        if len(self.winners) >= self.max_winners:
            self.finished = True
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            await interaction.channel.send(
                "🏁 Event ended! All winners have claimed "
                "their rewards.")


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduled_events_task.start()

    def cog_unload(self):
        self.scheduled_events_task.cancel()

    @tasks.loop(minutes=15)
    async def scheduled_events_task(self):
        """Fires scheduled events at the right time."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, title, description,
                       type, reward_type, reward_value,
                       reward_duration_hours, max_winners,
                       channel_id, embed_data,
                       schedule_type, schedule_time,
                       random_min_hours, random_max_hours
                FROM events
                WHERE enabled = 1
                  AND schedule_type = 'scheduled'
                  AND schedule_time <= ?
            """, (now,))
            due = await cursor.fetchall()

        for ev in due:
            (eid, guild_id, title, desc, etype,
             reward_type, reward_value, reward_dur,
             max_winners, channel_id, embed_data_str,
             stype, stime, rmin, rmax) = ev

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(int(channel_id)) \
                if channel_id else None
            if not channel:
                continue

            await self._launch_event(
                channel, eid, title, desc,
                reward_type, reward_value,
                reward_dur, max_winners, embed_data_str)

            # Disable after firing
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE events SET enabled = 0
                    WHERE id = ?
                """, (eid,))
                await db.commit()

    async def _launch_event(self, channel, event_id, title,
                             desc, reward_type, reward_value,
                             reward_dur, max_winners,
                             embed_data_str):
        try:
            color_int = 0x7c5cbf
            if embed_data_str:
                try:
                    ed        = json.loads(embed_data_str)
                    color_int = int(
                        ed.get("color", "#7c5cbf").strip("#"),
                        16)
                except Exception:
                    pass

            embed = discord.Embed(
                title=f"🎯 {title}",
                description=desc or "Click the button to win!",
                color=color_int)
            embed.add_field(name="Winners", value=str(max_winners))

            if reward_type == "coins":
                embed.add_field(name="Reward",
                                value=f"🪙 {int(reward_value):,} coins")
            elif reward_type == "xp":
                embed.add_field(name="Reward",
                                value=f"⭐ {int(reward_value):,} XP")
            else:
                embed.add_field(name="Reward",
                                value=f"🎁 {reward_value}")

            view = ButtonRaceView(
                event_id=event_id,
                max_winners=max_winners,
                reward_type=reward_type,
                reward_value=reward_value,
                reward_duration=reward_dur)

            await channel.send(embed=embed, view=view)

        except Exception as e:
            print(f"[EVENTS] Launch error: {e}")

    @scheduled_events_task.before_loop
    async def before_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="event_create",
                          description="Create and launch a button race event")
    @app_commands.checks.has_permissions(administrator=True)
    async def event_create(
            self, interaction: discord.Interaction,
            title: str,
            reward_type: str,
            reward_value: str,
            max_winners: int = 3,
            description: str = "Click the button to win!",
            channel: discord.TextChannel = None,
            duration_hours: int = None):
        target = channel or interaction.channel

        valid_types = ["coins", "xp", "role", "temp_role"]
        if reward_type not in valid_types:
            await interaction.response.send_message(
                f"reward_type must be one of: "
                f"{', '.join(valid_types)}",
                ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO events
                    (guild_id, title, description, type,
                     reward_type, reward_value,
                     reward_duration_hours, max_winners,
                     channel_id, enabled)
                VALUES (?, ?, ?, 'button_race', ?, ?, ?, ?, ?, 1)
            """, (
                interaction.guild.id, title, description,
                reward_type, reward_value,
                duration_hours, max_winners,
                target.id,
            ))
            await db.commit()
            event_id = cursor.lastrowid

        await self._launch_event(
            target, event_id, title, description,
            reward_type, reward_value,
            duration_hours, max_winners, None)

        await interaction.response.send_message(
            f"Event launched in {target.mention}!",
            ephemeral=True)

    @app_commands.command(name="event_list",
                          description="List recent events")
    @app_commands.checks.has_permissions(administrator=True)
    async def event_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, title, reward_type, reward_value,
                       max_winners, enabled, created_at
                FROM events
                WHERE guild_id = ?
                ORDER BY created_at DESC LIMIT 10
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No events yet.", ephemeral=True)
            return

        embed = discord.Embed(title="🎯 Events", color=0x7c5cbf)
        for (eid, title, rtype, rval,
             winners, enabled, ts) in rows:
            status = "✅ Active" if enabled else "⚫ Ended"
            embed.add_field(
                name=f"#{eid} — {title}",
                value=(f"{status} | {rtype}: {rval} | "
                       f"Max {winners} winners | "
                       f"{str(ts)[:10] if ts else '?'}"),
                inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Events(bot))
