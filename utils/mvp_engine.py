"""MVP cycles: per-cycle scores, ranked winners, atomic idempotent payout.

Source of truth is the database (``mvp_cycle_state`` / ``mvp_cycle_scores`` /
``mvp_payouts`` / ``mvp_history``); nothing is kept in memory between ticks.
``mvp_scores`` (the daily table used by /mvp_scores and the dashboard) is not
touched here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord

from database import DB_PATH

MAX_RANKS = 10
MAX_REWARD = 1_000_000_000
DEFAULTS = {
    "enabled": 1, "cycle_hours": 6, "mvp_role_id": None,
    "announce_channel_id": None, "chat_word_weight": 1.0,
    "voice_minute_weight": 2.0, "winner_ranks": 1, "rank_rewards": [],
    "min_activity_words": 0.0,
}


def parse_rank_rewards(raw, ranks: int) -> list[int]:
    """Coins per rank, padded/truncated to ``ranks`` entries (never negative)."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        values = []
    out = []
    for i in range(ranks):
        try:
            out.append(max(0, min(MAX_REWARD, int(values[i]))))
        except (IndexError, TypeError, ValueError):
            out.append(0)
    return out


async def get_config(guild_id: int) -> dict:
    cfg = dict(DEFAULTS)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT * FROM mvp_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        if row:
            cfg.update(dict(zip([d[0] for d in cursor.description], row)))
    ranks = max(1, min(MAX_RANKS, int(cfg.get("winner_ranks") or 1)))
    cfg["winner_ranks"] = ranks
    cfg["rank_rewards"] = parse_rank_rewards(cfg.get("rank_rewards"), ranks)
    cfg["cycle_hours"] = max(1, int(cfg.get("cycle_hours") or 6))
    cfg["min_activity_words"] = max(0.0, float(cfg.get("min_activity_words") or 0))
    cfg["chat_word_weight"] = float(cfg.get("chat_word_weight") or 0)
    cfg["voice_minute_weight"] = float(cfg.get("voice_minute_weight") or 0)
    return cfg


def threshold_points(cfg: dict) -> float:
    """Words x this guild's chat_word_weight (0 = disabled)."""
    return cfg["min_activity_words"] * cfg["chat_word_weight"]


def threshold_voice_minutes(cfg: dict) -> float | None:
    """The same threshold expressed in voice minutes (for the dashboard)."""
    if cfg["voice_minute_weight"] <= 0:
        return None
    return threshold_points(cfg) / cfg["voice_minute_weight"]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _open_cycle(db, guild_id: int, cycle_hours: int, now: datetime) -> str:
    row = await (await db.execute(
        "SELECT cycle_start FROM mvp_cycle_state WHERE guild_id=?",
        (guild_id,))).fetchone()
    if row:
        return row[0]
    start = _iso(now)
    await db.execute(
        "INSERT OR IGNORE INTO mvp_cycle_state (guild_id, cycle_start, cycle_hours) "
        "VALUES (?,?,?)", (guild_id, start, cycle_hours))
    row = await (await db.execute(
        "SELECT cycle_start FROM mvp_cycle_state WHERE guild_id=?",
        (guild_id,))).fetchone()
    return row[0]


async def record_activity(guild_id: int, user_id: int, *, message_score: float = 0.0,
                          voice_minutes: float = 0.0, voice_score: float = 0.0,
                          now: datetime | None = None, cycle_hours: int = 6) -> None:
    """Add activity to the guild's OPEN cycle (opened on first activity)."""
    now = now or datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        start = await _open_cycle(db, guild_id, cycle_hours, now)
        total = message_score + voice_score
        await db.execute("""
            INSERT INTO mvp_cycle_scores
                (guild_id, cycle_start, user_id, message_score, voice_minutes,
                 total_score, last_activity_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(guild_id, cycle_start, user_id) DO UPDATE SET
                message_score = message_score + excluded.message_score,
                voice_minutes = voice_minutes + excluded.voice_minutes,
                total_score = total_score + excluded.total_score,
                last_activity_at = excluded.last_activity_at
        """, (guild_id, start, user_id, message_score, voice_minutes, total,
              _iso(now)))
        await db.commit()


