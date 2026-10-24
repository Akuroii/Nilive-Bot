from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

from utils.daily_engine import (
    DailyAlreadyClaimed, perform_streak_claim, get_streak_preview,
    get_streak_state, format_remaining,
)
from utils.economy_safe import get_balance
from utils.currency import (
    get_currency_config, for_currency,
    DEFAULT_COIN_EMOJI, DEFAULT_DIAMOND_EMOJI,
)
from utils.emoji import as_partial_emoji
from utils.inventory import (
    get_inventory, drop_item, find_entry,
    ROLE_ITEM_TYPES,
)
from utils.timed_ownership import CATEGORY_COLOR
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

# Inventory tabs. Roles/temp_roles are grouped under "Items" because
# from the member's side they are "things I own and can wear"; the
# split that matters is equippable vs consumable vs cosmetic label,
# not which table the role_id lives in.
TAB_ITEMS = "items"
TAB_COLORS = "colors"
TAB_POTIONS = "potions"
TAB_TITLES = "titles"

EQUIPPABLE_TYPES = ROLE_ITEM_TYPES

# Tab BUTTON labels use the owner's small-caps words; the author line
# above the list uses the full display words in TAB_AUTHOR_WORD.
TAB_META = {
    TAB_ITEMS:   {"label": "ɪᴛᴇᴍꜱ",   "emoji": "🎒"},
    # Colours are role-backed: permanent or timed by their Duration. There is
    # deliberately no separate "temporary" tab — temporariness is a Duration,
    # never a category of its own.
    TAB_COLORS:  {"label": "ᴄᴏʟᴏʀꜱ",  "emoji": "🎨"},
    TAB_POTIONS: {"label": "ᴘᴏᴛɪᴏɴꜱ", "emoji": "🧪"},
    TAB_TITLES:  {"label": "ᴛɪᴛʟᴇꜱ",  "emoji": "🏷️"},
}

# The tab strip, in render order (Colors sits right after Items).
TAB_ORDER = (TAB_ITEMS, TAB_COLORS, TAB_POTIONS, TAB_TITLES)

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
      0. The prepared Shop emoji resolved from the stored URL alias
         (`app_emoji`, filled by attach_prepared_emojis()). Read-only —
         this function never prepares anything.
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
    names = sorted({n for n in item_names if isinstance(n, str) and n})
    if not names:
        return {}
    marks = ",".join("?" for _ in names)
    try:
        import aiosqlite

        from database import DB_PATH

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT name, MIN(icon_url), COUNT(*) FROM shop_items "
                f"WHERE guild_id=? AND name IN ({marks}) GROUP BY name",
                (guild_id, *names))
            rows = await cursor.fetchall()
    except Exception:
        # No shop_items table / unreadable DB: behave exactly as before.
        return {}
    found = {}
    for name, icon_url, count in rows:
        url = (icon_url or "").strip()
        if count != 1 or not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        found[name] = url
    return found


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
    """Attach catalog metadata + prepared emoji, then sort rarity-first."""
    out = []
    for it in items:
        catalog = await get_catalog_entry(guild_id, it["item_name"])
        out.append({
            **it,
            "icon_url": catalog["icon_url"],
            "rarity": catalog["rarity"],
            "app_emoji": "",
            "_sort": item_sort_key(
                catalog["rarity"], catalog["value_currency"],
                catalog["value_amount"]),
        })
    # Catalog first; only the entries it cannot answer for fall back to
    # the (exact, unambiguous) Shop listing URL.
    missing = [it for it in out if not (it.get("icon_url") or "").strip()]
    if missing:
        fallback = await _shop_icon_urls(
            guild_id, [it["item_name"] for it in missing])
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


def is_color_entry(entry: dict) -> bool:
    """Does this ownership record belong to the Colors category?

    Answered ONLY from the record's own provenance (`category`, stamped at
    grant time from a Colors listing). An ordinary role reward — an event
    prize, a level claim, a raw `role_id` grant — is *not* a Color and stays in
    the Items tab, exactly as before. Nothing is reclassified retroactively.
    """
    return entry.get("category") == CATEGORY_COLOR


