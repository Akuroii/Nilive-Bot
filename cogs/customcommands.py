import discord
from discord.ext import commands
import aiosqlite
import json
from database import DB_PATH
from utils.permissions import can_moderate, check_bot_role_position


class CustomCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # PERFORMANCE FIX (dark-fixes pass #2): same class of fix as
        # cogs/triggers.py — database.py's central init_db() already
        # creates "custom_commands" before the bot comes online, so
        # running ensure_table() again inside on_message (previously,
        # for every message starting with "!") was redundant work on
        # the hottest path in the bot.
        await self.ensure_table()

    async def ensure_table(self):
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
                    requires_reason   INTEGER DEFAULT 0
                )
            """)
            await db.commit()

    async def get_commands(self, guild_id):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM custom_commands WHERE guild_id = ? OR guild_id = 0",
                (guild_id,))
            return await cursor.fetchall()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not message.content.startswith("!"):
            return

        # ALIAS CONFLICT FIX: if the first word after "!" matches a
        # registered prefix command (e.g., !k from an alias), skip
        # custom command processing to prevent double execution.
        # The bot's process_commands handler will pick it up instead.
        first_word = message.content.split()[0][1:].lower()
        if first_word and first_word in self.bot.all_commands:
            return

        cmds = await self.get_commands(message.guild.id)

        for cmd in cmds:
            (id_, guild_id, trigger, allowed_roles, actions, embed_title,
             embed_desc, embed_color, log_channel_id, same_channel,
             dm_member, dm_message, requires_mention, requires_reason) = cmd

            if not message.content.lower().startswith(f"!{trigger.lower()}"):
                continue

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
                reason_parts  = parts[2:] if len(parts) > 2 else []
                reason = " ".join(reason_parts) if reason_parts else "No reason provided"
            else:
                reason_parts = parts[1:] if len(parts) > 1 else []
                reason = " ".join(reason_parts) if reason_parts else "No reason provided"

            action_list = json.loads(actions) if actions else []
            action_errors = []

            # Hierarchy check — block privilege escalation via custom commands
            #
            # SECURITY FIX: "warn" is now included here too. It's less
            # destructive than ban/kick, but it still writes a moderation
            # record against the target and previously let a member with a
            # custom-command trigger warn someone ABOVE them in the role
            # hierarchy, bypassing can_moderate() entirely — the same class
            # of gap that was already fixed for ban/kick/timeout/remove_all_roles.
            destructive = {"ban", "kick", "remove_all_roles", "warn"}
            is_destructive = (
                bool(destructive & set(action_list))
                or any(a.startswith("timeout:") for a in action_list)
            )
            if target_member and is_destructive:
                allowed, hmsg = await can_moderate(
                    message.author, target_member, message.guild.id)
                if not allowed:
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
                        role    = message.guild.get_role(role_id)
                        if role:
                            # SECURITY FIX: this only ever checked whether the
                            # BOT could assign the role (check_bot_role_position)
                            # — it never checked whether the ACTOR (the member
                            # who typed the !command) was allowed to grant it.
                            # A moderator with access to a custom command whose
                            # actions included add_role:<high-privilege-role-id>
                            # could hand out roles above their own rank —
                            # straightforward privilege escalation. Now also
                            # requires the actor be guild owner, or have a top
                            # role strictly above the role being granted — the
                            # same rule utils.permissions.check_hierarchy()
                            # already uses for /ban, /kick, /timeout, /warn.
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
                        role    = message.guild.get_role(role_id)
                        if role:
                            # SECURITY FIX: same gap as add_role above, mirrored
                            # for removal — previously any actor with access to
                            # the trigger could strip a role off anyone,
                            # including roles above the actor's own rank.
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
