import discord
from discord import app_commands
from discord.ext import commands

from database import DEV_DISCORD_IDS
from utils import guild_allowlist


class _ConfirmLeave(discord.ui.View):
    """Confirm / Cancel for one leave request. Only the developer who asked can press."""

    def __init__(self, requester_id: int, guild: discord.Guild):
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.guild = guild
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Not your request.", ephemeral=True)
            return False
        return True

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Leave server", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        self.stop()
        try:
            await self.guild.leave()
        except Exception as exc:
            print(f"[GUILD-ADMIN] dev={interaction.user.id} FAILED to leave "
                  f"guild={self.guild.id} ({self.guild.name}): {exc!r}")
            await interaction.response.edit_message(
                content=f"Could not leave **{self.guild.name}**: `{exc}`", view=self)
            return
        print(f"[GUILD-ADMIN] dev={interaction.user.id} left guild={self.guild.id} ({self.guild.name})")
        await interaction.response.edit_message(
            content=f"Left **{self.guild.name}** (`{self.guild.id}`).", view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        self.stop()
        await interaction.response.edit_message(content="Cancelled. The bot stays.", view=self)

    async def on_timeout(self):
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(content="Timed out. The bot stays.", view=self)
            except Exception:
                pass


class GuildAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dev_leave_guild",
                          description="Developer only: make the bot leave a server")
    @app_commands.describe(guild_id="ID of the server the bot should leave")
    @app_commands.default_permissions(administrator=True)
    async def dev_leave_guild(self, interaction: discord.Interaction, guild_id: str):
        if interaction.user.id not in DEV_DISCORD_IDS:
            await interaction.response.send_message("Developer only.", ephemeral=True)
            return

        raw = guild_id.strip()
        if not (raw.isascii() and raw.isdigit()):
            await interaction.response.send_message("That is not a server ID.", ephemeral=True)
            return
        gid = int(raw)

        # A server on the allowlist is protected: remove it from ALLOWED_GUILD_IDS first.
        if gid in guild_allowlist.get_config().ids:
            await interaction.response.send_message(
                "That server is in `ALLOWED_GUILD_IDS`, so it is protected. "
                "Remove it from the list first if you really mean it.", ephemeral=True)
            return

        guild = self.bot.get_guild(gid)
        if guild is None:
            await interaction.response.send_message(
                "The bot is not in a server with that ID.", ephemeral=True)
            return

        view = _ConfirmLeave(interaction.user.id, guild)
        await interaction.response.send_message(
            f"Leave **{guild.name}** (`{guild.id}`, {guild.member_count or '?'} members, "
            f"owner `{guild.owner_id}`)?", view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(GuildAdmin(bot))
