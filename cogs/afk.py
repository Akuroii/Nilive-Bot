"""AFK — mark yourself away with an optional reason.

One prefix command, ``!afk [سبب]``. Because the bot is created with
``case_insensitive=True`` (see main.py), ``!afk``, ``!AFK`` and ``!Afk`` all
resolve to the SAME command through discord.py's own resolver — no aliases
table is needed, and adding mixed-case aliases manually would in fact raise
``CommandRegistrationError`` (a case variant already counts as the command
itself).

Behaviour
---------
* ``!afk``            -> go AFK with no reason.
* ``!afk <reason>``   -> go AFK with a reason (any number of spaces allowed;
                         the trailing ``*reason`` parameter captures it all).

While AFK:
* a REAL Discord mention of the member (``message.mentions`` — never a plain
  text username search) makes the bot reply in that same channel that the
  member is AFK, with the reason when one exists;
* every *distinct message* that mentions the member increments the counter —
  one message counts at most once per target, so re-delivery / double event
  processing can never double-count (dedup key is ``(guild, message, target)``);
* the member's own next message in ANY channel (not tied to where ``!afk``
  was used, not tied to a follow-up command) cancels AFK, and the bot reports
  how many mentions arrived while they were away.

Re-running ``!afk`` while already AFK is *idempotent*: nothing changes —
not the reason, not ``started_at``, not the mention counter, no new session —
and the bot simply replies that the member is already away (showing the
current reason when one exists).

Cancellation rules
------------------
* bot messages never cancel AFK (``message.author.bot`` is ignored);
* the ``!afk`` invocation itself never cancels AFK — the command callback and
  this cog's ``on_message`` listener run as sibling tasks, so the listener
  checks ``bot.get_context`` and stands down for the afk command instead of
  racing the write (same resolver the router uses, no content rewriting);
* self-mentions never produce an AFK alert (and never loop).

Persistence
-----------
Guild-scoped state in SQLite (the same database every other cog uses):
``afk_state`` (one row per guild+member, with ``reason``, ``started_at`` and
``mention_count``) and ``afk_mentions`` (dedup ledger of counted messages,
deleted when AFK ends). All of it survives a bot restart.

Dashboard & mentions button (AFK dashboard/button pass, 2026-09-23)
-------------------------------------------------------------------
* The Dashboard Commands page lists ``afk`` under ``Utility & Trade`` as a
  *prefix* command (usage ``!afk [reason]``); the alias-editing UI is hidden
  for it, because the current alias bridge only targets slash commands.
* The dashboard toggle row for ``afk`` gates ONLY the start of NEW sessions —
  an owner who is already AFK can still cancel normally, mention alerts keep
  firing, and the button keeps answering while the feature is disabled.
* Every activation card carries a persistent ``AFKMentionsView`` button
  (always visible, re-registered on cog load). Only the AFK owner may click
  it: they get an ephemeral, paginated (10/page, newest-first) panel of the
  saved mention snapshots; anyone else gets an ephemeral rejection. After the
  session ends the button says so, ephemerally, instead of failing.
* Each deduplicated mention snapshot stores the mentioner's id, display name
  at mention time, channel id and message content (columns self-healed on
  older installs with guarded ALTERs). Snapshots are deleted at dismissal —
  the zero-state and the session-ended state are first-class UI states.
"""

import math

import aiosqlite
import discord
from datetime import datetime, timezone
from discord.ext import commands

from database import DB_PATH
from utils.command_gating import load_toggle_row

# Emojis and appearance follow the project's existing card conventions:
# 0x7c5cbf is the brand colour used by wallet/economy/leveling/welcome
# cards, and the avatar + username pair is shown the same way the welcome
# embed builder renders an "author" (name + icon_url).
AFK_COLOR = 0x7c5cbf
# Animated custom emoji, used both when entering AFK and in the mention alert.
AFK_EMOJI = "<a:AFK:1552170028961374259>"
# Cancellation card emojis — the approved text uses two DIFFERENT static
# emojis, and deliberately no space between the first one and "هلا" (correct
# <:name:id> syntax for non-animated emojis).
RUBY_BACK_EMOJI = "<:Ruby_BACK:1552200382699016253>"
AQUA_WELCOME_EMOJI = "<:Aqua_Welcome:1552200384355762226>"

