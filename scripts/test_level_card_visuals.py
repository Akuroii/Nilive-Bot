"""The /level card visuals: progress path, moving plane, and the three buttons.

Everything here drives the real runtime path - `Leveling.level` (the slash
command), `_level_embed()` (the only renderer for both pages) and
`LevelRewardView` (Level / Stats / Claim All) - over a real SQLite database with
fake Discord members. No Discord API call is made, so these are runtime
integration tests, not live Discord tests.

What it locks down:

* the level page shows the current level on the left and the next level on the
  right, the progress path under it, and the numeric XP progress beside it;
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
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import (GUILD, USER, execute, interaction, reset_database,
                            rows)

PLANE = "\u2708\ufe0e"    # ✈︎ the member's position (text presentation)
REACHED = "\u25c6"        # ◆ a station already reached
AHEAD = "\u25c7"          # ◇ a station not reached yet
LINK = "\u2501"           # ━ the path itself
ARROW = "\u27a4"          # ➤ current level -> next level

COIN_L3, COIN_L5, BOOST_ROLE_L5 = 100, 250, 601


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
    from utils.level_claims import ensure_tables, list_claims
    from utils.reward_engine import give_reward
    from utils.xp_calculator import xp_for_level, xp_progress

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,0,0,0)",
            (GUILD, USER))
    execute("INSERT INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,0,0)",
            (GUILD, USER))
    seed_rewards()

    guild = SimpleNamespace(id=GUILD, get_role=lambda rid: None,
                            me=SimpleNamespace(top_role=SimpleNamespace(position=100)))
    bot = SimpleNamespace(get_guild=lambda gid: guild if int(gid) == GUILD else None)
    guild.get_member = lambda uid: SimpleNamespace(id=USER) if int(uid) == USER else None

    async def to_level(level, extra=0):
        total = sum(xp_for_level(n) for n in range(1, level + 1)) + extra
        current = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                       (GUILD, USER))[0][0]
        await give_reward(bot, GUILD, USER, "xp", amount=total - current,
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

    # ── 2. the real /level embed: header, path, XP numbers ──
    print("== 2. the level page renders the reference layout ==")
    member = SimpleNamespace(id=USER, premium_since=None)
    await to_level(5)
    xp = rows("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0]
    level, current, needed = xp_progress(xp)
    embed = await _level_embed(guild, member, "level")
    check("the current level is on the left and the next level on the right",
          embed.title == f"LEVEL {level} {ARROW} LEVEL {level + 1}", embed.title)
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
    check("a Booster reward a non-booster cannot claim stays visible as locked",
          any("(Boosters only)" in line and line.rstrip().endswith("not earned")
              for line in lines), str(lines))

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
    check("delivered rewards are shown as fulfilled, never as claimable",
          "fulfilled" in after_lines and "ready to claim" not in after_lines,
          after_lines)
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
    itx2 = SimpleNamespace(
        user=itx.user, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 edit_message=AsyncMock()),
        edit_original_response=AsyncMock())
    await card.show_stats.callback(itx2)
    stats = itx2.response.edit_message.await_args.kwargs
    check("Stats swaps in the stats page and highlights itself",
          stats["embed"].title == "Level stats"
          and card.show_stats.style.name == "primary"
          and card.show_level.style.name == "secondary"
          and stats["view"] is card, str(stats["embed"].title))
    check("the stats page keeps its own claim history fields",
          field_names(stats["embed"])[:2] == ["Claimed", "Waiting"],
          str(field_names(stats["embed"])))

    await card.show_level.callback(itx2)
    back = itx2.response.edit_message.await_args.kwargs
    check("Level returns to the card page, path included, and re-highlights",
          PLANE in (back["embed"].description or "")
          and back["embed"].title.startswith("LEVEL ")
          and card.show_level.style.name == "primary"
          and card.show_stats.style.name == "secondary"
          and back["view"] is card, back["embed"].title)

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
