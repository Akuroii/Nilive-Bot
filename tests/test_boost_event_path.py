"""Boost EVENT-PATH verification: Boost.on_member_update() end to end.

The earlier suite exercised _handle_new_boost()/_handle_unboost() directly.
This one drives the real listener with a seeded, enabled boost configuration in
a scratch DB, so the enabled-flag gate, the config lookup, the role lookup, the
hierarchy check and the assign/remove calls are all on the tested path.

SCOPE / HONEST LIMITS
- Discord is a local double. This is NOT live-gateway verification.
- Discord exposes only Member.premium_since (yes/no). There is no per-member
  boost count, so a distinct "2nd boost" tier cannot be verified. Nothing here
  asserts that 2+ boost levels work; the level-2 role must NOT be granted from
  an unverifiable upgrade.

Read-only: scratch DB, no Discord network, no live role changes.

    python tests/test_boost_event_path.py      # exit 0 = all pass
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback

# Repository root discovered from this file, so no machine-specific path.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.update({
    "DATABASE_PATH": os.path.join(tempfile.mkdtemp(prefix="boost-ev-"), "s.db"),
    "NERO_ENVIRONMENT": "production",
    "OWNER_ID": "704453350384730237",
    "SECRET_KEY": "e" * 48,
    "DISCORD_TOKEN": "M" * 24 + "." + "A" * 6 + "." + "B" * 27,
})

GUILD = 1413528046785101865
USER = 555555555555555555
BOOST1_ROLE = 900000000000000001
BOOST2_ROLE = 900000000000000002
ANNOUNCE = 900000000000000010

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return ok


# ── Discord doubles ────────────────────────────────────────────────────────
class _Role:
    def __init__(self, rid, name="Role", position=10):
        self.id, self.name, self.position = rid, name, position
        self.mention = f"<@&{rid}>"


class _Member:
    """Doubles carry a per-member count; real Members do not (that hid the bug)."""
    def __init__(self, mid, premium_since=None, roles=None, guild=None):
        self.id = mid
        self.guild = guild
        self.premium_since = premium_since
        self.roles = list(roles or [])
        self.mention = f"<@{mid}>"
        self.display_avatar = None
        self.added, self.removed = [], []

    async def add_roles(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        if role in self.roles:
            self.roles.remove(role)
        self.removed.append(role.id)


class _Channel:
    def __init__(self):
        self.id = ANNOUNCE
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


class _Guild:
    def __init__(self, member):
        self.id = GUILD
        self.name = "Scratch"
        self.premium_subscription_count = 14   # server-wide; must be ignored
        self.roles = [_Role(1, "bot", 100), _Role(BOOST1_ROLE, "Booster", 10),
                      _Role(BOOST2_ROLE, "Double Booster", 11)]
        self.me = type("Me", (), {"top_role": self.roles[0]})()
        self.members = {member.id: member}
        self._channel = _Channel()

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, mid):
        return self.members.get(mid)

    def get_channel(self, cid):
        return self._channel if cid == ANNOUNCE else None

    async def fetch_member(self, mid):
        raise LookupError(mid)


class _Bot:
    def __init__(self):
        self.guilds = []
        self.user = type("U", (), {"id": 1})()
    def get_guild(self, gid):
        return self.guilds[0] if self.guilds else None
    def is_ready(self):
        return True


async def seed_config(enabled: int = 1):
    import aiosqlite
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        await db.execute("""
            INSERT INTO boost_config
                (guild_id, boost1_role_id, boost2_role_id, boost2_channel_id,
                 enabled, auto_remove_on_unboost)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET
                boost1_role_id=excluded.boost1_role_id,
                boost2_role_id=excluded.boost2_role_id,
                boost2_channel_id=excluded.boost2_channel_id,
                enabled=excluded.enabled
        """, (GUILD, BOOST1_ROLE, BOOST2_ROLE, ANNOUNCE, enabled))
        await db.commit()


async def main():
    import database
    await database.init_db()

    from cogs.boost import Boost

    bot = _Bot()
    cog = Boost(bot)

    # Prestige reconciliation touches the prestige tables on a premium_since
    # change; stub it so this test stays about boost role assignment.
    reconcile_calls = []

    async def fake_reconcile(member):
        reconcile_calls.append(member.id)

    cog._reconcile_prestige = fake_reconcile

    # ── 1. enabled config: new boost assigns the level-1 role via the listener ──
    await seed_config(enabled=1)
    member = _Member(USER, premium_since=None)
    guild = _Guild(member)
    bot.guilds = [guild]

    before = _Member(USER, premium_since=None, roles=list(member.roles), guild=guild)
    after = _Member(USER, premium_since="2024-05-01T00:00:00+00:00",
                    roles=list(member.roles), guild=guild)
    # `after` must be the object the listener mutates, so mirror it into the guild
    guild.members[USER] = after
    await cog.on_member_update(before, after)

    check("event path: new boost assigns the configured level-1 role",
          BOOST1_ROLE in after.added, f"added={after.added}")
    check("event path: level-2 role is NOT granted (unverifiable per-member count)",
          BOOST2_ROLE not in after.added, f"added={after.added}")
    check("event path: prestige reconciliation ran on the boost transition",
          reconcile_calls == [USER], str(reconcile_calls))
    check("event path: a new-boost announcement was posted to the configured channel",
          len(guild._channel.sent) == 1, f"messages={len(guild._channel.sent)}")

    # ── 2. repeated / unrelated updates must not duplicate role operations ─────
    adds_after_first = len(after.added)
    removes_after_first = len(after.removed)

    # 2a. identical boost state again (no premium_since change at all)
    same_before = _Member(USER, premium_since=after.premium_since, roles=list(after.roles), guild=guild)
    same_after = _Member(USER, premium_since=after.premium_since, roles=list(after.roles), guild=guild)
    guild.members[USER] = same_after
    await cog.on_member_update(same_before, same_after)
    check("repeated identical update performs no role add",
          not same_after.added, f"added={same_after.added}")
    check("repeated identical update performs no role remove",
          not same_after.removed, f"removed={same_after.removed}")

    # 2b. an unrelated member update (roles changed, boost state unchanged)
    unrelated_before = _Member(USER, premium_since=after.premium_since, roles=list(after.roles), guild=guild)
    unrelated_after = _Member(USER, premium_since=after.premium_since, roles=list(after.roles), guild=guild)
    guild.members[USER] = unrelated_after
    await cog.on_member_update(unrelated_before, unrelated_after)
    check("unrelated update performs no role add", not unrelated_after.added,
          f"added={unrelated_after.added}")
    check("unrelated update performs no role remove", not unrelated_after.removed,
          f"removed={unrelated_after.removed}")
    check("unrelated update does not re-run prestige reconciliation",
          reconcile_calls == [USER], str(reconcile_calls))

    # 2c. redelivered new-boost event (Discord can redeliver): already has role
    redelivered_before = _Member(USER, premium_since=None, roles=list(after.roles), guild=guild)
    redelivered_after = _Member(USER, premium_since=after.premium_since,
                                roles=list(after.roles), guild=guild)
    guild.members[USER] = redelivered_after
    await cog.on_member_update(redelivered_before, redelivered_after)
    check("redelivered new-boost event does not duplicate the role add",
          not redelivered_after.added, f"added={redelivered_after.added}")

    # ── 3. unboost through the event path removes the role ────────────────────
    unboost_before = _Member(USER, premium_since="2024-05-01T00:00:00+00:00",
                             roles=list(after.roles), guild=guild)
    unboost_after = _Member(USER, premium_since=None, roles=list(after.roles), guild=guild)
    guild.members[USER] = unboost_after
    await cog.on_member_update(unboost_before, unboost_after)
    check("event path: unboost removes the boost role",
          BOOST1_ROLE in unboost_after.removed, f"removed={unboost_after.removed}")
    check("event path: unboost leaves the level-2 role alone if absent",
          BOOST2_ROLE not in unboost_after.removed, f"removed={unboost_after.removed}")

    # ── 4. disabled config: the listener must do nothing ─────────────────────
    # Capture the count first: prestige reconciliation is independent of the
    # boost config (it runs on every premium_since transition, see the guard in
    # on_member_update), so assert the DELTA rather than an absolute total.
    reconciles_before_disabled = len(reconcile_calls)
    await seed_config(enabled=0)
    m_after = _Member(USER, premium_since="2024-06-01T00:00:00+00:00")
    g2 = _Guild(m_after)
    m_before = _Member(USER, premium_since=None, guild=g2)
    m_after.guild = g2
    bot.guilds = [g2]
    await cog.on_member_update(m_before, m_after)
    check("disabled config: no role is assigned", not m_after.added,
          f"added={m_after.added}")
    check("disabled config: no announcement is posted", not g2._channel.sent,
          f"messages={len(g2._channel.sent)}")
    check("disabled config: the premium transition is still reconciled exactly once",
          len(reconcile_calls) == reconciles_before_disabled + 1
          and set(reconcile_calls) == {USER},
          f"before={reconciles_before_disabled} after={len(reconcile_calls)}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} checks passed =====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} :: {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
