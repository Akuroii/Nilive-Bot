import discord
from discord.ext import commands
from discord import app_commands
import random

class Roleplay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def rp_embed(self, action: str, user: discord.Member,
                 target: discord.Member = None, color: discord.Color = None) -> discord.Embed:
        embed = discord.Embed(color=color or discord.Color.pink())
        if target:
            embed.description = action.format(user=user.mention, target=target.mention)
        else:
            embed.description = action.format(user=user.mention)
        return embed

    @app_commands.command(name="hug", description="Hug someone!")
    async def hug(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} gives {target} a warm hug!",
            "{user} wraps their arms around {target}!",
            "{user} hugs {target} tightly!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.pink()))

    @app_commands.command(name="pat", description="Pat someone!")
    async def pat(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} pats {target} on the head!",
            "{user} gently pats {target}!",
            "{user} gives {target} a friendly pat!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.blurple()))

    @app_commands.command(name="slap", description="Slap someone!")
    async def slap(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} slaps {target} across the face!",
            "{user} gives {target} a hard slap!",
            "{user} slaps {target}!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.red()))

    @app_commands.command(name="kiss", description="Kiss someone!")
    async def kiss(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} kisses {target}!",
            "{user} gives {target} a kiss on the cheek!",
            "{user} plants a kiss on {target}!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.pink()))

    @app_commands.command(name="poke", description="Poke someone!")
    async def poke(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} pokes {target}!",
            "{user} keeps poking {target}!",
            "{user} sneakily pokes {target}!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.teal()))

    @app_commands.command(name="cuddle", description="Cuddle with someone!")
    async def cuddle(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} cuddles with {target}!",
            "{user} snuggles up to {target}!",
            "{user} and {target} cuddle together!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.pink()))

    @app_commands.command(name="wave", description="Wave at someone!")
    async def wave(self, interaction: discord.Interaction, member: discord.Member = None):
        if member:
            messages = [
                "{user} waves at {target}!",
                "{user} enthusiastically waves at {target}!",
            ]
            await interaction.response.send_message(
                embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.green()))
        else:
            await interaction.response.send_message(
                embed=self.rp_embed("{user} waves at everyone!", interaction.user, color=discord.Color.green()))

    @app_commands.command(name="bite", description="Bite someone!")
    async def bite(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} bites {target}!",
            "{user} takes a chomp out of {target}!",
            "{user} sneaks up and bites {target}!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.orange()))

    @app_commands.command(name="cry", description="Cry!")
    async def cry(self, interaction: discord.Interaction):
        messages = [
            "{user} bursts into tears!",
            "{user} is crying!",
            "{user} starts sobbing!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, color=discord.Color.blue()))

    @app_commands.command(name="dance", description="Dance!")
    async def dance(self, interaction: discord.Interaction):
        messages = [
            "{user} starts dancing!",
            "{user} busts some moves!",
            "{user} is showing off their dance moves!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, color=discord.Color.purple()))

    @app_commands.command(name="highfive", description="High five someone!")
    async def highfive(self, interaction: discord.Interaction, member: discord.Member):
        messages = [
            "{user} high fives {target}!",
            "{user} and {target} share a high five!",
        ]
        await interaction.response.send_message(
            embed=self.rp_embed(random.choice(messages), interaction.user, member, discord.Color.green()))

async def setup(bot):
    await bot.add_cog(Roleplay(bot))
