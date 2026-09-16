#!/usr/bin/env python3
"""
Application-emoji check glyph — verification suite.

The Missions completion line (`⤷ `reward claimed` <reward> <check>`) and
every other success surface render CHECK_EMOJI, which is an APPLICATION
emoji: `<a:check:1549593658867712090>`, added in Developer Portal →
Application → Emoji and owned by the bot application, not by any server.

The bug this suite locks down: reachability was probed with
`bot.application_emojis()` (an API discord.py 2.x does not have) and
`bot.get_emoji()` (the GUILD emoji cache, which by design cannot see
application emojis — discord.py's own `Client.emojis` docstring says so).
Both missed, so the probe concluded "unreachable" and silently downgraded
the Missions panel to a unicode ✅ in every server, even though the emoji
was perfectly usable.

Covered here:
   1.  The constant is the application emoji token, and parses.
   2.  A bot whose ONLY emoji surface is the guild cache (emoji absent
       from every server) still renders the application emoji — never ✅.
   3.  A real, unlogged discord.py 2.7.1 Client behaves the same.
   4.  Discord's application-emoji endpoint confirms the emoji → token,
       state CONFIRMED, taken from the API's own name/animated flag.
   5.  The list-only API path (`fetch_application_emojis`, raw payload
       shape included) confirms it too.
   6.  A genuine 404 (endpoint AND list agree the id isn't ours) is the
       ONLY thing that may downgrade to ✅.
   7.  A 404 contradicted by the list is treated as confirmed.
   8.  Inconclusive probes (500, rate limit, not logged in, no API,
       exception thrown, a probe that runs past its timeout) keep the
       token — no silent downgrade.
   9.  Probing is throttled: one HTTP call, cached; force re-asks; a
       CONFIRMED verdict is terminal; a MISSING verdict survives an
       inconclusive re-probe.
  10.  The Missions renderer end-to-end: the completed-mission block and
       the whole /missions embed carry the application emoji, not ✅ —
       including with a bot that has no guild emoji match — plus
       Missions.on_ready's startup log, which used to announce "check
       emoji is not reachable" on every single boot.
  11.  Unrelated ✅ checkmarks in the codebase are untouched (the welcome
       rules button keeps its own literal).
  12.  The dashboard's HTML twin resolves the same id off Discord's CDN
       (a browser cannot render `<a:…>` markup), not the raw token.

Run:
  python3 scripts/test_check_emoji.py
(needs the bot's own deps — discord.py, aiosqlite — same as
requirements.txt; no network and no Discord token: every "HTTP" call is a
fake that records how often it was invoked.)
"""
import io
import os
import sys
import time
import asyncio
import tempfile
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="check_emoji_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "check_emoji.db")
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import discord  # noqa: E402
from database import init_db  # noqa: E402
import utils.mission_engine as me  # noqa: E402
import utils.reward_engine  # noqa: E402
import utils.emoji as E  # noqa: E402
from utils.emoji import (  # noqa: E402
    CHECK_EMOJI, CHECK_EMOJI_ID, CHECK_EMOJI_FALLBACK,
    CHECK_STATE_CONFIRMED, CHECK_STATE_MISSING, CHECK_STATE_UNKNOWN,
    check_emoji_detail, check_emoji_state,
    resolve_check_emoji, verify_check_emoji,
)

GUILD = 7700
USER = 8800

