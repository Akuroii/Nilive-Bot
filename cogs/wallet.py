import discord
from discord.ext import commands
from discord import app_commands

from utils.daily_engine import (
    DailyAlreadyClaimed, perform_streak_claim, get_streak_preview,
    get_streak_state, format_remaining, STREAK_BONUS_CAP_DAYS,
)
from utils.economy_safe import get_balance
from utils.inventory import get_inventory
from utils.equip_engine import get_equipped, equip_role
from utils.title_engine import (
    TITLE_ITEM_TYPE, get_equipped_title, equip_title, unequip_title,
    cleanup_unowned_title,
)
from utils.potion_engine import (
    POTION_ITEM_TYPE, use_potion, describe as describe_potion,
    is_usable as potion_is_usable, get_active_effects,
)
from utils.item_catalog import get_catalog_entry, item_sort_key
from utils.ledger import get_user_ledger_page, count_user_ledger

# ═══════════════════════════════════════════════════════════════════════
# WALLET — private, user-only economy hub
#
# Design notes (ours, not copied from any reference bot):
#
# * ONE ephemeral message, edited in place. Every panel — hub, streak,
#   inventory tabs, item detail, receipts — is a re-render of the same
#   message rather than a new reply. Discord's ephemeral messages can't
#   be deleted by the user, so spawning a fresh one per click leaves a
#   trail of stale, still-clickable panels in the channel; editing keeps
#   exactly one live surface and makes "Back" mean something.
#
# * Ownership is enforced twice: the command is ephemeral (only the
#   invoker can see it) AND every component re-checks interaction.user.id
#   against the owner recorded on the view. The second check is not
#   redundant — ephemeral only controls visibility, and a view left open
#   in a shared context should never act on someone else's wallet.
#
# * Views are ephemeral-lifetime with a real timeout, NOT persistent.
#   The project's persistent components (shop_buy_*) are stateless
#   custom_id lookups; a wallet panel carries per-user navigation state,
#   which is exactly the thing that must NOT survive a bot restart.
#   on_timeout disables the controls so a stale panel fails visibly
#   rather than silently.
#
# * The hub shows counts, not lists. Members open a wallet to answer
#   "how much do I have / can I claim yet", and only sometimes "what
#   exactly is in my bag" — so the bag is one click away instead of
#   flooding the first screen. This is the main way this differs from
#   the dump-everything-in-one-list approach.
#
# * The item detail view derives its actions from item TYPE and STATE,
#   so a potion never offers Equip and an equipped title offers only
#   Unequip. No action a member sees is one the backend would reject.
# ═══════════════════════════════════════════════════════════════════════

WALLET_COLOR = 0x7c5cbf
STREAK_COLOR = 0xF0883E
RECEIPTS_PER_PAGE = 8
VIEW_TIMEOUT = 180

# Inventory tabs. Roles/temp_roles are grouped under "Items" because
# from the member's side they're all "things I own and can wear"; the
# split that matters to them is equippable vs consumable vs cosmetic
# label, not which table the role_id happens to live in.
TAB_ITEMS = "items"
TAB_POTIONS = "potions"
TAB_TITLES = "titles"

EQUIPPABLE_TYPES = ("role", "temp_role")

TAB_META = {
    TAB_ITEMS:   {"label": "Items",   "emoji": "🎒"},
    TAB_POTIONS: {"label": "Potions", "emoji": "🧪"},
    TAB_TITLES:  {"label": "Titles",  "emoji": "🏷️"},
}

# Ledger `source` values in use across the project, mapped to what a
# member should see. Anything unmapped falls back to a title-cased
# version of the raw source rather than being hidden — an unlabelled
# transaction is still money that moved, and silently dropping it would
# make the receipts lie about the balance.
SOURCE_LABELS = {
    "daily": "Streak reward",
    "shop": "Shop",
    "give": "Transfer",
    "convert": "Exchange",
    "admin": "Staff adjustment",
    "leveling": "Level reward",
    "minigame": "Minigame",
    "mission": "Mission",
    "tag_mission": "Tag mission",
    "tag_partner": "Partner reward",
    "event": "Event",
    "trade": "Trade",
    "ledger": "Correction",
    "system": "System",
    "potion": "Potion",
}


