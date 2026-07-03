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
        """
        Detects manual timeouts (applied directly in Discord's UI,
        not through /timeout).

        P1 #13 FIX: this previously logged the action with no
        duration_minutes or expires_at at all, so manual timeouts
        never showed a duration and never expired out of the
        dashboard's "Active Punishments" tab. Now we read
        after.timed_out_until directly off the member object and
        derive both fields from it, the same way log_mod_action()
        does for bot-issued timeouts.
        """
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

                    expires_at        = None
                    duration_minutes  = None
                    if after.timed_out_until:
                        expires_at = after.timed_out_until.isoformat()
                        delta = after.timed_out_until - discord.utils.utcnow()
                        duration_minutes = max(
                            0, int(delta.total_seconds() // 60))

                    await self._save_log(
                        guild.id, after, entry.user,
                        action,
                        str(entry.reason or "No reason"),
                        "manual",
                        duration_minutes=duration_minutes,
                        expires_at=expires_at)
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
                         source: str,
                         duration_minutes: int = None,
                         expires_at: str = None):
        user_snap = snapshot_user(target)
        mod_snap  = snapshot_user(moderator)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO moderation_logs
                        (guild_id, user_id, user_display_name,
                         user_avatar_url, moderator_id,
                         moderator_display_name, action,
                         reason, source, duration_minutes,
                         expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    guild_id,
                    user_snap["id"],
                    user_snap["display_name"],
                    user_snap["avatar_url"],
                    mod_snap["id"],
                    mod_snap["display_name"],
                    action, reason, source,
                    duration_minutes, expires_at, now_iso(),
                ))
                await db.commit()
        except Exception as e:
            print(f"[AUDITLOG] Save error: {e}")


async def setup(bot):
    await bot.add_cog(AuditLog(bot))
