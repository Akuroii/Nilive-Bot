"""Offline tests for main.py's app-command error handler (bot.tree.error).

Pins the confirmed production bug and its preservation contract:

  1. CONFIRMED BUG: when a command had already deferred (or otherwise
     responded) and then raised, the handler's `if not
     interaction.response.is_done()` guard skipped the user-facing reply
     and the error was silently swallowed (13 defer sites in the bot).
     The reply must now go through interaction.followup.send(...);
  2. PRESERVED: the not-yet-responded path still uses
     response.send_message with the exact same text;
  3. PRESERVED: error logging (print + cogs.health.record_error with
     `command:/<name>`) on the generic path, the early
     MissingPermissions/CheckFailure branch, and delivery failures stay
     swallowed by `except Exception: pass`.

Imports the REAL main.py (with a dummy DISCORD_TOKEN — the module-level
validator only warns on odd shapes and never calls Discord) and calls
the real registered handler.

Run:  python3 scripts/test_tree_error.py
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# main.py validates DISCORD_TOKEN at import (shape check only), and its
# database import validates OWNER_ID / binds DB_PATH at import time —
# throwaway env first (same convention as scripts/test_afk.py).
os.environ.setdefault("DISCORD_TOKEN", "x" * 20 + "." + "y" * 20 + "." + "z" * 20)
os.environ["DATABASE_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="treeerrtest-"), "test.db")
if not os.getenv("OWNER_ID"):
    os.environ["OWNER_ID"] = "123456789012345678"

import discord  # noqa: E402
import cogs.health  # noqa: E402
import main  # noqa: E402  — the real handler lives here

PASS = 0
FAIL = 0

GENERIC_TEXT = ("Something went wrong running that command. "
                "The error has been logged.")
PERM_TEXT = "You don't have permission to use this command."


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make_interaction(is_done=True, name="purge"):
    return SimpleNamespace(
        command=SimpleNamespace(qualified_name=name),
        response=SimpleNamespace(
            is_done=Mock(return_value=is_done),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def call_handler(error, is_done=True, name="purge"):
    """Call the REAL on_app_command_error; return (interaction, record_mock)."""
    inter = make_interaction(is_done=is_done, name=name)
    record = AsyncMock()
    saved = cogs.health.record_error
    cogs.health.record_error = record
    try:
        await main.on_app_command_error(inter, error)
    finally:
        cogs.health.record_error = saved
    return inter, record


async def main_tests():
    print("[1] DEFERRED interaction: error reply goes through followup (the fix)")
    inter, record = await call_handler(Exception("boom"), is_done=True)
    check("followup.send called once", inter.followup.send.await_count == 1,
          f"count={inter.followup.send.await_count}")
    check("exact preserved text via followup",
          inter.followup.send.await_args.args[0] == GENERIC_TEXT,
          inter.followup.send.await_args.args)
    check("followup reply is ephemeral",
          inter.followup.send.await_args.kwargs.get("ephemeral") is True)
    check("response.send_message NOT used (slot consumed)",
          inter.response.send_message.await_count == 0)

    print("[2] logging preserved on the deferred path")
    check("record_error called", record.await_count == 1)
    check("record_error source is command:/purge",
          record.await_args.args[0] == "command:/purge", record.await_args.args)
    check("record_error carries the traceback text",
          "boom" in record.await_args.args[1], record.await_args.args[1][:80])

    print("[3] NOT-responded path preserved (send_message with same text)")
    inter, record = await call_handler(Exception("boom2"), is_done=False)
    check("response.send_message called once",
          inter.response.send_message.await_count == 1)
    check("exact preserved text via response",
          inter.response.send_message.await_args.args[0] == GENERIC_TEXT,
          inter.response.send_message.await_args.args)
    check("ephemeral preserved",
          inter.response.send_message.await_args.kwargs.get("ephemeral") is True)
    check("followup NOT used", inter.followup.send.await_count == 0)
    check("logging preserved", record.await_args.args[0] == "command:/purge"
          and "boom2" in record.await_args.args[1])

    print("[4] MissingPermissions early branch preserved")
    err = discord.app_commands.MissingPermissions(["manage_messages"])
    inter, record = await call_handler(err, is_done=False)
    check("permission text preserved",
          inter.response.send_message.await_args.args[0] == PERM_TEXT,
          inter.response.send_message.await_args.args)
    check("permission branch is ephemeral",
          inter.response.send_message.await_args.kwargs.get("ephemeral") is True)
    check("permission branch does not log to record_error",
          record.await_count == 0)
    check("permission branch does not touch followup",
          inter.followup.send.await_count == 0)

    print("[5] delivery failures stay swallowed (except-pass preserved)")
    inter = make_interaction(is_done=True)
    inter.followup.send = AsyncMock(side_effect=RuntimeError("unknown interaction"))
    record = AsyncMock()
    saved = cogs.health.record_error
    cogs.health.record_error = record
    raised = None
    try:
        await main.on_app_command_error(inter, Exception("boom3"))
    except Exception as e:  # noqa: BLE001
        raised = e
    finally:
        cogs.health.record_error = saved
    check("handler does not raise when followup fails", raised is None,
          f"raised {raised!r}")
    check("logging still happened before delivery", record.await_count == 1)

    inter = make_interaction(is_done=False)
    inter.response.send_message = AsyncMock(side_effect=RuntimeError("http 500"))
    saved = cogs.health.record_error
    cogs.health.record_error = AsyncMock()
    try:
        await main.on_app_command_error(inter, Exception("boom4"))
        raised = None
    except Exception as e:  # noqa: BLE001
        raised = e
    finally:
        cogs.health.record_error = saved
    check("handler does not raise when send_message fails", raised is None,
          f"raised {raised!r}")

    print("[6] command name resolution preserved (qualified_name)")
    inter, record = await call_handler(Exception("x"), is_done=True,
                                       name="lock/unlock-alias")
    check("qualified name used in log source",
          record.await_args.args[0] == "command:/lock/unlock-alias",
          record.await_args.args)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_tests()))