def _currency_emoji(currency: str) -> str:
    return "💎" if currency == "diamonds" else "🪙"


async def get_currency_name(guild_id: int) -> str:
    import aiosqlite
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT currency_name FROM guild_settings WHERE guild_id = ?",
            (guild_id,))
        row = await cursor.fetchone()
    return row[0] if row and row[0] else "Coins"


def _item_emoji(catalog: dict, fallback: str) -> str:
    """
    The Shop/item creation system stores an item's art as an icon_url,
    which Discord can't render inline in an embed line. Rather than
    inventing unrelated icons per item, a custom emoji is used when the
    admin configured one (icon_url holding a <:name:id> / emoji mention
    is supported), otherwise the category's own emoji is used so the
    list still reads as a grid of items. The icon_url itself is shown
    as the thumbnail on the item's detail view, which is the one place
    an image URL actually renders.
    """
    raw = (catalog.get("icon_url") or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw
    return fallback


def _rarity_label(rarity: str) -> str:
    return (rarity or "common").title()


async def _decorate(guild_id: int, items: list[dict]) -> list[dict]:
    """Attach catalog metadata + sort rarity-first, matching the rank card."""
    out = []
    for it in items:
        catalog = await get_catalog_entry(guild_id, it["item_name"])
        out.append({
            **it,
            "icon_url": catalog["icon_url"],
            "rarity": catalog["rarity"],
            "_sort": item_sort_key(
                catalog["rarity"], catalog["value_currency"],
                catalog["value_amount"]),
        })
    out.sort(key=lambda x: (x["_sort"], x["item_name"]), reverse=True)
    return out


async def load_wallet_snapshot(guild_id: int, user_id: int) -> dict:
    """
    Everything the hub shows, in one place. Kept as a plain function
    (not a view method) so every panel can refresh from the same source
    after an action mutates state.
    """
    await cleanup_unowned_title(guild_id, user_id)

    items = await get_inventory(guild_id, user_id, include_empty=False)
    total_items = sum(int(it["quantity"] or 0) for it in items)

    state = await get_streak_state(guild_id, user_id)

    return {
        "coins": await get_balance(guild_id, user_id, currency="balance"),
        "diamonds": await get_balance(guild_id, user_id, currency="diamonds"),
        "items": items,
        "total_items": total_items,
        "streak": state["streak"],
        "claimed_today": state["claimed_today"],
        "seconds_remaining": state["seconds_remaining"],
        "currency_name": await get_currency_name(guild_id),
    }


def split_by_tab(items: list[dict]) -> dict:
    return {
        TAB_ITEMS: [it for it in items
                    if it["item_type"] not in (POTION_ITEM_TYPE,
                                               TITLE_ITEM_TYPE)],
        TAB_POTIONS: [it for it in items
                      if it["item_type"] == POTION_ITEM_TYPE],
        TAB_TITLES: [it for it in items
                     if it["item_type"] == TITLE_ITEM_TYPE],
    }


# ─── Base view ──────────────────────────────────────────────────────────

class WalletBaseView(discord.ui.View):
    """
    Shared ownership guard + timeout behaviour for every wallet panel.

    interaction_check runs before any component callback, so the
    per-callback code below never has to repeat the owner check — one
    guard, impossible to forget on a new button.
    """

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.guild_id = guild_id
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This isn't your wallet — run `/wallet` to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                # Ephemeral messages expire on their own; a failed edit
                # here is cosmetic and must never raise into the loop.
                pass


# ─── Hub ────────────────────────────────────────────────────────────────

def build_hub_embed(user: discord.abc.User, snap: dict) -> discord.Embed:
    embed = discord.Embed(
        title="✦ Wallet",
        color=WALLET_COLOR)
    embed.set_author(name=user.display_name,
                     icon_url=user.display_avatar.url)

    embed.add_field(
        name=f"🪙 {snap['currency_name']}",
        value=f"**{snap['coins']:,}**", inline=True)
    embed.add_field(
        name="💎 Diamonds",
        value=f"**{snap['diamonds']:,}**", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(
        name="🎒 Items",
        value=f"**{snap['total_items']:,}**", inline=True)

    if snap["claimed_today"]:
        streak_value = (f"**Day {snap['streak']}** · next in "
                        f"{format_remaining(snap['seconds_remaining'])}")
    elif snap["streak"]:
        streak_value = f"**Day {snap['streak']}** · ready to claim"
    else:
        streak_value = "**—** · ready to start"
    embed.add_field(name="🔥 Streak", value=streak_value, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.set_footer(text="Only you can see this")
    return embed


class WalletHubView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, claimed_today: bool):
        super().__init__(guild_id, user_id)
        # The streak button reflects live state before it's ever
        # clicked: nothing to claim reads as a muted secondary button,
        # a ready claim is a green call to action. The member can still
        # open it either way to see their timer and bonus.
        self.streak_button.style = (
            discord.ButtonStyle.secondary if claimed_today
            else discord.ButtonStyle.success)

    @discord.ui.button(label="Streak", emoji="🔥", row=0)
    async def streak_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        await open_streak_panel(interaction, self.guild_id, self.user_id)

    @discord.ui.button(label="Inventory", emoji="🎒",
                       style=discord.ButtonStyle.primary, row=0)
    async def inventory_button(self, interaction: discord.Interaction,
                               button: discord.ui.Button):
        await open_inventory_panel(
            interaction, self.guild_id, self.user_id, TAB_ITEMS)

    @discord.ui.button(label="Receipts", emoji="🧾",
                       style=discord.ButtonStyle.secondary, row=1)
    async def receipts_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        await open_receipts_panel(
            interaction, self.guild_id, self.user_id, "balance", 0)

    @discord.ui.button(label="Vote", emoji="🗳️",
                       style=discord.ButtonStyle.secondary, row=1,
                       disabled=True)
    async def vote_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        # Intentionally inert. The Vote system is a planned feature and
        # is NOT implemented in this pass; the slot is reserved and
        # disabled so the hub's final layout is visible without
        # shipping a half-feature or a button that lies.
        await interaction.response.defer()


async def render_hub(interaction: discord.Interaction, guild_id: int,
                     user_id: int, *, first: bool = False):
    snap = await load_wallet_snapshot(guild_id, user_id)
    embed = build_hub_embed(interaction.user, snap)
    view = WalletHubView(guild_id, user_id, snap["claimed_today"])

    if first:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
    else:
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message


# ─── Streak ─────────────────────────────────────────────────────────────

def build_streak_embed(preview: dict, currency_name: str) -> discord.Embed:
    embed = discord.Embed(title="🔥 Streak", color=STREAK_COLOR)

    embed.add_field(
        name="Daily Reward",
        value=(f"{preview['daily_min']:,}–{preview['daily_max']:,} "
               f"{currency_name}"),
        inline=True)

    if preview["claimed_today"]:
        embed.add_field(
            name="Streak", value=f"Day **{preview['streak']}**", inline=True)
        embed.add_field(
            name="Next claim",
            value=f"**{format_remaining(preview['seconds_remaining'])}**",
            inline=True)
        embed.description = "Already claimed today — resets at **00:00 UTC**."
    else:
        embed.add_field(
            name="Streak",
            value=f"Day **{preview['next_streak']}** next", inline=True)
        embed.add_field(
            name="Bonus",
            value=(f"+**{preview['next_bonus']:,}**"
                   + (" *(capped)*" if preview["bonus_capped"] else "")),
            inline=True)
        embed.description = "Ready to claim."

    embed.set_footer(
        text=f"Bonus grows daily up to day {STREAK_BONUS_CAP_DAYS}")
    return embed


def build_claim_embed(result: dict, currency_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="🔥 Streak claimed",
        description=(f"You received **{result['amount']:,}** "
                     f"{currency_name}."),
        color=STREAK_COLOR)
    embed.add_field(
        name="Streak",
        value=(f"Day **{result['streak']}**"
               + (" *(bonus capped)*" if result["bonus_capped"] else "")),
        inline=True)
    if result["streak_bonus"]:
        embed.add_field(
            name="Bonus", value=f"+**{result['streak_bonus']:,}**", inline=True)
    if result["multiplier"] and result["multiplier"] != 1.0:
        embed.add_field(
            name="Prestige", value=f"×{result['multiplier']:g}", inline=True)
    embed.add_field(
        name="Balance",
        value=f"**{result['new_balance']:,}** {currency_name}", inline=False)
    embed.set_footer(
        text=f"Next claim in {format_remaining(result['next_reset_seconds'])}")
    return embed


class StreakPanelView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, can_claim: bool):
        super().__init__(guild_id, user_id)
        self.claim_button.disabled = not can_claim

    @discord.ui.button(label="Claim", emoji="🔥",
                       style=discord.ButtonStyle.success)
    async def claim_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        currency_name = await get_currency_name(self.guild_id)
        try:
            result = await perform_streak_claim(
                interaction.client, self.guild_id, self.user_id,
                member=interaction.user)
        except DailyAlreadyClaimed as e:
            # Lost a race against another entry point (/streak, /daily,
            # or a second wallet panel). Say so precisely and retire
            # the button rather than leaving it clickable.
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🔥 Streak",
                    description=(
                        f"Already claimed today — next claim in "
                        f"**{format_remaining(e.seconds_remaining)}** "
                        f"(resets 00:00 UTC)."),
                    color=STREAK_COLOR),
                view=self)
            return

        # Successful claim: the interaction that performed it must not
        # remain usable. Per the spec there are no extra buttons in the
        # result — the panel becomes a plain receipt.
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=build_claim_embed(result, currency_name), view=self)

    @discord.ui.button(label="Back", emoji="◀",
                       style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await render_hub(interaction, self.guild_id, self.user_id)


async def open_streak_panel(interaction: discord.Interaction,
                            guild_id: int, user_id: int):
    preview = await get_streak_preview(guild_id, user_id)
    currency_name = await get_currency_name(guild_id)
    view = StreakPanelView(
        guild_id, user_id, can_claim=not preview["claimed_today"])
    await interaction.response.edit_message(
        embed=build_streak_embed(preview, currency_name), view=view)
    view.message = interaction.message


# ─── Inventory ──────────────────────────────────────────────────────────

def build_inventory_embed(tab: str, entries: list[dict],
                          equipped_role: str | None,
                          equipped_title_name: str | None,
                          active_effects: list[dict]) -> discord.Embed:
    meta = TAB_META[tab]
    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['label']}", color=WALLET_COLOR)

    if not entries:
        empty = {
            TAB_ITEMS: "No items yet — buy one from `/shop`.",
            TAB_POTIONS: "No potions yet — buy one from `/shop`.",
            TAB_TITLES: "No titles yet — buy one from `/shop`.",
        }[tab]
        embed.description = empty
        return embed

    lines = []
    for it in entries:
        emoji = _item_emoji(it, meta["emoji"])
        name = it["item_name"]
        qty = int(it["quantity"] or 0)
        # Quantity is always shown per stack, never one line per copy.
        line = f"{emoji} **{name}** ×{qty}"

        if tab == TAB_ITEMS and name == equipped_role:
            line += " · *equipped*"
        elif tab == TAB_TITLES and name == equipped_title_name:
            line += " · *equipped*"
        elif tab == TAB_POTIONS and not potion_is_usable(it.get("metadata")):
            line += " · ⚠️ *not configured*"

        line += f"  `{_rarity_label(it['rarity'])}`"
        lines.append(line)

    embed.description = "\n".join(lines)

    if tab == TAB_POTIONS and active_effects:
        embed.add_field(
            name="Active effects",
            value="\n".join(
                f"{e['multiplier']:g}× XP · ends "
                f"{(e['expires_at'] or '')[:16].replace('T', ' ')} UTC"
                for e in active_effects[:5]),
            inline=False)

    embed.set_footer(text="Select an item below to manage it")
    return embed


class InventoryItemSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 entries: list[dict], equipped_role: str | None,
                 equipped_title_name: str | None):
        options = []
        for it in entries[:25]:
            name = it["item_name"]
            equipped = (
                (tab == TAB_ITEMS and name == equipped_role)
                or (tab == TAB_TITLES and name == equipped_title_name))
            options.append(discord.SelectOption(
                label=name[:100],
                description=(
                    f"×{it['quantity']} · {_rarity_label(it['rarity'])}"
                    + (" · equipped" if equipped else ""))[:100],
                value=name[:100]))
        super().__init__(
            placeholder=f"Manage a {TAB_META[tab]['label'].lower().rstrip('s')}…",
            options=options or [discord.SelectOption(label="—", value="—")],
            disabled=not options)
        self.guild_id = guild_id
        self.user_id = user_id
        self.tab = tab

    async def callback(self, interaction: discord.Interaction):
        await open_item_panel(
            interaction, self.guild_id, self.user_id,
            self.tab, self.values[0])


class InventoryView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 entries: list[dict], equipped_role: str | None,
                 equipped_title_name: str | None):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.add_item(InventoryItemSelect(
            guild_id, user_id, tab, entries,
            equipped_role, equipped_title_name))
        # The active tab is rendered as a disabled primary button — it
        # reads as "you are here" without needing a separate label, and
        # can't be clicked to re-render the panel it's already showing.
        for key in (TAB_ITEMS, TAB_POTIONS, TAB_TITLES):
            self.add_item(InventoryTabButton(key, active=(key == tab)))
        self.add_item(InventoryBackButton())


