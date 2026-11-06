import discord
from discord.ext import commands
from discord import app_commands

from utils.daily_engine import (
    DailyAlreadyClaimed, perform_streak_claim, get_streak_preview,
    get_streak_state, format_remaining,
)
from utils.economy_safe import get_balance
from utils.currency import get_currency_config, for_currency
from utils.emoji import as_partial_emoji
from utils.inventory import get_inventory, get_inventory_entries, find_entry, drop_item
from utils.equip_engine import get_equipped, equip_role, unequip_role
from utils.title_engine import (
    TITLE_ITEM_TYPE, get_equipped_title, equip_title, unequip_title,
    cleanup_unowned_title,
)
from utils.potion_engine import (
    POTION_ITEM_TYPE, use_potion, describe as describe_potion,
    is_usable as potion_is_usable,
)
from utils.item_catalog import get_catalog_entry, item_sort_key
from utils.ledger import get_user_ledger_page, count_user_ledger
from utils.formatters import format_relative

# ═══════════════════════════════════════════════════════════════════════
# WALLET — private, user-only economy hub
#
# Design notes (ours, not copied from any reference bot):
#
# * ONE ephemeral message, edited in place. Every panel — hub, streak,
#   inventory tabs, item detail, drop confirm, receipts — is a
#   re-render of the same message rather than a new reply. Discord's
#   ephemeral messages cannot be deleted by the user, so spawning a
#   fresh reply per click leaves a trail of stale, still-clickable
#   panels in the channel; editing keeps exactly one live surface and
#   makes "Back" mean something.
#
# * Ownership is enforced twice: the command is ephemeral (only the
#   invoker can see it) AND every component re-checks
#   interaction.user.id against the owner recorded on the view. The
#   second check is not redundant — ephemeral only controls
#   visibility, and a view left open in a shared context must never
#   act on someone else's wallet.
#
# * Views are ephemeral-lifetime with a long timeout, NOT persistent.
#   The project's persistent components (shop_buy_*) are stateless
#   custom_id lookups; a wallet panel carries per-user navigation
#   state, which is exactly what must NOT survive a bot restart.
#   on_timeout disables controls so a stale panel fails visibly
#   rather than silently.
#
# * The hub shows counts, not lists — inspired by wallet-card UIs in
#   bots like Tatsu but with our own typography and branding (the
#   title is the bot name, the member's identity is in the author
#   line). Currency names and icons come from guild settings, never
#   hardcoded, so admins can fully rename/re-icon both coins and
#   diamonds.
#
# * Item actions derive from type and state, so a potion never offers
#   Equip, an equipped title offers Unequip, and Drop always asks for
#   confirmation before destroying the stack.
# ═══════════════════════════════════════════════════════════════════════

WALLET_COLOR = 0x7c5cbf
STREAK_COLOR = 0xF0883E
RECEIPTS_PER_PAGE = 8
VIEW_TIMEOUT = 1800  # 30 minutes — long enough to browse comfortably
INVENTORY_COLOR = 15000312
INVENTORY_AUTHOR_ICON = "https://message.style/cdn/images/f21f9f7c3e914d9b9533f4399bfdfadf7447c85627034cbd00cd0e8d29df0d8f.png"
INVENTORY_FOOTER_TEXT = "-ˋˏ ༺❀ 𝙽𝚒𝚕𝚒𝚟𝚎 𝚃𝚎𝚊𝚖 ❀༻ˎˊ-"
INVENTORY_FOOTER_ICON = "https://message.style/cdn/images/b5b0bf7153118e3c82fc0229a1548dbf5fefdf1691fc0a73b01f45b90ce44b06.gif"

# Inventory tabs. Roles/temp_roles are grouped under "Items" because
# from the member's side they are "things I own and can wear"; the
# split that matters is equippable vs consumable vs cosmetic label,
# not which table the role_id lives in.
TAB_ITEMS = "items"
TAB_COLORS = "colors"
TAB_POTIONS = "potions"
TAB_TITLES = "titles"
TAB_EQUIPPED = "equipped"
TAB_HISTORY = "history"

EQUIPPABLE_TYPES = ("role", "temp_role")
TAB_ORDER = (TAB_ITEMS, TAB_COLORS, TAB_POTIONS, TAB_TITLES, TAB_EQUIPPED, TAB_HISTORY)

# `label`  -- plain text (logs, accessibility, empty states).
# `smallcaps` -- the stylized category label Discord buttons/embed
# titles wear (the agreed Wallet design language).
# `singular` -- used in the select placeholder.
TAB_META = {
    TAB_ITEMS:    {"label": "Items",    "smallcaps": "ɪᴛᴇᴍꜱ",   "singular": "item",  "emoji": "🎒"},
    TAB_COLORS:   {"label": "Colors",   "smallcaps": "ᴄᴏʟᴏʀꜱ",  "singular": "color", "emoji": "🎨"},
    TAB_POTIONS:  {"label": "Potions",  "smallcaps": "ᴘᴏᴛɪᴏɴꜱ", "singular": "potion","emoji": "🧪"},
    TAB_TITLES:   {"label": "Titles",   "smallcaps": "ᴛɪᴛʟᴇꜱ",  "singular": "title", "emoji": "🏷️"},
    TAB_EQUIPPED: {"label": "Equipped", "smallcaps": "ᴇǫᴜɪᴘᴘᴇᴅ", "singular": "entry", "emoji": "✨"},
    TAB_HISTORY:  {"label": "History",  "smallcaps": "ʜɪꜱᴛᴏʀʏ", "singular": "event", "emoji": "📜"},
}

# Rarity glyphs used when an item has no custom emoji or http image,
# so the inline list still has a visual anchor per rarity tier.
RARITY_GLYPH = {
    "common":    "⚪",
    "rare":      "🔵",
    "epic":      "🟣",
    "legendary": "🟡",
    "mythical":  "🟠",
    "secret":    "🔴",
}

