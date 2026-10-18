"""`/setlevel`, `/setxp` and the XP-reduction policy.

Both admin commands put ONE member's XP somewhere, so they share one writer
(`cogs.leveling._apply_admin_xp`). This suite drives the real command callbacks
against a scratch SQLite database with fake Discord objects.

What it locks down:

1. `/setlevel` exists, is administrator-gated, and validates its level argument.
2. It uses THE BOT'S OWN curve. `/setlevel 10` lands on the XP at which level 10
   BEGINS - not level 9's threshold and not somewhere in the middle of level 10 -
   and `/setxp` with that same total produces a byte-identical row.
3. Both commands report the level and the XP before AND after.
4. `/setxp`'s reply is unchanged, so nothing that parsed it breaks.
5. The XP-reduction policy (approved for this PR): eligibility follows the
   CORRECTED level. Raising XP by mistake, letting the member claim, and then
   correcting the XP down reverses the delivered rewards through provenance,
   deletes their rows only once the effect is gone, and takes the value back -
   the member can then earn the reward again legitimately, but never hold more
   than the configured total. Unclaimed rows the administrative change created
   are kept as history and become dormant above the corrected level: not
   claimable until the member really reaches it, then payable once. The admin is
   told what was reversed and which unclaimed rows stopped being claimable.

Run with:
    python scripts/test_setlevel_admin.py
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database

OTHER = USER + 500
COIN_L3, COIN_L5 = 100, 250
ROLE_L5 = 601

FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def leveling_cog(bot):
    from cogs.leveling import Leveling
    cog = Leveling.__new__(Leveling)
    cog.bot = bot
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}
    cog._spam_incidents = {}
    cog._spam_warn_times = {}
    return cog


def admin_itx(member, guild):
    """A deferred admin interaction that records what it was told."""
    return SimpleNamespace(
        guild=guild, guild_id=GUILD, user=SimpleNamespace(id=999),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


def seed():
    for gid, uid in ((GUILD, USER), (GUILD, OTHER)):
        execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
                "VALUES (?,?,0,0,0)", (gid, uid))
    for level, amount in ((3, COIN_L3), (5, COIN_L5)):
        execute("INSERT INTO leveling_currency_rewards "
                "(guild_id,level,currency,amount,audience) "
                "VALUES (?,?, 'balance', ?, 'everyone')", (GUILD, level, amount))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'everyone')", (GUILD, 5, ROLE_L5))


async def main():
    await reset_database()
    from utils.level_claims import claim_available, ensure_tables, list_claims
    from utils.level_claims import SOURCE_SETLEVEL, SOURCE_SETXP
    from utils.xp_calculator import xp_for_level, xp_progress
    import cogs.leveling as leveling
    from cogs.leveling import Leveling, level_start_xp, MAX_SETTABLE_LEVEL
    from discord import app_commands

    await ensure_tables()
    seed()

    real_role = SimpleNamespace(id=ROLE_L5, name="Moon", position=5,
                                mention=f"<@&{ROLE_L5}>")
    guild = SimpleNamespace(
        id=GUILD, get_role=lambda rid: real_role if rid == ROLE_L5 else None,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    held = []
    member = SimpleNamespace(id=USER, mention=f"<@{USER}>", display_name="Tester",
                             guild=guild, roles=held, premium_since=None)
    member.add_roles = AsyncMock(side_effect=lambda *r, **k: held.extend(
        x for x in r if x not in held))
    member.remove_roles = AsyncMock(
        side_effect=lambda *r, **k: [held.remove(x) for x in r if x in held])
    guild.get_member = lambda uid: member if uid == USER else None
    guild.fetch_member = AsyncMock(return_value=member)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)
    cog = leveling_cog(bot)

    def balance():
        return rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                    (GUILD, USER))[0][0]

    def state():
        return rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                    (GUILD, USER))[0]

    async def setlevel(level, who=member):
        itx = admin_itx(who, guild)
        await Leveling.setlevel.callback(cog, itx, who, level)
        return itx

    async def setxp(xp, who=member):
        itx = admin_itx(who, guild)
        await Leveling.setxp.callback(cog, itx, who, xp)
        return itx

    # ── 1. the command exists and is gated ──
    print("== 1. /setlevel is registered and administrator-gated ==")
    command = Leveling.setlevel
    checks = getattr(command.callback, "__discord_app_commands_checks__", [])
    non_admin = SimpleNamespace(
        guild=guild, guild_id=GUILD, user=SimpleNamespace(id=USER),
        permissions=SimpleNamespace(administrator=False))
    admin = SimpleNamespace(
        guild=guild, guild_id=GUILD, user=SimpleNamespace(id=USER),
        permissions=SimpleNamespace(administrator=True))
    denied = False
    try:
        for predicate in checks:
            predicate(non_admin)
    except app_commands.MissingPermissions:
        denied = True
    check("/setlevel is administrator-gated",
          bool(checks) and denied and all(p(admin) for p in checks),
          f"checks={len(checks)} denied={denied}")
    check("/setlevel is a slash command taking member and level",
          command.name == "setlevel"
          and {p.name for p in command.parameters} == {"member", "level"},
          str([p.name for p in command.parameters]))
    check("/setxp is still administrator-gated too",
          bool(getattr(Leveling.setxp.callback,
                       "__discord_app_commands_checks__", [])))

    # ── 2. the bot's own curve: the START of the level ──
    print("== 2. level_start_xp uses the bot's own formula ==")
    start10 = level_start_xp(10)
    manual = sum(xp_for_level(n) for n in range(1, 11))
    level, current, needed = xp_progress(start10)
    check("level 10 starts at the sum of the curve for levels 1..10",
          start10 == manual, f"{start10} vs {manual}")
    check("xp_progress reports exactly level 10 with 0 XP into it",
          (level, current, needed) == (10, 0, xp_for_level(11)),
          f"{(level, current, needed)} vs {(10, 0, xp_for_level(11))}")
    check("that is NOT level 9's threshold",
          start10 != level_start_xp(9) and start10 > level_start_xp(9),
          f"L9={level_start_xp(9)} L10={start10}")
    check("and NOT somewhere in the middle of level 10",
          current == 0 and xp_progress(start10 + xp_for_level(11) // 2)[1] > 0,
          str(current))
    check("one XP less is still level 9, so the boundary is exact",
          xp_progress(start10 - 1)[0] == 9, str(xp_progress(start10 - 1)))
    check("the same holds for every level 0..30",
          all(xp_progress(level_start_xp(n)) == (n, 0, xp_for_level(n + 1))
              for n in range(0, 31)))

    # ── 3. /setlevel writes that XP and reports before and after ──
    print("== 3. /setlevel applies and reports ==")
    itx = await setlevel(10)
    reply = itx.followup.send.await_args.args[0]
    check("the member's row is exactly the start of level 10",
          state() == (start10, 10), str(state()))
    check("the reply is ephemeral and shows level AND xp, before AND after",
          itx.followup.send.await_args.kwargs.get("ephemeral") is True
          and "Level 0 → Level 10" in reply
          and f"XP: 0 → {start10:,}" in reply, reply)
    check("the interaction was deferred before any database work",
          itx.response.defer.await_args.kwargs.get("ephemeral") is True
          and itx.response.send_message.await_count == 0)
    check("the crossing it created is attributed to /setlevel",
          {r[0] for r in rows(
              "SELECT DISTINCT source FROM level_reward_claims "
              "WHERE guild_id=? AND user_id=?", (GUILD, USER))} == {SOURCE_SETLEVEL},
          str(rows("SELECT DISTINCT source FROM level_reward_claims "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))))
    check("and it created the rewards the levels it crossed define",
          {(r[0], r[1]) for r in rows(
              "SELECT reward_level, track FROM level_reward_claims "
              "WHERE guild_id=? AND user_id=? ORDER BY 1", (GUILD, USER))}
          == {(3, "currency"), (5, "currency"), (5, "role")},
          str(rows("SELECT reward_level, track FROM level_reward_claims "
                   "WHERE guild_id=? AND user_id=?", (GUILD, USER))))

    # ── 4. /setxp with the same total is the same state: one formula ──
    print("== 4. /setxp and /setlevel agree ==")
    execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    itx = await setxp(start10)
    check("/setxp to the same total writes the same row",
          state() == (start10, 10), str(state()))
    check("/setxp's reply is unchanged, so nothing parsing it breaks",
          itx.followup.send.await_args.args[0].startswith(
              f"Set {member.mention}'s XP to {start10:,} (Level 10)."),
          itx.followup.send.await_args.args[0])
    check("/setxp attributes its crossings to /setxp",
          {r[0] for r in rows(
              "SELECT DISTINCT source FROM level_reward_claims "
              "WHERE guild_id=? AND user_id=?", (GUILD, USER))} == {SOURCE_SETXP})
    execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    await setlevel(10)
    via_level = rows("SELECT reward_level, track, reward_ref, status "
                     "FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                     (GUILD, USER))
    execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    await setxp(start10)
    via_xp = rows("SELECT reward_level, track, reward_ref, status "
                  "FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                  (GUILD, USER))
    check("both commands create identical entitlements (one shared writer)",
          sorted(via_level) == sorted(via_xp) and via_level,
          f"{sorted(via_level)} vs {sorted(via_xp)}")

    # ── 5. validation ──
    print("== 5. validation ==")
    for bad in (-1, MAX_SETTABLE_LEVEL + 1):
        execute("UPDATE levels SET xp=?, level=4 WHERE guild_id=? AND user_id=?",
                (level_start_xp(4), GUILD, USER))
        itx = await setlevel(bad)
        refused = itx.followup.send.await_count == 0
        text = (itx.response.send_message.await_args.args[0]
                if itx.response.send_message.await_count else "")
        check(f"/setlevel {bad} is refused and changes nothing",
              refused and "Nothing was changed" in text
              and state() == (level_start_xp(4), 4),
              f"{text!r} state={state()}")
    itx = await setlevel(0)
    check("/setlevel 0 is allowed and lands on level 0 with 0 XP",
          state() == (0, 0) and "Level 4 → Level 0" in
          itx.followup.send.await_args.args[0], str(state()))
    itx = await setxp(-500)
    check("/setxp still clamps a negative total to zero",
          state() == (0, 0), str(state()))

    # ── 6. the XP-reduction policy ──
    # XP raised by mistake -> the member claims -> the admin corrects it down.
    # Policy (approved for this PR): eligibility follows the CORRECTED level.
    # Delivered rewards that only the higher level unlocked are reversed through
    # provenance, and their claim rows are deleted only after the effect is gone,
    # so the reward can be earned again legitimately. Unclaimed rows created by
    # the administrative change stay in place as history but are dormant above
    # the corrected level - not claimable until the member really reaches it.
    print("== 6. XP raised by mistake, claimed, then corrected down ==")
    execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    await setlevel(10)
    claimed = await claim_available(GUILD, USER, member=member, bot=bot)
    paid = balance()
    claims_before = sorted(
        (r[0], r[1], r[2], r[3]) for r in rows(
            "SELECT reward_level, track, reward_ref, status "
            "FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER)))
    check("the mistaken level really paid the member",
          paid == COIN_L3 + COIN_L5 and len(claimed["fulfilled"]) == 3
          and all(s == "fulfilled" for *_, s in claims_before),
          f"paid={paid} fulfilled={claimed['fulfilled']}")

    itx = await setxp(level_start_xp(2))
    reply = itx.followup.send.await_args.args[0]
    claims_after = rows(
        "SELECT reward_level, track, reward_ref, status "
        "FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER))
    journal = rows(
        "SELECT kind, status FROM reward_reversals WHERE guild_id=? AND user_id=?",
        (GUILD, USER))
    check("correcting the XP down reverses the delivered rewards through "
          "provenance and deletes their rows only now",
          claims_after == []
          and sorted(journal) == [("currency", "applied"),
                                  ("currency", "applied"),
                                  ("role", "applied")],
          f"{claims_before} -> {claims_after} journal={journal}")
    check("and the value is taken back, not left in place",
          balance() == 0 and real_role not in held,
          f"balance={balance()} held={[r.id for r in held]}")
    check("the admin is told the level went down and exactly what was reversed",
          "Level went **down** (L10 → L2)" in reply
          and "Reversed 3 delivered reward(s)" in reply
          and f"{COIN_L3 + COIN_L5} balance" in reply
          and "1 role(s)" in reply, reply)
    check("the advisory no longer advertises the bulk history wipe",
          "/level_test_reset" not in reply
          and "include_delivered" not in reply, reply)
    check("the stored level and XP stay consistent with the curve",
          state() == (level_start_xp(2), 2)
          and xp_progress(state()[0])[0] == state()[1], str(state()))

    # The deleted rows are what makes the reward legitimately earnable again.
    # Receiving the configured total once - not twice - is the invariant.
    re_levelled = await setlevel(10)
    second = await claim_available(GUILD, USER, member=member, bot=bot)
    check("levelling back up re-earns the reward exactly once: the member holds "
          "the configured total, never double the mistaken payout",
          balance() == paid and len(second["fulfilled"]) == 3
          and len(claims_after) == 0
          and sorted((r[0], r[1], r[2], r[3]) for r in rows(
              "SELECT reward_level, track, reward_ref, status "
              "FROM level_reward_claims WHERE guild_id=? AND user_id=?",
              (GUILD, USER))) == claims_before
          and real_role in held,
          f"balance={balance()} paid={paid} second={second['fulfilled']}")
    check("the correction back up raises no false alarm",
          "not deleted" not in re_levelled.followup.send.await_args.args[0],
          re_levelled.followup.send.await_args.args[0])

    # An unclaimed administrative entitlement is a different case: it was never
    # delivered, so there is nothing to reverse, but it must not stay claimable
    # above the corrected level either. The row is kept as history and becomes
    # claimable again only if the member legitimately reaches that level.
    execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("UPDATE economy SET balance=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    execute("DELETE FROM reward_reversals WHERE guild_id=? AND user_id=?",
            (GUILD, USER))
    held.clear()                # model a fresh member; the row is at L0 again
    await setlevel(10)                       # crosses L3/L5, nothing claimed yet
    dormant_ids = [r[0] for r in rows(
        "SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=? "
        "ORDER BY id", (GUILD, USER))]
    itx = await setxp(level_start_xp(2))     # corrected down before claiming
    reply = itx.followup.send.await_args.args[0]
    check("an unclaimed reward above the new level is reported as no longer "
          "claimable, with its rows kept",
          "3 unclaimed reward(s) above L2" in reply
          and "no longer claimable" in reply
          and "rows are kept" in reply
          and "legitimately reaches those levels" in reply
          and "pay only once" in reply, reply)
    pending = await claim_available(GUILD, USER, member=member, bot=bot)
    check("and it is NOT payable while the member is below that level",
          pending["fulfilled"] == [] and pending["owned"] == 0
          and balance() == 0 and real_role not in held,
          f"{pending['fulfilled']} balance={balance()}")
    kept = rows("SELECT id, status FROM level_reward_claims WHERE guild_id=? "
                "AND user_id=? ORDER BY id", (GUILD, USER))
    check("the rows survive as history instead of being deleted or paid",
          [r[0] for r in kept] == dormant_ids
          and all(r[1] == "pending" for r in kept), kept)
    # The card reads the same predicate the reservation SQL enforces, so an old
    # message's Claim All cannot look pressable while the rows are dormant.
    from cogs.leveling import _claim_button_state
    view_rows = await list_claims(GUILD, USER)
    check("the Claim All button is disabled for the dormant rows",
          sorted(c["id"] for c in view_rows) == sorted(dormant_ids)
          and all(c["above_level"] and c["admin_created"] for c in view_rows)
          and await _claim_button_state(GUILD, view_rows, False) == (False, False),
          [(c["id"], c["status"], c["above_level"], c["admin_created"])
           for c in view_rows])

    # Reaching the level legitimately makes the SAME rows claimable again -
    # at most once - because the row is both the history and the entitlement.
    again = await setlevel(10)
    pending = await claim_available(GUILD, USER, member=member, bot=bot)
    check("reaching the level legitimately re-enables the same rows, once",
          len(pending["fulfilled"]) == 3 and balance() == COIN_L3 + COIN_L5
          and real_role in held
          and [r[0] for r in rows(
              "SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=? "
              "ORDER BY id", (GUILD, USER))] == dormant_ids
          and all(r[0] == "fulfilled" for r in rows(
              "SELECT status FROM level_reward_claims WHERE guild_id=? "
              "AND user_id=?", (GUILD, USER))),
          f"{pending['fulfilled']} balance={balance()}")
    check("that upward correction still raises no false alarm",
          "no longer claimable" not in again.followup.send.await_args.args[0],
          again.followup.send.await_args.args[0])

    # ── 7. scope ──
    print("== 7. scope ==")
    check("no other member's row moved",
          rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
               (GUILD, OTHER)) == [(0, 0)]
          and rows("SELECT COUNT(*) FROM level_reward_claims WHERE guild_id=? "
                   "AND user_id=?", (GUILD, OTHER)) == [(0,)])
    check("no reward definition was touched",
          rows("SELECT COUNT(*) FROM leveling_currency_rewards")[0][0] == 2
          and rows("SELECT COUNT(*) FROM leveling_rewards")[0][0] == 1)

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL /SETLEVEL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
