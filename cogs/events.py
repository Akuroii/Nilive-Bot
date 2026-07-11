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


async def get_currency_name(guild_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT currency_name FROM guild_settings
            WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else "Coins"


async def give_reward(bot: discord.Client,
                       guild: discord.Guild,
                       member: discord.Member,
                       reward_type: str,
                       reward_value: str,
                       duration_hours: int = None):
    """
    Awards an event reward to a member.
    reward_type: 'coins', 'diamonds', 'xp', 'role', 'temp_role', 'item'

    Phase 3 / E2: this used to duplicate the exact same coin/xp/role/
    temp_role granting logic that also lived in cogs/shop.py and
    cogs/leveling.py, each with its own small differences (this one,
    for instance, never actually checked add_roles() for failure
    beyond a bare except-pass, and its XP grant didn't do level-up
    detection at all — a member could hit a new level from an event
    reward and never get their level-up rewards or announcement).
    Now a thin wrapper around utils.reward_engine.give_reward(), the
    one place all of that logic lives.

    Phase 3 / E4 CLOSEOUT: 'item' rewards now route through the same
    engine call into utils/inventory.py — reward_value is used as the
    item_name and duration_hours is ignored for items (they don't
    expire). This mirrors how cogs/shop.py's "Custom" item type
    already delivers into Inventory instead of a no-op.
    """
    from utils.reward_engine import give_reward as _engine_give_reward

    if reward_type == "item":
        result = await _engine_give_reward(
            bot, guild.id, member.id, "item",
            amount=1, item_name=reward_value, item_type="event_drop",
            reason=f"Event reward: {reward_value}",
            source="event",
        )
    else:
        result = await _engine_give_reward(
            bot, guild.id, member.id, reward_type,
            amount=reward_value if reward_type in ("coins", "diamonds", "xp") else None,
            role_id=reward_value if reward_type in ("role", "temp_role") else None,
            duration_hours=duration_hours,
            reason="Event reward",
            source="event",
        )
    if not result.get("success"):
        print(f"[EVENTS] Reward grant failed: {result.get('error')}")
    return result


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
            interaction.client,
            interaction.guild,
            interaction.user,
            self.reward_type,
            self.reward_value,
            self.reward_duration)

        currency = await get_currency_name(interaction.guild.id)
        if self.reward_type == "coins":
            reward_str = (f"**{int(self.reward_value):,}** "
                          f"{currency}")
        elif self.reward_type == "diamonds":
            reward_str = f"**{int(self.reward_value):,}** 💎 Diamonds"
        elif self.reward_type == "xp":
            reward_str = f"**{int(self.reward_value):,}** XP"
        elif self.reward_type == "item":
            reward_str = f"**{self.reward_value}**"
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
        """
        Fires scheduled events at the right time.

        PHASE 2 FIX: the per-event body (guild/channel lookup,
        _launch_event, then the UPDATE that disables the event) had
        no try/except of its own. _launch_event() already guards its
        own internals, but a bad row, a transient DB error on the
        "disable after firing" UPDATE, or literally any other
        unexpected exception here would propagate up through
        tasks.loop and stop the whole 15-minute loop from ever
        rescheduling — silently killing scheduled events for every
        guild until the bot restarts, same class of bug already fixed
        in cogs/leveling.py's voice_xp_task. Each event is now
        isolated so one bad row can't take the rest down with it.
        """
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
            try:
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
            except Exception as e:
                print(f"[EVENTS] scheduled_events_task error for "
                      f"event {ev[0] if ev else '?'}: {e}")

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
            elif reward_type == "diamonds":
                embed.add_field(name="Reward",
                                value=f"💎 {int(reward_value):,} Diamonds")
            elif reward_type == "xp":
                embed.add_field(name="Reward",
                                value=f"⭐ {int(reward_value):,} XP")
            elif reward_type == "item":
                embed.add_field(name="Reward",
                                value=f"🎁 {reward_value} (item)")
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

        # Phase 3 / E4 CLOSEOUT: 'item' added as a valid reward_type —
        # it delivers into the winner's Inventory (utils/inventory.py)
        # via the Reward Engine instead of only ever supporting
        # coins/diamonds/xp/role/temp_role. For 'item', reward_value
        # is the item name (e.g. "Golden Ticket"), not a numeric ID.
        valid_types = ["coins", "diamonds", "xp", "role", "temp_role", "item"]
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