AFK_ON_TEXT = f"{AFK_EMOJI} لن يتمكن أحد من منشنك الآن {AFK_EMOJI}"
AFK_END_TEXT = f"{RUBY_BACK_EMOJI}هلا، تم إلغاء الـ AFK {AQUA_WELCOME_EMOJI}"

# Approved user-facing strings for the AFK dashboard/button pass (2026-09):
AFK_DISABLED_TEXT = "الـ AFK معطّل حاليًا في هذا السيرفر."
AFK_SESSION_ENDED_TEXT = "انتهت جلسة الـ AFK هذه، ولا توجد رسائل محفوظة لعرضها."
# Minimal functional texts left to implementation by the same pass:
AFK_NOT_OWNER_TEXT = "هذا الزر مخصص لصاحب جلسة الـ AFK فقط."
AFK_ZERO_MENTIONS_TEXT = "لا يوجد منشن محفوظ في جلسة الـ AFK هذه حتى الآن."

# Persistent "show saved messages" button on the AFK activation card. The
# approved label is a custom emoji plus this exact Arabic label — in
# discord.py terms that is Button(emoji=PartialEmoji(...), label=...), NOT a
# label containing the raw <:name:id> token.
AFK_BUTTON_LABEL = "إظهار الرسائل"
AFK_BUTTON_EMOJI = discord.PartialEmoji(
    name="930931paimonping", id=1552201663685595198)

# Mentions panel (ephemeral, owner-only): 10 snapshots per page, newest
# first (approved requirements).
MENTIONS_PAGE_SIZE = 10
# The panel view is NOT persistent — it only needs to live as long as the
# ephemeral response it is attached to.
MENTIONS_PANEL_TIMEOUT = 300
# Snapshot content is stored in full, but the panel truncates long messages
# for display so 10 entries can never overflow the embed description limit.
MAX_PANEL_CONTENT_LENGTH = 200

# Reasons longer than this are defensively truncated so the embed can never
# overflow Discord's field/description limits.
MAX_REASON_LENGTH = 1000
# Bounded in-memory guard against re-processing the same message within a
# single process run (belt and braces on top of the SQL dedup key).
SEEN_MESSAGES_CAP = 4096


def _reason_line(reason: str | None) -> str:
    """The ``**السبب:** ...`` line, or nothing when there is no reason."""
    if reason:
        return f"\n\n**السبب:** {reason}"
    return ""


def _display_name(user, guild) -> str:
    """Best-effort display name for a User/Member facing object."""
    if isinstance(user, discord.Member):
        return user.display_name
    member = guild.get_member(user.id) if guild is not None else None
    if member is not None:
        return member.display_name
    return (getattr(user, "display_name", None)
            or getattr(user, "global_name", None)
            or getattr(user, "name", None)
            or f"User {user.id}")


def _avatar_url(user) -> str | None:
    """Avatar url (display avatar when available), else None."""
    try:
        avatar = getattr(user, "display_avatar", None)
        url = getattr(avatar, "url", None) if avatar is not None else None
        if url:
            return str(url)
    except Exception:
        pass
    return None


def _mentions_custom_id(guild_id: int, owner_id: int) -> str:
    """Deterministic custom_id for a session's mentions button.

    Encodes guild+owner, which is exactly the ``afk_state`` primary key, so a
    re-registration after a restart resolves the same live session and an
    ended session can be told apart from a missing row.
    """
    return f"afk:mentions:{guild_id}:{owner_id}"


