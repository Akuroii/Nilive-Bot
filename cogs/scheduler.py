import re
import json
import discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from database import DB_PATH

# FEATURE (dark-fixes pass #2): scheduled_messages had a schema
# (database.py) with zero implementation anywhere. This cog is the
# missing scheduler: a background loop that checks for due messages
# once a minute, sends them, and either disables one-off messages or
# rolls repeating ones forward to their next occurrence.
#
# All times are UTC — every timestamp stored and compared here is UTC,
# and command output says so explicitly to avoid the classic
# "why did my 9am message send at 2am" support ticket.

WHEN_RELATIVE_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
REPEAT_TYPES = ("none", "hourly", "daily", "weekly")


def parse_when(when: str) -> datetime | None:
    """
    Accepts either a relative shorthand ("30m", "2h", "3d") or an
    absolute "YYYY-MM-DD HH:MM" string, both interpreted as UTC.
    Returns None if it can't parse — callers turn that into a user
    facing error rather than guessing.
    """
    when = when.strip()
    m = WHEN_RELATIVE_RE.match(when)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=amount),
                 "h": timedelta(hours=amount),
                 "d": timedelta(days=amount)}[unit]
        return datetime.now(timezone.utc) + delta

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(when, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def compute_next_send(current_send_at: datetime, repeat_type: str,
                       repeat_interval: int) -> datetime | None:
    """
    Rolls a repeating message forward to its next occurrence, strictly
    after "now" — if the bot was offline for a while and a daily
    message's slot passed twice, this skips the missed one(s) instead
    of firing a burst of catch-up sends the moment the bot reconnects.
    """
    if repeat_type not in ("hourly", "daily", "weekly"):
        return None
    interval = max(1, repeat_interval or 1)
    step = {"hourly": timedelta(hours=interval),
            "daily": timedelta(days=interval),
            "weekly": timedelta(weeks=interval)}[repeat_type]

    next_at = current_send_at + step
    now = datetime.now(timezone.utc)
    while next_at <= now:
        next_at += step
    return next_at


class Scheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_scheduled.start()

    def cog_unload(self):
        self.check_scheduled.cancel()

    @tasks.loop(seconds=60)
    async def check_scheduled(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, channel_id, message_text, embed_data,
                       send_at, repeat_type, repeat_interval
                FROM scheduled_messages
                WHERE enabled = 1 AND send_at <= ?
            """, (now,))
            due = await cursor.fetchall()

        for (row_id, guild_id, channel_id, message_text, embed_data,
             send_at_str, repeat_type, repeat_interval) in due:
            sent_ok = await self._fire(row_id, channel_id, message_text, embed_data)
            if not sent_ok:
                # BUGFIX (caught by testing, not review): don't advance
                # send_at / disable the row on a failed send (deleted
                # channel, missing permissions, etc). The original
                # version marked the message as "sent" regardless of
                # whether it actually went out, silently losing it
                # forever with only a console log line as a trace.
                # Leaving state untouched means it retries next tick —
                # this will retry forever for a permanently-deleted
                # channel, which is a real tradeoff (log spam over
                # silent data loss), but losing a message an admin
                # scheduled is the worse failure mode of the two.
                continue

            send_at_dt = datetime.strptime(
                send_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            next_at = compute_next_send(send_at_dt, repeat_type, repeat_interval)

            async with aiosqlite.connect(DB_PATH) as db:
                if next_at:
                    await db.execute("""
                        UPDATE scheduled_messages
                        SET last_sent = ?, send_at = ?
                        WHERE id = ?
                    """, (now, next_at.strftime("%Y-%m-%d %H:%M:%S"), row_id))
                else:
                    # One-off message — disable rather than delete, so
                    # /schedule_list history still shows it fired.
                    await db.execute("""
                        UPDATE scheduled_messages
                        SET last_sent = ?, enabled = 0
                        WHERE id = ?
                    """, (now, row_id))
                await db.commit()

    async def _fire(self, row_id: int, channel_id: int,
                     message_text: str | None, embed_data: str | None) -> bool:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            print(f"[SCHEDULER] Skipped scheduled message {row_id}: "
                  f"channel {channel_id} not found (deleted or bot removed?) — will retry")
            return False
        embed = None
        if embed_data:
            try:
                embed = discord.Embed.from_dict(json.loads(embed_data))
            except Exception as e:
                print(f"[SCHEDULER] Bad embed_data for scheduled message {row_id}: {e}")
        try:
            await channel.send(content=message_text or None, embed=embed)
            return True
        except Exception as e:
            print(f"[SCHEDULER] Failed to send scheduled message {row_id}: {e} — will retry")
            return False

    @check_scheduled.before_loop
    async def before_check_scheduled(self):
        await self.bot.wait_until_ready()

    # ── admin commands ───────────────────────────────────────────────

    @app_commands.command(name="schedule_message",
                          description="Schedule a message to be sent later (UTC times)")
    @app_commands.describe(
        channel="Where to send it",
        message="The message text",
        when="Relative (\"30m\", \"2h\", \"3d\") or absolute UTC \"YYYY-MM-DD HH:MM\"",
        repeat="Repeat this message? Default: no",
        repeat_interval="Repeat every N hours/days/weeks (matches 'repeat'), default 1")
    @app_commands.choices(repeat=[
        app_commands.Choice(name="No — send once", value="none"),
        app_commands.Choice(name="Every N hours", value="hourly"),
        app_commands.Choice(name="Every N days", value="daily"),
        app_commands.Choice(name="Every N weeks", value="weekly"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def schedule_message(self, interaction: discord.Interaction,
                               channel: discord.TextChannel, message: str, when: str,
                               repeat: app_commands.Choice[str] = None,
                               repeat_interval: int = 1):
        send_at = parse_when(when)
        if send_at is None:
            await interaction.response.send_message(
                "Couldn't parse that time. Use relative shorthand like `30m`, `2h`, `3d`, "
                "or an absolute UTC time like `2026-07-20 14:00`.", ephemeral=True)
            return

        repeat_type = repeat.value if repeat else "none"
        if repeat_interval < 1:
            repeat_interval = 1

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO scheduled_messages
                    (guild_id, channel_id, message_text, send_at,
                     repeat_type, repeat_interval, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (interaction.guild.id, channel.id, message,
                  send_at.strftime("%Y-%m-%d %H:%M:%S"),
                  repeat_type, repeat_interval, interaction.user.id))
            await db.commit()

        repeat_note = "" if repeat_type == "none" else \
            f" (repeating every {repeat_interval} {repeat_type.replace('ly', '')}{'s' if repeat_interval != 1 else ''})"
        await interaction.response.send_message(
            f"✅ Scheduled for {channel.mention} at "
            f"`{send_at.strftime('%Y-%m-%d %H:%M UTC')}`{repeat_note}.", ephemeral=True)

    @app_commands.command(name="schedule_list",
                          description="List this server's scheduled messages")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def schedule_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, channel_id, message_text, send_at, repeat_type,
                       repeat_interval, enabled
                FROM scheduled_messages
                WHERE guild_id = ?
                ORDER BY enabled DESC, send_at ASC
                LIMIT 20
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No scheduled messages for this server yet.", ephemeral=True)
            return

        embed = discord.Embed(title="🗓️ Scheduled Messages", color=0x5865F2)
        for row_id, channel_id, text, send_at, rtype, rinterval, enabled in rows:
            channel = interaction.guild.get_channel(channel_id)
            chan_label = channel.mention if channel else f"(deleted channel {channel_id})"
            status = "🟢 active" if enabled else "⚪ done / disabled"
            repeat_label = "one-off" if rtype == "none" else f"every {rinterval} {rtype.replace('ly','')}(s)"
            preview = (text[:60] + "…") if text and len(text) > 60 else (text or "(embed only)")
            embed.add_field(
                name=f"#{row_id} — {status}",
                value=f"{chan_label} · `{send_at} UTC` · {repeat_label}\n{preview}",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="schedule_cancel",
                          description="Cancel a scheduled message by ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def schedule_cancel(self, interaction: discord.Interaction, message_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                UPDATE scheduled_messages SET enabled = 0
                WHERE id = ? AND guild_id = ?
            """, (message_id, interaction.guild.id))
            await db.commit()
            found = cursor.rowcount > 0
        if found:
            await interaction.response.send_message(
                f"Cancelled scheduled message #{message_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"No scheduled message #{message_id} found for this server.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Scheduler(bot))
