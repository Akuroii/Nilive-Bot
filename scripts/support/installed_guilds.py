"""Test helper: developer access is limited to guilds the bot is installed in (PR #130), so
harnesses that drive the dashboard as the developer (OWNER_ID) must mark their guild installed."""


def seed_installed_guilds(*guild_ids):
    from database import sync_bot_guilds, restore_bot_sync_state
    from dashboard.utils.async_utils import run_async

    async def _seed():
        await sync_bot_guilds(
            [{"guild_id": int(g), "name": f"Test Guild {g}"} for g in guild_ids], authoritative=False)
        await restore_bot_sync_state()

    run_async(_seed())
