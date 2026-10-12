"""Keeps Shop Publication records in step with Discord deletions the bot can observe.

Handles single-message, bulk-message and channel deletion, plus one-time recovery of
operations interrupted by a restart. Every handler is idempotent and touches only the
exact publication rows the event identifies; see utils/shop_publication.py for the rules.

The bot can only react to deletions that happen while it is online. Anything it missed
is recovered through the dashboard's per-design "Re-check with Discord" action.
"""
import asyncio

from discord.ext import commands

from utils.shop_publication import (
    reconcile_channel_deleted, reconcile_message_deleted, recover_stale_operations,
)


class ShopPublicationSync(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._recovered = False

    async def _run(self, label, func, *args):
        # The service is synchronous (it uses a dedicated DB loop); keep it off the bot loop.
        try:
            removed = await asyncio.to_thread(func, *args)
        except Exception as exc:  # never let cleanup break event dispatch
            print(f"[shop-publication-sync] {label} failed: {exc!r}")
            return 0
        if removed:
            print(f"[shop-publication-sync] {label}: reconciled {removed} publication record(s)")
        return removed

    @commands.Cog.listener()
    async def on_ready(self):
        if self._recovered:
            return
        self._recovered = True
        await self._run("startup recovery", recover_stale_operations)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None:
            return
        await self._run("message deleted", reconcile_message_deleted,
                        payload.guild_id, payload.channel_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if payload.guild_id is None:
            return
        await self._run("bulk delete", reconcile_message_deleted,
                        payload.guild_id, payload.channel_id, list(payload.message_ids))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        await self._run("channel deleted", reconcile_channel_deleted, guild.id, channel.id)


async def setup(bot):
    await bot.add_cog(ShopPublicationSync(bot))
