import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timedelta
from database import DB_PATH

async def log_action(guild: discord.Guild, action: str, moderator: discord.Member,
                     target: discord.Member, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mod_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                action TEXT,
                moderator_id INTEGER,
                target_id INTEGER,
                reason TEXT,
                timestamp TEXT
            )
        """)
        await db.execute("""
            INSERT INTO mod_logs (guild_id, action, moderator_id, target_id, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild.id, action, moderator.id, target.id, reason,
              datetime.utcnow().isoformat()))
        await db.commit()

async def get_disabled_commands(guild_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS disabled_commands (
                guild_id INTEGER,
                command TEXT,
                PRIMARY KEY (guild_id, command)
            )
        """)
        await db.commit()
        cursor = await db.execute(
            "SELECT command FROM disabled_commands WHERE guild_id=?",
            (guild_id,))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

async def is_disabled(guild_id: int, command: str) -> bool:
    disabled = await get_disabled_commands(guild_id)
    return command in disabled

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction,
                   member: discord.Member,
                   reason: str = "No reason provided"):
        if await is_disabled(interaction.guild.id, "kick"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message(
                "You can't kick someone with an equal or higher role!", ephemeral=True)
            return
        if member == interaction.guild.me:
            await interaction.response.send_message(
                "I can't kick myself!", ephemeral=True)
            return
        try:
            await member.send(
                f"You have been kicked from **{interaction.guild.name}**\nReason: {reason}")
        except:
            pass
        await member.kick(reason=reason)
        await log_action(interaction.guild, "kick", interaction.user, member, reason)
        embed = discord.Embed(title="Member Kicked", color=discord.Color.orange())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction,
                  member: discord.Member,
                  reason: str = "No reason provided",
                  delete_days: int = 0):
        if await is_disabled(interaction.guild.id, "ban"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message(
                "You can't ban someone with an equal or higher role!", ephemeral=True)
            return
        try:
            await member.send(
                f"You have been banned from **{interaction.guild.name}**\nReason: {reason}")
        except:
            pass
        await member.ban(reason=reason, delete_message_days=min(delete_days, 7))
        await log_action(interaction.guild, "ban", interaction.user, member, reason)
        embed = discord.Embed(title="Member Banned", color=discord.Color.red())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction,
                    user_id: str,
                    reason: str = "No reason provided"):
        if await is_disabled(interaction.guild.id, "unban"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=reason)
            embed = discord.Embed(title="Member Unbanned", color=discord.Color.green())
            embed.add_field(name="User", value=str(user))
            embed.add_field(name="Reason", value=reason)
            await interaction.response.send_message(embed=embed)
        except discord.NotFound:
            await interaction.response.send_message(
                "User not found or not banned.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(
                f"Error: {e}", ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction,
                      member: discord.Member,
                      minutes: int,
                      reason: str = "No reason provided"):
        if await is_disabled(interaction.guild.id, "timeout"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message(
                "You can't timeout someone with an equal or higher role!", ephemeral=True)
            return
        duration = timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        await log_action(interaction.guild, "timeout", interaction.user, member,
                         f"{reason} ({minutes} minutes)")
        embed = discord.Embed(title="Member Timed Out", color=discord.Color.orange())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Duration", value=f"{minutes} minutes")
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="untimeout", description="Remove timeout from a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction,
                        member: discord.Member):
        if await is_disabled(interaction.guild.id, "untimeout"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        await member.timeout(None)
        await interaction.response.send_message(
            f"Removed timeout from {member.mention}", ephemeral=True)

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction,
                   member: discord.Member,
                   reason: str):
        if await is_disabled(interaction.guild.id, "warn"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    guild_id INTEGER,
                    user_id INTEGER,
                    moderator_id INTEGER,
                    reason TEXT,
                    timestamp TEXT
                )
            """)
            await db.execute("""
                INSERT INTO warnings (guild_id, user_id, moderator_id, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (interaction.guild.id, member.id, interaction.user.id,
                  reason, datetime.utcnow().isoformat()))
            await db.commit()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id))
            count = (await cursor.fetchone())[0]
        await log_action(interaction.guild, "warn", interaction.user, member, reason)
        embed = discord.Embed(title="Member Warned", color=discord.Color.yellow())
        embed.add_field(name="Member", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Total Warnings", value=str(count))
        embed.add_field(name="By", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)
        try:
            await member.send(
                f"You have been warned in **{interaction.guild.name}**\n"
                f"Reason: {reason}\nTotal warnings: {count}")
        except:
            pass

    @app_commands.command(name="warnings", description="Check warnings for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction,
                       member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT reason, timestamp FROM warnings
                WHERE guild_id=? AND user_id=?
                ORDER BY timestamp DESC
            """, (interaction.guild.id, member.id))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                f"{member.mention} has no warnings.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Warnings for {member.display_name}",
            color=discord.Color.yellow())
        for i, (reason, timestamp) in enumerate(rows, 1):
            embed.add_field(
                name=f"Warning #{i} — {timestamp[:10]}",
                value=reason,
                inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction,
                            member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM warnings WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id))
            await db.commit()
        await interaction.response.send_message(
            f"Cleared all warnings for {member.mention}", ephemeral=True)

    @app_commands.command(name="purge", description="Delete multiple messages at once")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int):
        if await is_disabled(interaction.guild.id, "purge"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                "Amount must be between 1 and 100.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(
            f"Deleted {len(deleted)} messages.", ephemeral=True)

    @app_commands.command(name="lock", description="Lock a channel so members can't send messages")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction,
                   channel: discord.TextChannel = None,
                   reason: str = "No reason provided"):
        if await is_disabled(interaction.guild.id, "lock"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        channel = channel or interaction.channel
        await channel.set_permissions(
            interaction.guild.default_role, send_messages=False)
        embed = discord.Embed(
            title="Channel Locked",
            description=f"{channel.mention} has been locked.\nReason: {reason}",
            color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="unlock", description="Unlock a channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction,
                     channel: discord.TextChannel = None):
        if await is_disabled(interaction.guild.id, "unlock"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        channel = channel or interaction.channel
        await channel.set_permissions(
            interaction.guild.default_role, send_messages=True)
        embed = discord.Embed(
            title="Channel Unlocked",
            description=f"{channel.mention} has been unlocked.",
            color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="slowmode", description="Set slowmode in a channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction,
                       seconds: int,
                       channel: discord.TextChannel = None):
        if await is_disabled(interaction.guild.id, "slowmode"):
            await interaction.response.send_message(
                "This command is disabled!", ephemeral=True)
            return
        channel = channel or interaction.channel
        await channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await interaction.response.send_message(
                f"Slowmode disabled in {channel.mention}", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Slowmode set to {seconds}s in {channel.mention}", ephemeral=True)

    @app_commands.command(name="modlogs", description="View moderation logs for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def modlogs(self, interaction: discord.Interaction,
                      member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT action, moderator_id, reason, timestamp
                FROM mod_logs WHERE guild_id=? AND target_id=?
                ORDER BY timestamp DESC LIMIT 10
            """, (interaction.guild.id, member.id))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                f"No mod logs for {member.mention}.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Mod Logs — {member.display_name}",
            color=discord.Color.orange())
        for action, mod_id, reason, timestamp in rows:
            embed.add_field(
                name=f"{action.upper()} — {timestamp[:10]}",
                value=f"By <@{mod_id}>\nReason: {reason}",
                inline=False)
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Moderation(bot))
