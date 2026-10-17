"""Post-merge /level fixes: path rendering, Stats ack, claim confirmation, labels.

One focused suite for the confirmed defects behind the post-merge audit. It
drives the real runtime path - `cogs/leveling.py` (the module `main.load_cogs()`
imports) and `utils/level_claims.py` - against a scratch SQLite database with
fake Discord objects, so nothing here touches a real guild or a real token.

What it locks down:

1. progress path  - a station is ◆ only once the plane's own XP progress has
   arrived at it. The old `+1 if step == 2` lit the next station one slot early
   (`◆━━◆━━✈︎◆━━◇━━◇` at ~46%). Geometry, plane insertion and monotonicity are
   unchanged; only the reached/unreached decision is corrected.
2. Stats/Level    - the interaction is acknowledged BEFORE any database work,
   and the page lands on the deferred response. This is the "Nero didn't respond
   in time" fix: the render opens several SQLite connections and used to run
   inside Discord's 3-second window with nothing sent.
3. claim rules    - an XP reduction keeps previously earned entitlements (they
   are legitimately owed), and re-levelling cannot pay any of them twice.
4. confirmation   - Claim All names what it actually delivered, totals it, and
   reports a partial failure honestly instead of only counting.
5. button state   - green only while a reward is waiting; grey+enabled for the
   exclusive-role reconcile action; grey+disabled when finished. Repeat clicks
   cannot deliver twice.
6. Stats labels   - "Highest unclaimed" no longer says "nothing crossed yet" on
   a page that just reported N of N crossed rewards delivered, and neither field
   claims everything is delivered while a row is in flight or stranded.

Run with:
    python scripts/test_level_card_fixes.py
"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, execute, rows, reset_database

PLANE = "\u2708\ufe0e"
REACHED = "\u25c6"
AHEAD = "\u25c7"
LINK = "\u2501"

COIN_L3, COIN_L5, GEM_L7 = 100, 250, 40
ROLE_L5 = 601


class FakeRole(SimpleNamespace):
    """A stand-in for discord.Role that hashes the way the real one does.

    SimpleNamespace defines __eq__, which clears __hash__, so it cannot go into a
    set. `reconcile_role_progression()` does `set(member.roles)` and discord.Role
    is hashed by id, so the exclusive-role reconcile path (reached through the
    Restore Level Role button) needs a hashable stand-in.
    """

    def __hash__(self):
        return hash(("role", self.id))

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


def set_exclusive(on: bool):
    """The dashboard's `remove_old_reward_role` setting (0 = OFF, 1 = ON)."""
    execute("INSERT INTO leveling_config (guild_id, remove_old_reward_role) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "remove_old_reward_role=excluded.remove_old_reward_role",
            (GUILD, 1 if on else 0))


def balance():
    found = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER))
    return found[0][0] if found else 0


