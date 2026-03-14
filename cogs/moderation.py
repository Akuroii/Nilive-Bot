import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("You can't kick someone with an equal or higher role!", ephemeral=True)
            return
        await member.kick(reason=reason)
        embed = discord.Embed(title="Member Kicked", color=discord.Color.orange())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("You can't ban someone with an equal or higher role!", ephemeral=True)
            return
        await member.ban(reason=reason)
        embed = discord.Embed(title="Member Banned", color=discord.Color.red())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=reason)
            embed = discord.Embed(title="Member Unbanned", color=discord.Color.green())
            embed.add_field(name="User", value=str(user))
            embed.add_field(name="Reason", value=reason)
            await interaction.response.send_message(embed=embed)
        except:
            await interaction.response.send_message("User not found or not banned.", ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      minutes: int, reason: str = "No reason provided"):
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("You can't timeout someone with an equal or higher role!", ephemeral=True)
            return
        from datetime import timedelta
        duration = timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        embed = discord.Embed(title="Member Timed Out", color=discord.Color.orange())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Duration", value=f"{minutes} minutes")
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        await member.timeout(None)
        await interaction.response.send_message(f"Removed timeout from {member.mention}", ephemeral=True)

    @app_commands.command(name="purge", description="Delete multiple messages at once")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int):
        if amount < 1 or amount > 100:
            await interaction.response.send_message("Amount must be between 1 and 100.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    guild_id INTEGER, user_id INTEGER, reason TEXT, timestamp TEXT
                )
            """)
            from datetime import datetime
            await db.execute("""
                INSERT INTO warnings (guild_id, user_id, reason, timestamp)
                VALUES (?, ?, ?, ?)
            """, (interaction.guild.id, member.id, reason, datetime.utcnow().isoformat()))
            await db.commit()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id))
            count = (await cursor.fetchone())[0]
        embed = discord.Embed(title="Member Warned", color=discord.Color.yellow())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Total Warnings", value=str(count))
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)
        try:
            await member.send(f"You have been warned in **{interaction.guild.name}**\nReason: {reason}\nTotal warnings: {count}")
        except:
            pass

    @app_commands.command(name="warnings", description="Check warnings for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT reason, timestamp FROM warnings
                WHERE guild_id=? AND user_id=?
                ORDER BY timestamp DESC
            """, (interaction.guild.id, member.id))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Warnings for {member.display_name}", color=discord.Color.yellow())
        for i, (reason, timestamp) in enumerate(rows, 1):
            embed.add_field(name=f"Warning #{i} — {timestamp[:10]}", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearwarnings", description="C
