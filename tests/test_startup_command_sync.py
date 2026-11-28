import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OWNER_ID", "704453350384730237")
os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.gettempdir(), "nilive-sync-test-scratch.db"))
os.environ.setdefault("DISCORD_TOKEN", "test.token.value-with-no-credential")

import main


@pytest.mark.asyncio
async def test_startup_sync_runs_once_after_success_and_skips_repeated_ready(monkeypatch):
    sync = AsyncMock(return_value=[object(), object()])
    monkeypatch.setattr(main.bot.tree, "sync", sync)
    monkeypatch.setattr(main.bot, "_startup_commands_synced", False, raising=False)

    assert len(await main.sync_startup_commands()) == 2
    assert await main.sync_startup_commands() is None
    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_ready_callbacks_coalesce_startup_sync(monkeypatch):
    async def slow_sync():
        await asyncio.sleep(0.01)
        return [object()]

    sync = AsyncMock(side_effect=slow_sync)
    monkeypatch.setattr(main.bot.tree, "sync", sync)
    monkeypatch.setattr(main.bot, "_startup_commands_synced", False, raising=False)

    results = await asyncio.gather(*(main.sync_startup_commands() for _ in range(5)))
    assert sum(result is not None for result in results) == 1
    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_sync_failure_is_reported_and_retryable(monkeypatch):
    sync = AsyncMock(side_effect=[RuntimeError("Discord API unavailable"), [object()]])
    monkeypatch.setattr(main.bot.tree, "sync", sync)
    monkeypatch.setattr(main.bot, "_startup_commands_synced", False, raising=False)

    with pytest.raises(RuntimeError, match="unavailable"):
        await main.sync_startup_commands()
    assert main.bot._startup_commands_synced is False
    assert len(await main.sync_startup_commands()) == 1
    assert main.bot._startup_commands_synced is True
    assert sync.await_count == 2
