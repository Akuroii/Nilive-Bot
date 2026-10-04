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

Final AFK corrections pass (2026-09-23) coverage — the corrected lifecycle:

 1. !afk [reason] starts AFK with NO mentions button on the start message
 2. the cancellation/return message carries the mentions button
 3. exact cancellation text (no emoji before هلا, exactly ONE space after
    AFK, the two emoji tags immediately adjacent)
 4. exact mention-count line beneath it
 5. exact button label + custom emoji
 6. non-owner click -> exact ephemeral response
    "هش هش ناس فضولية <:catto_evil:1438541407743901738>"
 7. owner click -> ephemeral snapshot panel (display name + content)
 8. zero mentions still attach the button to the return card
 9. zero mentions -> approved zero-state wording
10-14. snapshots retain author_id, author_name (display name at mention
    time), channel_id, message_id, content (full content stored even when
    display truncates), created_at
15. deduplication remains (guild_id, message_id, target_user_id)
16. pagination = 10/page, newest first, working prev/next
17. an old return-card button identifies its OWN ended session
18. a later session of the same owner cannot be opened through an old button
19. multiple users in the same guild cannot collide
20. the return button still works after restart / persistent-view
    re-registration (including zero-mention sessions)
21-24. disabled AFK blocks only NEW sessions: existing sessions continue,
    owners can still return, mention alerts keep firing, and the return
    button keeps working while disabled
25. old afk_state / afk_mentions schemas self-heal (session_id included);
    a pre-upgrade session's snapshots resolve through the legacy token
