"""Voice XP hard blocks stay in force when `Require unmuted to earn voice XP` is OFF.

The toggle only governs self/server MUTE. AFK channel, server/self DEAF and
being alone are hard blocks that must be independent of it. Drives the real
ActivityEngine voice tick -> the real Leveling listener against a scratch DB.

Run with:
    python scripts/test_leveling_voice_hard_blocks.py
"""
import asyncio
import sys
from types import SimpleNamespace

import discord

import phase1_support as S
import test_leveling_runtime as T   # reuse its fakes only: person / make_leveling / set_config / xp

GUILD = S.GUILD
U1, U2, BOT = 9101, 9102, 9103
FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class FVoice(discord.VoiceChannel):
    members = property(lambda s: s._m)


class FStage(discord.StageChannel):
    members = property(lambda s: s._m)


def channel(cls, cid, members, pos=0):
    c = object.__new__(cls)
    c.id, c.position, c._m = cid, pos, members
    return c


class FGuild:
    # the REAL discord.py property classifies channels
    voice_channels = discord.Guild.voice_channels

    def __init__(self, channels, afk=None):
        self.id = GUILD
        self._channels = {c.id: c for c in channels}
        self.afk_channel = afk


def P(uid, **kw):
    return T.person(uid, **kw)


async def run_tick(guild, toggle):
    """Real engine tick -> real listener. Returns the number of tick events."""
    from cogs.activity_engine import ActivityEngine
    from cogs.leveling import Leveling
    T.set_config(voice_require_unmuted=toggle)
    events = []
    engine = ActivityEngine.__new__(ActivityEngine)
    engine.bot = SimpleNamespace(guilds=[guild], dispatch=lambda *a: events.append(a))
    await ActivityEngine.voice_tick_task.coro(engine)
    listener = T.make_leveling()
    for _name, g, member, flags in events:
        await Leveling.on_activity_voice_tick(listener, g, member, flags)
    return len(events)


def two_humans(**h1):
    return FGuild([channel(FVoice, 1, [P(U1, **h1), P(U2)])])


def afk_channel(**h1):
    ch = channel(FVoice, 5, [P(U1, **h1), P(U2)])
    return FGuild([ch], afk=ch)


# name, builder, expected (h1_xp, h2_xp) for toggle OFF, and for toggle ON
SCENARIOS = [
    # positive controls: without these a 0-XP result could be a broken harness
    ("control: 2 humans unmuted",              lambda: two_humans(),                 (3, 3), (3, 3)),
    ("control: h1 self-muted (toggle matters)", lambda: two_humans(self_mute=True),   (3, 3), (0, 3)),
    ("control: h1 server-muted (toggle matters)", lambda: two_humans(mute=True),     (3, 3), (0, 3)),
    # the hard blocks under test
    ("AFK channel, 2 humans unmuted",          lambda: afk_channel(),                (0, 0), (0, 0)),
    ("AFK channel, h1 muted",                  lambda: afk_channel(self_mute=True),  (0, 0), (0, 0)),
    ("h1 SERVER-deafened",                     lambda: two_humans(deaf=True),        (0, 3), (0, 3)),
    ("h1 self-deafened",                       lambda: two_humans(self_deaf=True),   (0, 3), (0, 3)),
    ("h1 deafened and muted",                  lambda: two_humans(self_deaf=True, self_mute=True), (0, 3), (0, 3)),
    ("ALONE (1 human)",                        lambda: FGuild([channel(FVoice, 1, [P(U1)])]), (0, 0), (0, 0)),
    ("ALONE and muted",                        lambda: FGuild([channel(FVoice, 1, [P(U1, self_mute=True)])]), (0, 0), (0, 0)),
    ("1 human + 1 bot",                        lambda: FGuild([channel(FVoice, 1, [P(U1), P(BOT, bot=True)])]), (0, 0), (0, 0)),
    ("2 humans in DIFFERENT channels",         lambda: FGuild([channel(FVoice, 1, [P(U1)], 0), channel(FVoice, 2, [P(U2)], 1)]), (0, 0), (0, 0)),
]


async def main():
    await S.reset_database()
    for name, build, expect_off, expect_on in SCENARIOS:
        for label, toggle, expected in (("OFF", 0, expect_off), ("ON", 1, expect_on)):
            S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
            await run_tick(build(), toggle)
            got = (T.xp(U1)[0], T.xp(U2)[0])
            check(f"toggle {label}: {name}", got == expected, f"got {got}, expected {expected}")

    # Voice XP master switch still wins over everything.
    from cogs.leveling import Leveling
    S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    T.set_config(voice_xp_enabled=0)
    await Leveling.on_activity_voice_tick(
        T.make_leveling(), SimpleNamespace(id=GUILD), P(U1), {"self_mute": False, "mute": False})
    check("control: voice_xp_enabled=0 pays nothing", T.xp(U1)[0] == 0)

    if FAILS:
        print(f"\n*** {len(FAILS)} VOICE HARD-BLOCK CHECK(S) FAILED: {FAILS}")
        sys.exit(1)
    print("\nALL VOICE HARD-BLOCK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
