"""PROBE 12 — the alias path must survive a REAL discord.Message.

Live incident (2026-08-28): typing an alias such as `b` in Discord crashed
every alias invocation with

    File "cogs/command_aliases.py", line 900, in __init__
      self.message_reference = real.message_reference
    AttributeError: 'Message' object has no attribute 'message_reference'

because a real ``discord.Message`` (2.7.1) exposes ``reference`` — renamed
from ``message_reference`` in discord.py 2.0 — and the harness's
``FakeMessage`` in p0_env defined *both* names, so every probe that used it
validated the bug away instead of catching it.

This probe constructs a **real** ``discord.Message`` (exactly the object the
gateway hands ``on_message`` in production), dispatches it through the real
bot, and asserts:

  1. the installed ``discord.Message`` really has no ``message_reference``
     attribute (documents the root cause; if a future discord.py ever
     reintroduces the name, the check says so instead of silently
     changing what the probe defends);
  2. ``k <@id> being rude`` typed as a real message executes the real
     ``/kick`` callback (kick recorded by the stubbed
     ``discord.Member.kick``);
  3. ``ta hello some long response text`` executes ``/trigger_add`` with
     the trailing text intact, and carries a live ``reference`` payload
     (a reply-to message — the other shape real messages have);
  4. no "Couldn't run …" error banner was sent, i.e. nothing escaped
     ``bot.invoke``.

Run:  python tools/alias_probes/p12_real_message_surface.py
Exit code 0 = every check passed.
"""
import asyncio, datetime, pathlib, sys, types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeChannel, SENT_LOG)

import aiosqlite
import discord
from database import DB_PATH

GUILD = 333
AUTHOR_ID = 111
TARGET_ID = 260100000000004242   # discord.py's mention regex needs 15-20 digits
KICKS = []                       # (member_id, reason) recorded by Member.kick
_MID = [1_300_000_000_000_000_000]


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{('  -> ' + str(detail)) if detail else ''}")
    if not condition:
        FAILURES.append(label)


CHECKS = 0
FAILURES = []


async def _recorded_kick(self, *, reason=None, **kw):
    """Stands in for the only real network call /kick makes."""
    KICKS.append((self.id, reason))


def guild_for(gid):
    """Same fake guild as p8, except store_user also accepts the ``cache=``
    keyword that ``Message._handle_author`` passes in 2.7.1."""
    g = FakeGuildLike(id=gid)
    g.shard_id = 0
    g.owner_id = 999
    g.me = None
    g.get_role = lambda rid: None
    g.default_role = types.SimpleNamespace(id=1, position=0, name="@everyone")

    def store_user(user_id=None, data=None, **kw):
        # 2.7.1 Message calls store_user(data, cache=...); Member calls
        # store_user(data). Accept either.
        if isinstance(user_id, dict):
            user_id, data = user_id.get("id"), user_id
        data = data or {}
        uid = int(user_id)
        return types.SimpleNamespace(
            id=uid, id_str=str(uid), name=data.get("username", f"user{uid}"),
            discriminator=data.get("discriminator", "0"),
            global_name=data.get("global_name"),
            display_name=data.get("global_name") or data.get("username", f"user{uid}"),
            mention=f"<@{uid}>", bot=bool(data.get("bot")), avatar=None,
            created_at=datetime.datetime(2020, 1, 1),
            default_avatar_url="https://x/y.png")

    g._state = types.SimpleNamespace(
        get_user=lambda uid: None, dispatch=lambda *a, **k: None,
        store_user=store_user,
        member_cache=discord.MemberCacheFlags.none(),
        member_cache_flags=discord.MemberCacheFlags.none(),
        http=None)

    def real_member(uid):
        return discord.Member(
            guild=g, state=g._state,
            data={
                "user": {"id": str(uid), "username": f"user{uid}",
                         "discriminator": "0", "global_name": None,
                         "avatar": None, "bot": None, "public_flags": 0},
                "roles": [],
                "joined_at": "2024-01-01T00:00:00+00:00",
                "pending": False,
                "flags": 0,
                "communication_disabled_until": None,
            })

    def get_member(uid):
        return real_member(int(uid)) if uid is not None else None

    def get_member_named(spec):
        digits = "".join(ch for ch in str(spec) if ch.isdigit())
        if not digits:
            raise LookupError(spec)
        return real_member(int(digits))

    async def query_members(*a, limit=None, user=None, user_ids=None, **kw):
        ids = user_ids if user_ids is not None else (user or [])
        if not isinstance(ids, (list, tuple)):
            ids = [ids]
        return [real_member(int(uid)) for uid in ids]

    g.get_member = get_member
    g.get_member_named = get_member_named
    g.query_members = query_members
    g.real_member = real_member
    return g


class FakeGuildLike:
    """Identical to p0_env.FakeGuild except it must NOT be a discord.Guild —
    that is what makes a real Message keep its fake state object."""

    def __init__(self, id=333):
        self.id = id
        self.owner_id = 111
        self.name = "probe-guild"

    def get_channel(self, cid):
        return None