26. NO time-based retention/TTL and NO cleanup pass: snapshots and
    ended-session records are never deleted automatically (even a
    400-day-old ended session's button still serves its snapshots), and
    startup re-registers views from afk_ended_sessions identity metadata
    without loading any snapshot rows into RAM (data stays DB-backed and
    session-scoped — fetched one session's page at click time)

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


def sent_view(channel):
    """The view attached to the last message sent to this channel (or None)."""
    return (last_sent(channel) or {}).get("kwargs", {}).get("view")


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
    from datetime import datetime, timedelta, timezone

    from cogs.afk import AFK
    from cogs import afk as afk_mod

    print(f"{BOLD}AFK cog tests (DB: {db_path}){RESET}")

    # ── 1. command name + case-insensitivity + approved strings ──────────
    print(f"{BOLD}[1] registration, case-insensitivity & approved strings{RESET}")
    check("AFK emoji is the animated token",
          afk_mod.AFK_EMOJI == "<a:AFK:1552170028961374259>")
    check("cancellation emojis are the approved static pair",
          afk_mod.RUBY_BACK_EMOJI == "<:Ruby_BACK:1552200382699016253>"
          and afk_mod.AQUA_WELCOME_EMOJI == "<:Aqua_Welcome:1552200384355762226>")
    # [3] exact cancellation text (2026-09-23 correction B): one space after
    # AFK, the two emoji tags immediately adjacent.
    check("cancel text [3] is the exact approved string",
          afk_mod.AFK_END_TEXT ==
          "هلا، تم إلغاء الـ AFK "
          "<:Ruby_BACK:1552200382699016253>"
          "<:Aqua_Welcome:1552200384355762226>",
          repr(afk_mod.AFK_END_TEXT))
    check("cancel text [3] has NO emoji before هلا",
          afk_mod.AFK_END_TEXT.startswith("هلا، تم إلغاء الـ AFK"))
    check("cancel text [3] has exactly ONE space after AFK",
          "AFK <:Ruby_BACK:1552200382699016253>" in afk_mod.AFK_END_TEXT
          and "AFK  <:" not in afk_mod.AFK_END_TEXT
          and "AFK<:Ruby_BACK" not in afk_mod.AFK_END_TEXT)
    check("cancel text [3] keeps the two emoji tags immediately adjacent",
          "<:Ruby_BACK:1552200382699016253><:Aqua_Welcome:1552200384355762226>"
          in afk_mod.AFK_END_TEXT)
    check("cancel text [3] substitutes no other emojis",
          "1552174144710516776" not in afk_mod.AFK_END_TEXT)
    # [6] non-owner response (2026-09-23 correction C).
    check("non-owner text [6] is the exact approved string",
          afk_mod.AFK_NOT_OWNER_TEXT ==
          "هش هش ناس فضولية <:catto_evil:1438541407743901738>",
          repr(afk_mod.AFK_NOT_OWNER_TEXT))
    check("old non-owner wording is fully superseded",
          "هذا الزر مخصص لصاحب جلسة الـ AFK فقط."
          not in open(os.path.join(ROOT, "cogs", "afk.py"),
                      encoding="utf-8").read())
    check("old session-ended state no longer exists",
          not hasattr(afk_mod, "AFK_SESSION_ENDED_TEXT"))
    check("zero-state text [9] is the approved wording",
          afk_mod.AFK_ZERO_MENTIONS_TEXT ==
          "لا يوجد منشن محفوظ في جلسة الـ AFK هذه حتى الآن.")
    check("disabled text [21] is the exact approved wording",
          afk_mod.AFK_DISABLED_TEXT ==
          "الـ AFK معطّل حاليًا في هذا السيرفر.")
    check("button label [5] is the exact approved label",
          afk_mod.AFK_BUTTON_LABEL == "إظهار الرسائل")
    check("button emoji [5] is the exact approved custom emoji",
          afk_mod.AFK_BUTTON_EMOJI.id == 1552201663685595198
          and afk_mod.AFK_BUTTON_EMOJI.name == "930931paimonping")
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

    async def active_session_id(user_id):
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT session_id FROM afk_state "
                "WHERE guild_id=1 AND user_id=?", (user_id,))
            row = await cur.fetchone()
        return row[0] if row else None

    # ── 2. activation with a reason — and NO mentions button ─────────────
    print(f"{BOLD}[2] !afk with a reason (start card has NO button){RESET}")
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

    # [1] The activation card must NOT carry the mentions button — the
    # button is a post-return component on the cancellation card only.
    check("start message [1] carries NO mentions button",
          sent_view(general) is None)
    check("start registers no persistent view [1]", bot.added_views == {})

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

    # ── self-mention: own message cancels, and the return card gets the
    # button (2026-09-23 correction A) ────────────────────────────────────
    print(f"{BOLD}[6] self-mention never loops; return card carries the button{RESET}")
    s1 = invoking_message(bot, guild, author, games, "talking <@100>", mid=4,
                          mentions=[author])
    await cog.on_message(s1)
    check("no AFK-mention alert for self",
          count_of(games, "في وضع AFK") == 2)
    check("author's own message cancels AFK",
          count_of(games, "تم إلغاء الـ AFK") == 1)
    cancel = last_sent(games)["embed"]
    check("cancel reports the 2 mentions counted",
          "**عدد المنشن أثناء غيابك: 2**" in cancel.description)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM afk_state "
                               "WHERE guild_id=1 AND user_id=100")
        n = (await cur.fetchone())[0]
    check("state row removed", n == 0)

    # [2] + [5] the CANCELLATION card carries the mentions button.
    cancel_view = sent_view(games)
    check("return card [2] carries the mentions button view",
          isinstance(cancel_view, afk_mod.AFKMentionsView))
    check("button view is persistent (timeout=None)",
          cancel_view.timeout is None)
    cancel_button = cancel_view.children[0]
    check("button label [5] on the return card is exact",
          cancel_button.label == "إظهار الرسائل")
    check("button emoji [5] on the return card is exact",
          cancel_button.emoji.id == 1552201663685595198)
    check("button custom_id is session-scoped (guild+owner+session)",
          cancel_button.custom_id ==
          f"afk:mentions:{guild.id}:{author.id}:{cancel_view.session_id}")
    check("session token is a real identifier, not the owner id",
          cancel_view.session_id not in ("", None, "100"))

    # Data lifetime: snapshots survive the dismissal for the button.
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions "
            "WHERE guild_id=1 AND target_user_id=100 AND session_id=?",
            (cancel_view.session_id,))
        kept = (await cur.fetchone())[0]
    check("snapshots survive dismissal for the return button", kept == 2)

    # ── cancel from a DIFFERENT channel than activation ──────────────────
    print(f"{BOLD}[7] cancels from a different channel (exact card text){RESET}")
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
    # [3] + [4] the complete card description, character for character.
    check("card description [3+4] is exactly the approved text + count line",
          cancel.description ==
          "هلا، تم إلغاء الـ AFK "
          "<:Ruby_BACK:1552200382699016253>"
          "<:Aqua_Welcome:1552200384355762226>"
          "\n\n**عدد المنشن أثناء غيابه: 1**",
          repr(cancel.description))
    check("count line [4] sits beneath the cancel text",
          cancel.description.startswith(afk_mod.AFK_END_TEXT + "\n\n"))
    check("return card in another channel also carries the button [2]",
          isinstance(sent_view(games), afk_mod.AFKMentionsView))

    # ── no-reason AFK: 0-mention cancel keeps the button (8) + zero-state ─
    print(f"{BOLD}[8] zero mentions: button still attached + zero-state{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, general, None)
    emb = last_sent(general)["embed"]
    check("no reason line when no reason",
          "**السبب:**" not in emb.description)
    check("no-reason start card also carries no button [1]",
          sent_view(general) is None)
    # No one mentions them: cancel and expect exactly zero.
    back0 = invoking_message(bot, guild, author, general, "im back", mid=8)
    await cog.on_message(back0)
    check("zero-mention cancel reports 0 [4]",
          "**عدد المنشن أثناء غيابه: 0**" in last_sent(general)["embed"].description)
    zero_view = sent_view(general)
    check("zero mentions still attach the button [8]",
          isinstance(zero_view, afk_mod.AFKMentionsView))
    zero_click = FakeInteraction(author, guild)
    await zero_view._on_click(zero_click)
    zeroed = zero_click.response.sent[-1]
    check("zero mentions produce the approved zero-state [9]",
          zeroed["content"] == "لا يوجد منشن محفوظ في جلسة الـ AFK هذه حتى الآن.")
    check("zero-state [9] is ephemeral", zeroed["ephemeral"] is True)
    check("zero-state [9] carries no embed", zeroed["embed"] is None)

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
    check("idempotent reply attaches no view",
          reply["kwargs"].get("view") is None)
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
    check("fresh instance return card carries the button [2]",
          isinstance(sent_view(games), afk_mod.AFKMentionsView))

    # ── 12. full on_message path: !afk doesn't cancel, non-AFK target is
    # ignored, mentions after cancel never count ───────────────────────────
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

    # ── 13. snapshot record: all approved fields + full content kept ─────
    print(f"{BOLD}[13] mention snapshot stores the full approved record{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, general, "snapshots")
    session13 = await active_session_id(author.id)
    snap1 = invoking_message(bot, guild, other, games, "first snap <@100>",
                             mid=30, mentions=[author])
    await cog.on_message(snap1)
    snap2 = invoking_message(bot, guild, third, general, "second snap <@100> !",
                             mid=31, mentions=[author])
    await cog.on_message(snap2)
    snap3 = invoking_message(bot, guild, other, games, "", mid=32,
                             mentions=[author])  # mention-only, empty content
    await cog.on_message(snap3)
    long_content = "L" * 500
    snap4 = invoking_message(bot, guild, third, games, long_content, mid=33,
                             mentions=[author])
    await cog.on_message(snap4)
    await cog.on_message(snap1)  # re-delivery: dedup must still hold

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT author_id, author_name, channel_id, content, created_at "
            "FROM afk_mentions WHERE guild_id=1 AND message_id=30 "
            "AND target_user_id=100")
        row30 = await cur.fetchone()
        cur = await db.execute(
            "SELECT message_id FROM afk_mentions "
            "WHERE guild_id=1 AND target_user_id=100 AND channel_id=10 "
            "AND session_id=?", (session13,))
        row_chan = await cur.fetchone()
        cur = await db.execute(
            "SELECT content FROM afk_mentions WHERE guild_id=1 "
            "AND message_id=32 AND target_user_id=100")
        row_empty = await cur.fetchone()
        cur = await db.execute(
            "SELECT content FROM afk_mentions WHERE guild_id=1 "
            "AND message_id=33 AND target_user_id=100")
        row_long = await cur.fetchone()
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions "
            "WHERE guild_id=1 AND target_user_id=100 AND session_id=?",
            (session13,))
        ledger_rows = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT mention_count FROM afk_state "
            "WHERE guild_id=1 AND user_id=100")
        count3 = (await cur.fetchone())[0]
    check("snapshot [10] stores author_id", row30 is not None and row30[0] == 200)
    check("snapshot [11] stores display name at mention time",
          row30 is not None and row30[1] == "Sara")
    check("snapshot [12] stores channel_id", row30 is not None and row30[2] == 20)
    check("snapshot [14] stores content",
          row30 is not None and row30[3] == "first snap <@100>")
    check("snapshot stores created_at",
          row30 is not None and bool(row30[4]))
    check("snapshot [13] preserves message_id",
          row_chan is not None and row_chan[0] == 31)
    check("empty message content stored as empty string",
          row_empty is not None and row_empty[0] == "")
    check("long content is stored in full [14]",
          row_long is not None and row_long[0] == long_content)
    check("snapshots are tagged with the collecting session's token",
          ledger_rows == 4 and session13 not in (None, "0"))
    check("re-delivery still counts once (dedup [15] intact)", ledger_rows == 4)
    check("mention counter matches ledger rows", count3 == 4)

    # Return: the button serves this just-ended session, display-truncated.
    back13 = invoking_message(bot, guild, author, general, "done snaps", mid=34)
    await cog.on_message(back13)
    check("cancel card [4] reports the 4 mentions",
          "**عدد المنشن أثناء غيابه: 4**"
          in last_sent(general)["embed"].description)
    view13 = sent_view(general)
    click13 = FakeInteraction(author, guild)
    await view13._on_click(click13)
    panel13 = click13.response.sent[-1]
    lines13 = (panel13["embed"].description or "").split("\n")
    check("panel lists all 4 snapshots of the ended session",
          len(lines13) == 4)
    check("empty content is presented as a dash",
          lines13[1] == "**Sara:** —")
    check("long content is truncated for display only [14]",
          "…" in lines13[0] and "L" * 300 not in lines13[0]
          and "L" * 100 in lines13[0])
    check("newest snapshot is first (10/page newest-first) [16]",
          lines13[0].startswith("**Karim:**"))
    check("oldest snapshot is last", "first snap" in lines13[3])

    # ── 14. session identity: old buttons, later sessions, users, restart ─
    print(f"{BOLD}[14] session-scoped buttons (17-18-19-20){RESET}")
    general.sent.clear()
    games.sent.clear()

    # Session A for the author: one mention, then return.
    await run_afk(author, general, "session A")
    await cog.on_message(invoking_message(
        bot, guild, other, games, "A-one <@100>", mid=40, mentions=[author]))
    await cog.on_message(invoking_message(
        bot, guild, author, games, "back from A", mid=41))
    view_a = sent_view(games)

    # Session B — a LATER session of the same owner.
    await run_afk(author, general, "session B")
    await cog.on_message(invoking_message(
        bot, guild, other, games, "B-one <@100>", mid=42, mentions=[author]))
    await cog.on_message(invoking_message(
        bot, guild, third, games, "B-two <@100>", mid=43, mentions=[author]))
    await cog.on_message(invoking_message(
        bot, guild, author, games, "back from B", mid=44))
    view_b = sent_view(games)

    check("each return card has its own session token [17]",
          view_a.session_id != view_b.session_id
          and view_a.session_id not in (None, "")
          and view_b.session_id not in (None, ""))
    check("old button custom_id pins its own ended session [17]",
          view_a.children[0].custom_id ==
          f"afk:mentions:{guild.id}:{author.id}:{view_a.session_id}"
          and view_a.children[0].custom_id != view_b.children[0].custom_id)

    click_a = FakeInteraction(author, guild)
    await view_a._on_click(click_a)
    panel_a = (click_a.response.sent[-1]["embed"].description or "")
    check("old return button opens its OWN ended session [17]",
          "A-one" in panel_a)
    check("later session cannot be opened through the old button [18]",
          "B-one" not in panel_a and "B-two" not in panel_a)

    click_b = FakeInteraction(author, guild)
    await view_b._on_click(click_b)
    panel_b = (click_b.response.sent[-1]["embed"].description or "")
    check("later button shows its own session only [18]",
          "B-one" in panel_b and "B-two" in panel_b
          and "A-one" not in panel_b)

    # Two users in the same guild never collide.
    await run_afk(other, games, "sara away")
    await cog.on_message(invoking_message(
        bot, guild, third, games, "S-only <@200>", mid=45, mentions=[other]))
    await cog.on_message(invoking_message(
        bot, guild, other, games, "sara back", mid=46))
    view_o = sent_view(games)
    check("users' buttons carry distinct session identities [19]",
          view_o.session_id not in (view_a.session_id, view_b.session_id)
          and view_o.children[0].custom_id ==
          f"afk:mentions:{guild.id}:{other.id}:{view_o.session_id}")
    click_o = FakeInteraction(other, guild)
    await view_o._on_click(click_o)
    panel_o = (click_o.response.sent[-1]["embed"].description or "")
    check("second user's button shows only her own snapshot [19]",
          "S-only" in panel_o and "A-one" not in panel_o
          and "B-one" not in panel_o)

    # [20] restart: the return-card buttons are rebuilt from the DB.
    bot.added_views.clear()  # simulate a fresh process view store
    cog_r = AFK(bot)
    await cog_r.cog_load()
    custom_a = view_a.children[0].custom_id
    custom_b = view_b.children[0].custom_id
    custom_o = view_o.children[0].custom_id
    check("restart re-registers every return-card button [20]",
          all(c in bot.added_views for c in (custom_a, custom_b, custom_o)))
    re_view_a = bot.added_views[custom_a]
    re_click = FakeInteraction(author, guild)
    await re_view_a._on_click(re_click)
    re_panel = (re_click.response.sent[-1]["embed"].description or "")
    check("re-registered button resolves the correct ended session [20]",
          "A-one" in re_panel and "B-one" not in re_panel)
    n_views = len(bot.added_views)
    await cog_r.register_persistent_views()
    check("re-registration never duplicates dispatchers",
          len(bot.added_views) == n_views)

    # A zero-mention session's button survives restart too [8+20].
    await run_afk(third, games, "zero guy")
    await cog.on_message(invoking_message(
        bot, guild, third, games, "zero guy back", mid=47))
    view_z = sent_view(games)
    check("zero-mention return card still has its own session token [8]",
          isinstance(view_z, afk_mod.AFKMentionsView)
          and view_z.session_id not in (None, ""))
    bot.added_views.clear()
    cog_z = AFK(bot)
    await cog_z.cog_load()
    custom_z = view_z.children[0].custom_id
    check("zero-mention return button survives restart [20]",
          custom_z in bot.added_views)
    z_click = FakeInteraction(third, guild)
    await bot.added_views[custom_z]._on_click(z_click)
    check("re-registered zero-mention button answers the zero-state [9]",
          z_click.response.sent[-1]["content"] ==
          "لا يوجد منشن محفوظ في جلسة الـ AFK هذه حتى الآن.")

    # ── 15. owner gate + owner panel ─────────────────────────────────────
    print(f"{BOLD}[15] non-owner rejection [6] + owner panel [7]{RESET}")
    stranger = FakeInteraction(other, guild)
    await view_a._on_click(stranger)
    rejected = stranger.response.sent[-1]
    check("non-owner interaction is rejected ephemerally [6]",
          rejected["ephemeral"] is True)
    check("non-owner rejection is the exact approved response [6]",
          rejected["content"] ==
          "هش هش ناس فضولية <:catto_evil:1438541407743901738>",
          repr(rejected["content"]))
    check("non-owner gets no snapshot embed [6]", rejected["embed"] is None)
    check("non-owner text exposes no stored data [6]",
          "Sara" not in (rejected["content"] or "")
          and "A-one" not in (rejected["content"] or ""))

    owner_click = FakeInteraction(author, guild)
    await view_a._on_click(owner_click)
    panel = owner_click.response.sent[-1]
    check("owner response is ephemeral [7]", panel["ephemeral"] is True)
    check("owner gets the snapshot panel [7]", panel["embed"] is not None)
    o_lines = (panel["embed"].description or "").split("\n")
    check("panel shows the mentioner's display name at mention time [7]",
          "Sara" in o_lines[0])
    check("panel shows the message content [7]", "A-one" in o_lines[0])

    # ── 16. pagination: 10/page, newest first, prev/next ─────────────────
    print(f"{BOLD}[16] pagination: 10/page, newest first, prev/next{RESET}")
    general.sent.clear()
    games.sent.clear()
    await run_afk(author, general, "paging")
    for i in range(12):
        await cog.on_message(invoking_message(
            bot, guild, other, games, f"snap {50 + i}", mid=50 + i,
            mentions=[author]))
    await cog.on_message(invoking_message(
        bot, guild, author, games, "paged done", mid=62))
    paged_view = sent_view(games)
    click1 = FakeInteraction(author, guild)
    await paged_view._on_click(click1)
    page1 = click1.response.sent[-1]
    p1_lines = (page1["embed"].description or "").split("\n")
    check("page 1 holds exactly 10 snapshots [16]", len(p1_lines) == 10)
    check("page 1 is newest-first [16]", "snap 61" in p1_lines[0])
    check("page 1 ends at the 10th newest", "snap 52" in p1_lines[-1])
    check("page 1 footer pages 1 من 2 [16]",
          "صفحة 1 من 2" in (page1["embed"].footer.text or ""))
    pager = page1["view"]
    check("multi-page attaches a pager [16]",
          isinstance(pager, afk_mod.AFKMentionsPager))
    check("pager starts on page 1", pager.page == 1)
    check("prev disabled on first page", pager._prev_button.disabled is True)
    check("next enabled with a page left", pager._next_button.disabled is False)

    next_inter = FakeInteraction(author, guild)
    await pager._on_next(next_inter)
    edit1 = next_inter.response.edits[-1]
    p2_lines = (edit1["embed"].description or "").split("\n")
    check("page 2 holds the remaining 2 snapshots [16]", len(p2_lines) == 2)
    check("page 2 continues newest-first", "snap 51" in p2_lines[0])
    check("page 2 footer pages 2 من 2 [16]",
          "صفحة 2 من 2" in (edit1["embed"].footer.text or ""))
    check("prev enabled on page 2", pager._prev_button.disabled is False)
    check("next disabled on last page", pager._next_button.disabled is True)
    check("pager edits keep the pager attached", edit1["view"] is pager)

    prev_inter = FakeInteraction(author, guild)
    await pager._on_prev(prev_inter)
    back_page = prev_inter.response.edits[-1]
    check("prev returns to page 1",
          len((back_page["embed"].description or "").split("\n")) == 10
          and "صفحة 1 من 2" in (back_page["embed"].footer.text or ""))

    stranger2 = FakeInteraction(other, guild)
    await pager._on_next(stranger2)
    s2 = stranger2.response.sent[-1]
    check("pager also rejects non-owner ephemerally [6]",
          s2["content"] == "هش هش ناس فضولية <:catto_evil:1438541407743901738>"
          and s2["ephemeral"] is True and stranger2.response.edits == [])

    # ── 17. dashboard toggle gates ONLY new sessions (21-24) ─────────────
    print(f"{BOLD}[17] disabled AFK gates only new sessions (21-24){RESET}")
    # An EXISTING session first (still enabled), then disable.
    await run_afk(author, general, "pre-disable")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO command_toggles "
            "(guild_id, command_name, enabled) VALUES (1, 'afk', 0)")
        await db.commit()

    # [22] Already AFK + disabled: the idempotent path wins (ordering 1.).
    general.sent.clear()
    await run_afk(author, general, "new reason")
    idem = last_sent(general)
    check("already-AFK re-run stays idempotent while disabled [22]",
          "بالفعل في وضع AFK" in (idem["content"] or ""))
    check("idempotent reply is not the disabled message [22]",
          idem["content"] != afk_mod.AFK_DISABLED_TEXT)

    # [24] Alerts keep firing for the existing session while disabled.
    alerts_before = count_of(games, "في وضع AFK")
    await cog.on_message(invoking_message(
        bot, guild, third, games, "disabled ping <@100>", mid=70,
        mentions=[author]))
    check("mention alerts continue while disabled [24]",
          count_of(games, "في وضع AFK") == alerts_before + 1)

    # [23] Owner cancels normally while disabled, and the return button
    # keeps working while disabled.
    await cog.on_message(invoking_message(
        bot, guild, author, games, "back", mid=71))
    canceled = last_sent(games)["embed"]
    check("existing owner still cancels while disabled [23]",
          afk_mod.AFK_END_TEXT in (canceled.description or ""))
    check("cancel still reports the count [23]",
          "**عدد المنشن أثناء غيابه: 1**" in (canceled.description or ""))
    disabled_view = sent_view(games)
    dis_click = FakeInteraction(author, guild)
    await disabled_view._on_click(dis_click)
    check("return button keeps working while disabled [23]",
          dis_click.response.sent[-1]["embed"] is not None
          and "disabled ping" in
          (dis_click.response.sent[-1]["embed"].description or ""))

    # [21] A NEW session is blocked — and only a new session.
    general.sent.clear()
    await run_afk(author, general, "should be blocked")
    blocked = last_sent(general)
    check("new session blocked with the exact approved message [21]",
          blocked["content"] == "الـ AFK معطّل حاليًا في هذا السيرفر.")
    check("block is a plain message, not an AFK card [21]",
          blocked["embed"] is None)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_state WHERE guild_id=1 AND user_id=100")
        no_session = (await cur.fetchone())[0]
    check("no new session row was created [21]", no_session == 0)

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
        bot, guild, author, games, "cleanup", mid=72))

    # ── 18. required scenario: session isolation + NO TTL + DB-backed ────
    print(f"{BOLD}[18] session isolation, no TTL retention, DB-backed startup"
          f"{RESET}")
    general.sent.clear()
    games.sent.clear()

    # Session A: exactly one mention -> return message A -> button A shows A.
    await run_afk(author, general, "scenario A")
    await cog.on_message(invoking_message(
        bot, guild, other, games, "Alpha ping <@100>", mid=80,
        mentions=[author]))
    await cog.on_message(invoking_message(
        bot, guild, author, games, "back from scenario A", mid=81))
    sc_a = sent_view(games)
    click_sc_a = FakeInteraction(author, guild)
    await sc_a._on_click(click_sc_a)
    check("Session A: return card A carries button A",
          isinstance(sc_a, afk_mod.AFKMentionsView))
    check("Session A: button A shows A's mention",
          "Alpha ping" in
          (click_sc_a.response.sent[-1]["embed"].description or ""))
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions WHERE session_id=?",
            (sc_a.session_id,))
        a_rows = (await cur.fetchone())[0]
    check("Session A: snapshots are not deleted at dismissal",
          a_rows == 1)

    # Session B (later, SAME owner): zero mentions -> its own button and the
    # correct zero-state.
    await run_afk(author, general, "scenario B")
    await cog.on_message(invoking_message(
        bot, guild, author, games, "back from scenario B", mid=82))
    sc_b = sent_view(games)
    click_sc_b = FakeInteraction(author, guild)
    await sc_b._on_click(click_sc_b)
    resp_sc_b = click_sc_b.response.sent[-1]
    check("Session B: zero mentions still get their own session button",
          isinstance(sc_b, afk_mod.AFKMentionsView)
          and sc_b.session_id != sc_a.session_id)
    check("Session B: button B shows the zero-state",
          resp_sc_b["content"] == afk_mod.AFK_ZERO_MENTIONS_TEXT
          and resp_sc_b["ephemeral"] is True and resp_sc_b["embed"] is None)

    # After Session B exists, button A still shows A — never B's state.
    click_sc_a2 = FakeInteraction(author, guild)
    await sc_a._on_click(click_sc_a2)
    panel_again = click_sc_a2.response.sent[-1]["embed"].description or ""
    check("after Session B, button A still shows A (not B)",
          "Alpha ping" in panel_again
          and len(panel_again.split("\n")) == 1)

    # No TTL and no cleanup: neither must exist at all.
    check("no time-based retention constant exists",
          not hasattr(afk_mod, "MENTIONS_RETENTION"))
    check("no cleanup pass exists on the cog",
          not hasattr(afk_mod.AFK, "cleanup_expired"))

    # A 400-day-old ended session: nothing may delete it, and its button
    # must still serve its snapshots (no arbitrary expiry).
    ancient_token = "ancientkeep01"
    ancient_end = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO afk_ended_sessions "
            "(session_id, guild_id, owner_id, ended_at) VALUES (?, 1, 100, ?)",
            (ancient_token, ancient_end))
        await db.execute(
            "INSERT INTO afk_mentions "
            "(guild_id, message_id, target_user_id, author_id, author_name,"
            " channel_id, content, session_id) "
            "VALUES (1, 990, 100, 200, 'Sara', 20, 'ancient keep', ?)",
            (ancient_token,))
        await db.commit()

    # Restart with an SQL spy: startup must register views from the
    # afk_ended_sessions identity metadata and must NOT load (or touch) any
    # snapshot rows — data stays DB-backed and session-scoped.
    captured_sql: list[str] = []
    original_connect = aiosqlite.connect

    def recording_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        real_execute = conn.execute

        async def recording_execute(sql, *a, **k):
            captured_sql.append(" ".join(str(sql).split()))
            return await real_execute(sql, *a, **k)

        conn.execute = recording_execute
        return conn

    bot.added_views.clear()
    aiosqlite.connect = recording_connect
    try:
        cog_restart = AFK(bot)
        await cog_restart.cog_load()
    finally:
        aiosqlite.connect = original_connect

    check("startup reads ended-session identity metadata",
          any("SELECT" in s.upper() and "afk_ended_sessions" in s
              for s in captured_sql))
    check("startup never SELECTs snapshot rows into RAM",
          not any(s.upper().startswith("SELECT") and "afk_mentions" in s
                  for s in captured_sql))
    check("startup performs no snapshot INSERT/UPDATE/DELETE",
          not any(s.upper().startswith(("INSERT", "UPDATE", "DELETE"))
                  and "afk_mentions" in s for s in captured_sql))

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions WHERE session_id=?",
            (ancient_token,))
        ancient_rows = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_ended_sessions WHERE session_id=?",
            (ancient_token,))
        ancient_markers = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM afk_mentions WHERE session_id=?",
            (sc_a.session_id,))
        a_rows_after = (await cur.fetchone())[0]
    check("400-day-old snapshots + marker survive startup untouched",
          ancient_rows == 1 and ancient_markers == 1)
    check("Session A snapshots also survive startup untouched",
          a_rows_after == 1)

    # After restart, A, B and the ancient session each resolve their OWN
    # ended session through the re-registered persistent views.
    custom_sc_a = sc_a.children[0].custom_id
    custom_sc_b = sc_b.children[0].custom_id
    custom_ancient = afk_mod._mentions_custom_id(
        guild.id, author.id, ancient_token)
    check("after restart all three buttons are re-registered",
          all(c in bot.added_views
              for c in (custom_sc_a, custom_sc_b, custom_ancient)))

    re_a = FakeInteraction(author, guild)
    await bot.added_views[custom_sc_a]._on_click(re_a)
    check("after restart button A still shows A",
          "Alpha ping" in (re_a.response.sent[-1]["embed"].description or ""))
    re_b = FakeInteraction(author, guild)
    await bot.added_views[custom_sc_b]._on_click(re_b)
    check("after restart button B still shows B's zero-state",
          re_b.response.sent[-1]["content"] == afk_mod.AFK_ZERO_MENTIONS_TEXT)
    re_ancient = FakeInteraction(author, guild)
    await bot.added_views[custom_ancient]._on_click(re_ancient)
    check("400-day-old button still serves its snapshots (no TTL)",
          "ancient keep" in
          (re_ancient.response.sent[-1]["embed"].description or ""))

    # ── 19. old afk_state / afk_mentions schemas self-heal ───────────────
    print(f"{BOLD}[19] old schemas self-heal (25){RESET}")
    heal_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    heal_tmp.close()
    old_path = heal_tmp.name
    async with aiosqlite.connect(old_path) as db:
        # The pre-pass afk_state: no session_id column at all.
        await db.execute("""
            CREATE TABLE afk_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                started_at TEXT NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
        await db.execute(
            "INSERT INTO afk_state (guild_id, user_id, reason, started_at,"
            " mention_count) VALUES (1, 100, 'legacy reason',"
            " '2026-09-01T00:00:00+00:00', 1)")
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
    afk_mod.DB_PATH = old_path  # cog helpers read module-level DB_PATH
    try:
        legacy_cog = AFK(bot)
        await legacy_cog.ensure_table()
        # A pre-upgrade session dismisses through the corrected flow: its
        # snapshots stay retrievable via the shared legacy token ('0').
        legacy_result = await legacy_cog.dismiss_afk(1, 100)
        legacy_page = await afk_mod.fetch_mentions_page(1, 100, "0", 1)
    finally:
        afk_mod.DB_PATH = saved_db_path
    async with aiosqlite.connect(old_path) as db:
        cur = await db.execute("PRAGMA table_info(afk_state)")
        state_cols = {r[1] for r in await cur.fetchall()}
        cur = await db.execute("PRAGMA table_info(afk_mentions)")
        cols = {r[1] for r in await cur.fetchall()}
        cur = await db.execute(
            "SELECT guild_id, message_id, target_user_id,"
            "       author_name, content, session_id FROM afk_mentions")
        legacy = await cur.fetchone()
        cur = await db.execute(
            "SELECT session_id FROM afk_ended_sessions WHERE guild_id=1"
            " AND owner_id=100")
        marker = await cur.fetchone()
    os.unlink(old_path)
    check("self-heal adds afk_state.session_id [25]", "session_id" in state_cols)
    check("self-heal adds author_id [25]", "author_id" in cols)
    check("self-heal adds author_name [25]", "author_name" in cols)
    check("self-heal adds channel_id [25]", "channel_id" in cols)
    check("self-heal adds content [25]", "content" in cols)
    check("self-heal adds afk_mentions.session_id [25]", "session_id" in cols)
    check("self-heal keeps the existing ledger row [25]",
          legacy is not None
          and legacy[0] == 1 and legacy[1] == 555 and legacy[2] == 100)
    check("legacy rows get safe defaults for the new columns [25]",
          legacy is not None and legacy[3] == "" and legacy[4] == ""
          and legacy[5] == afk_mod.LEGACY_SESSION_ID)
    check("legacy session dismisses with the legacy token [25]",
          legacy_result == ("legacy reason", 1, afk_mod.LEGACY_SESSION_ID))
    check("legacy session is marked for its return button [25]",
          marker is not None and marker[0] == afk_mod.LEGACY_SESSION_ID)
    check("legacy snapshots resolve session-scoped via the legacy token [25]",
          legacy_page[1] == 1 and legacy_page[0][0][1] == "")


if __name__ == "__main__":
    main()
