"""Booster-only Level roles are dropped when a member's last active boost ends.

Drives the real runtime hook (``cogs/leveling.py::on_member_update``) and the
real removal helper (``utils/level_claims.strip_booster_roles``) against a real
SQLite claim ledger, with fake Discord members/guilds. No Discord API call is
made, so this is a runtime integration test, not a live Discord test.

What it locks down:

* losing the LAST boost removes Booster-only Level reward roles;
* losing one of several boosts (``premium_since`` still set) removes nothing;
* Member (everyone) Level roles, unrelated roles, manually granted roles and
  roles configured for both audiences are never removed;
* only roles that can be identified reliably as Booster-only Level rewards are
  candidates: the member's own ``booster:`` claim ledger rows plus the guild's
  ``audience='boosters'`` role rewards;
* an unverifiable boost status fails closed - nothing is removed, and a stale
  event payload cannot strip roles from a member the cache still shows boosting;
* Discord removal failures are reported per role and never abort the pass;
* boosting again restores what was earned, and levels passed without a boost are
  never unlocked retroactively;
* the claim ledger is never rewritten by the strip.

Run with:
    python scripts/test_booster_role_strip.py
"""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from phase1_support import GUILD, USER, execute, reset_database, rows
from test_level_reward_role_progression import (MANUAL_ROLE, UNRELATED_ROLE,
                                                set_exclusive, xp_to)

BOOSTING = datetime(2026, 1, 1, tzinfo=timezone.utc)
CREATED = "2026-01-01T00:00:00+00:00"

MEMBER_L10, MEMBER_L20, MEMBER_L30 = 600, 602, 607
BOOST_L10, BOOST_L20, BOOST_L30 = 601, 603, 604
DUAL_ROLE = 605            # configured for BOTH audiences on purpose
LEDGER_ONLY_BOOST = 606    # booster claim in the ledger, config row deleted
GHOST_BOOST = 609          # claimed as a booster role, no longer in the guild
MANUAL_BOOST = 610         # configured Booster-only, but never earned here


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" - {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


class Role:
    def __init__(self, rid, name="Level Reward"):
        self.id = int(rid)
        self.name = name
        self.position = 1
        self.mention = f"<@&{rid}>"


class Member:
    """Fake Discord member exposing everything the boost-loss path reads."""

    def __init__(self, guild, uid, roles=(), boosting=False):
        self.guild = guild
        self.id = int(uid)
        self.bot = False
        self.mention = f"<@{self.id}>"
        self.roles = list(roles)
        self.added, self.removed = [], []
        self.fail_remove = None
        self.premium_since = BOOSTING if boosting else None
        self.premium_subscription_count = 1 if boosting else 0

    async def add_roles(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        if self.fail_remove:
            self.fail_remove(role)
        self.roles = [r for r in self.roles if r.id != role.id]
        self.removed.append(role.id)

    def held(self):
        return sorted(r.id for r in self.roles)


class OpaqueMember:
    """A member object that does not expose ``premium_since`` at all."""

    def __init__(self, guild, uid, roles=()):
        self.guild = guild
        self.id = int(uid)
        self.bot = False
        self.roles = list(roles)
        self.removed = []

    async def remove_roles(self, role, reason=None):
        self.roles = [r for r in self.roles if r.id != role.id]
        self.removed.append(role.id)

    def held(self):
        return sorted(r.id for r in self.roles)


def build_guild(role_ids, people):
    catalog = {int(rid): Role(rid, f"Level Role {rid}") for rid in role_ids}
    catalog[UNRELATED_ROLE] = Role(UNRELATED_ROLE, "Member")
    catalog[MANUAL_ROLE] = Role(MANUAL_ROLE, "Event Winner")
    return SimpleNamespace(
        id=GUILD,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_role=lambda rid: catalog.get(int(rid)),
        get_member=lambda uid: people.get(int(uid)),
    )


def seed(level, role_id, audience):
    execute("INSERT INTO leveling_rewards (guild_id, level, role_id, audience) "
            "VALUES (?,?,?,?)", (GUILD, level, role_id, audience))


def seed_claim(uid, level, role_id, *, booster=False, status="fulfilled"):
    ref = ("booster:" if booster else "") + str(role_id)
    execute("INSERT OR IGNORE INTO level_reward_claims "
            "(guild_id,user_id,reward_level,track,reward_ref,payload_json,status,"
            "created_at,source) VALUES (?,?,?,'role',?,?,?,?,?)",
            (GUILD, uid, level, ref, json.dumps({"role_id": int(role_id)}),
             status, CREATED, "strip-test"))


def ledger(uid):
    return rows("SELECT reward_level,reward_ref,status FROM level_reward_claims "
                "WHERE guild_id=? AND user_id=? ORDER BY reward_level,reward_ref",
                (GUILD, uid))


def seed_member_row(uid):
    execute("INSERT OR IGNORE INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,0,0,0)", (GUILD, uid))
    execute("INSERT OR IGNORE INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,0,0)", (GUILD, uid))


def leveling_cog(bot):
    from cogs.leveling import Leveling
    cog = Leveling.__new__(Leveling)
    cog.bot = bot
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}
    cog._spam_incidents = {}
    cog._spam_warn_times = {}
    return cog


