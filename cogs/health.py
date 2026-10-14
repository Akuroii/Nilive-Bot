import asyncio
import discord
import math
import platform
import traceback
from discord.ext import commands, tasks
import aiosqlite
import json
from database import DB_PATH
from utils.formatters import now_iso

HEARTBEAT_INTERVAL_SECONDS = 30


async def _ensure_row():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO bot_status (id, started_at)
            VALUES (1, ?)
        """, (now_iso(),))
        await db.commit()


async def _mark_startup():
    """Refresh ``started_at`` with THIS process's start time.

    ``_ensure_row`` only ever INSERTs the row (INSERT OR IGNORE), so
    without this the "Uptime — since last restart" card showed the age of
    the row — i.e. of the first-ever boot — and never reset across
    restarts. Called from ``Health.cog_load``, i.e. once per bot startup
    (a ``!reload health`` also re-stamps, which is accurate for the health
    system itself).
    """
    await _ensure_row()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bot_status SET started_at = ? WHERE id = 1",
            (now_iso(),))
        await db.commit()


def _latency_ms(raw) -> int:
    """Gateway latency as whole milliseconds; 0 when it isn't usable.

    ``bot.latency`` is a float *sentinel* before it is a measurement:
    ``inf`` until the first HEARTBEAT_ACK, ``nan`` with no websocket
    (discord.py gateway.py KeepAliveHandler / Client.latency). The old
    inline guard only rejected ``None``, so ``round(inf * 1000)`` /
    ``round(nan * 1000)`` raised (OverflowError / ValueError) and the
    loop's except swallowed the ENTIRE heartbeat write — silently
    starving ``bot_status`` of updates whenever latency was still a
    sentinel.
    """
    try:
        if raw is None or not math.isfinite(raw):
            return 0
        return round(raw * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


async def record_error(source: str, text: str):
    """Called from main.py's global error handlers (on_error,
    tree.on_error) so the health dashboard can surface the most
    recent failure without anyone having to SSH in and read logs.
    Only the single most recent error is kept — this is a health
    signal, not an audit trail (moderation_logs/audit_log already
    cover the things that need history).
    """
    await _ensure_row()
    trimmed = text[:4000] if text else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE bot_status
            SET last_error = ?, last_error_at = ?
            WHERE id = 1
        """, (f"[{source}] {trimmed}" if trimmed else None, now_iso()))
        await db.commit()


class Health(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.heartbeat.start()

    async def cog_load(self):
        # "Uptime — since last restart" must track THIS startup. A failure
        # to write health data must not prevent the cog (and therefore the
        # heartbeat) from loading — same policy as the loop body below.
        try:
            await _mark_startup()
        except Exception:
            print(f"[HEALTH] started_at stamp failed:\n{traceback.format_exc()}")

    def cog_unload(self):
        self.heartbeat.cancel()

    @tasks.loop(seconds=HEARTBEAT_INTERVAL_SECONDS)
    async def heartbeat(self):
        try:
            await _ensure_row()
            loaded = getattr(self.bot, "loaded_cogs", [])
            failed = getattr(self.bot, "failed_cogs", [])
            latency_ms = _latency_ms(self.bot.latency)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE bot_status
                    SET last_heartbeat     = ?,
                        guild_count        = ?,
                        latency_ms         = ?,
                        loaded_cogs        = ?,
                        failed_cogs        = ?,
                        discord_py_version = ?,
                        python_version     = ?
                    WHERE id = 1
                """, (
                    now_iso(),
                    len(self.bot.guilds),
                    latency_ms,
                    json.dumps(loaded),
                    json.dumps(failed),
                    discord.__version__,
                    platform.python_version(),
                ))
                await db.commit()
        except Exception:
            # A failure to WRITE health data shouldn't itself crash
            # the loop — print and let the next tick try again.
            print(f"[HEALTH] heartbeat write failed:\n{traceback.format_exc()}")

    @heartbeat.before_loop
    async def before_heartbeat(self):
        # PRE-LOGIN SAFETY: main.py loads cogs BEFORE bot.start(), and
        # bot.wait_until_ready() raises RuntimeError while Client._ready
        # is still the MISSING sentinel (it is only created inside
        # _async_setup_hook at login). tasks.Loop awaits before_loop
        # OUTSIDE its own try/except, so that exception killed the loop
        # task on arrival at every boot ("Task exception was never
        # retrieved") and last_heartbeat froze forever. is_ready() is
        # False — never an error — before login and flips True at READY,
        # so polling it keeps "wait until ready" semantics for both
        # boot-time loading and post-login reloads.
        while not self.bot.is_ready():
            await asyncio.sleep(0.5)


async def setup(bot):
    await bot.add_cog(Health(bot))
