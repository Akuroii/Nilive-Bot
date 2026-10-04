import pathlib
"""Shared harness: real discord.py 2.7.1 + the repo's real cogs/database,
no network, no Discord gateway."""
import asyncio, os, sys, types, pathlib

REPO = os.environ.get("REPO", str(pathlib.Path(__file__).resolve().parents[2]))
PROBE_DIR = os.environ.get("PROBE_DIR", "/tmp/probes")
os.makedirs(PROBE_DIR, exist_ok=True)
PROBE_DB = os.path.join(PROBE_DIR, "nero_test.db")

os.environ["DISCORD_TOKEN"] = "MTA.xxxxxxxxxxxxxxxxxxxxxxxxxxx.yyyyyyyyyyyyyyyyyyyyyyyyyyy"
os.environ["DATABASE_PATH"] = PROBE_DB
os.environ["NERO_ENVIRONMENT"] = "test"
os.environ["OWNER_ID"] = "704453350384730237"

sys.path.insert(0, REPO)

SENT_LOG: list = []


def fresh_db():
    p = pathlib.Path(PROBE_DB)
    if p.exists():
        p.unlink()


async def init_database():
    from database import init_db
    await init_db()


class FakeMember:
    def __init__(self, id=111, guild=None, roles=None, bot=False):
        self.id = id
        self.bot = bot
        self.guild = guild
        self.roles = roles or []
        self.display_name = f"user{id}"
        self.top_role = types.SimpleNamespace(position=0)
        self.mention = f"<@{id}>"
        self.mentioned_in = lambda m: True

    def __repr__(self):
        return f"FakeMember({self.id})"


class FakeChannel:
    def __init__(self, id=222, guild=None):
        self.id = id
        self.guild = guild
        self.sent = []
        self.type = types.SimpleNamespace(name="text")

    def permissions_for(self, obj):
        return types.SimpleNamespace(
            kick_members=True, ban_members=True, administrator=False,
            manage_roles=True, mention_everyone=False, manage_messages=True,
        )

    async def send(self, content=None, **kw):
        self.sent.append(content)
        emb = kw.get("embed")
        SENT_LOG.append(("channel.send",
                         content if content is not None else f"[embed:{getattr(emb,'title',None)}]"))
        return None


class FakeGuild:
    def __init__(self, id=333):
        self.id = id
        self.owner_id = 111
        self.name = "probe-guild"
        self.default_role = types.SimpleNamespace(id=1, position=0, name="@everyone", is_assignable=lambda: True)
        import discord as _d
        self._state = types.SimpleNamespace(
            get_user=lambda uid: None, dispatch=lambda *a, **k: None,
            member_cache_flags=_d.MemberCacheFlags.all(),
            http=_d.http.HTTPClient.__new__(_d.http.HTTPClient))
        self._state.http.send_message = lambda *a, **k: None

    def get_channel(self, cid):
        return None

    def get_role(self, rid):
        return None

    def get_member(self, uid):
        return None

    async def query_members(self, *a, limit=None, user=None, **kw):
        ids = user if isinstance(user, (list, tuple)) else [user]
        out = []
        for uid in ids:
            try:
                m = FakeMember(id=int(uid), guild=self)
            except (TypeError, ValueError):
                continue
            m.top_role = types.SimpleNamespace(position=5)
            m.guild_permissions = types.SimpleNamespace(
                kick_members=True, administrator=False, ban_members=True,
                manage_roles=True, mention_everyone=False, manage_messages=True)
            m.is_bot = lambda: False
            m.get_top_role = lambda: m.top_role
            m.timed_out_until = None
            m.kick = lambda *, reason=None, _m=m: SENT_LOG.append(("MEMBER.KICKED", _m.id, reason))
            out.append(m)
        return out


class FakeMessage:
    def __init__(self, content, guild=None, channel=None, author=None):
        self.content = content
        self.guild = guild or FakeGuild()
        self.channel = channel or FakeChannel(guild=self.guild)
        self.author = author or FakeMember(guild=self.guild)
        self.id = 1
        self.type = types.SimpleNamespace(name="default")
        self.webhook_id = None
        self.reactions = []
        self.attachments = []
        self.embeds = []
        self.mentions = [self.author]
        self.message_reference = None
        self.reference = None
        self.edited_at = None
        self.created_at = None
        self.flags = types.SimpleNamespace(ephemeral=False)
        self._state = types.SimpleNamespace(
            http=types.SimpleNamespace(send_message=self._http_send),
            guilds={}, channels={},
            get_message=lambda *a, **k: None,
            dispatch=lambda *a, **k: None,
            client=None,
            _get_guild=lambda gid: self.guild,
        )

    async def _http_send(self, channel_id, params=None, **kw):
        SENT_LOG.append((channel_id, params or {}))
        return {
            "id": "123", "channel_id": str(channel_id), "content": "",
            "author": {"id": "999", "bot": True, "username": "Nero",
                       "discriminator": "0", "global_name": None,
                       "avatar": None, "public_flags": 0},
            "timestamp": "2026-01-01T00:00:00+00:00", "edited_timestamp": None,
            "mentions": [], "mention_roles": [], "attachments": [],
            "embeds": [], "pinned": False, "type": 0, "flags": 0,
        }

    async def delete(self):
        pass

    async def reply(self, content=None, **kw):
        SENT_LOG.append(("reply", content))

    async def add_reaction(self, e):
        pass


def make_bot():
    import main  # noqa: creates the real commands.Bot with NeroCommandTree
    bot = main.bot
    try:
        bot.loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    if bot._connection.user is None:
        bot._connection.user = types.SimpleNamespace(id=999, bot=True, display_name="Nero")
    return bot


def patch_context_send():
    """Replace Context.send with a recorder (no HTTP)."""
    from discord.ext.commands import Context

    async def fake_send(self, content=None, **kw):
        SENT_LOG.append(("ctx.send", content))
        return None

    Context.send = fake_send
