import discord
from discord.ext import commands
from discord import app_commands
from utils.bot_profile import get_guild_bot_profile, apply_bot_profile_via_rest


class BotProfile(commands.Cog):
    """
    Per-server bot identity: nickname, avatar, banner, and bio, all
    scoped to a single guild via Discord's "Modify Current Member"
    endpoint. The write path (Save button) lives in
    dashboard/api/botprofile.py and calls Discord's REST API directly
    with the bot token — same pattern dashboard/api/core.py already
    uses for role/channel lookups — so it applies instantly with no
    dependency on this cog or the bot's gateway connection.

    This cog's only job: if the bot is removed from a server and later
    re-invited, Discord resets its guild member row (nick/avatar/
    banner/bio all reset) — on_guild_join reapplies whatever was last
    configured, same "reapply stored config on join" pattern
    cogs/welcome.py already uses for auto-role.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            profile = await get_guild_bot_profile(guild.id)
            if not any([profile.get("nickname"), profile.get("avatar_url"),
                        profile.get("banner_url"), profile.get("bio")]):
                return  # nothing configured for this guild — nothing to restore
            result = apply_bot_profile_via_rest(
                guild.id,
                profile.get("nickname"),
                profile.get("avatar_url"),
                profile.get("banner_url"),
                profile.get("bio"),
            )
            if not result.get("success"):
                print(f"[BOTPROFILE] Failed to restore profile for "
                      f"guild {guild.id}: {result.get('errors')}")
        except Exception as e:
            print(f"[BOTPROFILE] on_guild_join error for guild {guild.id}: {e}")

    @app_commands.command(name="botprofile_view",
                          description="View this server's configured bot profile")
    @app_commands.checks.has_permissions(administrator=True)
    async def botprofile_view(self, interaction: discord.Interaction):
        profile = await get_guild_bot_profile(interaction.guild.id)
        embed = discord.Embed(
            title=f"🪪 Bot Profile — {interaction.guild.name}",
            description=("This is the bot's real, per-server Discord identity — "
                         "set only for this server, via Discord's guild member "
                         "profile fields."),
            color=0x7c5cbf)
        embed.add_field(name="Nickname", value=profile.get("nickname") or "*(none set)*", inline=True)
        embed.add_field(name="Bio", value=profile.get("bio") or "*(none set)*", inline=False)
        if profile.get("avatar_url"):
            embed.set_thumbnail(url=profile["avatar_url"])
        if profile.get("banner_url"):
            embed.set_image(url=profile["banner_url"])
        embed.set_footer(text="Manage this from the dashboard → Config → Bot Profile")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BotProfile(bot))
