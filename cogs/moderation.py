import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timedelta
from database import DB_PATH
from utils.permissions import can_moderate, check_bot_role_position
from utils.formatters import snapshot_user, now_iso, format_duration


async def log_mod_action(guild_id: int, user: discord.Member,
                          moderator: discord.Member, action: str,
                          reason: str, source: str = "bot",
                          duration_minutes: int = None):
    """
    Logs a moderation action to BOTH mod_logs (legacy) and
    moderation_logs (new Blueprint table with snapshots).
    Rule 1 — Snapshot Rule applied here.
    """
    user_snap = snapshot_user(user)
    mod_snap  = snapshot_user(moderator)
    ts        = now_iso()

    async with aiosqlite.connect(DB_PATH) as db:
        # New moderation_logs table (Blueprint)
        await db.execute("""
            INSERT INTO moderation_logs
                (guild_id, user_id, user_display_name, user_avatar_url,
                 moderator_id, moderator_display_name, action, reason,
                 source, duration_minutes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id,
            user_snap["id"], user_snap["display_name"], user_snap["avatar_url"],
            mod_snap["id"],  mod_snap["display_name"],
            action, reason, source, duration_minutes, ts,
        ))
        # Legacy mod_logs table (keep for backwards compat)
        await db.execute("""
            INSERT INTO mod_logs
                (guild_id, action, moderator_id, target_id, reason,
                 timestamp, user_display_name, user_avatar_url,
                 moderator_display_name, source, duration_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id, action,
            mod_snap["id"], user_snap["id"],
            reason, ts,
            user_snap["display_name"], user_snap["avatar_url"],
            mod_snap["display_name"], source, duration_minutes,
        ))
        await db.commit()


