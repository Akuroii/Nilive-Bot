import discord
from discord.ext import commands
from discord import app_commands
from database import DB_PATH
from utils.trade_engine import (
    ensure_trade_table, execute_trade, _empty_offer, TradeError,
    MAX_ITEM_LINES_PER_SIDE,
)
from utils.inventory import has_item
from utils.economy_safe import get_balance

TRADE_TIMEOUT_SECONDS = 600  # 10 minutes

# guild-scoped active-session guard — one open trade per pair of users
# at a time, mirroring how cogs/reactionroles.py/etc keep transient UI
# state in memory rather than the DB (the DB only ever sees the final
# completed trade, via trade_history).
_active_trades: dict[tuple[int, frozenset], "TradeSession"] = {}


def _currency_name_sync_default() -> str:
    return "Coins"


async def get_currency_name(guild_id: int) -> str:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT currency_name FROM guild_settings WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else "Coins"


class TradeSession:
    def __init__(self, guild_id: int, user_a: discord.Member, user_b: discord.Member):
        self.guild_id = guild_id
        self.user_a = user_a
        self.user_b = user_b
        self.offers: dict[int, dict] = {
            user_a.id: _empty_offer(),
            user_b.id: _empty_offer(),
        }
        self.ready: set[int] = set()
        self.message: discord.Message | None = None
        self.finished = False

    def other(self, user_id: int) -> discord.Member:
        return self.user_b if user_id == self.user_a.id else self.user_a

    def key(self) -> tuple[int, frozenset]:
        return (self.guild_id, frozenset({self.user_a.id, self.user_b.id}))

    def reset_ready(self):
        self.ready.clear()

    async def build_embed(self, currency_name: str) -> discord.Embed:
        embed = discord.Embed(
            title="🤝 Trade Offer",
            description="Both sides add what they're offering, then click **Ready**. "
                        "Changing your offer clears both Ready states.",
            color=0x57F287 if len(self.ready) == 2 else 0x7c5cbf)
        for user in (self.user_a, self.user_b):
            offer = self.offers[user.id]
            lines = []
            if offer["coins"]:
                lines.append(f"🪙 {offer['coins']:,} {currency_name}")
            if offer["diamonds"]:
                lines.append(f"💎 {offer['diamonds']:,} Diamonds")
            for name, qty in offer["items"].items():
                lines.append(f"🎁 {name} ×{qty}")
            if not lines:
                lines.append("*(nothing offered yet)*")
            ready_mark = " ✅" if user.id in self.ready else ""
            embed.add_field(
                name=f"{user.display_name}'s offer{ready_mark}",
                value="\n".join(lines), inline=True)
        return embed


