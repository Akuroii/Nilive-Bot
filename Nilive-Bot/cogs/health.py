import discord
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


async def record_error(source: str, text: str):
    """
    Called from main.py's global error handlers (on_error,
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

    def cog_unload(self):
        self.heartbeat.cancel()

    @tasks.loop(seconds=HEARTBEAT_INTERVAL_SECONDS)
    async def heartbeat(self):
        try:
            await _ensure_row()
            loaded = getattr(self.bot, "loaded_cogs", [])
            failed = getattr(self.bot, "failed_cogs", [])
            latency_ms = (
                round(self.bot.latency * 1000)
                if self.bot.latency is not None else 0
            )
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
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Health(bot))
