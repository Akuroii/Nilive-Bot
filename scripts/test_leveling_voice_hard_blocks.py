"""Voice XP anti-farming guard: ONE setting (`voice_farming_guard`) decides what
alone / deafened (self or server) / AFK-channel mean for Voice XP.

    guard ON  (default) -> none of the three earns Voice XP (the old behaviour)
    guard OFF           -> none of the three blocks Voice XP by itself

Everything else is independent and must stay so:
  * `voice_require_unmuted` still governs mute only;
  * `voice_xp_enabled` still switches Voice XP off entirely;
  * the Activity Engine keeps gating `activity_stats.voice_minutes` and the
    legacy `activity_voice_tick` event (missions, mvp) exactly as before.

Everything runs through the REAL ActivityEngine voice tick -> the REAL Leveling
listener -> give_reward -> a scratch DB, plus the real Dashboard API for the
persistence path. (The file keeps its original name so the old always-blocked
expectations cannot linger next to it.)

Run with:
    python scripts/test_leveling_voice_hard_blocks.py
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite
import discord
from flask import Flask

import phase1_support as S
import test_leveling_runtime as T   # reuse its fakes only: person / make_leveling / set_config / xp

GUILD = S.GUILD
U1, U2, BOT = 9101, 9102, 9103
FAILS = []
LEGACY_FLAG_KEYS = {"self_mute", "mute", "self_deaf", "deaf", "channel_id"}


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class FVoice(discord.VoiceChannel):
    members = property(lambda s: s._m)


def channel(cid, members, pos=0):
    c = object.__new__(FVoice)
    c.id, c.position, c._m = cid, pos, members
    return c


class FGuild:
    voice_channels = discord.Guild.voice_channels   # the REAL discord.py property

    def __init__(self, channels, afk=None):
        self.id = GUILD
        self._channels = {c.id: c for c in channels}
        self.afk_channel = afk


def P(uid, **kw):
    return T.person(uid, **kw)


def configure(guard, unmuted=1, enabled=1):
    """Write the guild's config exactly like the dashboard would store it."""
    T.set_config(voice_xp_enabled=enabled, voice_require_unmuted=unmuted)
    S.execute("UPDATE leveling_config SET voice_farming_guard=? WHERE guild_id=?", (guard, GUILD))


async def run_tick(guild):
    """Real engine tick -> real listener. Returns the engine's legacy tick events."""
    from cogs.activity_engine import ActivityEngine
    from cogs.leveling import Leveling
    events = []
    engine = ActivityEngine.__new__(ActivityEngine)
    engine.bot = SimpleNamespace(guilds=[guild], dispatch=lambda *a: events.append(a))
    await ActivityEngine.voice_tick_task.coro(engine)
    listener = T.make_leveling()
    for name, g, member, flags in events:
        if name == "activity_voice_xp_tick":
            await Leveling.on_activity_voice_xp_tick(listener, g, member, flags)
    return [e for e in events if e[0] == "activity_voice_tick"]


def two_humans(**h1):
    return FGuild([channel(1, [P(U1, **h1), P(U2)])])


def afk_two():
    ch = channel(5, [P(U1), P(U2)])
    return FGuild([ch], afk=ch)


def afk_one():
    ch = channel(5, [P(U1)])
    return FGuild([ch], afk=ch)


# name, builder, expected (h1, h2) XP with guard ON, with guard OFF (unmuted rule ON, the default),
# and who the engine's LEGACY tick / voice_minutes must still reach (guard-independent).
SCENARIOS = [
    ("normal eligible voice state",   lambda: two_humans(),                         (3, 3), (3, 3), {U1, U2}),
    ("alone (1 human)",               lambda: FGuild([channel(1, [P(U1)])]),         (0, 0), (3, 0), set()),
    ("1 human + 1 bot (alone)",       lambda: FGuild([channel(1, [P(U1), P(BOT, bot=True)])]), (0, 0), (3, 0), set()),
    ("2 humans, different channels",  lambda: FGuild([channel(1, [P(U1)], 0), channel(2, [P(U2)], 1)]), (0, 0), (3, 3), set()),
    ("h1 SERVER-deafened",            lambda: two_humans(deaf=True),                 (0, 3), (3, 3), {U2}),
    ("h1 self-deafened",              lambda: two_humans(self_deaf=True),            (0, 3), (3, 3), {U2}),
    ("AFK channel, 2 humans",         afk_two,                                       (0, 0), (3, 3), set()),
    ("AFK channel, 1 human",          afk_one,                                       (0, 0), (3, 0), set()),
    # the mute rule is independent of the guard in BOTH directions
    ("h1 self-muted (unmuted ON)",    lambda: two_humans(self_mute=True),            (0, 3), (0, 3), {U1, U2}),
    ("h1 server-muted (unmuted ON)",  lambda: two_humans(mute=True),                 (0, 3), (0, 3), {U1, U2}),
    ("alone AND muted (unmuted ON)",  lambda: FGuild([channel(1, [P(U1, self_mute=True)])]), (0, 0), (0, 0), set()),
    ("deafened AND muted (unmuted ON)", lambda: two_humans(self_deaf=True, self_mute=True), (0, 3), (0, 3), {U2}),
]


