"""Durable, DM-only notifications for expired streak episodes."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
import aiosqlite
from database import DB_PATH
from utils.timezone import CAIRO_TZ


def _utc_sql(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _event_marker(guild_id: int, user_id: int, last_claim_date: str) -> str:
    return f"streak-break:{guild_id}:{user_id}:{last_claim_date}"


async def record_break_event(db: aiosqlite.Connection, guild_id: int,
                             user_id: int, last_claim_date: str,
                             streak_count: int) -> bool:
    """Insert an episode once; safe inside the caller's claim transaction."""
    cur = await db.execute("""
        INSERT OR IGNORE INTO streak_break_notifications
            (guild_id, user_id, break_after_date, streak_count)
        VALUES (?, ?, ?, ?)
    """, (int(guild_id), int(user_id), str(last_claim_date),
          max(0, int(streak_count))))
    return cur.rowcount == 1


async def enqueue_due_streak_breaks(now: datetime | None = None) -> int:
    """Catch up all streaks broken by at least one missed Cairo calendar day."""
    now = now or datetime.now(timezone.utc)
    cairo_today = now.astimezone(CAIRO_TZ).date()
    cutoff = (cairo_today - timedelta(days=2)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = await (await db.execute("""
                SELECT guild_id, user_id, last_claim_date, streak_count
                FROM daily_claims
                WHERE last_claim_date <= ? AND streak_count > 0
            """, (cutoff,))).fetchall()
            added = 0
            for guild_id, user_id, last_date, count in rows:
                added += int(await record_break_event(
                    db, guild_id, user_id, last_date, count))
            await db.commit()
            return added
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def _set_status(guild_id: int, user_id: int, break_after_date: str,
                      status: str, *, error: str | None = None,
                      attempts: int | None = None,
                      next_attempt_at: str | None = None,
                      sent_at: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE streak_break_notifications
            SET status = ?, last_error = ?,
                attempts = COALESCE(?, attempts),
                next_attempt_at = COALESCE(?, next_attempt_at),
                sent_at = COALESCE(?, sent_at)
            WHERE guild_id = ? AND user_id = ? AND break_after_date = ?
        """, (status, error, attempts, next_attempt_at, sent_at,
              guild_id, user_id, break_after_date))
        await db.commit()


async def _claim_due_events(now: datetime, limit: int) -> list[tuple]:
    """Mark due rows as sending before network I/O; stale rows are recoverable."""
    now_sql = _utc_sql(now)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = await (await db.execute("""
                SELECT guild_id, user_id, break_after_date, streak_count,
                       status, attempts
                FROM streak_break_notifications
                WHERE status IN ('pending', 'sending')
                  AND next_attempt_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
            """, (now_sql, int(limit)))).fetchall()
            claimed = []
            for row in rows:
                guild_id, user_id, last_date, count, status, attempts = row
                # A row marked sending is a recovery after a prior process
                # stopped between Discord accepting the DM and SQLite marking it.
                attempts = int(attempts or 0) + 1
                await db.execute("""
                    UPDATE streak_break_notifications
                    SET status='sending', attempts=?, last_attempt_at=?,
                        next_attempt_at=?
                    WHERE guild_id=? AND user_id=? AND break_after_date=?
                """, (attempts, now_sql,
                      _utc_sql(now + timedelta(minutes=10)),
                      guild_id, user_id, last_date))
                claimed.append((guild_id, user_id, last_date, count,
                                status, attempts))
            await db.commit()
            return claimed
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def _marker_already_sent(channel, bot_user_id: int, marker: str) -> bool:
    """Search recent DM history before retrying an ambiguous send."""
    async for message in channel.history(limit=100):
        if message.author.id != bot_user_id:
            continue
        if any(marker in (embed.footer.text or "")
               for embed in getattr(message, "embeds", [])):
            return True
    return False


async def dispatch_streak_break_notifications(bot, *, now: datetime | None = None,
                                              limit: int = 25) -> dict:
    """Enqueue due breaks and send private DMs; never falls back to a guild channel.

    Blocked DMs are marked ``blocked``. Transient failures remain durable and
    retry with bounded exponential backoff. A recovery checks recent DM history
    for the stable event marker before sending again.
    """
    now = now or datetime.now(timezone.utc)
    enqueued = await enqueue_due_streak_breaks(now)
    events = await _claim_due_events(now, limit)
    sent = blocked = retried = deduped = 0
    bot_user_id = getattr(getattr(bot, "user", None), "id", None)

    for guild_id, user_id, last_date, streak_count, prior_status, attempts in events:
        marker = _event_marker(guild_id, user_id, last_date)
        try:
            user = bot.get_user(int(user_id)) if hasattr(bot, "get_user") else None
            if user is None:
                user = await bot.fetch_user(int(user_id))
            channel = await user.create_dm()
            if prior_status == "sending" or attempts > 1:
                if bot_user_id is None:
                    raise RuntimeError("Bot identity unavailable for DM duplicate check")
                if await _marker_already_sent(channel, int(bot_user_id), marker):
                    await _set_status(guild_id, user_id, last_date, "sent",
                                      sent_at=_utc_sql(now))
                    sent += 1
                    deduped += 1
                    continue
            break_date = (date.fromisoformat(last_date) + timedelta(days=2)).isoformat()
            embed = __import__("discord").Embed(
                title="🔥 Your streak has broken",
                description=(
                    f"Your **{int(streak_count)}-day** streak broke at the start "
                    f"of **{break_date}** (Africa/Cairo) after a full Cairo day "
                    f"without a claim. Your current streak is **0**. Your next "
                    f"successful claim starts at **Stage 1**."
                ),
                color=0xE67E22,
            )
            embed.add_field(name="Last successful claim", value=last_date, inline=True)
            embed.set_footer(text=f"NILIVE streak notice · {marker}")
            await channel.send(embed=embed)
            await _set_status(guild_id, user_id, last_date, "sent",
                              sent_at=_utc_sql(now))
            sent += 1
        except Exception as exc:
            # A forbidden DM is terminal for this prototype; do not publish
            # a private notice in a guild channel or let it affect the award.
            name = exc.__class__.__name__
            if name == "Forbidden":
                await _set_status(guild_id, user_id, last_date, "blocked",
                                  error="DM blocked or unavailable")
                blocked += 1
                continue
            delay_minutes = min(24 * 60, 2 ** min(max(attempts, 1), 10))
            await _set_status(
                guild_id, user_id, last_date, "pending",
                error=f"{name}: {str(exc)[:300]}",
                next_attempt_at=_utc_sql(now + timedelta(minutes=delay_minutes)),
            )
            retried += 1

    return {"enqueued": enqueued, "sent": sent, "blocked": blocked,
            "retried": retried, "deduplicated": deduped}
