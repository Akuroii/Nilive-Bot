import discord
from discord.ext import commands
import aiosqlite
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso


class AuditLog(commands.Cog):
    """
    Catches manual moderation actions done directly in Discord
    (not through bot commands) and logs them to moderation_logs
    with source='manual'.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild,
                             user: discord.User):
        await self._log_from_audit(guild, user, "ban")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild,
                               user: discord.User):
        await self._log_from_audit(guild, user, "unban")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Detects kicks via audit log."""
        guild = member.guild
        try:
            await discord.utils.sleep_until(
                discord.utils.utcnow())
            async for entry in guild.audit_logs(
                    limit=5,
                    action=discord.AuditLogAction.kick):
                if entry.target.id == member.id:
                    await self._save_log(
                        guild.id,
                        member,
                        entry.user,
                        "kick",
                        str(entry.reason or "No reason"),
                        "manual")
                    return
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member,
                                after: discord.Member):
        """Detects manual timeouts."""
        if before.timed_out_until == after.timed_out_until:
            return
        guild = after.guild
        try:
            async for entry in guild.audit_logs(
                    limit=5,
                    action=discord.AuditLogAction.member_update):
                if entry.target.id == after.id:
                    action = ("timeout" if after.timed_out_until
                              else "untimeout")
                    # Skip if done by our own bot
                    if entry.user.id == self.bot.user.id:
                        return
                    await self._save_log(
                        guild.id, after, entry.user,
                        action,
                        str(entry.reason or "No reason"),
                        "manual")
                    return
        except Exception:
            pass

    async def _log_from_audit(self, guild: discord.Guild,
                               user: discord.User, action: str):
        """Reads audit log to find who performed a ban/unban."""
        try:
            audit_action = (discord.AuditLogAction.ban
                            if action == "ban"
                            else discord.AuditLogAction.unban)
            async for entry in guild.audit_logs(
                    limit=5, action=audit_action):
                if entry.target.id == user.id:
                    # Skip if performed by our bot
                    if entry.user.id == self.bot.user.id:
                        return
                    await self._save_log(
                        guild.id, user, entry.user,
                        action,
                        str(entry.reason or "No reason"),
                        "manual")
                    return
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[AUDITLOG] Error: {e}")

    async def _save_log(self, guild_id: int,
                         target, moderator,
                         action: str, reason: str,
                         source: str):
        user_snap = snapshot_user(target)
        mod_snap  = snapshot_user(moderator)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO moderation_logs
                        (guild_id, user_id, user_display_name,
                         user_avatar_url, moderator_id,
                         moderator_display_name, action,
                         reason, source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    guild_id,
                    user_snap["id"],
                    user_snap["display_name"],
                    user_snap["avatar_url"],
                    mod_snap["id"],
                    mod_snap["display_name"],
                    action, reason, source, now_iso(),
                ))
                await db.commit()
        except Exception as e:
            print(f"[AUDITLOG] Save error: {e}")


async def setup(bot):
    await bot.add_cog(AuditLog(bot))