def voice_minutes():
    return {uid: m for uid, m in S.rows(
        "SELECT user_id, voice_minutes FROM activity_stats WHERE guild_id=?", (GUILD,))}


async def matrix():
    for name, build, on, off, legacy_users in SCENARIOS:
        for label, guard, expected in (("guard ON", 1, on), ("guard OFF", 0, off)):
            S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
            S.execute("DELETE FROM activity_stats WHERE guild_id=?", (GUILD,))
            configure(guard)
            legacy = await run_tick(build())
            got = (T.xp(U1)[0], T.xp(U2)[0])
            check(f"{label}: {name}", got == expected, f"got {got}, expected {expected}")
            # other consumers: identical regardless of the guard
            check(f"{label}: {name} — legacy activity_voice_tick + voice_minutes unchanged",
                  {e[2].id for e in legacy} == legacy_users
                  and set(voice_minutes()) == legacy_users
                  and all(m == 1 for m in voice_minutes().values())
                  and all(set(e[3]) == LEGACY_FLAG_KEYS for e in legacy),
                  f"legacy={sorted(e[2].id for e in legacy)} minutes={voice_minutes()}")

    print("\n== the mute rule (`voice_require_unmuted`) stays independent of the guard")
    for guard in (1, 0):
        S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
        configure(guard, unmuted=0)
        await run_tick(two_humans(self_mute=True))
        check(f"guard {'ON' if guard else 'OFF'} + unmuted OFF: a muted member earns Voice XP",
              (T.xp(U1)[0], T.xp(U2)[0]) == (3, 3))
    S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    configure(0, unmuted=0)
    await run_tick(FGuild([channel(1, [P(U1, deaf=True, self_mute=True)])]))
    check("guard OFF + unmuted OFF: alone + deafened + muted member earns Voice XP",
          T.xp(U1)[0] == 3)

    print("\n== the master switch still wins")
    S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    configure(0, enabled=0)
    await run_tick(two_humans())
    check("voice_xp_enabled=0 pays nothing even with the guard OFF", (T.xp(U1)[0], T.xp(U2)[0]) == (0, 0))


async def default_and_migration():
    print("\n== default ON keeps the old behaviour")
    from utils.xp_calculator import get_leveling_config
    S.execute("DELETE FROM leveling_config WHERE guild_id=?", (GUILD,))
    S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
    check("a guild with no config row runs with voice_farming_guard=1",
          (await get_leveling_config(GUILD))["voice_farming_guard"] == 1)
    await run_tick(FGuild([channel(1, [P(U1)])]))
    check("no row at all: alone earns nothing (old behaviour)", T.xp(U1)[0] == 0)
    T.set_config()   # a row written by code that does not know the column -> SQL default
    check("a row inserted without the column stores 1",
          S.rows("SELECT voice_farming_guard FROM leveling_config WHERE guild_id=?", (GUILD,)) == [(1,)])

    print("\n== existing database: additive migration keeps current guilds ON")
    from database import DB_PATH, migrate_leveling_config
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("ALTER TABLE leveling_config DROP COLUMN voice_farming_guard")
        await db.commit()
    S.execute("UPDATE leveling_config SET voice_xp_per_minute=7 WHERE guild_id=?", (GUILD,))
    async with aiosqlite.connect(DB_PATH) as db:
        await migrate_leveling_config(db)
        await db.commit()
    cols = [r[1] for r in S.rows("PRAGMA table_info(leveling_config)")]
    check("migration adds the column once", cols.count("voice_farming_guard") == 1)
    check("the existing guild row becomes ON and keeps its other values",
          S.rows("SELECT voice_farming_guard, voice_xp_per_minute FROM leveling_config WHERE guild_id=?",
                 (GUILD,)) == [(1, 7)])
    async with aiosqlite.connect(DB_PATH) as db:
        await migrate_leveling_config(db)     # idempotent
        await db.commit()
    check("running the migration again is a no-op",
          [r[1] for r in S.rows("PRAGMA table_info(leveling_config)")].count("voice_farming_guard") == 1)


