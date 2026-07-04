import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
import random
from database import DB_PATH
from utils.permissions import check_bot_role_position
from utils.formatters import snapshot_user


async def get_welcome_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM welcome_config WHERE guild_id = ?",
            (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return {}


async def get_welcome_messages(guild_id: int,
                                msg_type: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT embed_data FROM welcome_messages
            WHERE guild_id = ? AND type = ?
            ORDER BY position ASC LIMIT 3
        """, (guild_id, msg_type))
        rows = await cursor.fetchall()
    result = []
    for (data,) in rows:
        try:
            result.append(json.loads(data))
        except Exception:
            pass
    return result


def build_embed(embed_data: dict,
                member: discord.Member) -> discord.Embed:
    """
    Builds a Discord embed from stored embed_data dict.
    Replaces placeholders:
      {user}        → member mention
      {name}        → display name
      {server}      → server name
      {member_count}→ member count
    """
    def replace(text: str) -> str:
        if not text:
            return text
        return (text
                .replace("{user}", member.mention)
                .replace("{name}", member.display_name)
                .replace("{server}", member.guild.name)
                .replace("{member_count}",
                         str(member.guild.member_count)))

    color_str = embed_data.get("color", "#7c5cbf")
    try:
        color_int = int(color_str.strip("#"), 16)
    except Exception:
        color_int = 0x7c5cbf

    embed = discord.Embed(color=color_int)
    if embed_data.get("title"):
        embed.title = replace(embed_data["title"])
    if embed_data.get("description"):
        embed.description = replace(embed_data["description"])
    if embed_data.get("footer"):
        embed.set_footer(text=replace(embed_data["footer"]))
    if embed_data.get("thumbnail"):
        embed.set_thumbnail(url=embed_data["thumbnail"])
    if embed_data.get("image"):
        embed.set_image(url=embed_data["image"])
    if embed_data.get("author"):
        embed.set_author(name=replace(embed_data["author"]))
    for field in embed_data.get("fields", []):
        embed.add_field(
            name=replace(field.get("name", "")),
            value=replace(field.get("value", "")),
            inline=field.get("inline", False))
    return embed


class RulesView(discord.ui.View):
    """Persistent rules gate button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ I Accept",
                       style=discord.ButtonStyle.green,
                       custom_id="rules_accept")
    async def accept(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        config = await get_welcome_config(interaction.guild.id)
        rules_role_id = config.get("rules_role_id")
        button_text   = config.get("rules_button_text", "✅ I Accept")
        button.label  = button_text

        if not rules_role_id:
            await interaction.response.send_message(
                "Rules role not configured.", ephemeral=True)
            return

        role = interaction.guild.get_role(int(rules_role_id))
        if not role:
            await interaction.response.send_message(
                "Rules role not found.", ephemeral=True)
            return

        # Rule 5
        can, warn = check_bot_role_position(interaction.guild, role)
        if not can:
            await interaction.response.send_message(
                warn, ephemeral=True)
            return

        if role in interaction.user.roles:
            await interaction.response.send_message(
                "You already accepted the rules!", ephemeral=True)
            return

        await interaction.user.add_roles(
            role, reason="Accepted rules")
        await interaction.response.send_message(
            f"✅ Welcome! You've been given the {role.name} role.",
            ephemeral=True)


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(RulesView())  # Register persistent view

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await get_welcome_config(member.guild.id)
        if not config:
            return

        # Auto-role (Rule 5 check)
        auto_role_id = config.get("auto_role_id")
        if auto_role_id:
            role = member.guild.get_role(int(auto_role_id))
            if role:
                can, warn = check_bot_role_position(
                    member.guild, role)
                if can:
                    try:
                        await member.add_roles(
                            role, reason="Auto-role on join")
                    except Exception as e:
                        print(f"[WELCOME] Auto-role failed: {e}")
                else:
                    print(f"[WELCOME ROLE WARNING] {warn}")

        # Welcome message
        if not config.get("join_enabled"):
            return
        channel_id = config.get("join_channel_id")
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not channel:
            return

        messages = await get_welcome_messages(
            member.guild.id, "join")

        if not messages:
            # Default embed
            embed = discord.Embed(
                title=f"Welcome to {member.guild.name}!",
                description=(f"Hey {member.mention}, welcome! "
                             f"You are member #{member.guild.member_count}."),
                color=0x7c5cbf)
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await channel.send(embed=embed)
            except Exception:
                pass
            return

        mode = config.get("join_message_mode", "random")
        if mode == "random":
            selected = [random.choice(messages)]
        else:
            selected = messages

        for embed_data in selected:
            try:
                embed = build_embed(embed_data, member)
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[WELCOME] Failed to send embed: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        config = await get_welcome_config(member.guild.id)
        if not config or not config.get("leave_enabled"):
            return

        channel_id = config.get("leave_channel_id")
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not channel:
            return

        messages = await get_welcome_messages(
            member.guild.id, "leave")

        if not messages:
            embed = discord.Embed(
                description=(f"**{member.display_name}** has left the server."),
                color=0xED4245)
            try:
                await channel.send(embed=embed)
            except Exception:
                pass
            return

        mode = config.get("join_message_mode", "random")
        if mode == "random":
            selected = [random.choice(messages)]
        else:
            selected = messages

        for embed_data in selected:
            try:
                embed = build_embed(embed_data, member)
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[LEAVE] Failed to send embed: {e}")

    @app_commands.command(name="welcome_setup",
                          description="Send the rules gate embed")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_setup(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel = None):
        config = await get_welcome_config(interaction.guild.id)
        if not config.get("rules_enabled"):
            await interaction.response.send_message(
                "Rules gate is not enabled. Enable it in the dashboard.",
                ephemeral=True)
            return

        target = channel or interaction.channel
        button_text = config.get("rules_button_text", "✅ I Accept")

        view   = RulesView()
        embed  = discord.Embed(
            title="📋 Rules",
            description=("Please read the rules and click the button "
                         "below to gain access to the server."),
            color=0x7c5cbf)

        # Update button label from config
        for item in view.children:
            if hasattr(item, "label"):
                item.label = button_text

        await target.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"Rules gate sent to {target.mention}.",
            ephemeral=True)

    @app_commands.command(name="welcome_test",
                          description="Test welcome message for yourself")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_test(self, interaction: discord.Interaction):
        await self.on_member_join(interaction.user)
        await interaction.response.send_message(
            "Triggered welcome message.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