class InventoryTabButton(discord.ui.Button):
    def __init__(self, tab: str, active: bool):
        meta = TAB_META[tab]
        super().__init__(
            label=meta["label"], emoji=meta["emoji"], row=1,
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            disabled=active)
        self.tab = tab

    async def callback(self, interaction: discord.Interaction):
        view: InventoryView = self.view
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id, self.tab)


class InventoryBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="◀", row=2,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: InventoryView = self.view
        await render_hub(interaction, view.guild_id, view.user_id)


async def open_inventory_panel(interaction: discord.Interaction,
                               guild_id: int, user_id: int, tab: str):
    await cleanup_unowned_title(guild_id, user_id)
    items = await get_inventory(guild_id, user_id, include_empty=False)
    entries = await _decorate(guild_id, split_by_tab(items)[tab])

    equipped = await get_equipped(guild_id, user_id)
    equipped_role = equipped["item_name"] if equipped else None
    equipped_title = await get_equipped_title(guild_id, user_id)
    equipped_title_name = equipped_title["item_name"] if equipped_title else None
    active_effects = (await get_active_effects(guild_id, user_id)
                      if tab == TAB_POTIONS else [])

    view = InventoryView(guild_id, user_id, tab, entries,
                         equipped_role, equipped_title_name)
    await interaction.response.edit_message(
        embed=build_inventory_embed(
            tab, entries, equipped_role, equipped_title_name, active_effects),
        view=view)
    view.message = interaction.message


