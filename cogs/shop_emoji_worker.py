"""Drives the Shop icon preparation queue in the background.

Two loops, both bounded and both restarted with the process:

* ``worker_loop`` every ``WORKER_INTERVAL_SECONDS``: drains up to
  ``BATCH_LIMIT`` due rows, reconciling ambiguous assets first.
* ``backfill_loop`` every ``BACKFILL_INTERVAL_SECONDS``: tops up the queue from
  ``shop_items`` for guilds whose owner has authorised a backfill.

Discovery is **owner-gated per guild**. The loop iterates the
``shop_icon_backfill`` authorisation table, not ``bot.guilds``, so deploying
this build cannot start creating application emojis for every connected
server. New and changed products are still queued automatically by the
Dashboard save path, which is already an authorised, owner-issued action.

The loops never overlap with the manual prepare action: both go through
``utils.shop_emoji_worker.run_pass``, which holds a process-wide lock, and the
underlying queue writes are generation-fenced in the database.

Nothing here reads or writes Inventory or Rank state, and no loop touches
Discord unless there is due work — an idle bot makes zero API calls.
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

    async def authorized_guild_ids(self):
        """Guilds whose owner explicitly authorised backfill."""
        async with aiosqlite.connect(DB_PATH) as db:
            await Q.ensure_schema(db)
            return await Q.authorized_guilds(db)

    @tasks.loop(seconds=BACKFILL_INTERVAL_SECONDS)
    async def backfill_loop(self):
        """Discover eligible products for AUTHORISED guilds only."""
        if not _credentials_present() or not self.bot.is_ready():
            return
        for guild_id in await self.authorized_guild_ids():
            # A guild the bot has left must not keep accruing work.
            if self.bot.get_guild(guild_id) is None:
                continue
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    result = await Q.enqueue_guild_products(db, guild_id,
                                                            reason="backfill")
                    queued = result["queued"]
                    if queued:
                        await Q.record_sweep(db, guild_id, f"queued {queued}")
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
                "deferred=%s failed=%s stale=%s pruned=%s budget_exhausted=%s",
                report.get("processed"), report.get("created"), report.get("reused"),
                report.get("renamed"), report.get("deferred"), report.get("failed"),
                report.get("stale"), report.get("pruned"),
                report.get("budget_exhausted"))

    @backfill_loop.before_loop
    async def _before_backfill(self):
        await self.bot.wait_until_ready()

    @worker_loop.before_loop
    async def _before_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ShopEmojiWorker(bot))
