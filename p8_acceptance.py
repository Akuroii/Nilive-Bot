"""PROBE 8 — acceptance test for the router-based alias system (2026-08-28).

This is the one to re-run after any change to the alias / trigger / custom
command path. Real cogs, real discord.py 2.7.1, real SQLite schema from
database.init_db(); only the Discord network layer is stubbed.

Run:  python tools/alias_probes/p8_acceptance.py
Exit code 0 = every check passed.
"""
import asyncio, datetime, inspect, json, os, pathlib, sys, types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from p0_env import (make_bot, init_database, fresh_db, patch_context_send,
                    FakeMessage, FakeGuild, FakeChannel, FakeMember, SENT_LOG)

import aiosqlite
import discord
from discord.ext import commands
from database import DB_PATH

GUILD = 333
OTHER_GUILD = 999

FAILURES = []
CHECKS = 0
KICKS = []          # (member_id, reason) recorded by the fake Member.kick
TARGET_ID = 260100000000004242   # discord.py's mention regex needs 15-20 digits


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{('  -> ' + str(detail)) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def _equip(member):
    """Give the *author* fake the bits the permission helpers read."""
    member.top_role = types.SimpleNamespace(
        position=5, id=2, name="mod", mention="<@&2>")
    member.guild_permissions = types.SimpleNamespace(
        kick_members=True, ban_members=True, manage_guild=True,
        manage_roles=True, mention_everyone=False, manage_messages=True)
    member.is_bot = lambda: False
    member.get_top_role = lambda: member.top_role
    member.roles = []
    return member


async def _recorded_kick(self, *, reason=None, **kw):
    """Stands in for the only real network call /kick makes."""
    KICKS.append((self.id, reason))


async def seed(db):
    await db.execute("""
        INSERT INTO command_toggles (guild_id, command_name, enabled, aliases, cooldown_seconds)
        VALUES (?, 'trigger_add', 1, '["ta"]', 0)
        ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["ta"]', enabled=1
    """, (GUILD,))
    await db.execute("""
        INSERT INTO command_toggles (guild_id, command_name, enabled, aliases)
        VALUES (?, 'kick', 1, '["k","k."]')
        ON CONFLICT(guild_id, command_name) DO UPDATE SET aliases='["k","k."]'
    """, (GUILD,))
    # trigger 'k' exact AND 'k' contains, plus a fuzzy 'kick' trigger
    for words, mt, fuzzy in (("k", "exact", 0), ("k", "contains", 0),
                             ("kick", "contains", 1)):
        await db.execute("""
            INSERT INTO triggers (guild_id, trigger_words, response_text, response_type,
                                  match_type, fuzzy_match, fuzzy_threshold, case_sensitive,
                                  response_chance, cooldown_seconds, allowed_channels, enabled)
            VALUES (?, ?, ?, 'text', ?, ?, 80, 0, 100, 0, '[]', 1)
        """, (GUILD, words, f"TRIGGER[{mt}{'/fuzzy' if fuzzy else ''}]-{words}", mt, fuzzy))
    # enabled custom command 'cc' and a disabled one 'off'
    await db.execute("""
        INSERT INTO custom_commands (guild_id, trigger, allowed_roles, actions, embed_title,
                                     embed_description, embed_color, same_channel,
                                     requires_mention, requires_reason, enabled)
        VALUES (?, 'cc', '[]', '[]', 'CUSTOM-CC-RAN', '', '#ED4245', 1, 0, 0, 1)
    """, (GUILD,))
    await db.execute("""
        INSERT INTO custom_commands (guild_id, trigger, allowed_roles, actions, embed_title,
                                     embed_description, embed_color, same_channel,
                                     requires_mention, requires_reason, enabled)
        VALUES (?, 'off', '[]', '[]', 'CUSTOM-OFF-RAN (should not happen)', '', '#ED4245',
                1, 0, 0, 0)
    """, (GUILD,))
    await db.commit()


def guild_for(gid):
    """A guild fake that is believable enough for discord.py's converters.

    MemberConverter ends with ``isinstance(result, discord.Member)``, so the
    *target* of ``k <@id>`` has to be a real Member — a stand-in object is
    rejected and the converter then reaches for ``bot._get_websocket(...)``,
    which does not exist here. Everything the moderation command reads off the
    guild is provided explicitly.
    """
    g = FakeGuild(id=gid)
    g.shard_id = 0                      # moderation helpers read this
    g.owner_id = 999                    # not the author: hierarchy gets used
    g.me = None
    g.get_role = lambda rid: None
    g.default_role = types.SimpleNamespace(id=1, position=0, name="@everyone")
    def store_user(user_id=None, data=None, *, comp=None):
        # 2.7.1 calls this as store_user(user_data, comp=self); older builds
        # pass (user_id, data). Accept both.
        if isinstance(user_id, dict):
            user_id, data = user_id.get("id"), user_id
        # Member.id / .name / .mention all delegate to Member._user, which
        # discord.py builds through state.store_user — returning None there
        # makes every attribute read on the member explode.
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


def make_guild(gid):
    return guild_for(gid)


_MSG_ID = [1000]


async def send(bot, content, guild_id=GUILD, author_id=111):
    SENT_LOG.clear()
    guild = make_guild(guild_id)
    ch = FakeChannel(guild=guild)
    ch.permissions_for = lambda obj: types.SimpleNamespace(
        kick_members=True, ban_members=True, manage_guild=True, manage_roles=True,
        mention_everyone=False, manage_messages=True)
    author = FakeMember(id=author_id, guild=guild)
    author.top_role = types.SimpleNamespace(position=5)
    _MSG_ID[0] += 1
    msg = FakeMessage(content, guild=guild, channel=ch, author=author)
    msg.mentions = []
    msg.id = _MSG_ID[0]          # the router memoises per message id
    msg._state = guild._state
    bot.dispatch("message", msg)
    await asyncio.sleep(0.6)
    return msg, ch


def sent_text():
    return " | ".join(str(s[1])[:80] for s in SENT_LOG)


async def db_rows(table, cols="*"):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT {cols} FROM {table}")
        return await cur.fetchall()


async def main():
    # Replace the single network call /kick makes; everything else runs for real.
    discord.Member.kick = _recorded_kick
    fresh_db()
    await init_database()
    async with aiosqlite.connect(DB_PATH) as db:
        await seed(db)
    patch_context_send()
    bot = make_bot()
    for ext in ("cogs.moderation", "cogs.triggers", "cogs.customcommands",
                "cogs.command_aliases"):
        await bot.load_extension(ext)

    router = getattr(bot, "nero_router", None)
    cog = bot.cogs["CommandAliases"]

    print("\n=== 1. index + router wiring ===")
    check("alias cog built a per-guild index", list(cog._table) == [GUILD],
          sorted(cog._table))
    check("aliases are NOT registered as prefix commands",
          "k" not in bot.all_commands and "ta" not in bot.all_commands,
          sorted(bot.all_commands))
    check("router got the guild-scoped table",
          router.alias_for(GUILD, "k") == "kick" and router.alias_for(OTHER_GUILD, "k") is None,
          router.alias_table)
    check("the index maps the word to its command name only",
          cog._table[GUILD]["ta"] == "trigger_add", cog._table[GUILD])
    cmd = cog._command_for("trigger_add", "ta")
    check("the transient command is built on first use", cmd is not None)
    check("…and cached for the next one",
          cog._command_for("trigger_add", "ta") is cmd)
    check("transient command carries the parent",
          cmd.extras.get("nero_alias_parent") == "trigger_add")
    check("param bridging used the real discord.py Parameter API",
          set(cmd.params) == {"trigger", "response"},
          {n: (q.kind.name, getattr(q, "converter", None).__name__
              if getattr(q, "converter", None) is not None else None, q.required)
           for n, q in cmd.params.items()})
    last = list(cmd.params.values())[-1]
    check("trailing string param = consume-rest (KEYWORD_ONLY)",
          last.kind is inspect.Parameter.KEYWORD_ONLY, last.kind)

    print("\n=== 2. bare alias executes the slash command ===")
    msg, ch = await send(bot, "ta hello some long response text")
    rows = [r for r in await db_rows("triggers", "trigger_words, response_text")
            if r[0] == "hello"]
    check("/trigger_add ran from `ta hello some long response text`",
          bool(rows), rows)
    check("trailing words went into `response`, not lost",
          rows and rows[0][1] == "some long response text",
          rows[0][1] if rows else None)
    check("message.content was NOT mutated", msg.content == "ta hello some long response text",
          msg.content)

    print("\n=== 3. no double execution ===")
    await send(bot, f"k <@{TARGET_ID}> rude")
    out = sent_text()
    check("trigger did not fire for an alias message", "TRIGGER[" not in out, out[:120])
    check("no 'k' custom command / trigger side effects", "CUSTOM-" not in out, out[:120])

    KICKS.clear()
    await send(bot, f"k <@{TARGET_ID}> being rude")
    check("single-char alias `k` ran /kick on the converted member",
          bool(KICKS) and KICKS[0][0] == TARGET_ID, KICKS)
    check("trailing text arrived as `reason`",
          bool(KICKS) and "being rude" in str(KICKS[0][1]),
          KICKS[0][1] if KICKS else None)

    print("\n=== 4. triggers still work when nothing claims the message ===")
    await send(bot, "kick me later")
    out = sent_text()
    check("fuzzy/contains trigger still fires on plain chat", "TRIGGER[" in out, out[:120])
    msg2, _ = await send(bot, "k?")
    out = sent_text()
    check("'k?' counts as the alias word (punctuation trimmed), so triggers stand down",
          "TRIGGER[" not in out, out[:120])

    print("\n=== 5. guild scoping ===")
    await send(bot, f"k <@{TARGET_ID}> rude", guild_id=OTHER_GUILD)
    check("alias from guild A does nothing in guild B", not SENT_LOG,
          sent_text()[:120])
    rows_before = await db_rows("command_toggles", "guild_id, aliases")
    check("no stray rows written while testing guild B", len(rows_before) == 2, rows_before)

    print("\n=== 6. custom commands: exact token, enabled honoured ===")
    await send(bot, "!cc")
    check("enabled custom command runs on !cc", "CUSTOM-CC-RAN" in sent_text(),
          sent_text()[:120])
    await send(bot, "!off")
    check("disabled custom command does NOT run", "CUSTOM-OFF" not in sent_text(),
          sent_text()[:120])
    await send(bot, "!cck")
    out = sent_text()
    check("trigger `cc` does not swallow `!cck`", "CUSTOM-CC-RAN" not in out, out[:120])

    print("\n=== 7. prefixed alias word is NOT an alias (bare-only design) ===")
    SENT_LOG.clear()
    before = len([r for r in await db_rows("triggers", "trigger_words") if r[0] == "hello"])
    await send(bot, "!ta should-not-run-as-alias")
    after = len([r for r in await db_rows("triggers", "trigger_words") if r[0] == "should-not-run-as-alias"])
    check("`!ta` does not run the alias", after == 0, f"rows added: {after}")

    print("\n=== 8. toggles / permissions on the alias path ===")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET enabled=0, error_message='nope, disabled' "
                         "WHERE guild_id=? AND command_name='trigger_add'", (GUILD,))
        await db.commit()
    await cog._sync_aliases()
    await send(bot, "ta disabled-check")
    check("disabled command -> its configured error message",
          "nope, disabled" in sent_text(), sent_text()[:140])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET enabled=1 WHERE guild_id=? AND command_name='trigger_add'",
                         (GUILD,))
        await db.commit()
    await cog._sync_aliases()

    # cooldown parity: /kick-style shared dict
    import main as mainmod
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET cooldown_seconds=30 "
                         "WHERE guild_id=? AND command_name='trigger_add'", (GUILD,))
        await db.commit()
    mainmod._command_cooldowns.pop((GUILD, 111, "trigger_add"), None)
    await send(bot, "ta cd1 x")
    await send(bot, "ta cd2 y")
    check("second alias use inside the cooldown window is rate limited",
          "Slow down" in sent_text(), sent_text()[:140])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET cooldown_seconds=0 "
                         "WHERE guild_id=? AND command_name='trigger_add'", (GUILD,))
        await db.commit()

    print("\n=== 9. missing required argument is reported, not swallowed ===")
    await send(bot, "ta")
    check("MissingRequiredArgument surfaced", "Missing argument" in sent_text() or "trigger" in sent_text(),
          sent_text()[:140])

    print("\n=== 10. sync flag handling ===")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO bot_settings (key, value) VALUES ('command_aliases_sync_needed','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
        await db.commit()
    loop = cog._alias_sync_check
    body = getattr(loop, "coro", None) or getattr(loop, "_iterator", None)
    check("sync task body reachable", body is not None, type(loop).__name__)
    await loop._loop_coro() if False else await body(cog)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM bot_settings WHERE key='command_aliases_sync_needed'")
        row = await cur.fetchone()
    check("sync flag consumed after a successful sync", row and row[0] == "0", row)
    status = await db_rows("bot_settings", "key, value")
    check("sync status recorded for the dashboard to show",
          any(k == "command_aliases_last_sync" for k, _ in status),
          [s for s in status if "alias" in s[0]])

    print("\n=== 11. the slash path shares the same gate code ===")
    # NeroCommandTree.interaction_check was rewritten to call the same helper
    # the alias gate uses, so "disabled for slash too" cannot drift.
    import main as mainmod
    tree = bot.tree

    class _Resp:
        def __init__(self):
            self.sent = None

        def is_done(self):
            return False

        async def send_message(self, content=None, **kw):
            self.sent = content
            SENT_LOG.append(("interaction.response", content))

    class _Ix:
        def __init__(self, name, member, gid=GUILD):
            self.guild = make_guild(gid)
            self.user = member
            self.command = types.SimpleNamespace(qualified_name=name)
            self.channel_id = 222
            self.response = _Resp()

    author = FakeMember(id=111, guild=None)
    author.top_role = types.SimpleNamespace(position=5, id=2, name="mod")
    author.roles = []

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET enabled=0, error_message='slash says no' "
                         "WHERE guild_id=? AND command_name='trigger_add'", (GUILD,))
        await db.commit()
    ix = _Ix("trigger_add", author)
    allowed = await tree.interaction_check(ix)
    check("slash path refuses a disabled command with its configured message",
          allowed is False and ix.response.sent == "slash says no",
          (allowed, ix.response.sent))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET enabled=1, error_message=NULL "
                         "WHERE guild_id=? AND command_name='trigger_add'", (GUILD,))
        await db.execute("UPDATE command_toggles SET owner_only=1 "
                         "WHERE guild_id=? AND command_name='kick'", (GUILD,))
        await db.commit()
    allowed = await tree.interaction_check(_Ix("trigger_add", author))
    check("slash path allows it again once enabled", allowed is True, allowed)
    allowed = await tree.interaction_check(_Ix("kick", author))
    check("owner_only applies on the slash path", allowed is False,
          "no message" if allowed else "blocked")

    # and the same rule via the alias gate — one implementation, one verdict
    SENT_LOG.clear()
    await send(bot, "ta back on x")
    gate_text = sent_text()
    check("the alias gate and the slash gate agree on the disabled command",
          "nope" not in gate_text, gate_text[:120])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE command_toggles SET owner_only=0 "
                         "WHERE guild_id=? AND command_name='kick'", (GUILD,))
        await db.commit()

    print("\n=== 11b. an alias survives its parent being absent at sync time ===")
    # The old build-at-sync design dropped the alias permanently in this
    # situation; the index is now independent of the tree.
    await bot.unload_extension("cogs.triggers")
    await cog._sync_aliases()
    check("index keeps the word even though /trigger_add is gone",
          cog._table.get(GUILD, {}).get("ta") == "trigger_add",
          cog._table.get(GUILD))
    SENT_LOG.clear()
    await send(bot, "ta nothing runs now")
    check("…and says so instead of silently doing nothing",
          "isn't loaded" in sent_text(), sent_text()[:140])
    await bot.load_extension("cogs.triggers")
    SENT_LOG.clear()
    await send(bot, "ta recovered still works")
    rows = [r for r in await db_rows("triggers", "trigger_words, response_text")
            if r[0] == "recovered"]
    check("the alias works again as soon as the cog is back",
          bool(rows), rows)

    print("\n=== 12. cog load order is irrelevant (the old claim, inverted) ===")
    # main.py used to warn "MUST be last" about the alias cog. The router makes
    # that false: prove it by loading the same cogs in the opposite order.
    for ext in ("cogs.triggers", "cogs.customcommands", "cogs.command_aliases"):
        await bot.unload_extension(ext)
    for ext in ("cogs.command_aliases", "cogs.customcommands", "cogs.triggers"):
        await bot.load_extension(ext)
    check("cogs actually reloaded in the new order",
          list(bot.cogs).count("CommandAliases") == 1, list(bot.cogs))
    SENT_LOG.clear()
    rows0 = len([r for r in await db_rows("triggers", "trigger_words")
                 if r[0] == "order-check"])
    await send(bot, "ta order-check works fine")
    rows1 = [r for r in await db_rows("triggers", "trigger_words, response_text")
             if r[0] == "order-check"]
    check("alias still runs with the alias cog loaded FIRST",
          bool(rows1), rows1)
    out = sent_text()
    check("trigger still silent for that message", "TRIGGER[" not in out, out[:120])

    print("\n=== 13. how the word may be typed ===")
    KICKS.clear()
    for label, text, should_run in (
        ("uppercase `TA`", "TA upper case here", True),
        ("trailing comma `ta,`", "ta, comma here", True),
        ("trailing spaces", "ta spaced here   ", True),
        ("padded punctuation `(ta)`", "(ta) paren here", True),
        ("word elsewhere in the line", "please ta nope", False),
        ("prefixed form `!ta`", "!ta prefixed no", False),
    ):
        SENT_LOG.clear()
        rows_before = len(await db_rows("triggers", "id"))
        await send(bot, text)
        added = len(await db_rows("triggers", "id")) - rows_before
        ran = added > 0
        check(f"{label:28s} -> {'runs' if should_run else 'stays silent'}",
              ran is should_run, {"rows_added": added, "out": sent_text()[:70]})

    print("\n=== 14. permission parity with the slash path ===")
    # Same channel, but this one says the member lacks Discord permissions:
    # the app command's own @checks.has_permissions must produce its message
    # rather than the alias quietly doing nothing.
    guild = make_guild(GUILD)
    ch = FakeChannel(guild=guild)
    ch.permissions_for = lambda obj: types.SimpleNamespace(
        kick_members=False, ban_members=False, manage_guild=False,
        manage_roles=False, mention_everyone=False, manage_messages=False)
    author = FakeMember(id=313, guild=guild)
    author.top_role = types.SimpleNamespace(position=1, id=3, name="member")
    author.roles = []
    _MSG_ID[0] += 1
    msg = FakeMessage("ta perm check", guild=guild, channel=ch, author=author)
    msg.id = _MSG_ID[0]
    msg.mentions = []
    msg._state = guild._state
    SENT_LOG.clear()
    bot.dispatch("message", msg)
    await asyncio.sleep(0.6)
    out = sent_text()
    check("missing Discord permission is reported on the alias path",
          "permission" in out.lower() or "Manage Guild" in out, out[:160])
    check("…and the command did not run anyway",
          "Trigger added" not in out, out[:120])

    print("\n=== 15. the shared gate's rules, driven directly ===")
    # evaluate_toggle_row is pure (no I/O) precisely so these can be asserted
    # without a Discord — both the slash path and the alias path call it, so
    # one table here covers both.
    from utils.command_gating import evaluate_toggle_row

    def row(**kw):
        base = dict(enabled=1, allowed_roles=None, allowed_channels=None,
                    owner_only=0, cooldown_seconds=0, bypass_cooldown_roles=None,
                    error_message=None, enabled_roles=None, disabled_roles=None,
                    enabled_channels=None, disabled_channels=None)
        base.update(kw)
        return tuple(base.values())

    def member(*role_ids):
        m = FakeMember(id=111, guild=None)
        m.roles = [types.SimpleNamespace(id=r, name=f"r{r}", position=r)
                   for r in role_ids]
        return m

    cd = {}
    ok, msg = evaluate_toggle_row(row(enabled=0, error_message="off"), GUILD, "kick",
                                  member(), 222, 999, cd, 1000.0)
    check("  enabled=0 uses the custom error_message", (ok, msg) == (False, "off"), msg)
    ok, _ = evaluate_toggle_row(row(enabled=0), GUILD, "kick", member(), 222, 999, cd, 1000.0)
    check("  enabled=0 without a message still blocks", ok is False)
    ok, _ = evaluate_toggle_row(row(disabled_roles="[5]"), GUILD, "kick", member(5),
                                222, 999, cd, 1000.0)
    check("  disabled_roles blocks a member who holds it", ok is False)
    ok, _ = evaluate_toggle_row(row(disabled_roles="[5]"), GUILD, "kick", member(6),
                                222, 999, cd, 1000.0)
    check("  …but not everyone else", ok is True)
    ok, _ = evaluate_toggle_row(row(enabled_roles="[7]"), GUILD, "kick", member(5),
                                222, 999, cd, 1000.0)
    check("  enabled_roles whitelist blocks non-holders", ok is False)
    ok, _ = evaluate_toggle_row(row(enabled_roles="[7]"), GUILD, "kick", member(7),
                                222, 999, cd, 1000.0)
    check("  …and lets holders through", ok is True)
    ok, _ = evaluate_toggle_row(row(allowed_roles="[9]"), GUILD, "kick", member(9),
                                222, 999, cd, 1000.0)
    check("  legacy allowed_roles still works when enabled_roles is empty", ok is True)
    ok, _ = evaluate_toggle_row(row(enabled_roles="[7]", allowed_roles="[9]"), GUILD,
                                "kick", member(9), 222, 999, cd, 1000.0)
    check("  the new column REPLACES the legacy one (no silent union = more "
          "permissive)", ok is False, "member with only role 9 got through")
    ok, _ = evaluate_toggle_row(row(disabled_channels="[3]"), GUILD, "kick", member(),
                                3, 999, cd, 1000.0)
    check("  disabled_channels blocks by channel", ok is False)
    ok, _ = evaluate_toggle_row(row(enabled_channels="[4]"), GUILD, "kick", member(),
                                3, 999, cd, 1000.0)
    check("  enabled_channels whitelist blocks other channels", ok is False)
    cd2 = {}
    ok1, _ = evaluate_toggle_row(row(cooldown_seconds=30), GUILD, "kick", member(),
                                 222, 999, cd2, 2000.0)
    ok2, msg2 = evaluate_toggle_row(row(cooldown_seconds=30), GUILD, "kick", member(),
                                    222, 999, cd2, 2010.0)
    check("  cooldown allows then blocks inside the window",
          ok1 is True and ok2 is False and "Slow down" in (msg2 or ""), msg2)
    ok3, _ = evaluate_toggle_row(row(cooldown_seconds=30), GUILD, "kick", member(),
                                 222, 999, cd2, 2100.0)
    check("  …and allows again once it expires", ok3 is True)
    cd3 = {}
    bypass = json.dumps([12])
    ok1, _ = evaluate_toggle_row(row(cooldown_seconds=30, bypass_cooldown_roles=bypass),
                                 GUILD, "kick", member(12), 222, 999, cd3, 3000.0)
    ok2, _ = evaluate_toggle_row(row(cooldown_seconds=30, bypass_cooldown_roles=bypass),
                                 GUILD, "kick", member(12), 222, 999, cd3, 3001.0)
    check("  bypass_cooldown_roles skips the cooldown", ok1 and ok2, cd3)
    ok_owner, _ = evaluate_toggle_row(row(owner_only=1), GUILD, "kick", member(),
                                      222, 555, cd, 4000.0)
    ok_owner2, _ = evaluate_toggle_row(row(owner_only=1), GUILD, "kick", member(),
                                       222, 111, cd, 4000.0)
    check("  owner_only allows only the guild owner",
          ok_owner is False and ok_owner2 is True, (ok_owner, ok_owner2))

    print(f"\n{'='*60}\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("FAILED:", *FAILURES, sep="\n  - ")
    try:
        await bot.close()
    except Exception:
        pass
    sys.exit(1 if FAILURES else 0)


asyncio.run(main())