def event(uid, guild, premium_since, count=0, bot=False):
    """A gateway member snapshot (only the fields the hook reads)."""
    return SimpleNamespace(id=uid, guild=guild, bot=bot,
                           premium_since=premium_since,
                           premium_subscription_count=count)


async def main():
    await reset_database()
    from cogs.leveling import Leveling
    from utils.level_claims import (claim_available, ensure_tables,
                                    reconcile_role_progression,
                                    strip_booster_roles)
    from utils.reward_engine import give_reward

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    set_exclusive(False)

    people = {}
    guild = build_guild([MEMBER_L10, MEMBER_L20, MEMBER_L30, BOOST_L10, BOOST_L20,
                         BOOST_L30, DUAL_ROLE, LEDGER_ONLY_BOOST, MANUAL_BOOST],
                        people)
    bot = SimpleNamespace(get_guild=lambda gid: guild)
    cog = leveling_cog(bot)

    seed(10, MEMBER_L10, "everyone")
    seed(10, BOOST_L10, "boosters")
    seed(20, MEMBER_L20, "everyone")
    seed(20, BOOST_L20, "boosters")
    seed(30, MEMBER_L30, "everyone")
    seed(30, BOOST_L30, "boosters")
    seed(40, DUAL_ROLE, "everyone")
    seed(40, DUAL_ROLE, "boosters")
    seed(50, MANUAL_BOOST, "boosters")

    # ── 1. the real path: earn both roles while boosting, then lose the boost ──
    u1 = USER + 1
    m1 = Member(guild, u1, boosting=True)
    people[u1] = m1
    seed_member_row(u1)
    await give_reward(bot, GUILD, u1, "xp", amount=xp_to(10),
                      reason="strip-test", source="leveling")
    await claim_available(GUILD, u1, member=m1, bot=bot)
    check("a boosting member earns the Member reward and the Booster reward",
          m1.held() == [MEMBER_L10, BOOST_L10], str(m1.held()))

    # roles from other systems the member also wears
    m1.roles += [Role(UNRELATED_ROLE, "Member"), Role(MANUAL_ROLE, "Event Winner")]
    before_ledger = ledger(u1)
    m1.premium_since = None          # the cache agrees with the incoming event
    m1.premium_subscription_count = 0
    await Leveling.on_member_update(
        cog, event(u1, guild, BOOSTING, 1), event(u1, guild, None, 0))
    check("losing the last boost removes the Booster-only Level role",
          m1.removed == [BOOST_L10] and BOOST_L10 not in m1.held(),
          f"removed={m1.removed} held={m1.held()}")
    check("the Member Level role survives boost loss", MEMBER_L10 in m1.held())
    check("unrelated and manually granted roles survive",
          UNRELATED_ROLE in m1.held() and MANUAL_ROLE in m1.held())
    check("the strip never rewrites the claim ledger",
          ledger(u1) == before_ledger, str(ledger(u1)))

    # ── 2. losing one of several boosts keeps every Booster-only role ──
    u2 = USER + 2
    m2 = Member(guild, u2, roles=[Role(MEMBER_L10), Role(BOOST_L10)], boosting=True)
    m2.premium_subscription_count = 2
    people[u2] = m2
    seed_claim(u2, 10, MEMBER_L10)
    seed_claim(u2, 10, BOOST_L10, booster=True)
    m2.premium_subscription_count = 1                 # one boost remains
    await Leveling.on_member_update(cog, event(u2, guild, BOOSTING, 2),
                                    event(u2, guild, BOOSTING, 1))
    check("a member with one boost remaining keeps the Booster-only role",
          m2.removed == [] and m2.held() == [MEMBER_L10, BOOST_L10],
          f"held={m2.held()}")
    still = await strip_booster_roles(m2, GUILD, u2)
    check("the helper refuses to strip a member who is still boosting",
          still["still_boosting"] and still["removed"] == [] and m2.removed == [],
          str(still))

    # ── 3. a member who never boosted is left alone ──
    u3 = USER + 3
    m3 = Member(guild, u3, roles=[Role(MEMBER_L10)], boosting=False)
    people[u3] = m3
    seed_claim(u3, 10, MEMBER_L10)
    await Leveling.on_member_update(cog, event(u3, guild, None, 0),
                                    event(u3, guild, None, 0))
    check("the hook ignores members who were never boosting",
          m3.removed == [] and m3.held() == [MEMBER_L10])

    # ── 4. candidate identification: dual audience, ledger-only, not held ──
    u4 = USER + 4
    m4 = Member(guild, u4, roles=[Role(DUAL_ROLE), Role(LEDGER_ONLY_BOOST),
                                  Role(MEMBER_L20), Role(MANUAL_BOOST)],
                boosting=False)
    people[u4] = m4
    seed_claim(u4, 40, DUAL_ROLE)
    seed_claim(u4, 40, DUAL_ROLE, booster=True)
    seed_claim(u4, 25, LEDGER_ONLY_BOOST, booster=True)   # config row deleted
    seed_claim(u4, 20, MEMBER_L20)
    seed_claim(u4, 20, BOOST_L20, booster=True)           # earned, not worn
    res4 = await strip_booster_roles(m4, GUILD, u4)
    check("a role configured for both audiences is never stripped",
          DUAL_ROLE in m4.held() and DUAL_ROLE not in res4["removed"], str(res4))
    check("a Booster-only role proven by the ledger is stripped after its config "
          "row is deleted", LEDGER_ONLY_BOOST in res4["removed"], str(res4["removed"]))
    check("a Member Level role is never a candidate",
          MEMBER_L20 in m4.held() and MEMBER_L20 not in res4["removed"])
    check("a Booster-only role the member wears but never earned here is left "
          "alone", MANUAL_BOOST in m4.held() and MANUAL_BOOST not in res4["removed"],
          str(res4["removed"]))
    check("earned Booster-only roles the member does not wear are reported, not "
          "forced", BOOST_L20 in res4["not_held"] and BOOST_L20 not in m4.removed,
          str(res4["not_held"]))
    check("only the ledger-proven Booster-only role was removed",
          res4["removed"] == [LEDGER_ONLY_BOOST], str(res4["removed"]))

    # ── 5. boosting again restores; nothing is unlocked retroactively ──
    m1.premium_since = BOOSTING
    back = await reconcile_role_progression(m1, GUILD, u1)
    check("boosting again restores the previously earned Booster-only role",
          BOOST_L10 in back["restored"] and BOOST_L10 in m1.held(), str(back))

    u5 = USER + 5
    m5 = Member(guild, u5, boosting=False)
    people[u5] = m5
    seed_member_row(u5)
    await give_reward(bot, GUILD, u5, "xp", amount=xp_to(30),
                      reason="strip-test", source="leveling")
    await claim_available(GUILD, u5, member=m5, bot=bot)
    check("crossing a level without a boost creates no Booster entitlement",
          MEMBER_L30 in m5.held() and BOOST_L30 not in m5.held()
          and not any(ref.startswith("booster:") for _, ref, _ in ledger(u5)),
          str(ledger(u5)))
    m5.premium_since = BOOSTING
    late = await reconcile_role_progression(m5, GUILD, u5)
    check("boosting later never unlocks the Booster reward for a level already "
          "passed", BOOST_L30 not in late["restored"] and BOOST_L30 not in m5.held(),
          str(late))

    # ── 6. verification failures fail closed ──
    res_none = await strip_booster_roles(None, GUILD, u1)
    check("no member object: nothing is removed and the result says unverified",
          res_none["unverified"] and res_none["removed"] == [], str(res_none))

    u6 = USER + 6
    opaque = OpaqueMember(guild, u6, roles=[Role(BOOST_L10)])
    seed_claim(u6, 10, BOOST_L10, booster=True)
    res_opaque = await strip_booster_roles(opaque, GUILD, u6)
    check("a member whose boost status cannot be read keeps its roles",
          res_opaque["unverified"] and opaque.held() == [BOOST_L10], str(res_opaque))

    u7 = USER + 7
    m7 = Member(guild, u7, roles=[Role(BOOST_L10)], boosting=False)
    people[u7] = m7
    seed_claim(u7, 10, BOOST_L10, booster=True)
    bare = SimpleNamespace(id=u7, guild=guild, bot=False)   # no premium_since
    await Leveling.on_member_update(cog, bare, bare)
    check("the runtime hook does nothing when the event cannot verify boost "
          "status", m7.held() == [BOOST_L10] and m7.removed == [])

    u8 = USER + 8
    cached = Member(guild, u8, roles=[Role(BOOST_L10)], boosting=True)
    people[u8] = cached
    seed_claim(u8, 10, BOOST_L10, booster=True)
    await Leveling.on_member_update(cog, event(u8, guild, BOOSTING, 1),
                                    event(u8, guild, None, 0))
    check("a stale event payload cannot strip a member the cache still shows "
          "boosting", cached.held() == [BOOST_L10] and cached.removed == [])

    u9 = USER + 9
    m9 = Member(guild, u9, roles=[Role(BOOST_L10), Role(BOOST_L20)], boosting=False)
    people[u9] = m9
    seed_claim(u9, 10, BOOST_L10, booster=True)
    seed_claim(u9, 20, BOOST_L20, booster=True)

    def deny(role, _seen=[]):
        if role.id == BOOST_L10:
            raise RuntimeError("Missing Permissions")

    m9.fail_remove = deny
    res9 = await strip_booster_roles(m9, GUILD, u9)
    check("a Discord removal failure is reported and does not stop the rest",
          [rid for rid, _ in res9["failed"]] == [BOOST_L10]
          and res9["removed"] == [BOOST_L20] and BOOST_L10 in m9.held(), str(res9))

    u10 = USER + 10
    m10 = Member(guild, u10, roles=[Role(GHOST_BOOST)], boosting=False)
    people[u10] = m10
    seed_claim(u10, 15, GHOST_BOOST, booster=True)
    res10 = await strip_booster_roles(m10, GUILD, u10)
    check("a role the guild cannot resolve is reported, never guessed",
          [rid for rid, _ in res10["failed"]] == [GHOST_BOOST]
          and res10["removed"] == [] and GHOST_BOOST in m10.held(), str(res10))

    # ── 7. bots are ignored, and the exclusive toggle changes nothing here ──
    u11 = USER + 11
    m11 = Member(guild, u11, roles=[Role(BOOST_L10)], boosting=False)
    m11.bot = True
    people[u11] = m11
    seed_claim(u11, 10, BOOST_L10, booster=True)
    await Leveling.on_member_update(cog, event(u11, guild, BOOSTING, 1),
                                    event(u11, guild, None, 0, bot=True))
    check("bot members are ignored by the hook", m11.held() == [BOOST_L10])

    set_exclusive(True)
    u12 = USER + 12
    m12 = Member(guild, u12, roles=[Role(MEMBER_L20), Role(BOOST_L20)], boosting=False)
    people[u12] = m12
    seed_claim(u12, 20, MEMBER_L20)
    seed_claim(u12, 20, BOOST_L20, booster=True)
    await Leveling.on_member_update(cog, event(u12, guild, BOOSTING, 1),
                                    event(u12, guild, None, 0))
    check("with exclusive Level roles ON only the Booster-only role is removed",
          m12.removed == [BOOST_L20] and m12.held() == [MEMBER_L20],
          f"removed={m12.removed} held={m12.held()}")

    print("ALL BOOSTER ROLE STRIP CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)
