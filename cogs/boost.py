import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH

class Boost(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_config(self, guild_id):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS boost_config (
                    guild_id INTEGER PRIMARY KEY,
                    role1_id INTEGER,
                    role2_id INTEGER,
                    announce_channel_id INTEGER
                )
            """)
            await db.commit()
            cursor = await db.execute(
                "SELECT role1_id, role2_id, announce_channel_id FROM boost_config WHERE guild_id=?",
                (guild_id,))
            return await cursor.fetchone()

    async def update_boost_roles(self, member: discord.Member):
        config = await self.get_config(member.guild.id)
        if not config:
            return
        role1_id, role2_id, channel_id = config
        role1 = member.guild.get_role(role1_id) if role1_id else None
        role2 = member.guild.get_role(role2_id) if role2_id else None
        boost_count = member.premium_since is not None
        boosts = 0
        if member.premium_since:
            for entry in member.guild.premium_subscribers:
                if entry.id == member.id:
                    boosts = member.guild.premium_subscription_count
                    break
            boosts = 1
            try:
                if hasattr(member, '_user'):
                    pass
            except:
                pass
            premium_role = member.guild.premium_subscriber_role
            if premium_role and premium_role in member.roles:
                boosts = 1

        if boosts == 0:
            if role1 and role1 in member.roles:
                await member.remove_roles(role1)
            if role2 and role2 in member.roles:
                await member.remove_roles(role2)
        elif boosts >= 2:
            if role1 and role1 not in member.roles:
                await member.add_roles(role1)
            if role2 and role2 not in member.roles:
                await member.add_roles(role2)
        else:
            if role1 and role1 not in member.roles:
                await member.add_roles(role1)
            if role2 and role2 in member.roles:
                await member.remove_roles(role2)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since == after.premium_since:
            return
        config = await self.get_config(after.guild.id)
        if not config:
            return
        role1_id, role2_id, channel_id = config
        role1 = after.guild.get_role(role1_id) if role1_id else None
        role2 = after.guild.get_role(role2_id) if role2_id else None
        just_boosted = before.premium_since is None and after.premium_since is not None
        just_unboosted = before.premium_since is not None and after.premium_since is None

        if just_boosted:
            current_boosts = sum(1 for m in after.guild.premium_subscribers if m.id == after.id)
            if role1 and role1 not in after.roles:
                await after.add_roles(role1)
            if current_boosts >= 2 and role2 and role2 not in after.roles:
                await after.add_roles(role2)
            if channel_id:
                channel = after.guild.get_channel(channel_id)
                if channel:
                    embed = discord.Embed(
                        title="New Boost!",
                        description=f"{after.mention} just boosted the server! Thank you!",
                        color=discord.Color.pink()
                    )
                    if role1:
                        embed.add_field(name="Reward", value=role1.mention)
                    await channel.send(embed=embed)

        elif just_unboosted:
            remaining_boosts = sum(1 for m in after.guild.premium_subscribers if m.id == after.id)
            if remaining_boosts == 0:
                if role1 and role1 in after.roles:
                    await after.remove_roles(role1)
                if role2 and role2 in after.roles:
                    await after.remove_roles(role2)
                if channel_id:
                    channel = after.guild.get_channel(channel_id)
                    if channel:
                        embed = discord.Embed(
                            title="Boost Ended",
                            description=f"{after.mention}'s boost has ended. Boost rewards removed.",
                            color=discord.Color.red()
                        )
                        await channel.send(embed=embed)
            elif remaining_boosts == 1:
                if role2 and role2 in after.roles:
                    await after.remove_roles(role2)
                if channel_id:
                    channel = after.guild.get_channel(channel_id)
                    if channel:
                        embed = discord.Embed(
                            title="Boost Reduced",
                            description=f"{after.mention} removed one boost. Double boost role removed.",
                            color=discord.Color.orange()
                        )
                        await channel.send(embed=embed)

    @app_commands.command(name="boost_setup", description="Configure boost roles and announcement channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def boost_setup(self, interaction: discord.Interaction,
                          role1: discord.Role,
                          role2: discord.Role,
                          announce_channel: discord.TextChannel):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS boost_config (
                    guild_id INTEGER PRIMARY KEY,
                    role1_id INTEGER,
                    role2_id INTEGER,
                    announce_channel_id INTEGER
                )
            """)
            await db.execute("""
                INSERT OR REPLACE INTO boost_config (guild_id, role1_id, role2_id, announce_channel_id)
                VALUES (?, ?, ?, ?)
            """, (interaction.guild.id, role1.id, role2.id, announce_channel.id))
            await db.commit()
        embed = discord.Embed(title="Boost System Configured!", color=discord.Color.pink())
        embed.add_field(name="1st Boost Role", value=role1.mention)
        embed.add_field(name="2nd Boost Role", value=role2.mention)
        embed.add_field(name="Announcements", value=announce_channel.mention)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="boosters", description="See all current server boosters")
    async def boosters(self, interaction: discord.Interaction):
        boosters = interaction.guild.premium_subscribers
        if not boosters:
            await interaction.response.send_message("No one is boosting the server right now.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Server Boosters ({len(boosters)})",
            color=discord.Color.pink()
        )
        for booster in boosters[:20]:
            embed.add_field(name=booster.display_name, value=booster.mention, inline=True)
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Boost(bot))