def rank_winners(rows, ranks: int, min_points: float, is_member) -> list[dict]:
    """Dense-ranked winners. ``rows`` = (user_id, total_score, last_activity_at).

    Members below the activity threshold or no longer in the guild are removed
    BEFORE ranking, so the next eligible member moves up. Equal scores share a
    rank; every tied member is a winner. ``ranks`` counts ranks, not people.
    """
    eligible = [
        (uid, round(float(score), 6), last)
        for uid, score, last in rows
        if float(score) > 0
        and (min_points <= 0 or float(score) >= min_points)
        and is_member(uid)
    ]
    eligible.sort(key=lambda r: (-r[1], r[2] or "", r[0]))
    winners, rank, previous = [], 0, None
    for uid, score, last in eligible:
        if score != previous:
            rank += 1
            previous = score
        if rank > ranks:
            break
        winners.append({"rank": rank, "user_id": uid, "score": score})
    return winners


async def settle_cycle(bot, guild, *, now: datetime | None = None,
                       force: bool = False) -> dict | None:
    """Close the guild's cycle if due. Returns the settled cycle or None.

    Payout, payout records, history and the move to the next cycle commit in
    ONE transaction (BEGIN IMMEDIATE), so a failure pays nothing, and a second
    scheduler pass finds the cycle already advanced and does nothing.
    No missed cycle is replayed: after downtime the next cycle starts now.
    """
    from utils.reward_engine import give_reward
    from utils.formatters import snapshot_user

    now = now or datetime.now(timezone.utc)
    cfg = await get_config(guild.id)
    if not cfg.get("enabled", 1):
        return None
    hours = cfg["cycle_hours"]

    async with aiosqlite.connect(DB_PATH) as db:
        start_iso = await _open_cycle(db, guild.id, hours, now)
        await db.commit()
    start = _parse(start_iso)
    natural_end = start + timedelta(hours=hours)
    if not force and now < natural_end:
        return None
    end = now if force else natural_end
    end_iso = _iso(end)

    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT user_id, total_score, last_activity_at FROM mvp_cycle_scores "
            "WHERE guild_id=? AND cycle_start=?", (guild.id, start_iso))).fetchall()
    winners = rank_winners(rows, cfg["winner_ranks"], threshold_points(cfg),
                           lambda uid: guild.get_member(uid) is not None)
    rewards = cfg["rank_rewards"]

    # Resolve each winner's Prestige (a Booster check can write) BEFORE the
    # payout transaction opens; give_reward then only reads it.
    if any(rewards[w["rank"] - 1] > 0 for w in winners):
        from utils.prestige import get_prestige_earn_multiplier, is_booster
        for w in winners:
            member = guild.get_member(w["user_id"])
            try:
                await get_prestige_earn_multiplier(
                    guild.id, w["user_id"], "balance",
                    is_booster=is_booster(member), bot=bot)
            except Exception as exc:
                print(f"[MVP] prestige pre-resolve failed guild={guild.id} "
                      f"user={w['user_id']}: {exc}")

    next_start = end if (now - end) < timedelta(hours=hours) else now
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            current = await (await db.execute(
                "SELECT cycle_start FROM mvp_cycle_state WHERE guild_id=?",
                (guild.id,))).fetchone()
            if not current or current[0] != start_iso:
                await db.rollback()            # another pass already settled it
                return None
            for w in winners:
                base = rewards[w["rank"] - 1]
                credited = 0
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO mvp_payouts "
                    "(guild_id, cycle_end, rank, user_id, coins_base) VALUES (?,?,?,?,?)",
                    (guild.id, end_iso, w["rank"], w["user_id"], base))
                if cursor.rowcount == 0:       # unique key: already paid
                    continue
                if base > 0:
                    res = await give_reward(
                        bot, guild.id, w["user_id"], "coins", base,
                        reason=f"MVP rank {w['rank']}", source="mvp", db=db)
                    credited = int(res["amount"])
                    await db.execute(
                        "UPDATE mvp_payouts SET coins_credited=? "
                        "WHERE guild_id=? AND cycle_end=? AND rank=? AND user_id=?",
                        (credited, guild.id, end_iso, w["rank"], w["user_id"]))
                w["coins_credited"] = credited
                member = guild.get_member(w["user_id"])
                name = snapshot_user(member)["display_name"] if member else str(w["user_id"])
                await db.execute(
                    "INSERT INTO mvp_history (guild_id, user_id, user_display_name, "
                    "cycle_start, cycle_end, score, rank) VALUES (?,?,?,?,?,?,?)",
                    (guild.id, w["user_id"], name, start_iso, end_iso,
                     int(w["score"]), w["rank"]))
            await db.execute(
                "UPDATE mvp_cycle_state SET cycle_start=?, cycle_hours=? WHERE guild_id=?",
                (_iso(next_start), hours, guild.id))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    settled = {"guild_id": guild.id, "cycle_start": start_iso, "cycle_end": end_iso,
               "winners": winners, "cycle_hours": hours}
    await _grant_roles_then_announce(bot, guild, cfg, settled)
    return settled


