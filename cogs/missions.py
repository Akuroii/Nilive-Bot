import discord
from discord.ext import commands
from discord import app_commands
from utils.mission_engine import (
    ensure_tables, get_definitions, get_user_progress, record_activity,
    VALID_TYPES, VALID_PERIODS, VALID_REWARD_TYPES,
)

TYPE_LABEL = {
    "messages": "Messages sent",
    "words": "Words typed",
    "voice_minutes": "Minutes in voice",
}
PERIOD_LABEL = {"daily": "Daily", "weekly": "Weekly", "once": "One-time"}


class Missions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await ensure_tables()

    # ─── PROGRESS HOOKS (same events cogs/mvp.py and cogs/leveling.py
    # already listen to — no new tracking wired anywhere else) ──────
    @commands.Cog.listener()
    async def on_activity_message(self, message: discord.Message,
                                   word_count: int):
        if message.author.bot or not message.guild:
            return
        await record_activity(self.bot, message.guild.id,
                              message.author.id, "messages", 1)
        if word_count > 0:
            await record_activity(self.bot, message.guild.id,
                                  message.author.id, "words", word_count)

    @commands.Cog.listener()
    async def on_activity_voice_tick(self, guild: discord.Guild,
                                      member: discord.Member, flags: dict):
        try:
            await record_activity(self.bot, guild.id, member.id,
                                  "voice_minutes", 1)
        except Exception as e:
            print(f"[MISSIONS] voice tick error for member {member.id} "
                  f"in guild {guild.id}: {e}")

    # ─── SLASH COMMANDS ──────────────────────────────────
    @app_commands.command(name="missions",
                          description="View your active missions and progress")
    async def missions(self, interaction: discord.Interaction):
        progress = await get_user_progress(interaction.guild.id, interaction.user.id)
        if not progress:
            await interaction.response.send_message(
                "No missions configured yet — an admin can add some via the dashboard "
                "or /mission_create.", ephemeral=True)
            return

        embed = discord.Embed(title="🗺️ Your Missions", color=0x7c5cbf)
        for m in progress:
            status = "✅ Complete" if m["completed"] else f"{m['progress']}/{m['target']}"
            embed.add_field(
                name=f"{m['name']} ({PERIOD_LABEL.get(m['period'], m['period'])})",
                value=(f"{m['description'] or TYPE_LABEL.get(m['type'], m['type'])}\n"
                       f"Progress: **{status}**"),
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="mission_create",
                          description="Create a mission (admin)")
    @app_commands.describe(
        name="Display name",
        type="messages / words / voice_minutes",
        target="How much progress is needed to complete it",
        period="daily / weekly / once",
        reward_type="coins / diamonds / xp / role / temp_role / item",
        reward_value="Amount, Role ID, or item name",
        duration_hours="Only used for temp_role")
    @app_commands.checks.has_permissions(administrator=True)
    async def mission_create(self, interaction: discord.Interaction,
                             name: str, type: str, target: int,
                             reward_type: str, reward_value: str,
                             period: str = "daily",
                             description: str = None,
                             duration_hours: int = None):
        type = type.lower().strip()
        period = period.lower().strip()
        if type not in VALID_TYPES:
            await interaction.response.send_message(
                f"type must be one of: {', '.join(VALID_TYPES)}", ephemeral=True)
            return
        if period not in VALID_PERIODS:
            await interaction.response.send_message(
                f"period must be one of: {', '.join(VALID_PERIODS)}", ephemeral=True)
            return
        if reward_type not in VALID_REWARD_TYPES:
            await interaction.response.send_message(
                f"reward_type must be one of: {', '.join(VALID_REWARD_TYPES)}",
                ephemeral=True)
            return
        if target <= 0:
            await interaction.response.send_message(
                "target must be positive.", ephemeral=True)
            return

        import aiosqlite
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO missions_definitions
                    (guild_id, name, description, type, target, period,
                     reward_type, reward_value, reward_duration_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (interaction.guild.id, name, description, type, target,
                  period, reward_type, reward_value, duration_hours))
            await db.commit()

        await interaction.response.send_message(
            f"✅ Created mission **{name}** — {target} {TYPE_LABEL.get(type, type)} "
            f"({PERIOD_LABEL.get(period, period)}) → {reward_type}: {reward_value}",
            ephemeral=True)

    @app_commands.command(name="mission_list",
                          description="List configured missions (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mission_list(self, interaction: discord.Interaction):
        defs = await get_definitions(interaction.guild.id, enabled_only=False)
        if not defs:
            await interaction.response.send_message(
                "No missions configured yet.", ephemeral=True)
            return
        embed = discord.Embed(title="🗺️ Configured Missions", color=0x7c5cbf)
        for d in defs:
            status = "✅" if d["enabled"] else "❌"
            embed.add_field(
                name=f"#{d['id']} {status} {d['name']}",
                value=(f"{d['target']} {TYPE_LABEL.get(d['type'], d['type'])} "
                       f"({PERIOD_LABEL.get(d['period'], d['period'])}) → "
                       f"{d['reward_type']}: {d['reward_value']}"),
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="mission_remove",
                          description="Remove a mission by ID (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mission_remove(self, interaction: discord.Interaction, mission_id: int):
        import aiosqlite
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM missions_definitions WHERE id=? AND guild_id=?",
                (mission_id, interaction.guild.id))
            await db.commit()
            found = cursor.rowcount > 0
        if found:
            await interaction.response.send_message(
                f"Removed mission #{mission_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"No mission #{mission_id} found.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Missions(bot))
