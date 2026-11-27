import asyncio
import copy
import discord
from discord.ext import commands
from discord import app_commands
from database import DB_PATH
from utils.trade_engine import (
    ensure_trade_table, execute_trade, _empty_offer, TradeError,
    MAX_ITEM_LINES_PER_SIDE,
)
from utils.inventory import has_item, get_inventory
from utils.economy_safe import get_balance
from utils.currency import (
    get_currency_config, for_currency, currency_label, currency_amount,
)
from utils.emoji import CHECK_EMOJI, as_partial_emoji as _as_button_emoji

TRADE_TIMEOUT_SECONDS = 600  # 10 minutes

# guild-scoped active-session guard — one open trade per pair of users
# at a time, mirroring how cogs/reactionroles.py/etc keep transient UI
# state in memory rather than the DB (the DB only ever sees the final
# completed trade, via trade_history).
_active_trades: dict[tuple[int, frozenset], "TradeSession"] = {}


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
        # Serializes offer mutations, public renders, Ready changes, and settlement.
        self.lock = asyncio.Lock()

    def other(self, user_id: int) -> discord.Member:
        return self.user_b if user_id == self.user_a.id else self.user_a

    def key(self) -> tuple[int, frozenset]:
        return (self.guild_id, frozenset({self.user_a.id, self.user_b.id}))

    def reset_ready(self):
        self.ready.clear()

    async def build_embed(self, cur: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🤝 Trade Offer",
            description="Both sides add what they are offering, then click **Ready**. "
                        "Changing your offer clears both Ready states.",
            color=0x57F287 if len(self.ready) == 2 else 0x7c5cbf)
        cc, cd = cur["coins"], cur["diamonds"]
        # Offer lines resolve name + emoji from the same config the buttons
        # and the dashboard use, so a renamed currency reads identically
        # everywhere in this panel.
        for user in (self.user_a, self.user_b):
            offer = self.offers[user.id]
            lines = []
            if offer["coins"]:
                lines.append(f"{cc['emoji']} {offer['coins']:,} {cc['name']}")
            if offer["diamonds"]:
                lines.append(f"{cd['emoji']} {offer['diamonds']:,} {cd['name']}")
            for name, qty in offer["items"].items():
                lines.append(f"🎁 {name} ×{qty}")
            if not lines:
                lines.append("*(nothing offered yet)*")
            ready_mark = f" {CHECK_EMOJI}" if user.id in self.ready else ""
            embed.add_field(
                name=f"{user.display_name}'s offer{ready_mark}",
                value="\n".join(lines), inline=True)
        return embed


class AddCoinsModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g. 500", required=True)

    def __init__(self, view: "TradeView"):
        # Title carries the configured currency name. `discord.ui.Modal`'s
        # title is a normal instance attribute, so it can be set per-open —
        # the decorator form cannot, because it is evaluated once at class
        # definition and would freeze the default name server-wide.
        info = for_currency(view.cur, "coins")
        super().__init__(title=f"Offer {info['name']}")
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True)
            return
        if amt < 0:
            await interaction.response.send_message("Amount cannot be negative.", ephemeral=True)
            return
        if not await self.view_ref._acknowledge(interaction):
            return
        bal = await get_balance(self.view_ref.session.guild_id, interaction.user.id, currency="balance")
        if amt > bal:
            cc = self.view_ref.cur["coins"]
            await self.view_ref._private_error(
                interaction, f"You only have {bal:,} {cc['emoji']} — cannot offer {amt:,}.")
            return
        await self.view_ref.refresh_public_trade(
            interaction, lambda: self._set_offer(interaction.user.id, amt))

    def _set_offer(self, user_id: int, amount: int):
        self.view_ref.session.offers[user_id]["coins"] = amount
        self.view_ref.session.reset_ready()


class AddDiamondsModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Amount", placeholder="e.g. 10", required=True)

    def __init__(self, view: "TradeView"):
        info = for_currency(view.cur, "diamonds")
        super().__init__(title=f"Offer {info['name']}")
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("Enter a whole number.", ephemeral=True)
            return
        if amt < 0:
            await interaction.response.send_message("Amount cannot be negative.", ephemeral=True)
            return
        if not await self.view_ref._acknowledge(interaction):
            return
        gems = await get_balance(self.view_ref.session.guild_id, interaction.user.id, currency="diamonds")
        if amt > gems:
            cd = self.view_ref.cur["diamonds"]
            await self.view_ref._private_error(
                interaction, f"You only have {gems:,} {cd['emoji']} — cannot offer {amt:,}.")
            return
        await self.view_ref.refresh_public_trade(
            interaction, lambda: self._set_offer(interaction.user.id, amt))

    def _set_offer(self, user_id: int, amount: int):
        self.view_ref.session.offers[user_id]["diamonds"] = amount
        self.view_ref.session.reset_ready()


