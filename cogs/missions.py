import math
from typing import Optional, Union

import discord
from discord.ext import commands
from discord import app_commands

from utils.mission_engine import (
    ensure_tables, get_definitions, get_user_progress,
    record_activity, record_activities,
    create_definition, delete_definition,
    seconds_until_daily_reset, seconds_until_weekly_reset,
    format_reset_countdown,
)

# ═══════════════════════════════════════════════════════════════════════
# MISSIONS — v2 display
#
# /missions renders one ephemeral message containing a separate embed
# per period that has missions (Daily / Weekly / One-time), each with:
#   * a dynamic "(completed / total)" heading for THIS member
#   * one block per mission: name, -# description line, glyph progress
#     bar + percentage, then either `Progress: x / y unit` or — because
#     rewards are granted automatically the instant the target is
#     crossed, with no claim step — `⤷ Reward claimed 💎`
#   * the period's next-rotation countdown, computed at render time
#     from the same UTC period math the engine keys progress by (there
#     is no timer job to duplicate — the reset is the period_key
#     rollover, this just reports it)
#
# The Refresh button re-renders that same message in place
# (interaction.response.edit_message — the Wallet panel pattern), so
# refreshing never spawns duplicate mission messages.
# ═══════════════════════════════════════════════════════════════════════

TYPE_LABEL = {
    "messages": "Messages sent",
    "words": "Words typed",
    "voice_minutes": "Minutes in voice",
    "daily_completions": "Daily missions completed",
}
# Unit shown on the member-facing progress line, per type.
UNIT_LABEL = {
    "messages": "messages",
    "words": "words",
    "voice_minutes": "min",
    "daily_completions": "missions",
}
PERIOD_LABEL = {"daily": "Daily", "weekly": "Weekly", "once": "One-time"}
PERIOD_HEADING = {
    "daily": "Daily Mission Progress",
    "weekly": "Weekly Mission Progress",
    "once": "One-time Missions",
}
PERIOD_FOOTER = {
    "once": "No reset — completes once, forever",
}

MISSION_COLOR = 0x7c5cbf
VIEW_TIMEOUT = 1800  # same as the Wallet panels

# Single source of truth for the animated reward emoji (locked spec).
# The emoji itself is uploaded through the Discord Developer Portal; if
# it's ever re-uploaded, only this constant changes.
DIAMOND_EMOJI = "<a:diamond:1532745018324815982>"
CHECKMARK_EMOJI = "✅"

# Refresh button emoji — the server's custom emoji (locked by Dark).
# Static server emoji, so the plain <:name:id> form. Same
# single-source-of-truth rule as DIAMOND_EMOJI above: if the emoji is
# ever re-uploaded, only this constant changes. Note: this is a GUILD
# emoji — if it's ever deleted from the server, the button silently
# falls back to no glyph (still fully functional); the label carries
# the meaning, the emoji is decoration.
REFRESH_EMOJI = "<:imagePhotoroom17:1549206183498481714>"

PROGRESS_NODES = 6      # v3: 6 nodes — a full bar means completed
NODE_FILLED = "⬤"
NODE_EMPTY = "◯"
NODE_JOIN = "──"
EMBED_DESC_LIMIT = 3900  # safety margin under Discord's 4096 cap


def progress_bar(pct: float, nodes: int = PROGRESS_NODES) -> str:
    """Glyph bar — a node lights up once progress crosses into its
    share of the bar (62% of 6 nodes = 3.72 → 4 lit, 20% → 2 lit, so a
    bar never reads as emptier than the mission actually is). Pure
    percentage→glyph mapping; the CALLER decides what percentage to
    feed it — _mission_block reserves a completely full bar for the
    completed state, so a full bar always means 'reward claimed'."""
    pct = max(0.0, min(100.0, pct))
    # Tiny epsilon avoids floating error where 83.333...*6/100=5.000000000000001 ceil->6
    filled = math.ceil(pct / 100 * nodes - 1e-9) if pct > 0 else 0
    filled = max(0, min(nodes, filled))
    return NODE_JOIN.join([NODE_FILLED] * filled + [NODE_EMPTY] * (nodes - filled))


# An incomplete mission's bar is capped one node short of full: a
# completely filled bar reads as "done", and done means the reward was
# already granted — 95% must not look identical to 100%.
_INCOMPLETE_BAR_CAP = (PROGRESS_NODES - 1) / PROGRESS_NODES * 100


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _auto_description(m: dict) -> str:
    t, target = m["type"], m["target"]
    if t == "words":
        return f"Write at least {_plural(target, 'word')}"
    if t == "messages":
        return f"Send {_plural(target, 'message')}"
    if t == "voice_minutes":
        return f"Spend {_plural(target, 'minute')} in voice"
    return f"Complete {_plural(target, 'daily mission')}"


