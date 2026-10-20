"""A lease that expires mid-correction must not cost the member a role.

The defect: a sweep starts removing a delivered role, the lease expires during
the Discord round-trip, the removal succeeds but authority is lost before the
database cleanup, and the member legitimately re-earns the level. That used to
leave the claim `fulfilled` while its reversal journal said `applied` - and
`reconcile_role_progression` deliberately refuses to restore a role whose
reversal is terminal, so the role was gone with no open case and no way back.

Note what is NOT accepted as evidence of recovery: `fulfilled` on its own. A
claim row that survives behind a terminal reversal is unrecoverable by every
production path.

Deterministic: the pause hook fires inside `member.remove_roles`, which is where
a real Discord round-trip would spend the time that outlives the lease TTL, and
the lease is expired explicitly rather than by waiting. Disposable database and
mocked Discord members only - no live Discord, no production data.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase1_support import GUILD, USER, execute, rows, reset_database  # noqa: E402

ROLE_ID = 7001
FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def xp_at(level):
    from utils.xp_calculator import xp_for_level
    return sum(xp_for_level(n) for n in range(1, level + 1))


def level_of():
    from utils.xp_calculator import xp_progress
    r = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER))
    return xp_progress(int(r[0][0]) if r else 0)[0]


def xp_of():
    r = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER))
    return int(r[0][0]) if r else 0


def claim_status(cid):
    r = rows("SELECT status FROM level_reward_claims WHERE id=?", (cid,))
    return r[0][0] if r else None


def journal():
    return rows("SELECT reversal_id, claim_id, kind, status, reason "
                "FROM reward_reversals WHERE guild_id=? AND user_id=? ORDER BY id",
                (GUILD, USER))


class Role:
    __hash__ = object.__hash__

    def __init__(self, rid=ROLE_ID):
        self.id = rid
        self.name = "role"
        self.position = 5

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id

    def __repr__(self):
        return f"Role({self.id})"


class World:
    def __init__(self, held=False, on_remove=None):
        self.role = Role()
        self.held = [self.role] if held else []
        self.on_remove = on_remove
        self.guild = SimpleNamespace(
            id=GUILD,
            get_role=lambda rid: self.role if int(rid) == self.role.id else None,
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
        self.member = SimpleNamespace(
            id=USER, mention=f"<@{USER}>", display_name="T", guild=self.guild,
            roles=self.held, premium_since=None)
        self.member.add_roles = AsyncMock(side_effect=self._add)
        self.member.remove_roles = AsyncMock(side_effect=self._remove)
        self.guild.get_member = lambda uid: self.member if uid == USER else None
        self.guild.fetch_member = AsyncMock(return_value=self.member)

    async def _add(self, *roles, **kw):
        for r in roles:
            if r not in self.held:
                self.held.append(r)

    async def _remove(self, *roles, **kw):
        if self.on_remove is not None:
            await self.on_remove(self)
        for r in roles:
            if r in self.held:
                self.held.remove(r)


def bot_for(world):
    return SimpleNamespace(get_guild=lambda gid: world.guild
                           if gid == GUILD else None)


async def fresh(levels=12):
    await reset_database()
    from utils.level_claims import ensure_tables
    from utils import level_ops as ops
    ops.invalidate_tables_cache()
    await ensure_tables()
    await ops.ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
            "VALUES (?,?,?,?,0)", (GUILD, USER, xp_at(levels), levels))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
            "VALUES (?,?,0,0)", (GUILD, USER))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'everyone')", (GUILD, levels, ROLE_ID))


async def deliver_role_claim(world):
    from utils.level_claims import claim_available
    from utils.xp_calculator import xp_progress
    level = xp_progress(xp_of())[0]
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "track,reward_ref,payload_json,status,created_at,source,"
            "operation_id) VALUES (?,?,?,'role',?,?,'pending',"
            "CURRENT_TIMESTAMP,'setlevel','op-setlevel-audit')",
            (GUILD, USER, level, str(ROLE_ID), json.dumps({"role_id": ROLE_ID})))
    res = await claim_available(GUILD, USER, member=world.member)
    assert res["fulfilled"], res
    return res["fulfilled"][0]


async def main():
    print("=" * 72)
    print("REPRO 2: lease expires inside remove_roles; member re-crosses")
    print("=" * 72)
    with contextlib.redirect_stdout(io.StringIO()):
        await fresh()
    world = World(held=False)
    claim = await deliver_role_claim(world)
    print(f"  setup: level={level_of()} claim={claim_status(claim)} "
          f"held={world.held}")

    from utils import level_ops as ops
    from cogs.leveling import _apply_admin_xp
    from utils.reward_engine import give_reward

    async def expire_then_recross(w):
        """Stand in for a Discord round-trip that outlives the lease TTL.

        The role removal itself still SUCCEEDS (the loop below removes it);
        only the worker's authority is gone by the time it returns.
        """
        # (1) the lease's TTL elapses while Discord is working
        execute("UPDATE level_op_locks SET expires_at='2000-01-01T00:00:00+00:00' "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))
        # (2) with the fence gone the member legitimately re-crosses L12
        res = await give_reward(None, GUILD, USER, "xp",
                                amount=xp_at(12) - xp_at(11),
                                reason="Message XP", source="leveling")
        print(f"  inside remove_roles: lease expired, re-cross award -> {res}")

    world.on_remove = expire_then_recross

    report = await _apply_admin_xp(GUILD, world.member, xp_at(11), bot_for(world),
                                   source="setlevel", actor_id=1,
                                   reason="lease expiry audit")
    corr = report.get("correction") or {}

    print()
    print(f"  final level        = {level_of()}")
    print(f"  roles held         = {world.held}")
    print(f"  claim {claim} status  = {claim_status(claim)}")
    print(f"  journal            = {journal()}")
    print(f"  lease_lost         = {corr.get('lease_lost')}")
    print(f"  kept_reached       = {corr.get('kept_reached')}")
    print(f"  deleted_claims     = {corr.get('deleted_claims')}")
    print(f"  roles_restored     = {corr.get('roles_restored')}")

    # Recoverability: can the member ever get the role back?
    from utils.level_claims import reconcile_role_progression
    from utils.level_ops import outstanding_reversals
    before = list(world.held)
    restored = await reconcile_role_progression(world.member, GUILD, USER)
    print()
    print(f"  reconcile_role_progression -> {restored}")

    try:
        open_cases = await outstanding_reversals(GUILD, USER)
    except Exception as exc:
        open_cases = f"error: {exc}"
    print(f"  open cases         = {open_cases}")

    reached = level_of() >= 12
    role_worn = world.role in world.held
    claim_alive = claim_status(claim) is not None
    print()
    print("  B1-style invariant: at an eligible level, is the reward recoverable?")
    print(f"    eligible(level>=12)={reached}  role_worn={role_worn}  "
          f"claim_alive={claim_alive}")
    recoverable = role_worn or (claim_alive and not (
        any(st in ("applied", "already_reversed") for _, _, _, st, _ in journal())
    ))
    print(f"    -> recoverable = {recoverable}")

    # The member is eligible, so the reward must be recoverable by a production
    # path - not merely present as a row.
    check("the member is legitimately eligible for the reward again", reached,
          level_of())
    check("the role is on the member, or a claim survives that production can "
          "restore from", recoverable,
          (role_worn, claim_alive, journal()))
    check("no reversal journal row is left terminal behind a surviving claim",
          not (claim_alive and any(
              st in ("applied", "already_reversed")
              for _, _, _, st, _ in journal())), journal())

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LEASE-EXPIRY ROLE RECOVERY CHECKS PASSED")
    raise SystemExit(0)


if __name__ == "__main__":
    asyncio.run(main())
