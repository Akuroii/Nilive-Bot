"""
Server Tag features — Feature 1: Tag-Loyalty Mission.

A mission (dashboard-created) asks members to wear this server's own
Discord "server tag" (Member.primary_guild, added in discord.py 2.6.0).
Confirming is a live check at press-time; the actual payout is decided
by a SECOND, independent live check at mission end — never from cached
state — so wearing the tag just long enough to confirm, then removing
it, does not pay out. This is the one piece of this feature that isn't
allowed to take a shortcut.

Deliberately its own cog, own tables (tag_missions /
tag_mission_participants), no shared state with cogs/tagpartners.py
(Feature 2) — per spec, the two features stay independent all the way
down to the code, not just the product surface.
"""
import discord
from discord.ext import commands, tasks
import aiosqlite

from database import DB_PATH
from utils.reward_engine import give_reward, RewardError


class TagMissionConfirmView(discord.ui.View):
    """
    Persistent (timeout=None) view, one instance per mission. custom_id
    is namespaced per mission_id so more than one mission can be live
    at once without their buttons colliding. Fully self-contained — no
    reference to the cog/bot needed, since a confirm only ever touches
    interaction.guild / interaction.user (already a full Member in a
    guild interaction) and the DB.
    """

    def __init__(self, mission_id: int):
        super().__init__(timeout=None)
        self.mission_id = mission_id
        button = discord.ui.Button(
            label="Confirm Participation",
            style=discord.ButtonStyle.primary,
            custom_id=f"tagmission_confirm:{mission_id}",
            emoji="🏷️",
        )
        button.callback = self._on_confirm
        self.add_item(button)

    async def _on_confirm(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT confirm_message, not_wearing_message, already_message, status
                FROM tag_missions WHERE id = ? AND guild_id = ?
            """, (self.mission_id, guild.id))
            mission = await cursor.fetchone()

        if not mission or mission[3] != "active":
            await interaction.response.send_message(
                "This mission isn't currently active.", ephemeral=True)
            return

        confirm_message, not_wearing_message, already_message, _ = mission

        primary = member.primary_guild
        if not primary or not primary.identity_enabled or primary.identity_guild_id != guild.id:
            await interaction.response.send_message(not_wearing_message, ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute("""
                    INSERT INTO tag_mission_participants (mission_id, guild_id, user_id)
                    VALUES (?, ?, ?)
                """, (self.mission_id, guild.id, member.id))
                await db.commit()
                await interaction.response.send_message(confirm_message, ephemeral=True)
            except aiosqlite.IntegrityError:
                # UNIQUE(mission_id, user_id) already hit -- this IS the
                # "pressed it twice" check, not a separate lookup.
                await interaction.response.send_message(already_message, ephemeral=True)


class TagMissions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.mission_poll.start()

    def cog_unload(self):
        self.mission_poll.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        # Restore persistent views for missions already live when the
        # bot (re)connects -- same restore-from-DB-on-ready pattern
        # cogs/reactionroles.py already uses for its own persistent
        # views.
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id FROM tag_missions
                WHERE status = 'active' AND message_id IS NOT NULL
            """)
            rows = await cursor.fetchall()
        for (mission_id,) in rows:
            self.bot.add_view(TagMissionConfirmView(mission_id))

    @tasks.loop(minutes=5)
    async def mission_poll(self):
        await self._start_due_missions()
        await self._end_due_missions()

    @mission_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _start_due_missions(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, title, channel_id
                FROM tag_missions
                WHERE status = 'scheduled' AND starts_at <= CURRENT_TIMESTAMP
            """)
            due = await cursor.fetchall()

        for mission_id, guild_id, title, channel_id in due:
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild and channel_id else None

            if not channel:
                print(f"[TAGMISSIONS] mission {mission_id}: no valid channel "
                      f"configured, marking active without posting")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE tag_missions SET status='active' WHERE id=?",
                        (mission_id,))
                    await db.commit()
                continue

            view = TagMissionConfirmView(mission_id)
            embed = discord.Embed(
                title=f"🏷️ {title}",
                description=("Wear this server's tag and press the button "
                              "below to confirm participation."),
                color=discord.Color.blurple(),
            )
            try:
                msg = await channel.send(embed=embed, view=view)
            except discord.HTTPException as e:
                print(f"[TAGMISSIONS] mission {mission_id}: failed to post: {e}")
                continue

            self.bot.add_view(view, message_id=msg.id)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE tag_missions SET status='active', message_id=?
                    WHERE id=?
                """, (msg.id, mission_id))
                await db.commit()

    async def _end_due_missions(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, reward_type, reward_amount, reward_role_id,
                       reward_duration_hours, success_message, failure_message
                FROM tag_missions
                WHERE status = 'active' AND ends_at <= CURRENT_TIMESTAMP
            """)
            due = await cursor.fetchall()

        for (mission_id, guild_id, reward_type, reward_amount, reward_role_id,
             reward_duration_hours, success_message, failure_message) in due:
            await self._resolve_mission(
                mission_id, guild_id, reward_type, reward_amount,
                reward_role_id, reward_duration_hours,
                success_message, failure_message)

    async def _resolve_mission(self, mission_id, guild_id, reward_type,
                                reward_amount, reward_role_id,
                                reward_duration_hours, success_message,
                                failure_message):
        guild = self.bot.get_guild(guild_id)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id FROM tag_mission_participants
                WHERE mission_id = ? AND outcome IS NULL
            """, (mission_id,))
            participants = await cursor.fetchall()

        for (user_id,) in participants:
            outcome = "removed_tag"
            member = None

            if guild:
                try:
                    # Live REST fetch, deliberately not the member
                    # cache -- this is the check that decides whether
                    # the reward pays out, so it has to reflect right
                    # now, not whatever primary_guild looked like the
                    # last time the gateway happened to update it.
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    member = None
                except discord.HTTPException as e:
                    print(f"[TAGMISSIONS] mission {mission_id} fetch_member "
                          f"failed user={user_id}: {e}")
                    member = None

            if member:
                primary = member.primary_guild
                if (primary and primary.identity_enabled
                        and primary.identity_guild_id == guild_id):
                    try:
                        result = await give_reward(
                            self.bot, guild_id, user_id,
                            reward_type=reward_type,
                            amount=reward_amount,
                            role_id=reward_role_id,
                            duration_hours=reward_duration_hours,
                            reason="Tag-loyalty mission reward",
                            source="tag_mission",
                        )
                        if result.get("success"):
                            outcome = "rewarded"
                        else:
                            print(f"[TAGMISSIONS] mission {mission_id} reward "
                                  f"failed user={user_id}: {result.get('error')}")
                    except RewardError as e:
                        print(f"[TAGMISSIONS] mission {mission_id} reward "
                              f"config error: {e}")

                try:
                    await member.send(
                        success_message if outcome == "rewarded" else failure_message)
                except discord.Forbidden:
                    pass

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE tag_mission_participants
                    SET outcome=?, resolved_at=CURRENT_TIMESTAMP
                    WHERE mission_id=? AND user_id=?
                """, (outcome, mission_id, user_id))
                await db.commit()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tag_missions SET status='completed' WHERE id=?",
                (mission_id,))
            await db.commit()


async def setup(bot):
    await bot.add_cog(TagMissions(bot))