def make_real_message(content, *, with_reference=False):
    """A genuine ``discord.Message`` built the way the gateway builds it:
    real class, real ``__init__``, only the network layer absent."""
    _MID[0] += 1
    guild = guild_for(GUILD)
    ch = FakeChannel(guild=guild)
    ch.permissions_for = lambda obj: types.SimpleNamespace(
        kick_members=True, ban_members=True, manage_guild=True,
        manage_roles=True, mention_everyone=False, manage_messages=True)

    data = {
        "id": str(_MID[0]),
        "type": 0,
        "content": content,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "flags": 0,
        "pinned": False,
        "author": {"id": str(AUTHOR_ID), "username": f"user{AUTHOR_ID}",
                   "discriminator": "0", "global_name": None,
                   "avatar": None, "bot": None, "public_flags": 0},
    }
    if with_reference:
        # A reply: the gateway payload carries message_reference, which the
        # 2.7.1 Message stores as .reference (NOT .message_reference).
        data["message_reference"] = {
            "message_id": str(_MID[0] - 1),
            "channel_id": str(ch.id),
            "guild_id": str(GUILD),
        }

    msg = discord.Message(state=guild._state, channel=ch, data=data)
    # In production the gateway cache has already resolved the author to a
    # Member; do the same so every downstream attribute read is the real one.
    msg.author = guild.real_member(AUTHOR_ID)
    return msg, ch, guild


def sent_text():
    return " | ".join(str(s[1])[:90] for s in SENT_LOG)


def install_probe_send():
    """p0_env's recorder only keeps ``content``; the kick confirmation is an
    embed-only send, so this probe records the embed title too."""
    from discord.ext.commands import Context

    async def probe_send(self, content=None, **kw):
        emb = kw.get("embed")
        if content is None and emb is not None:
            content = f"{emb.title} {emb.description}".strip()
        SENT_LOG.append(("ctx.send", content))
        return None

    Context.send = probe_send


async def dispatch(bot, content, **kw):
    SENT_LOG.clear()
    msg, ch, _ = make_real_message(content, **kw)
    bot.dispatch("message", msg)
    await asyncio.sleep(0.6)
    return msg


async def seed(db):
    await db.execute("""
        INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
        VALUES (?, 'kick', 1, '["k"]')
        ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["k"]', enabled=1
    """, (GUILD,))
    await db.execute("""
        INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
        VALUES (?, 'trigger_add', 1, '["ta"]')
        ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["ta"]', enabled=1
    """, (GUILD,))
    # can_moderate() short-circuits on the dashboard owner, so a real
    # Member with an empty role list can still kick in the probe.
    await db.execute("""
        INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled)
        VALUES (?, ?, 'owner', 1)
    """, (GUILD, AUTHOR_ID))
    await db.commit()


async def main():
    discord.Member.kick = _recorded_kick
    fresh_db()
    await init_database()
    async with aiosqlite.connect(DB_PATH) as db:
        await seed(db)
    patch_context_send()
    install_probe_send()
    bot = make_bot()
    for ext in ("cogs.moderation", "cogs.triggers",
                "cogs.command_aliases"):
        await bot.load_extension(ext)

    print("\n=== 0. the root cause, documented ===")
    check("discord.Message has NO `message_reference` attribute (2.7.1 renamed it)",
          not hasattr(discord.Message, "message_reference"),
          f"discord.py {discord.__version__}")
    m = make_real_message("attr surface check")[0]
    check("…but it DOES have `reference` (instance attribute)",
          hasattr(m, "reference") and not hasattr(m, "message_reference"))

    print("\n=== 1. a real Message runs the alias (the live `b` crash) ===")
    KICKS.clear()
    msg = await dispatch(bot, f"k <@{TARGET_ID}> being rude")
    check("no AttributeError / error banner on a real message",
          not any("Couldn't run" in str(s[1]) for s in SENT_LOG), sent_text()[:140])
    check("a REAL discord.Message object drove the whole path",
          isinstance(msg, discord.Message) and type(msg).__name__ == "Message",
          type(msg).__name__)
    check("bare alias `k` on a real message ran the real /kick callback",
          bool(KICKS) and KICKS[0][0] == TARGET_ID, KICKS)
    check("trailing text arrived as `reason`",
          bool(KICKS) and "being rude" in str(KICKS[0][1]),
          KICKS[0][1] if KICKS else None)
    check("the kick confirm was sent to the channel",
          any("kicked" in str(s[1]).lower() for s in SENT_LOG), sent_text()[:140])

    print("\n=== 2. real Message with a live reference (a reply) ===")
    msg2 = await dispatch(bot, "ta hello some long response text",
                          with_reference=True)
    ref = getattr(msg2, "reference", None)
    check("the real message carried a MessageReference",
          isinstance(ref, discord.MessageReference), type(ref).__name__)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT response_text FROM triggers WHERE trigger_words='hello' "
            "AND guild_id=?", (GUILD,))
        rows = await cur.fetchall()
    check("no AttributeError / error banner when a reference is present",
          not any("Couldn't run" in str(s[1]) for s in SENT_LOG), sent_text()[:140])
    check("/trigger_add ran from the real message",
          bool(rows), [r[0] for r in rows])
    check("trailing words went into `response`, not lost",
          bool(rows) and rows[0][0] == "some long response text",
          rows[0][0] if rows else None)

    print("\n=== 3. synthetic view exposes both reference names ===")
    # The whole class of this bug: a message-shaped object built from a real
    # one must offer whatever name older/newer code expects.
    from cogs.command_aliases import _SyntheticMessage
    real, ch, g = make_real_message("b @user", with_reference=True)
    syn = _SyntheticMessage(content="!b @user", author=real.author,
                            guild=real.guild, channel=real.channel, real=real)
    check("synthetic.reference mirrors the real reference",
          isinstance(syn.reference, discord.MessageReference),
          type(syn.reference).__name__)
    check("synthetic.message_reference equals synthetic.reference",
          syn.message_reference is syn.reference)

    print(f"\n{'='*60}\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", *FAILURES, sep="\n  - ")
    try:
        await bot.close()
    except Exception:
        pass
    sys.exit(1 if FAILURES else 0)


asyncio.run(main())
