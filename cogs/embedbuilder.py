import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
from database import DB_PATH

class EmbedBuilder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS embed_templates (
                    guild_id INTEGER,
                    name TEXT,
                    data TEXT,
                    PRIMARY KEY (guild_id, name)
                )
            """)
            await db.commit()

    def parse_color(self, color: str) -> discord.Color:
        color = color.strip().lower()
        colors = {
            "red": discord.Color.red(),
            "blue": discord.Color.blue(),
            "green": discord.Color.green(),
            "gold": discord.Color.gold(),
            "purple": discord.Color.purple(),
            "orange": discord.Color.orange(),
            "pink": discord.Color.pink(),
            "white": discord.Color.from_rgb(255, 255, 255),
            "black": discord.Color.from_rgb(0, 0, 0),
            "blurple": discord.Color.blurple(),
            "teal": discord.Color.teal(),
            "yellow": discord.Color.yellow(),
        }
        if color in colors:
            return colors[color]
        try:
            color = color.lstrip("#")
            return discord.Color(int(color, 16))
        except:
            return discord.Color.blurple()

    def build_embed(self, data: dict) -> discord.Embed:
        embed = discord.Embed()
        if data.get("title"):
            embed.title = data["title"]
        if data.get("description"):
            embed.description = data["description"]
        if data.get("color"):
            embed.color = self.parse_color(data["color"])
        if data.get("footer"):
            embed.set_footer(text=data["footer"], icon_url=data.get("footer_icon"))
        if data.get("image"):
            embed.set_image(url=data["image"])
        if data.get("thumbnail"):
            embed.set_thumbnail(url=data["thumbnail"])
        if data.get("author"):
            embed.set_author(name=data["author"], icon_url=data.get("author_icon"))
        if data.get("fields"):
            for field in data["fields"]:
                embed.add_field(
                    name=field.get("name", "Field"),
                    value=field.get("value", "Value"),
                    inline=field.get("inline", False))
        return embed

    @app_commands.command(name="embed_create", description="Create and send a custom embed")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed_create(self, interaction: discord.Interaction,
                           channel: discord.TextChannel,
                           title: str = None,
                           description: str = None,
                           color: str = "blurple",
                           footer: str = None,
                           image: str = None,
                           thumbnail: str = None,
                           author: str = None,
                           save_as: str = None):
        await self.ensure_table()
        data = {
            "title": title,
            "description": description,
            "color": color,
            "footer": footer,
            "image": image,
            "thumbnail": thumbnail,
            "author": author,
        }
        embed = self.build_embed(data)
        if not title and not description:
            await interaction.response.send_message(
                "You need at least a title or description!", ephemeral=True)
            return
        msg = await channel.send(embed=embed)
        if save_as:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO embed_templates (guild_id, name, data)
                    VALUES (?, ?, ?)
                """, (interaction.guild.id, save_as.lower(), json.dumps(data)))
                await db.commit()
            await interaction.response.send_message(
                f"Embed sent to {channel.mention} and saved as `{save_as}`!\nMessage ID: `{msg.id}`",
                ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Embed sent to {channel.mention}!\nMessage ID: `{msg.id}`",
                ephemeral=True)

    @app_commands.command(name="embed_field", description="Add a field to an existing embed")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed_field(self, interaction: discord.Interaction,
                          message_id: str,
                          field_name: str,
                          field_value: str,
                          inline: bool = False):
        msg_id = int(message_id)
        for ch in interaction.guild.text_channels:
            try:
                msg = await ch.fetch_message(msg_id)
                if not msg.embeds:
                    await interaction.response.send_message("No embed found on that message!", ephemeral=True)
                    return
                embed = msg.embeds[0]
                embed.add_field(name=field_name, value=field_value, inline=inline)
                await msg.edit(embed=embed)
                await interaction.response.send_message("Field added!", ephemeral=True)
                return
            except:
                continue
        await interaction.response.send_message("Message not found!", ephemeral=True)

    @app_commands.command(name="embed_edit", description="Edit an existing embed sent by the bot")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed_edit(self, interaction: discord.Interaction,
                         message_id: str,
                         title: str = None,
                         description: str = None,
                         color: str = None,
                         footer: str = None,
                         image: str = None,
                         thumbnail: str = None):
        msg_id = int(message_id)
        for ch in interaction.guild.text_channels:
            try:
                msg = await ch.fetch_message(msg_id)
                if not msg.embeds:
                    await interaction.response.send_message("No embed found!", ephemeral=True)
                    return
                embed = msg.embeds[0].copy()
                if title: embed.title = title
                if description: embed.description = description
                if color: embed.color = self.parse_color(color)
                if footer: embed.set_footer(text=footer)
                if image: embed.set_image(url=image)
                if thumbnail: embed.set_thumbnail(url=thumbnail)
                await msg.edit(embed=embed)
                await interaction.response.send_message("Embed updated!", ephemeral=True)
                return
            except:
                continue
        await interaction.response.send_message("Message not found!", ephemeral=True)

    @app_commands.command(name="embed_send", description="Send a saved embed template to a channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed_send(self, interaction: discord.Interaction,
                         name: str,
                         channel: discord.TextChannel):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT data FROM embed_templates WHERE guild_id=? AND name=?",
                (interaction.guild.id, name.lower()))
            row = await cursor.fetchone()
        if not row:
            await interaction.response.send_message(
                f"No template found with name `{name}`!", ephemeral=True)
            return
        data = json.loads(row[0])
        embed = self.build_embed(data)
        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"Template `{name}` sent to {channel.mention}!", ephemeral=True)

    @app_commands.command(name="embed_list", description="List all saved embed templates")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed_list(self, interaction: discord.Interaction):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT name FROM embed_templates WHERE guild_id=?",
                (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No saved templates yet!", ephemeral=True)
            return
        embed = discord.Embed(title="Saved Embed Templates", color=discord.Color.blurple())
        embed.description = "\n".join(f"• `{row[0]}`" for row in rows)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="embed_delete_template", description="Delete a saved embed template")
    @app_commands.checks.has_permissions(administrator=True)
    async def embed_delete_template(self, interaction: discord.Interaction, name: str):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM embed_templates WHERE guild_id=? AND name=?",
                (interaction.guild.id, name.lower()))
            await db.commit()
        await interaction.response.send_message(
            f"Template `{name}` deleted!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(EmbedBuilder(bot))
