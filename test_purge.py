"""Offline (no-network) tests for the /purge command fix.

Pins the confirmed production bug and its exact preservation contract:

  1. CONFIRMED BUG: `check=None` was passed to channel.purge() for every
     purge without a member filter. discord.py only substitutes its
     match-everything default when the argument is the MISSING sentinel;
     a literal None reaches PurgeIterator's `if self.check(message)` as
     a non-callable and the command dies with "'NoneType' object is not
     callable". The fix must pass a REAL callable;
  2. real purge behaviour unchanged: same limit, same member-filter
     semantics (author equality), same "Deleted N messages." reply,
     same amount bounds, same defer(ephemeral=True);
  3. CONFIRMED LIMITATION (guard): discord.ForumChannel has no .purge
     (verified against discord.py 2.7.1), so /purge in a forum channel
     must reply with the limitation text instead of AttributeError.
     Threads DO have .purge and must keep the normal path.

Run:  python3 scripts/test_purge.py
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Throwaway env BEFORE any repo import (database.py validates OWNER_ID
# and binds DB_PATH at import time) — same convention as scripts/test_afk.py.
os.environ["DATABASE_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="purgetest-"), "test.db")
if not os.getenv("OWNER_ID"):
    os.environ["OWNER_ID"] = "123456789012345678"

# Path the repo root so `cogs.moderation` imports cleanly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import discord  # noqa: E402
from cogs.moderation import Moderation  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


class FakeChannelWithPurge:
    """Text-channel / thread surface: has .purge like the real ones."""

    def __init__(self, deleted_count=3):
        self.purge = AsyncMock(return_value=[object() for _ in range(deleted_count)])


class FakeForumChannel:
    """Forum-channel surface: deliberately NO .purge attribute.

    Mirrors the real discord.ForumChannel (verified:
    hasattr(discord.ForumChannel, 'purge') is False).
    """


def make_interaction(channel):
    return SimpleNamespace(
        channel=channel,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def run_purge(channel, amount=10, member=None):
    """Invoke the real /purge callback; return (interaction, error|None)."""
    inter = make_interaction(channel)
    err = None
    try:
        await Moderation.purge.callback(None, inter, amount=amount, member=member)
    except Exception as e:  # noqa: BLE001 — the test IS about crashes
        err = e
    return inter, err


async def main():
    print("[1] no-member purge: check is a real callable (the confirmed bug)")
    ch = FakeChannelWithPurge(deleted_count=3)
    inter, err = await run_purge(ch, amount=5, member=None)
    check("no crash (was: 'NoneType' object is not callable)", err is None,
          f"raised {err!r}")
    check("purge called", ch.purge.await_count == 1)
    kwargs = ch.purge.await_args.kwargs
    check("check passed to purge is callable",
          callable(kwargs.get("check")), f"check={kwargs.get('check')!r}")
    check("check accepts a message and matches (no filter)",
          kwargs.get("check") is not None
          and kwargs["check"](SimpleNamespace(author=object())) is True)
    check("purge called with keyword args only",
          ch.purge.await_args.args == (),
          f"positional={ch.purge.await_args.args}")
    check("limit preserved (== amount)", kwargs.get("limit") == 5,
          f"limit={kwargs.get('limit')!r}")
    check("reply preserved: 'Deleted 3 messages.'",
          inter.followup.send.await_args.args[0] == "Deleted 3 messages.",
          inter.followup.send.await_args.args)
    check("reply is ephemeral", inter.followup.send.await_args.kwargs.get("ephemeral") is True)

    print("[2] member filter behaviour preserved (author equality)")
    bob, alice = object(), object()
    ch = FakeChannelWithPurge()
    inter, err = await run_purge(ch, amount=10, member=bob)
    check("no crash with member filter", err is None, f"raised {err!r}")
    f = ch.purge.await_args.kwargs.get("check")
    check("filter keeps bob's messages", f(SimpleNamespace(author=bob)) is True)
    check("filter drops alice's messages", f(SimpleNamespace(author=alice)) is False)

    print("[3] amount bounds preserved")
    for bad in (0, 101, -1, 1000):
        ch = FakeChannelWithPurge()
        inter, err = await run_purge(ch, amount=bad, member=None)
        check(f"amount={bad} refused without crash", err is None)
        check(f"amount={bad} bounds text preserved",
              inter.followup.send.await_args.args[0]
              == "Amount must be between 1 and 100.",
              inter.followup.send.await_args.args)
        check(f"amount={bad} does not touch the channel", ch.purge.await_count == 0)

    print("[4] ForumChannel limitation guard (no .purge -> clear reply)")
    ch = FakeForumChannel()
    inter, err = await run_purge(ch, amount=5, member=None)
    check("no AttributeError crash on forum channel", err is None, f"raised {err!r}")
    check("limitation text sent",
          inter.followup.send.await_args.args[0]
          == "Bulk message deletion isn't supported in this channel type.",
          inter.followup.send.await_args.args)
    check("limitation reply is ephemeral",
          inter.followup.send.await_args.kwargs.get("ephemeral") is True)

    print("[5] thread-like channels keep the normal path (they have .purge)")
    ch = FakeChannelWithPurge(deleted_count=2)
    inter, err = await run_purge(ch, amount=2, member=None)
    check("thread-like purge works", err is None and ch.purge.await_count == 1)
    check("thread-like reply preserved",
          inter.followup.send.await_args.args[0] == "Deleted 2 messages.")

    print("[6] preserved plumbing")
    ch = FakeChannelWithPurge()
    inter, err = await run_purge(ch)
    check("defer(ephemeral=True) preserved",
          inter.response.defer.await_args.kwargs.get("ephemeral") is True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
