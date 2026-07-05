import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH
from utils.formatters import snapshot_user, now_iso


async def get_report_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM report_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return {"guild_id": guild_id, "enabled": 0, "report_channel_id": None,
            "staff_role_id": None}


class ReportReasonModal(discord.ui.Modal, title="Report Message"):
    reason = discord.ui.TextInput(
        label="Why are you reporting this message?",
        style=discord.TextStyle.paragraph,
        placeholder="Spam, harassment, NSFW, scam link, etc.",
        max_length=500,
        required=True,
    )

    def __init__(self, target_message: discord.Message,
                 report_channel: discord.TextChannel,
                 staff_role_id: int | None):
        super().__init__()
        self.target_message = target_message
        self.report_channel = report_channel
        self.staff_role_id  = staff_role_id

    async def on_submit(self, interaction: discord.Interaction):
        msg      = self.target_message
        reporter = interaction.user
        reporter_snap = snapshot_user(reporter)
        author_snap   = snapshot_user(msg.author)

        content_preview = msg.content or "*(no text content — embed, image, or attachment only)*"
        if len(content_preview) > 900:
            content_preview = content_preview[:900] + "…"

        embed = discord.Embed(
            title="🚩 New Report",
            description=self.reason.value,
            color=0xED4245,
            timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Reported User",
                        value=f"{msg.author.mention} (`{msg.author.id}`)",
                        inline=True)
        embed.add_field(name="Reported By",
                        value=f"{reporter.mention} (`{reporter.id}`)",
                        inline=True)
        embed.add_field(name="Channel",
                        value=msg.channel.mention, inline=True)
        embed.add_field(name="Message Content",
                        value=content_preview, inline=False)
        embed.add_field(name="Jump to Message",
                        value=f"[Click here]({msg.jump_url})",
                        inline=False)
        if msg.attachments:
            embed.add_field(
                name="Attachments",
                value="\n".join(a.url for a in msg.attachments[:3]),
                inline=False)
        if author_snap["avatar_url"]:
            embed.set_thumbnail(url=author_snap["avatar_url"])
        embed.set_footer(text="Status: Open")

        ping = ""
        if self.staff_role_id:
            role = interaction.guild.get_role(self.staff_role_id)
            if role:
                ping = role.mention

        try:
            sent = await self.report_channel.send(
                content=ping or None, embed=embed,
                view=ReportActionView())
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to post in the report channel. "
                "Ask an admin to check my permissions there.",
                ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reports
                    (guild_id, reporter_id, reporter_name,
                     reported_user_id, reported_user_name,
                     message_id, channel_id, report_message_id,
                     report_channel_id, message_content,
                     message_jump_url, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """, (
                interaction.guild.id,
                reporter.id, reporter_snap["display_name"],
                msg.author.id, author_snap["display_name"],
                msg.id, msg.channel.id, sent.id,
                self.report_channel.id,
                msg.content or "", msg.jump_url,
                self.reason.value, now_iso(),
            ))
            await db.commit()

        await interaction.response.send_message(
            "✅ Thanks — your report has been sent to the staff team.",
            ephemeral=True)


class ReportActionView(discord.ui.View):
    """Persistent view attached to every report embed sent to staff."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _require_staff(self, interaction: discord.Interaction) -> bool:
        config = await get_report_config(interaction.guild.id)
        staff_role_id = config.get("staff_role_id")
        if not staff_role_id:
            # No staff role configured — fall back to manage_messages perm
            if interaction.user.guild_permissions.manage_messages:
                return True
            await interaction.response.send_message(
                "No staff role configured for reports. "
                "Ask an admin to run `/report_setup`.", ephemeral=True)
            return False
        role = interaction.guild.get_role(int(staff_role_id))
        if role and role in interaction.user.roles:
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "Only staff can act on reports.", ephemeral=True)
        return False

    async def _update_status(self, interaction: discord.Interaction,
                              new_status: str, color: int,
                              footer_prefix: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE reports SET status = ?
                WHERE guild_id = ? AND report_message_id = ?
            """, (new_status, interaction.guild.id, interaction.message.id))
            await db.commit()

        embed = interaction.message.embeds[0]
        embed.color = color
        embed.set_footer(
            text=f"{footer_prefix} by {interaction.user.display_name}")
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Resolve", emoji="✅",
                       style=discord.ButtonStyle.success,
                       custom_id="report_resolve")
    async def resolve(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        if not await self._require_staff(interaction):
            return
        await self._update_status(
            interaction, "resolved", 0x57F287, "Status: Resolved")

    @discord.ui.button(label="Dismiss", emoji="🗑️",
                       style=discord.ButtonStyle.secondary,
                       custom_id="report_dismiss")
    async def dismiss(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        if not await self._require_staff(interaction):
            return
        await self._update_status(
            interaction, "dismissed", 0x4e5058, "Status: Dismissed")


class Report(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Context menu commands are created outside the class body and
        # added to the tree manually — app_commands.context_menu
        # doesn't bind to Cog methods the way app_commands.command does.
        self.report_ctx_menu = app_commands.ContextMenu(
            name="Report Message",
            callback=self.report_message_ctx,
        )
        self.bot.tree.add_command(self.report_ctx_menu)

    def cog_unload(self):
        self.bot.tree.remove_command(
            self.report_ctx_menu.name,
            type=self.report_ctx_menu.type)

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(ReportActionView())

    async def report_message_ctx(self, interaction: discord.Interaction,
                                  message: discord.Message):
        if not interaction.guild:
            await interaction.response.send_message(
                "Reports can only be used inside a server.", ephemeral=True)
            return

        if message.author.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't report your own message.", ephemeral=True)
            return

        if message.author.bot:
            await interaction.response.send_message(
                "You can't report a bot message.", ephemeral=True)
            return

        config = await get_report_config(interaction.guild.id)
        if not config.get("enabled") or not config.get("report_channel_id"):
            await interaction.response.send_message(
                "The report system isn't set up on this server yet. "
                "Ask an admin to run `/report_setup`.", ephemeral=True)
            return

        report_channel = interaction.guild.get_channel(
            int(config["report_channel_id"]))
        if not report_channel:
            await interaction.response.send_message(
                "The configured report channel no longer exists. "
                "Ask an admin to run `/report_setup` again.", ephemeral=True)
            return

        # Basic duplicate-report guard — one open report per
        # (reporter, message) pair.
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id FROM reports
                WHERE guild_id = ? AND reporter_id = ?
                  AND message_id = ? AND status = 'open'
            """, (interaction.guild.id, interaction.user.id, message.id))
            existing = await cursor.fetchone()
        if existing:
            await interaction.response.send_message(
                "You've already reported this message — staff will "
                "review it shortly.", ephemeral=True)
            return

        await interaction.response.send_modal(
            ReportReasonModal(
                target_message=message,
                report_channel=report_channel,
                staff_role_id=config.get("staff_role_id")))

    # ─── SLASH COMMANDS ──────────────────────────────────
    @app_commands.command(name="report_setup",
                          description="Configure the report system")
    @app_commands.checks.has_permissions(administrator=True)
    async def report_setup(self, interaction: discord.Interaction,
                           report_channel: discord.TextChannel,
                           staff_role: discord.Role = None,
                           enabled: bool = True):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO report_config
                    (guild_id, enabled, report_channel_id, staff_role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled           = excluded.enabled,
                    report_channel_id = excluded.report_channel_id,
                    staff_role_id     = excluded.staff_role_id
            """, (
                interaction.guild.id,
                int(enabled),
                report_channel.id,
                staff_role.id if staff_role else None,
            ))
            await db.commit()

        embed = discord.Embed(
            title="Report System Configured",
            color=0x57F287 if enabled else 0xED4245)
        embed.add_field(name="Status",
                        value="Enabled" if enabled else "Disabled")
        embed.add_field(name="Report Channel",
                        value=report_channel.mention)
        if staff_role:
            embed.add_field(name="Staff Role", value=staff_role.mention)
        embed.set_footer(
            text="Right-click any message → Apps → Report Message")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="report_list",
                          description="View recent reports (staff)")
    @app_commands.checks.has_permissions(administrator=True)
    async def report_list(self, interaction: discord.Interaction,
                          status: str = "open"):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, reported_user_name, reporter_name,
                       reason, message_jump_url, created_at
                FROM reports
                WHERE guild_id = ? AND status = ?
                ORDER BY created_at DESC LIMIT 10
            """, (interaction.guild.id, status))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                f"No {status} reports.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🚩 Reports — {status.title()}", color=0xED4245)
        for (rid, target, reporter, reason, jump, created) in rows:
            embed.add_field(
                name=f"#{rid} — {target}",
                value=(f"By: {reporter}\n"
                       f"Reason: {(reason or '')[:80]}\n"
                       f"[Jump]({jump}) • {str(created)[:16]}"),
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Report(bot))
