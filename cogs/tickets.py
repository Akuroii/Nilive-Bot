import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH
from datetime import datetime

class TicketCategory(discord.ui.Select):
    def __init__(self, categories):
        options = [
            discord.SelectOption(label=cat, emoji=emoji)
            for cat, emoji in categories
        ]
        super().__init__(placeholder="Select a category...", options=options, custom_id="ticket_category")

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])

class TicketCreateView(discord.ui.View):
    def __init__(self, categories):
        super().__init__(timeout=None)
        self.add_item(TicketCategory(categories))

class TicketOpenButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT categories FROM ticket_config WHERE guild_id=?
            """, (interaction.guild.id,))
            row = await cursor.fetchone()
        if row and row[0]:
            cats = [(c.strip(), "🎫") for c in row[0].split(",")]
            view = TicketCreateView(cats)
            await interaction.response.send_message("Please select a category:", view=view, ephemeral=True)
        else:
            await create_ticket(interaction, "General Support")

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)

    @discord.ui.button(label="Claim", emoji="✋", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT staff_role_id FROM ticket_config WHERE guild_id=?",
                (interaction.guild.id,))
            row = await cursor.fetchone()
        if row:
            staff_role = interaction.guild.get_role(row[0])
            if staff_role and staff_role not in interaction.user.roles:
                await interaction.response.send_message("Only staff can claim tickets!", ephemeral=True)
                return
        await interaction.channel.edit(topic=f"Claimed by {interaction.user.display_name}")
        embed = discord.Embed(
            description=f"Ticket claimed by {interaction.user.mention}",
            color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="Add Member", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="ticket_add")
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Use `/ticket_add @member` to add someone to this ticket.", ephemeral=True)

class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reopen", emoji="🔓", style=discord.ButtonStyle.success, custom_id="ticket_reopen")
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id, staff_role_id FROM tickets WHERE channel_id=?",
                (interaction.channel.id,))
            row = await cursor.fetchone()
        if not row:
            await interaction.response.send_message("Ticket data not found!", ephemeral=True)
            return
        user_id, staff_role_id = row
        member = interaction.guild.get_member(user_id)
        staff_role = interaction.guild.get_role(staff_role_id) if staff_role_id else None
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        await interaction.channel.edit(overwrites=overwrites)
        await db.execute("UPDATE tickets SET status='open' WHERE channel_id=?", (interaction.channel.id,))
        await db.commit()
        embed = discord.Embed(description=f"Ticket reopened by {interaction.user.mention}", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, view=TicketControlView())

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="ticket_delete")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT staff_role_id FROM ticket_config WHERE guild_id=?",
                (interaction.guild.id,))
            row = await cursor.fetchone()
        if row:
            staff_role = interaction.guild.get_role(row[0])
            if staff_role and staff_role not in interaction.user.roles:
                await interaction.response.send_message("Only staff can delete tickets!", ephemeral=True)
                return
        await save_transcript(interaction.channel, interaction.guild)
        await interaction.channel.delete()

async def create_ticket(interaction: discord.Interaction, category: str):
    guild = interaction.guild
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT staff_role_id, ticket_category_id, log_channel_id FROM ticket_config WHERE guild_id=?",
            (guild.id,))
        config = await cursor.fetchone()
        existing = await db.execute(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
            (guild.id, interaction.user.id))
        existing_row = await existing.fetchone()

    if existing_row:
        await interaction.response.send_message(
            f"You already have an open ticket! <#{existing_row[0]}>", ephemeral=True)
        return

    staff_role_id = config[0] if config else None
    category_id = config[1] if config else None
    log_channel_id = config[2] if config else None
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    ticket_category = guild.get_channel(category_id) if category_id else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=?", (guild.id,))
        count = (await cursor.fetchone())[0] + 1

    channel_name = f"ticket-{count:04d}"
    channel = await guild.create_text_channel(
        name=channel_name,
        overwrites=overwrites,
        category=ticket_category,
        topic=f"Ticket by {interaction.user.display_name} | Category: {category}"
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tickets (guild_id, channel_id, user_id, staff_role_id, status, category, created_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
        """, (guild.id, channel.id, interaction.user.id, staff_role_id, category,
              datetime.utcnow().isoformat()))
        await db.commit()

    embed = discord.Embed(
        title=f"Ticket #{count:04d} — {category}",
        description=f"Hello {interaction.user.mention}! Support will be with you shortly.\n\nPlease describe your issue in detail.",
        color=discord.Color.blurple()
    )
    embed.set_footer(text=f"Opened by {interaction.user.display_name}")
    await channel.send(
        content=f"{interaction.user.mention}{' | ' + staff_role.mention if staff_role else ''}",
        embed=embed,
        view=TicketControlView()
    )

    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            log_embed = discord.Embed(
                title="Ticket Opened",
                color=discord.Color.green())
            log_embed.add_field(name="User", value=interaction.user.mention)
            log_embed.add_field(name="Category", value=category)
            log_embed.add_field(name="Channel", value=channel.mention)
            await log_channel.send(embed=log_embed)

    await interaction.response.send_message(f"Ticket created! {channel.mention}", ephemeral=True)