# ─── Item detail / management ───────────────────────────────────────────

def build_item_embed(guild: discord.Guild, tab: str, item: dict,
                     equipped: bool) -> discord.Embed:
    emoji = _item_emoji(item, TAB_META[tab]["emoji"])
    embed = discord.Embed(
        title=f"{emoji} {item['item_name']}", color=WALLET_COLOR)

    embed.add_field(name="Quantity",
                    value=f"×{int(item['quantity'] or 0)}", inline=True)
    embed.add_field(name="Rarity",
                    value=_rarity_label(item["rarity"]), inline=True)

    if tab == TAB_POTIONS:
        embed.add_field(name="Type", value="Consumable", inline=True)
        embed.add_field(name="Effect",
                        value=describe_potion(item.get("metadata")),
                        inline=False)
    else:
        embed.add_field(
            name="Status",
            value=("**Equipped**" if equipped else "Not equipped"),
            inline=True)

    # A role item's Discord role is shown as a mention so the member can
    # see exactly what wearing it grants — but the DB row above is the
    # source of truth for ownership/equipped state, not the role.
    if tab == TAB_ITEMS and item["item_type"] in EQUIPPABLE_TYPES:
        role_id = (item.get("metadata") or {}).get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        embed.add_field(
            name="Role",
            value=(role.mention if role else "*missing — ask an admin*"),
            inline=True)
        if item["item_type"] == "temp_role":
            expires = (item.get("metadata") or {}).get("expires_at")
            if expires:
                embed.add_field(
                    name="Expires",
                    value=f"{str(expires)[:16].replace('T', ' ')} UTC",
                    inline=True)

    icon_url = (item.get("icon_url") or "").strip()
    if icon_url.startswith("http"):
        embed.set_thumbnail(url=icon_url)

    return embed