async def fetch_mentions_page(guild_id: int, owner_id: int, page: int,
                              per_page: int = MENTIONS_PAGE_SIZE):
    """One page of the owner's saved mention snapshots, NEWEST first.

    Returns ``(rows, total, total_pages)``; rows are 1-based-page slices of
    ``(author_name, content)``.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT COUNT(*) FROM afk_mentions
            WHERE guild_id = ? AND target_user_id = ?
        """, (guild_id, owner_id))
        total = (await cursor.fetchone())[0]
        cursor = await db.execute("""
            SELECT author_name, content FROM afk_mentions
            WHERE guild_id = ? AND target_user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (guild_id, owner_id, per_page, (page - 1) * per_page))
        rows = await cursor.fetchall()
    total_pages = max(1, math.ceil(total / per_page))
    return rows, total, total_pages


def build_mentions_panel(user, guild, rows, page: int,
                         total_pages: int) -> discord.Embed:
    """The ephemeral snapshot panel: mentioner's display name + content."""
    embed = discord.Embed(color=AFK_COLOR)
    embed.set_author(name=_display_name(user, guild),
                     icon_url=_avatar_url(user))
    lines = []
    for author_name, content in rows:
        text = content if content else "—"
        if len(text) > MAX_PANEL_CONTENT_LENGTH:
            text = text[:MAX_PANEL_CONTENT_LENGTH - 1] + "…"
        lines.append(f"**{author_name}:** {text}")
    embed.description = "\n".join(lines)
    if total_pages > 1:
        embed.set_footer(text=f"صفحة {page} من {total_pages}")
    return embed


class AFKMentionsPager(discord.ui.View):
    """Prev/next navigation for the ephemeral mentions panel.

    Deliberately NOT persistent: it is attached to an ephemeral interaction
    response, which only lives for the viewing session. The persistent part
    of the flow is the AFK-card button (``AFKMentionsView`` below).
    """

    def __init__(self, guild_id: int, owner_id: int, page: int,
                 total_pages: int):
        super().__init__(timeout=MENTIONS_PANEL_TIMEOUT)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.page = page
        self.total_pages = total_pages
        prev_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="◀️",
            disabled=page <= 1)
        prev_button.callback = self._on_prev
        next_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="▶️",
            disabled=page >= total_pages)
        next_button.callback = self._on_next
        self._prev_button = prev_button
        self._next_button = next_button
        self.add_item(prev_button)
        self.add_item(next_button)

    async def _flip(self, interaction: discord.Interaction, new_page: int):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                AFK_NOT_OWNER_TEXT, ephemeral=True)
            return
        rows, _total, total_pages = await fetch_mentions_page(
            self.guild_id, self.owner_id, new_page)
        self.page = new_page
        self.total_pages = total_pages
        self._prev_button.disabled = new_page <= 1
        self._next_button.disabled = new_page >= total_pages
        embed = build_mentions_panel(
            interaction.user, interaction.guild, rows, new_page, total_pages)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_prev(self, interaction: discord.Interaction):
        await self._flip(interaction, self.page - 1)

    async def _on_next(self, interaction: discord.Interaction):
        await self._flip(interaction, self.page + 1)