async def close_ticket(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, staff_role_id FROM tickets WHERE channel_id=? AND status='open'",
            (interaction.channel.id,))
        row = await cursor.fetchone()
    if not row:
        await interaction.response.send_message("This is not an open ticket!", ephemeral=True)
        return
    user_id, staff_role_id = row
    staff_role = interaction.guild.get_role(staff_role_id) if staff_role_id else None
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    await interaction.channel.edit(overwrites=overwrites)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status='closed' WHERE channel_id=?",
            (interaction.channel.id,))
        await db.commit()
    embed = discord.Embed(
        description=f"Ticket closed by {interaction.user.mention}",
        color=discord.Color.red())
    await interaction.response.send_message(embed=embed, view=ClosedTicketView())

async def save_transcript(channel: discord.TextChannel, guild: discord.Guild):
    messages = []
    async for msg in channel.history(limit=500, oldest_first=True):
        messages.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.display_name}: {msg.content}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT log_channel_id FROM ticket_config WHERE guild_id=?", (guild.id,))
        row = await cursor.fetchone()
    if row and row[0]:
        log_channel = guild.get_channel(row[0])
        if log_channel:
            transcript_text = "\n".join(messages)
            file = discord.File(
                fp=__import__('io').StringIO(transcript_text),
                filename=f"transcript-{channel.name}.txt")
            embed = discord.Embed(
                title=f"Transcript — {channel.name}",
                color=discord.Color.blurple())
            await log_channel.send(embed=embed, file=file)

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_config (
                    guild_id INTEGER PRIMARY KEY,
                    staff_role_id INTEGER,
                    ticket_category_id INTEGER,
                    log_channel_id INTEGER,
                    categories TEXT DEFAULT 'General Support,Report,Ban Appeal,Other'
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER,
                    staff_role_id INTEGER,
                    status TEXT DEFAULT 'open',
                    category TEXT,
                    created_at TEXT
                )
            """)
            await db.commit()
        self.bot.add_view(TicketOpenButton())
        self.bot.add_view(TicketControlView())
        self.bot.add_view(ClosedTicketView())

    @app_commands.command(name="ticket_setup", description="Set up the ticket system")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction: discord.Interaction,
                           channel: discord.TextChannel,
                           staff_role: discord.Role,
                           log_channel: discord.TextChannel,
                           ticket_category: discord.CategoryChannel = None,
                           categories: str = "General Support,Report,Ban Appeal,Other"):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO ticket_config
                (guild_id, staff_role_id, ticket_category_id, log_channel_id, categories)
                VALUES (?, ?, ?, ?, ?)
            """, (interaction.guild.id, staff_role.id,
                  ticket_category.id if ticket_category else None,
                  log_channel.id, categories))
            await db.commit()
        embed = discord.Embed(
            title="Ticket System",
            description="Click the button below to open a support ticket!",
            color=discord.Color.blurple())
        embed.set_footer(text="One ticket per user at a time")
        await channel.send(embed=embed, view=TicketOpenButton())
        await interaction.response.send_message(
            f"Ticket system set up in {channel.mention}!", ephemeral=True)

    @app_commands.command(name="ticket_add", description="Add a member to the current ticket")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.send_message(f"Added {member.mention} to the ticket!")

    @app_commands.command(name="ticket_remove", description="Remove a member from the current ticket")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.channel.set_permissions(member, view_channel=False)
        await interaction.response.send_message(f"Removed {member.mention} from the ticket!")

    @app_commands.command(name="ticket_close", description="Close the current ticket")
    async def ticket_close(self, interaction: discord.Interaction):
        await close_ticket(interaction)

async def setup(bot):
    await bot.add_cog(Tickets(bot))
