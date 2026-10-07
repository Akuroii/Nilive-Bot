"""
Server Tag features — Feature 2: Cross-Server Join Reward.

On join, checks the new member's Discord "server tag" (Member.primary_guild,
added in discord.py 2.6.0) against this guild's configured partner-server
list (dashboard-managed, tag_partner_rewards). A match grants a one-time
reward via the shared reward_engine; tag_join_reward_log makes it one-time
per (guild, partner, user) so a leave-and-rejoin can't farm it.

Deliberately its own cog with its own on_member_join listener, separate
from cogs/welcome.py's — same multi-listener pattern main.py/botprofile.py
already use for on_guild_join. No shared state with cogs/tagmissions.py
(Feature 1) — separate tables, separate listener, per spec.
"""
import discord
from discord.ext import commands
import aiosqlite

from database import DB_PATH
from utils.reward_engine import give_reward, RewardError


class TagPartners(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        primary = member.primary_guild
        if not primary or not primary.identity_enabled or not primary.identity_guild_id:
            return

        partner_guild_id = primary.identity_guild_id

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT reward_type, reward_amount, reward_role_id, welcome_message
                FROM tag_partner_rewards
                WHERE guild_id = ? AND partner_guild_id = ? AND enabled = 1
            """, (member.guild.id, partner_guild_id))
            config_row = await cursor.fetchone()

        if not config_row:
            return

        reward_type, reward_amount, reward_role_id, welcome_message = config_row

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT 1 FROM tag_join_reward_log
                WHERE guild_id = ? AND partner_guild_id = ? AND user_id = ?
            """, (member.guild.id, partner_guild_id, member.id))
            already_rewarded = await cursor.fetchone()

        if already_rewarded:
            return

        try:
            result = await give_reward(
                self.bot, member.guild.id, member.id,
                reward_type=reward_type,
                amount=reward_amount,
                role_id=reward_role_id,
                reason="Cross-server tag partner reward",
                source="tag_partner",
            )
        except RewardError as e:
            print(f"[TAGPARTNERS] reward config error guild={member.guild.id} "
                  f"partner={partner_guild_id}: {e}")
            return

        if not result.get("success"):
            print(f"[TAGPARTNERS] reward failed guild={member.guild.id} "
                  f"user={member.id}: {result.get('error')}")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR IGNORE INTO tag_join_reward_log
                    (guild_id, partner_guild_id, user_id)
                VALUES (?, ?, ?)
            """, (member.guild.id, partner_guild_id, member.id))
            await db.commit()

        if welcome_message:
            try:
                await member.send(welcome_message)
            except discord.Forbidden:
                pass  # DMs closed — reward is already granted regardless


async def setup(bot):
    await bot.add_cog(TagPartners(bot))