# ─── Item picker (replaces free-text item-name entry) ───────────────────
#
# Previously "Offer Item" opened a modal with a raw text field for the
# item name — a member had to know/type the exact name (case, spacing,
# etc. all had to match inventory_items.item_name exactly, since
# has_item() does a literal lookup). This is the carry-forward gap
# flagged since pass #15 ("Item autocomplete/picker for Trade UI — not
# yet built"). Discord slash-command autocomplete only works on command
# options, not modal text inputs, so a modal can't offer live
# suggestions — the fix is a Select menu instead: fetch the member's
# actual inventory first, list what they hold as pickable options, and
# only ask for a quantity (via a small modal) once they've picked a
# real item they own. This makes offering a nonexistent/misspelled item
# structurally impossible instead of just validated-and-rejected.
#
# Discord caps a Select at 25 options — if a member holds more than 25
# distinct items, this shows their 25 highest-quantity ones. That's a
# real (if unlikely) limitation for very item-heavy inventories; not
# solved here since it would need pagination UI, which is out of scope
# for this pass.

class ItemQuantityModal(discord.ui.Modal):
    quantity = discord.ui.TextInput(label="Quantity", default="1", required=True)

    def __init__(self, view: "TradeView", item_name: str, available_qty: int):
        super().__init__(title=f"Offer: {item_name}"[:45])
        self.view_ref = view
        self.item_name = item_name
        self.quantity.placeholder = f"You have {available_qty}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
        except ValueError:
            await interaction.response.send_message("Quantity must be a whole number.", ephemeral=True)
            return
        if qty <= 0:
            await interaction.response.send_message("Quantity must be positive.", ephemeral=True)
            return
        # Re-checked live (not just against the snapshot the Select was
        # built from) — the same "trust nothing older than this click"
        # rule execute_trade() itself follows at execution time.
        if not await self.view_ref._acknowledge(interaction):
            return
        owns = await has_item(self.view_ref.session.guild_id, interaction.user.id, self.item_name, qty)
        if not owns:
            await self.view_ref._private_error(
                interaction, f"You don't have {qty}x **{self.item_name}** to offer.")
            return
        def mutate():
            offer = self.view_ref.session.offers[interaction.user.id]
            if self.item_name not in offer["items"] and len(offer["items"]) >= MAX_ITEM_LINES_PER_SIDE:
                return f"Max {MAX_ITEM_LINES_PER_SIDE} distinct items per side."
            offer["items"][self.item_name] = qty
            self.view_ref.session.reset_ready()
        await self.view_ref.refresh_public_trade(interaction, mutate)


class ItemPickerSelect(discord.ui.Select):
    def __init__(self, view: "TradeView", items: list[dict]):
        options = [
            discord.SelectOption(
                label=f"{it['item_name']} (×{it['quantity']})"[:100],
                value=it["item_name"][:100],
                description=it.get("item_type") or None,
            )
            for it in items
        ]
        super().__init__(placeholder="Choose an item to offer...", options=options)
        self.view_ref = view
        self._qty_by_name = {it["item_name"]: it["quantity"] for it in items}

    async def callback(self, interaction: discord.Interaction):
        item_name = self.values[0]
        available = self._qty_by_name.get(item_name, 0)
        await interaction.response.send_modal(
            ItemQuantityModal(self.view_ref, item_name, available))


class ItemPickerView(discord.ui.View):
    def __init__(self, trade_view: "TradeView", items: list[dict]):
        super().__init__(timeout=60)
        self.add_item(ItemPickerSelect(trade_view, items))


class AddItemModal(discord.ui.Modal, title="Offer an Item"):
    """
    Manual-entry fallback — kept for the >25-distinct-items case (where
    a Select can't list everything) and for anyone who'd rather just
    type the exact name. The picker above is the default path from the
    "Offer Item" button; this is reached only when there are too many
    items to list.
    """
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
        if not await self.view_ref._acknowledge(interaction):
            return
        owns = await has_item(self.view_ref.session.guild_id, interaction.user.id, name, qty)
        if not owns:
            await self.view_ref._private_error(
                interaction, f"You don't have {qty}x **{name}** to offer.")
            return

        def mutate():
            offer = self.view_ref.session.offers[interaction.user.id]
            if name not in offer["items"] and len(offer["items"]) >= MAX_ITEM_LINES_PER_SIDE:
                return f"Max {MAX_ITEM_LINES_PER_SIDE} distinct items per side."
            offer["items"][name] = qty
            self.view_ref.session.reset_ready()
        await self.view_ref.refresh_public_trade(interaction, mutate)