_passed = 0
_failed = 0
_failures = []


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[92mPASS\033[0m  {label}")
    else:
        _failed += 1
        _failures.append(label)
        print(f"  \033[91mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


def section(title):
    print(f"\n\033[1m{title}\033[0m")


def reset_state():
    """Back to a cold process: nothing probed, no verdict."""
    E._check_state["state"] = CHECK_STATE_UNKNOWN
    E._check_state["token"] = CHECK_EMOJI
    E._check_state["detail"] = "not probed yet"
    E._check_state["probed_at"] = 0.0


# ── fakes ──────────────────────────────────────────────────────────────
def app_emoji(name="check", animated=True, emoji_id=CHECK_EMOJI_ID):
    """Exactly what discord.py builds for an application emoji:
    `Emoji(guild=Object(0), state=…, data=payload)` — no guild, no roles."""
    return discord.Emoji(guild=discord.Object(0), state=None, data={
        "id": str(emoji_id), "name": name, "animated": animated,
        "require_colons": True, "managed": False, "available": True,
    })


class _Resp:
    def __init__(self, status, reason=""):
        self.status = status
        self.reason = reason or f"HTTP {status}"
        self.headers = {}


def not_found():
    return discord.NotFound(_Resp(404, "Not Found"),
                            {"message": "Unknown Emoji", "code": 10014})


def server_error():
    return discord.HTTPException(_Resp(500, "Internal Server Error"),
                                 "boom")


class FakeBot:
    """A logged-in client with configurable emoji surfaces.

    `guild_emoji` reproduces the real gateway cache (which never contains
    an application emoji); `app_single` / `app_list` reproduce the two
    application-emoji endpoints. Every call is counted so the throttling
    rules can be asserted.
    """

    def __init__(self, *, guild_emoji=None, app_single="missing",
                 app_list=None, logged_in=True, drop_single=False,
                 drop_list=False):
        self.application_id = 1234567890 if logged_in else None
        self._guild_emoji = guild_emoji
        self._app_single = app_single        # Emoji | "missing" | Exception
        self._app_list = app_list            # list | dict | Exception | None
        self.single_calls = 0
        self.list_calls = 0
        self.get_emoji_calls = 0
        if not drop_single:
            self.fetch_application_emoji = self._fetch_one
        if not drop_list and app_list is not None:
            self.fetch_application_emojis = self._fetch_all

    def get_emoji(self, emoji_id):
        """The GUILD emoji cache. discord.py: "This does not include the
        emojis that are owned by the application"."""
        self.get_emoji_calls += 1
        if self._guild_emoji is not None and self._guild_emoji.id == emoji_id:
            return self._guild_emoji
        return None

    async def _fetch_one(self, emoji_id):
        self.single_calls += 1
        if isinstance(self._app_single, Exception):
            raise self._app_single
        if self._app_single == "missing":
            raise not_found()
        return self._app_single

    async def _fetch_all(self):
        self.list_calls += 1
        if isinstance(self._app_list, Exception):
            raise self._app_list
        return self._app_list


# ═══════════════════════════════════════════════════════════════════
def constant_tests():
    section("1. The constant IS the application emoji")
    check("CHECK_EMOJI is the locked application emoji token",
          CHECK_EMOJI == "<a:check:1549593658867712090>", CHECK_EMOJI)
    check("CHECK_EMOJI_ID matches the token's id",
          str(CHECK_EMOJI_ID) == "1549593658867712090")
    parsed = E.parse_emoji_input(CHECK_EMOJI)
    check("token parses as an animated custom emoji",
          parsed is not None and parsed[0] == str(CHECK_EMOJI_ID)
          and parsed[1] == "check" and parsed[2] is True, str(parsed))
    check("the unicode fallback is still ✅ (last resort only)",
          CHECK_EMOJI_FALLBACK == "✅")


def cold_start_tests():
    section("2/3. Nothing probed yet → the token, never a silent ✅")
    reset_state()
    check("cold process resolves to the application emoji",
          resolve_check_emoji() == CHECK_EMOJI, resolve_check_emoji())
    check("cold state is 'unknown', not 'missing'",
          check_emoji_state() == CHECK_STATE_UNKNOWN, check_emoji_state())

    # A real discord.py 2.7.1 client, not logged in: no application-emoji
    # cache exists on the class at all, and the guild cache is empty.
    real = discord.Client(intents=discord.Intents.default())
    check("discord.py 2.7.1 has no Client.application_emojis() cache "
          "(the probe the old code relied on)",
          not hasattr(real, "application_emojis"))
    check("real client's guild cache does not hold the application emoji",
          real.get_emoji(CHECK_EMOJI_ID) is None)
    check("real, unlogged client still renders the application emoji",
          resolve_check_emoji(real) == CHECK_EMOJI, resolve_check_emoji(real))
    check("verifying an unlogged client is inconclusive, not 'missing'",
          asyncio.run(verify_check_emoji(real)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_UNKNOWN,
          check_emoji_detail())
    check("no bot at all is inconclusive too (never raises)",
          asyncio.run(verify_check_emoji(None)) == CHECK_EMOJI)


def guild_only_tests():
    section("2. Absent from EVERY guild emoji list → still the app emoji")
    reset_state()
    # The exact shape of the reported bug: the emoji is not a guild emoji
    # anywhere, and (in 2.x) there is no app-emoji cache to find it in.
    bot = FakeBot(guild_emoji=None, app_single="missing", app_list=[],
                  drop_single=True, drop_list=True)
    check("guild-cache-only probe finds nothing",
          bot.get_emoji(CHECK_EMOJI_ID) is None)
    resolved = asyncio.run(verify_check_emoji(bot))
    check("no application-emoji API at all → inconclusive, keeps the token",
          resolved == CHECK_EMOJI, f"{resolved} / {check_emoji_detail()}")
    check("state stays 'unknown' (guild absence proves nothing)",
          check_emoji_state() == CHECK_STATE_UNKNOWN, check_emoji_state())
    check("render path therefore emits the application emoji",
          resolve_check_emoji(bot) == CHECK_EMOJI)
    check("the reason names the missing API, not a missing guild emoji",
          "application-emoji API" in check_emoji_detail(),
          check_emoji_detail())

    # Some servers do carry unrelated guild emoji; that must not matter.
    reset_state()
    other = discord.Emoji(guild=discord.Object(1), state=None, data={
        "id": "1549206183498481714", "name": "imagePhotoroom17",
        "animated": False})
    bot2 = FakeBot(guild_emoji=other, app_single=app_emoji(), app_list=None,
                   drop_list=True)
    check("a guild emoji the bot CAN see does not shadow the app emoji",
          asyncio.run(verify_check_emoji(bot2)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED,
          check_emoji_detail())
    check("guild cache was consulted but the confirmation came from the "
          "application endpoint",
          bot2.get_emoji_calls >= 1 and bot2.single_calls == 1,
          f"guild={bot2.get_emoji_calls} single={bot2.single_calls}")

    # Same id present as a guild emoji (hypothetical) confirms for free.
    reset_state()
    bot3 = FakeBot(guild_emoji=app_emoji(), app_single="missing",
                   app_list=None, drop_single=True, drop_list=True)
    check("a cached emoji with the same id confirms without any HTTP call",
          asyncio.run(verify_check_emoji(bot3)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED
          and bot3.single_calls == 0 and bot3.list_calls == 0,
          check_emoji_detail())


def endpoint_tests():
    section("4/5. Discord's application-emoji endpoint confirms it")
    reset_state()
    bot = FakeBot(app_single=app_emoji(), app_list=None, drop_list=True)
    check("single-emoji GET confirms → token",
          asyncio.run(verify_check_emoji(bot)) == CHECK_EMOJI)
    check("state is CONFIRMED", check_emoji_state() == CHECK_STATE_CONFIRMED,
          check_emoji_state())
    check("the reason says it is application-owned, not guild-owned",
          "application" in check_emoji_detail().lower(), check_emoji_detail())
    check("one HTTP call, and the list endpoint was not needed",
          bot.single_calls == 1 and bot.list_calls == 0,
          f"single={bot.single_calls} list={bot.list_calls}")

    # The name comes from Discord, so a Portal rename stays canonical.
    reset_state()
    renamed = FakeBot(app_single=app_emoji(name="check_mark"),
                      app_list=None, drop_list=True)
    check("a renamed application emoji resolves to Discord's own token",
          asyncio.run(verify_check_emoji(renamed)) ==
          "<a:check_mark:1549593658867712090>",
          asyncio.run(verify_check_emoji(renamed)))

    # A static (non-animated) application emoji must lose the `a:` prefix.
    reset_state()
    static = FakeBot(app_single=app_emoji(animated=False), app_list=None,
                     drop_list=True)
    check("a static application emoji renders with <:name:id>",
          asyncio.run(verify_check_emoji(static)) ==
          f"<:check:{CHECK_EMOJI_ID}>", asyncio.run(verify_check_emoji(static)))

    # List-only client (older/fork API surface), incl. the raw payload.
    reset_state()
    list_only = FakeBot(app_single=None, app_list=[app_emoji()],
                        drop_single=True)
    check("list-only client confirms via fetch_application_emojis",
          asyncio.run(verify_check_emoji(list_only)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED
          and list_only.list_calls == 1, check_emoji_detail())
    reset_state()
    raw_payload = FakeBot(app_single=None,
                          app_list={"items": [app_emoji()]},
                          drop_single=True)
    check("the raw {'items': [...]} payload shape is handled too",
          asyncio.run(verify_check_emoji(raw_payload)) == CHECK_EMOJI,
          check_emoji_detail())
    # ~2,000 application emoji: the id must be found anywhere in the list.
    reset_state()
    many = [app_emoji(name=f"e{i}", emoji_id=CHECK_EMOJI_ID + 1 + i)
            for i in range(1999)]
    many.insert(137, app_emoji())
    big = FakeBot(app_single=None, app_list=many, drop_single=True)
    check("found among ~2,000 application emoji (large-emoji app)",
          asyncio.run(verify_check_emoji(big)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED,
          check_emoji_detail())

    # The real discord.py Client with only the HTTP call stubbed — the
    # production code path, attribute discovery and all. This is the shape
    # that used to fail: a real client has no application_emojis() cache
    # and its get_emoji() only sees guild emoji.
    reset_state()
    real = discord.Client(intents=discord.Intents.default())
    real._connection.application_id = 1234567890     # what login() sets
    asked = []

    async def fake_fetch(emoji_id):
        asked.append(emoji_id)
        return app_emoji()

    real.fetch_application_emoji = fake_fetch
    check("a real discord.py Client confirms through fetch_application_emoji",
          asyncio.run(verify_check_emoji(real)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED
          and asked == [CHECK_EMOJI_ID],
          f"{check_emoji_detail()} asked={asked}")
    check("…and the render path then emits the application emoji",
          resolve_check_emoji(real) == CHECK_EMOJI)

    reset_state()
    real2 = discord.Client(intents=discord.Intents.default())
    real2._connection.application_id = 1234567890

    async def fake_404(emoji_id):
        raise not_found()

    async def fake_list():
        return []

    real2.fetch_application_emoji = fake_404
    real2.fetch_application_emojis = fake_list
    check("a real Client whose app really lacks the emoji → MISSING → ✅",
          asyncio.run(verify_check_emoji(real2)) == CHECK_EMOJI_FALLBACK
          and check_emoji_state() == CHECK_STATE_MISSING,
          check_emoji_detail())


def missing_tests():
    section("6/7. Only a real 404 may downgrade to ✅")
    reset_state()
    gone = FakeBot(app_single="missing", app_list=[], )
    check("endpoint 404 + list without the id → MISSING",
          asyncio.run(verify_check_emoji(gone)) == CHECK_EMOJI_FALLBACK
          and check_emoji_state() == CHECK_STATE_MISSING,
          f"{check_emoji_state()} / {check_emoji_detail()}")
    check("the reason mentions the application's emoji list",
          "application" in check_emoji_detail().lower(),
          check_emoji_detail())
    check("resolve() keeps returning ✅ while the verdict stands",
          resolve_check_emoji(gone) == CHECK_EMOJI_FALLBACK)

    # A 404 the list contradicts: trust the list (two opinions before a
    # user-visible downgrade).
    reset_state()
    flaky = FakeBot(app_single="missing", app_list=[app_emoji()])
    check("404 on the single GET but present in the list → CONFIRMED",
          asyncio.run(verify_check_emoji(flaky)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED
          and flaky.single_calls == 1 and flaky.list_calls == 1,
          check_emoji_detail())

    # 404 + list unavailable → inconclusive, so NO downgrade.
    reset_state()
    half = FakeBot(app_single="missing", app_list=server_error())
    check("404 with a failing second opinion → inconclusive, keeps token",
          asyncio.run(verify_check_emoji(half)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_UNKNOWN,
          f"{check_emoji_state()} / {check_emoji_detail()}")

    # List-only client, emoji genuinely absent.
    reset_state()
    list_gone = FakeBot(app_single=None, app_list=[app_emoji(name="other",
                                                             emoji_id=1)],
                        drop_single=True)
    check("list-only client without the id → MISSING",
          asyncio.run(verify_check_emoji(list_gone)) == CHECK_EMOJI_FALLBACK,
          check_emoji_detail())


def inconclusive_tests():
    section("8. Inconclusive probes never downgrade")
    for label, bot in [
        ("HTTP 500 on the endpoint",
         FakeBot(app_single=server_error(), app_list=[app_emoji()])),
        ("rate limit / generic HTTP error",
         FakeBot(app_single=discord.HTTPException(_Resp(429, "Too Many "
                                                    "Requests"), "slow down"),
                 app_list=None, drop_list=True)),
        ("client not logged in yet (cog_load runs before bot.start)",
         FakeBot(app_single=app_emoji(), logged_in=False)),
        ("endpoint returns an unrelated emoji id",
         FakeBot(app_single=app_emoji(emoji_id=CHECK_EMOJI_ID + 1),
                 app_list=None, drop_list=True)),
        ("endpoint raises something unexpected",
         FakeBot(app_single=RuntimeError("socket closed"), app_list=None,
                 drop_list=True)),
    ]:
        reset_state()
        resolved = asyncio.run(verify_check_emoji(bot))
        check(f"{label} → token kept", resolved == CHECK_EMOJI,
              f"{resolved} / {check_emoji_detail()}")
        check(f"{label} → state not MISSING",
              check_emoji_state() != CHECK_STATE_MISSING, check_emoji_state())

    # A bot double with none of the expected attributes at all.
    reset_state()

    class Bare:
        pass

    check("a bot object with no emoji surface at all keeps the token",
          asyncio.run(verify_check_emoji(Bare())) == CHECK_EMOJI
          and resolve_check_emoji(Bare()) == CHECK_EMOJI,
          check_emoji_detail())

    # A slow probe must not eat the interaction's acknowledgement budget
    # (/missions renders before it responds, hence timeout=1.0 there).
    reset_state()

    class SlowBot(FakeBot):
        async def _fetch_one(self, emoji_id):
            self.single_calls += 1
            await asyncio.sleep(0.4)
            return app_emoji()

    slow = SlowBot(app_single=app_emoji(), app_list=None, drop_list=True)
    started = time.monotonic()
    resolved = asyncio.run(verify_check_emoji(slow, timeout=0.05))
    elapsed = time.monotonic() - started
    check("a slow probe is abandoned at its timeout, keeping the token",
          resolved == CHECK_EMOJI and elapsed < 0.3,
          f"{resolved} after {elapsed:.2f}s")
    check("a timed-out probe is inconclusive, never MISSING",
          check_emoji_state() == CHECK_STATE_UNKNOWN
          and "abandoned" in check_emoji_detail(), check_emoji_detail())
    check("the same probe without a cap completes and confirms",
          asyncio.run(verify_check_emoji(slow, force=True)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED,
          check_emoji_detail())


def throttle_tests():
    section("9. One HTTP call, cached — force re-asks")
    reset_state()
    bot = FakeBot(app_single=server_error(), app_list=None, drop_list=True)
    asyncio.run(verify_check_emoji(bot))
    first = bot.single_calls
    for _ in range(25):
        asyncio.run(verify_check_emoji(bot))
    check("25 renders after an inconclusive probe add no HTTP calls",
          bot.single_calls == first == 1,
          f"calls={bot.single_calls}")
    asyncio.run(verify_check_emoji(bot, force=True))
    check("force=True re-asks immediately", bot.single_calls == first + 1,
          f"calls={bot.single_calls}")

    reset_state()
    ok = FakeBot(app_single=app_emoji(), app_list=None, drop_list=True)
    asyncio.run(verify_check_emoji(ok))
    for _ in range(5):
        asyncio.run(verify_check_emoji(ok))
    check("a CONFIRMED verdict is terminal — no repeat calls",
          ok.single_calls == 1, f"calls={ok.single_calls}")
    asyncio.run(verify_check_emoji(ok, force=True))
    check("force=True can still re-ask after a CONFIRMED verdict",
          ok.single_calls == 2, f"calls={ok.single_calls}")

    # Stale inconclusive/negative verdicts do get re-asked once the TTL
    # passes, so adding the emoji in the Portal self-heals without a
    # redeploy.
    reset_state()
    late = FakeBot(app_single="missing", app_list=[])
    asyncio.run(verify_check_emoji(late))
    check("first probe: 404 and absent from the list → MISSING",
          check_emoji_state() == CHECK_STATE_MISSING, check_emoji_detail())
    check("MISSING renders the unicode fallback in the meantime",
          resolve_check_emoji(late) == CHECK_EMOJI_FALLBACK)
    E._check_state["probed_at"] -= (E.CHECK_REPROBE_SECONDS + 1)
    late._app_single = app_emoji()      # emoji re-added in the Portal
    late._app_list = [app_emoji()]
    check("after the re-probe window the emoji is picked up again",
          asyncio.run(verify_check_emoji(late)) == CHECK_EMOJI
          and check_emoji_state() == CHECK_STATE_CONFIRMED,
          check_emoji_detail())
    calls_before = late.single_calls
    asyncio.run(verify_check_emoji(late))
    check("a confirmed verdict needs no further HTTP calls",
          late.single_calls == calls_before, f"calls={late.single_calls}")

    # …and an inconclusive re-probe must not resurrect a MISSING verdict.
    reset_state()
    dead = FakeBot(app_single="missing", app_list=[])
    asyncio.run(verify_check_emoji(dead))
    E._check_state["probed_at"] -= (E.CHECK_REPROBE_SECONDS + 1)
    dead._app_single = server_error()
    dead._app_list = server_error()
    check("an inconclusive re-probe keeps the MISSING verdict",
          asyncio.run(verify_check_emoji(dead)) == CHECK_EMOJI_FALLBACK
          and check_emoji_state() == CHECK_STATE_MISSING,
          f"{check_emoji_state()} / {check_emoji_detail()}")


# ── Missions renderer (real engine, scratch DB) ────────────────────────
reward_calls = []


async def fake_give_reward(bot, guild_id, user_id, reward_type, **kwargs):
    reward_calls.append((guild_id, user_id, reward_type))
    return {"success": True, "reward_type": reward_type}


utils.reward_engine.give_reward = fake_give_reward


class FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.mention = f"#{name}"


class FakeGuild:
    def __init__(self, guild_id=GUILD, name="TestBot"):
        self.id = guild_id
        self._name = name
        self.me = type("Me", (), {"display_name": name})()

    def get_channel(self, cid):
        return None


async def mission_render_tests():
    section("10. The Missions completion line renders the app emoji")
    from cogs import missions as cog

    await me.ensure_tables()
    mid = await me.create_definition(
        GUILD, name="Say hello", mtype="messages", target=1,
        reward_type="coins", reward_value="5")
    await me.record_activity(None, GUILD, USER, "messages", 1)

    m = (await me.get_user_progress(GUILD, USER))[0]
    check("the test mission actually completed", bool(m["completed"]), str(m))

    # The renderer's own default, with no bot involved at all.
    block = cog._mission_block(m, FakeGuild())
    check("completed block carries the application emoji token",
          CHECK_EMOJI in block, block)
    check("completed block carries no unicode ✅",
          CHECK_EMOJI_FALLBACK not in block, block)
    check("completion line reads `⤷ `reward claimed` <reward> <check>`",
          f"⤷ `reward claimed`" in block and block.rstrip().endswith(CHECK_EMOJI),
          block.splitlines()[-1])

    # End-to-end /missions with a bot whose guild caches know nothing
    # about the emoji — the reported production shape.
    guild_bot = FakeBot(guild_emoji=None, app_single="missing", app_list=[],
                        drop_single=True, drop_list=True)
    guild_bot.get_guild = lambda gid: FakeGuild(gid)
    guild_bot.user = type("U", (), {"display_name": "GlobalBot"})()
    reset_state()
    display = await cog.build_mission_display(guild_bot, FakeGuild(), USER)
    check("display built", display is not None)
    _content, embeds = display
    desc = "\n".join(e.description or "" for e in embeds)
    check("/missions embed renders the application emoji (guild-cache-only "
          "bot)", CHECK_EMOJI in desc, desc[-160:])
    check("/missions embed renders no unicode ✅",
          CHECK_EMOJI_FALLBACK not in desc, desc[-160:])

    # Same render, but Discord has confirmed the emoji → identical output,
    # and the confirmed token is what lands in the embed.
    reset_state()
    app_bot = FakeBot(app_single=app_emoji(), app_list=None, drop_list=True)
    app_bot.get_guild = lambda gid: FakeGuild(gid)
    app_bot.user = type("U", (), {"display_name": "GlobalBot"})()
    _c2, embeds2 = await cog.build_mission_display(app_bot, FakeGuild(), USER)
    desc2 = "\n".join(e.description or "" for e in embeds2)
    check("with a confirmed application emoji the render is the same token",
          CHECK_EMOJI in desc2 and CHECK_EMOJI_FALLBACK not in desc2,
          desc2[-160:])
    check("the confirmed render made exactly one HTTP call",
          app_bot.single_calls == 1, f"calls={app_bot.single_calls}")

    # The documented last resort: emoji genuinely deleted from the app.
    reset_state()
    dead_bot = FakeBot(app_single="missing", app_list=[])
    dead_bot.get_guild = lambda gid: FakeGuild(gid)
    dead_bot.user = type("U", (), {"display_name": "GlobalBot"})()
    _c3, embeds3 = await cog.build_mission_display(dead_bot, FakeGuild(), USER)
    desc3 = "\n".join(e.description or "" for e in embeds3)
    check("a deleted application emoji degrades to ✅ (never raw token text)",
          CHECK_EMOJI_FALLBACK in desc3 and CHECK_EMOJI not in desc3,
          desc3[-160:])

    # The Refresh button re-renders through the same path.
    reset_state()
    view = cog.MissionsView(app_bot, GUILD, USER)
    check("the Missions view holds the same bot (one resolution path)",
          view.bot is app_bot)

    # Missions.on_ready — the production startup probe, and the log line
    # that used to claim the emoji was unreachable on EVERY boot (it ran
    # in cog_load, before the client had a token, and it probed caches
    # that cannot see application emojis).
    async def ready_log(bot):
        reset_state()
        instance = cog.Missions(bot)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await instance.on_ready()
        return instance, buf.getvalue().strip()

    ok_cog, log = await ready_log(FakeBot(app_single=app_emoji(),
                                          app_list=None, drop_list=True))
    check("on_ready logs the emoji as a confirmed APPLICATION emoji",
          "APPLICATION emoji" in log and CHECK_EMOJI in log
          and CHECK_EMOJI_FALLBACK not in log, log)
    check("on_ready never tells the operator to look in a guild emoji list",
          "guild" in log.lower() and "no guild emoji required" in log, log)
    check("on_ready latches, so reconnects don't re-ask Discord",
          ok_cog._check_emoji_confirmed is True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await ok_cog.on_ready()
    check("a second on_ready is silent and makes no HTTP call",
          buf.getvalue() == "", repr(buf.getvalue()))

    _cog, log = await ready_log(FakeBot(app_single="missing", app_list=[]))
    check("on_ready is honest when the application really lacks the emoji",
          "NOT owned by this application" in log
          and "Application → Emoji" in log and CHECK_EMOJI_FALLBACK in log,
          log)

    _cog, log = await ready_log(
        FakeBot(guild_emoji=None, app_single="missing", app_list=[],
                drop_single=True, drop_list=True))
    check("on_ready does NOT report 'not reachable' when it merely could "
          "not verify", "unverified" in log and "no unicode downgrade" in log
          and "not reachable" not in log, log)

    reset_state()
    check("reward grant was patched, not the real economy",
          len(reward_calls) >= 1, str(reward_calls))


def unrelated_checkmark_tests():
    section("11. Unrelated ✅ usages are untouched")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "cogs", "welcome.py"), encoding="utf-8") as f:
        welcome = f.read()
    check("the welcome rules button keeps its own literal ✅",
          '"✅ I Accept"' in welcome)
    with open(os.path.join(root, "cogs", "missions.py"), encoding="utf-8") as f:
        missions_src = f.read()
    check("Missions has no hardcoded ✅ of its own (only the shared "
          "fallback constant is referenced)",
          '"✅"' not in missions_src and "'✅'" not in missions_src,
          [l for l in missions_src.splitlines() if "✅" in l][:3])
    check("Missions renders the check glyph from utils.emoji",
          "resolve_check_emoji" in missions_src
          and "verify_check_emoji" in missions_src)
    check("Missions never re-declares the emoji token itself",
          "1549593658867712090" not in missions_src.replace(
              "CHECK_EMOJI_ID", ""))


def dashboard_tests():
    section("12. Dashboard HTML twin (browser can't render <a:…> markup)")
    from dashboard.utils.check_icon import check_icon_html
    html = str(check_icon_html())
    check("dashboard check icon is an <img>, not the raw Discord token",
          "<img" in html and CHECK_EMOJI not in html, html)
    check("it points at Discord's CDN for the SAME emoji id",
          str(CHECK_EMOJI_ID) in html and "cdn.discordapp.com/emojis/" in html,
          html)
    check("animated application emoji → the .gif CDN asset",
          html.rstrip().endswith(".gif") is False and ".gif" in html, html)
    check("the unicode ✅ survives only as alt text / onerror fallback",
          f'alt="{CHECK_EMOJI_FALLBACK}"' in html
          and CHECK_EMOJI_FALLBACK in html, html)
    check("cdn url helper: animated → gif, static → png",
          E.emoji_cdn_url(CHECK_EMOJI_ID, True).endswith(".gif")
          and E.emoji_cdn_url(CHECK_EMOJI_ID, False).endswith(".png"))


# ═══════════════════════════════════════════════════════════════════
def main():
    # Every suite except the Missions renderer is synchronous on purpose:
    # each drives verify_check_emoji() through asyncio.run(), which cannot
    # be nested inside an already-running loop. The renderer suite needs
    # the scratch DB and gets one loop of its own.
    asyncio.run(init_db())
    constant_tests()
    cold_start_tests()
    guild_only_tests()
    endpoint_tests()
    missing_tests()
    inconclusive_tests()
    throttle_tests()
    asyncio.run(mission_render_tests())
    unrelated_checkmark_tests()
    dashboard_tests()
    reset_state()

    print(f"\n{'='*60}")
    if _failed:
        print(f"RESULT: {_passed} passed, {_failed} FAILED")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"RESULT: all {_passed} checks passed")


if __name__ == "__main__":
    main()