def _mission_description(m: dict, guild) -> str:
    """The `-#` subtext line. Channel restrictions are enforced
    internally but never surfaced to members — no 'counts in #channel'
    text is ever appended."""
    desc = (m.get("description") or "").strip() or _auto_description(m)
    desc = desc.rstrip(".")
    return desc + "."


def _reward_display_sync(m: dict, cur: dict | None) -> str:
    """Format the actual reward for the completed line, using currency source of truth."""
    rt = m.get("reward_type")
    rv = m.get("reward_value")
    if rt == "coins":
        emoji = (cur["coins"]["emoji"] if cur and "coins" in cur else "🪙")
        return f"{rv} {emoji}"
    if rt == "diamonds":
        emoji = (cur["diamonds"]["emoji"] if cur and "diamonds" in cur else "💎")
        return f"{rv} {emoji}"
    if rt == "xp":
        return f"{rv} XP"
    if rt in ("role", "temp_role"):
        # Role IDs are rendered as mentions when possible
        try:
            return f"<@&{int(rv)}>"
        except Exception:
            return str(rv)
    if rt == "item":
        return str(rv)
    return str(rv) if rv is not None else ""


def _mission_block(m: dict, guild, cur: dict | None = None) -> str:
    target = int(m["target"] or 0)
    progress = min(int(m["progress"] or 0), target)
    pct = (progress / target * 100) if target > 0 else 100.0
    if m["completed"]:
        pct_display = 100
        bar = progress_bar(100)
        reward_str = _reward_display_sync(m, cur)
        # Dynamic currency, checkmark; no hardcoded DIAMOND for non-diamond rewards
        status = f"⤷ `reward claimed` {reward_str} {CHECKMARK_EMOJI}".strip()
    else:
        # Never claim 100% before the mission is actually complete
        # (e.g. 199/200 rounding up) — and never RENDER a full bar
        # either: both the number and the glyphs come from the same
        # progress/target value, capped just short of full.
        # v3: 6 nodes, incomplete max 5 filled. Cap by nodes, not just pct, to avoid float edge.
        pct_display = min(99, round(pct))
        # Compute filled then cap to PROGRESS_NODES-1 for incomplete
        raw_filled = math.ceil(pct / 100 * PROGRESS_NODES - 1e-9) if pct > 0 else 0
        capped_filled = min(raw_filled, PROGRESS_NODES - 1)
        bar = NODE_JOIN.join([NODE_FILLED] * capped_filled + [NODE_EMPTY] * (PROGRESS_NODES - capped_filled))
        unit = UNIT_LABEL.get(m["type"], "points")
        status = f"`Progress: {progress} / {target} {unit}`"
    return (
        f"**{m['name']}**\n"
        f"-# {_mission_description(m, guild)}\n"
        f"{bar}⁀જ➣ **`{pct_display}%`**ˎˊ˗\n"
        f"{status}"
    )
def _bot_display_name(bot, guild) -> str:
    """The bot's name as this server sees it — the per-guild nickname
    when one is configured (Bot Profile system), else the global
    display name. Never a hardcoded product name."""
    me = getattr(guild, "me", None)
    if me is not None:
        return me.display_name
    user = getattr(bot, "user", None)
    return user.display_name if user else "Missions"


