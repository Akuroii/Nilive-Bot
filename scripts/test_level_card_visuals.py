"""The /level card visuals: progress path, moving plane, and the three buttons.

Everything here drives the real runtime path - `Leveling.level` (the slash
command), `_level_embed()` (the only renderer for both pages) and
`LevelRewardView` (Level / Stats / Claim All) - over a real SQLite database with
fake Discord members. No Discord API call is made, so these are runtime
integration tests, not live Discord tests.

What it locks down:

* the level page shows the current level on the left and the next configured,
  eligible Level Reward milestone on the right ("REWARD L{n}"), falling back to
  "LEVEL {n+1}" when no eligible milestone is ahead; the progress path and its
  numbers under it keep measuring the current level only;
* "Rewards Available" lists only rewards the member can claim right now - never
  locked, not-yet-crossed or already-delivered rows;
* the path uses ◆ for reached stations, ◇ for stations ahead and ━━ between
  them, and the plane position is derived from real XP - it moves monotonically
  and is never a static decoration;
* the reward section is titled "Rewards Available" and lists what the member can
  actually claim first (including earlier levels Claim All would collect),
  without dropping the locked / fulfilled / not-crossed rows;
* all three buttons keep their exact component JSON (rows, labels, styles,
  disabled flags) and their existing interaction behaviour, including the
  wrong-user guard and the real payout behind Claim All.

Run with:
    python scripts/test_level_card_visuals.py
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import (GUILD, USER, execute, interaction, reset_database,
                            rows)

PLANE = "\u2708\ufe0e"    # ✈︎ the member's position (text presentation)
REACHED = "\u25c6"        # ◆ a station already reached
AHEAD = "\u25c7"          # ◇ a station not reached yet
LINK = "\u2501"           # ━ the path itself
ARROW = "\u27a4"          # ➤ current level -> next level

COIN_L3, COIN_L5, COIN_L10 = 100, 250, 900
BOOST_ROLE_L5, BOOST_ROLE_L8 = 601, 602
MILESTONE_EVERYONE, MILESTONE_BOOSTER = 10, 8
BOOSTING = datetime(2026, 1, 1, tzinfo=timezone.utc)
EMPTY = "Nothing to claim right now."


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" - {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def leveling_cog(bot=None):
    from cogs.leveling import Leveling
    cog = Leveling.__new__(Leveling)
    cog.bot = bot or SimpleNamespace(get_guild=lambda _gid: None)
    cog._xp_cooldowns = {}
    cog._spam_tracker = {}
    cog._spam_incidents = {}
    cog._spam_warn_times = {}
    return cog


def seed_rewards():
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?,?,?,'everyone')",
            (GUILD, 3, "balance", COIN_L3))
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?,?,?,'everyone')",
            (GUILD, 5, "balance", COIN_L5))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'boosters')", (GUILD, 5, BOOST_ROLE_L5))
    # A Booster-only milestone between the member and the next Everyone one: a
    # verified booster aims at L8, anybody else aims at L10.
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,'boosters')", (GUILD, MILESTONE_BOOSTER, BOOST_ROLE_L8))
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?,?,?,'everyone')",
            (GUILD, MILESTONE_EVERYONE, "balance", COIN_L10))


def balance():
    found = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                 (GUILD, USER))
    return found[0][0] if found else 0


def field_text(embed):
    return "\n".join(f.value for f in embed.fields)


def field_names(embed):
    return [f.name for f in embed.fields]


async def main():
    await reset_database()
    import cogs.leveling as leveling
    from cogs.leveling import LevelRewardView, _level_embed, _progress_path
    from utils.level_claims import (ensure_tables, list_claims,
                                    member_is_booster, next_reward_level)
    from utils.reward_engine import give_reward
    from utils.xp_calculator import xp_for_level, xp_progress

    await ensure_tables()
    for gid in (GUILD, GUILD + 1):
        execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (gid,))
        execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) "
                "VALUES (?,?,0,0,0)", (gid, USER))
        execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
                "VALUES (?,?,0,0)", (gid, USER))
    seed_rewards()          # GUILD only; GUILD + 1 has no Level Rewards at all

    guild = SimpleNamespace(id=GUILD, get_role=lambda rid: None,
                            me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    bare_guild = SimpleNamespace(id=GUILD + 1, get_role=lambda rid: None,
                                 me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    known = {GUILD: guild, GUILD + 1: bare_guild}
    bot = SimpleNamespace(get_guild=lambda gid: known.get(int(gid)))
    guild.get_member = lambda uid: SimpleNamespace(id=USER) if int(uid) == USER else None

    async def to_level(level, extra=0, guild_id=GUILD):
        total = sum(xp_for_level(n) for n in range(1, level + 1)) + extra
        current = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                       (guild_id, USER))[0][0]
        await give_reward(bot, guild_id, USER, "xp", amount=total - current,
                          reason="card-test", source="leveling")
        return total

    # ── 1. the path helper: geometry, edges, monotonic movement ──
    print("== 1. progress path geometry ==")
    path = _progress_path(0, 1000)
    bare = path.replace(PLANE, "")
    check("five stations, four links, one plane",
          bare.count(REACHED) + bare.count(AHEAD) == 5
          and bare.count(LINK) == 8 and path.count(PLANE) == 1, path)
    check("the plane is inserted into the path, never added to it",
          len(path) == len(bare) + len(PLANE), f"{len(path)} vs {len(bare)}")
    check("at 0 XP the first station is reached and the plane is at the start",
          path.startswith(f"{REACHED}{PLANE}") and path.count(REACHED) == 1, path)
    full = _progress_path(1000, 1000)
    check("at 100% every station is reached and the plane is on the final link",
          full.count(REACHED) == 5 and AHEAD not in full
          and full.endswith(f"{LINK}{PLANE}{REACHED}"), full)
    check("over-earning and a zero denominator cannot break the path",
          _progress_path(5000, 1000) == full
          and _progress_path(10, 0) == _progress_path(0, 0)
          and _progress_path(-5, 1000) == _progress_path(0, 1000),
          _progress_path(10, 0))
    # needed=12 with current=0..11 lands exactly on the twelve segment
    # sub-positions (4 connectors x start/middle/end), so the plane has to move
    # on every single XP step of this sample.
    indexes = [_progress_path(step, 12).index(PLANE) for step in range(12)]
    check("the plane moves monotonically with XP (12 distinct positions)",
          indexes == sorted(indexes) and len(set(indexes)) == 12, str(indexes))
    reached = [_progress_path(step, 12).count(REACHED) for step in range(13)]
    check("stations flip to reached as the plane passes them",
          reached == sorted(reached) and reached[0] == 1 and reached[-1] == 5,
          str(reached))
    check("each XP step of the sample changes the drawn path",
          len({_progress_path(step, 12) for step in range(12)}) == 12)
    # The regression this locks down: at the END of a link (slot 3k+2) the next
    # station used to flip to ◆ one slot early, drawing `◆━━◆━━✈︎◆━━◇━━◇` at
    # ~46% progress - a reached station immediately after the plane for a
    # station the member has not reached. A station may only be ◆ once the
    # plane's own progress has actually arrived at it.
    overdrawn = []
    for step in range(12):
        drawn = _progress_path(step, 12)
        glyphs = [c for c in drawn if c in (REACHED, AHEAD)]
        reached = glyphs.index(AHEAD) if AHEAD in glyphs else len(glyphs)
        # station i sits at i/(stations-1) of the path; the plane is at step/12
        if (reached - 1) / (len(glyphs) - 1) > step / 12 + 1e-9:
            overdrawn.append((step, drawn))
        if PLANE in drawn and drawn[drawn.index(PLANE) + len(PLANE):].startswith(REACHED):
            overdrawn.append((step, drawn, "reached station glued after the plane"))
    check("no station is marked reached ahead of the plane (all 12 positions)",
          not overdrawn, str(overdrawn))
    check("the end of a link leaves the next station unreached",
          _progress_path(2, 12) == f"{REACHED}{LINK * 2}{PLANE}{AHEAD}{LINK * 2}{AHEAD}{LINK * 2}{AHEAD}{LINK * 2}{AHEAD}"
          and _progress_path(5, 12) == f"{REACHED}{LINK * 2}{REACHED}{LINK * 2}{PLANE}{AHEAD}{LINK * 2}{AHEAD}{LINK * 2}{AHEAD}",
          _progress_path(5, 12))

    # ── 1b. the station boundary, proven rather than asserted ──
    # The approved shape is 5 stations, 4 two-dash links, one plane inside the
    # active link: 12 slots, slot s lights s//3 + 1 stations and puts the plane
    # in link s//3 at offset s%3. Written out as literal glyphs below so this is
    # an oracle a reviewer can eyeball, not a second implementation of the
    # function under test. The glyph identities are asserted first so the table
    # cannot silently drift from the production constants.
    print("== 1b. station boundary: 46/49/50%, all 12 slots, zero denominator ==")
    P = PLANE
    check("the test's glyphs are the production glyphs",
          REACHED == "\u25c6" and AHEAD == "\u25c7" and LINK == "\u2501"
          and PLANE == leveling._PATH_PLANE and LINK * 2 == leveling._PATH_LINK,
          f"{REACHED!r} {AHEAD!r} {LINK!r} {PLANE!r}")
    APPROVED = [
        f"\u25c6{P}\u2501\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 0  0%
        f"\u25c6\u2501{P}\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 1
        f"\u25c6\u2501\u2501{P}\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 2  end of link 0
        f"\u25c6\u2501\u2501\u25c6{P}\u2501\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 3
        f"\u25c6\u2501\u2501\u25c6\u2501{P}\u2501\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 4
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501{P}\u25c7\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 5  ~46% / 49%
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6{P}\u2501\u2501\u25c7\u2501\u2501\u25c7",   # 6  exactly 50%
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501{P}\u2501\u25c7\u2501\u2501\u25c7",   # 7
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501{P}\u25c7\u2501\u2501\u25c7",   # 8
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6{P}\u2501\u2501\u25c7",   # 9
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501{P}\u2501\u25c7",   # 10
        f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501{P}\u25c7",   # 11
    ]
    COMPLETED = f"\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501\u25c6\u2501\u2501{P}\u25c6"
    LIT = [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4]

    # All twelve positions, each drawn from a (current, needed) pair that really
    # lands on it: needed=1200 and current=slot*100 gives slot = current*12//1200.
    wrong_slots = [(slot, _progress_path(slot * 100, 1200), want)
                   for slot, want in enumerate(APPROVED)
                   if _progress_path(slot * 100, 1200) != want]
    check("all 12 positions draw the approved path exactly",
          not wrong_slots, str(wrong_slots))
    check("each slot lights exactly the approved number of stations",
          [_progress_path(slot * 100, 1200).count(REACHED) for slot in range(12)] == LIT,
          str([_progress_path(slot * 100, 1200).count(REACHED) for slot in range(12)]))
    check("the path starts at the first station and ends only when the level is done",
          _progress_path(0, 1200) == APPROVED[0]
          and _progress_path(1199, 1200) == APPROVED[11]
          and _progress_path(1200, 1200) == COMPLETED
          and _progress_path(99999, 1200) == COMPLETED,
          _progress_path(1199, 1200))

    # The boundary the review asked to see proven, not assumed. Station index 2
    # (the third station) sits at 2/4 = 50% of the path, so it must still be ◇ at
    # 49% and must be ◆ at exactly 50%.
    def station_glyphs(drawn):
        return [c for c in drawn if c in (REACHED, AHEAD)]

    at46 = _progress_path(46, 100)
    at49 = _progress_path(4900, 10000)
    one_under = _progress_path(4999, 10000)
    at50 = _progress_path(5000, 10000)
    check("46% and 49% both draw the approved ~46% path, third station unreached",
          at46 == at49 == APPROVED[5] == one_under
          and station_glyphs(at49)[2] == AHEAD
          and station_glyphs(at49).count(REACHED) == 2, f"{at46} / {at49}")
    check("exactly 50% lights the third station and moves the plane onto link 2",
          at50 == APPROVED[6] and station_glyphs(at50)[2] == REACHED
          and station_glyphs(at50).count(REACHED) == 3, at50)
    check("the flip happens at the boundary XP, one either side",
          _progress_path(4999, 10000) != _progress_path(5000, 10000)
          and all(_progress_path(c, 10000) == APPROVED[5] for c in (4167, 4600, 4900, 4999))
          and all(_progress_path(c, 10000) == APPROVED[6] for c in (5000, 5100, 5833)),
          f"{[(_progress_path(c, 10000)) for c in (4999, 5000)]}")

    # The general rule behind those two numbers, checked exhaustively rather than
    # at one convenient level: a station may only be ◆ once real progress has
    # reached the fraction of the path that station sits at, and all five are
    # reached only by a completed level.
    violations = []
    for needed in (1, 2, 3, 4, 7, 12, 100, 999, 1200, 14264):
        for current in range(0, needed + 1):
            drawn = _progress_path(current, needed)
            glyphs = station_glyphs(drawn)
            lit = glyphs.count(REACHED)
            if (lit == 5) != (current >= needed):
                violations.append((current, needed, drawn, "all-five vs completed"))
            for k in range(lit):
                # station k sits at k/(stations-1); lighting it needs
                # current/needed >= k/(stations-1), in integers.
                if current * (len(glyphs) - 1) < k * needed:
                    violations.append((current, needed, drawn, f"station {k} early"))
            if len(glyphs) != 5 or drawn.count(PLANE) != 1:
                violations.append((current, needed, drawn, "geometry"))
    check("no station fills before real progress reaches it, and only a completed "
          "level fills all five (every XP of 10 sample levels)",
          not violations, str(violations[:4]))

    # Zero denominator: needed=0 must not divide, and must not invent progress.
    zero = _progress_path(0, 0)
    check("a zero denominator draws the path start, whatever current says",
          zero == APPROVED[0] == _progress_path(999, 0) == _progress_path(-5, 0)
          == _progress_path(5, -10) and station_glyphs(zero).count(REACHED) == 1,
          zero)

    # ── 2. the real /level embed: header, path, XP numbers ──
    print("== 2. the level page renders the reference layout ==")
    member = SimpleNamespace(id=USER, premium_since=None)
    boosting = SimpleNamespace(id=USER, premium_since=BOOSTING)
    await to_level(5)
    xp = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0]
    level, current, needed = xp_progress(xp)
    embed = await _level_embed(guild, member, "level")
    milestone = await next_reward_level(GUILD, level, boosters=False)
    check("the current level is on the left, the next reward milestone on the right",
          milestone == MILESTONE_EVERYONE and milestone != level + 1
          and embed.title == f"LEVEL {level} {ARROW} REWARD L{milestone}",
          f"{embed.title} (milestone L{milestone}, level+1 L{level + 1})")
    # Every configured Everyone milestone above each level, in one pass: from
    # L2 the next is L3, and from L5/L7/L8 it is L10 (L8 is Booster-only).
    walked = [(n, await next_reward_level(GUILD, n, boosters=False))
              for n in (2, 3, 5, 7, 8)]
    check("the milestone comes from the configured rewards, not from level+1",
          walked == [(2, 3), (3, 5), (5, MILESTONE_EVERYONE),
                     (7, MILESTONE_EVERYONE), (8, MILESTONE_EVERYONE)],
          str(walked))
    check("a Booster-only milestone is skipped for a member who is not boosting",
          await next_reward_level(GUILD, level, boosters=True) == MILESTONE_BOOSTER
          and f"L{MILESTONE_BOOSTER}" not in embed.title, embed.title)
    check("a verified booster aims at the Booster-only milestone instead",
          (await _level_embed(guild, boosting, "level")).title
          == f"LEVEL {level} {ARROW} REWARD L{MILESTONE_BOOSTER}",
          (await _level_embed(guild, boosting, "level")).title)
    check("an unverifiable boost status fails closed, like the claim gate does",
          await next_reward_level(GUILD, level,
                                  boosters=member_is_booster(SimpleNamespace(id=USER)))
          == MILESTONE_EVERYONE
          and await next_reward_level(GUILD, level, boosters=member_is_booster(None))
          == MILESTONE_EVERYONE)
    check("no eligible milestone ahead reports None instead of inventing a target",
          await next_reward_level(GUILD, MILESTONE_EVERYONE) is None
          and await next_reward_level(GUILD, MILESTONE_EVERYONE + 5) is None
          and await next_reward_level(GUILD + 1, 0) is None)
    await to_level(4, 300, GUILD + 1)
    bare = await _level_embed(bare_guild, member, "level")
    bare_level, bare_current, bare_needed = xp_progress(
        rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
             (GUILD + 1, USER))[0][0])
    check("a guild with no Level Rewards keeps the LEVEL n ➤ LEVEL n+1 heading",
          bare_level == 4 and bare.title == f"LEVEL {bare_level} {ARROW} LEVEL 5"
          and bare.fields[0].value == EMPTY, f"{bare.title} / {bare.fields[0].value}")
    check("the fallback heading keeps the same within-level bar",
          bare.description.endswith(f"{bare_current} / {bare_needed} XP")
          and PLANE in bare.description, bare.description)
    span = sum(xp_for_level(n) for n in range(level + 1, milestone + 1))
    check("the bar still measures one level, not the distance to the milestone",
          needed == xp_for_level(level + 1) and needed != span
          and embed.description.endswith(f"{current} / {needed} XP"),
          f"one level={needed} XP vs L{level}->L{milestone}={span} XP")
    check("the path is drawn under the level line",
          all(g in embed.description for g in (REACHED, AHEAD, LINK, PLANE)),
          embed.description)
    check("the numeric XP progress sits beside the path",
          embed.description.endswith(f"{current} / {needed} XP"), embed.description)
    check("the numbers agree with the engine, not with a second calculation",
          (level, current, needed) == xp_progress(xp)
          and needed == xp_for_level(level + 1), f"{current}/{needed}")
    check("the reward section is titled Rewards Available",
          field_names(embed) == ["Rewards Available"], str(field_names(embed)))

    # the plane really follows XP on the live embed, not just in the helper
    positions = []
    for extra in (0, 300, 700, 1100):
        await to_level(5, extra)
        page = await _level_embed(guild, member, "level")
        positions.append(page.description.index(PLANE))
    check("the plane moves along the path as the member earns XP",
          positions == sorted(positions) and len(set(positions)) == len(positions),
          str(positions))

    # ── 3. Rewards Available lists what can actually be claimed ──
    print("== 3. Rewards Available content ==")
    embed = await _level_embed(guild, member, "level")
    lines = [line for line in embed.fields[0].value.split("\n") if line]
    check("every reward is a bullet line", all(l.startswith("- ") for l in lines),
          str(lines))
    check("the claimable reward at this level is listed first",
          f"- {COIN_L5}x Coins · pending" in lines[0], lines[0])
    check("an earlier level Claim All would collect is listed too",
          any(f"L3 · {COIN_L3}x Coins · ready to claim" in line for line in lines),
          str(lines))
    check("the footer still counts the earlier rewards",
          embed.footer.text == "Claim All also collects 1 earlier reward(s).",
          embed.footer.text)
    check("rewards the member cannot claim are not offered on this page",
          not any(word in embed.fields[0].value for word in
                  ("(Boosters only)", "locked", "not earned", "not crossed",
                   "fulfilled")), str(lines))

    claims = await list_claims(GUILD, USER)
    view = LevelRewardView(bot, GUILD, USER, claimable=True)
    itx = SimpleNamespace(
        user=member, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 edit_message=AsyncMock()),
        edit_original_response=AsyncMock())
    before = balance()
    await view.claim_all.callback(itx)
    paid = balance() - before
    check("Claim All still pays the real rewards (both levels)",
          paid == COIN_L3 + COIN_L5, f"paid={paid}")
    after = await _level_embed(guild, member, "level")
    after_lines = after.fields[0].value
    check("delivered rewards leave Rewards Available (claimable only)",
          after_lines == EMPTY and "fulfilled" not in after_lines
          and "ready to claim" not in after_lines, after_lines)
    check("the section and the Claim All button agree on what is claimable",
          await leveling._claim_button_available(
              GUILD, await list_claims(GUILD, USER), False) is False
          and after.footer.text is None, str(after.footer.text))
    stats = await _level_embed(guild, member, "stats")
    check("the Stats page still reports the delivered rewards",
          "Claimed" in field_names(stats)
          and f"{COIN_L5}x Coins" in field_text(stats), field_text(stats))
    footer = itx.edit_original_response.await_args.kwargs["embed"].footer.text
    check("the Claim All footer still reports the real result",
          footer.startswith("Claimed 2.") and "Retryable failures: 0." in footer,
          footer)
    check("Claim All re-rendered the card page, not the stats page",
          itx.edit_original_response.await_args.kwargs["embed"].title.startswith("LEVEL ")
          and itx.edit_original_response.await_args.kwargs["view"] is view)

    # ── 4. the real slash command and all three buttons ──
    print("== 4. /level command and button interactions ==")
    execute("INSERT INTO leveling_currency_rewards "
            "(guild_id,level,currency,amount,audience) VALUES (?,?,?,?,'everyone')",
            (GUILD, 6, "balance", 500))
    await to_level(6)                      # a fresh crossing => claimable again
    cog = leveling_cog(bot)
    itx = interaction()
    await leveling.Leveling.level.callback(cog, itx)
    check("/level defers ephemerally before rendering",
          itx.response.defer.await_args.kwargs.get("ephemeral") is True)
    sent = itx.followup.send.await_args.kwargs
    check("/level sends exactly one ephemeral card with an embed and a view",
          sent.get("ephemeral") is True and sent["embed"] is not None
          and isinstance(sent["view"], LevelRewardView), str(sorted(sent)))
    check("the card /level sends is the level page with the path",
          sent["embed"].title.startswith("LEVEL ")
          and PLANE in (sent["embed"].description or ""), sent["embed"].title)
    check("Claim All is enabled because a reward is claimable again",
          sent["view"].claim_all.disabled is False
          and sent["view"].claim_all.style.name == "success"
          and await leveling._claim_button_available(
              GUILD, await list_claims(GUILD, USER), False) is True)
    check("the freshly crossed reward is listed as claimable on the card",
          "500x Coins · pending" in field_text(sent["embed"]),
          field_text(sent["embed"]))

    card = sent["view"]
    # `defer()` must be the FIRST thing a page button does: the ack has to beat
    # every SQLite round trip the render needs, or Discord shows "Nero didn't
    # respond in time". The call log proves the ordering, not just that both
    # were eventually called.
    calls = []
    itx2 = SimpleNamespace(
        user=itx.user, guild=guild,
        response=SimpleNamespace(
            defer=AsyncMock(side_effect=lambda *a, **k: calls.append("defer")),
            send_message=AsyncMock(),
            edit_message=AsyncMock(
                side_effect=lambda *a, **k: calls.append("edit_message"))),
        edit_original_response=AsyncMock(
            side_effect=lambda *a, **k: calls.append("edit_original_response")))
    await card.show_stats.callback(itx2)
    check("Stats acknowledges the interaction before rendering",
          calls == ["defer", "edit_original_response"]
          and itx2.response.defer.await_count == 1, str(calls))
    stats = itx2.edit_original_response.await_args.kwargs
    check("Stats swaps in the stats page and highlights itself",
          stats["embed"].title == "Level stats"
          and card.show_stats.style.name == "primary"
          and card.show_level.style.name == "secondary"
          and stats["view"] is card, str(stats["embed"].title))
    check("the stats page keeps its own claim history fields",
          field_names(stats["embed"])[:2] == ["Claimed", "Waiting"],
          str(field_names(stats["embed"])))
    check("the page edit goes to the deferred response, never to response.edit_message",
          itx2.response.edit_message.await_count == 0)

    calls.clear()
    await card.show_level.callback(itx2)
    back = itx2.edit_original_response.await_args.kwargs
    check("Level returns to the card page, path included, and re-highlights",
          PLANE in (back["embed"].description or "")
          and back["embed"].title.startswith("LEVEL ")
          and card.show_level.style.name == "primary"
          and card.show_stats.style.name == "secondary"
          and back["view"] is card, back["embed"].title)
    check("Level acknowledges first too", calls == ["defer", "edit_original_response"],
          str(calls))

    stranger = SimpleNamespace(id=USER + 999, premium_since=None)
    itx3 = SimpleNamespace(
        user=stranger, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 edit_message=AsyncMock()),
        edit_original_response=AsyncMock())
    await card.show_level.callback(itx3)
    check("somebody else's card is refused ephemerally and never re-rendered",
          itx3.response.send_message.await_args.kwargs.get("ephemeral") is True
          and itx3.response.edit_message.await_count == 0
          and itx3.edit_original_response.await_count == 0
          and itx3.response.defer.await_count == 0)

    # ── 5. the component JSON must not regress ──
    print("== 5. button component JSON ==")
    import discord
    for claimable, style, disabled in ((True, "success", False),
                                       (False, "secondary", True)):
        fresh = LevelRewardView(SimpleNamespace(), GUILD, USER, claimable=claimable)
        rows_json = fresh.to_components()
        buttons = [b for row in rows_json for b in row["components"]]
        check(f"claimable={claimable}: three buttons over two rows",
              len(rows_json) == 2 and len(buttons) == 3
              and [len(r["components"]) for r in rows_json] == [2, 1],
              str([[b["label"] for b in r["components"]] for r in rows_json]))
        check(f"claimable={claimable}: labels, order and types are unchanged",
              [b["label"] for b in buttons] == ["Level", "Stats", "Claim All"]
              and all(b["type"] == 2 for b in buttons)
              and all(r["type"] == 1 for r in rows_json), str(buttons))
        check(f"claimable={claimable}: styles and disabled flags are unchanged",
              [b["style"] for b in buttons]
              == [int(discord.ButtonStyle.primary),
                  int(discord.ButtonStyle.secondary),
                  int(getattr(discord.ButtonStyle, style))]
              and [b["disabled"] for b in buttons] == [False, False, disabled],
              str([(b["style"], b["disabled"]) for b in buttons]))
        check(f"claimable={claimable}: every button keeps a unique custom_id",
              len({b["custom_id"] for b in buttons}) == 3
              and all(b["custom_id"] for b in buttons))
        check(f"claimable={claimable}: the view still expires after 180s",
              fresh.timeout == 180, str(fresh.timeout))

    print("ALL LEVEL CARD VISUAL CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)