# Ledger `source` values in use across the project, mapped to what a
# member sees. Anything unmapped falls back to a title-cased version
# of the raw source rather than being hidden — an unlabelled
# transaction is still money that moved, and silently dropping it
# would make the receipts lie about the balance.
SOURCE_LABELS = {
    "daily": "Daily reward",
    "shop": "Shop purchase",
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


def _rarity_label(rarity: str) -> str:
    return (rarity or "common").title()


def _rarity_glyph(rarity: str) -> str:
    return RARITY_GLYPH.get((rarity or "common").lower(), RARITY_GLYPH["common"])


def _item_display(catalog: dict, fallback_glyph: str) -> str:
    """
    Chooses the best inline representation for an item in a list:
      1. A custom emoji the admin configured (icon_url = "<:name:id>").
         Discord renders these inline inside embed field text.
      2. Otherwise, the fallback glyph (rarity dot or tab emoji) so
         every line still has a visual anchor. HTTP image URLs cannot
         be rendered inline in field values, so those are reserved for
         the embed thumbnail on the item's detail view (the one place
         Discord actually renders them).
    """
    prepared = (catalog.get("app_emoji") or "").strip()
    if prepared:
        return prepared
    raw = (catalog.get("icon_url") or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw
    return fallback_glyph


def _item_thumbnail_url(catalog: dict) -> str | None:
    """HTTP(s) image URL for an item, if the admin configured one."""
    raw = (catalog.get("icon_url") or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return None


async def _shop_icon_urls(guild_id: int,
                          item_names: list[str]) -> dict[str, str]:
    """Read-only icon_url lookup for owned items with no catalog URL.

    `item_catalog` is written when a Shop item is SAVED, so a listing
    that predates that write path has no catalog row at all — even
    though its emoji may already be prepared against its icon_url. The
    listing itself is the only other place that URL lives.

    Only an unambiguous, exact, guild-scoped match is accepted: one
    single `shop_items` row for that exact name in this guild, carrying
    an http(s) icon_url. Two listings sharing a name (or a nameless
    match) return nothing, so another product's emoji can never be
    attached to this item. No writes, no preparation, no backfill.
    """
    try:
        import aiosqlite

        from database import DB_PATH
        from utils.shop_emoji import find_listing_icon_urls

        async with aiosqlite.connect(DB_PATH) as db:
            return await find_listing_icon_urls(db, guild_id, item_names)
    except Exception:
        # No shop_items table / unreadable DB: behave exactly as before.
        return {}


async def _attach_prepared_app_emojis(guild_id: int,
                                      entries: list[dict]) -> None:
    """Resolve already-prepared Shop emoji for owned entries. READ-ONLY.

    Inventory rows are keyed by item_name and carry no Shop product id,
    so the source of truth for an entry's icon is the catalog row
    (get_catalog_entry), whose `icon_url` is exactly the URL Shop
    preparation aliased. When the catalog has no URL (a listing saved
    before the catalog write path existed), the exact guild-scoped Shop
    listing is used instead — see _shop_icon_urls(). Passing that URL
    through attach_prepared_emojis() means the lookup goes through the
    same canonicalisation, guild alias and `ready` asset the shop
    preview uses — no second emoji mechanism, no guessed emoji name/ID.

    Only batched SELECTs run here. No image download, no upload, no
    delete, no recovery/preparation, no DDL: if the tables are missing
    or nothing is prepared, `app_emoji` simply stays empty and the
    caller falls back. The source URL may change later in Shop; this
    deliberately reads the mapping that exists NOW rather than fetching
    new bytes during navigation.
    """
    if not entries:
        return
    products = [{"icon_url": (entry.get("icon_url")
                              or entry.get("_shop_icon_url"))}
                for entry in entries]
    try:
        import aiosqlite

        from database import DB_PATH
        from utils.shop_emoji import attach_prepared_emojis

        async with aiosqlite.connect(DB_PATH) as db:
            await attach_prepared_emojis(db, guild_id, products)
    except Exception:
        # An unprepared/unreadable mapping is an ordinary state, never a
        # reason for Inventory to fail or to start preparing.
        pass
    for entry, product in zip(entries, products):
        entry["app_emoji"] = (product.get("app_emoji") or "").strip()


async def _decorate(guild_id: int, items: list[dict]) -> list[dict]:
    """Attach catalog metadata + sort rarity-first."""
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
    missing = [it for it in out if not (it.get("icon_url") or "").strip()]
    if missing:
        fallback = await _shop_icon_urls(guild_id, [it["item_name"] for it in missing])
        for it in missing:
            it["_shop_icon_url"] = fallback.get(it["item_name"], "")
    await _attach_prepared_app_emojis(guild_id, out)
    out.sort(key=lambda x: (x["_sort"], x["item_name"]), reverse=True)
    return out


async def load_wallet_snapshot(guild_id: int, user_id: int) -> dict:
    """
    Everything the hub shows, in one place. Kept as a plain function
    (not a view method) so every panel can refresh from the same
    source after an action mutates state.
    """
    await cleanup_unowned_title(guild_id, user_id)

    items = await get_inventory(guild_id, user_id, include_empty=False)
    total_items = sum(int(it["quantity"] or 0) for it in items)

    state = await get_streak_state(guild_id, user_id)
    currency = await get_currency_config(guild_id)

    return {
        "coins": await get_balance(guild_id, user_id, currency="balance"),
        "diamonds": await get_balance(guild_id, user_id, currency="diamonds"),
        "items": items,
        "total_items": total_items,
        "streak": state["streak"],
        "claimed_today": state["claimed_today"],
        "seconds_remaining": state["seconds_remaining"],
        "currency": currency,
    }


def split_by_tab(items: list[dict]) -> dict:
    return {
        TAB_ITEMS: [it for it in items
                    if it["item_type"] not in (POTION_ITEM_TYPE, TITLE_ITEM_TYPE)
                    and str(it.get("category") or (it.get("metadata") or {}).get("category") or "").lower() not in ("color", "colors")],
        TAB_COLORS: [it for it in items
                     if str(it.get("category") or (it.get("metadata") or {}).get("category") or "").lower() in ("color", "colors")],
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
    per-callback code never has to repeat the owner check — one
    guard, impossible to forget on a new button.
    """

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.guild_id = guild_id
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.user.id != self.user_id or
                getattr(interaction, "guild_id", self.guild_id) != self.guild_id):
            await interaction.response.send_message(
                "This is not your wallet — run `/wallet` to open your own.",
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

def build_hub_embed(snap: dict) -> discord.Embed:
    """
    NERO-branded wallet card. The bot name, not the member name, sits
    in the title (the member identity is still shown via set_author so
    ownership is unambiguous). Layout inspiration comes from Tatsu's
    wallet card — a strong wordmark, two currencies stacked vertically,
    summary stats below — but the color palette, typography, field
    layout and labels are our own.
    """
    cc = snap["currency"]["coins"]
    cd = snap["currency"]["diamonds"]

    embed = discord.Embed(
        title="✦ WALLET ✦",
        color=WALLET_COLOR)
    embed.set_author(name="NERO")

    # Currency block — vertical stack, the visual focus of the card.
    coins_line = f"{cc['emoji']} **{snap['coins']:,}** {cc['name']}"
    diamonds_line = f"{cd['emoji']} **{snap['diamonds']:,}** {cd['name']}"
    embed.add_field(
        name="Currency",
        value=f"{coins_line}\n{diamonds_line}",
        inline=False)

    # Summary stats in one row.
    if snap["claimed_today"]:
        streak_value = (f"Day **{snap['streak']}** · next in "
                        f"{format_remaining(snap['seconds_remaining'])}")
    elif snap["streak"]:
        streak_value = f"Day **{snap['streak']}** · ready to claim"
    else:
        streak_value = "Ready to start"

    embed.add_field(
        name="Items",
        value=f"**{snap['total_items']:,}**", inline=True)
    embed.add_field(
        name="Streak",
        value=streak_value, inline=True)

    return embed


class WalletHubView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, claimed_today: bool):
        super().__init__(guild_id, user_id)
        # The streak button reflects live state before it is ever
        # clicked: nothing to claim reads as a muted secondary button,
        # a ready claim is a green call to action. The member can
        # still open it either way to see their timer and bonus.
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
        # Vote is a planned feature. The slot is reserved and disabled
        # so the hub's final layout is visible without shipping a
        # half-feature or a button that lies.
        await interaction.response.defer()


async def render_hub(interaction: discord.Interaction, guild_id: int,
                     user_id: int, *, first: bool = False):
    snap = await load_wallet_snapshot(guild_id, user_id)
    embed = build_hub_embed(snap)
    view = WalletHubView(guild_id, user_id, snap["claimed_today"])

    if first:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
    else:
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message


# ─── Streak ─────────────────────────────────────────────────────────────

def build_streak_embed(preview: dict, currency: dict) -> discord.Embed:
    """
    Streak panel. Two states:

    NOT claimed yet:
        Daily Reward  · Up to <max> <coin>
        Streak        · Day <next_streak>
        Bonus         · +<bonus> <coin>
        (No Cooldown field — Claim button is the call to action.)

    Already claimed today:
        Daily Reward  · Already claimed
        Streak        · Day <streak>
        Cooldown      · <remaining>
        (No Bonus field — bonus was already paid.)

    The post-claim state (build_claim_embed) renders the mock exactly:
    Daily Reward as the actual earned amount, Bonus as the awarded
    bonus, Streak Day N, Cooldown. Pre-claim we cannot show an exact
    coin number because the roll is random — "Up to <max>" is a
    single, honest ceiling rather than a fake or averaged number.
    """
    cc = currency["coins"]
    embed = discord.Embed(title="🔥 STREAK", color=STREAK_COLOR)

    if preview["claimed_today"]:
        embed.add_field(
            name="Daily Reward",
            value="Already claimed",
            inline=True)
        embed.add_field(
            name="Streak",
            value=f"Day **{preview['streak']}**",
            inline=True)
        embed.add_field(
            name="Cooldown",
            value=f"**{format_remaining(preview['seconds_remaining'])}**",
            inline=True)
        return embed

    # Pre-claim: max possible = daily_max + next_bonus (ignoring
    # prestige multiplier, which is user-specific and cannot be
    # predicted without reading booster state here — kept simple).
    bonus = int(preview.get("next_bonus") or 0)
    hi = int(preview.get("daily_max") or 0)
    max_possible = hi + bonus
    embed.add_field(
        name="Daily Reward",
        value=f"Up to **{max_possible:,}** {cc['emoji']} {cc['name']}"
              if max_possible > 0 else f"Claim to receive {cc['emoji']} {cc['name']}",
        inline=True)
    embed.add_field(
        name="Streak",
        value=f"Day **{preview['next_streak']}**",
        inline=True)
    embed.add_field(
        name="Bonus",
        value=f"+**{bonus:,}** {cc['name']}" if bonus > 0 else "—",
        inline=True)
    return embed


def build_claim_embed(result: dict, currency: dict) -> discord.Embed:
    """
    Post-claim receipt — matches the spec's mock exactly:
       Daily Reward   132 Coins
       Streak         Day 7
       Bonus          +32 Coins
       Cooldown       2h 15m
    The prestige multiplier is folded into the awarded amount rather
    than shown as a separate line (it is already accounted for in the
    number the member receives, and surfacing it as a math line adds
    internal noise).
    """
    cc = currency["coins"]
    embed = discord.Embed(title="🔥 STREAK", color=STREAK_COLOR)
    embed.add_field(
        name="Daily Reward",
        value=f"{cc['emoji']} **{result['amount']:,}** {cc['name']}",
        inline=True)
    embed.add_field(
        name="Streak",
        value=f"Day **{result['streak']}**",
        inline=True)
    bonus = int(result.get("streak_bonus") or 0)
    embed.add_field(
        name="Bonus",
        value=f"+**{bonus:,}** {cc['name']}" if bonus > 0 else "—",
        inline=True)
    embed.add_field(
        name="Cooldown",
        value=f"**{format_remaining(result['next_reset_seconds'])}**",
        inline=True)
    return embed


def build_streak_already_claimed_embed(seconds_remaining: int,
                                       preview: dict,
                                       currency: dict) -> discord.Embed:
    """Rendered if Claim loses a race (e.g. claimed from another entry point)."""
    cc = currency["coins"]
    embed = discord.Embed(
        title="🔥 STREAK",
        description="Already claimed today.",
        color=STREAK_COLOR)
    embed.add_field(
        name="Daily Reward",
        value=f"{cc['emoji']} Already claimed",
        inline=True)
    embed.add_field(
        name="Streak",
        value=f"Day **{preview['streak']}**",
        inline=True)
    embed.add_field(
        name="Cooldown",
        value=f"**{format_remaining(seconds_remaining)}**",
        inline=True)
    return embed


class StreakPanelView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, can_claim: bool):
        super().__init__(guild_id, user_id)
        self.claim_button.disabled = not can_claim

    @discord.ui.button(label="Claim", emoji="🔥",
                       style=discord.ButtonStyle.success)
    async def claim_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button):
        currency = await get_currency_config(self.guild_id)
        try:
            result = await perform_streak_claim(
                interaction.client, self.guild_id, self.user_id,
                member=interaction.user)
        except DailyAlreadyClaimed as e:
            # Lost a race against another entry point. Disable all
            # controls and re-render as a quiet "already claimed"
            # receipt rather than leaving Claim clickable.
            for child in self.children:
                child.disabled = True
            preview = await get_streak_preview(self.guild_id, self.user_id)
            await interaction.response.edit_message(
                embed=build_streak_already_claimed_embed(
                    e.seconds_remaining, preview, currency),
                view=self)
            return

        # Successful claim — every button retires.
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=build_claim_embed(result, currency), view=self)

    @discord.ui.button(label="Back", emoji="◀",
                       style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        await render_hub(interaction, self.guild_id, self.user_id)


async def open_streak_panel(interaction: discord.Interaction,
                            guild_id: int, user_id: int,
                            *, first: bool = False):
    """
    Opens the streak panel. Used by BOTH the Wallet Streak button
    (first=False → edit_message) and the /streak slash command
    (first=True → send_message as a fresh ephemeral message). There
    is exactly ONE render path and ONE view for streak; the command
    and the button are identical surfaces.
    """
    preview = await get_streak_preview(guild_id, user_id)
    currency = await get_currency_config(guild_id)
    view = StreakPanelView(
        guild_id, user_id, can_claim=not preview["claimed_today"])
    embed = build_streak_embed(preview, currency)
    if first:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
    else:
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message


# ─── Inventory ──────────────────────────────────────────────────────────

def _brand_inventory(embed: discord.Embed, interaction, tab: str, footer: str) -> None:
    bot_user = getattr(getattr(interaction, "client", None), "user", None)
    bot_name = getattr(bot_user, "name", None) or "Nilive"
    user = getattr(interaction, "user", None)
    username = getattr(user, "display_name", None) or getattr(user, "name", None) or "Inventory"
    meta = TAB_META[tab]
    embed.color = INVENTORY_COLOR
    embed.set_author(name=f"{bot_name} · {meta['smallcaps']} · {username}"[:256],
                     icon_url=INVENTORY_AUTHOR_ICON)
    embed.set_footer(text=f"{INVENTORY_FOOTER_TEXT} · {footer}"[:2048],
                     icon_url=INVENTORY_FOOTER_ICON)


def _role_entry_is_equipped(entry: dict, equipped_role: str | None) -> bool:
    if not equipped_role:
        return False
    if equipped_role.startswith(("i:", "h:")):
        return entry.get("entry_key") == equipped_role
    return not entry.get("timed") and entry.get("item_name") == equipped_role


def _code_name(name: str) -> str:
    """Item name inside an inline code span (the agreed decorated-row style).

    Discord renders inline code literally — markdown is inert there, so
    markdown-escaping would only leak backslashes. Only the code-span
    delimiters themselves need neutralising; mentions are likewise inert
    inside code and are left untouched so copy/paste stays clean.
    """
    clean = (name or "").replace("\n", " ").replace("\\", "\\\\").replace("`", "'")
    return clean[:100]


def _expiry_dt(entry: dict):
    if not entry.get("timed"):
        return None
    from utils.timed_ownership import parse_time
    return parse_time(entry.get("expires_at"))


def _entry_emoji(item: dict) -> str:
    return _item_display(item, _rarity_glyph(item["rarity"]))


def _is_equipped_entry(it: dict, equipped_role: str | None,
                       equipped_title_name: str | None) -> bool:
    if it["item_type"] in EQUIPPABLE_TYPES and _role_entry_is_equipped(it, equipped_role):
        return True
    return (it["item_type"] == TITLE_ITEM_TYPE and it["item_name"] == equipped_title_name)


def _product_row(it: dict, tab: str, equipped_role: str | None,
                 equipped_title_name: str | None) -> str:
    """One decorated inventory line: prepared emoji + backticked name + tags."""
    emoji = _entry_emoji(it)
    name = _code_name(it["item_name"])
    qty = int(it["quantity"] or 0)
    line = f"{emoji} `{name}`"
    if qty > 1:
        line += f" **×{qty}**"

    tags = []
    if tab == TAB_POTIONS:
        tags.append(describe_potion(it.get("metadata")))
        if not potion_is_usable(it.get("metadata")):
            tags.append("*Setup needed*")
    else:
        if _is_equipped_entry(it, equipped_role, equipped_title_name):
            tags.append("*✅ Equipped*")
    tags.append(f"*{_rarity_label(it['rarity'])}*")
    expiry = _expiry_dt(it)
    if expiry is not None:
        tags.append(f"expires {discord.utils.format_dt(expiry, 'R')}")
    elif it.get("timed"):
        tags.append("expires *unknown*")
    return line + " — " + " · ".join(tags)


def _thumbnail_for(entries: list[dict]) -> str | None:
    # The first (highest rarity) item with an http image is promoted to
    # the thumbnail so the panel isn't just text. Discord only allows
    # one thumbnail per embed, so this is a "featured item" treatment
    # rather than a per-line image (which is not possible in field text).
    return next((u for u in (_item_thumbnail_url(it) for it in entries) if u), None)


def build_inventory_embed(tab: str, entries: list[dict],
                          equipped_role: str | None,
                          equipped_title_name: str | None,
                          *, slots: dict | None = None) -> discord.Embed:
    meta = TAB_META[tab]
    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['smallcaps']}", color=INVENTORY_COLOR)

    if tab == TAB_EQUIPPED:
        return _build_equipped_embed(embed, entries, equipped_role,
                                     equipped_title_name, slots)

    if not entries:
        empty = {
            TAB_ITEMS: "No items yet — get one from `/shop`.",
            TAB_COLORS: "No colors yet — find one in `/shop`.",
            TAB_POTIONS: "No potions yet — get one from `/shop`.",
            TAB_TITLES: "No titles yet — get one from `/shop`.",
        }[tab]
        embed.description = empty
        return embed

    thumb = _thumbnail_for(entries)
    if thumb:
        embed.set_thumbnail(url=thumb)

    lines = []
    if tab == TAB_COLORS:
        lines.append("-# Color roles are worn from this panel — one equipped at a time.")
    lines.extend(_product_row(it, tab, equipped_role, equipped_title_name)
                 for it in entries)
    embed.description = "\n".join(lines)
    return embed


def _slot_value(slot: dict | None) -> str:
    if not slot:
        return "*Nothing equipped*"
    emoji = slot.get("emoji") or _rarity_glyph(slot.get("rarity") or "common")
    name = _code_name(slot["name"])
    lines = [f"{emoji} `{name}`"]
    bits = []
    if slot.get("rarity"):
        bits.append(f"*{_rarity_label(slot['rarity'])}*")
    if slot.get("mention"):
        bits.append(slot["mention"])
    if bits:
        lines.append(" · ".join(bits))
    if slot.get("expires") is not None:
        lines.append(f"expires {discord.utils.format_dt(slot['expires'], 'R')}")
    elif slot.get("timed"):
        lines.append("expires *unknown*")
    return "\n".join(lines)


def _build_equipped_embed(embed: discord.Embed, entries: list[dict],
                          equipped_role: str | None,
                          equipped_title_name: str | None,
                          slots: dict | None) -> discord.Embed:
    slots = slots or {"role": None, "title": None}
    role_slot, title_slot = slots.get("role"), slots.get("title")

    if not role_slot and not title_slot:
        embed.description = (
            "Nothing equipped right now.\n"
            "Equip a role, a color or a title and it will appear here.")
        return embed

    thumb = _thumbnail_for(entries)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.description = (
        "-# What you're wearing right now — your full collection lives in the other tabs.")
    embed.add_field(name="🎭 Role", value=_slot_value(role_slot),
                    inline=True)
    embed.add_field(name="🏷️ Title", value=_slot_value(title_slot),
                    inline=True)
    return embed


def _select_description(it: dict, equipped: bool) -> str:
    bits = [f"×{int(it['quantity'] or 0)}", _rarity_label(it["rarity"])]
    expiry = _expiry_dt(it)
    if expiry is not None:
        bits.append(f"until {expiry.strftime('%Y-%m-%d')}")
    elif it.get("timed"):
        bits.append("timed")
    else:
        bits.append("Permanent")
    if equipped:
        bits.append("Equipped")
    return " · ".join(bits)[:100]


class InventoryItemSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 entries: list[dict], equipped_role: str | None,
                 equipped_title_name: str | None, page: int = 0):
        options = []
        for it in entries[:25]:
            name = it["item_name"]
            equipped = _is_equipped_entry(it, equipped_role, equipped_title_name)
            options.append(discord.SelectOption(
                label=name[:100],
                description=_select_description(it, equipped),
                value=it.get("entry_key") or name[:100],
                # The product's OWN prepared emoji, the same one the
                # action entries of a published Shop use. Empty until
                # prepared — never a substituted stand-in.
                emoji=as_partial_emoji(it.get("app_emoji") or "")))
        super().__init__(
            placeholder=f"Select a {TAB_META[tab]['singular']}…",
            options=options or [discord.SelectOption(label="—", value="—")],
            disabled=not options)
        self.guild_id = guild_id
        self.user_id = user_id
        self.tab = tab
        self.page = page

    async def callback(self, interaction: discord.Interaction):
        await open_item_panel(
            interaction, self.guild_id, self.user_id,
            self.tab, self.values[0], page=self.page)


class InventoryView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 entries: list[dict], equipped_role: str | None,
                 equipped_title_name: str | None, page: int = 0, pages: int = 1):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.page = page
        if tab != TAB_HISTORY:
            self.add_item(InventoryItemSelect(
                guild_id, user_id, tab, entries,
                equipped_role, equipped_title_name, page))
        for key in (TAB_ITEMS, TAB_COLORS, TAB_POTIONS, TAB_TITLES, TAB_EQUIPPED, TAB_HISTORY):
            self.add_item(InventoryTabButton(key, active=(key == tab)))
        self.add_item(InventoryBackButton())
        if page > 0 or page + 1 < pages:
            if page > 0:
                self.add_item(HistoryPageButton(-1))
            if page + 1 < pages:
                self.add_item(HistoryPageButton(1))


class InventoryTabButton(discord.ui.Button):
    def __init__(self, tab: str, active: bool):
        meta = TAB_META[tab]
        super().__init__(
            label=meta["smallcaps"], emoji=meta["emoji"],
            row=1 if tab in (TAB_ITEMS, TAB_COLORS, TAB_POTIONS) else 2,
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            disabled=active)
        self.tab = tab

    async def callback(self, interaction: discord.Interaction):
        view: InventoryView = self.view
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id, self.tab)


class HistoryPageButton(discord.ui.Button):
    def __init__(self, direction: int):
        super().__init__(label="Previous" if direction < 0 else "Next",
                         style=discord.ButtonStyle.secondary, row=3)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: InventoryView = self.view
        await open_inventory_panel(interaction, view.guild_id, view.user_id,
                                   view.tab, page=view.page + self.direction)


class InventoryBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="◀", row=3,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: InventoryView = self.view
        await render_hub(interaction, view.guild_id, view.user_id)



# Arrows/moves shown in the History tab. Maps only KNOWN event types —
# anything else falls back to a neutral glyph rather than being hidden.
EVENT_GLYPHS = {
    "acquired": "📥",
    "equipped": "✨",
    "unequipped": "✖",
    "used": "🧪",
    "dropped": "🗑️",
    "expired": "⏳",
    "reversed": "↩️",
    "ownership_reversed": "↩️",
    "restored": "♻️",
}


def _event_time(raw):
    """SQLite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS', UTC) → Discord relative
    timestamp; unparseable input is shown verbatim (never dropped)."""
    if not raw:
        return "unknown time"
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(str(raw)[:26], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return discord.utils.format_dt(parsed, "R")
        except ValueError:
            continue
    return discord.utils.escape_markdown(str(raw))[:40]


def build_history_embed(events, started):
    """Append-only activity log — reads as an audit trail, not as money.

    Only real, recorded events are rendered; unknown event types are shown
    with their raw (escaped) name rather than hidden or reinterpreted.
    """
    meta = TAB_META[TAB_HISTORY]
    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['smallcaps']}", color=INVENTORY_COLOR)
    lines = []
    for event in events:
        raw_type = str(event.get("event_type") or "event").lower()
        glyph = EVENT_GLYPHS.get(raw_type, "•")
        name = _code_name(event.get("item_name") or "?")
        source = discord.utils.escape_mentions(discord.utils.escape_markdown(
            str(event.get("source") or "system")))[:40]
        kind = raw_type.replace("_", " ").title()[:40]
        qty = int(event.get("quantity") or 1)
        head = f"{glyph} **{kind}** · `{name}`"
        if qty > 1:
            head += f" **×{qty}**"
        head += f" · {source}"
        lines.append(f"{head}\n*{_event_time(event.get('created_at'))}*")
    embed.description = ("\n\n".join(lines) if lines
                         else "No recorded inventory activity yet.")
    return embed


def _equipped_slots(decorated_all, equipped, equipped_title_name):
    """Resolve the Role and Title equip slots against the FULL decorated
    inventory (not the page slice), so the Equipped view shows the truth
    even when the equipped entry is paged out."""
    role_slot = None
    if equipped:
        match = None
        role_key = equipped.get("entry_key")
        for it in decorated_all:
            if it["item_type"] not in EQUIPPABLE_TYPES:
                continue
            if role_key:
                if it.get("entry_key") == role_key:
                    match = it
                    break
            elif not it.get("timed") and it["item_name"] == equipped.get("item_name"):
                match = it
                break
        if match is not None:
            role_slot = {
                "name": match["item_name"],
                "emoji": _entry_emoji(match),
                "rarity": match.get("rarity"),
                "expires": _expiry_dt(match),
                "timed": bool(match.get("timed")),
                "mention": None,
            }
        else:
            # Equipped but not present in inventory (e.g. an admin removed
            # the stack) — still tell the truth about what is worn.
            role_slot = {
                "name": equipped.get("item_name") or "?",
                "emoji": None,
                "rarity": None,
                "expires": None,
                "timed": False,
                "mention": None,
            }
        role_id = equipped.get("role_id")
        if role_id:
            role_slot["mention"] = f"<@&{int(role_id)}>"
    title_slot = None
    if equipped_title_name:
        match = next(
            (it for it in decorated_all
             if it["item_type"] == TITLE_ITEM_TYPE
             and it["item_name"] == equipped_title_name), None)
        title_slot = {
            "name": equipped_title_name,
            "emoji": _entry_emoji(match) if match else None,
            "rarity": match.get("rarity") if match else None,
            "expires": None,
            "timed": False,
            "mention": None,
        }
    return {"role": role_slot, "title": title_slot}


async def open_inventory_panel(interaction: discord.Interaction,
                               guild_id: int, user_id: int, tab: str,
                               *, first: bool = False, page: int = 0):
    if tab == TAB_HISTORY:
        from utils.inventory_activity import count_activity, get_activity_page, get_activity_start
        total = await count_activity(guild_id, user_id)
        pages = max(1, (total + 14) // 15)
        page = max(0, min(page, pages - 1))
        events = await get_activity_page(guild_id, user_id, offset=page * 15, limit=15)
        started = await get_activity_start()
        embed = build_history_embed(events, started)
        _brand_inventory(embed, interaction, tab,
                         f"Partial coverage · Recording started {started or 'unknown'} UTC · Page {page + 1}/{pages}")
        view = InventoryView(guild_id, user_id, tab, [], None, None, page, pages)
        if first:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            view.message = await interaction.original_response()
        else:
            await interaction.response.edit_message(embed=embed, view=view)
            view.message = interaction.message
        return
    await cleanup_unowned_title(guild_id, user_id)
    items = await get_inventory_entries(guild_id, user_id)
    equipped = await get_equipped(guild_id, user_id)
    equipped_role_name = equipped["item_name"] if equipped else None
    equipped_role = (equipped.get("entry_key") or equipped_role_name) if equipped else None
    equipped_title = await get_equipped_title(guild_id, user_id)
    equipped_title_name = (equipped_title["item_name"]
                           if equipped_title else None)

    decorated_all = await _decorate(guild_id, items)
    if tab == TAB_EQUIPPED:
        role_key = equipped.get("entry_key") if equipped else None
        entries = [it for it in decorated_all if
                   (equipped and (it.get("entry_key") == role_key if role_key else
                    not it.get("timed") and it["item_name"] == equipped_role_name)) or
                   (it["item_type"] == TITLE_ITEM_TYPE and it["item_name"] == equipped_title_name)]
    else:
        entries = split_by_tab(decorated_all)[tab]
    slots = (_equipped_slots(decorated_all, equipped, equipped_title_name)
             if tab == TAB_EQUIPPED else None)
    pages = max(1, (len(entries) + 14) // 15)
    page = max(0, min(page, pages - 1))
    entries = entries[page * 15:(page + 1) * 15]
    view = InventoryView(guild_id, user_id, tab, entries,
                         equipped_role, equipped_title_name, page, pages)
    embed = build_inventory_embed(tab, entries, equipped_role, equipped_title_name,
                                  slots=slots)
    if tab == TAB_ITEMS:
        config = await get_currency_config(guild_id)
        balances = []
        for currency_key in ("balance", "diamonds"):
            currency = for_currency(config, currency_key)
            balance = await get_balance(guild_id, user_id, currency=currency_key)
            balances.append(f"{currency['emoji']} **{balance:,}** {currency['name']}")
        embed.add_field(name="Balances", value="\n".join(balances), inline=False)
    _brand_inventory(embed, interaction, tab, f"Select an item · Page {page + 1}/{pages}")
    if first:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
    else:
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message


# ─── Item detail / management ───────────────────────────────────────────

def build_item_embed(guild: discord.Guild, tab: str, item: dict,
                     equipped: bool) -> discord.Embed:
    thumb = _item_thumbnail_url(item)
    glyph = _rarity_glyph(item["rarity"])
    emoji = _item_display(item, glyph)
    embed = discord.Embed(
        title=f"{emoji} `{_code_name(item['item_name'])}`",
        color=INVENTORY_COLOR)

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

    # A role item's Discord role is shown as a mention so the member
    # can see what wearing it grants — but the DB row is the source
    # of truth, not the role itself. No "Expires" / "Paid" / "Bought"
    # per spec — that data is admin-side and clutters the member view.
    if item["item_type"] in EQUIPPABLE_TYPES:
        role_id = (item.get("metadata") or {}).get("role_id") or item.get("role_id")
        try:
            role = guild.get_role(int(role_id)) if guild and role_id else None
        except (TypeError, ValueError):
            role = None
        embed.add_field(
            name="Role",
            value=(role.mention if role else "*missing — ask an admin*"),
            inline=True)

    if item.get("timed"):
        from utils.timed_ownership import parse_time
        expiry = parse_time(item.get("expires_at"))
        embed.add_field(name="Expires", value=discord.utils.format_dt(expiry, 'R') if expiry else "Unknown", inline=True)
    if thumb:
        embed.set_thumbnail(url=thumb)

    return embed


class ItemPanelView(WalletBaseView):
    """
    Actions are built from the item's type and current state, so the
    member is never offered an action the backend would reject:
      * Potions     -> Activate (disabled if effect is misconfigured)
      * Titles      -> Equip or Unequip, whichever applies
      * Equippables -> Equip or Unequip, whichever applies
      * Permanent items -> Drop (with confirmation)
      * All items   -> Back
    """

    def __init__(self, guild_id: int, user_id: int, tab: str,
                 item: dict, equipped: bool):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.item_name = item["item_name"]
        self.item_type = item["item_type"]
        self.entry_key = item.get("entry_key")
        self.page = item.get("_page", 0)
        self.timed = bool(item.get("timed"))
        self.slot_checked = item.get("_slot_checked", False)
        self.slot_snapshot = item.get("_slot_snapshot")

        if item["item_type"] == POTION_ITEM_TYPE:
            self.add_item(ActivateButton(
                enabled=potion_is_usable(item.get("metadata"))))
        elif item["item_type"] == TITLE_ITEM_TYPE or item["item_type"] in EQUIPPABLE_TYPES:
            self.add_item(EquipToggleButton(equipped))

        if not self.timed:
            self.add_item(DropButton())
        self.add_item(ItemBackButton())


class ActivateButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        super().__init__(label="Activate", emoji="🧪",
                         style=discord.ButtonStyle.success,
                         disabled=not enabled)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        result = await use_potion(
            view.guild_id, view.user_id, view.item_name, entry_key=view.entry_key)
        if not result.get("success"):
            await interaction.response.send_message(
                f"Could not activate that potion — {result.get('error', 'something went wrong.')}",
                ephemeral=True)
            return
        if result["remaining"] <= 0:
            await open_inventory_panel(
                interaction, view.guild_id, view.user_id, view.tab, page=view.page)
        else:
            await open_item_panel(
                interaction, view.guild_id, view.user_id,
                view.tab, view.entry_key or view.item_name, page=view.page)


class EquipToggleButton(discord.ui.Button):
    def __init__(self, equipped: bool):
        self.equipped = equipped
        if equipped:
            super().__init__(label="Unequip", emoji="✖",
                             style=discord.ButtonStyle.secondary)
        else:
            super().__init__(label="Equip", emoji="✨",
                             style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        if view.item_type == TITLE_ITEM_TYPE:
            if self.equipped:
                result = await unequip_title(
                    view.guild_id, view.user_id, view.item_name, entry_key=view.entry_key)
            else:
                options = {"entry_key": view.entry_key}
                if view.slot_checked:
                    options["expected_title"] = view.slot_snapshot
                result = await equip_title(
                    view.guild_id, view.user_id, view.item_name, **options)
        elif view.item_type in EQUIPPABLE_TYPES:
            if self.equipped:
                result = await unequip_role(
                    interaction.client, view.guild_id, view.user_id,
                    view.item_name, entry_key=view.entry_key)
            else:
                options = {"entry_key": view.entry_key}
                if view.slot_checked:
                    options["expected_slot"] = view.slot_snapshot
                result = await equip_role(
                    interaction.client, view.guild_id, view.user_id,
                    view.item_name, **options)
        else:
            result = {"success": False,
                      "error": "This item cannot be equipped."}

        if not result.get("success"):
            await interaction.response.send_message(
                f"Could not change equip state — {result.get('error', 'something went wrong.')}",
                ephemeral=True)
            return

        await open_item_panel(
            interaction, view.guild_id, view.user_id,
            view.tab, view.entry_key or view.item_name, page=view.page)


class DropButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Drop", emoji="🗑️",
                         style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        await open_drop_confirm(
            interaction, view.guild_id, view.user_id,
            view.tab, view.entry_key or view.item_name, page=view.page)


class ItemBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="◀",
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id, view.tab, page=view.page)


# ─── Drop confirmation ─────────────────────────────────────────────────

def build_drop_confirm_embed(tab: str, item_name: str,
                             item: dict) -> discord.Embed:
    glyph = _rarity_glyph(item["rarity"])
    emoji = _item_display(item, glyph)
    thumb = _item_thumbnail_url(item)
    embed = discord.Embed(
        title=f"{emoji} `{_code_name(item['item_name'])}`",
        description=(
            "Are you sure you want to drop this item?\n"
            "*This cannot be undone.*"),
        color=0xE74C3C)
    embed.add_field(
        name="Quantity",
        value=f"×{int(item['quantity'] or 0)} (all copies)",
        inline=True)
    embed.add_field(name="Rarity",
                    value=_rarity_label(item["rarity"]), inline=True)
    if thumb:
        embed.set_thumbnail(url=thumb)
    return embed


class DropConfirmView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, tab: str, item_name: str,
                 item_id: int | None = None, item_type: str | None = None,
                 page: int = 0, quantity: int | None = None,
                 metadata: dict | None = None, timed_holding: dict | None = None):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.item_name = item_name
        self.item_id = item_id
        self.item_type = item_type
        self.page = page
        self.quantity = quantity
        self.metadata = metadata
        self.timed_holding = timed_holding

    @discord.ui.button(label="Confirm", emoji="🗑️",
                       style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        if self.timed_holding is not None:
            await interaction.response.send_message(
                "Timed holdings cannot be dropped.", ephemeral=True)
            return
        from utils.equip_engine import slot_lock
        async with slot_lock(self.guild_id, self.user_id):
            # Unequip first if needed, then delete the stack. Order
            # matters: equip/title cleanup looks at inventory to decide
            # what to clear; drop_item removes the row, so we resolve the
            # current equipped state up front.
            if self.item_id is not None:
                entries = await get_inventory(self.guild_id, self.user_id, include_empty=False)
                if not any(entry['id'] == self.item_id and entry['item_name'] == self.item_name
                           and (self.item_type is None or entry['item_type'] == self.item_type)
                           and (self.quantity is None or entry['quantity'] == self.quantity)
                           and (self.metadata is None or (entry.get('metadata') or {}) == self.metadata)
                           for entry in entries):
                    await interaction.response.send_message(
                        "That inventory entry has changed. Open it again.", ephemeral=True)
                    return
            unequip_result = None
            if self.item_type == TITLE_ITEM_TYPE or (self.item_type is None and self.tab == TAB_TITLES):
                current = await get_equipped_title(self.guild_id, self.user_id)
                if current and current["item_name"] == self.item_name:
                    unequip_result = await unequip_title(self.guild_id, self.user_id,
                                                         self.item_name, entry_key=f"i:{self.item_id}" if self.item_id else None)
            elif self.item_type in EQUIPPABLE_TYPES or (self.item_type is None and self.tab == TAB_ITEMS):
                current = await get_equipped(self.guild_id, self.user_id)
                if current and current["item_name"] == self.item_name and (
                        not current.get("entry_key") or current.get("entry_key") == f"i:{self.item_id}"):
                    # The enclosing confirmation holds the role slot lock already.
                    unlocked = getattr(unequip_role, "__wrapped__", unequip_role)
                    unequip_result = await unlocked(interaction.client, self.guild_id,
                                                    self.user_id, self.item_name,
                                                    entry_key=f"i:{self.item_id}" if self.item_id else None)
            if unequip_result is not None and not unequip_result.get("success"):
                await interaction.response.send_message(
                    f"Could not unequip that item — {unequip_result.get('error', 'something went wrong.')}",
                    ephemeral=True)
                return

            result = await drop_item(self.guild_id, self.user_id, self.item_name,
                                     expected_id=self.item_id, expected_type=self.item_type,
                                     expected_quantity=self.quantity, expected_metadata=self.metadata,
                                     prevent_equipped=True)
            if not result.get("success"):
                await interaction.response.send_message(
                    f"Could not drop that item — {result.get('error', 'something went wrong.')}",
                    ephemeral=True)
                return
            await open_inventory_panel(
                interaction, self.guild_id, self.user_id, self.tab, page=self.page)

    @discord.ui.button(label="Cancel", emoji="✖",
                       style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        await open_item_panel(
            interaction, self.guild_id, self.user_id,
            self.tab, (self.timed_holding["entry_key"] if self.timed_holding else
                       f"i:{self.item_id}" if self.item_id else self.item_name), page=self.page)


async def open_drop_confirm(interaction: discord.Interaction,
                            guild_id: int, user_id: int,
                            tab: str, item_name: str, *, page: int = 0):
    items = await get_inventory_entries(guild_id, user_id)
    key = item_name if item_name.startswith(("i:", "h:")) else None
    item = find_entry(items, entry_key=key, item_name=None if key else item_name)
    if not item or (tab != TAB_EQUIPPED and item not in split_by_tab(items).get(tab, [])):
        await interaction.response.send_message("That entry cannot be dropped.", ephemeral=True)
        return
    if item.get("timed"):
        await interaction.response.send_message(
            "Timed holdings cannot be dropped.", ephemeral=True)
        return
    item = (await _decorate(guild_id, [item]))[0]
    view = DropConfirmView(guild_id, user_id, tab, item["item_name"],
                           item_id=item["id"], item_type=item["item_type"], page=page,
                           quantity=item["quantity"], metadata=item.get("metadata") or {})
    embed = build_drop_confirm_embed(tab, item["item_name"], item)
    _brand_inventory(embed, interaction, tab, "Confirm or cancel")
    await interaction.response.edit_message(embed=embed, view=view)
    view.message = interaction.message


async def open_item_panel(interaction: discord.Interaction, guild_id: int,
                          user_id: int, tab: str, item_name: str, *, page: int = 0):
    items = await get_inventory_entries(guild_id, user_id)
    key = item_name if item_name.startswith(("i:", "h:")) else None
    item = find_entry(items, entry_key=key, item_name=None if key else item_name)
    if not item or (tab != TAB_EQUIPPED and item not in split_by_tab(items).get(tab, [])):
        await interaction.response.send_message("That inventory entry is no longer available.", ephemeral=True)
        return
    item = (await _decorate(guild_id, [item]))[0]
    item["_page"] = page
    if item["item_type"] == TITLE_ITEM_TYPE:
        current = await get_equipped_title(guild_id, user_id)
        equipped = bool(current and current["item_name"] == item["item_name"])
        item["_slot_checked"] = True
        item["_slot_snapshot"] = current["item_name"] if current else None
    elif item["item_type"] in EQUIPPABLE_TYPES:
        from utils.equip_engine import slot_identity
        current = await get_equipped(guild_id, user_id)
        item["_slot_checked"] = True
        item["_slot_snapshot"] = slot_identity(current)
        equipped = bool(current and (current.get("entry_key") == item["entry_key"]
                        if current.get("entry_key") else
                        not item.get("timed") and current["item_name"] == item["item_name"]))
    else:
        equipped = False
    view = ItemPanelView(guild_id, user_id, tab, item, equipped)
    embed = build_item_embed(interaction.guild, tab, item, equipped)
    _brand_inventory(embed, interaction, tab, f"Item details · Page {page + 1}")
    await interaction.response.edit_message(embed=embed, view=view)
    view.message = interaction.message


# ─── Receipts ───────────────────────────────────────────────────────────

def _format_entry(entry: dict, cur_cfg: dict) -> str:
    amount = int(entry["amount"] or 0)
    sign = "+" if amount > 0 else "−"
    cinfo = for_currency(cur_cfg, entry["currency"])
    emoji = cinfo["emoji"]
    label = SOURCE_LABELS.get(
        entry["source"],
        (entry["source"] or "system").replace("_", " ").title())

    when = format_relative(entry.get("created_at"))
    reason = (entry["reason"] or "").strip()

    line = f"`{sign}{abs(amount):,}` {emoji} **{label}**"
    if entry["reversed"]:
        line += " · ~~reversed~~"
    line += f"\n*{when}*"
    if reason:
        line += f" · {reason[:80]}"
    return line


def build_receipts_embed(currency_key: str, cur_cfg: dict,
                         entries: list[dict], page: int,
                         total: int) -> discord.Embed:
    pages = max(1, -(-total // RECEIPTS_PER_PAGE))
    cinfo = for_currency(cur_cfg, currency_key)
    embed = discord.Embed(
        title=f"🧾 Receipts · {cinfo['emoji']} {cinfo['name']}",
        color=WALLET_COLOR)

    if not entries:
        embed.description = "No transactions recorded yet."
        return embed

    embed.description = "\n\n".join(
        _format_entry(e, cur_cfg) for e in entries)
    embed.set_footer(text=f"Page {page + 1}/{pages} · {total:,} transactions")
    return embed


class ReceiptsView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, currency_key: str,
                 page: int, total: int):
        super().__init__(guild_id, user_id)
        self.currency_key = currency_key
        self.page = page
        self.total = total
        pages = max(1, -(-total // RECEIPTS_PER_PAGE))

        self.prev_page.disabled = page <= 0
        self.next_page.disabled = page >= pages - 1

        self.add_item(CurrencyTabButton(
            "balance", active=(currency_key == "balance")))
        self.add_item(CurrencyTabButton(
            "diamonds", active=(currency_key == "diamonds")))
        self.add_item(ReceiptsBackButton())

    def apply_currency_config(self, cur_cfg: dict):
        """Label the currency tabs from this guild's configured currency
        names/icons. Called immediately after construction, because the
        view is built before the config is awaited."""
        for child in self.children:
            if isinstance(child, CurrencyTabButton):
                info = for_currency(cur_cfg, child.currency_key)
                child.label = info["name"]
                child.emoji = as_partial_emoji(info["emoji"])

    @discord.ui.button(label="Previous", emoji="◀",
                       style=discord.ButtonStyle.secondary, row=0)
    async def prev_page(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        await open_receipts_panel(
            interaction, self.guild_id, self.user_id,
            self.currency_key, max(0, self.page - 1))

    @discord.ui.button(label="Next", emoji="▶",
                       style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        await open_receipts_panel(
            interaction, self.guild_id, self.user_id,
            self.currency_key, self.page + 1)


class CurrencyTabButton(discord.ui.Button):
    def __init__(self, currency_key: str, active: bool):
        # The label/emoji are placeholders — open_receipts_panel() always
        # replaces them with the guild's resolved currency config right
        # after the view is constructed (see the loop over view.children
        # there), so this __init__ must NOT carry its own copy of the
        # default names or icons. It used to, which was a second source of
        # truth for the defaults that could drift from utils/currency.py.
        super().__init__(
            label="…",
            row=1,
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            disabled=active)
        self.currency_key = currency_key

    async def callback(self, interaction: discord.Interaction):
        view: ReceiptsView = self.view
        await open_receipts_panel(
            interaction, view.guild_id, view.user_id,
            self.currency_key, 0)


class ReceiptsBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Wallet", emoji="✦", row=2,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: ReceiptsView = self.view
        await render_hub(interaction, view.guild_id, view.user_id)


async def open_receipts_panel(interaction: discord.Interaction,
                              guild_id: int, user_id: int,
                              currency_key: str, page: int):
    total = await count_user_ledger(guild_id, user_id, currency=currency_key)
    pages = max(1, -(-total // RECEIPTS_PER_PAGE))
    page = max(0, min(page, pages - 1))

    entries = await get_user_ledger_page(
        guild_id, user_id, currency=currency_key,
        offset=page * RECEIPTS_PER_PAGE, limit=RECEIPTS_PER_PAGE)
    cur_cfg = await get_currency_config(guild_id)

    view = ReceiptsView(guild_id, user_id, currency_key, page, total)
    view.apply_currency_config(cur_cfg)

    await interaction.response.edit_message(
        embed=build_receipts_embed(
            currency_key, cur_cfg, entries, page, total),
        view=view)
    view.message = interaction.message


# ─── Cog ────────────────────────────────────────────────────────────────

class Wallet(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="wallet",
        description="Open your wallet — balances, streak, inventory and receipts")
    async def wallet(self, interaction: discord.Interaction):
        await render_hub(
            interaction, interaction.guild.id, interaction.user.id,
            first=True)

    @app_commands.command(
        name="streak",
        description="Open your daily streak panel")
    async def streak(self, interaction: discord.Interaction):
        # The /streak command opens the SAME panel the Wallet Streak
        # button opens — not a separate claim embed. Claiming happens
        # exclusively through the [Claim] button inside that panel, so
        # the command and the button share 100% of their logic.
        await open_streak_panel(
            interaction, interaction.guild.id, interaction.user.id,
            first=True)


async def setup(bot):
    await bot.add_cog(Wallet(bot))