class ItemPanelView(WalletBaseView):
    """
    Actions are built from the item's type and current state, so the
    member is never offered an action the backend would reject:
      * consumable  -> Use (disabled if its effect isn't configured)
      * equippable  -> Equip / Unequip, whichever applies
      * plain item  -> no action, just details
    """

    def __init__(self, guild_id: int, user_id: int, tab: str,
                 item: dict, equipped: bool):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.item_name = item["item_name"]
        self.item_type = item["item_type"]

        if tab == TAB_POTIONS:
            self.add_item(UseButton(
                enabled=potion_is_usable(item.get("metadata"))))
        elif tab == TAB_TITLES:
            self.add_item(EquipToggleButton(equipped))
        elif self.item_type in EQUIPPABLE_TYPES:
            # An equipped role has no Unequip: the project's role slot
            # is a swap-only slot (equip_engine swaps the worn role, it
            # has no "wear nothing" path), so offering Unequip here
            # would promise behaviour the engine doesn't implement.
            # Equipping a different role from the Items tab replaces it.
            self.add_item(EquipToggleButton(equipped, swap_only=True))

        self.add_item(ItemBackButton())


class UseButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        super().__init__(label="Use", emoji="🧪",
                         style=discord.ButtonStyle.success,
                         disabled=not enabled)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        result = await use_potion(
            view.guild_id, view.user_id, view.item_name)
        if not result.get("success"):
            await interaction.response.send_message(
                f"❌ {result.get('error', 'Something went wrong.')}",
                ephemeral=True)
            return
        # Consuming the last copy makes the item panel meaningless, so
        # return to the tab; otherwise re-render the panel with the
        # decremented quantity.
        if result["remaining"] <= 0:
            await open_inventory_panel(
                interaction, view.guild_id, view.user_id, TAB_POTIONS)
        else:
            await open_item_panel(
                interaction, view.guild_id, view.user_id,
                TAB_POTIONS, view.item_name)