async def build_mission_display(bot, guild, user_id: int):
    """
    Builds the whole /missions surface: (content, embeds). Returns
    None when the guild has no enabled missions at all (the caller
    sends the 'nothing configured' reply instead). One embed per
    period that actually has missions — no empty embeds — and each
    embed's heading counts THIS member's completions over the guild's
    enabled missions for that period.
    """
    progress = await get_user_progress(guild.id, user_id)
    if not progress:
        return None

    # Currency source of truth for completed-reward display
    cur = None
    try:
        from utils.currency import get_currency_config
        cur = await get_currency_config(guild.id)
    except Exception:
        cur = None

    sections: dict[str, list] = {}
    for m in progress:
        sections.setdefault(m["period"], []).append(m)

    embeds = []
    for period in ("daily", "weekly", "once"):
        missions = sections.get(period) or []
        if not missions:
            continue
        done = sum(1 for m in missions if m["completed"])
        blocks = [_mission_block(m, guild, cur) for m in missions]
        if period == "daily":
            blocks.append(f"**Next mission:** "
                          f"`{format_reset_countdown(seconds_until_daily_reset())}`")
        elif period == "weekly":
            blocks.append(f"**Next mission:** "
                          f"`{format_reset_countdown(seconds_until_weekly_reset())}`")

        # Defensive 4096 split: a guild with dozens of missions in one
        # period flows into "(part n)" continuation embeds rather than
        # an HTTP 400 from Discord. A single pathological block (only
        # possible from a legacy row created before length validation —
        # create_definition now caps name/description) is hard-cut at
        # the limit so no input can ever produce an oversized embed.
        chunks, buf, size = [], [], 0
        for block in blocks:
            if len(block) > EMBED_DESC_LIMIT:
                block = block[:EMBED_DESC_LIMIT - 1] + "…"
            if buf and size + len(block) + 2 > EMBED_DESC_LIMIT:
                chunks.append("\n\n".join(buf))
                buf, size = [], 0
            buf.append(block)
            size += len(block) + 2
        if buf:
            chunks.append("\n\n".join(buf))

        for i, chunk in enumerate(chunks):
            title = f"{PERIOD_HEADING.get(period, period.title())} ({done} / {len(missions)})"
            if len(chunks) > 1:
                title += f" — part {i + 1}"
            embed = discord.Embed(title=title, description=chunk,
                                  color=MISSION_COLOR)
            footer = PERIOD_FOOTER.get(period)
            if footer:
                embed.set_footer(text=footer)
            embeds.append(embed)

    content = f"### {_bot_display_name(bot, guild)} Missions"
    return content, embeds


def _effective_channel_id(channel) -> Optional[int]:
    """
    The channel a message counts toward. A message sent inside a
    thread belongs to the thread's parent channel (a forum post's
    parent is the forum channel itself), so a mission restricted to
    #writing also counts activity in #writing's threads. An orphaned
    thread (parent deleted) resolves to None: unrestricted missions
    still count it, restricted ones correctly don't.
    """
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    return getattr(channel, "id", None)


