"""Known-red diagnostic: source-ended cleanup must leave a durable obligation.

Run with PYTHONPATH=. python docs/execution-20261030/reproduce_booster_obligation_gap.py
All database state is scratch-only. Discord/notification boundaries are faked.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from phase1_support import GUILD, USER, reset_database, execute, rows
from test_timed_ownership import purchase_ctx, role_stub
from cogs.twitch import Twitch
from utils import creator_notify_engine as engine, timed_ownership as TO

async def main():
    await reset_database()
    role = role_stub(557, "Shared Twitch/Booster")
    _, person, bot, guild = purchase_ctx(granted_role=role, roles_held=[role])
    person.premium_since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Discord does not supply a reliable per-member boost count here.
    execute("INSERT INTO boost_config(guild_id,enabled,boost2_role_id) VALUES (?,1,557)",
            (GUILD,))
    assert not hasattr(person,"premium_subscription_count")
    with patch.object(engine,"get_watch_group",AsyncMock(return_value=None)), \
         patch.object(engine,"end_live_session",AsyncMock()):
        await Twitch._go_offline(SimpleNamespace(bot=bot),guild,GUILD,123,"scratch",557,USER)
    person.remove_roles.assert_not_awaited()
    obligations = rows("SELECT source,role_id FROM timed_holdings WHERE guild_id=? AND user_id=?",
                       (GUILD,USER))
    print({'role_preserved_under_unknown':role in person.roles,'durable_obligations':obligations},
          flush=True)
    assert obligations, "BLOCKER: source ended under unknown Booster eligibility without a durable retry obligation"

if __name__ == "__main__":
    asyncio.run(main())
