import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH

class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_roles (
                    guild_id INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    button_label TEXT,
                    button_emoji TEXT,
                    button_color TEXT DEFAULT 'blurple',
                    role_id INTEGER,
                    booster_only INTEGER DEFAULT 0,
                    PRIMARY KEY (message_id, role_id)
                )
            """)
            await db.commit()

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_table()
        await self.restore_views()

    async def restore_views(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT DISTINCT guild_id, channel_id, message_id FROM reaction_roles
            """)
            messages = await cursor.fetchall()
        for guild_id, channel_id, message_id in messages:
            view = await self.build_view(message_id)
            self.bot.add_view(view, message_id=message_id)

    async def build_view(self, message_id: int) -> discord.ui.View:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT role_id, button_label, button_emoji, button_color, booster_only
                FROM reaction_roles WHERE message_id=?
            """, (message_id,))
            rows = await cursor.fetchall()
        view = discord.ui.View(timeout=None)
        for role_id, label, emoji, color, booster_only in rows:
            color_map = {
                "blurple": discord.ButtonStyle.primary,
                "gray": discord.ButtonStyle.secondary,
                "green": discord.ButtonStyle.success,
                "red": discord.ButtonStyle.danger,
            }
            style = color_map.get(color, discord.ButtonStyle.primary)
            button = RoleButton(
                role_id=role_id,
                label=label,
                emoji=emoji or None,
                style=style,
                booster_only=bool(booster_only)
            )
            view.add_item(button)
        return view

    @app_commands.command(name="reactionrole_create", description="Create a reaction role message with buttons")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_create(self, interaction: discord.Interaction,
                                   channel: discord.TextChannel,
                                   title: str,
                                   description: str):
        await self.ensure_table()
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.set_footer(text="Click a button to get or remove a role!")
        view = discord.ui.View(timeout=None)
        msg = await channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"Reaction role message created in {channel.mention}!\nMessage ID: `{msg.id}`\nNow use `/reactionrole_add` to add buttons.",
            ephemeral=True)

    @app_commands.command(name="reactionrole_add", description="Add a role button to a reaction role message")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_add(self, interaction: discord.Interaction,
                                message_id: str,
                                role: discord.Role,
                                label: str,
                                color: str = "blurple",
                                emoji: str = None,
                                booster_only: bool = False):
        await self.ensure_table()
        color = color.lower()
        if color not in ["blurple", "gray", "green", "red"]:
            await interaction.response.send_message(
                "Color must be: blurple, gray, green, or red", ephemeral=True)
            return
        msg_id = int(message_id)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT channel_id FROM reaction_roles WHERE message_id=?", (msg_id,))
            row = await cursor.fetchone()
            channel_id = row[0] if row else None
            await db.execute("""
                INSERT OR REPLACE INTO reaction_roles
                (guild_id, channel_id, message_id, button_label, button_emoji, button_color, role_id, booster_only)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (interaction.guild.id, channel_id or interaction.channel.id,
                  msg_id, label, emoji, color, role.id, int(booster_only)))
            await db.commit()
        for ch in interaction.guild.text_channels:
            try:
                msg = await ch.fetch_message(msg_id)
                view = await self.build_view(msg_id)
                await msg.edit(view=view)
                self.bot.add_view(view, message_id=msg_id)
                await interaction.response.send_message(
                    f"Added button **{label}** → {role.mention}", ephemeral=True)
                return
            except:
                continue
        await interaction.response.send_message("Message not found!", ephemeral=True)

    @app_commands.command(name="reactionrole_remove", description="Remove a role button from a reaction role message")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_remove(self, interaction: discord.Interaction,
                                   message_id: str,
                                   role: discord.Role):
        msg_id = int(message_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM reaction_roles WHERE message_id=? AND role_id=?",
                (msg_id, role.id))
            await db.commit()
        for ch in interaction.guild.text_channels:
            try:
                msg = await ch.fetch_message(msg_id)
                view = await self.build_view(msg_id)
                await msg.edit(view=view)
                await interaction.response.send_message(
                    f"Removed button for {role.mention}", ephemeral=True)
                return
            except:
                continue
        await interaction.response.send_message("Message not found!", ephemeral=True)

    @app_commands.command(name="reactionrole_list", description="List all reaction role messages")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT DISTINCT message_id, channel_id FROM reaction_roles
                WHERE guild_id=?
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No reaction role messages found!", ephemeral=True)
            return
        embed = discord.Embed(title="Reaction Role Messages", color=discord.Color.blurple())
        for msg_id, ch_id in rows:
            embed.add_field(
                name=f"Message ID: {msg_id}",
                value=f"Channel: <#{ch_id}>",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class RoleButton(discord.ui.Button):
    def __init__(self, role_id: int, label: str, emoji, style, booster_only: bool):
        super().__init__(label=label, emoji=emoji, style=style, custom_id=f"rr_{role_id}")
        self.role_id = role_id
        self.booster_only = booster_only

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        role = guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("Role not found!", ephemeral=True)
            return
        if self.booster_only and not member.premium_since:
            await interaction.response.send_message(
                "This role is for boosters only! Boost the server to unlock it.",
                ephemeral=True)
            return
        if role in member.roles:
            await member.remove_roles(role)
            await interaction.response.send_message(
                f"Removed **{role.name}** from you!", ephemeral=True)
        else:
            await member.add_roles(role)
            await interaction.response.send_message(
                f"Gave you **{role.name}**!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