async def _grant_roles_then_announce(bot, guild, cfg, settled) -> None:
    """Roles first, then ONE announcement (a failure of either never undoes the payout)."""
    winners = settled["winners"]
    if not winners:
        print(f"[MVP] guild={guild.id}: cycle ended with no eligible winner")
        return
    role_id = cfg.get("mvp_role_id")
    role = guild.get_role(int(role_id)) if role_id else None
    if role_id and role is None:
        print(f"[MVP] guild={guild.id}: configured MVP role {role_id} not found")
    if role is not None:
        winner_ids = {w["user_id"] for w in winners}
        from utils.equip_engine import slot_lock
        from utils.timed_ownership import role_removal_blocked
        for m in list(guild.members):
            if role in m.roles and m.id not in winner_ids:
                async with slot_lock(guild.id, m.id):
                    if role in m.roles:
                        kept = await role_removal_blocked(
                            guild.id, m.id, int(role_id), retiring="mvp",
                            bot=bot, member=m)
                        if kept:
                            print(f"[MVP] Keeping MVP role {role_id} for {m.id}: {kept}")
                        else:
                            try:
                                await m.remove_roles(role, reason="MVP cycle reset")
                            except Exception as exc:
                                print(f"[MVP] could not remove role {role_id} "
                                      f"from {m.id}: {type(exc).__name__}: {exc}")
        for w in winners:
            member = guild.get_member(w["user_id"])
            if member is None:
                continue
            try:
                await member.add_roles(role, reason="MVP of the cycle")
            except Exception as exc:
                print(f"[MVP] Failed to add MVP role guild={guild.id} "
                      f"user={w['user_id']} role={role_id}: {type(exc).__name__}: {exc}")

    channel_id = cfg.get("announce_channel_id")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        print(f"[MVP] guild={guild.id}: no announcement channel available")
        return
    lines = [f"{w['rank']}- <@{w['user_id']}>" for w in winners]
    embed = discord.Embed(title="🏆 MVP Winners", description="\n".join(lines),
                          color=0xFFD700)
    embed.set_footer(text=f"Next cycle in {settled['cycle_hours']} hours")
    content = f"<@&{int(role_id)}>" if role is not None else None
    mentions = discord.AllowedMentions(
        everyone=False, users=False,
        roles=[discord.Object(id=int(role_id))] if role is not None else False)
    try:
        await channel.send(content=content, embed=embed, allowed_mentions=mentions)
    except Exception as exc:
        print(f"[MVP] announcement failed guild={guild.id}: {type(exc).__name__}: {exc}")
