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
"""

import aiosqlite
import discord
from datetime import datetime, timezone
from discord.ext import commands

from database import DB_PATH

# Emojis and appearance follow the project's existing card conventions:
# 0x7c5cbf is the brand colour used by wallet/economy/leveling/welcome
# cards, and the avatar + username pair is shown the same way the welcome
# embed builder renders an "author" (name + icon_url).
AFK_COLOR = 0x7c5cbf
# Animated custom emoji, used both when entering AFK and in the mention alert.
AFK_EMOJI = "<a:AFK:1552170028961374259>"
# Static custom emoji, used in the AFK-cancelled message (correct <:name:id>
# syntax for a non-animated emoji).
WELCOME_EMOJI = "<:welcome:1552174144710516776>"

AFK_ON_TEXT = f"{AFK_EMOJI} لن يتمكن أحد من منشنك الآن {AFK_EMOJI}"
AFK_END_TEXT = f"{WELCOME_EMOJI} هلا، تم إلغاء الـ AFK {WELCOME_EMOJI}"

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


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._seen_messages: set[int] = set()

    # ─── schema (same ensure_table pattern as sticky.py / customcommands.py) ──

    async def cog_load(self):
        await self.ensure_table()

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
            # only ever holds entries for currently-AFK members.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS afk_mentions (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id       INTEGER NOT NULL,
                    message_id     INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (guild_id, message_id, target_user_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_afk_mentions_target
                ON afk_mentions(guild_id, target_user_id)
            """)
            await db.commit()

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
        # started_at and mention_count all stay), and just tell them.
        already_afk, current_reason = await self.get_afk_reason(
            ctx.guild.id, ctx.author.id)
        if already_afk:
            suffix = f"\n**السبب:** {current_reason}" if current_reason else ""
            await ctx.send(
                f"{ctx.author.mention} بالفعل في وضع AFK." + suffix)
            return

        reason = (reason or "").strip()[:MAX_REASON_LENGTH] or None
        await self.set_afk(ctx.guild.id, ctx.author.id, reason)
        embed = self.build_afk_on_embed(ctx.author, ctx.guild, reason)
        try:
            await ctx.send(embed=embed)
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
                               message_id: int) -> tuple[bool, str | None]:
        """Count one mention of an AFK member.

        Returns ``(is_afk, reason)`` — ``is_afk`` is False when the target is
        not AFK (nothing was counted); otherwise True with the stored reason
        (which may be None). The ``(guild, message, target)`` unique key makes
        the increment idempotent per message: INSERT OR IGNORE reports a
        rowcount of 0 for an already-counted message, so the counter can never
        be bumped twice by double event processing — while two *different*
        messages each count.
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
                    (guild_id, message_id, target_user_id)
                VALUES (?, ?, ?)
            """, (guild_id, message_id, target_id))
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
                guild.id, target_id, mid)
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