def diamonds():
    found = rows("SELECT diamonds FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER))
    return found[0][0] if found else 0


def field(embed, name):
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


def component_itx(user, guild, log=None):
    """A component interaction that records the ORDER of acknowledgement calls."""
    def rec(name):
        async def _side(*a, **k):
            if log is not None:
                log.append(name)
        return _side
    return SimpleNamespace(
        user=user, guild=guild,
        response=SimpleNamespace(
            defer=AsyncMock(side_effect=rec("defer")),
            send_message=AsyncMock(side_effect=rec("send_message")),
            edit_message=AsyncMock(side_effect=rec("response.edit_message"))),
        edit_original_response=AsyncMock(side_effect=rec("edit_original_response")),
        followup=SimpleNamespace(send=AsyncMock(side_effect=rec("followup.send"))))


async def main():
    await reset_database()
    import cogs.leveling as leveling
    from cogs.leveling import (LevelRewardView, _claim_button_state, _level_embed,
                               _progress_path, claim_confirmation)
    from utils.level_claims import (ensure_tables, list_claims, record_crossing)
    from utils.reward_engine import give_reward
    from utils.xp_calculator import xp_for_level

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,0,0,0)",
            (GUILD, USER))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, USER))
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,audience) "
            "VALUES (?,?, 'balance', ?, 'everyone')", (GUILD, 3, COIN_L3))
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,audience) "
            "VALUES (?,?, 'balance', ?, 'everyone')", (GUILD, 5, COIN_L5))
    execute("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount,audience) "
            "VALUES (?,?, 'diamonds', ?, 'everyone')", (GUILD, 7, GEM_L7))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'everyone')", (GUILD, 5, ROLE_L5))

    real_role = FakeRole(id=ROLE_L5, mention=f"<@&{ROLE_L5}>",
                         position=10, name="Moon")
    guild = SimpleNamespace(
        id=GUILD, get_role=lambda rid: real_role if rid == ROLE_L5 else None,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    member = SimpleNamespace(id=USER, mention=f"<@{USER}>", display_name="Tester",
                             premium_since=None, guild=guild, roles=[])
    member.add_roles = AsyncMock(side_effect=lambda *r, **k: member.roles.extend(r))
    member.remove_roles = AsyncMock(
        side_effect=lambda *r, **k: [member.roles.remove(x) for x in r if x in member.roles])
    guild.get_member = lambda uid: member if uid == USER else None
    guild.fetch_member = AsyncMock(return_value=member)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)

    async def to_level(level, extra=0):
        total = sum(xp_for_level(n) for n in range(1, level + 1)) + extra
        current = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                       (GUILD, USER))[0][0]
        await give_reward(bot, GUILD, USER, "xp", amount=total - current,
                          reason="fix-test", source="leveling")
        return total

    # ── 1. progress path: reached/unreached at every plane position ──
    print("== 1. progress path ==")
    drawn = [_progress_path(slot * 100 + 50, 1200) for slot in range(12)]
    overdrawn = []
    for slot, path in enumerate(drawn):
        glyphs = [c for c in path if c in (REACHED, AHEAD)]
        reached = glyphs.index(AHEAD) if AHEAD in glyphs else len(glyphs)
        progress = (slot * 100 + 50) / 1200
        if (reached - 1) / (len(glyphs) - 1) > progress + 1e-9:
            overdrawn.append((slot, path, reached, round(progress, 3)))
        after_plane = path[path.index(PLANE) + len(PLANE):]
        if after_plane.startswith(REACHED):
            overdrawn.append((slot, path, "reached station glued after the plane"))
    check("no position marks a station reached ahead of the plane",
          not overdrawn, str(overdrawn))
    check("the reported end-of-link cases now leave the next station unreached",
          drawn[2] == f"{REACHED}{LINK * 2}{PLANE}{AHEAD}{LINK * 2}{AHEAD}{LINK * 2}{AHEAD}{LINK * 2}{AHEAD}"
          and drawn[5] == f"{REACHED}{LINK * 2}{REACHED}{LINK * 2}{PLANE}{AHEAD}{LINK * 2}{AHEAD}{LINK * 2}{AHEAD}"
          and drawn[8] == f"{REACHED}{LINK * 2}{REACHED}{LINK * 2}{REACHED}{LINK * 2}{PLANE}{AHEAD}{LINK * 2}{AHEAD}"
          and drawn[11] == f"{REACHED}{LINK * 2}{REACHED}{LINK * 2}{REACHED}{LINK * 2}{REACHED}{LINK * 2}{PLANE}{AHEAD}",
          str([drawn[2], drawn[5], drawn[8], drawn[11]]))
    check("geometry is unchanged: 5 stations, 4 two-char links, 1 inserted plane",
          all(len([c for c in p if c in (REACHED, AHEAD)]) == 5
              and p.count(LINK) == 8 and p.count(PLANE) == 1
              and len(p) == len(p.replace(PLANE, "")) + len(PLANE) for p in drawn))
    positions = [p.index(PLANE) for p in drawn]
    check("the plane still moves monotonically through 12 distinct positions",
          positions == sorted(positions) and len(set(positions)) == 12, str(positions))
    check("12 distinct paths, reached count monotonic, a finished level fills all 5",
          len(set(drawn)) == 12
          and [p.count(REACHED) for p in drawn] == sorted(p.count(REACHED) for p in drawn)
          and _progress_path(1200, 1200).count(REACHED) == 5
          and AHEAD not in _progress_path(1200, 1200),
          str([p.count(REACHED) for p in drawn]))
    check("the zero-denominator and negative-input paths are unchanged",
          _progress_path(10, 0) == _progress_path(0, 0)
          and _progress_path(-5, 1200) == _progress_path(0, 1200),
          _progress_path(10, 0))

    # ── 2. Stats/Level acknowledge before any database work ──
    print("== 2. component acknowledgement ==")
    await to_level(5)
    view = LevelRewardView(bot, GUILD, USER, claimable=True)
    log = []
    itx = component_itx(member, guild, log)
    await view.show_stats.callback(itx)
    check("Stats defers first and edits the deferred response",
          log == ["defer", "edit_original_response"], str(log))
    check("Stats never uses the single blocking response.edit_message",
          itx.response.edit_message.await_count == 0)
    stats = itx.edit_original_response.await_args.kwargs["embed"]
    check("the deferred edit carries the real stats page and the same view",
          stats.title == "Level stats"
          and itx.edit_original_response.await_args.kwargs["view"] is view
          and field(stats, "Claimed") is not None)
    log.clear()
    await view.show_level.callback(itx)
    check("Level defers first too", log == ["defer", "edit_original_response"], str(log))
    log.clear()
    stranger = SimpleNamespace(id=USER + 1, premium_since=None)
    itx_stranger = component_itx(stranger, guild, log)
    await view.show_stats.callback(itx_stranger)
    check("a stranger is refused without deferring or editing anything",
          log == ["send_message"]
          and itx_stranger.response.send_message.await_args.kwargs.get("ephemeral") is True,
          str(log))

    # ── 3. XP reduction: earned entitlements persist, and never pay twice ──
    print("== 3. reward eligibility after an XP reduction ==")
    before = rows("SELECT reward_level,track,reward_ref,status FROM level_reward_claims "
                  "WHERE guild_id=? AND user_id=? ORDER BY 1,2", (GUILD, USER))
    cog = leveling_cog(bot)
    low_xp = sum(xp_for_level(n) for n in range(1, 3))
    itx_set = SimpleNamespace(user=member, guild=guild,
                              response=SimpleNamespace(defer=AsyncMock(),
                                                       send_message=AsyncMock()),
                              followup=SimpleNamespace(send=AsyncMock()))
    await leveling.Leveling.setxp.callback(cog, itx_set, member, low_xp)
    after_down = rows("SELECT reward_level,track,reward_ref,status FROM level_reward_claims "
                      "WHERE guild_id=? AND user_id=? ORDER BY 1,2", (GUILD, USER))
    now_level = rows("SELECT level FROM levels WHERE guild_id=? AND user_id=?",
                     (GUILD, USER))[0][0]
    check("reducing XP does not revoke an already-earned entitlement",
          after_down == before and now_level == 2 and len(before) >= 3,
          f"level={now_level} before={before} after={after_down}")
    page = await _level_embed(guild, member, "level")
    available = field(page, "Rewards Available")
    check("the reduced member still sees every legitimately claimable reward",
          "L3" in available and "L5" in available, available)

    from utils.level_claims import claim_available
    paid_before = balance()
    first = await claim_available(GUILD, USER, member=member, bot=bot)
    paid = balance() - paid_before
    check("Claim All pays the earlier rewards at the reduced level",
          paid == COIN_L3 + COIN_L5 and len(first["fulfilled"]) >= 2,
          f"paid={paid} fulfilled={first['fulfilled']}")
    ids_before = {r[0] for r in rows(
        "SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER))}
    await to_level(7)
    dup = rows("SELECT reward_level,track,reward_ref,COUNT(*) FROM level_reward_claims "
               "WHERE guild_id=? AND user_id=? GROUP BY 1,2,3 HAVING COUNT(*)>1",
               (GUILD, USER))
    replay = await claim_available(GUILD, USER, member=member, bot=bot)
    check("re-levelling creates no duplicate identity and re-pays nothing already delivered",
          dup == [] and paid_before + paid == balance()
          and ids_before <= {r[0] for r in rows(
              "SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=?",
              (GUILD, USER))},
          f"dup={dup} balance={balance()} expected={paid_before + paid}")
    check("only the newly crossed reward was paid on the way back up",
          replay["delivered_currency"] == 1 and replay["delivered_roles"] == 0
          and diamonds() == GEM_L7,
          f"{ {k: v for k, v in replay.items() if k.startswith('delivered')} } diamonds={diamonds()}")

    # ── 4. Claim All confirmation ──
    print("== 4. claim confirmation ==")
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?, 'balance', 77, 'everyone')",
            (GUILD, 8))
    await to_level(8)                     # a fresh crossing => something to confirm
    claims_now = await list_claims(GUILD, USER)
    res = await claim_available(GUILD, USER, member=member, bot=bot)
    claims_after = await list_claims(GUILD, USER)
    name, value = await claim_confirmation(guild, res, claims_after, False)
    paid = len({int(i) for i in res["fulfilled"]})
    check("the confirmation is Arabic and names what was actually delivered",
          name == "نتيجة الاستلام" and "مبروك" in value and "تم تسليم" in value
          and str(paid) in value, value)
    check("a clear total appears when, and only when, there is more than one reward",
          ("الإجمالي:" in value) == (paid > 1), value)
    totals = leveling._claim_totals(
        [c for c in claims_after if c["id"] in {int(i) for i in res["fulfilled"]}],
        {"coins": {"name": "Coins"}, "diamonds": {"name": "Diamonds"}})
    check("the total aggregates fungible quantities instead of listing arithmetic",
          totals == "77x Coins", totals)
    check("nothing left to claim is stated, not implied",
          "لا يتبقّى شيء للاستلام" in value, value)
    noop = await claim_confirmation(guild, {"fulfilled": [], "failed": []}, claims_after, False)
    check("a no-op repeat click produces no confirmation field at all", noop is None, str(noop))

    # a real partial failure: one reward at L9 pays, the role beside it does not
    member.add_roles = AsyncMock(
        side_effect=RuntimeError("simulated Discord role failure"))
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?, 'balance', 55, 'everyone')",
            (GUILD, 9))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'everyone')", (GUILD, 9, ROLE_L5))
    before_partial = balance()
    await to_level(9)
    part = await claim_available(GUILD, USER, member=member, bot=bot)
    check("the pass really was partial: the currency paid, the role did not",
          balance() - before_partial == 55 and len(part["failed"]) == 1
          and len(part["fulfilled"]) == 1,
          f"delta={balance() - before_partial} fulfilled={part['fulfilled']} failed={part['failed']}")
    after = await list_claims(GUILD, USER)
    name, value = await claim_confirmation(guild, part, after, False)
    check("a partial failure is reported honestly, with the reason and what remains",
          part["failed"] and "ولم يتم تسليم" in value
          and "السبب: simulated Discord role failure" in value
          and "ما زال متاحًا للاستلام" in value, value)
    check("a partial pass never reads as a general success",
          "مبروك" not in value and "⚠️" in value, value)
    check("a reward is called delivered only because the engine paid it",
          value.count("✅ **تم تسليم") == 1
          and str(len(part["fulfilled"])) in value, value)
    check("the delivered part of a partial pass is still itemised",
          "55x Coins" in value, value)
    footer = leveling.claim_result_footer(part)
    check("the existing footer contract is unchanged",
          footer.startswith("Claimed ") and "Retryable failures: 1." in footer, footer)

    # ── 4b. full failure, then a successful retry ──
    # The requirement is that a reward is never presented as delivered merely
    # because a claim row exists, and that a retry after a failure is real. Here
    # every reward in the pass fails, then the same pass is retried with Discord
    # working again.
    print("== 4b. full failure and retry ==")
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'everyone')", (GUILD, 10, ROLE_L5))
    await to_level(10)
    bal_before_retry = balance()
    delivered_before = {r["id"] for r in await list_claims(GUILD, USER)
                        if r["status"] == "fulfilled"}
    none_ok = await claim_available(GUILD, USER, member=member, bot=bot)
    rows_after = await list_claims(GUILD, USER)
    name, value = await claim_confirmation(guild, none_ok, rows_after, False)
    check("a pass where nothing could be delivered says so, in Arabic",
          name == "نتيجة الاستلام" and "تعذّر تسليم المكافآت" in value
          and "مبروك" not in value and "✅" not in value
          and none_ok["fulfilled"] == [] and len(none_ok["failed"]) == 2, value)
    check("each undelivered reward is named with its reason and stays retryable",
          value.count("السبب: simulated Discord role failure") == 2
          and "يمكن إعادة المحاولة" in value
          and "ما زال متاحًا للاستلام" in value, value)
    check("nothing was paid and no row was marked delivered",
          balance() == bal_before_retry
          and {r["id"] for r in rows_after if r["status"] == "fulfilled"}
              == delivered_before,
          str([(r["reward_level"], r["track"], r["status"]) for r in rows_after]))

    # the retry: Discord works again, the failed rows are picked up and paid once
    member.add_roles = AsyncMock(side_effect=lambda *r, **k: member.roles.extend(r))
    retried = await claim_available(GUILD, USER, member=member, bot=bot)
    rows_retry = await list_claims(GUILD, USER)
    name, value = await claim_confirmation(guild, retried, rows_retry, False)
    check("a retry delivers what failed and reports it as a success",
          len(retried["fulfilled"]) == 2 and retried["failed"] == []
          and "مبروك" in value and "تعذّر" not in value
          and "لا يتبقّى شيء للاستلام" in value, value)
    check("the retry names both role rewards and totals them",
          value.count("<@&601>") == 2 and "الإجمالي:" in value
          and "2 role reward(s)" in value, value)
    check("the retry paid nothing twice",
          balance() == bal_before_retry
          and all(r["status"] == "fulfilled" for r in rows_retry
                  if r["reward_level"] in (9, 10)),
          str([(r["reward_level"], r["status"]) for r in rows_retry]))
    again = await claim_confirmation(
        guild, await claim_available(GUILD, USER, member=member, bot=bot),
        await list_claims(GUILD, USER), False)
    check("and a third pass has nothing to confirm at all", again is None, str(again))

    # The confirmation is a Discord embed field, so it is capped at 1024 chars.
    # A truncated reward list must not read as a complete one, and no line may be
    # cut in half - a half-rendered reward name is worse than an omitted one.
    long_list = ["\u0627\u0644\u0633\u0637\u0631 \u0627\u0644\u0623\u0648\u0644"] + [
        f"- L{i} \u00b7 {i}x Coins \u00b7 reward number {i} with a long name"
        for i in range(80)] + ["\u2714 \u0644\u0627 \u064a\u062a\u0628\u0642\u0649 \u0634\u064a\u0621"]
    bounded = leveling._bounded_lines(long_list)
    check("a long confirmation is cut to whole lines and says how many it dropped",
          len(bounded) <= 1024 and bounded.startswith(long_list[0])
          and bounded.endswith(long_list[-1])
          and "\u0633\u0637\u0631\u064b\u0627 \u0623\u062e\u0631\u0649" in bounded
          and all(line in long_list or "\u0623\u062e\u0631\u0649" in line
                  for line in bounded.split("\n")),
          f"{len(bounded)} chars, {len(bounded.splitlines())} lines")
    check("a short confirmation is returned untouched",
          leveling._bounded_lines(["a", "b"]) == "a\nb"
          and leveling._bounded_lines(["only"]) == "only")

    # ── 5. Claim All button state and repeat clicks ──
    print("== 5. button state ==")
    member.add_roles = AsyncMock(side_effect=lambda *r, **k: member.roles.extend(r))
    # 4b's retry left nothing outstanding, so cross one more reward level: this
    # section is about the button's state while something IS waiting, and then
    # after it is not.
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?, 'balance', 33, 'everyone')",
            (GUILD, 11))
    await to_level(11)
    view2 = LevelRewardView(bot, GUILD, USER)
    claims_now = await list_claims(GUILD, USER)
    await view2._apply_claim_state(claims_now, False)
    check("green while a reward is actually waiting",
          view2.claim_all.style.name == "success" and view2.claim_all.disabled is False)
    await claim_available(GUILD, USER, member=member, bot=bot)
    log = []
    itx_claim = component_itx(member, guild, log)
    await view2.claim_all.callback(itx_claim)
    check("after everything is delivered Claim All goes grey and disabled",
          view2.claim_all.style.name == "secondary" and view2.claim_all.disabled is True,
          f"style={view2.claim_all.style.name} disabled={view2.claim_all.disabled}")
    check("Claim All defers, then edits the deferred response once",
          log == ["defer", "edit_original_response"], str(log))
    set_exclusive(True)
    claimable, reconcile_only = await _claim_button_state(
        GUILD, await list_claims(GUILD, USER), False)
    view2.set_claimable(claimable, reconcile_only)
    check("the exclusive-role reconcile gets its own button; Claim All is disabled",
          claimable is False and reconcile_only is True
          and view2.claim_all.style.name == "secondary"
          and view2.claim_all.disabled is True
          and view2.sync_roles in view2.children
          and view2.sync_roles.disabled is False,
          f"claimable={claimable} reconcile={reconcile_only}")
    check("the reconcile button is labelled for what it does, not 'Claim All'",
          view2.sync_roles.label == "Restore Level Role"
          and view2.sync_roles.row == view2.claim_all.row == 1,
          view2.sync_roles.label)
    sync_log = []
    itx_sync = component_itx(member, guild, sync_log)
    await view2.sync_roles.callback(itx_sync)
    check("the reconcile button runs the same deferred, single-response pass",
          sync_log == ["defer", "edit_original_response"], str(sync_log))
    check("_claim_button_available still reports the same reachability (D2 contract)",
          await leveling._claim_button_available(
              GUILD, await list_claims(GUILD, USER), False) is True)
    set_exclusive(False)
    claimable, reconcile_only = await _claim_button_state(
        GUILD, await list_claims(GUILD, USER), False)
    check("nothing claimable and nothing to reconcile -> grey and disabled",
          (claimable, reconcile_only) == (False, False))
    view2.set_claimable(claimable, reconcile_only)
    check("the reconcile button leaves the card again once it has no work",
          view2.sync_roles not in view2.children
          and view2.claim_all.disabled is True
          and [c.label for c in view2.children] == ["Level", "Stats", "Claim All"],
          str([c.label for c in view2.children]))

    # repeat clicks must not deliver twice
    execute("UPDATE level_reward_claims SET status='pending', last_error=NULL, "
            "owner_token=NULL, processing_started_at=NULL "
            "WHERE guild_id=? AND user_id=? AND reward_level=3", (GUILD, USER))
    view3 = LevelRewardView(bot, GUILD, USER)
    await view3._apply_claim_state(await list_claims(GUILD, USER), False)
    before_swarm = balance()
    itxs = [component_itx(member, guild) for _ in range(5)]
    view3._claim_running = True          # a pass is already in flight
    await view3.claim_all.callback(itxs[0])
    check("a click while a pass is in flight is told to wait and delivers nothing",
          itxs[0].response.send_message.await_args.kwargs.get("ephemeral") is True
          and itxs[0].edit_original_response.await_count == 0
          and balance() == before_swarm)
    view3._claim_running = False
    await asyncio.gather(*[view3.claim_all.callback(i) for i in itxs[1:]])
    delivered = sum(1 for i in itxs[1:] if i.edit_original_response.await_count)
    told_to_wait = sum(1 for i in itxs[1:] if i.response.send_message.await_count)
    check("concurrent Claim All clicks pay the reward exactly once",
          balance() - before_swarm == COIN_L3 and delivered == 1 and told_to_wait == 3,
          f"delta={balance() - before_swarm} delivered={delivered} waiting={told_to_wait}")
    check("the view clears its in-flight guard after the pass",
          view3._claim_running is False)
    check("the guard never blocks the next legitimate pass",
          view3.claim_all.disabled is True)

    # ── 6. Stats labels vs the actual records ──
    print("== 6. Stats labels ==")
    await claim_available(GUILD, USER, member=member, bot=bot)
    stats = await _level_embed(guild, member, "stats")
    claimed = field(stats, "Claimed")
    highest = field(stats, "Highest unclaimed")
    waiting = field(stats, "Waiting")
    total_rows = len(await list_claims(GUILD, USER))
    check("Claimed counts every crossed reward row",
          claimed.startswith(f"{total_rows} of {total_rows} crossed rewards"), claimed)
    check("Highest unclaimed no longer contradicts Claimed",
          "nothing crossed yet" not in highest
          and "nothing left to claim" in highest
          and str(total_rows) in highest, highest)
    check("Waiting only says 'delivered' when every row really is fulfilled",
          "every crossed reward is delivered" in waiting, waiting)

    # a row with a LIVE lease is in flight, not delivered
    from datetime import datetime, timezone
    from utils.level_claims import LEASE_SECONDS
    now_iso = datetime.now(timezone.utc).isoformat()
    execute("UPDATE level_reward_claims SET status='processing', owner_token='live', "
            "processing_started_at=? "
            "WHERE guild_id=? AND user_id=? AND reward_level=3",
            (now_iso, GUILD, USER))
    stats = await _level_embed(guild, member, "stats")
    waiting = field(stats, "Waiting")
    highest = field(stats, "Highest unclaimed")
    check("an in-flight row stops the 'every crossed reward is delivered' claim",
          "every crossed reward is delivered" not in waiting
          and "being delivered now" in waiting, waiting)
    check("an in-flight row is not reported as 'nothing crossed yet' either",
          "nothing crossed yet" not in highest and "being delivered now" in highest,
          highest)
    page = await _level_embed(guild, member, "level")
    check("an in-flight row is not offered as claimable on the Level page",
          "ready to claim" not in (field(page, "Rewards Available") or ""),
          field(page, "Rewards Available"))

    # a row with an EXPIRED lease is recoverable: the engine reserves it, so the
    # card must offer it and the labels must not call it delivered
    execute("UPDATE level_reward_claims SET status='processing', owner_token='dead', "
            "processing_started_at='2020-01-01T00:00:00+00:00' "
            "WHERE guild_id=? AND user_id=? AND reward_level=3", (GUILD, USER))
    stale = await list_claims(GUILD, USER)
    stale_row = [c for c in stale if c["reward_level"] == 3][0]
    stats = await _level_embed(guild, member, "stats")
    page = await _level_embed(guild, member, "level")
    check("a stranded lease is reported as claimable, matching what the engine reserves",
          stale_row["status"] == "pending" and stale_row["stale_processing"] is True
          and "being delivered now" not in field(stats, "Waiting"),
          f"{stale_row['status']} / {field(stats, 'Waiting')}")
    check("the Level page offers the stranded reward instead of hiding it",
          "ready to claim" in (field(page, "Rewards Available") or ""),
          field(page, "Rewards Available"))
    check("the Claim All button agrees with the page about the stranded reward",
          await leveling._claim_button_available(GUILD, stale, False) is True)
    before_recover = balance()
    recovered = await claim_available(GUILD, USER, member=member, bot=bot)
    check("the stranded reward is actually recoverable and paid exactly once",
          before_recover + COIN_L3 == balance() and recovered["delivered_currency"] == 1,
          f"delta={balance() - before_recover}")

    # a brand-new member really has crossed nothing
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,0,0,0)",
            (GUILD, USER + 7))
    fresh = SimpleNamespace(id=USER + 7, premium_since=None)
    stats = await _level_embed(guild, fresh, "stats")
    check("a member with no claim rows still reads 'nothing crossed yet'",
          field(stats, "Highest unclaimed") == "nothing crossed yet"
          and field(stats, "Claimed") == "0 of 0 crossed rewards",
          f"{field(stats, 'Claimed')} / {field(stats, 'Highest unclaimed')}")

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LEVEL CARD FIX CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