class AFKMentionsView(discord.ui.View):
    """Persistent mentions button, attached to every AFK activation card.

    Requirements it satisfies: always visible (attached unconditionally at
    activation, including when zero mentions exist so far); survives restarts
    (``timeout=None`` plus a deterministic custom_id re-registered on cog
    load); interaction access is owner-only — Discord cannot hide a public
    component visually, so a non-owner click is rejected ephemerally and
    never exposes the stored messages; an ended session answers with the
    approved session-ended text instead of resurrecting old snapshots.
    """

    def __init__(self, guild_id: int, owner_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.owner_id = owner_id
        button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=AFK_BUTTON_LABEL,
            emoji=AFK_BUTTON_EMOJI,
            custom_id=_mentions_custom_id(guild_id, owner_id))
        button.callback = self._on_click
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                AFK_NOT_OWNER_TEXT, ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT 1 FROM afk_state
                WHERE guild_id = ? AND user_id = ?
            """, (self.guild_id, self.owner_id))
            session_active = (await cursor.fetchone()) is not None
        if not session_active:
            await interaction.response.send_message(
                AFK_SESSION_ENDED_TEXT, ephemeral=True)
            return
        rows, total, total_pages = await fetch_mentions_page(
            self.guild_id, self.owner_id, 1)
        if total == 0:
            await interaction.response.send_message(
                AFK_ZERO_MENTIONS_TEXT, ephemeral=True)
            return
        embed = build_mentions_panel(
            interaction.user, interaction.guild, rows, 1, total_pages)
        pager = (AFKMentionsPager(self.guild_id, self.owner_id, 1, total_pages)
                 if total_pages > 1 else None)
        await interaction.response.send_message(
            embed=embed, view=pager, ephemeral=True)


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._seen_messages: set[int] = set()

    # ─── schema (same ensure_table pattern as sticky.py / customcommands.py) ──

    async def cog_load(self):
        await self.ensure_table()
        await self.register_persistent_views()

    async def register_persistent_views(self):
        """Re-register a mentions view for every active AFK session.

        Without this the button would die with the process: the message in
        Discord still shows it, but nothing would answer the click after a
        restart. Registration is keyed by the deterministic custom_id, and
        discord.py's view store keeps one dispatcher per custom_id, so a
        duplicate registration for the same session cannot create
        double-replies.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT guild_id, user_id FROM afk_state")
                sessions = await cursor.fetchall()
        except Exception as e:
            print(f"[AFK] could not load active sessions for view "
                  f"re-registration: {e}")
            return
        for guild_id, owner_id in sessions:
            try:
                self.bot.add_view(AFKMentionsView(guild_id, owner_id))
            except Exception as e:
                print(f"[AFK] could not re-register mentions view "
                      f"{guild_id}/{owner_id}: {e}")

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            # One active AFK row per guild+member.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS afk_state (
                    guild_id      INTEGER NOT NULL,
                    user_id       INTEGER NOT NULL,
                    reason        TEXT,
                    started_at    TEXT NOT NULL,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_afk_state_guild
                ON afk_state(guild_id)
            """)
            # Dedup ledger: a given message can count at most once per
            # target. Rows are deleted when the session ends, so the table
            # only ever holds entries for currently-AFK members. The
            # (author_id, author_name, channel_id, content) columns turn each
            # deduplicated mention into the owner's viewable snapshot —
            # self-healed onto older installs by _heal_mentions_schema below.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS afk_mentions (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id       INTEGER NOT NULL,
                    message_id     INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                    author_id      INTEGER NOT NULL DEFAULT 0,
                    author_name    TEXT NOT NULL DEFAULT '',
                    channel_id     INTEGER NOT NULL DEFAULT 0,
                    content        TEXT NOT NULL DEFAULT '',
                    UNIQUE (guild_id, message_id, target_user_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_afk_mentions_target
                ON afk_mentions(guild_id, target_user_id)
            """)
            await self._heal_mentions_schema(db)
            await db.commit()

    async def _heal_mentions_schema(self, db):
        """Add the snapshot columns to an older afk_mentions table.

        The repo's established self-heal approach (same PRAGMA + guarded
        ``ALTER TABLE ADD COLUMN`` pattern database.py uses to grow
        command_toggles: no separate migration framework, never a ``DROP``):
        a pre-existing final-session ledger row keeps its dedup data and gets
        safe defaults for columns it never recorded.
        """
        cursor = await db.execute("PRAGMA table_info(afk_mentions)")
        existing = {row[1] for row in await cursor.fetchall()}
        for column, ddl in (
            ("author_id",   "author_id INTEGER NOT NULL DEFAULT 0"),
            ("author_name", "author_name TEXT NOT NULL DEFAULT ''"),
            ("channel_id",  "channel_id INTEGER NOT NULL DEFAULT 0"),
            ("content",     "content TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                await db.execute(
                    f"ALTER TABLE afk_mentions ADD COLUMN {ddl}")

    # ─── command ──────────────────────────────────────────────────────────

    @commands.command(name="afk")
    async def afk(self, ctx, *, reason: str = None):
        """ضع نفسك في وضع AFK. استخدم ``!afk`` أو ``!afk السبب``."""
        await self._run_afk(ctx, reason)

    async def _run_afk(self, ctx, reason: str | None):
        if ctx.guild is None:
            await ctx.send("هذا الأمر يعمل داخل السيرفر فقط.")
            return

        # Idempotent: if the member is already AFK, change nothing (reason,
        # started_at and mention_count all stay), and just tell them. This
        # comes BEFORE the disabled gate on purpose: the approved toggle
        # semantics only gate NEW sessions, and an already-AFK owner re-running
        # !afk must keep getting the idempotent reply.
        already_afk, current_reason = await self.get_afk_reason(
            ctx.guild.id, ctx.author.id)
        if already_afk:
            suffix = f"\n**السبب:** {current_reason}" if current_reason else ""
            await ctx.send(
                f"{ctx.author.mention} بالفعل في وضع AFK." + suffix)
            return

        # Dashboard command toggle ("afk" row in the existing
        # command_toggles table, written by the existing Commands page
        # endpoints). Approved semantics: it gates ONLY starting NEW AFK
        # sessions — nothing else in this cog ever reads the toggle, so
        # dismissal, mention alerts and the mentions button keep working
        # while AFK is disabled. Only the enabled flag is honored, not the
        # role/channel/cooldown/owner-only columns. enabled is the first
        # column of utils.command_gating.TOGGLE_SELECT.
        toggle_row = await load_toggle_row(ctx.guild.id, "afk")
        if toggle_row is not None and not toggle_row[0]:
            await ctx.send(AFK_DISABLED_TEXT)
            return

        reason = (reason or "").strip()[:MAX_REASON_LENGTH] or None
        await self.set_afk(ctx.guild.id, ctx.author.id, reason)
        embed = self.build_afk_on_embed(ctx.author, ctx.guild, reason)
        view = AFKMentionsView(ctx.guild.id, ctx.author.id)
        try:
            self.bot.add_view(view)
        except Exception as e:
            print(f"[AFK] could not register mentions view: {e}")
        try:
            await ctx.send(embed=embed, view=view)
        except Exception as e:
            print(f"[AFK] could not send AFK-on embed: {e}")

    # ─── state reads/writes ──────────────────────────────────────────────

    async def get_afk_reason(self, guild_id: int, user_id: int):
        """Current reason for an AFK member.

        Returns a tuple ``(is_afk, reason)``: ``is_afk`` False when the member
        is not AFK; otherwise True and the stored reason (which may be None,
        i.e. they went AFK without a reason).
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT reason FROM afk_state
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            row = await cursor.fetchone()
        if row is None:
            return (False, None)
        return (True, row[0])

    async def set_afk(self, guild_id: int, user_id: int,
                      reason: str | None) -> None:
        """Mark a member AFK.

        Only called after ``get_afk_reason`` confirmed the member is not
        already AFK. ``ON CONFLICT DO NOTHING`` guards the (near-impossible)
        race where a session was inserted between that check and this write —
        in that case the existing session is left untouched rather than
        overwritten, preserving idempotency.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO afk_state (guild_id, user_id, reason, started_at, mention_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(guild_id, user_id) DO NOTHING
            """, (guild_id, user_id, reason,
                  datetime.now(timezone.utc).isoformat()))
            await db.commit()

    async def register_mention(self, guild_id: int, target_id: int,
                               message_id: int, *, author_id: int,
                               author_name: str, channel_id: int,
                               content: str) -> tuple[bool, str | None]:
        """Count one mention of an AFK member and store its snapshot.

        Returns ``(is_afk, reason)`` — ``is_afk`` is False when the target is
        not AFK (nothing was counted); otherwise True with the stored reason
        (which may be None). The ``(guild, message, target)`` unique key makes
        the increment idempotent per message: INSERT OR IGNORE reports a
        rowcount of 0 for an already-counted message, so the counter can never
        be bumped twice by double event processing — while two *different*
        messages each count.

        The snapshot fields are what the owner later sees through the panel:
        the mentioner's id, their display name at mention time, the channel
        and message ids (preserved identifiers), and the full message content
        (empty string is valid for mention-only messages).
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT reason FROM afk_state
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, target_id))
            row = await cursor.fetchone()
            if row is None:
                return (False, None)
            reason = row[0]

            cursor = await db.execute("""
                INSERT OR IGNORE INTO afk_mentions
                    (guild_id, message_id, target_user_id,
                     author_id, author_name, channel_id, content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, message_id, target_id,
                  author_id, author_name, channel_id, content))
            if cursor.rowcount:
                await db.execute("""
                    UPDATE afk_state SET mention_count = mention_count + 1
                    WHERE guild_id = ? AND user_id = ?
                """, (guild_id, target_id))
            await db.commit()
        return (True, reason)

    async def dismiss_afk(self, guild_id: int, user_id: int):
        """End a member's AFK session atomically.

        Returns ``(reason, mention_count)`` for the finished session, or
        None if the member was not AFK. The read/count/delete transition
        happens in one ``BEGIN IMMEDIATE`` transaction so two concurrent
        messages from the same member can never both dismiss and
        double-report.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute("""
                    SELECT reason, mention_count FROM afk_state
                    WHERE guild_id = ? AND user_id = ?
                """, (guild_id, user_id))
                row = await cursor.fetchone()
                if row is None:
                    await db.execute("ROLLBACK")
                    return None
                reason, count = row
                await db.execute("""
                    DELETE FROM afk_state WHERE guild_id = ? AND user_id = ?
                """, (guild_id, user_id))
                await db.execute("""
                    DELETE FROM afk_mentions WHERE guild_id = ? AND target_user_id = ?
                """, (guild_id, user_id))
                await db.commit()
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return (reason, count)

    # ─── embed builders (match the project's card conventions) ───────────

    def _card(self, user, guild) -> discord.Embed:
        name = _display_name(user, guild)
        avatar = _avatar_url(user)
        embed = discord.Embed(color=AFK_COLOR)
        embed.set_author(name=name, icon_url=avatar)
        return embed

    def build_afk_on_embed(self, user, guild,
                           reason: str | None) -> discord.Embed:
        embed = self._card(user, guild)
        embed.description = AFK_ON_TEXT + _reason_line(reason)
        return embed

    def build_afk_mention_embed(self, target, guild,
                                reason: str | None) -> discord.Embed:
        embed = self._card(target, guild)
        mention = getattr(target, "mention", None) or f"<@{target.id}>"
        embed.description = (f"{AFK_EMOJI} {mention} في وضع AFK حالياً "
                             f"{AFK_EMOJI}" + _reason_line(reason))
        return embed

    def build_afk_end_embed(self, user, guild,
                            mention_count: int) -> discord.Embed:
        embed = self._card(user, guild)
        embed.description = (AFK_END_TEXT +
                             f"\n\n**عدد المنشن أثناء غيابه: {mention_count}**")
        return embed

    # ─── event handling ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        guild = message.guild
        if guild is None:
            return

        # Only ever process a message once in this process.
        mid = message.id
        if mid in self._seen_messages:
            return
        self._seen_messages.add(mid)
        if len(self._seen_messages) > SEEN_MESSAGES_CAP:
            self._seen_messages.clear()

        # The ``!afk`` invocation itself must never cancel AFK. Because
        # process_commands and this listener run as sibling tasks, the check
        # has to be deterministic (content-based), not timing-based. We reuse
        # discord.py's own resolver exactly like utils/message_router does —
        # no manual content sniffing.
        try:
            ctx = await self.bot.get_context(message)
        except Exception:
            ctx = None
        if ctx is not None and ctx.command is not None and ctx.command.name == "afk":
            return

        # Member's own message (any channel) cancels AFK.
        result = await self.dismiss_afk(guild.id, message.author.id)
        if result is not None:
            reason, count = result
            embed = self.build_afk_end_embed(
                message.author, guild, count)
            try:
                await message.channel.send(embed=embed)
            except Exception as e:
                print(f"[AFK] could not send AFK-end embed: {e}")

        # Real mentions of AFK members (never a username text search).
        handled: set[int] = set()
        for target in message.mentions:
            target_id = target.id
            if target_id == message.author.id:
                continue  # mentioning yourself never alerts / never loops
            if target_id == self.bot.user.id:
                continue
            if target_id in handled:
                continue  # one alert per message, however many @ in it
            handled.add(target_id)

            is_afk, reason = await self.register_mention(
                guild.id, target_id, mid,
                author_id=message.author.id,
                author_name=_display_name(message.author, guild),
                channel_id=message.channel.id,
                content=message.content or "")
            if not is_afk:
                continue  # not AFK — no alert and nothing counted
            embed = self.build_afk_mention_embed(
                target, guild, reason)
            try:
                await message.channel.send(embed=embed)
            except Exception as e:
                print(f"[AFK] could not send AFK-mention embed: {e}")


async def setup(bot):
    await bot.add_cog(AFK(bot))
