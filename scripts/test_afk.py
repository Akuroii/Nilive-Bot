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

Dashboard/button pass (2026-09-23) coverage:

    exact approved cancellation text (two different emojis, no space after
        the first) with the mention-count line beneath it
    exact approved button label + custom emoji on every activation card
    persistent view (timeout=None), deterministic custom_id, re-registered
        on cog load without duplicates
    snapshot record: author_id / display name at mention time / channel_id /
        message_id / content (incl. empty content) + dedup still enforced
    dashboard toggle gates ONLY new !afk sessions: existing sessions keep
        idempotent re-run, alerts keep firing, owner can still cancel
    non-owner button click is rejected ephemerally with no data exposure
    owner panel is ephemeral, 10 per page, newest first, prev/next works
    zero-mention state answers with a clear ephemeral message
    post-dismissal click returns the exact approved session-ended text and
        snapshots are not retained
    old afk_mentions schema self-heals via guarded ALTERs, keeping old rows

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
        self.added_views: dict[str, object] = {}

    async def get_context(self, message):
        return message._ctx

    def add_view(self, view):
        # Mirrors discord.py's connection view store: one dispatcher per
        # custom_id — a duplicate registration can never create double
        # replies, the newest view simply owns the custom_id.
        for item in view.children:
            custom_id = getattr(item, "custom_id", None)
            if custom_id:
                self.added_views[custom_id] = view

    def dispatch(self, *a, **k):
        pass


class FakeInteractionResponse:
    """Records send_message / edit_message calls like a real response object."""

    def __init__(self):
        self.sent: list = []
        self.edits: list = []

    async def send_message(self, content=None, *, embed=None, ephemeral=False,
                           view=None, **kwargs):
        self.sent.append({"content": content, "embed": embed,
                          "ephemeral": ephemeral, "view": view})

    async def edit_message(self, *, embed=None, view=None, **kwargs):
        self.edits.append({"embed": embed, "view": view})


