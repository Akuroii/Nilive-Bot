import discord
from discord.ext import commands
from discord import app_commands
from utils.bot_profile import get_guild_bot_profile


class BotProfile(commands.Cog):
    """
    Per-server bot identity.

    Nickname is the only part of a bot's per-server appearance Discord's
    API actually supports changing — see utils/bot_profile.py's header
    for why avatar can't work the same way. The write path for nickname
    lives in dashboard/api/botprofile.py, which calls Discord's REST API
    directly with the bot token (utils.bot_profile.apply_nickname_via_rest)
    — that's what actually applies a change live, immediately, with no
    dependency on this cog or the bot's gateway connection.

    This cog only handles one edge case: on_guild_join re-applies a
    previously configured nickname if the bot is removed from a server
    and later re-invited (Discord resets nickname on member-object
    recreation) — same "reapply stored config on join" shape as
    cogs/welcome.py's auto-role.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            profile = await get_guild_bot_profile(guild.id)
            nickname = profile.get("nickname")
            if nickname and guild.me and guild.me.nick != nickname:
                await guild.me.edit(
                    nick=nickname,
                    reason="Restoring configured per-server bot nickname")
        except Exception as e:
            print(f"[BOTPROFILE] Failed to restore nickname for "
                  f"guild {guild.id}: {e}")

    @app_commands.command(name="botprofile_view",
                          description="View this server's configured bot profile")
    @app_commands.checks.has_permissions(administrator=True)
    async def botprofile_view(self, interaction: discord.Interaction):
        profile = await get_guild_bot_profile(interaction.guild.id)
        embed = discord.Embed(title="🤖 Bot Profile — This Server", color=0x7c5cbf)
        embed.add_field(
            name="Nickname",
            value=profile.get("nickname") or "*(using global name)*",
            inline=False)
        embed.add_field(
            name="Branding icon",
            value=((profile.get("avatar_url") or "*(none set)*") +
                   "\n*(Discord doesn't support a real per-server bot "
                   "avatar — this is only used for branding in the "
                   "bot's own embeds.)*"),
            inline=False)
        embed.set_footer(text="Manage this from the dashboard → Config → Bot Profile")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BotProfile(bot))
