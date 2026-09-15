import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH

class Sticky(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sticky_cache = {}

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sticky_messages (
                    guild_id INTEGER,
                    channel_id INTEGER,
                    content TEXT,
                    last_message_id INTEGER,
                    PRIMARY KEY (guild_id, channel_id)
                )
            """)
            await db.commit()

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_table()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT content, last_message_id FROM sticky_messages WHERE guild_id=? AND channel_id=?",
                (message.guild.id, message.channel.id))
            row = await cursor.fetchone()
        if not row:
            return
        content, last_msg_id = row
        if last_msg_id:
            try:
                old_msg = await message.channel.fetch_message(last_msg_id)
                await old_msg.delete()
            except:
                pass
        embed = discord.Embed(description=content, color=discord.Color.yellow())
        embed.set_footer(text="📌 Sticky Message")
        new_msg = await message.channel.send(embed=embed)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE sticky_messages SET last_message_id=? WHERE guild_id=? AND channel_id=?",
                (new_msg.id, message.guild.id, message.channel.id))
            await db.commit()

    @app_commands.command(name="sticky_set", description="Set a sticky message in a channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def sticky_set(self, interaction: discord.Interaction,
                         channel: discord.TextChannel,
                         content: str):
        await self.ensure_table()
        embed = discord.Embed(description=content, color=discord.Color.yellow())
        embed.set_footer(text="📌 Sticky Message")
        msg = await channel.send(embed=embed)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO sticky_messages (guild_id, channel_id, content, last_message_id)
                VALUES (?, ?, ?, ?)
            """, (interaction.guild.id, channel.id, content, msg.id))
            await db.commit()
        await interaction.response.send_message(
            f"Sticky message set in {channel.mention}!", ephemeral=True)

    @app_commands.command(name="sticky_remove", description="Remove the sticky message from a channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def sticky_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT last_message_id FROM sticky_messages WHERE guild_id=? AND channel_id=?",
                (interaction.guild.id, channel.id))
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    msg = await channel.fetch_message(row[0])
                    await msg.delete()
                except:
                    pass
            await db.execute(
                "DELETE FROM sticky_messages WHERE guild_id=? AND channel_id=?",
                (interaction.guild.id, channel.id))
            await db.commit()
        await interaction.response.send_message(
            f"Sticky message removed from {channel.mention}!", ephemeral=True)

    @app_commands.command(name="sticky_list", description="List all sticky messages in the server")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def sticky_list(self, interaction: discord.Interaction):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT channel_id, content FROM sticky_messages WHERE guild_id=?",
                (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No sticky messages set!", ephemeral=True)
            return
        embed = discord.Embed(title="Sticky Messages", color=discord.Color.yellow())
        for ch_id, content in rows:
            embed.add_field(
                name=f"<#{ch_id}>",
                value=content[:100] + ("..." if len(content) > 100 else ""),
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Sticky(bot))