class MissionsView(discord.ui.View):
    """
    Ephemeral mission panel with an in-place Refresh button — the same
    interaction contract as the Wallet panels (owner-checked, one
    message, edited on click, controls disabled on timeout). Refresh
    re-renders the SAME message; it never sends a second message while
    the first one still exists. The only exception is recovery: if the
    original panel was deleted (channel purge, message remove), a fresh
    ephemeral response replaces the now-unreachable one — see the
    Refresh callback below.
    """

    def __init__(self, bot, guild_id: int, user_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This isn't your mission panel — run `/missions` to see your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.refresh_button.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                # Ephemeral messages expire on their own; a failed edit
                # here is cosmetic and must never raise into the loop.
                pass

    @discord.ui.button(label="ʀᴇꜰʀᴇꜱʜ", emoji=REFRESH_EMOJI,
                       style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        guild = self.bot.get_guild(self.guild_id)
        display = (await build_mission_display(self.bot, guild, self.user_id)
                   if guild else None)
        if display is None:
            content = "No missions are currently configured."
            embeds, view = [], None
        else:
            content, embeds = display
            view = self
        try:
            await interaction.response.edit_message(
                content=content, embeds=embeds, view=view)
        except discord.HTTPException:
            # Stale/deleted surface: the original panel message was
            # purged, or this click arrived on an interaction token
            # Discord no longer accepts. Editing in place is the normal
            # path; when the old message no longer exists a fresh
            # ephemeral response is the recovery — the previous panel
            # is unreachable, so this is a replacement, not a
            # duplicate. If even the followup fails (token fully
            # expired), there is nothing left to do: swallow, never
            # raise into the component dispatch.
            try:
                await interaction.followup.send(
                    content=content, embeds=embeds, view=view,
                    ephemeral=True)
            except discord.HTTPException:
                pass


class Missions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await ensure_tables()

    # ─── PROGRESS HOOKS (same events cogs/mvp.py and cogs/leveling.py
    # already listen to — no new tracking wired anywhere else) ──────
    @commands.Cog.listener()
    async def on_activity_message(self, message: discord.Message,
                                   word_count: int):
        if message.author.bot or not message.guild:
            return
        # messages + words share one definitions query and one SQLite
        # connection (record_activities batches both counters from this
        # single event; a 0 word count is filtered out inside).
        await record_activities(
            self.bot, message.guild.id, message.author.id,
            {"messages": 1, "words": word_count},
            channel_id=_effective_channel_id(message.channel),
        )

    @commands.Cog.listener()
    async def on_activity_voice_tick(self, guild: discord.Guild,
                                      member: discord.Member, flags: dict):
        try:
            await record_activity(
                self.bot, guild.id, member.id, "voice_minutes", 1,
                channel_id=flags.get("channel_id"))
        except Exception as e:
            print(f"[MISSIONS] voice tick error for member {member.id} "
                  f"in guild {guild.id}: {e}")

    # ─── SLASH COMMANDS ──────────────────────────────────
    @app_commands.command(name="missions",
                          description="View your active missions and progress")
    @app_commands.guild_only()
    async def missions(self, interaction: discord.Interaction):
        display = await build_mission_display(
            self.bot, interaction.guild, interaction.user.id)
        if display is None:
            await interaction.response.send_message(
                "No missions configured yet — an admin can add some via the dashboard "
                "or /mission_create.", ephemeral=True)
            return
        content, embeds = display
        view = MissionsView(self.bot, interaction.guild.id,
                            interaction.user.id)
        await interaction.response.send_message(
            content=content, embeds=embeds, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="mission_create",
                          description="Create a mission (admin)")
    @app_commands.describe(
        name="Display name",
        type="messages / words / voice_minutes / daily_completions",
        target="How much progress is needed to complete it",
        period="daily / weekly / once",
        reward_type="coins / diamonds / xp / role / temp_role / item",
        reward_value="Amount, Role ID, or item name",
        duration_hours="Only used for temp_role",
        channel="Only count activity in this channel (optional)",
        description="Shown to members in /missions (optional)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def mission_create(self, interaction: discord.Interaction,
                             name: str, type: str, target: int,
                             reward_type: str, reward_value: str,
                             period: str = "daily",
                             description: str = None,
                             duration_hours: int = None,
                             channel: Union[discord.TextChannel,
                                            discord.VoiceChannel,
                                            discord.StageChannel,
                                            discord.ForumChannel] = None):
        mtype = (type or "").lower().strip()
        try:
            await create_definition(
                interaction.guild.id, name=name, mtype=mtype, target=target,
                period=period, reward_type=reward_type,
                reward_value=reward_value, description=description,
                reward_duration_hours=duration_hours,
                channel_id=channel.id if channel else None)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        where = (f" in {channel.mention}"
                 if channel and mtype != "daily_completions" else "")
        await interaction.response.send_message(
            f"✅ Created mission **{name}** — {target} "
            f"{TYPE_LABEL.get(mtype, mtype)} "
            f"({PERIOD_LABEL.get(period, period)}){where} → "
            f"{reward_type}: {reward_value}", ephemeral=True)

    @app_commands.command(name="mission_list",
                          description="List configured missions (admin)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def mission_list(self, interaction: discord.Interaction):
        defs = await get_definitions(interaction.guild.id, enabled_only=False)
        if not defs:
            await interaction.response.send_message(
                "No missions configured yet.", ephemeral=True)
            return
        # Discord caps an embed at 25 fields — a guild running many
        # daily+weekly missions would crash the command at mission #26.
        # Page into multiple embeds of 20 (same shape the member
        # display's "(part n)" split already uses).
        embeds = []
        pages = (len(defs) + 19) // 20
        for i in range(0, len(defs), 20):
            page = defs[i:i + 20]
            title = "🗺️ Configured Missions"
            if pages > 1:
                title += f" ({i // 20 + 1}/{pages})"
            embed = discord.Embed(title=title, color=MISSION_COLOR)
            for d in page:
                status = "✅" if d["enabled"] else "❌"
                where = f" · <#{d['channel_id']}>" if d.get("channel_id") else ""
                embed.add_field(
                    name=f"#{d['id']} {status} {d['name']}",
                    value=(f"{d['target']} {TYPE_LABEL.get(d['type'], d['type'])} "
                           f"({PERIOD_LABEL.get(d['period'], d['period'])}){where} → "
                           f"{d['reward_type']}: {d['reward_value']}"),
                    inline=False)
            embeds.append(embed)
        await interaction.response.send_message(
            embeds=embeds, ephemeral=True)

    @app_commands.command(name="mission_remove",
                          description="Remove a mission by ID (admin)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def mission_remove(self, interaction: discord.Interaction,
                             mission_id: int):
        found = await delete_definition(interaction.guild.id, mission_id)
        if found:
            await interaction.response.send_message(
                f"Removed mission #{mission_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"No mission #{mission_id} found.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Missions(bot))