class EquipToggleButton(discord.ui.Button):
    def __init__(self, equipped: bool, swap_only: bool = False):
        self.equipped = equipped
        self.swap_only = swap_only
        if equipped:
            super().__init__(
                label=("Equipped" if swap_only else "Unequip"),
                emoji="✅" if swap_only else "✖",
                style=discord.ButtonStyle.secondary,
                disabled=swap_only)
        else:
            super().__init__(label="Equip", emoji="✨",
                             style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        if view.tab == TAB_TITLES:
            if self.equipped:
                result = await unequip_title(
                    view.guild_id, view.user_id, view.item_name)
            else:
                result = await equip_title(
                    view.guild_id, view.user_id, view.item_name)
        else:
            result = await equip_role(
                interaction.client, view.guild_id, view.user_id,
                view.item_name)

        if not result.get("success"):
            await interaction.response.send_message(
                f"❌ {result.get('error', 'Something went wrong.')}",
                ephemeral=True)
            return

        await open_item_panel(
            interaction, view.guild_id, view.user_id,
            view.tab, view.item_name)


class ItemBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="◀",
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id, view.tab)


async def open_item_panel(interaction: discord.Interaction, guild_id: int,
                          user_id: int, tab: str, item_name: str):
    items = await get_inventory(guild_id, user_id, include_empty=False)
    entries = await _decorate(guild_id, split_by_tab(items)[tab])
    item = next((it for it in entries if it["item_name"] == item_name), None)

    if not item:
        # The member no longer owns it (used the last one, traded it
        # away, admin removed it) — fall back to the tab instead of
        # rendering a detail view for something that isn't there.
        await open_inventory_panel(interaction, guild_id, user_id, tab)
        return

    if tab == TAB_TITLES:
        current = await get_equipped_title(guild_id, user_id)
        equipped = bool(current and current["item_name"] == item_name)
    elif item["item_type"] in EQUIPPABLE_TYPES:
        current = await get_equipped(guild_id, user_id)
        equipped = bool(current and current["item_name"] == item_name)
    else:
        equipped = False

    view = ItemPanelView(guild_id, user_id, tab, item, equipped)
    await interaction.response.edit_message(
        embed=build_item_embed(interaction.guild, tab, item, equipped),
        view=view)
    view.message = interaction.message


# ─── Receipts ───────────────────────────────────────────────────────────

def _format_entry(entry: dict, currency_name: str) -> str:
    amount = int(entry["amount"] or 0)
    sign = "+" if amount > 0 else "−"
    emoji = _currency_emoji(entry["currency"])
    label = SOURCE_LABELS.get(
        entry["source"], (entry["source"] or "system").replace("_", " ").title())

    when = (entry["created_at"] or "")[:16].replace("T", " ")
    reason = (entry["reason"] or "").strip()

    line = f"`{sign}{abs(amount):,}` {emoji} **{label}**"
    if entry["reversed"]:
        line += " · ~~reversed~~"
    line += f"\n*{when} UTC*"
    if reason:
        line += f" · {reason[:80]}"
    return line


