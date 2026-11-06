"""Drives the Shop icon preparation queue in the background.

Two loops, both bounded and both restarted with the process:

* ``worker_loop`` every ``WORKER_INTERVAL_SECONDS``: drains up to
  ``BATCH_LIMIT`` due rows, reconciling ambiguous assets first.
* ``backfill_loop`` every ``BACKFILL_INTERVAL_SECONDS``: top up the queue from
  ``shop_items`` (the authoritative table) so products that were never prepared
  — including everything that predates this feature — are picked up without any
  Shop Designer roster entry.

The loops never overlap with the manual prepare action: both go through
``utils.shop_emoji_worker.run_pass``, which holds a process-wide lock, and the
underlying asset writes are fenced in the database.

Nothing here reads or writes Inventory or Rank state, and no loop touches
Discord unless the queue has due work — an idle bot makes zero API calls.
"""
import asyncio
import logging
import os

import aiosqlite
from discord.ext import commands, tasks

from database import DB_PATH
from utils import shop_emoji as SE
from utils import shop_emoji_queue as Q
from utils.shop_emoji_worker import run_pass

log = logging.getLogger(__name__)

WORKER_INTERVAL_SECONDS = 60
BACKFILL_INTERVAL_SECONDS = 300
BATCH_LIMIT = Q.DEFAULT_BATCH_LIMIT


def _credentials_present():
    return bool(SE.configured_application_id() and os.getenv("DISCORD_TOKEN", "").strip())


class ShopEmojiWorker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.worker_loop.start()
        self.backfill_loop.start()

    def cog_unload(self):
        self.worker_loop.cancel()
        self.backfill_loop.cancel()

    async def _guild_ids(self):
        """Every guild this bot serves — the queue is per guild by design."""
        return [guild.id for guild in self.bot.guilds]

    async def _top_up(self, guild_id, *, reason="backfill"):
        async with aiosqlite.connect(DB_PATH) as db:
            return await Q.enqueue_guild_products(db, guild_id, reason=reason)

    @tasks.loop(seconds=BACKFILL_INTERVAL_SECONDS)
    async def backfill_loop(self):
        """Discover eligible products straight from the Shop database."""
        if not _credentials_present() or not self.bot.is_ready():
            return
        for guild_id in await self._guild_ids():
            try:
                queued = await self._top_up(guild_id)
                if queued:
                    log.info("shop emoji backfill: queued %s product(s) for guild %s",
                             queued, guild_id)
            except Exception as exc:  # noqa: BLE001 - a discovery tick must not kill the loop
                log.warning("shop emoji backfill: guild %s failed: %r", guild_id, exc)

    @tasks.loop(seconds=WORKER_INTERVAL_SECONDS)
    async def worker_loop(self):
        """Process due rows. Idle when the queue is empty: no Discord calls."""
        if not _credentials_present() or not self.bot.is_ready():
            return
        try:
            report = await run_pass(limit=BATCH_LIMIT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("shop emoji worker pass failed: %r", exc)
            return
        if report.get("processed") or report.get("renamed") or report.get("pruned"):
            log.info(
                "shop emoji pass: processed=%s created=%s reused=%s renamed=%s "
                "deferred=%s failed=%s pruned=%s budget_exhausted=%s",
                report.get("processed"), report.get("created"), report.get("reused"),
                report.get("renamed"), report.get("deferred"), report.get("failed"),
                report.get("pruned"), report.get("budget_exhausted"))

    @backfill_loop.before_loop
    async def _before_backfill(self):
        await self.bot.wait_until_ready()

    @worker_loop.before_loop
    async def _before_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ShopEmojiWorker(bot))
