"""A reversed currency reward that the member re-earns must be theirs again.

The defect: the sweep debits the wallet, the member legitimately re-crosses the
level before the correction finalises, and the final guard reports the reward as
KEPT while the money is already gone. The claim is `fulfilled`, which is
terminal, so Claim All cannot compensate and nothing else will - the reward is
simply gone.

This does not reproduce with a live fenced lease and every writer normal, so the
re-cross is injected deterministically at the expiry/finalisation boundary:
`ops.reverse_currency` is wrapped so the award lands immediately after the debit
commits and before the sweep finalises - the window the final guard exists for.

Disposable database and mocked Discord members only. No live Discord, no
production data.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase1_support import GUILD, USER, execute, rows, reset_database  # noqa: E402

ROLE_ID = 7001
COINS = 500
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


def wallet():
    r = rows("SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
             (GUILD, USER))
    return tuple(r[0]) if r else (None, None)


def claim_status(cid):
    r = rows("SELECT status FROM level_reward_claims WHERE id=?", (cid,))
    return r[0][0] if r else None


def journal():
    return rows("SELECT claim_id, kind, status, reason FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? ORDER BY id", (GUILD, USER))


class World:
    def __init__(self, held=False):
        self.role = Role()
        self.held = [self.role] if held else []
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
        for r in roles:
            if r in self.held:
                self.held.remove(r)


class Role:
    __hash__ = object.__hash__

    def __init__(self, rid=ROLE_ID):
        self.id = rid
        self.name = "role"
        self.position = 5

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id


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
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,"
            "amount,audience) VALUES (?,?,?,?,'everyone')",
            (GUILD, levels, "balance", COINS))


async def deliver_currency_claim(world):
    from utils.level_claims import claim_available
    from utils.xp_calculator import xp_progress
    level = xp_progress(xp_of())[0]
    execute("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "track,reward_ref,payload_json,status,created_at,source,"
            "operation_id) VALUES (?,?,?,'currency',?,?,'pending',"
            "CURRENT_TIMESTAMP,'setlevel','op-setlevel-audit')",
            (GUILD, USER, level, "balance",
             json.dumps({"currency": "balance", "amount": COINS})))
    res = await claim_available(GUILD, USER, member=world.member)
    assert res["fulfilled"], res
    return res["fulfilled"][0]


async def main():
    print("=" * 72)
    print("REPRO 3: currency reversed, then the member re-crosses the level")
    print("=" * 72)
    with contextlib.redirect_stdout(io.StringIO()):
        await fresh()
    world = World(held=False)
    claim = await deliver_currency_claim(world)
    print(f"  setup: level={level_of()} claim={claim_status(claim)} "
          f"wallet={wallet()}")

    from utils import level_ops as ops
    from cogs.leveling import _apply_admin_xp
    from utils.reward_engine import give_reward

    orig_reverse_currency = ops.reverse_currency

    async def reverse_then_recross(**kwargs):
        outcome = await orig_reverse_currency(**kwargs)
        print(f"  reverse_currency -> status={outcome.get('status')} "
              f"wallet now={wallet()}")
        # Lease expiry: the sweep's TTL elapses after the debit committed and
        # before it finalises. The fence is gone, so the member's ordinary play
        # legitimately re-crosses Level 12 - the window the final guard exists
        # for. (With a live fenced lease the award is correctly deferred, which
        # is why this only reproduces at the expiry/finalisation boundary.)
        execute("UPDATE level_op_locks SET "
                "expires_at='2000-01-01T00:00:00+00:00' "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))
        res = await give_reward(None, GUILD, USER, "xp",
                                amount=xp_at(12) - xp_at(11),
                                reason="Message XP", source="leveling")
        print(f"  re-cross award -> {res.get('success')} new_level="
              f"{res.get('new_level')}")
        return outcome

    ops.reverse_currency = reverse_then_recross
    try:
        report = await _apply_admin_xp(GUILD, world.member, xp_at(11),
                                       bot_for(world), source="setlevel",
                                       actor_id=1, reason="currency audit")
    finally:
        ops.reverse_currency = orig_reverse_currency

    corr = report.get("correction") or {}
    print()
    print(f"  final level     = {level_of()}")
    print(f"  wallet          = {wallet()}   (expected ({COINS}, 0) if kept)")
    print(f"  claim {claim}      = {claim_status(claim)}")
    print(f"  journal         = {journal()}")
    print(f"  kept_reached    = {bool(corr.get('kept_reached'))}")
    print(f"  deleted_claims  = {corr.get('deleted_claims')}")
    print(f"  reversed        = {corr.get('reversed')}")

    bal = wallet()[0]
    kept = claim_status(claim) == "fulfilled"
    reached = level_of() >= 12

    # Accounting and claim state must agree. The old failure mode was exactly
    # this: the guard reports the reward KEPT while the wallet stays debited,
    # and `fulfilled` is terminal so nothing can pay it again.
    check("the member re-crossed the level, so the reward is earned", reached,
          level_of())
    check("the claim the member re-earned was kept", kept, claim_status(claim))
    check("a KEPT reward leaves the wallet credited, not debited",
          bal == COINS, (bal, COINS))
    check("the reversal journal is closed as an administrative fact, not left "
          "as a standing reversal",
          all(st not in ("applied", "already_reversed")
              for _, _, st, _ in journal()), journal())
    check("the restore is not a double credit", bal <= COINS, bal)

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL CURRENCY REVERSAL CONSISTENCY CHECKS PASSED")
    raise SystemExit(0)


if __name__ == "__main__":
    asyncio.run(main())
