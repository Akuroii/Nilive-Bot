"""Boost-role level logic must not read the guild total as a member attribute.

Uses a REAL discord.Member built from real payload data (the same shape Discord
sends), which is what production has. Test doubles used elsewhere in this repo
attach premium_subscription_count to fakes, which is why the original defect
passed CI -- so this test deliberately uses the real class.

Read-only. No Discord network, no role changes.

    python tests/test_boost_member_level.py     # exit 0 = all pass
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.update({
    "DATABASE_PATH": os.path.join(tempfile.mkdtemp(prefix="nilive-boost-"), "s.db"),
    "NERO_ENVIRONMENT": "production",
    "OWNER_ID": "704453350384730237",
    "SECRET_KEY": "b" * 48,
    "DISCORD_TOKEN": "M" * 24 + "." + "A" * 6 + "." + "B" * 27,
})

USER, GUILD = 555555555555555555, 1413528046785101865
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return ok


def build_member(premium_since="2024-01-01T00:00:00+00:00"):
    import discord

    class _State:
        def store_user(self, data):
            return discord.User(state=self, data={"id": USER, "username": "u",
                                                  "discriminator": "0",
                                                  "global_name": None, "avatar": None,
                                                  "bot": False, "public_flags": 0})

    class _Guild:
        id = GUILD
        # The server-wide total. A correct implementation must IGNORE this.
        premium_subscription_count = 14
        premium_tier = 3

    data = {
        "user": {"id": USER, "username": "u", "discriminator": "0", "global_name": None,
                 "avatar": None, "bot": False, "public_flags": 0},
        "joined_at": "2024-01-01T00:00:00+00:00", "roles": [], "deaf": False,
        "mute": False, "pending": False, "premium_since": premium_since,
        "communication_disabled_until": None, "nick": None, "flags": 0,
    }
    return discord.Member(data=data, guild=_Guild(), state=_State())


class _Role:
    def __init__(self, rid, name="Booster", position=1):
        self.id, self.name, self.position = rid, name, position
        self.mention = f"<@&{rid}>"


class _MemberDouble:
    """Doubles carry a per-member count, which is exactly why CI missed the bug."""
    def __init__(self, mid, premium_since="2024-01-01T00:00:00+00:00", roles=None):
        self.id = mid
        self.premium_since = premium_since
        self.roles = list(roles or [])
        self.mention = f"<@{mid}>"
        self.display_avatar = None
        self.added, self.removed, self.fail_add = [], [], None

    async def add_roles(self, role, reason=None):
        if self.fail_add:
            raise self.fail_add
        if role not in self.roles:
            self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        if role in self.roles:
            self.roles.remove(role)
        self.removed.append(role.id)


class _GuildDouble:
    def __init__(self, roles, me_position=100, member=None):
        self.id = GUILD
        self.roles = list(roles)
        self.me = type("Me", (), {"top_role": _Role(1, "bot", me_position)})()
        self.members = {member.id: member} if member else {}
        self.premium_subscription_count = 14   # server-wide; must be ignored

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, mid):
        return self.members.get(mid)

    def get_channel(self, cid):
        return None


async def _lifecycle_checks():
    import database
    from cogs.boost import Boost

    # _handle_unboost reads reaction_roles / boost_color_roles, so the scratch
    # DB needs the real schema (same init_db the bot runs).
    await database.init_db()

    cog = Boost.__new__(Boost)          # no client needed for these helpers
    boost1, boost2 = _Role(900000000000000001), _Role(900000000000000002)

    # 1. new boost assigns boost1 exactly once
    m = _MemberDouble(USER)
    g = _GuildDouble([boost1, boost2], member=m)
    await cog._handle_new_boost(g, m, boost1.id, boost2.id, None, {})
    check("new boost grants the level-1 role", m.added == [boost1.id], f"added={m.added}")
    await cog._give_role(g, m, boost1.id)
    check("granting again is idempotent (no duplicate add)", m.added == [boost1.id],
          f"added={m.added}")

    # 2. level-2 role is NOT falsely granted to a 1-boost member
    check("level-2 role is not granted on an unverifiable 2nd boost",
          boost2.id not in m.added, f"added={m.added}")

    # 3. unboost removes the role
    m.premium_since = None
    await cog._handle_unboost(g, m, boost1.id, boost2.id, {"auto_remove_on_unboost": 1})
    check("unboost removes the boost role", boost1.id in m.removed, f"removed={m.removed}")

    # 4. auto-remove disabled -> role is kept
    m2 = _MemberDouble(USER + 1, roles=[boost1])
    g2 = _GuildDouble([boost1, boost2], member=m2)
    m2.premium_since = None
    await cog._handle_unboost(g2, m2, boost1.id, boost2.id, {"auto_remove_on_unboost": 0})
    check("auto_remove_on_unboost=0 keeps the role", not m2.removed, f"removed={m2.removed}")

    # 5. re-boost during the retirement window is respected
    m3 = _MemberDouble(USER + 2, roles=[boost1])
    g3 = _GuildDouble([boost1, boost2], member=m3)
    await cog._retire_booster_role(g3, m3, boost1, "role", log_failure=True)
    check("a member boosting again keeps the role (no removal)",
          not m3.removed and boost1 in m3.roles, f"removed={m3.removed}")

    # 6. hierarchy: a role at/above the bot's top role is not assigned
    high = _Role(900000000000000003, position=500)
    m4 = _MemberDouble(USER + 3)
    g4 = _GuildDouble([high], me_position=100, member=m4)
    await cog._give_role(g4, m4, high.id)
    check("a role above the bot's top role is refused, not silently skipped",
          not m4.added, f"added={m4.added}")

    # 7. a missing role is a no-op, never a crash
    m5 = _MemberDouble(USER + 4)
    g5 = _GuildDouble([], member=m5)
    await cog._give_role(g5, m5, 999999)
    await cog._retire_booster_role(g5, m5, None, "role", log_failure=True)
    check("absent roles are handled without raising", not m5.added and not m5.removed)

    # 8. add_roles failure is caught (no unhandled exception in the event path)
    m6 = _MemberDouble(USER + 5)
    m6.fail_add = RuntimeError("Missing Permissions")
    g6 = _GuildDouble([boost1], member=m6)
    try:
        await cog._give_role(g6, m6, boost1.id)
        raised = False
    except Exception:
        raised = True
    check("a Discord permission error does not escape the boost event handler",
          not raised)


def main():
    import discord
    from cogs.boost import _booster_level
    from utils.timed_ownership import _booster_level_for

    # ── the API reality this fix depends on ──────────────────────────────
    member = build_member()
    check("real Member is actively boosting", member.premium_since is not None)
    check("real Member has NO premium_subscription_count attribute",
          not hasattr(member, "premium_subscription_count"),
          "premium_subscription_count is a discord.Guild attribute")
    check("the guild DOES carry a server-wide total (14) that must be ignored",
          getattr(member.guild, "premium_subscription_count", None) == 14)

    # ── cogs/boost.py must not invent a level from the guild total ────────
    level = _booster_level(member)
    check("boosting member reports level 1, not the guild total",
          level == 1, f"_booster_level()={level} (guild total is 14)")
    check("level is never >= 2 for a real Member",
          level < 2, "2+ cannot be verified from discord.Member")

    # ── not boosting -> 0 ────────────────────────────────────────────────
    non_booster = build_member(premium_since=None)
    check("non-boosting member reports level 0",
          _booster_level(non_booster) == 0, f"level={_booster_level(non_booster)}")

    # ── parity with the codebase's existing correct implementation ────────
    check("cogs/boost._booster_level agrees with timed_ownership._booster_level_for",
          _booster_level(member) == _booster_level_for(member)[0],
          f"{_booster_level(member)} vs {_booster_level_for(member)[0]}")

    # a double that DOES carry a per-member count is still honoured
    class CountedDouble:
        premium_since = "2024-01-01T00:00:00+00:00"
        premium_subscription_count = 2
    check("an explicit per-member count (test double/wrapper) is still respected",
          _booster_level(CountedDouble()) == 2,
          f"level={_booster_level(CountedDouble())}")

    # ── source guard: no phantom attribute reads remain ──────────────────
    src = open(os.path.join(REPO, "cogs", "boost.py"), encoding="utf-8").read()
    code_lines = [ln for ln in src.splitlines()
                  if "premium_subscription_count" in ln
                  and not ln.lstrip().startswith("#")
                  and "``" not in ln]
    check("no executable per-member premium_subscription_count read remains",
          not code_lines, str(code_lines[:3]))

    # ── assign / unboost-removal lifecycle, on doubles (no live roles) ────
    import asyncio
    asyncio.run(_lifecycle_checks())

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} checks passed =====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} :: {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