class AddCoinsModal(discord.ui.Modal, title="Offer Coins"):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g. 500", required=True)

    def __init__(self, view: "TradeView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True)
            return
        if amt < 0:
            await interaction.response.send_message("Amount can't be negative.", ephemeral=True)
            return
        bal = await get_balance(self.view_ref.session.guild_id, interaction.user.id, currency="balance")
        if amt > bal:
            await interaction.response.send_message(
                f"You only have {bal:,} — can't offer {amt:,}.", ephemeral=True)
            return
        self.view_ref.session.offers[interaction.user.id]["coins"] = amt
        self.view_ref.session.reset_ready()
        await self.view_ref.refresh(interaction)


class AddDiamondsModal(discord.ui.Modal, title="Offer Diamonds"):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g. 10", required=True)

    def __init__(self, view: "TradeView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True)
            return
        if amt < 0:
            await interaction.response.send_message("Amount can't be negative.", ephemeral=True)
            return
        gems = await get_balance(self.view_ref.session.guild_id, interaction.user.id, currency="diamonds")
        if amt > gems:
            await interaction.response.send_message(
                f"You only have {gems:,} 💎 — can't offer {amt:,}.", ephemeral=True)
            return
        self.view_ref.session.offers[interaction.user.id]["diamonds"] = amt
        self.view_ref.session.reset_ready()
        await self.view_ref.refresh(interaction)


class AddItemModal(discord.ui.Modal, title="Offer an Item"):
    item_name = discord.ui.TextInput(label="Item name (exact)", required=True)
    quantity = discord.ui.TextInput(label="Quantity", default="1", required=True)

    def __init__(self, view: "TradeView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
        except ValueError:
            await interaction.response.send_message("Quantity must be a whole number.", ephemeral=True)
            return
        if qty <= 0:
            await interaction.response.send_message("Quantity must be positive.", ephemeral=True)
            return
        name = str(self.item_name.value).strip()
        owns = await has_item(self.view_ref.session.guild_id, interaction.user.id, name, qty)
        if not owns:
            await interaction.response.send_message(
                f"You don't have {qty}x **{name}** to offer.", ephemeral=True)
            return
        offer = self.view_ref.session.offers[interaction.user.id]
        if name not in offer["items"] and len(offer["items"]) >= MAX_ITEM_LINES_PER_SIDE:
            await interaction.response.send_message(
                f"Max {MAX_ITEM_LINES_PER_SIDE} distinct items per side.", ephemeral=True)
            return
        offer["items"][name] = qty
        self.view_ref.session.reset_ready()
        await self.view_ref.refresh(interaction)


class TradeView(discord.ui.View):
    def __init__(self, session: TradeSession, currency_name: str):
        super().__init__(timeout=TRADE_TIMEOUT_SECONDS)
        self.session = session
        self.currency_name = currency_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.session.user_a.id, self.session.user_b.id):
            await interaction.response.send_message(
                "This isn't your trade.", ephemeral=True)
            return False
        if self.session.finished:
            await interaction.response.send_message(
                "This trade has already ended.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        embed = await self.session.build_embed(self.currency_name)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        self.session.finished = True
        _active_trades.pop(self.session.key(), None)
        for item in self.children:
            item.disabled = True
        if self.session.message:
            try:
                await self.session.message.edit(
                    content="⌛ Trade timed out — no changes were made.", view=self)
            except Exception:
                pass

    @discord.ui.button(label="Offer Coins", emoji="🪙", style=discord.ButtonStyle.secondary)
    async def offer_coins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCoinsModal(self))

    @discord.ui.button(label="Offer Diamonds", emoji="💎", style=discord.ButtonStyle.secondary)
    async def offer_diamonds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddDiamondsModal(self))

    @discord.ui.button(label="Offer Item", emoji="🎁", style=discord.ButtonStyle.secondary)
    async def offer_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddItemModal(self))

    @discord.ui.button(label="Clear My Offer", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def clear_offer(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session.offers[interaction.user.id] = _empty_offer()
        self.session.reset_ready()
        await self.refresh(interaction)

    @discord.ui.button(label="Ready", emoji="✅", style=discord.ButtonStyle.success)
    async def ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session.ready.add(interaction.user.id)

        if len(self.session.ready) < 2:
            await self.refresh(interaction)
            return

        # Both ready — execute atomically. interaction_check already
        # gated who can click; execute_trade re-validates live balances
        # inside its own transaction regardless (offers can go stale
        # between "both readied up" and now).
        a_id, b_id = self.session.user_a.id, self.session.user_b.id
        result = await execute_trade(
            self.session.guild_id, a_id, self.session.offers[a_id],
            b_id, self.session.offers[b_id],
            reason="Player trade")

        self.session.finished = True
        _active_trades.pop(self.session.key(), None)
        for item in self.children:
            item.disabled = True

        if not result.get("success"):
            embed = discord.Embed(
                title="❌ Trade Failed",
                description=result.get("error", "Something changed — trade could not complete."),
                color=0xED4245)
            await interaction.response.edit_message(embed=embed, view=self)
            return

        embed = await self.session.build_embed(self.currency_name)
        embed.title = "✅ Trade Complete!"
        embed.color = 0x57F287
        embed.set_footer(text=f"Trade #{result['trade_id']}")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session.finished = True
        _active_trades.pop(self.session.key(), None)
        for item in self.children:
            item.disabled = True
        embed = discord.Embed(
            description=f"Trade cancelled by {interaction.user.mention}.",
            color=0xED4245)
        await interaction.response.edit_message(embed=embed, view=self)


class Trade(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await ensure_trade_table()

    @app_commands.command(name="trade", description="Start a trade with another member")
    async def trade(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == interaction.user.id:
            await interaction.response.send_message("You can't trade with yourself.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("You can't trade with a bot.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        key = (guild_id, frozenset({interaction.user.id, member.id}))
        if key in _active_trades:
            await interaction.response.send_message(
                "There's already an open trade between you two. Finish or let it time out first.",
                ephemeral=True)
            return

        session = TradeSession(guild_id, interaction.user, member)
        _active_trades[key] = session
        currency_name = await get_currency_name(guild_id)
        view = TradeView(session, currency_name)
        embed = await session.build_embed(currency_name)

        await interaction.response.send_message(
            content=f"{interaction.user.mention} 🤝 {member.mention}",
            embed=embed, view=view)
        session.message = await interaction.original_response()

    @app_commands.command(name="trade_history", description="View your recent trades")
    async def trade_history(self, interaction: discord.Interaction, member: discord.Member = None):
        from utils.trade_engine import get_trade_history
        target = member or interaction.user
        rows = await get_trade_history(interaction.guild.id, target.id, limit=10)
        if not rows:
            await interaction.response.send_message(
                f"No trade history for {target.mention}.", ephemeral=True)
            return

        embed = discord.Embed(title=f"📜 Trade History — {target.display_name}", color=0x7c5cbf)
        for r in rows:
            a_id, b_id = r["user_a"], r["user_b"]
            offer_a, offer_b = r["offer_a"], r["offer_b"]

            def summarize(offer):
                parts = []
                if offer.get("coins"):
                    parts.append(f"🪙{offer['coins']:,}")
                if offer.get("diamonds"):
                    parts.append(f"💎{offer['diamonds']:,}")
                for name, qty in offer.get("items", {}).items():
                    parts.append(f"{name}×{qty}")
                return ", ".join(parts) or "nothing"

            embed.add_field(
                name=f"#{r['id']} — {str(r['created_at'])[:16]}",
                value=f"<@{a_id}> gave {summarize(offer_a)}\n<@{b_id}> gave {summarize(offer_b)}",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Trade(bot))