async def persistence():
    print("\n== Dashboard API -> DB -> runtime")
    from dashboard.api import api_bp
    import dashboard.api.leveling as leveling_api
    import dashboard.auth as auth
    import dashboard.permissions as permissions
    from utils.xp_calculator import get_leveling_config

    app = Flask("voice-guard-test")
    app.secret_key = "test-only-voice-guard-secret"
    app.register_blueprint(api_bp, url_prefix="/api")
    client = app.test_client()

    async def admin_permission(_guild, _user):
        return "admin"

    with patch.object(auth, "is_session_valid", return_value=True), \
         patch.object(auth, "refresh_session_if_needed", return_value=None), \
         patch.object(permissions, "_get_permission_level", new=admin_permission), \
         patch.object(leveling_api, "log_action", return_value=None):
        csrf = "voice-guard-csrf"
        with client.session_transaction() as sess:
            sess["user"] = {"id": U1, "username": "voice-guard-test"}
            sess["guild_id"] = GUILD
            sess["csrf_token"] = csrf
        headers = {"X-CSRF-Token": csrf}

        S.execute("DELETE FROM leveling_config WHERE guild_id=?", (GUILD,))
        got = client.get("/api/leveling/config").get_json()["config"]
        check("GET surfaces the setting, default ON", got.get("voice_farming_guard") == 1, str(got))

        base = {"voice_xp_enabled": 1, "voice_xp_per_minute": 3, "voice_require_unmuted": 1,
                "message_xp_enabled": 1, "spam_detection_enabled": 0}
        for value in (0, 1):
            S.execute("DELETE FROM levels WHERE guild_id=?", (GUILD,))
            res = client.post("/api/leveling/config", json={**base, "voice_farming_guard": value}, headers=headers)
            stored = S.rows("SELECT voice_farming_guard, voice_require_unmuted FROM leveling_config WHERE guild_id=?", (GUILD,))
            check(f"POST voice_farming_guard={value}: saved, DB row = {value}, unmuted untouched",
                  res.status_code == 200 and stored == [(value, 1)], f"{res.get_json()} {stored}")
            check(f"GET returns {value}",
                  client.get("/api/leveling/config").get_json()["config"]["voice_farming_guard"] == value)
            check(f"runtime config reads {value}", (await get_leveling_config(GUILD))["voice_farming_guard"] == value)
            await run_tick(FGuild([channel(1, [P(U1)])]))
            check(f"saved guard={value}: a member alone in voice "
                  + ("earns Voice XP" if value == 0 else "earns nothing"),
                  T.xp(U1)[0] == (3 if value == 0 else 0), str(T.xp(U1)))

        res = client.post("/api/leveling/config", json={**base, "voice_require_unmuted": 0, "voice_farming_guard": 1}, headers=headers)
        check("guard and unmuted are saved independently (guard ON, unmuted OFF)",
              res.status_code == 200 and S.rows(
                  "SELECT voice_farming_guard, voice_require_unmuted FROM leveling_config WHERE guild_id=?", (GUILD,)) == [(1, 0)])
        client.post("/api/leveling/config", json={**base, "voice_farming_guard": 0}, headers=headers)
        client.post("/api/leveling/config", json=base, headers=headers)   # field absent -> default
        check("a payload without the field keeps the default (ON)",
              S.rows("SELECT voice_farming_guard FROM leveling_config WHERE guild_id=?", (GUILD,)) == [(1,)])
        res = client.post("/api/leveling/config", json={**base, "voice_farming_guard": 2}, headers=headers)
        check("an out-of-range value is rejected and nothing is written",
              res.status_code == 400 and S.rows(
                  "SELECT voice_farming_guard FROM leveling_config WHERE guild_id=?", (GUILD,)) == [(1,)])

    html = (S.ROOT / "dashboard/templates/systems/leveling.html").read_text()
    check("dashboard form has the checkbox, loads it and saves it",
          'name="voice_farming_guard" id="cfg-voice-guard"' in html
          and "c.voice_farming_guard" in html and "voice_farming_guard:" in html)
    check("the old fixed-rule hint is gone and the hint says it is controlled by the toggle",
          "Voice XP is never given when alone, deafened, or in AFK channel." not in html
          and "Controlled by this toggle" in html)
    check("`Require unmuted` checkbox is still its own, separate control",
          'name="voice_require_unmuted" id="cfg-voice-unmuted"' in html)


async def main():
    await S.reset_database()
    await matrix()
    await persistence()
    await default_and_migration()
    if FAILS:
        print(f"\n*** {len(FAILS)} VOICE GUARD CHECK(S) FAILED: {FAILS}")
        sys.exit(1)
    print("\nALL VOICE FARMING-GUARD CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