class FakeInteraction:
    def __init__(self, user, guild):
        self.user = user
        self.guild = guild
        self.response = FakeInteractionResponse()


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
    check("cancellation emojis are the approved static pair",
          afk_mod.RUBY_BACK_EMOJI == "<:Ruby_BACK:1552200382699016253>"
          and afk_mod.AQUA_WELCOME_EMOJI == "<:Aqua_Welcome:1552200384355762226>")
    check("cancellation text is the exact approved string",
          afk_mod.AFK_END_TEXT ==
          "<:Ruby_BACK:1552200382699016253>هلا، تم إلغاء الـ AFK "
          "<:Aqua_Welcome:1552200384355762226>")
    check("no space between first cancel emoji and هلا",
          afk_mod.AFK_END_TEXT.startswith("<:Ruby_BACK:1552200382699016253>هلا"))
    check("old welcome emoji fully removed from the cancel text",
          "1552174144710516776" not in afk_mod.AFK_END_TEXT)
    bot_commands = commands.Bot(
        command_prefix="!", intents=discord.Intents.default(),
        case_insensitive=True)

    @bot_commands.command(name="afk")
    async def afk(cmd_ctx, *, reason: str = None):
        pass

    check("`afk` registered", bot_commands.get_command("afk") is not None)
    check("no slash /afk is registered (prefix-only)",
          bot_commands.tree.get_command("afk") is None)
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

    # In production database.py init creates command_toggles before any cog
    # loads, so the toggle read below always has its table. This throwaway
    # DB normally initializes nothing outside cog code — mirror only the
    # column subset utils.command_gating.TOGGLE_SELECT selects.
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS command_toggles (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                allowed_roles TEXT, allowed_channels TEXT,
                owner_only INTEGER DEFAULT 0,
                cooldown_seconds INTEGER DEFAULT 0,
                bypass_cooldown_roles TEXT, error_message TEXT,
                enabled_roles TEXT, disabled_roles TEXT,
                enabled_channels TEXT, disabled_channels TEXT,
                PRIMARY KEY (guild_id, command_name)
            )""")
        await db.commit()

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

    # The activation card must carry the persistent mentions button — always,
    # including while zero mentions still exist.
    first_view = last_sent(general)["kwargs"].get("view")
    check("activation card carries the mentions button view",
          isinstance(first_view, afk_mod.AFKMentionsView))
    check("button view is persistent (timeout=None)",
          first_view.timeout is None)
    first_button = first_view.children[0]
    check("button custom_id deterministically keys guild+owner",
          first_button.custom_id == f"afk:mentions:{guild.id}:{author.id}")
    check("button label is the exact approved label",
          first_button.label == "إظهار الرسائل")
    check("button emoji is the exact approved custom emoji",
          first_button.emoji.id == 1552201663685595198)

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
    check("cancel embed uses the exact approved cancellation text",
          afk_mod.AFK_END_TEXT in cancel.description)

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

    # ── 13. snapshot record: all approved fields + dedup intact ────────────
    print(f"{BOLD}[13] mention snapshot stores the full approved record{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, general, "snapshots")
    snap1 = invoking_message(bot, guild, other, games, "first snap <@100>",
                             mid=30, mentions=[author])
    await cog.on_message(snap1)
    snap2 = invoking_message(bot, guild, third, general, "second snap <@100> !",
                             mid=31, mentions=[author])
    await cog.on_message(snap2)
    snap3 = invoking_message(bot, guild, other, games, "", mid=32,
                             mentions=[author])  # mention-only, empty content
    await cog.on_message(snap3)
    await cog.on_message(snap1)  # re-delivery: dedup must still hold

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT author_id, author_name, channel_id, content "
            "FROM afk_mentions WHERE guild_id=1 AND message_id=30 "
            "AND target_user_id=100")
        row30 = await cur.fetchone()
        cur = await db.execute(
            "SELECT message_id FROM afk_mentions "
            "WHERE guild_id=1 AND target_user_id=100 AND channel_id=10")
        row_chan = await cur.fetchone()
        cur = await db.execute(
            "SELECT content FROM afk_mentions WHERE guild_id=1 "
            "AND message_id=32 AND target_user_id=100")
        row_empty = await cur.fetchone()
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions "
            "WHERE guild_id=1 AND target_user_id=100")
        ledger_rows = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT mention_count FROM afk_state "
            "WHERE guild_id=1 AND user_id=100")
        count3 = (await cur.fetchone())[0]
    check("snapshot stores author_id", row30 is not None and row30[0] == 200)
    check("snapshot stores display name at mention time",
          row30 is not None and row30[1] == "Sara")
    check("snapshot stores channel_id", row30 is not None and row30[2] == 20)
    check("snapshot stores content",
          row30 is not None and row30[3] == "first snap <@100>")
    check("snapshot preserves message_id",
          row_chan is not None and row_chan[0] == 31)
    check("empty message content stored as empty string",
          row_empty is not None and row_empty[0] == "")
    check("re-delivery still counts once (dedup intact)", ledger_rows == 3)
    check("mention counter matches ledger rows", count3 == 3)

    # ── 14. persistent view registration lifecycle ─────────────────────────
    print(f"{BOLD}[14] persistent view registration (restart-safe){RESET}")
    check("activation registered the view with the bot",
          bot.added_views.get(f"afk:mentions:{guild.id}:{author.id}")
          is not None)
    cog_re = AFK(bot)
    await cog_re.ensure_table()
    await cog_re.register_persistent_views()
    check("cog load re-registers the live session's view",
          isinstance(bot.added_views.get(f"afk:mentions:{guild.id}:{author.id}"),
                     afk_mod.AFKMentionsView))
    n_views = len(bot.added_views)
    await cog_re.register_persistent_views()
    check("re-registration never duplicates dispatchers",
          len(bot.added_views) == n_views)

    # ── 15. owner gate + zero-state ────────────────────────────────────────
    print(f"{BOLD}[15] non-owner rejection + owner panel + zero-state{RESET}")
    live_view = afk_mod.AFKMentionsView(guild.id, author.id)
    stranger = FakeInteraction(other, guild)
    await live_view._on_click(stranger)
    rejected = stranger.response.sent[-1]
    check("non-owner interaction is rejected ephemerally",
          rejected["ephemeral"] is True)
    check("non-owner gets no snapshot embed", rejected["embed"] is None)
    check("non-owner text exposes no stored data",
          "Sara" not in (rejected["content"] or "")
          and "first snap" not in (rejected["content"] or ""))

    owner_click = FakeInteraction(author, guild)
    await live_view._on_click(owner_click)
    panel = owner_click.response.sent[-1]
    check("owner response is ephemeral", panel["ephemeral"] is True)
    check("owner gets the snapshot panel", panel["embed"] is not None)
    lines = (panel["embed"].description or "").split("\n")
    check("panel lists all 3 saved snapshots", len(lines) == 3)
    check("newest snapshot is first", "Sara" in lines[0])
    check("oldest snapshot is last", "Karim" in lines[1]
          and "second snap" in lines[1])
    check("single page attaches no pager", panel["view"] is None)

    await run_afk(other, games, None)
    zero_view = afk_mod.AFKMentionsView(guild.id, other.id)
    zero_click = FakeInteraction(other, guild)
    await zero_view._on_click(zero_click)
    zeroed = zero_click.response.sent[-1]
    check("zero-mention button still works (verifiable zero-state)",
          zeroed["content"] == afk_mod.AFK_ZERO_MENTIONS_TEXT)
    check("zero-state is ephemeral", zeroed["ephemeral"] is True)
    check("zero-state carries no embed", zeroed["embed"] is None)
    # other leaves AFK again; zero-state session is done.
    await cog.on_message(invoking_message(bot, guild, other, games, "bye",
                                          mid=33))

    # ── 16. pagination: 10/page, newest first, prev/next ──────────────────
    print(f"{BOLD}[16] pagination: 10/page, newest first, prev/next{RESET}")
    general.sent.clear()
    games.sent.clear()
    for i in range(12):
        await cog.on_message(invoking_message(
            bot, guild, other, games, f"snap {40 + i}", mid=40 + i,
            mentions=[author]))
    # 15 snapshots total now (mids 30-32 + 40-51).
    paged_view = afk_mod.AFKMentionsView(guild.id, author.id)
    click1 = FakeInteraction(author, guild)
    await paged_view._on_click(click1)
    page1 = click1.response.sent[-1]
    p1_lines = (page1["embed"].description or "").split("\n")
    check("page 1 holds exactly 10 snapshots", len(p1_lines) == 10)
    check("page 1 is newest-first", "snap 51" in p1_lines[0])
    check("page 1 ends at the 10th newest", "snap 42" in p1_lines[-1])
    check("page 1 footer pages 1 من 2",
          "صفحة 1 من 2" in (page1["embed"].footer.text or ""))
    pager = page1["view"]
    check("multi-page attaches a pager",
          isinstance(pager, afk_mod.AFKMentionsPager))
    check("pager starts on page 1", pager.page == 1)
    check("prev disabled on first page", pager._prev_button.disabled is True)
    check("next enabled with a page left", pager._next_button.disabled is False)

    next_inter = FakeInteraction(author, guild)
    await pager._on_next(next_inter)
    edit1 = next_inter.response.edits[-1]
    p2_lines = (edit1["embed"].description or "").split("\n")
    check("page 2 holds the remaining 5 snapshots", len(p2_lines) == 5)
    check("page 2 continues newest-first", "snap 41" in p2_lines[0])
    check("page 2 footer pages 2 من 2",
          "صفحة 2 من 2" in (edit1["embed"].footer.text or ""))
    check("prev enabled on page 2", pager._prev_button.disabled is False)
    check("next disabled on last page", pager._next_button.disabled is True)
    check("pager edits keep the pager attached", edit1["view"] is pager)

    prev_inter = FakeInteraction(author, guild)
    await pager._on_prev(prev_inter)
    back = prev_inter.response.edits[-1]
    check("prev returns to page 1",
          len((back["embed"].description or "").split("\n")) == 10
          and "صفحة 1 من 2" in (back["embed"].footer.text or ""))

    stranger2 = FakeInteraction(other, guild)
    await pager._on_next(stranger2)
    s2 = stranger2.response.sent[-1]
    check("pager also rejects non-owner ephemerally",
          s2["ephemeral"] is True and stranger2.response.edits == [])

    # ── 17. dashboard toggle gates ONLY new sessions (approved D5) ─────────
    print(f"{BOLD}[17] disabled AFK gates only new sessions{RESET}")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO command_toggles "
            "(guild_id, command_name, enabled) VALUES (1, 'afk', 0)")
        await db.commit()

    # Already AFK + disabled: the idempotent path wins (never the disabled msg).
    general.sent.clear()
    await run_afk(author, general, "new reason")
    idem = last_sent(general)
    check("already-AFK re-run stays idempotent while disabled",
          "بالفعل في وضع AFK" in (idem["content"] or ""))
    check("idempotent reply is not the disabled message",
          idem["content"] != afk_mod.AFK_DISABLED_TEXT)

    # Alerts keep firing for the existing session while disabled.
    alerts_before = count_of(games, "في وضع AFK")
    await cog.on_message(invoking_message(
        bot, guild, third, games, "ping <@100>", mid=60, mentions=[author]))
    check("mention alerts continue while disabled",
          count_of(games, "في وضع AFK") == alerts_before + 1)

    # Owner cancels normally while disabled.
    await cog.on_message(invoking_message(
        bot, guild, author, games, "back", mid=61))
    canceled = last_sent(games)["embed"]
    check("existing owner still cancels while disabled",
          afk_mod.AFK_END_TEXT in (canceled.description or ""))
    check("cancel still reports the count (16)",
          "**عدد المنشن أثناء غيابه: 16**" in (canceled.description or ""))

    # Actually disabled now blocks only NEW sessions.
    general.sent.clear()
    await run_afk(author, general, "should be blocked")
    blocked = last_sent(general)
    check("new session blocked with the exact approved message",
          blocked["content"] == "الـ AFK معطّل حاليًا في هذا السيرفر.")
    check("block is a plain message, not an AFK card",
          blocked["embed"] is None)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_state WHERE guild_id=1 AND user_id=100")
        no_session = (await cur.fetchone())[0]
    check("no new session row was created", no_session == 0)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO command_toggles "
            "(guild_id, command_name, enabled) VALUES (1, 'afk', 1)")
        await db.commit()
    await run_afk(author, general, "live again")
    check("re-enabled AFK starts sessions again",
          "لن يتمكن أحد من منشنك الآن"
          in (last_sent(general)["embed"].description or ""))
    await cog.on_message(invoking_message(
        bot, guild, author, games, "cleanup", mid=62))

    # ── 18. dismissal behaviour: deletion + ghost-click ───────────────────
    print(f"{BOLD}[18] post-dismissal button behaviour{RESET}")
    general.sent.clear()
    await run_afk(author, general, "exit test")
    dead_view = last_sent(general)["kwargs"]["view"]
    await cog.on_message(invoking_message(
        bot, guild, other, games, "see ya <@100>", mid=70,
        mentions=[author]))
    await cog.on_message(invoking_message(
        bot, guild, author, games, "done here", mid=71))
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions "
            "WHERE guild_id=1 AND target_user_id=100")
        retained = (await cur.fetchone())[0]
    check("snapshots are not retained after dismissal", retained == 0)

    ghost = FakeInteraction(author, guild)
    await dead_view._on_click(ghost)
    ended = ghost.response.sent[-1]
    check("post-dismissal click returns the exact approved message",
          ended["content"] ==
          "انتهت جلسة الـ AFK هذه، ولا توجد رسائل محفوظة لعرضها.")
    check("post-dismissal click is ephemeral", ended["ephemeral"] is True)
    check("post-dismissal click never resurrects old snapshots",
          ended["embed"] is None
          and "see ya" not in (ended["content"] or ""))

    # ── 19. old afk_mentions schema self-heals ─────────────────────────────
    print(f"{BOLD}[19] old afk_mentions schema self-heals{RESET}")
    heal_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    heal_tmp.close()
    old_path = heal_tmp.name
    async with aiosqlite.connect(old_path) as db:
        await db.execute("""
            CREATE TABLE afk_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                started_at TEXT NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
        # The pre-pass table: no snapshot columns at all.
        await db.execute("""
            CREATE TABLE afk_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, message_id, target_user_id)
            )""")
        await db.execute(
            "INSERT INTO afk_mentions (guild_id, message_id, target_user_id)"
            " VALUES (1, 555, 100)")
        await db.commit()
    saved_db_path = afk_mod.DB_PATH
    afk_mod.DB_PATH = old_path  # ensure_table reads module-level DB_PATH
    try:
        await AFK(bot).ensure_table()
    finally:
        afk_mod.DB_PATH = saved_db_path
    async with aiosqlite.connect(old_path) as db:
        cur = await db.execute("PRAGMA table_info(afk_mentions)")
        cols = {r[1] for r in await cur.fetchall()}
        cur = await db.execute(
            "SELECT guild_id, message_id, target_user_id,"
            "       author_name, content FROM afk_mentions")
        legacy = await cur.fetchone()
    os.unlink(old_path)
    check("self-heal adds author_id", "author_id" in cols)
    check("self-heal adds author_name", "author_name" in cols)
    check("self-heal adds channel_id", "channel_id" in cols)
    check("self-heal adds content", "content" in cols)
    check("self-heal keeps the existing ledger row",
          legacy is not None
          and legacy[0] == 1 and legacy[1] == 555 and legacy[2] == 100)
    check("legacy rows get safe defaults for the new columns",
          legacy is not None and legacy[3] == "" and legacy[4] == "")


if __name__ == "__main__":
    main()
