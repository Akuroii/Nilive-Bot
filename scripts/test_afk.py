"""Offline (no-network) tests for cogs/afk.py.

Runs against a throwaway SQLite file via DATABASE_PATH, with fake users,
guild, channels and messages. No Discord connection is made. Covers:

    !afk with no reason / with a reason (spaces preserved)
    case-insensitive resolution (!AFK / !Afk -> same command)
    mention counting incl. idempotency + several mentions + self-mention no-op
    author message in a DIFFERENT channel cancels AFK + reports count
    bot messages never cancel AFK
    0-mention cancel and multi-mention cancel
    re-running !afk while AFK is idempotent (reason/started_at/count unchanged)
    !afk without reason while already AFK does not erase the current reason
    no afk_history table exists (audit removed)
    persistence: a second cog instance (fresh process view) sees the state

Run:  DATABASE_PATH=/tmp/afk_test.db OWNER_ID=123 python3 scripts/test_afk.py
"""

import asyncio
import inspect
import os
import sys
import tempfile

# Path the repo root so `cogs.afk` / `database` import cleanly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402


class FakeChannel:
    def __init__(self, cid: int):
        self.id = cid
        self.sent: list = []

    async def send(self, content=None, *, embed=None, **kwargs):
        self.sent.append({"content": content, "embed": embed, "kwargs": kwargs})
        return None


class FakeGuild:
    def __init__(self, gid: int):
        self.id = gid
        self.name = f"Guild {gid}"
        self._members = {}
        self._channels = {}

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_member(self, uid):
        return self._members.get(uid)


class _FakeAsset:
    def __init__(self, url):
        self.url = url


class FakeUser:
    def __init__(self, uid: int, name: str, display: str | None = None,
                 avatar_url: str | None = None, is_bot: bool = False):
        self.id = uid
        self.name = name
        self.display_name = display or name
        self.global_name = name
        self.bot = is_bot
        self._avatar = _FakeAsset(avatar_url) if avatar_url else None

    @property
    def display_avatar(self):
        return self._avatar

    @property
    def mention(self):
        return f"<@{self.id}>"

    def __str__(self):
        return self.display_name


class FakeBot:
    def __init__(self, guild: FakeGuild, users: dict[int, FakeUser]):
        self.guild = guild
        self.member_users = users
        self.user = FakeUser(999999, "Bot", "Bot", is_bot=True)

    async def get_context(self, message):
        return message._ctx

    def dispatch(self, *a, **k):
        pass


class FakeMessage:
    def __init__(self, author, guild, channel, content, mid, mentions=None,
                 ctx=None):
        self.id = mid
        self.author = author
        self.guild = guild
        self.channel = channel
        self.content = content
        self.mentions = list(mentions or [])
        self._ctx = ctx


class FakeCtx:
    def __init__(self, bot, guild, author, channel, message, command=None,
                 prefix="!"):
        self.bot = bot
        self.guild = guild
        self.author = author
        self.channel = channel
        self.message = message
        self.command = command
        self.prefix = prefix

    async def send(self, content=None, *, embed=None, **kwargs):
        self.channel.sent.append({"content": content, "embed": embed,
                                  "kwargs": kwargs})
        return None


def invoking_message(bot, guild, author, channel, content, mid, mentions=None):
    """A chat message whose get_context resolves to no command."""
    msg = FakeMessage(author, guild, channel, content, mid, mentions=mentions)
    msg._ctx = FakeCtx(bot, guild, author, channel, msg, command=None)
    return msg


def last_sent(channel):
    return channel.sent[-1] if channel.sent else None


def count_of(channel, needle):
    return sum(
        1 for s in channel.sent
        if s.get("embed") and needle in (s["embed"].description or ""))


PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


RESET = "\x1b[0m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
BOLD = "\x1b[1m"


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DATABASE_PATH"] = tmp.name
    # database.py hard-requires OWNER_ID at import time (security fix) even
    # though the AFK cog never reads it.
    if not os.getenv("OWNER_ID"):
        os.environ["OWNER_ID"] = "123456789012345678"

    asyncio.run(run(tmp.name))
    os.unlink(tmp.name)
    print()
    color = GREEN if FAIL == 0 else RED
    print(f"{BOLD}{color}{PASS}/{PASS + FAIL} passed.{RESET}")
    sys.exit(1 if FAIL else 0)


