"""Role Replacement listener.

Discord dispatches ``on_member_update`` to every listener concurrently, so this
cog does the minimum possible in the event path: one indexed rules read (empty
for guilds without configuration), and removal work only for roles that were
actually added. All of the judgement lives in :mod:`utils.role_replacement`.

No slash commands: the feature is configured from the dashboard only, so this
cog adds nothing to command synchronization.
"""
import discord
from discord.ext import commands, tasks

from utils.role_replacement import describe, process_member_update, retry_pending
from utils.task_lifecycle import ensure_started, wait_until_client_ready


class RoleReplacement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        ensure_started(self.retry_loop)

    def cog_unload(self):
        self.retry_loop.cancel()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member,
                               after: discord.Member):
        if getattr(after, "bot", False) or after.guild is None:
            return
        try:
            result = await process_member_update(self.bot, before, after)
        except Exception as exc:
            # A role-replacement failure must never break role processing for
            # the rest of the bot (or discord.py's event loop).
            print(f"[ROLE REPLACEMENT] guild={getattr(after.guild, 'id', '?')} "
                  f"user={after.id}: {exc}")
            return
        for line in describe(result):
            print(f"{line} guild={after.guild.id} user={after.id}")

    @tasks.loop(minutes=10)
    async def retry_loop(self):
        """Retry removals Discord never confirmed.

        The row itself is the retry marker (same contract as the Shop's
        temp-role sweep): it is deleted only after the member's Discord state
        proves the role is gone.
        """
        try:
            summary = await retry_pending(self.bot)
        except Exception as exc:
            print(f"[ROLE REPLACEMENT] retry sweep failed: {exc}")
            return
        if summary.get("removed") or summary.get("cleared"):
            print(f"[ROLE REPLACEMENT] retry sweep: {summary}")

    @retry_loop.before_loop
    async def before_retry(self):
        # Poll is_ready() instead of awaiting wait_until_ready(): this cog is
        # loaded before login(), where wait_until_ready() raises and kills the
        # loop task on arrival (see utils/task_lifecycle.py).
        await wait_until_client_ready(self.bot)


async def setup(bot):
    await bot.add_cog(RoleReplacement(bot))
