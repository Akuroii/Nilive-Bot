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
            "SELECT * FROM boost_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return {}


def _booster_level(member: discord.Member) -> int:
    """
    FEATURE (dark-fixes pass #2, boost_color_roles): reuses the same
    "how many boosts is this member contributing" signal the existing
    boost1/boost2 logic above already relies on
    (premium_subscription_count), so a color role's requires_boost_level
    lines up with the same tiers admins already think in terms of via
    /boost_setup's boost1/boost2 roles. 0 if not currently boosting.
    """
    if not member.premium_since:
        return 0
    return getattr(member, "premium_subscription_count", 1) or 1


async def get_boost_color_roles(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, role_id, role_name, requires_boost_level
            FROM boost_color_roles WHERE guild_id = ? ORDER BY requires_boost_level ASC
        """, (guild_id,))
        rows = await cursor.fetchall()
    return [{"id": r[0], "role_id": r[1], "role_name": r[2],
              "requires_boost_level": r[3]} for r in rows]


class Boost(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild  = after.guild
        config = await get_boost_config(guild.id)
        if not config or not config.get("enabled", 1):
            return

        before_boosts = before.premium_since
        after_boosts  = after.premium_since

        boost1_id  = config.get("boost1_role_id")
        boost2_id  = config.get("boost2_role_id")
        channel_id = config.get("boost2_channel_id")

        if not before_boosts and after_boosts:
            await self._handle_new_boost(guild, after, boost1_id, boost2_id, channel_id, config)
        elif before_boosts and not after_boosts:
            await self._handle_unboost(guild, after, boost1_id, boost2_id, config)
        elif before_boosts and after_boosts:
            before_count = getattr(before, "premium_subscription_count", 0) or 0
            after_count  = getattr(after, "premium_subscription_count", 0) or 0
            if after_count > before_count and after_count >= 2:
                await self._give_role(guild, after, boost2_id)

    async def _handle_new_boost(self, guild, member, boost1_id, boost2_id, channel_id, config):
        await self._give_role(guild, member, boost1_id)

        boost_count = getattr(member, "premium_subscription_count", 1) or 1
        if boost_count >= 2:
            await self._give_role(guild, member, boost2_id)

        # Finalized Prestige: an active Booster has effective Prestige VI.
        # Roles are cosmetic only — entitlement derives from premium_since.
        # Sync the configured tier-VI prestige role (if any) as a
        # representation of that state. Never touches Coins/XP/Level.
        try:
            from utils.prestige import sync_prestige_roles, BOOSTER_TIER
            await sync_prestige_roles(
                self.bot, guild, member, effective_tier=BOOSTER_TIER)
        except Exception as e:
            print(f"[BOOST] Prestige VI role sync (new boost) failed: {e}")

        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel:
                embed = discord.Embed(
                    title="💜 New Booster!",
                    description=f"{member.mention} just boosted the server! Thank you!",
                    color=0xf47fff)
                if member.display_avatar:
                    embed.set_thumbnail(url=member.display_avatar.url)
                try:
                    await channel.send(embed=embed)
                except Exception:
                    pass

    async def _handle_unboost(self, guild, member, boost1_id, boost2_id, config):
        if not config.get("auto_remove_on_unboost", 1):
            return

        for role_id in [boost1_id, boost2_id]:
            if not role_id:
                continue
            role = guild.get_role(int(role_id))
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Boost ended")
                except Exception as e:
                    print(f"[BOOST] Failed to remove role: {e}")

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
                    await member.remove_roles(role, reason="Boost ended")
                except Exception:
                    pass

        # FEATURE (dark-fixes pass #2, boost_color_roles): a self-picked
        # color role is a boost perk too, so it comes off on unboost
        # under the same auto_remove_on_unboost setting checked above
        # (this function already returned early if that's disabled).
        color_roles = await get_boost_color_roles(guild.id)
        for cr in color_roles:
            role = guild.get_role(cr["role_id"])
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Boost ended")
                except Exception:
                    pass

        # Finalized Prestige: when the boost ends, effective Prestige
        # returns to the permanent tier (never VI). Sync the configured
        # prestige roles so the member wears their permanent tier's role
        # (and loses the temporary VI role). Representation only; never
        # touches Coins/XP/Level.
        try:
            from utils.prestige import sync_prestige_roles, get_permanent_prestige
            perm = await get_permanent_prestige(guild.id, member.id)
            await sync_prestige_roles(self.bot, guild, member, effective_tier=perm)
        except Exception as e:
            print(f"[BOOST] Prestige role sync (unboost) failed: {e}")

    async def _give_role(self, guild, member, role_id):
        if not role_id:
            return
        role = guild.get_role(int(role_id))
        if not role:
            print(f"[BOOST] Role {role_id} not found in guild")
            return
        can_assign, warning = check_bot_role_position(guild, role)
        if not can_assign:
            print(f"[BOOST ROLE WARNING] {warning}")
            return
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Server boost reward")
            except Exception as e:
                print(f"[BOOST] Failed to add role: {e}")

    @app_commands.command(name="boost_setup", description="Configure boost roles")
    @app_commands.checks.has_permissions(administrator=True)
    async def boost_setup(self, interaction: discord.Interaction,
                          boost1_role: discord.Role = None,
                          boost2_role: discord.Role = None,
                          announce_channel: discord.TextChannel = None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_config
                    (guild_id, boost1_role_id, boost2_role_id, boost2_channel_id, enabled, auto_remove_on_unboost)
                VALUES (?, ?, ?, ?, 1, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    boost1_role_id    = excluded.boost1_role_id,
                    boost2_role_id    = excluded.boost2_role_id,
                    boost2_channel_id = excluded.boost2_channel_id
            """, (interaction.guild.id, boost1_role.id if boost1_role else None,
                  boost2_role.id if boost2_role else None,
                  announce_channel.id if announce_channel else None))
            await db.commit()

        warnings = []
        for role in [boost1_role, boost2_role]:
            if role:
                can, warn = check_bot_role_position(interaction.guild, role)
                if not can:
                    warnings.append(warn)

        embed = discord.Embed(title="Boost System Configured", color=0x57F287)
        if boost1_role:
            embed.add_field(name="1st Boost Role", value=boost1_role.mention)
        if boost2_role:
            embed.add_field(name="2nd Boost Role", value=boost2_role.mention)
        if announce_channel:
            embed.add_field(name="Announce Channel", value=announce_channel.mention)
        if warnings:
            embed.add_field(name="⚠️ Role Position Warnings", value="\n".join(warnings), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="boosters", description="List current server boosters")
    async def boosters(self, interaction: discord.Interaction):
        boosters = [m for m in interaction.guild.members if m.premium_since]
        if not boosters:
            await interaction.response.send_message("No boosters yet.", ephemeral=True)
            return
        embed = discord.Embed(title=f"💜 Server Boosters ({len(boosters)})", color=0xf47fff)
        embed.description = "\n".join(
            f"• {m.mention} — since {m.premium_since.strftime('%Y-%m-%d')}" for m in boosters)
        await interaction.response.send_message(embed=embed)

    # ── boost_color_roles feature ────────────────────────────────────
    # FEATURE (dark-fixes pass #2): boost_color_roles had a schema
    # (database.py) with no implementation anywhere — this closes that
    # gap. Lets boosters self-pick a cosmetic color role from an
    # admin-configured list, gated per-role by boost level.

    @app_commands.command(name="boostcolor_add",
                          description="Add a self-pickable color role for boosters (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def boostcolor_add(self, interaction: discord.Interaction,
                             role: discord.Role, requires_boost_level: int = 1):
        can_assign, warning = check_bot_role_position(interaction.guild, role)
        if not can_assign:
            await interaction.response.send_message(f"⚠️ {warning}", ephemeral=True)
            return
        if requires_boost_level < 1:
            await interaction.response.send_message(
                "requires_boost_level must be at least 1.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_color_roles (guild_id, role_id, role_name, requires_boost_level)
                VALUES (?, ?, ?, ?)
            """, (interaction.guild.id, role.id, role.name, requires_boost_level))
            await db.commit()
        await interaction.response.send_message(
            f"✅ Added {role.mention} as a boost color option "
            f"(requires {requires_boost_level} boost{'s' if requires_boost_level != 1 else ''}).",
            ephemeral=True)

    @app_commands.command(name="boostcolor_remove",
                          description="Remove a color role from the boost picker (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def boostcolor_remove(self, interaction: discord.Interaction, role: discord.Role):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM boost_color_roles WHERE guild_id = ? AND role_id = ?",
                (interaction.guild.id, role.id))
            await db.commit()
            removed = cursor.rowcount > 0
        if removed:
            await interaction.response.send_message(
                f"Removed {role.mention} from the boost color picker.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "That role wasn't in the boost color picker.", ephemeral=True)

    @app_commands.command(name="boostcolor_list",
                          description="List configured boost color role options")
    async def boostcolor_list(self, interaction: discord.Interaction):
        options = await get_boost_color_roles(interaction.guild.id)
        if not options:
            await interaction.response.send_message(
                "No boost color roles configured yet. Admins can add some with /boostcolor_add.",
                ephemeral=True)
            return
        embed = discord.Embed(title="🎨 Boost Color Roles", color=0xf47fff)
        for opt in options:
            role = interaction.guild.get_role(opt["role_id"])
            label = role.mention if role else f"(deleted role: {opt['role_name']})"
            embed.add_field(
                name=f"Requires {opt['requires_boost_level']} boost"
                     f"{'s' if opt['requires_boost_level'] != 1 else ''}",
                value=label, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _boostcolor_autocomplete(self, interaction: discord.Interaction, current: str):
        level = _booster_level(interaction.user)
        options = await get_boost_color_roles(interaction.guild.id)
        eligible = [o for o in options if o["requires_boost_level"] <= level]
        return [
            app_commands.Choice(name=o["role_name"], value=str(o["role_id"]))
            for o in eligible
            if current.lower() in o["role_name"].lower()
        ][:25]

    @app_commands.command(name="boostcolor",
                          description="Pick your boost color role")
    @app_commands.describe(color="Choose from the roles you're eligible for")
    @app_commands.autocomplete(color=_boostcolor_autocomplete)
    async def boostcolor(self, interaction: discord.Interaction, color: str):
        level = _booster_level(interaction.user)
        if level < 1:
            await interaction.response.send_message(
                "This is a booster perk — boost the server to unlock a color role.",
                ephemeral=True)
            return

        options = await get_boost_color_roles(interaction.guild.id)
        try:
            target_role_id = int(color)
        except ValueError:
            await interaction.response.send_message(
                "Pick an option from the autocomplete list.", ephemeral=True)
            return

        chosen = next((o for o in options if o["role_id"] == target_role_id), None)
        if not chosen:
            await interaction.response.send_message(
                "That's not a configured color role. Pick one from the list.", ephemeral=True)
            return
        if chosen["requires_boost_level"] > level:
            await interaction.response.send_message(
                f"You need {chosen['requires_boost_level']} boosts for that color "
                f"(you currently have {level}).", ephemeral=True)
            return

        role = interaction.guild.get_role(target_role_id)
        if not role:
            await interaction.response.send_message(
                "That role no longer exists on this server — ask an admin to reconfigure it.",
                ephemeral=True)
            return
        can_assign, warning = check_bot_role_position(interaction.guild, role)
        if not can_assign:
            await interaction.response.send_message(f"⚠️ {warning}", ephemeral=True)
            return

        # Single-select: swap out any other configured color role the
        # member currently holds before assigning the new one, so
        # members don't stack multiple color roles at once.
        other_role_ids = {o["role_id"] for o in options if o["role_id"] != target_role_id}
        to_remove = [r for r in interaction.user.roles if r.id in other_role_ids]
        try:
            if to_remove:
                await interaction.user.remove_roles(*to_remove, reason="Switching boost color")
            if role not in interaction.user.roles:
                await interaction.user.add_roles(role, reason="Boost color pick")
        except Exception as e:
            await interaction.response.send_message(f"Couldn't update your role: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🎨 You're now wearing {role.mention}!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Boost(bot))