async def run(db_path):
    import aiosqlite
    from cogs.afk import AFK
    from cogs import afk as afk_mod

    print(f"{BOLD}AFK cog tests (DB: {db_path}){RESET}")

    # ── 1. command name + case-insensitivity + emoji tokens ─────────────
    print(f"{BOLD}[1] registration & case-insensitivity & emoji tokens{RESET}")
    check("AFK emoji is the animated token",
          afk_mod.AFK_EMOJI == "<a:AFK:1552170028961374259>")
    check("welcome emoji is the static token",
          afk_mod.WELCOME_EMOJI == "<:welcome:1552174144710516776>")
    bot_commands = commands.Bot(
        command_prefix="!", intents=discord.Intents.default(),
        case_insensitive=True)

    @bot_commands.command(name="afk")
    async def afk(cmd_ctx, *, reason: str = None):
        pass

    check("`afk` registered", bot_commands.get_command("afk") is not None)
    check("!AFK resolves to the same command",
          bot_commands.get_command("AFK").name == "afk")
    check("!Afk resolves to the same command",
          bot_commands.get_command("Afk").name == "afk")
    check("reason is a trailing * param",
          inspect.Parameter.KEYWORD_ONLY ==
          bot_commands.all_commands["afk"].params["reason"].kind)

    guild = FakeGuild(1)
    author = FakeUser(100, "ali", "Ali the OG",
                      "https://cdn.discordapp.com/avatars/100/a_abc.png")
    other = FakeUser(200, "sara", "Sara",
                     "https://cdn.discordapp.com/avatars/200/xyz.png")
    third = FakeUser(300, "karim", "Karim")
    botuser = FakeUser(999999, "Bot", "Bot", is_bot=True)
    users = {100: author, 200: other, 300: third, 999999: botuser}
    general = FakeChannel(10)
    games = FakeChannel(20)
    guild._channels = {10: general, 20: games}
    guild._members = users

    bot = FakeBot(guild, users)
    cog = AFK(bot)
    await cog.cog_load()
    afk_cmd = bot_commands.all_commands["afk"]

    async def run_afk(member, channel, reason):
        msg = FakeMessage(member, guild, channel, "!afk",
                          mid=900000 + member.id)
        ctx = FakeCtx(bot, guild, member, channel, msg, command=afk_cmd)
        msg._ctx = ctx
        await cog._run_afk(ctx, reason)
        return ctx, msg

    # ── 2. activation with a reason ──────────────────────────────────────
    print(f"{BOLD}[2] !afk with a reason{RESET}")
    await run_afk(author, general, "يتم اخذ راحة من البشر")
    emb = last_sent(general)["embed"]
    check("responds in the invoking channel", len(general.sent) == 1)
    check("card author is the username", emb.author.name == "Ali the OG")
    check("card author carries the avatar",
          emb.author.icon_url ==
          "https://cdn.discordapp.com/avatars/100/a_abc.png")
    check("AFK text present", "لن يتمكن أحد من منشنك الآن" in emb.description)
    check("reason line present",
          "**السبب:** يتم اخذ راحة من البشر" in emb.description)

    # ── 3/4. mention counting + idempotency ──────────────────────────────
    print(f"{BOLD}[3-4] mention counting + no double count{RESET}")
    m1 = invoking_message(bot, guild, other, games, "hey <@100>", mid=1,
                          mentions=[author])
    await cog.on_message(m1)
    check("alert sent in the mentioner's channel",
          count_of(games, "في وضع AFK") == 1)
    alert = last_sent(games)["embed"]
    check("alert embeds the reason",
          "**السبب:** يتم اخذ راحة من البشر" in alert.description)
    check("alert uses a real mention", "<@100>" in alert.description)

    await cog.on_message(m1)  # re-delivery of the SAME message
    check("same message re-delivery does not double count",
          count_of(games, "في وضع AFK") == 1)

    m2 = invoking_message(bot, guild, other, games, "<@100> again", mid=2,
                          mentions=[author])
    await cog.on_message(m2)
    check("a second, distinct message counts again",
          count_of(games, "في وضع AFK") == 2)

    # ── bot messages are ignored entirely ────────────────────────────────
    print(f"{BOLD}[5] bot messages never touch AFK{RESET}")
    bm = invoking_message(bot, guild, botuser, games, "i am a bot", mid=3)
    await cog.on_message(bm)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT mention_count FROM afk_state "
                               "WHERE guild_id=1 AND user_id=100")
        row = await cur.fetchone()
    check("author still AFK with untouched count", row is not None and row[0] == 2)

    # ── self-mention: own message cancels, and no self alert ─────────────
    print(f"{BOLD}[6] self-mention never loops; own message cancels{RESET}")
    s1 = invoking_message(bot, guild, author, games, "talking <@100>", mid=4,
                          mentions=[author])
    await cog.on_message(s1)
    check("no AFK-mention alert for self",
          count_of(games, "في وضع AFK") == 2)
    check("author's own message cancels AFK",
          count_of(games, "تم إلغاء الـ AFK") == 1)
    cancel = last_sent(games)["embed"]
    check("cancel reports the 2 mentions counted",
          "**عدد المنشن أثناء غيابه: 2**" in cancel.description)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM afk_state "
                               "WHERE guild_id=1 AND user_id=100")
        n = (await cur.fetchone())[0]
    check("state row removed", n == 0)

    # ── cancel from a DIFFERENT channel than activation ──────────────────
    print(f"{BOLD}[7] cancels from a different channel{RESET}")
    await run_afk(author, general, "away from everything")
    games.sent.clear()
    moff = invoking_message(bot, guild, other, games, "yo <@100>", mid=5,
                            mentions=[author])
    await cog.on_message(moff)
    check("one mention recorded while AFK in #games",
          count_of(games, "في وضع AFK") == 1)
    back = invoking_message(bot, guild, author, games, "hello everyone", mid=6)
    await cog.on_message(back)
    check("cancelled by a message in #games (not the activation channel)",
          count_of(games, "تم إلغاء الـ AFK") == 1)
    cancel = last_sent(games)["embed"]
    check("count reported correctly",
          "**عدد المنشن أثناء غيابه: 1**" in cancel.description)
    check("cancel embed uses the AFK+on text with welcome emoji",
          "<:welcome:1552174144710516776>" in cancel.description
          and "تم إلغاء الـ AFK" in cancel.description)

    # ── no-reason AFK: activation, 0-mention cancel, then no-reason alert ──
    print(f"{BOLD}[8] no reason + 0 mentions{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, general, None)
    emb = last_sent(general)["embed"]
    check("no reason line when no reason",
          "**السبب:**" not in emb.description)
    # No one mentions them: cancel and expect exactly zero.
    back0 = invoking_message(bot, guild, author, general, "im back", mid=8)
    await cog.on_message(back0)
    check("zero-mention cancel reports 0",
          "**عدد المنشن أثناء غيابه: 0**" in last_sent(general)["embed"].description)

    # No-reason AFK again, but this time someone mentions them: the alert
    # must hide the reason line, and the cancel must report that 1 mention.
    await run_afk(author, general, None)
    mm = invoking_message(bot, guild, other, games, "psst <@100>", mid=7,
                          mentions=[author])
    await cog.on_message(mm)
    aem = last_sent(games)["embed"]
    check("no-reason alert hides the reason line",
          "**السبب:**" not in aem.description)
    back1 = invoking_message(bot, guild, author, general, "im back again", mid=13)
    await cog.on_message(back1)
    check("no-reason cancel reports the 1 mention",
          "**عدد المنشن أثناء غيابه: 1**" in last_sent(general)["embed"].description)

    # ── reason with spaces ───────────────────────────────────────────────
    print(f"{BOLD}[9] reason with spaces preserved{RESET}")
    general.sent.clear()
    await run_afk(author, general, "أخذ   راحة   طويلة")
    emb = last_sent(general)["embed"]
    check("multi-space reason kept intact",
          "**السبب:** أخذ   راحة   طويلة" in emb.description)
    await cog.on_message(invoking_message(
        bot, guild, author, games, "done", mid=9))

    # ── re-running !afk while already AFK is idempotent ──────────────────
    print(f"{BOLD}[10] re-running !afk while AFK is idempotent{RESET}")
    general.sent.clear()
    await run_afk(author, general, "first reason")

    async def afk_state_row():
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT reason, started_at, mention_count FROM afk_state "
                "WHERE guild_id=1 AND user_id=100")
            return await cur.fetchone()

    row_before = await afk_state_row()
    reason_before, started_before, count_before = row_before

    # Re-run with a DIFFERENT reason: must not change anything.
    await run_afk(author, general, "second reason")
    row = await afk_state_row()
    check("reason unchanged on re-run", row[0] == reason_before == "first reason")
    check("started_at unchanged on re-run", row[1] == started_before)
    check("mention_count unchanged on re-run", row[2] == count_before)

    reply = last_sent(general)
    check("idempotent reply is a plain message, not an AFK-on embed",
          reply["embed"] is None and reply["content"])
    check("reply says already AFK", "بالفعل في وضع AFK" in reply["content"])
    check("reply shows the current reason",
          "**السبب:** first reason" in reply["content"])

    # Re-run with NO reason: must NOT erase the existing reason.
    await run_afk(author, general, None)
    row = await afk_state_row()
    check("!afk without reason does not erase the stored reason",
          row[0] == "first reason")
    check("mention_count still intact after no-reason re-run",
          row[2] == count_before)

    # Exactly one session row, never duplicated.
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM afk_state "
                               "WHERE guild_id=1 AND user_id=100")
        n = (await cur.fetchone())[0]
    check("exactly one AFK row", n == 1)

    # afk_history must not exist at all.
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='afk_history'")
        hist = (await cur.fetchone())[0]
    check("no afk_history table exists", hist == 0)

    # Cleanup: author writes again to leave AFK.
    await cog.on_message(invoking_message(
        bot, guild, author, games, "ok", mid=10))

    # ── persistence across a fresh instance (simulated restart) ──────────
    print(f"{BOLD}[11] persistence across restart{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, general, "zzz")
    cog2 = AFK(bot)
    await cog2.cog_load()  # fresh instance = another "process" view
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT reason FROM afk_state "
                               "WHERE guild_id=1 AND user_id=100")
        row = await cur.fetchone()
    check("fresh instance sees the active AFK row",
          row is not None and row[0] == "zzz")
    off = invoking_message(bot, guild, other, games, "<@100> wake up", mid=11,
                           mentions=[author])
    await cog2.on_message(off)
    check("fresh instance counts a mention",
          count_of(games, "في وضع AFK") == 1)
    final = invoking_message(bot, guild, author, games, "morning", mid=12)
    await cog2.on_message(final)
    check("fresh instance cancels correctly",
          count_of(games, "تم إلغاء الـ AFK") == 1)
    check("fresh instance reports the persisted count",
          "**عدد المنشن أثناء غيابه: 1**" in last_sent(games)["embed"].description)

    # ── 12. full on_message path: !afk doesn't cancel, non-AFK target is
    # ignored, mentions after cancel never count ───────────────────────
    print(f"{BOLD}[12] full on_message path & non-AFK mentions{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, games, "busy")

    # A non-AFK member being mentioned produces no AFK alert.
    before = len(games.sent)
    m_non_afk = invoking_message(bot, guild, other, games, "hi <@300>", mid=20,
                                 mentions=[third])
    await cog.on_message(m_non_afk)
    check("mention of a non-AFK member -> no AFK response",
          count_of(games, "في وضع AFK") == 0 and len(games.sent) == before)

    # The !afk invocation itself, flowing through on_message, does NOT
    # cancel the session.
    afk_msg = FakeMessage(author, guild, games, "!afk", mid=21)
    afk_msg._ctx = FakeCtx(bot, guild, author, games, afk_msg,
                           command=afk_cmd)
    await cog.on_message(afk_msg)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM afk_state "
                               "WHERE guild_id=1 AND user_id=100")
        still_afk = (await cur.fetchone())[0]
    check("!afk message through on_message does NOT cancel AFK",
          still_afk == 1)

    # A real message from the author cancels; afterwards new mentions are
    # simply ignored (no AFK alerts, no counting).
    wake = invoking_message(bot, guild, author, games, "im awake", mid=22)
    await cog.on_message(wake)
    check("author's real message cancels AFK",
          count_of(games, "تم إلغاء الـ AFK") == 1)
    alerts_before = count_of(games, "في وضع AFK")
    late = invoking_message(bot, guild, other, games, "<@100> late", mid=23,
                            mentions=[author])
    await cog.on_message(late)
    check("mention after cancellation produces no AFK response",
          count_of(games, "في وضع AFK") == alerts_before)


if __name__ == "__main__":
    main()