class TradeView(discord.ui.View):
    def __init__(self, session: TradeSession, cur: dict):
        super().__init__(timeout=TRADE_TIMEOUT_SECONDS)
        self.session = session
        self.cur = cur
        # Apply the guild's configured currency names to the two offer
        # buttons. Discord requires a button's emoji to be a real,
        # bot-usable emoji — an emoji the bot cannot reach renders as the
        # literal token text — so a unicode/plain name falls back to no
        # glyph rather than showing something broken.
        for button, key in ((self.offer_coins, "coins"),
                            (self.offer_diamonds, "diamonds")):
            info = for_currency(cur, key)
            button.label = f"Offer {info['name']}"
            button.emoji = _as_button_emoji(info["emoji"])

    def _active_error(self, interaction: discord.Interaction) -> str | None:
        if interaction.user.id not in (self.session.user_a.id, self.session.user_b.id):
            return "This is not your trade."
        if interaction.guild_id != self.session.guild_id:
            return "This trade is only valid in its original server."
        if self.session.finished or _active_trades.get(self.session.key()) is not self.session:
            return "This trade has already ended."
        return None

    async def _private_error(self, interaction: discord.Interaction, message: str):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _acknowledge(self, interaction: discord.Interaction) -> bool:
        error = self._active_error(interaction)
        if error:
            await self._private_error(interaction, error)
            return False
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        error = self._active_error(interaction)
        if error is None and self.session.message is not None:
            if getattr(interaction.message, "id", None) != self.session.message.id:
                error = "This Trade message is no longer current."
        if error:
            await self._private_error(interaction, error)
            return False
        return True

    async def refresh_public_trade(self, interaction: discord.Interaction, mutation=None):
        """Apply one authorized state change and explicitly edit the shared message.

        Modal interactions may originate from ephemeral picker messages; they
        are acknowledged privately, while this method always edits the stored
        public Trade message. The session lock prevents stale renders from
        replacing newer concurrent offer/Ready state.
        """
        if not await self._acknowledge(interaction):
            return False
        async with self.session.lock:
            error = self._active_error(interaction)
            message = self.session.message
            if error:
                await self._private_error(interaction, error)
                return False
            if message is None:
                await self._private_error(interaction, "The shared Trade message is unavailable.")
                return False

            old_offers = copy.deepcopy(self.session.offers)
            old_ready = set(self.session.ready)
            try:
                if mutation is not None:
                    mutation_error = mutation()
                    if mutation_error:
                        await self._private_error(interaction, mutation_error)
                        return False
                embed = await self.session.build_embed(self.cur)
                await message.edit(embed=embed, view=self)
                return True
            except discord.NotFound:
                self.session.offers = old_offers
                self.session.ready = old_ready
                await self._private_error(interaction, "The shared Trade message no longer exists.")
            except discord.HTTPException as exc:
                self.session.offers = old_offers
                self.session.ready = old_ready
                print(f"[TRADE] Public message refresh failed ({type(exc).__name__}).")
                await self._private_error(interaction, "Could not update the shared Trade message. Please try again.")
            except Exception:
                self.session.offers = old_offers
                self.session.ready = old_ready
                raise
            return False

    async def on_timeout(self):
        async with self.session.lock:
            if self.session.finished:
                return
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

    # Labels/emoji here are PLACEHOLDERS: `discord.ui.button` arguments are
    # evaluated once, at class-definition time, so they cannot hold a
    # per-guild currency name. __init__ overwrites both from the resolved
    # config on every render (same pattern as the Wallet's currency tabs).
    @discord.ui.button(label="Offer", style=discord.ButtonStyle.secondary)
    async def offer_coins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCoinsModal(self))

    @discord.ui.button(label="Offer", style=discord.ButtonStyle.secondary)
    async def offer_diamonds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddDiamondsModal(self))

    @discord.ui.button(label="Offer Item", emoji="🎁", style=discord.ButtonStyle.secondary)
    async def offer_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = await get_inventory(self.session.guild_id, interaction.user.id, include_empty=False)
        if not items:
            await interaction.response.send_message(
                "You don't have any items to offer.", ephemeral=True)
            return
        if len(items) > 25:
            # Select is capped at 25 options — fall back to manual
            # entry rather than silently hiding items past the 25th.
            await interaction.response.send_modal(AddItemModal(self))
            return
        view = ItemPickerView(self, items)
        await interaction.response.send_message(
            "Pick an item to offer:", view=view, ephemeral=True)

    @discord.ui.button(label="Clear My Offer", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def clear_offer(self, interaction: discord.Interaction, button: discord.ui.Button):
        def mutate():
            self.session.offers[interaction.user.id] = _empty_offer()
            self.session.reset_ready()
        await self.refresh_public_trade(interaction, mutate)

    @discord.ui.button(label="Ready", emoji=CHECK_EMOJI,
                       style=discord.ButtonStyle.success)
    async def ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._acknowledge(interaction):
            return
        async with self.session.lock:
            error = self._active_error(interaction)
            if error:
                await self._private_error(interaction, error)
                return
            message = self.session.message
            if message is None:
                await self._private_error(interaction, "The shared Trade message is unavailable.")
                return
            previous_ready = set(self.session.ready)
            self.session.ready.add(interaction.user.id)
            if len(self.session.ready) < 2:
                try:
                    await message.edit(embed=await self.session.build_embed(self.cur), view=self)
                except discord.HTTPException as exc:
                    self.session.ready = previous_ready
                    print(f"[TRADE] Ready-state message refresh failed ({type(exc).__name__}).")
                    await self._private_error(interaction, "Could not update the shared Trade message. Please try again.")
                return

            # The session lock closes the gap between both Ready clicks and
            # settlement; execute_trade still owns the atomic DB transaction.
            a_id, b_id = self.session.user_a.id, self.session.user_b.id
            result = await execute_trade(
                self.session.guild_id, a_id, self.session.offers[a_id],
                b_id, self.session.offers[b_id], reason="Player trade")

            self.session.finished = True
            _active_trades.pop(self.session.key(), None)
            for item in self.children:
                item.disabled = True

            if not result.get("success"):
                embed = discord.Embed(
                    title="❌ Trade failed",
                    description=result.get("error", "Something changed — trade could not complete."),
                    color=0xED4245)
            else:
                embed = await self.session.build_embed(self.cur)
                embed.title = f"{CHECK_EMOJI} Trade complete"
                embed.color = 0x57F287
                embed.set_footer(text=f"Trade #{result['trade_id']}")
            try:
                await message.edit(embed=embed, view=self)
            except discord.HTTPException as exc:
                print(f"[TRADE] Settlement completed but public receipt edit failed ({type(exc).__name__}).")
                await self._private_error(
                    interaction,
                    "Trade processing finished, but the shared message could not be updated. Check trade history.")

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._acknowledge(interaction):
            return
        async with self.session.lock:
            error = self._active_error(interaction)
            if error:
                await self._private_error(interaction, error)
                return
            self.session.finished = True
            _active_trades.pop(self.session.key(), None)
            for item in self.children:
                item.disabled = True
            embed = discord.Embed(
                description=f"Trade cancelled by {interaction.user.mention}.",
                color=0xED4245)
            if self.session.message:
                await self.session.message.edit(embed=embed, view=self)


class Trade(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await ensure_trade_table()

    @app_commands.command(name="trade", description="Start a trade with another member")
    async def trade(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == interaction.user.id:
            await interaction.response.send_message("You cannot trade with yourself.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("You cannot trade with a bot.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        key = (guild_id, frozenset({interaction.user.id, member.id}))
        if key in _active_trades:
            await interaction.response.send_message(
                "There is already an open trade between you two. Finish or let it time out first.",
                ephemeral=True)
            return

        session = TradeSession(guild_id, interaction.user, member)
        _active_trades[key] = session
        cur = await get_currency_config(guild_id)
        view = TradeView(session, cur)
        embed = await session.build_embed(cur)

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

        cur = await get_currency_config(interaction.guild.id)
        embed = discord.Embed(title=f"📜 Trade History — {target.display_name}", color=0x7c5cbf)
        for r in rows:
            a_id, b_id = r["user_a"], r["user_b"]
            offer_a, offer_b = r["offer_a"], r["offer_b"]

            def summarize(offer):
                # Historical rows store only the stable keys ('coins' /
                # 'diamonds') plus amounts — never a currency name — so a
                # past trade re-labels itself with whatever the guild's
                # currency is called NOW. Renaming never invalidates
                # history.
                parts = []
                if offer.get("coins"):
                    parts.append(currency_amount(cur, "coins", offer["coins"]))
                if offer.get("diamonds"):
                    parts.append(currency_amount(cur, "diamonds", offer["diamonds"]))
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
