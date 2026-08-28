import discord
from discord.ext import commands
import aiosqlite
import json
import time
from database import DB_PATH
from utils.permissions import can_moderate, check_bot_role_position


class CustomCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.ensure_table()

    async def ensure_table(self):
        # Keep schema in sync with database.py central init_db
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS custom_commands (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id          INTEGER,
                    trigger           TEXT,
                    allowed_roles     TEXT DEFAULT '[]',
                    actions           TEXT DEFAULT '[]',
                    embed_title       TEXT,
                    embed_description TEXT,
                    embed_color       TEXT DEFAULT '#ED4245',
                    log_channel_id    INTEGER,
                    same_channel      INTEGER DEFAULT 0,
                    dm_member         INTEGER DEFAULT 0,
                    dm_message        TEXT,
                    requires_mention  INTEGER DEFAULT 1,
                    requires_reason   INTEGER DEFAULT 0,
                    enabled           INTEGER DEFAULT 1,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def get_commands(self, guild_id):
        # FIX: explicit columns, not SELECT *, and respect enabled + guild 0
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, trigger, allowed_roles, actions,
                       embed_title, embed_description, embed_color,
                       log_channel_id, same_channel, dm_member, dm_message,
                       requires_mention, requires_reason, enabled
                FROM custom_commands
                WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1
                ORDER BY id ASC
            """, (guild_id,))
            return await cursor.fetchall()

    def _matches_word_boundary(self, content: str, trigger: str) -> bool:
        """
        Word-boundary-aware matching for custom commands.
        Prevents "!k" from firing on "!kick".
        Matches "!trigger" exactly or "!trigger " (space after).
        Case-insensitive, as before.
        """
        if not trigger:
            return False
        content_lower = content.lower().strip()
        trig_lower = trigger.lower().strip()
        if not trig_lower:
            return False
        # Exact match: "!k"
        if content_lower == f"!{trig_lower}":
            return True
        # Prefix with space: "!k @user" or "!k reason"
        if content_lower.startswith(f"!{trig_lower} "):
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not message.content.startswith("!"):
            return

        # Router claimed check — if router (alias cog) already handled this message
        # (e.g., alias wins over custom), skip to guarantee exactly one executor
        if hasattr(self.bot, "_nero_claimed_messages") and message.id in self.bot._nero_claimed_messages:
            return

        # Prefix command check — let process_commands handle real prefix commands
        first_word = message.content.split()[0][1:].lower()
        if first_word and first_word in self.bot.all_commands:
            return

        cmds = await self.get_commands(message.guild.id)

        for cmd_row in cmds:
            # Explicit unpacking — 15 columns, not SELECT *
            (id_, guild_id, trigger, allowed_roles, actions, embed_title,
             embed_desc, embed_color, log_channel_id, same_channel,
             dm_member, dm_message, requires_mention, requires_reason, enabled) = cmd_row

            # Enabled already filtered in SQL, but double-check
            if not enabled:
                continue

            # Word-boundary matching fix
            if not self._matches_word_boundary(message.content, trigger):
                continue

            # If router decides to handle custom, it would have claimed already
            # But we also claim here to prevent triggers from firing after custom
            if hasattr(self.bot, "_nero_claimed_messages"):
                self.bot._nero_claimed_messages[message.id] = time.time()

            allowed = json.loads(allowed_roles) if allowed_roles else []
            if allowed:
                member_role_ids = [r.id for r in message.author.roles]
                if not any(int(r) in member_role_ids for r in allowed):
                    await message.channel.send(
                        f"{message.author.mention} You don't have permission.",
                        delete_after=5)
                    return

            parts = message.content.split()
            target_member = None
            reason = "No reason provided"

            if requires_mention:
                if not message.mentions:
                    await message.channel.send(
                        f"Usage: `!{trigger} @member reason`",
                        delete_after=5)
                    return
                target_member = message.mentions[0]
                reason_parts = parts[2:] if len(parts) > 2 else []
                reason = " ".join(reason_parts) if reason_parts else "No reason provided"
            else:
                reason_parts = parts[1:] if len(parts) > 1 else []
                reason = " ".join(reason_parts) if reason_parts else "No reason provided"

            action_list = json.loads(actions) if actions else []
            action_errors = []

            destructive = {"ban", "kick", "remove_all_roles", "warn"}
            is_destructive = (
                bool(destructive & set(action_list))
                or any(a.startswith("timeout:") for a in action_list)
            )
            if target_member and is_destructive:
                allowed_mod, hmsg = await can_moderate(
                    message.author, target_member, message.guild.id)
                if not allowed_mod:
                    await message.channel.send(
                        f"{message.author.mention} {hmsg}", delete_after=6)
                    return

            for action in action_list:
                try:
                    if action == "ban" and target_member:
                        await target_member.ban(reason=reason)
                    elif action == "kick" and target_member:
                        await target_member.kick(reason=reason)
                    elif action == "warn" and target_member:
                        async with aiosqlite.connect(DB_PATH) as db:
                            from datetime import datetime
                            await db.execute("""
                                INSERT INTO warnings
                                    (guild_id, user_id, moderator_id,
                                     reason, timestamp,
                                     user_display_name, moderator_display_name)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (
                                message.guild.id,
                                target_member.id,
                                message.author.id,
                                reason,
                                datetime.utcnow().isoformat(),
                                target_member.display_name,
                                message.author.display_name,
                            ))
                            await db.commit()
                    elif action.startswith("timeout:") and target_member:
                        from datetime import timedelta
                        minutes = int(action.split(":")[1])
                        await target_member.timeout(
                            timedelta(minutes=minutes), reason=reason)
                    elif action == "remove_all_roles" and target_member:
                        roles_to_remove = [
                            r for r in target_member.roles
                            if r != message.guild.default_role
                            and r.is_assignable()
                        ]
                        if roles_to_remove:
                            await target_member.remove_roles(*roles_to_remove)
                    elif action.startswith("add_role:") and target_member:
                        role_id = int(action.split(":")[1])
                        role = message.guild.get_role(role_id)
                        if role:
                            can_assign, warn = check_bot_role_position(
                                message.guild, role)
                            actor_ok = (
                                message.author.id == message.guild.owner_id
                                or message.author.top_role.position > role.position
                            )
                            if not actor_ok:
                                action_errors.append(
                                    f"You don't have permission to grant "
                                    f"@{role.name} (it's at or above your "
                                    f"highest role).")
                            elif can_assign:
                                await target_member.add_roles(role)
                            else:
                                action_errors.append(warn)
                    elif action.startswith("remove_role:") and target_member:
                        role_id = int(action.split(":")[1])
                        role = message.guild.get_role(role_id)
                        if role:
                            actor_ok = (
                                message.author.id == message.guild.owner_id
                                or message.author.top_role.position > role.position
                            )
                            if not actor_ok:
                                action_errors.append(
                                    f"You don't have permission to remove "
                                    f"@{role.name} (it's at or above your "
                                    f"highest role).")
                            else:
                                await target_member.remove_roles(role)
                    elif action == "delete_message":
                        try:
                            await message.delete()
                        except Exception:
                            pass
                except Exception as e:
                    action_errors.append(str(e))

            try:
                color_int = int(
                    embed_color.strip("#"), 16) if embed_color else 0xED4245
            except Exception:
                color_int = 0xED4245

            embed = discord.Embed(color=color_int)

            if embed_title:
                title = embed_title
                if target_member:
                    title = title.replace("{target}", target_member.display_name)
                title = title.replace("{moderator}", message.author.display_name)
                title = title.replace("{reason}", reason)
                embed.title = title

            if embed_desc:
                desc = embed_desc
                if target_member:
                    desc = desc.replace("{target}", target_member.mention)
                    desc = desc.replace("{target_name}", target_member.display_name)
                desc = desc.replace("{moderator}", message.author.mention)
                desc = desc.replace("{reason}", reason)
                embed.description = desc

            if target_member:
                embed.add_field(name="Member", value=target_member.mention)
            embed.add_field(name="Moderator", value=message.author.mention)
            embed.add_field(name="Reason", value=reason)

            if action_errors:
                embed.add_field(
                    name="Errors",
                    value="\n".join(action_errors),
                    inline=False)

            if same_channel:
                await message.channel.send(embed=embed)

            if log_channel_id:
                log_ch = message.guild.get_channel(int(log_channel_id))
                if log_ch:
                    await log_ch.send(embed=embed)

            if dm_member and target_member and dm_message:
                try:
                    dm_text = dm_message
                    dm_text = dm_text.replace("{server}", message.guild.name)
                    dm_text = dm_text.replace("{reason}", reason)
                    dm_text = dm_text.replace(
                        "{moderator}", message.author.display_name)
                    await target_member.send(dm_text)
                except Exception:
                    pass

            break


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
