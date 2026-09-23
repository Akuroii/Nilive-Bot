"""Offline (no-network) tests for the /lock and /unlock fixes.

Pins the confirmed production bug and its preservation contract:

  1. CONFIRMED BUG: threads and forum posts crashed with
     `AttributeError: 'Thread' object has no attribute 'overwrites_for'`
     (and would equally hit the missing set_permissions) because the
     commands assumed a guild channel with permission overwrites. A
     thread's lock IS its native `locked` flag; forum posts ARE threads.
  2. PRESERVED: on guild channels the behaviour is byte-for-byte the old
     one — overwrite.send_messages False/None on @everyone +
     set_permissions(..., reason=...) + the same embeds + the same
     log_mod_action call on lock only (unlock still does not log).
  3. SCHEMA VERIFICATION (user-requested evidence for the legacy
     mod_logs question): the CURRENT database.py schema is exercised
     with a REAL log_mod_action write to BOTH mod_logs and
     moderation_logs. No migration is added — production evidence of a
     legacy schema does not exist (see report); this test proves the
     current schema is self-consistent and would only need to change if
     production proves otherwise.

Run:  python3 scripts/test_lock.py
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Redirect DB + OWNER_ID BEFORE any repo import (database.py validates
# OWNER_ID and binds DB_PATH at import time) — same convention as
# scripts/test_afk.py. Throwaway paths only: no repo artifacts.
os.environ["DATABASE_PATH"] = os.path.join(
    (_TMPDIR := tempfile.mkdtemp(prefix="locktest-")), "test.db")
if not os.getenv("OWNER_ID"):
    os.environ["OWNER_ID"] = "123456789012345678"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import aiosqlite  # noqa: E402
import discord  # noqa: E402
import cogs.moderation as M  # noqa: E402
from database import DB_PATH, init_db  # noqa: E402

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


class TextLike:
    """Guild-channel surface used by /lock and /unlock (has overwrites)."""

    def __init__(self, mention="#general"):
        self.mention = mention
        self.set_permissions = AsyncMock()
        self.overwrite_targets = []

    def overwrites_for(self, target):
        self.overwrite_targets.append(target)
        return discord.PermissionOverwrite()


class _Thread(discord.Thread):
    """Real Thread subclass — still isinstance(x, discord.Thread) for the
    fix branch, but has a __dict__ so tests can shadow .edit (the real
    class uses __slots__ and its methods are read-only on instances)."""


def make_thread(kind=11, name="post"):
    """Real discord.Thread (forum posts ARE threads) with a mocked edit."""
    st = discord.state.ConnectionState(
        dispatch=lambda *a, **k: None, handlers={}, hooks={},
        http=SimpleNamespace(), max_messages=None, application_id=None,
        intents=discord.Intents.all(), chunk_guilds_at_startup=False)
    g = discord.Guild(state=st, data={
        "id": 5, "name": "g", "owner_id": 1, "region": "europe",
        "verification_level": 0, "explicit_content_filter": 0,
        "default_message_notifications": 0, "vanity_url_code": None,
        "mfa_level": 0, "premium_tier": 0, "nsfw_level": 0,
        "premium_subscription_count": 0, "description": None, "banner": None,
        "icon": None, "splash": None, "discovery_splash": None, "features": [],
        "roles": [], "emojis": [], "large": False, "unavailable": False,
        "member_count": 1, "max_presences": 0, "max_members": 0,
        "channels": [], "threads": []})
    parent = discord.TextChannel(state=st, guild=g,
                                 data={"id": 4, "type": 0, "name": "chan",
                                       "position": 0})
    meta = {"archived": False, "auto_archive_duration": 60,
            "archive_timestamp": "2026-01-01T00:00:00+00:00",
            "locked": False, "create_timestamp": None}
    th = _Thread(guild=g, state=st, data={
        "id": 9, "type": kind, "name": name, "guild_id": g.id,
        "parent_id": parent.id, "owner_id": 2, "message_count": 1,
        "member_count": 1, "rate_limit_per_user": 0, "last_message_id": 1,
        "total_message_sent": 1, "flags": 0, "thread_metadata": meta})
    th.edit = AsyncMock(return_value=th)
    return th


def make_forum_post():
    """Real discord.Thread parented by a ForumChannel — a forum post."""
    st = discord.state.ConnectionState(
        dispatch=lambda *a, **k: None, handlers={}, hooks={},
        http=SimpleNamespace(), max_messages=None, application_id=None,
        intents=discord.Intents.all(), chunk_guilds_at_startup=False)
    g = discord.Guild(state=st, data={
        "id": 6, "name": "g2", "owner_id": 1, "region": "europe",
        "verification_level": 0, "explicit_content_filter": 0,
        "default_message_notifications": 0, "vanity_url_code": None,
        "mfa_level": 0, "premium_tier": 0, "nsfw_level": 0,
        "premium_subscription_count": 0, "description": None, "banner": None,
        "icon": None, "splash": None, "discovery_splash": None, "features": [],
        "roles": [], "emojis": [], "large": False, "unavailable": False,
        "member_count": 1, "max_presences": 0, "max_members": 0,
        "channels": [], "threads": []})
    forum = discord.ForumChannel(state=st, guild=g, data={
        "id": 7, "type": 15, "name": "forum", "position": 0,
        "rate_limit_per_user": 0, "available_tags": [], "flags": 0})
    g._channels[forum.id] = forum  # so the post resolves its forum parent
    meta = {"archived": False, "auto_archive_duration": 60,
            "archive_timestamp": "2026-01-01T00:00:00+00:00",
            "locked": False, "create_timestamp": None}
    th = _Thread(guild=g, state=st, data={
        "id": 11, "type": 11, "name": "a-post", "guild_id": g.id,
        "parent_id": forum.id, "owner_id": 2, "message_count": 1,
        "member_count": 1, "rate_limit_per_user": 0, "last_message_id": 1,
        "total_message_sent": 1, "flags": 0, "thread_metadata": meta})
    th.edit = AsyncMock(return_value=th)
    return th


def make_interaction(channel):
    return SimpleNamespace(
        channel=channel,
        guild=SimpleNamespace(id=10,
                              default_role=SimpleNamespace(id=1, name="@everyone")),
        user=SimpleNamespace(id=2, display_name="mod"),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


async def run_cmd(cmd, channel, reason="because"):
    """Invoke the real lock/unlock callback; return (interaction, err, logs)."""
    inter = make_interaction(channel)
    logs = AsyncMock()
    saved = M.log_mod_action
    M.log_mod_action = logs
    err = None
    try:
        await cmd.callback(None, inter, reason=reason)
    except Exception as e:  # noqa: BLE001 — the test IS about crashes
        err = e
    finally:
        M.log_mod_action = saved
    return inter, err, logs


def embed_of(inter):
    return inter.response.send_message.await_args.kwargs.get("embed")


async def main():
    print("[0] root-cause structure (why the old code crashed)")
    th = make_thread()
    check("Thread has no overwrites_for", not hasattr(th, "overwrites_for"))
    check("Thread has no set_permissions", not hasattr(th, "set_permissions"))

    print("[1] lock on a guild channel — PRESERVED overwrite behaviour")
    ch = TextLike()
    inter, err, logs = await run_cmd(M.Moderation.lock, ch, reason="spam")
    check("no crash", err is None, f"raised {err!r}")
    check("overwrites_for consulted with @everyone",
          ch.overwrite_targets and ch.overwrite_targets[0].id == 1)
    sp = ch.set_permissions.await_args
    check("set_permissions called", ch.set_permissions.await_count == 1)
    check("send_messages denied (False)", sp.kwargs["overwrite"].send_messages is False)
    check("reason plumbed into set_permissions", sp.kwargs.get("reason") == "spam")
    check("set_permissions target is @everyone", sp.args[0].id == 1)
    check("log_mod_action called exactly like before",
          logs.await_count == 1
          and logs.await_args.args == (10, inter.user, inter.user, "lock", "spam"),
          logs.await_args.args if logs.await_count else "")
    e = embed_of(inter)
    check("embed title preserved", e.title == "🔒 Channel Locked")
    check("embed description preserved",
          e.description == "#general has been locked.", e.description)
    check("embed Reason field preserved",
          e.fields[0].name == "Reason" and e.fields[0].value == "spam")

    print("[2] unlock on a guild channel — PRESERVED overwrite behaviour")
    ch = TextLike()
    inter, err, logs = await run_cmd(M.Moderation.unlock, ch, reason="ok")
    check("no crash", err is None, f"raised {err!r}")
    sp = ch.set_permissions.await_args
    check("send_messages reset (None)", sp.kwargs["overwrite"].send_messages is None)
    check("reason plumbed into set_permissions", sp.kwargs.get("reason") == "ok")
    check("unlock still does NOT call log_mod_action (preserved)",
          logs.await_count == 0)
    e = embed_of(inter)
    check("embed title preserved", e.title == "🔓 Channel Unlocked")
    check("embed description preserved",
          e.description == "#general has been unlocked.", e.description)

    print("[3] lock on a THREAD (the confirmed crash) — native locked flag")
    th = make_thread()
    inter, err, logs = await run_cmd(M.Moderation.lock, th, reason="raid")
    check("no AttributeError (was: 'Thread' object has no attribute "
          "'overwrites_for')", err is None, f"raised {err!r}")
    check("thread.edit called once", th.edit.await_count == 1)
    check("thread locked natively (locked=True)",
          th.edit.await_args.kwargs.get("locked") is True)
    check("reason plumbed into thread.edit",
          th.edit.await_args.kwargs.get("reason") == "raid")
    check("log_mod_action still called on lock",
          logs.await_count == 1 and logs.await_args.args[3] == "lock")
    e = embed_of(inter)
    check("lock embed preserved for threads", e.title == "🔒 Channel Locked")

    print("[4] lock/unlock on a FORUM POST (thread under a ForumChannel)")
    post = make_forum_post()
    inter, err, logs = await run_cmd(M.Moderation.lock, post, reason="nsfw")
    check("forum post lock: no crash", err is None, f"raised {err!r}")
    check("forum post locked natively",
          post.edit.await_args.kwargs.get("locked") is True)
    post = make_forum_post()
    inter, err, logs = await run_cmd(M.Moderation.unlock, post, reason="fixed")
    check("forum post unlock: no crash", err is None, f"raised {err!r}")
    check("forum post unlocked natively (locked=False)",
          post.edit.await_args.kwargs.get("locked") is False)
    check("reason plumbed into forum-post edit",
          post.edit.await_args.kwargs.get("reason") == "fixed")
    e = embed_of(inter)
    check("unlock embed preserved for forum posts",
          e.title == "🔓 Channel Unlocked")

    print("[5] unlock on a THREAD — the other half of the confirmed crash")
    th = make_thread()
    inter, err, logs = await run_cmd(M.Moderation.unlock, th)
    check("no AttributeError", err is None, f"raised {err!r}")
    check("thread unlocked natively (locked=False)",
          th.edit.await_args.kwargs.get("locked") is False)
    check("unlock on thread still does NOT log (preserved)",
          logs.await_count == 0)

    print("[6] SCHEMA VERIFICATION — real log_mod_action on the CURRENT schema")
    await init_db()
    u = SimpleNamespace(id=3, display_name="u",
                        display_avatar=SimpleNamespace(url="https://x/a.png"))
    mod = SimpleNamespace(id=4, display_name="mod",
                          display_avatar=SimpleNamespace(url="https://x/b.png"))
    err = None
    try:
        await M.log_mod_action(10, u, mod, "lock", "because")
    except Exception as e:  # noqa: BLE001
        err = e
    check("real log_mod_action write succeeds on current schema",
          err is None, f"raised {err!r}")
    async with aiosqlite.connect(DB_PATH) as db:
        cols = {r[1] for r in await (await db.execute(
            "PRAGMA table_info(mod_logs)")).fetchall()}
        check("mod_logs HAS 'source' (current schema)", "source" in cols, sorted(cols))
        check("mod_logs HAS 'duration_minutes' (current schema)",
              "duration_minutes" in cols, sorted(cols))
        n1 = (await (await db.execute(
            "SELECT COUNT(*) FROM mod_logs")).fetchone())[0]
        n2 = (await (await db.execute(
            "SELECT COUNT(*) FROM moderation_logs")).fetchone())[0]
    check("mod_logs row written", n1 == 1, f"n={n1}")
    check("moderation_logs row written (Blueprint table)", n2 == 1, f"n={n2}")
    print("  => current schema is self-consistent; no migration needed")
    print("     unless production PRAGMA table_info(mod_logs) proves otherwise")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