def split_by_tab(items: list[dict]) -> dict:
    colors = [it for it in items if is_color_entry(it)]
    return {
        TAB_COLORS: colors,
        # Items keeps every non-potion, non-title entry that is not a Color,
        # including role-backed ones: a role reward is still an item.
        TAB_ITEMS: [it for it in items
                    if it["item_type"] not in (POTION_ITEM_TYPE,
                                               TITLE_ITEM_TYPE)
                    and not is_color_entry(it)],
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
        if interaction.user.id != self.user_id:
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
#
# ONE template, three tabs. The embed carries exactly what the owner
# specified — author line, description lines, colour, footer — and
# nothing else: a title or extra fields would compete with the item list
# that IS the panel. Every value is live (bot account name, member
# account username, guild-configured currency names/icons, real
# inventory rows); none of it is a sample.
#
# Prepared Shop emoji are resolved READ-ONLY through
# utils.shop_emoji.attach_prepared_emojis(). Opening the panel, paging or
# switching tabs must never download an image, upload/delete an emoji or
# start a preparation pass. An unprepared icon is an ordinary state: fall
# back to a legacy configured custom emoji, then to the rarity glyph.
# A raw http(s) URL is never emitted as inline content.

INVENTORY_COLOR = 15000312
INVENTORY_PAGE_SIZE = 15
INVENTORY_AUTHOR_ICON = (
    "https://message.style/cdn/images/"
    "f21f9f7c3e914d9b9533f4399bfdfadf7447c85627034cbd00cd0e8d29df0d8f.png")
INVENTORY_FOOTER_TEXT = "-ˋˏ ༺❀ 𝙽𝚒𝚕𝚒𝚟𝚎 𝚃𝚎𝚊𝚖 ❀༻ˎˊ-"
INVENTORY_FOOTER_ICON = (
    "https://message.style/cdn/images/"
    "b5b0bf7153118e3c82fc0229a1548dbf5fefdf1691fc0a73b01f45b90ce44b06.gif")

# Author-line words are NOT the tab button labels: the buttons use the
# small-caps labels in TAB_META, the identity line uses the owner's
# supplied display words verbatim.
TAB_AUTHOR_WORD = {
    TAB_ITEMS: "𝐈𝐓𝐄𝐌𝐒",
    TAB_COLORS: "ᴄᴏʟᴏʀꜱ",
    TAB_POTIONS: "𝐩𝐨𝐭𝐢𝐨𝐧𝐬",
    TAB_TITLES: "𝐓𝐈𝐓𝐋𝐄𝐒",
}

TAB_SELECT_PLACEHOLDER = {
    TAB_ITEMS: "Select an item…",
    TAB_COLORS: "Select a color…",
    TAB_POTIONS: "Select a potion…",
    TAB_TITLES: "Select a title…",
}

TAB_EMPTY_TEXT = {
    TAB_ITEMS: "No items yet — get one from `/shop`.",
    TAB_COLORS: "No colors yet — get one from `/shop`.",
    TAB_POTIONS: "No potions yet — get one from `/shop`.",
    TAB_TITLES: "No titles yet — get one from `/shop`.",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value) -> datetime | None:
    """Parse a persisted ISO timestamp; None when it is absent/invalid.

    Mirrors the tolerance already used by the rest of the project (a
    trailing Z, a naive value treated as UTC, a space instead of a T).
    """
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _display_label(raw, limit: int = 48) -> str:
    """A safe, single-line label for backtick-wrapped embed text.

    Backticks are replaced because a member's item name must never be
    able to close the surrounding inline-code span and re-style the rest
    of the line. Length is capped so one pathological name cannot blow
    the 4096-char description budget (items are never dropped for it).
    """
    text = " ".join(str(raw or "").split()).replace("`", "'")
    if len(text) > limit:
        text = text[:max(1, limit - 1)].rstrip() + "…"
    return text or "—"


def _entry_emoji(entry: dict, fallback: str) -> str:
    """Inline glyph for one owned entry, in priority order.

    1. `app_emoji` — the prepared Shop emoji resolved from the stored
       alias by attach_prepared_emojis().
    2. a legacy custom emoji an admin typed straight into the catalog
       icon_url (`<:name:id>` / `<a:name:id>`).
    3. the caller's fallback (rarity glyph, or empty for Select options).
    """
    prepared = (entry.get("app_emoji") or "").strip()
    if prepared:
        return prepared
    legacy = (entry.get("icon_url") or "").strip()
    if legacy.startswith("<") and legacy.endswith(">"):
        return legacy
    return fallback


def _inventory_line(emoji: str, quantity, name, suffix: str | None = None) -> str:
    """`> - {emoji} • ` {quantity:,} ` • ` {name} `` — the exact template."""
    line = f"> - {emoji} • ` {int(quantity or 0):,} ` • ` {_display_label(name)} `"
    if suffix:
        line += f" • ` {suffix} `"
    return line


def remaining_hours_minutes(metadata) -> str | None:
    """`H:MM` remaining, from a persisted `expires_at` only.

    Accepts either the entry's metadata dict (the historical shape) or the
    bare ISO `expires_at` the unified inventory view exposes on timed
    holdings — both end at the same parsed instant.

    Never invents a duration, never returns a negative countdown: an
    absent, unparseable or already-expired timestamp yields None and the
    line simply has no time suffix.
    """
    if isinstance(metadata, str):
        expires = _parse_timestamp(metadata)
    else:
        expires = _parse_timestamp((metadata or {}).get("expires_at"))
    if expires is None:
        return None
    remaining = (expires - _utcnow()).total_seconds()
    if remaining <= 0:
        return None
    hours, minutes = divmod(int(remaining) // 60, 60)
    return f"{hours}:{minutes:02d}"


def _color_line(emoji: str, entry: dict) -> str:
    """One Colors row: the role is rendered as a real mention.

    `<@&id>` only becomes a pill when it is NOT wrapped in backticks, so the
    name column is the mention itself instead of `_inventory_line`'s
    backticked text. A permanent Color has no suffix; a timed one shows the
    remaining `H:MM`, read from the record rather than recomputed.
    """
    role_id = entry.get("role_id") or (entry.get("metadata") or {}).get("role_id")
    mention = f"<@&{int(role_id)}>" if role_id else _display_label(
        entry.get("item_name"))
    line = f"> - {emoji} • ` {int(entry.get('quantity') or 0):,} ` • {mention}"
    remaining = remaining_hours_minutes(entry.get("expires_at")
                                        or (entry.get("metadata") or {}).get("expires_at"))
    if remaining:
        line += f" • ` {remaining} `"
    return line


def paginate_entries(entries: list[dict],
                     page: int = 0) -> tuple[list[dict], int, int]:
    """Slice one page out of the tab's entries.

    Returns (page_entries, clamped_page, total_pages). Clamping is what
    keeps an action that empties the last page from stranding the member
    on an empty, arrow-less panel.
    """
    entries = list(entries or [])
    pages = max(1, -(-len(entries) // INVENTORY_PAGE_SIZE))
    page = max(0, min(int(page or 0), pages - 1))
    start = page * INVENTORY_PAGE_SIZE
    return entries[start:start + INVENTORY_PAGE_SIZE], page, pages


async def currency_balance_rows(guild_id: int, user_id: int) -> list[dict]:
    """Real configured currency balances, read through the economy API.

    Names/icons come from the guild's currency configuration (never a
    hardcoded label) and the amount from get_balance() (never an
    inventory_items row — a balance is not an owned item).
    """
    currency = await get_currency_config(guild_id)
    defaults = {"coins": DEFAULT_COIN_EMOJI, "diamonds": DEFAULT_DIAMOND_EMOJI}
    rows = []
    for key in ("coins", "diamonds"):
        info = currency[key]
        emoji = (info["emoji"] or "").strip()
        # A URL is never valid inline content; the module default is the
        # honest glyph for a currency whose icon is misconfigured.
        if emoji.lower().startswith(("http://", "https://")):
            emoji = defaults[key]
        rows.append({
            "emoji": emoji,
            "name": info["name"],
            "quantity": await get_balance(
                guild_id, user_id, currency=info["key"]),
        })
    return rows


def build_inventory_embed(tab: str, entries: list[dict], *,
                          page: int = 0,
                          currency_rows: list[dict] | None = None,
                          bot_name: str = "",
                          username: str = "") -> discord.Embed:
    page_entries, page, _pages = paginate_entries(entries, page)
    embed = discord.Embed(color=INVENTORY_COLOR)
    embed.set_author(
        name=f"{bot_name}: {TAB_AUTHOR_WORD[tab]} {username}",
        icon_url=INVENTORY_AUTHOR_ICON)

    lines = []
    # Currency balances belong to the Items view only, are never
    # selectable, and never count toward INVENTORY_PAGE_SIZE.
    if tab == TAB_ITEMS:
        for row in currency_rows or []:
            lines.append(_inventory_line(
                row["emoji"], row["quantity"], row["name"]))
    for entry in page_entries:
        emoji = _entry_emoji(entry, _rarity_glyph(entry.get("rarity")))
        if tab == TAB_COLORS:
            lines.append(_color_line(emoji, entry))
            continue
        # The remaining time comes from the entry's own expiry field, so a
        # permanent item (no expiry) never shows a countdown and a timed one
        # shows exactly the deadline the cleanup loop will honour.
        suffix = remaining_hours_minutes(entry.get("expires_at")
                                         or (entry.get("metadata") or {}).get("expires_at"))
        lines.append(_inventory_line(
            emoji, entry.get("quantity"), entry.get("item_name"), suffix))

    embed.description = "\n".join(lines) if lines else TAB_EMPTY_TEXT[tab]
    embed.set_footer(text=INVENTORY_FOOTER_TEXT,
                     icon_url=INVENTORY_FOOTER_ICON)
    return embed


def _inventory_identity(interaction: discord.Interaction) -> tuple[str, str]:
    """(actual bot account name, member's actual account username)."""
    client_user = getattr(interaction.client, "user", None)
    bot_name = (getattr(client_user, "name", None)
                or getattr(client_user, "display_name", None) or "")
    user = interaction.user
    username = (getattr(user, "name", None)
                or getattr(user, "display_name", None) or "unknown")
    return bot_name, username


def _select_option_value(name: str, used: set[str]) -> str:
    """A unique, Discord-legal Select value that still resolves to `name`.

    Long or colliding names must not silently share a value — the select
    maps its value back to the real item_name, so a truncated collision
    would open the wrong item.
    """
    base = str(name)[:100]
    candidate = base
    counter = 1
    while candidate in used:
        suffix = f"#{counter}"
        candidate = base[:100 - len(suffix)] + suffix
        counter += 1
    used.add(candidate)
    return candidate


class InventoryItemSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 entries: list[dict], page: int = 0):
        options = []
        used = set()
        for entry in entries:
            name = entry["item_name"]
            options.append(discord.SelectOption(
                label=_display_label(name, 100),
                description=f"×{int(entry.get('quantity') or 0):,}"[:100],
                value=_select_option_value(name, used),
                emoji=as_partial_emoji(_entry_emoji(entry, ""))))
        super().__init__(
            placeholder=TAB_SELECT_PLACEHOLDER[tab],
            options=options or [discord.SelectOption(label="—", value="—")],
            disabled=not options,
            row=1)
        self.guild_id = guild_id
        self.user_id = user_id
        self.tab = tab
        self.page = page
        # value -> entry_key. A member can own a permanent "Flame" and two
        # timed "Flame" holdings at once; the label disambiguates them for
        # reading, and the entry_key — not the name — says which one the
        # member actually picked.
        self._by_value = {opt.value: entry.get("entry_key")
                          for opt, entry in zip(options, entries)}

    async def callback(self, interaction: discord.Interaction):
        # Selecting is navigation ONLY — it never equips, drops,
        # activates or consumes anything.
        await open_item_panel(
            interaction, self.guild_id, self.user_id,
            self.tab, page=self.page,
            entry_key=self._by_value.get(self.values[0]))


class InventoryTabButton(discord.ui.Button):
    def __init__(self, tab: str, active: bool):
        meta = TAB_META[tab]
        super().__init__(
            label=meta["label"], emoji=meta["emoji"], row=0,
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            disabled=active)
        self.tab = tab

    async def callback(self, interaction: discord.Interaction):
        view: "InventoryView" = self.view
        # Switching tabs starts that tab at page one.
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id, self.tab, 0)


class InventoryPageButton(discord.ui.Button):
    """`◀`/`▶` — created only when that direction actually exists."""

    def __init__(self, direction: int):
        self.direction = direction
        super().__init__(
            emoji="◀" if direction < 0 else "▶",
            style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: "InventoryView" = self.view
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id,
            view.tab, view.page + self.direction)


class InventoryBackButton(discord.ui.Button):
    """Back to Wallet — deliberately distinct from the page arrows."""

    def __init__(self):
        super().__init__(label="Back to Wallet", emoji="✦", row=3,
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: "InventoryView" = self.view
        await render_hub(interaction, view.guild_id, view.user_id)


class InventoryView(WalletBaseView):
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 entries: list[dict], page: int = 0, pages: int = 1):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.page = page
        self.pages = pages
        # Row 0: tabs. Row 1: select. Row 2: arrows (when usable).
        # Row 3: Back to Wallet.
        for key in TAB_ORDER:
            self.add_item(InventoryTabButton(key, active=(key == tab)))
        self.add_item(InventoryItemSelect(guild_id, user_id, tab, entries, page))
        if page > 0:
            self.add_item(InventoryPageButton(-1))
        if page < pages - 1:
            self.add_item(InventoryPageButton(1))
        self.add_item(InventoryBackButton())


async def open_inventory_panel(interaction: discord.Interaction,
                               guild_id: int, user_id: int, tab: str,
                               page: int = 0, *, first: bool = False):
    """The ONE inventory renderer.

    Used by the Wallet Inventory button (first=False → edits the current
    ephemeral message), by `/inventory` (first=True → sends a fresh
    ephemeral message) and by every tab/page/detail navigation. There is
    no second copy of the template or of the interaction logic.
    """
    if tab not in TAB_META:
        tab = TAB_ITEMS
    await cleanup_unowned_title(guild_id, user_id)

    items = await get_inventory(guild_id, user_id, include_empty=False)
    entries = await _decorate(guild_id, split_by_tab(items)[tab])
    page_entries, page, pages = paginate_entries(entries, page)

    currency_rows = (await currency_balance_rows(guild_id, user_id)
                     if tab == TAB_ITEMS else [])
    bot_name, username = _inventory_identity(interaction)

    embed = build_inventory_embed(
        tab, entries, page=page, currency_rows=currency_rows,
        bot_name=bot_name, username=username)
    view = InventoryView(guild_id, user_id, tab, page_entries, page, pages)

    if first:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)
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

    # A role item's Discord role is shown as a mention so the member
    # can see what wearing it grants — but the DB row is the source
    # of truth, not the role itself. No "Expires" / "Paid" / "Bought"
    # per spec — that data is admin-side and clutters the member view.
    if tab in (TAB_ITEMS, TAB_COLORS) and item["item_type"] in EQUIPPABLE_TYPES:
        role_id = (item.get("metadata") or {}).get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        embed.add_field(
            name="Role",
            value=(role.mention if role else "*missing — ask an admin*"),
            inline=True)

    # Remaining time is a property of the entry, so it is shown for any tab
    # that can hold a timed entry (Colors and Items). A permanent entry has no
    # expires_at and therefore no countdown.
    remaining = remaining_hours_minutes(item.get("expires_at")
                                        or (item.get("metadata") or {}).get("expires_at"))
    if remaining:
        embed.add_field(name="Time remaining", value=f"**{remaining}**",
                        inline=True)

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
      * All items   -> Drop (with confirmation) and Back
    """

    def __init__(self, guild_id: int, user_id: int, tab: str,
                 item: dict, equipped: bool, page: int = 0):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.item_name = item["item_name"]
        self.item_type = item["item_type"]
        # Which entry this panel is about. A name is not an identity: with a
        # permanent and a timed copy owned at once, every action below must
        # target the entry the member actually opened.
        self.entry_key = item.get("entry_key")
        self.timed = bool(item.get("timed"))
        # The page the member came from, so Back returns to where they
        # were instead of dumping them on page one.
        self.page = page

        if tab == TAB_POTIONS:
            self.add_item(ActivateButton(
                enabled=potion_is_usable(item.get("metadata"))))
        else:
            self.add_item(EquipToggleButton(equipped))

        # Dropping is permanent-row only. A timed holding is retired by its
        # own expiry (which also has to strip the Discord role and clear the
        # equipped slot), so offering "Drop" here would either leave a role
        # behind or silently promise something this button cannot do.
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
            view.guild_id, view.user_id, view.item_name)
        if not result.get("success"):
            await interaction.response.send_message(
                f"Could not activate that potion — {result.get('error', 'something went wrong.')}",
                ephemeral=True)
            return
        if result["remaining"] <= 0:
            # The stack is gone: refresh the list (the page is clamped
            # to the last valid one inside open_inventory_panel).
            await open_inventory_panel(
                interaction, view.guild_id, view.user_id, TAB_POTIONS,
                view.page)
        else:
            await open_item_panel(
                interaction, view.guild_id, view.user_id,
                TAB_POTIONS, view.item_name, view.page)


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
        if view.tab == TAB_TITLES:
            if self.equipped:
                result = await unequip_title(
                    view.guild_id, view.user_id, view.item_name)
            else:
                result = await equip_title(
                    view.guild_id, view.user_id, view.item_name)
        elif view.item_type in EQUIPPABLE_TYPES:
            if self.equipped:
                result = await unequip_role(
                    interaction.client, view.guild_id, view.user_id,
                    view.item_name, entry_key=view.entry_key)
            else:
                result = await equip_role(
                    interaction.client, view.guild_id, view.user_id,
                    view.item_name, entry_key=view.entry_key)
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
            view.tab, page=view.page, entry_key=view.entry_key)


class DropButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Drop", emoji="🗑️",
                         style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        await open_drop_confirm(
            interaction, view.guild_id, view.user_id,
            view.tab, view.item_name, view.page, entry_key=view.entry_key)


class ItemBackButton(discord.ui.Button):
    def __init__(self):
        # Not a paging arrow: this returns to the Inventory list the
        # member came from, keeping that tab and page.
        super().__init__(label="Back to Inventory", emoji="↩",
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: ItemPanelView = self.view
        await open_inventory_panel(
            interaction, view.guild_id, view.user_id, view.tab, view.page)


# ─── Drop confirmation ─────────────────────────────────────────────────

def build_drop_confirm_embed(tab: str, item_name: str,
                             item: dict) -> discord.Embed:
    glyph = _rarity_glyph(item["rarity"])
    emoji = _item_display(item, glyph)
    thumb = _item_thumbnail_url(item)
    embed = discord.Embed(
        title=f"{emoji} {item_name}",
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
    def __init__(self, guild_id: int, user_id: int, tab: str,
                 item_name: str, page: int = 0):
        super().__init__(guild_id, user_id)
        self.tab = tab
        self.item_name = item_name
        self.page = page

    @discord.ui.button(label="Confirm", emoji="🗑️",
                       style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        # Unequip first if needed, then delete the stack. Order
        # matters: equip/title cleanup looks at inventory to decide
        # what to clear; drop_item removes the row, so we resolve the
        # current equipped state up front.
        if self.tab == TAB_TITLES:
            current = await get_equipped_title(self.guild_id, self.user_id)
            if current and current["item_name"] == self.item_name:
                await unequip_title(self.guild_id, self.user_id,
                                    self.item_name)
        elif self.tab in (TAB_ITEMS, TAB_COLORS):
            current = await get_equipped(self.guild_id, self.user_id)
            if current and current["item_name"] == self.item_name:
                await unequip_role(interaction.client, self.guild_id,
                                   self.user_id, self.item_name)

        result = await drop_item(self.guild_id, self.user_id, self.item_name)
        if not result.get("success"):
            await interaction.response.send_message(
                f"Could not drop that item — {result.get('error', 'something went wrong.')}",
                ephemeral=True)
            return
        await open_inventory_panel(
            interaction, self.guild_id, self.user_id, self.tab, self.page)

    @discord.ui.button(label="Cancel", emoji="✖",
                       style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        await open_item_panel(
            interaction, self.guild_id, self.user_id,
            self.tab, self.item_name, self.page)


async def open_drop_confirm(interaction: discord.Interaction,
                            guild_id: int, user_id: int,
                            tab: str, item_name: str, page: int = 0,
                            *, entry_key: str | None = None):
    items = await get_inventory(guild_id, user_id, include_empty=False)
    entries = await _decorate(guild_id, split_by_tab(items)[tab])
    item = find_entry(entries, entry_key=entry_key, item_name=item_name)
    if not item:
        await open_inventory_panel(interaction, guild_id, user_id, tab, page)
        return
    view = DropConfirmView(guild_id, user_id, tab, item_name, page)
    await interaction.response.edit_message(
        embed=build_drop_confirm_embed(tab, item_name, item), view=view)
    view.message = interaction.message


async def open_item_panel(interaction: discord.Interaction, guild_id: int,
                          user_id: int, tab: str, item_name: str | None = None,
                          page: int = 0, *, entry_key: str | None = None):
    items = await get_inventory(guild_id, user_id, include_empty=False)
    entries = await _decorate(guild_id, split_by_tab(items)[tab])
    # `find_entry` refuses to guess: an ambiguous name (or a stale entry_key
    # from a panel the holding has since expired out of) resolves to nothing
    # and the member is dropped back to the list instead of onto another
    # record.
    item = find_entry(entries, entry_key=entry_key, item_name=item_name)

    if not item:
        await open_inventory_panel(interaction, guild_id, user_id, tab, page)
        return

    if tab == TAB_TITLES:
        current = await get_equipped_title(guild_id, user_id)
        equipped = bool(current and current["item_name"] == item_name)
    elif item["item_type"] in EQUIPPABLE_TYPES:
        current = await get_equipped(guild_id, user_id)
        # entry_key decides when both sides have one: an equipped permanent
        # "Flame" does not make a timed "Flame" the equipped entry. The name
        # comparison stays for legacy equipped_roles rows (entry_key NULL).
        if current and current.get("entry_key") and item.get("entry_key"):
            equipped = current["entry_key"] == item["entry_key"]
        else:
            equipped = bool(current and current["item_name"] == item_name)
    else:
        equipped = False

    view = ItemPanelView(guild_id, user_id, tab, item, equipped, page)
    await interaction.response.edit_message(
        embed=build_item_embed(interaction.guild, tab, item, equipped),
        view=view)
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
