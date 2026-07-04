import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH
from utils.permissions import check_bot_role_position
from utils.formatters import now_iso


async def get_boost_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM boost_config WHERE guild_id = ?",
            (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return {}


class Boost(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member,
                                after: discord.Member):
        guild  = after.guild
        config = await get_boost_config(guild.id)
        if not config or not config.get("enabled", 1):
            return

        before_boosts = before.premium_since
        after_boosts  = after.premium_since

        boost1_id  = config.get("boost1_role_id")
        boost2_id  = config.get("boost2_role_id")
        channel_id = config.get("boost2_channel_id")

        # Member just started boosting
        if not before_boosts and after_boosts:
            await self._handle_new_boost(
                guild, after, boost1_id, boost2_id,
                channel_id, config)

        # Member stopped boosting
        elif before_boosts and not after_boosts:
            await self._handle_unboost(
                guild, after, boost1_id, boost2_id, config)

        # Member had boost before and still does —
        # check if they gained a 2nd boost
        elif before_boosts and after_boosts:
            before_count = getattr(before, "premium_subscription_count", 0) or 0
            after_count  = getattr(after, "premium_subscription_count", 0) or 0
            if after_count > before_count and after_count >= 2:
                await self._give_role(guild, after, boost2_id)

    async def _handle_new_boost(self, guild, member, boost1_id,
                                  boost2_id, channel_id, config):
        # Give boost1 role
        await self._give_role(guild, member, boost1_id)

        boost_count = getattr(member, "premium_subscription_count", 1) or 1
        if boost_count >= 2:
            await self._give_role(guild, member, boost2_id)

        # Announce
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel:
                embed = discord.Embed(
                    title="💜 New Booster!",
                    description=(f"{member.mention} just boosted the server! "
                                 f"Thank you!"),
                    color=0xf47fff)
                if member.display_avatar:
                    embed.set_thumbnail(url=member.display_avatar.url)
                try:
                    await channel.send(embed=embed)
                except Exception:
                    pass

    async def _handle_unboost(self, guild, member, boost1_id,
                               boost2_id, config):
        if not config.get("auto_remove_on_unboost", 1):
            return

        for role_id in [boost1_id, boost2_id]:
            if not role_id:
                continue
            role = guild.get_role(int(role_id))
            if role and role in member.roles:
                try:
                    await member.remove_roles(
                        role, reason="Boost ended")
                except Exception as e:
                    print(f"[BOOST] Failed to remove role: {e}")

        # Remove any booster-only reaction roles
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT role_id FROM reaction_roles
                WHERE guild_id = ? AND booster_only = 1
            """, (guild.id,))
            booster_roles = await cursor.fetchall()

        for (role_id,) in booster_roles:
            role = guild.get_role(role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(
                        role, reason="Boost ended")
                except Exception:
                    pass

    async def _give_role(self, guild, member, role_id):
        if not role_id:
            return
        role = guild.get_role(int(role_id))
        if not role:
            print(f"[BOOST] Role {role_id} not found in guild")
            return
        # Rule 5 — Bot-Role Warning
        can_assign, warning = check_bot_role_position(guild, role)
        if not can_assign:
            print(f"[BOOST ROLE WARNING] {warning}")
            return
        if role not in member.roles:
            try:
                await member.add_roles(
                    role, reason="Server boost reward")
            except Exception as e:
                print(f"[BOOST] Failed to add role: {e}")

    @app_commands.command(name="boost_setup",
                          description="Configure boost roles")
    @app_commands.checks.has_permissions(administrator=True)
    async def boost_setup(self, interaction: discord.Interaction,
                          boost1_role: discord.Role = None,
                          boost2_role: discord.Role = None,
                          announce_channel: discord.TextChannel = None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_config
                    (guild_id, boost1_role_id, boost2_role_id,
                     boost2_channel_id, enabled,
                     auto_remove_on_unboost)
                VALUES (?, ?, ?, ?, 1, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    boost1_role_id    = excluded.boost1_role_id,
                    boost2_role_id    = excluded.boost2_role_id,
                    boost2_channel_id = excluded.boost2_channel_id
            """, (
                interaction.guild.id,
                boost1_role.id if boost1_role else None,
                boost2_role.id if boost2_role else None,
                announce_channel.id if announce_channel else None,
            ))
            await db.commit()

        # Rule 5 warnings
        warnings = []
        for role in [boost1_role, boost2_role]:
            if role:
                can, warn = check_bot_role_position(
                    interaction.guild, role)
                if not can:
                    warnings.append(warn)

        embed = discord.Embed(
            title="Boost System Configured",
            color=0x57F287)
        if boost1_role:
            embed.add_field(name="1st Boost Role",
                            value=boost1_role.mention)
        if boost2_role:
            embed.add_field(name="2nd Boost Role",
                            value=boost2_role.mention)
        if announce_channel:
            embed.add_field(name="Announce Channel",
                            value=announce_channel.mention)
        if warnings:
            embed.add_field(
                name="⚠️ Role Position Warnings",
                value="\n".join(warnings),
                inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True)

    @app_commands.command(name="boosters",
                          description="List current server boosters")
    async def boosters(self, interaction: discord.Interaction):
        boosters = [m for m in interaction.guild.members
                    if m.premium_since]
        if not boosters:
            await interaction.response.send_message(
                "No boosters yet.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"💜 Server Boosters ({len(boosters)})",
            color=0xf47fff)
        embed.description = "\n".join(
            f"• {m.mention} — since {m.premium_since.strftime('%Y-%m-%d')}"
            for m in boosters)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Boost(bot))