def build_receipts_embed(currency: str, currency_name: str,
                         entries: list[dict], page: int,
                         total: int) -> discord.Embed:
    pages = max(1, -(-total // RECEIPTS_PER_PAGE))
    embed = discord.Embed(
        title=f"🧾 Receipts · {_currency_emoji(currency)} "
              f"{currency_name if currency == 'balance' else 'Diamonds'}",
        color=WALLET_COLOR)

    if not entries:
        embed.description = "No transactions recorded yet."
        embed.set_footer(text="Only you can see this")
        return embed

    embed.description = "\n\n".join(
        _format_entry(e, currency_name) for e in entries)
    embed.set_footer(
        text=f"Page {page + 1}/{pages} · {total:,} transactions")
    return embed


class ReceiptsView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, currency: str,
                 page: int, total: int):
        super().__init__(guild_id, user_id)
        self.currency = currency
        self.page = page
        self.total = total
        pages = max(1, -(-total // RECEIPTS_PER_PAGE))

        self.back_page.disabled = page <= 0
        self.forward_page.disabled = page >= pages - 1

        self.add_item(CurrencyTabButton(
            "balance", active=(currency == "balance")))
        self.add_item(CurrencyTabButton(
            "diamonds", active=(currency == "diamonds")))
        self.add_item(ReceiptsBackButton())

    @discord.ui.button(label="Back", emoji="◀",
                       style=discord.ButtonStyle.secondary, row=0)
    async def back_page(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        await open_receipts_panel(
            interaction, self.guild_id, self.user_id,
            self.currency, max(0, self.page - 1))

    @discord.ui.button(label="Forward", emoji="▶",
                       style=discord.ButtonStyle.secondary, row=0)
    async def forward_page(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        await open_receipts_panel(
            interaction, self.guild_id, self.user_id,
            self.currency, self.page + 1)


class CurrencyTabButton(discord.ui.Button):
    def __init__(self, currency: str, active: bool):
        super().__init__(
            label=("Coins" if currency == "balance" else "Diamonds"),
            emoji=_currency_emoji(currency), row=1,
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            disabled=active)
        self.currency = currency

    async def callback(self, interaction: discord.Interaction):
        view: ReceiptsView = self.view
        # Switching currency always restarts at page 0 — carrying a page
        # index across two histories of different lengths lands the
        # member on an empty page.
        await open_receipts_panel(
            interaction, view.guild_id, view.user_id, self.currency, 0)


class ReceiptsBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Wallet", emoji="✦", row=2,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: ReceiptsView = self.view
        await render_hub(interaction, view.guild_id, view.user_id)


async def open_receipts_panel(interaction: discord.Interaction,
                              guild_id: int, user_id: int,
                              currency: str, page: int):
    total = await count_user_ledger(guild_id, user_id, currency=currency)
    pages = max(1, -(-total // RECEIPTS_PER_PAGE))
    page = max(0, min(page, pages - 1))

    entries = await get_user_ledger_page(
        guild_id, user_id, currency=currency,
        offset=page * RECEIPTS_PER_PAGE, limit=RECEIPTS_PER_PAGE)
    currency_name = await get_currency_name(guild_id)

    view = ReceiptsView(guild_id, user_id, currency, page, total)
    await interaction.response.edit_message(
        embed=build_receipts_embed(
            currency, currency_name, entries, page, total),
        view=view)
    view.message = interaction.message


# ─── Cog ────────────────────────────────────────────────────────────────

class Wallet(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="wallet",
        description="Open your private wallet — balances, streak, "
                    "inventory and receipts")
    async def wallet(self, interaction: discord.Interaction):
        await render_hub(
            interaction, interaction.guild.id, interaction.user.id,
            first=True)

    @app_commands.command(
        name="streak",
        description="Claim your daily streak reward")
    async def streak(self, interaction: discord.Interaction):
        await run_streak_command(interaction)


async def run_streak_command(interaction: discord.Interaction):
    """
    The /streak command body, shared with the /daily compatibility
    alias in cogs/economy.py. Both call the same engine
    (utils.daily_engine.perform_streak_claim) as the Wallet button, so
    all three entry points share one claim guard and one reward
    implementation — claiming with one immediately blocks the others.
    """
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    currency_name = await get_currency_name(guild_id)

    try:
        result = await perform_streak_claim(
            interaction.client, guild_id, user_id, member=interaction.user)
    except DailyAlreadyClaimed as e:
        preview = await get_streak_preview(guild_id, user_id)
        embed = discord.Embed(
            title="🔥 Streak",
            description=(
                f"Already claimed today — next claim in "
                f"**{format_remaining(e.seconds_remaining)}** "
                f"(resets 00:00 UTC)."),
            color=STREAK_COLOR)
        embed.add_field(
            name="Streak", value=f"Day **{preview['streak']}**", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.send_message(
        embed=build_claim_embed(result, currency_name), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Wallet(bot))