async def check_warning_thresholds(guild: discord.Guild,
                                    member: discord.Member,
                                    moderator: discord.Member):
    """
    After a new warning is issued, checks if the member has hit
    a warning threshold and auto-escalates the action.
    Reads from: warning_thresholds table
    Configured on: dashboard Moderation page
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Count current warnings
        cursor = await db.execute("""
            SELECT COUNT(*) FROM warnings
            WHERE guild_id = ? AND user_id = ?
        """, (guild.id, member.id))
        warn_count = (await cursor.fetchone())[0]

        # Get matching thresholds
        cursor = await db.execute("""
            SELECT action, duration_minutes, role_id
            FROM warning_thresholds
            WHERE guild_id = ? AND warn_count = ? AND enabled = 1
        """, (guild.id, warn_count))
        thresholds = await cursor.fetchall()

    for action, duration_minutes, role_id in thresholds:
        try:
            reason = f"Auto: reached {warn_count} warnings"
            if action == "kick":
                await member.kick(reason=reason)
                await log_mod_action(guild.id, member, moderator,
                                     "kick", reason, "auto-threshold")
            elif action == "ban":
                await member.ban(reason=reason)
                await log_mod_action(guild.id, member, moderator,
                                     "ban", reason, "auto-threshold")
            elif action == "timeout" and duration_minutes:
                await member.timeout(
                    timedelta(minutes=duration_minutes), reason=reason)
                await log_mod_action(guild.id, member, moderator,
                                     "timeout", reason, "auto-threshold",
                                     duration_minutes)
            elif action == "add_role" and role_id:
                role = guild.get_role(role_id)
                if role:
                    can, warn = check_bot_role_position(guild, role)
                    if can:
                        await member.add_roles(role, reason=reason)
                    else:
                        print(f"[ROLE WARNING] {warn}")
        except Exception as e:
            print(f"[THRESHOLD ERROR] {e}")


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ─── KICK ───────────────────────────────────────────
    @app_commands.command(name="kick", description="Kick a member")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction,
                   member: discord.Member, reason: str = "No reason provided"):
        allowed, msg = await can_moderate(
            interaction.user, member, interaction.guild.id)
        if not allowed:
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await member.kick(reason=reason)
        await log_mod_action(interaction.guild.id, member,
                             interaction.user, "kick", reason)
        embed = discord.Embed(
            title="Member Kicked",
            description=f"{member.mention} has been kicked.",
            color=0xED4245)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    # ─── BAN ────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction,
                  member: discord.Member, reason: str = "No reason provided",
                  delete_days: int = 0):
        allowed, msg = await can_moderate(
            interaction.user, member, interaction.guild.id)
        if not allowed:
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await member.ban(reason=reason,
                         delete_message_days=min(delete_days, 7))
        await log_mod_action(interaction.guild.id, member,
                             interaction.user, "ban", reason)
        embed = discord.Embed(
            title="Member Banned",
            description=f"{member.mention} has been banned.",
            color=0xED4245)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    # ─── UNBAN ──────────────────────────────────────────
    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction,
                    user_id: str, reason: str = "No reason provided"):
        await interaction.response.defer()
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=reason)
            await log_mod_action(interaction.guild.id, user,
                                 interaction.user, "unban", reason)
            await interaction.followup.send(
                f"Unbanned {user.mention}. Reason: {reason}")
        except discord.NotFound:
            await interaction.followup.send("User not found or not banned.")
        except ValueError:
            await interaction.followup.send("Invalid user ID.")

    # ─── TIMEOUT ────────────────────────────────────────
    @app_commands.command(name="timeout",
                          description="Timeout a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction,
                      member: discord.Member,
                      minutes: int = 10,
                      reason: str = "No reason provided"):
        allowed, msg = await can_moderate(
            interaction.user, member, interaction.guild.id)
        if not allowed:
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await log_mod_action(interaction.guild.id, member,
                             interaction.user, "timeout", reason,
                             duration_minutes=minutes)
        embed = discord.Embed(
            title="Member Timed Out",
            description=f"{member.mention} timed out for {format_duration(minutes)}.",
            color=0xFEE75C)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

    # ─── UNTIMEOUT ──────────────────────────────────────
    @app_commands.command(name="untimeout",
                          description="Remove a timeout from a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction,
                        member: discord.Member,
                        reason: str = "No reason provided"):
        await member.timeout(None, reason=reason)
        await log_mod_action(interaction.guild.id, member,
                             interaction.user, "untimeout", reason)
        await interaction.response.send_message(
            f"Removed timeout from {member.mention}.")

    # ─── WARN ───────────────────────────────────────────
    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction,
                   member: discord.Member,
                   reason: str = "No reason provided"):
        allowed, msg = await can_moderate(
            interaction.user, member, interaction.guild.id)
        if not allowed:
            await interaction.response.send_message(msg, ephemeral=True)
            return

        user_snap = snapshot_user(member)
        mod_snap  = snapshot_user(interaction.user)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO warnings
                    (guild_id, user_id, moderator_id, reason, timestamp,
                     user_display_name, user_avatar_url,
                     moderator_display_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                interaction.guild.id,
                user_snap["id"], mod_snap["id"],
                reason, now_iso(),
                user_snap["display_name"], user_snap["avatar_url"],
                mod_snap["display_name"],
            ))
            cursor = await db.execute("""
                SELECT COUNT(*) FROM warnings
                WHERE guild_id = ? AND user_id = ?
            """, (interaction.guild.id, member.id))
            warn_count = (await cursor.fetchone())[0]
            await db.commit()

        await log_mod_action(interaction.guild.id, member,
                             interaction.user, "warn", reason)

        embed = discord.Embed(
            title="Member Warned",
            description=f"{member.mention} has been warned.",
            color=0xFEE75C)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Total Warnings", value=str(warn_count))
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)

        # Check thresholds after warning
        await check_warning_thresholds(
            interaction.guild, member, interaction.user)

    # ─── WARNINGS ───────────────────────────────────────
    @app_commands.command(name="warnings",
                          description="View warnings for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction,
                       member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT reason, timestamp, moderator_display_name
                FROM warnings
                WHERE guild_id = ? AND user_id = ?
                ORDER BY timestamp DESC LIMIT 10
            """, (interaction.guild.id, member.id))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                f"{member.mention} has no warnings.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Warnings — {member.display_name}",
            color=0xFEE75C)
        for i, (reason, ts, mod_name) in enumerate(rows, 1):
            embed.add_field(
                name=f"#{i} — {ts[:10] if ts else 'Unknown'}",
                value=f"**Reason:** {reason}\n**By:** {mod_name or 'Unknown'}",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── CLEAR WARNINGS ─────────────────────────────────
    @app_commands.command(name="clearwarnings",
                          description="Clear all warnings for a member")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction,
                             member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                DELETE FROM warnings
                WHERE guild_id = ? AND user_id = ?
            """, (interaction.guild.id, member.id))
            await db.commit()
        await interaction.response.send_message(
            f"Cleared all warnings for {member.mention}.")

    # ─── PURGE ──────────────────────────────────────────
    @app_commands.command(name="purge",
                          description="Delete messages in bulk")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction,
                    amount: int = 10,
                    member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        if amount < 1 or amount > 100:
            await interaction.followup.send(
                "Amount must be between 1 and 100.", ephemeral=True)
            return
        check = (lambda m: m.author == member) if member else None
        deleted = await interaction.channel.purge(
            limit=amount, check=check)
        await interaction.followup.send(
            f"Deleted {len(deleted)} messages.", ephemeral=True)

    # ─── LOCK ───────────────────────────────────────────
    @app_commands.command(name="lock",
                          description="Lock a channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction,
                   reason: str = "No reason provided"):
        overwrite = interaction.channel.overwrites_for(
            interaction.guild.default_role)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite, reason=reason)
        await log_mod_action(interaction.guild.id, interaction.user,
                             interaction.user, "lock", reason)
        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"{interaction.channel.mention} has been locked.",
            color=0xED4245)
        embed.add_field(name="Reason", value=reason)
        await interaction.response.send_message(embed=embed)

    # ─── UNLOCK ─────────────────────────────────────────
    @app_commands.command(name="unlock",
                          description="Unlock a channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction,
                     reason: str = "No reason provided"):
        overwrite = interaction.channel.overwrites_for(
            interaction.guild.default_role)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite, reason=reason)
        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=f"{interaction.channel.mention} has been unlocked.",
            color=0x57F287)
        embed.add_field(name="Reason", value=reason)
        await interaction.response.send_message(embed=embed)

    # ─── SLOWMODE ───────────────────────────────────────
    @app_commands.command(name="slowmode",
                          description="Set slowmode in a channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction,
                       seconds: int = 0):
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await interaction.response.send_message(
                "Slowmode disabled.")
        else:
            await interaction.response.send_message(
                f"Slowmode set to {seconds} seconds.")

    # ─── MOD LOGS ───────────────────────────────────────
    @app_commands.command(name="modlogs",
                          description="View mod logs for a member")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def modlogs(self, interaction: discord.Interaction,
                      member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT action, reason, moderator_display_name,
                       created_at, source
                FROM moderation_logs
                WHERE guild_id = ? AND user_id = ? AND deleted = 0
                ORDER BY created_at DESC LIMIT 10
            """, (interaction.guild.id, member.id))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                f"No mod logs found for {member.mention}.",
                ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Mod Logs — {member.display_name}",
            color=0x7c5cbf)
        for action, reason, mod_name, ts, source in rows:
            embed.add_field(
                name=f"{action.upper()} — {ts[:10] if ts else '?'}",
                value=(f"**Reason:** {reason or 'None'}\n"
                       f"**By:** {mod_name or 'Unknown'}\n"
                       f"**Source:** {source}"),
                inline=False)
        await interaction.response.send_message(embed=embed,
                                                ephemeral=True)

    # ─── MASSBAN ────────────────────────────────────────
    @app_commands.command(name="massban",
                          description="Ban multiple users by ID")
    @app_commands.checks.has_permissions(ban_members=True)
    async def massban(self, interaction: discord.Interaction,
                      user_ids: str,
                      reason: str = "Mass ban"):
        await interaction.response.defer()
        ids     = [i.strip() for i in user_ids.split(",") if i.strip()]
        banned  = []
        failed  = []
        for uid in ids:
            try:
                user = await self.bot.fetch_user(int(uid))
                await interaction.guild.ban(user, reason=reason)
                await log_mod_action(
                    interaction.guild.id, user,
                    interaction.user, "ban", reason, "massban")
                banned.append(str(uid))
            except Exception:
                failed.append(str(uid))
        embed = discord.Embed(title="Mass Ban Complete", color=0xED4245)
        embed.add_field(name="Banned",
                        value=", ".join(banned) or "None")
        if failed:
            embed.add_field(name="Failed",
                            value=", ".join(failed))
        await interaction.followup.send(embed=embed)

    # ─── LOCKDOWN ───────────────────────────────────────
    @app_commands.command(name="lockdown",
                          description="Lock all channels in the server")
    @app_commands.checks.has_permissions(administrator=True)
    async def lockdown(self, interaction: discord.Interaction,
                       reason: str = "Server lockdown"):
        await interaction.response.defer()
        locked = 0
        for channel in interaction.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(
                    interaction.guild.default_role)
                overwrite.send_messages = False
                await channel.set_permissions(
                    interaction.guild.default_role,
                    overwrite=overwrite)
                locked += 1
            except Exception:
                pass
        embed = discord.Embed(
            title="🔒 Server Lockdown",
            description=f"Locked {locked} channels.",
            color=0xED4245)
        embed.add_field(name="Reason", value=reason)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="unlockdown",
                          description="Unlock all channels in the server")
    @app_commands.checks.has_permissions(administrator=True)
    async def unlockdown(self, interaction: discord.Interaction):
        await interaction.response.defer()
        unlocked = 0
        for channel in interaction.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(
                    interaction.guild.default_role)
                overwrite.send_messages = None
                await channel.set_permissions(
                    interaction.guild.default_role,
                    overwrite=overwrite)
                unlocked += 1
            except Exception:
                pass
        await interaction.followup.send(
            f"🔓 Unlocked {unlocked} channels.")


async def setup(bot):
    await bot.add_cog(Moderation(bot))
