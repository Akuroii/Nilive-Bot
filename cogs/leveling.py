import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import time
import io
from datetime import datetime, timezone, timedelta
from database import DB_PATH
from utils.xp_calculator import (
    calculate_message_xp, calculate_voice_xp,
    xp_progress, get_leveling_config,
    is_role_blacklisted,
)
from utils.emoji import CHECK_EMOJI


# ─── Phase 5 / Leveling expansion — reset config helpers ────────────────
async def get_reset_config(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT enabled, period, last_reset FROM leveling_reset_config "
            "WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    if not row:
        return {"enabled": 0, "period": "weekly", "last_reset": None}
    return {"enabled": row[0], "period": row[1], "last_reset": row[2]}


def _period_hours(period: str) -> int:
    return 24 * 30 if period == "monthly" else 24 * 7


async def perform_leaderboard_reset(guild_id: int, period: str):
    """
    Snapshots the full current leaderboard into
    leveling_leaderboard_history (so a reset preserves the completed
    cycle instead of destroying it), then zeroes xp/level for every
    member of THIS guild only. Per-guild isolated throughout — every
    query is scoped to guild_id, matching every other reset/cleanup
    task in the project (cogs/mvp.py's cycle task, cogs/shop.py's
    temp_role_cleanup).

    Scheduling note: the reset_config write below only ever UPDATES an
    existing row (never inserts one). A manual /resetleaderboard must not
    arm the scheduled auto-reset for a guild that never configured it —
    see the comment on the statement itself.

    CRITICAL FIX (dark-fixes pass): this used to run as four
    separate, unguarded statements — a SELECT, up to N history
    INSERTs, an UPDATE zeroing every member, and an upsert into
    leveling_reset_config — with no transaction wrapping the group.
    This function permanently destroys XP data (that's the entire
    point of a reset), so a crash or connection drop partway through
    the loop used to be able to leave the guild in a half-archived,
    half-zeroed state with no way to detect or recover from it: some
    members' history rows written, others not, and the zeroing UPDATE
    possibly applied to some but not all rows depending on exactly
    where the failure landed.

    The whole operation — snapshot, zero, and the reset_config
    bookkeeping row — now runs inside a single BEGIN IMMEDIATE
    transaction and either commits completely or rolls back
    completely, the same all-or-nothing guarantee
    utils/economy_safe.py already gives every coin/diamond mutation.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                WHERE guild_id = ? ORDER BY xp DESC
            """, (guild_id,))
            rows = await cursor.fetchall()

            for rank, (user_id, xp, level) in enumerate(rows, 1):
                await db.execute("""
                    INSERT INTO leveling_leaderboard_history
                        (guild_id, user_id, xp, level, rank, period, period_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (guild_id, user_id, xp, level, rank, period, now))

            await db.execute(
                "UPDATE levels SET xp = 0, level = 0 WHERE guild_id = ?",
                (guild_id,))

            # Only an EXISTING reset-config row is updated. This used to be an
            # upsert that wrote enabled=1, so a manual /resetleaderboard silently
            # enrolled a guild that had never configured a scheduled reset into
            # the 7/30-day auto-reset loop (leaderboard_reset_task picks up every
            # row with enabled=1). A guild that did configure one keeps its
            # period/last_reset bookkeeping exactly as before; a guild that did
            # not is left unconfigured (get_reset_config already reports
            # enabled=0 for a missing row).
            await db.execute("""
                UPDATE leveling_reset_config
                SET period = ?, last_reset = ?
                WHERE guild_id = ?
            """, (period, now, guild_id))

            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return len(rows)


def _compact_qty(amount: int) -> str:
    return f"{int(amount)}x"


# ─── /level progress path (presentation only) ───────────────────────────
# The path is drawn from the same numbers the engine already uses
# (xp_progress -> (level, current, needed)), so it can never disagree with the
# level, the XP total or what Claim All will pay. Nothing here reads or writes
# reward state.
#
# Discord renders embed text in a proportional font and has no sub-character
# positioning, so the plane moves in discrete steps: it sits inside the segment
# it is currently flying and takes the start, middle or end of that segment.
# With 5 stations that is 12 distinct positions, strictly monotonic in XP.
_PATH_STATIONS = 5
_PATH_LINK = "━━"          # ━━ the path between two stations
_PATH_REACHED = "◆"        # ◆ a station already reached
_PATH_AHEAD = "◇"          # ◇ a station not reached yet
_PATH_PLANE = "✈︎"     # ✈︎ the member's position on the path


def _progress_path(current: int, needed: int,
                   stations: int = _PATH_STATIONS) -> str:
    """`◆━━◆━━✈︎━━◇━━◇━━◇` positioned by real XP progress.

    `current`/`needed` are the values `xp_progress()` returns for the member's
    active level: XP already earned inside it, and XP the next level requires.
    The plane is never decorative - its index in the returned string grows with
    `current / needed`, and stations flip from ◇ to ◆ as it passes them.

    The position is an integer slot (every segment has a start, a middle and an
    end), so no floating-point comparison decides where the plane sits: equal
    progress always draws an equal path, and one slot of progress always moves
    it one character further along.

    A station flips to ◆ only once the plane has ARRIVED at it. Slot 3*k is the
    start of link k (station k reached, station k+1 not), 3*k+1 its middle and
    3*k+2 its end - still short of station k+1, which sits at (k+1)/segments of
    the path. The old `+1 if step == 2` therefore lit the next station one slot
    early and drew `◆━━◆━━✈︎◆━━◇━━◇` at ~46% progress: a reached station
    immediately after the plane for a station the member has not reached. Only a
    finished level (current >= needed) has every station reached.
    """
    stations = max(2, int(stations))
    segments = stations - 1
    slots = segments * 3
    span = max(0, int(needed or 0))
    done = min(max(int(current or 0), 0), span)
    slot = min(done * slots // span, slots - 1) if span else 0
    segment, step = divmod(slot, 3)
    reached = stations if (span and done >= span) else segment + 1

    parts = []
    for index in range(stations):
        parts.append(_PATH_REACHED if index < reached else _PATH_AHEAD)
        if index == segments:
            break
        if index != segment:
            parts.append(_PATH_LINK)
        elif step == 0:
            parts.append(f"{_PATH_PLANE}{_PATH_LINK}")
        elif step == 1:
            parts.append(f"{_PATH_LINK[0]}{_PATH_PLANE}{_PATH_LINK[1:]}")
        else:
            parts.append(f"{_PATH_LINK}{_PATH_PLANE}")
    return "".join(parts)


# ─── Claim All button state (presentation only) ─────────────────────────
# "Claimable" here is exactly the set the /level card already reports as
# "Ready to claim": a claim row that is pending, or failed and therefore
# retryable by the next Claim All. Nothing about eligibility, reservation or
# delivery changes — the claim engine (utils/level_claims.py) remains the only
# thing that decides what may actually be claimed; this just decides whether
# the button is drawn green+enabled or gray+disabled. Rows that are fulfilled
# (terminal) or mid-lease ('processing') are not offered.
_CLAIMABLE_STATUSES = ("pending", "failed")


def _is_locked_claim(claim, booster_ok: bool) -> bool:
    """A Booster-only reward the member cannot claim right now."""
    from utils.level_claims import is_booster_ref
    return (not booster_ok and is_booster_ref((claim or {}).get("reward_ref"))
            and (claim or {}).get("status") in _CLAIMABLE_STATUSES)


def _claimable_now(claim, booster_ok: bool = False) -> bool:
    """One claim row this member can collect with Claim All right now.

    The single predicate behind the Claim All button, the "Rewards Available"
    list and what the claim pass will actually pay, so those three can never
    disagree: pending or retryable-failed, and not a Booster-only reward the
    member's verified boost status leaves locked.
    """
    return ((claim or {}).get("status") in _CLAIMABLE_STATUSES
            and not _is_locked_claim(claim, booster_ok))


def _has_claimable(claims, booster_ok: bool = False) -> bool:
    return any(_claimable_now(c, booster_ok) for c in (claims or []))


async def _claim_button_state(guild_id: int, claims,
                              booster_ok: bool = False) -> tuple[bool, bool]:
    """`(claimable, reconcile_only)` - what the Claim All button should look like.

    ``claimable``      at least one row this member can collect right now. This
                       is the only state that earns the green/success style:
                       green has always read as "rewards are waiting for you".
    ``reconcile_only`` nothing to collect, but the exclusive-role reconcile
                       action (a fulfilled role claim plus
                       `remove_old_reward_role`, see
                       test_leveling_claim_all_d2.py) still has work to do.

    These two drive two DIFFERENT buttons now. Before, both states lit the same
    `Claim All` button, so a card whose rewards had all been delivered stayed
    clickable whenever `remove_old_reward_role` was on - an enabled button named
    "Claim All" that could not claim anything, which is what invited the repeat
    clicks that each answered "Claimed 0". `claimable` alone earns the green
    Claim All; `reconcile_only` alone earns a separate, explicitly labelled role
    button, and Claim All sits grey and disabled meanwhile.

    Read-only. `_claim_button_available()` is the boolean "is either action
    reachable" form of this, kept for the D2 contract in
    test_leveling_claim_all_d2.py / test_slice2_level_claims.py.
    """
    if _has_claimable(claims, booster_ok):
        return True, False
    if not any((claim or {}).get("track") == "role"
               and (claim or {}).get("status") == "fulfilled"
               for claim in (claims or [])):
        return False, False
    config = await get_leveling_config(guild_id)
    return False, bool(config.get("remove_old_reward_role"))


async def _claim_button_available(guild_id: int, claims,
                                  booster_ok: bool = False) -> bool:
    """Also expose Claim All as the explicit exclusive-role reconcile action."""
    claimable, reconcile_only = await _claim_button_state(
        guild_id, claims, booster_ok)
    return claimable or reconcile_only


def _claim_age(value) -> str:
    """'3d ago' for a claim timestamp; '' when it is missing or unparsable."""
    if not value:
        return ""
    try:
        then = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _claim_reward_text(guild, claim, currency) -> str:
    """One claim rendered for the Stats view — the frozen payload, not config."""
    payload = claim["payload"] or {}
    track, level = claim["track"], claim["reward_level"]
    if track == "currency":
        code = payload.get("currency")
        label = currency["coins" if code == "balance" else "diamonds"]["name"]
        return f"L{level} · {_compact_qty(payload.get('amount', 0))} {label}"
    if track == "shop":
        return f"L{level} · {_compact_qty(payload.get('quantity', 0))} {payload.get('name', 'item')}"
    if track == "boost":
        return (f"L{level} · {float(payload.get('multiplier', 1)):g}x XP · "
                f"{int(payload.get('duration_hours', 0))}h")
    role = guild.get_role(int(payload.get("role_id", 0) or 0)) if guild else None
    return f"L{level} · {role.mention if role else 'role reward'}"


def claim_result_footer(result: dict) -> str:
    """تذييل Claim All - the Claim All footer, including any reconciled role.

    This line is attached to the member-facing Claim All embed, so it is Arabic
    end to end like `claim_confirmation()` above it: one message, one language.
    It carries counts and status only - never an item or role name, which is why
    nothing here needs the original English wording. "Boosters" stays as the
    product's own name for the paid tier.

    A restored role is not a newly fulfilled claim (nothing was paid, the claim
    was already fulfilled), so it would otherwise be invisible in the footer.
    Role reconciliation errors are also reported separately from claim failures.
    """
    got = len(result.get("fulfilled") or [])
    missed = len(result.get("failed") or [])
    text = (f"تم الاستلام: {got}. "
            f"إخفاقات قابلة لإعادة المحاولة: {missed}.")
    locked = int(result.get("locked") or 0)
    if locked:
        text += f" مقفلة (Boosters فقط): {locked}."
        if result.get("booster_unverified"):
            text += " تعذّر التحقق من حالة Boosters."
    reconciled = result.get("reconciled") or {}
    restored = len(reconciled.get("restored") or [])
    reconciliation_failures = len(reconciled.get("failed") or [])
    if restored:
        text += f" تمت إعادة رتبة المستوى: {restored}."
    if reconciliation_failures:
        text += (f" فشل في مطابقة رتبة المستوى: "
                 f"{reconciliation_failures}.")
    return text


def _claim_totals(delivered: list, currency: dict) -> str:
    """One summary line for what was actually paid, aggregated per unit.

    Currency and Shop rows are fungible quantities, so they are summed by
    currency/item name (three coin rewards read as one coin total, not three
    lines of arithmetic for the member). Roles and XP boosts are not quantities,
    so they are counted. Empty groups are omitted.
    """
    names = currency or {}
    coin_name = ((names.get("coins") or {}).get("name") or "Coins")
    gem_name = ((names.get("diamonds") or {}).get("name") or "Diamonds")
    money = {"balance": 0, "diamonds": 0}
    items: dict[str, int] = {}
    roles = boosts = 0
    for claim in delivered:
        track, payload = claim.get("track"), (claim.get("payload") or {})
        if track == "currency":
            code = "balance" if payload.get("currency") == "balance" else "diamonds"
            money[code] += int(payload.get("amount", 0) or 0)
        elif track == "shop":
            name = str(payload.get("name") or "item")
            items[name] = items.get(name, 0) + int(payload.get("quantity", 0) or 0)
        elif track == "boost":
            boosts += 1
        else:
            roles += 1
    parts = []
    if money["balance"]:
        parts.append(f"{_compact_qty(money['balance'])} {coin_name}")
    if money["diamonds"]:
        parts.append(f"{_compact_qty(money['diamonds'])} {gem_name}")
    parts += [f"{_compact_qty(qty)} {name}" for name, qty in sorted(items.items())]
    if roles:
        parts.append(f"{roles} role reward(s)")
    if boosts:
        parts.append(f"{boosts} XP boost(s)")
    return " · ".join(parts) if parts else "nothing"


_DROP_NOTE = "… و{n} سطرًا أخرى (القائمة أطول من حدّ Discord)"


def _bounded_lines(lines: list[str], limit: int = 1024) -> str:
    """Join field lines without ever cutting one in half (Discord caps at 1024).

    The headline and the closing status line are always kept whole; whole reward
    lines are dropped from the end of the list until it fits, and the note says
    how many were dropped. A truncated list must not read as a complete one, and
    the status line ("nothing left to claim" / "N still available") is the part a
    member actually acts on, so it is never the thing that gets cut.
    """
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    if len(lines) < 3:
        # Nothing between the headline and the closing line to drop.
        return text[:limit]
    head, body, tail = lines[0], list(lines[1:-1]), lines[-1]
    for keep in range(len(body), -1, -1):
        dropped = len(body) - keep
        parts = [head, *body[:keep]]
        if dropped:
            parts.append(_DROP_NOTE.format(n=dropped))
        parts.append(tail)
        text = "\n".join(parts)
        if len(text) <= limit:
            return text
    # Even the headline, the note and the status line do not fit together, so
    # the headline absorbs the cut rather than the status line.
    note = _DROP_NOTE.format(n=len(body))
    room = max(0, limit - len(note) - len(tail) - 2)
    return f"{head[:room]}\n{note}\n{tail}"[:limit]


async def claim_confirmation(guild, result: dict, claims,
                             booster_ok: bool = False) -> tuple[str, str] | None:
    """رسالة استلام المكافآت - what Claim All actually delivered, in Arabic.

    `result` carries claim IDs only, so each one is resolved against the claim
    rows read straight after the pass and rendered with the same
    `_claim_reward_text()` the Stats page uses: one vocabulary and one source of
    truth, so a name here can never disagree with the row that was paid.

    Honesty rules this is written against:
    * `result["fulfilled"]` is appended by the claim engine only after the
      delivery AND the `fulfilled` state change both succeeded - never merely
      because a claim row exists. So a row is listed as delivered only if it was
      actually paid.
    * The congratulatory headline appears ONLY when nothing failed. A partial
      pass says what landed, what did not (with the recorded reason) and what is
      still claimable - it never reads as a general success.
    * A pass that delivered nothing says so plainly.

    Returns `(field name, field value)`, or None when the pass neither delivered
    nor failed anything - a no-op repeat click, or a reconcile-only pass, whose
    restored Level role `claim_result_footer()` already reports.
    """
    from utils.currency import get_currency_config
    by_id = {int(c["id"]): c for c in (claims or []) if c.get("id") is not None}
    order = lambda c: (int(c["reward_level"]), str(c["track"]), int(c["id"]))
    delivered = sorted((by_id[int(i)] for i in (result.get("fulfilled") or [])
                        if int(i) in by_id), key=order)
    broken = sorted((by_id[int(i)] for i in (result.get("failed") or [])
                     if int(i) in by_id), key=order)
    if not delivered and not broken:
        return None
    currency = (await get_currency_config(guild.id)) if guild is not None else {}
    remaining = [c for c in (claims or []) if _claimable_now(c, booster_ok)]

    if delivered and not broken:
        head = "🎉 مبروك! تم استلام مكافآتك"
    elif delivered:
        head = "⚠️ تم تسليم بعض المكافآت فقط"
    else:
        head = "❌ تعذّر تسليم المكافآت"

    lines = [head]
    if delivered:
        lines.append(f"✅ **تم تسليم {len(delivered)} مكافأة فعلًا:**")
        lines += [f"- {_claim_reward_text(guild, c, currency)}" for c in delivered]
        # A total only adds information once there is more than one reward.
        if len(delivered) > 1:
            lines.append(f"**الإجمالي:** {_claim_totals(delivered, currency)}")
    if broken:
        verb = ("**تعذّر تسليم**" if not delivered
                else "**ولم يتم تسليم**")
        lines.append(f"❌ {verb} {len(broken)} مكافأة "
                     f"(ما زالت محفوظة لك ويمكن إعادة المحاولة):")
        for claim in broken:
            reason = (claim.get("last_error") or "no error recorded").strip()
            lines.append(f"- {_claim_reward_text(guild, claim, currency)}"
                         f" — السبب: {reason[:160]}")
    if remaining:
        lines.append(f"⏳ **ما زال متاحًا للاستلام:** {len(remaining)} مكافأة — "
                     f"زر Claim All ما زال مفعّلًا.")
    elif delivered and not broken:
        lines.append("✔ لا يتبقّى شيء للاستلام — كل المكافآت تم تسليمها.")
    return "نتيجة الاستلام", _bounded_lines(lines)


def _level_reset_embed(member, report: dict, *, applied: bool) -> discord.Embed:
    """The /level_test_reset preview (or result). Nothing here writes anything.

    The preview has to name every record and side effect the reset touches, every
    delivered level reward it CAN take back and how, and - just as importantly -
    every one it CANNOT, so an admin never confirms a destructive step on a
    guess. Things that are not level rewards (prestige, value from other systems)
    are reported as notes and never counted against the result.
    """
    plan = report.get("plan") or {}
    if applied:
        title = "Level test reset — APPLIED"
        color = 0x3ba55d
    else:
        title = "Level test reset — PREVIEW (nothing changed)"
        color = 0x7c5cbf
    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name="Scope",
        value=(f"guild `{report['guild_id']}` · member {member.mention} "
               f"(`{report['user_id']}`)\nenvironment: `{report['environment']}`\n"
               f"Never any other member, guild, or reward definition."),
        inline=False)
    prestige_now = report.get("prestige", 0)
    vi_now = report.get("vi_activation")
    embed.add_field(
        name="Current record",
        value=(f"Level {report['level']} · {report['xp']:,} XP\n"
               f"Prestige: permanent tier {prestige_now}"
               + (f" · VI activation {vi_now}" if vi_now is not None else "")
               + ("" if plan.get("reset_prestige")
                  else " · **not reset** (pass include_prestige)") + "\n"
               f"claim rows: {len(report['claims'])} "
               f"({', '.join(f'{n} {s}' for s, n in sorted(report['claims_by_status'].items())) or 'none'})"),
        inline=False)
    would = "Reset" if applied else "Would reset"
    lines = [
        "- zero XP/Level in `levels`" if plan.get("zero_levels_row")
        else "- no `levels` row to zero",
        f"- delete {plan.get('delete_claims', 0)} `level_reward_claims` row(s) "
        f"→ those reward levels become earnable again",
        f"- delete {plan.get('delete_boosts', 0)} level-claim `leveling_active_boosts` row(s)",
        f"- delete {plan.get('delete_temp_roles', 0)} level-claim `temp_roles` unit(s)",
    ]
    roles = plan.get("remove_discord_roles") or []
    lines.append(f"- remove {len(roles)} reward role(s) from the member"
                 + (f": {', '.join(f'<@&{r}>' for r in roles)}" if roles else ""))
    if plan.get("reset_prestige"):
        lines.append(
            f"- zero permanent Prestige (tier {plan.get('prestige_now', 0)}) and "
            f"delete the Prestige VI activation. **Shop purchases are not "
            f"refunded.**")
    left_on = plan.get("roles_left_on_member") or []
    if left_on:
        lines.append(
            "- leave on the member (another grant also gives them, so removing "
            "them would revoke it): "
            + ", ".join(f"<@&{r['role_id']}> via {', '.join(r['also_granted_by'])}"
                        for r in left_on))
    lines.append(
        "- a role that cannot be taken off keeps its claim and temp-role rows, "
        "so it can never be paid twice and never becomes undocumented")
    embed.add_field(name=would, value="\n".join(lines)[:1024], inline=False)

    # Every level reward this member actually received, split by whether the
    # reset can prove it and take it back. Amounts come from the append-only
    # ledger and the frozen claim payloads - what was really paid, not what a
    # reward definition says today.
    will = []
    for entry in plan.get("reverse_currency") or []:
        will.append(f"- L{entry['reward_level']} · {entry['amount']:,} "
                    f"{entry['currency']} → reversed via ledger row "
                    f"`{entry['ledger_id']}`"
                    + (" (already reversed earlier)"
                       if entry.get("already_reversed") else ""))
    for entry in plan.get("reverse_inventory") or []:
        will.append(f"- L{entry['reward_level']} · {entry['quantity']}x "
                    f"{entry['item_name']} → removed from the inventory")
    if will:
        embed.add_field(
            name=("Delivered level rewards this reset reverses" if not applied
                  else "Delivered level rewards reversed"),
            value="\n".join(will)[:1024], inline=False)

    cannot = plan.get("cannot_reverse") or []
    if cannot:
        embed.add_field(
            name="⚠️ Delivered value that CANNOT be reversed",
            value="\n".join(
                f"- L{e['reward_level']} {e['kind']}: {e.get('note') or e.get('error')}"
                for e in cannot[:20])[:1024]
            + "\n**These claim rows are KEPT**, so the reward cannot be claimed "
              "a second time while its value is still in the member's hands. "
              "Reversing them needs a manual decision.",
            inline=False)

    # The manual-review half of the report: what looks related to a level reward
    # but cannot be pinned to one, so it is left alone and explained rather than
    # guessed at. Shown on the preview too, because an admin deciding whether to
    # confirm needs to know what will NOT be undone.
    unattributable = (report.get("reconciliation") or {}).get("unattributable") or []
    if unattributable:
        embed.add_field(
            name="Left alone: cannot be attributed to a level reward",
            value="\n".join(
                f"- `{u['table']}` — {u['reason']}" for u in unattributable)[:1024],
            inline=False)
    if applied:
        removed = report.get("roles_removed") or []
        failures = report.get("role_failures") or []
        preserved = report.get("claims_preserved") or []
        after = report.get("after") or {}
        result_lines = [
            f"XP/Level now {after.get('xp', 0):,} / L{after.get('level', 0)}",
            f"Prestige now tier {after.get('prestige', 0)}"
            + (" · VI activation cleared" if plan.get("reset_prestige")
               else " · **left in place**"),
            f"claim rows left: {after.get('claims', 0)} · boosts: "
            f"{after.get('boosts', 0)} · temp-role units: "
            f"{after.get('temp_roles', 0)}",
        ]
        embed.add_field(name="Result", value="\n".join(result_lines), inline=False)
        if report.get("ran"):
            embed.add_field(name="What ran",
                            value="\n".join(f"- {r}" for r in report["ran"])[:1024],
                            inline=False)
        if failures:
            embed.add_field(
                name="Roles that could NOT be removed",
                value="\n".join(
                    f"- <@&{f['role_id']}> — {str(f['error'])[:120]}"
                    for f in failures[:20])[:1024]
                + ("\nTheir claim and temp-role rows were KEPT, so the member is "
                   "not left wearing an undocumented reward role and the removal "
                   "can be retried." if preserved else ""),
                inline=False)
        elif removed:
            embed.add_field(name="Roles removed",
                            value=", ".join(f"<@&{r}>" for r in removed)[:1024],
                            inline=False)
        if report.get("could_not"):
            embed.add_field(
                name="What could NOT be done",
                value="\n".join(f"- {c}" for c in report["could_not"])[:1024],
                inline=False)
        if report.get("unreversed"):
            embed.set_footer(
                text=f"{len(report['unreversed'])} delivered reward(s) could not "
                     f"be reversed and their claim rows were kept — see above.")
        else:
            # The applied embed always ends on the same question the admin has:
            # did anything have to be left behind?
            embed.set_footer(
                text="Every delivered level reward this reset could attribute was "
                     "reversed; nothing was preserved.")

    # The honesty rule the whole tool is written against: if anything from the
    # previous run survived, this is not a New Game and the admin is told so,
    # before they rely on the member as a clean test subject.
    traces = report.get("traces") or []
    if traces:
        embed.add_field(
            name="⚠️ Not a clean New Game — traces of the previous run",
            value="\n".join(f"- {t}" for t in traces)[:1024], inline=False)
    elif applied:
        embed.add_field(
            name="✅ Clean New Game",
            value="No level-reward trace of the previous run survived: XP, level, "
                  "claim records, boosts, temp roles, Discord roles and the "
                  "delivered currency/items this reset could prove are all "
                  "reversed for this member only.", inline=False)
    # Not level rewards, so not traces - but the admin should still see them.
    notes = report.get("notes") or []
    if notes:
        embed.add_field(name="Left alone on purpose (not level rewards)",
                        value="\n".join(f"- {n}" for n in notes)[:1024],
                        inline=False)
    if not applied:
        embed.set_footer(text="Preview only. Re-run with confirm: True to apply.")
    return embed


async def _level_embed(guild, member, page: str, *, claims=None) -> discord.Embed:
    """Render one /level page.

    ``claims`` lets a caller that already read ``level_reward_claims`` (every
    button interaction does) hand the rows over instead of paying for a second
    read. That matters because this runs inside Discord's 3-second interaction
    window: each ``list_claims()`` is two SQLite connections (``ensure_tables()``
    plus the query), and a click used to open six of them before anything was
    acknowledged. Read-only either way - omitting it changes nothing but cost.
    """
    from utils.currency import get_currency_config
    from utils.level_claims import (
        current_level_definitions, list_claims, next_reward_level,
    )
    from utils.xp_calculator import xp_progress

    # timeout=10 like every other leveling connection: the bot's own XP writer
    # holds BEGIN IMMEDIATE locks, and the 5s default made a busy database both
    # slower to answer and able to raise "database is locked" mid-render.
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        cursor = await db.execute(
            "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
            (guild.id, member.id))
        row = await cursor.fetchone()
    xp = row[0] if row else 0
    level, current, needed = xp_progress(xp)
    from utils.level_claims import is_booster_ref, member_is_booster
    booster_ok = member_is_booster(member)
    if claims is None:
        claims = await list_claims(guild.id, member.id)
    by_key = {(c["reward_level"], c["track"], c["reward_ref"]): c for c in claims}
    if page == "stats":
        # Reward/claim history, not a second copy of the progression numbers
        # (Level / Total XP / Progress already fill the Level page). Everything
        # below comes from the existing level_reward_claims rows.
        currency = await get_currency_config(guild.id)
        by_status = {}
        for claim in claims:
            by_status.setdefault(claim["status"], []).append(claim)
        fulfilled = by_status.get("fulfilled", [])
        waiting_all = by_status.get("pending", []) + by_status.get("failed", [])
        locked_claims = [c for c in waiting_all if _is_locked_claim(c, booster_ok)]
        waiting = [c for c in waiting_all if not _is_locked_claim(c, booster_ok)]
        failed = [c for c in by_status.get("failed", [])
                  if not _is_locked_claim(c, booster_ok)]
        # A row still reporting 'processing' holds a LIVE lease (list_claims
        # reports an expired one as 'pending', because the next Claim All really
        # can take it). It is neither delivered nor waiting-to-be-claimed, and
        # counting it in neither made this page announce "every crossed reward
        # is delivered" while a reward was in flight or stranded.
        in_flight = by_status.get("processing", [])
        newest = max((c for c in fulfilled if c.get("fulfilled_at")),
                     key=lambda c: c["fulfilled_at"], default=None)
        oldest = min((c for c in waiting if c.get("created_at")),
                     key=lambda c: c["created_at"], default=None)
        embed = discord.Embed(title="Level stats", color=0x7c5cbf)
        claimed_txt = f"{len(fulfilled)} of {len(claims)} crossed rewards"
        if newest:
            claimed_txt += (f"\nLatest: {_claim_reward_text(guild, newest, currency)} · "
                            f"{_claim_age(newest['fulfilled_at'])}")
        embed.add_field(name="Claimed", value=claimed_txt, inline=False)
        pending_ready = [c for c in by_status.get("pending", [])
                         if not _is_locked_claim(c, booster_ok)]
        waiting_txt = (f"{len(pending_ready)} pending · "
                       f"{len(failed)} retryable failure(s)")
        if in_flight:
            waiting_txt += f" · {len(in_flight)} being delivered now"
        if locked_claims:
            waiting_txt += f" · {len(locked_claims)} locked (Boosters only)"
        if oldest:
            waiting_txt += (f"\nOldest: {_claim_reward_text(guild, oldest, currency)} · "
                            f"{_claim_age(oldest['created_at'])}")
        elif not waiting and not locked_claims:
            # Only say "delivered" when the rows actually say so: an in-flight
            # lease or a stranded row is not a delivery. The counts are rebuilt
            # rather than appended so this sentence cannot drop the in-flight
            # rows it is qualified by.
            if not in_flight and len(fulfilled) == len(claims):
                waiting_txt = "Nothing waiting — every crossed reward is delivered."
            else:
                held = []
                if in_flight:
                    held.append(f"{len(in_flight)} being delivered now")
                undelivered = len(claims) - len(fulfilled) - len(in_flight)
                if undelivered > 0:
                    held.append(f"{undelivered} not delivered")
                waiting_txt = ("Nothing claimable right now — "
                               + (", ".join(held) or "nothing outstanding")
                               + f" ({len(fulfilled)} of {len(claims)} crossed "
                                 f"reward(s) delivered).")
        embed.add_field(name="Waiting", value=waiting_txt, inline=False)
        if failed:
            first = failed[0]
            reason = (first.get("last_error") or "no error recorded").strip()
            embed.add_field(name="Last failure",
                            value=f"{_claim_reward_text(guild, first, currency)} — {reason[:200]}",
                            inline=False)
        next_up = max(waiting, key=lambda c: c["reward_level"], default=None)
        if next_up:
            highest = (f"{_claim_reward_text(guild, next_up, currency)} "
                       f"({next_up['status']})")
        elif not claims:
            highest = "nothing crossed yet"
        elif in_flight or locked_claims:
            # Nothing the member can collect this moment, but not because
            # nothing was crossed - say which of the two it actually is.
            held = []
            if in_flight:
                held.append(f"{len(in_flight)} being delivered now")
            if locked_claims:
                held.append(f"{len(locked_claims)} locked (Boosters only)")
            highest = "nothing claimable now — " + ", ".join(held)
        else:
            # Contradicted "Claimed — N of N crossed rewards" above: rewards had
            # been crossed and delivered, so "nothing crossed yet" was false.
            highest = (f"nothing left to claim — all {len(fulfilled)} crossed "
                       f"reward(s) delivered")
        embed.add_field(name="Highest unclaimed", value=highest, inline=False)
        return embed

    # Reference layout: the current level on the left, the next configured
    # Level Reward milestone on the right, then the progress path with the
    # numeric XP progress beside it. The right-hand word carries the meaning -
    # "REWARD L{n}" names a reward milestone the member is eligible for, while
    # the fallback "LEVEL {level + 1}" names the path's own destination, so the
    # heading never implies the bar measures something it does not. The path
    # and its numbers stay exactly what xp_progress reports for this level:
    # progress from the current level towards the next one, never towards the
    # milestone. Milestone selection is read-only and audience-aware through
    # next_reward_level(); nothing about XP or eligibility changes here.
    milestone = await next_reward_level(guild.id, level, boosters=booster_ok)
    target = f"REWARD L{milestone}" if milestone else f"LEVEL {level + 1}"
    embed = discord.Embed(
        title=f"LEVEL {level} ➤ {target}",
        description=(f"{_progress_path(current, needed)}  "
                     f"{int(current)} / {int(needed)} XP"),
        color=0x7c5cbf)
    # Everything the member can actually claim right now, at any level. Claim
    # All already collects the earlier ones, so they are listed as well instead
    # of only being counted in the footer.
    earlier = [c for c in claims
               if c["reward_level"] != level
               and c["status"] in ("pending", "failed")
               and not _is_locked_claim(c, booster_ok)]
    if earlier:
        embed.set_footer(
            text=f"Claim All also collects {len(earlier)} earlier reward(s).")
    defs = await current_level_definitions(guild.id, level)
    currency = await get_currency_config(guild.id)
    # "Rewards Available" is a list of offers, so it carries only the rows this
    # member can claim right now - _claimable_now(), the same predicate the
    # Claim All button uses. Locked, not-yet-crossed and already-delivered rows
    # are not offers and stay off this page; the Stats page still reports every
    # one of them (Claimed / Waiting / locked (Boosters only) / Last failure).
    available = []
    for item in defs:
        key = (item["reward_level"], item["track"], item["reward_ref"])
        claim = by_key.get(key)
        if not _claimable_now(claim, booster_ok):
            continue
        booster_item = is_booster_ref(item["reward_ref"])
        status = claim["status"]
        if item["track"] == "currency":
            code = item["payload"]["currency"]
            label = currency["coins" if code == "balance" else "diamonds"]["name"]
            amount = claim["payload"]["amount"] if claim else item["payload"]["amount"]
            text = f"{_compact_qty(amount)} {label}"
        elif item["track"] == "shop":
            payload = claim["payload"] if claim else item["payload"]
            text = f"{_compact_qty(payload['quantity'])} {payload['name']}"
        elif item["track"] == "boost":
            payload = claim["payload"] if claim else item["payload"]
            text = f"{float(payload['multiplier']):g}x XP · {int(payload['duration_hours'])}h"
        else:
            role = guild.get_role(int(item["payload"]["role_id"]))
            text = role.mention if role else f"role {item['payload']['role_id']}"
        if claim and claim.get("last_error") and status == "failed":
            text += f" — failed, can retry"
        if booster_item:
            text = f"⭐ {text} (Boosters only)"
        available.append(f"{text} · {status}")
    # A deleted Shop product or boost config leaves the frozen claim.
    shown = {(item["reward_level"], item["track"], item["reward_ref"]) for item in defs}
    for claim in claims:
        key = (claim["reward_level"], claim["track"], claim["reward_ref"])
        if claim["reward_level"] != level or claim["track"] not in ("shop", "boost") or key in shown:
            continue
        if not _claimable_now(claim, booster_ok):
            continue
        payload = claim["payload"]
        if claim["track"] == "boost":
            text = f"{float(payload['multiplier']):g}x XP · {int(payload['duration_hours'])}h"
        else:
            text = f"{_compact_qty(payload['quantity'])} {payload['name']}"
        if claim.get("last_error") and claim["status"] == "failed":
            text += " — failed, can retry"
        available.append(f"{text} · {claim['status']}")
    # Earlier levels Claim All would collect are offers too, so they are listed
    # rather than only counted in the footer.
    available += [f"{_claim_reward_text(guild, claim, currency)} · ready to claim"
                  for claim in sorted(earlier, key=lambda c: c["reward_level"])]
    bullets = [f"- {line}" for line in available]
    embed.add_field(
        name="Rewards Available",
        value="\n".join(bullets)[:1024] if bullets
        else "Nothing to claim right now.",
        inline=False)
    return embed


class LevelRewardView(discord.ui.View):
    def __init__(self, bot, guild_id: int, user_id: int, *,
                 claimable: bool = False, reconcile_only: bool = False):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        # Re-entrancy guard for Claim All. The claim engine's lease already
        # makes a concurrent second pass deliver nothing, so this is purely
        # about not queueing up repeat clicks that each answer "Claimed 0".
        self._claim_running = False
        # The role-sync button only appears when it has something to do, so it
        # is taken off the card here and put back by set_claimable().
        self.remove_item(self.sync_roles)
        self.set_page_state("level")
        self.set_claimable(claimable, reconcile_only)

    def set_page_state(self, page: str) -> None:
        """Highlight the page currently shown by the Level/Stats buttons."""
        self.show_level.style = (discord.ButtonStyle.primary if page == "level"
                                 else discord.ButtonStyle.secondary)
        self.show_stats.style = (discord.ButtonStyle.primary if page == "stats"
                                 else discord.ButtonStyle.secondary)

    def set_claimable(self, claimable: bool, reconcile_only: bool = False) -> None:
        """Button appearance, driven by `_claim_button_state()`.

        - ``claimable``      -> Claim All success/green + enabled: a reward is
          waiting. This is the ONLY state in which Claim All is clickable.
        - not ``claimable``  -> Claim All secondary/grey + **disabled**, whether
          or not a reconcile is available. A button named "Claim All" must not
          be pressable when there is nothing to claim.
        - ``reconcile_only`` -> a separate grey **Restore Level Role** button is
          added and enabled. The exclusive-role reconcile is real work, but it
          is not a claim, so it no longer borrows Claim All's name or its slot.

        Self-contained UI state: it changes colours, `disabled` flags and which
        children the view serializes (all re-sent on every edit), never the claim
        engine, the schema, or what either action attempts.
        """
        self.claim_all.style = (discord.ButtonStyle.success if claimable
                                else discord.ButtonStyle.secondary)
        self.claim_all.disabled = not claimable
        self.sync_roles.disabled = not reconcile_only
        present = self.sync_roles in self.children
        if reconcile_only and not present:
            # Appended, so it lands after Claim All in row 1.
            self.add_item(self.sync_roles)
        elif not reconcile_only and present:
            self.remove_item(self.sync_roles)

    async def _apply_claim_state(self, claims, booster_ok: bool) -> None:
        """Refresh the Claim All button from the rows just read."""
        claimable, reconcile_only = await _claim_button_state(
            self.guild_id, claims, booster_ok)
        self.set_claimable(claimable, reconcile_only)

    async def _refresh(self, interaction, page: str):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This level card belongs to someone else.", ephemeral=True)
            return
        # Acknowledge FIRST. Drawing a page means several sequential SQLite
        # connections (the member's XP row, the claim rows plus their
        # ensure_tables() DDL, the currency names, the reward definitions, the
        # guild config) and none of that was acked until it had all finished.
        # On a busy or slow database that ran past Discord's 3-second window,
        # the client showed "Nero didn't respond in time", and the
        # response.edit_message() below could then never land at all - which is
        # exactly why the button only failed *sometimes*. Claim All already
        # deferred for the same reason; Level/Stats did not.
        await interaction.response.defer()
        from utils.level_claims import list_claims, member_is_booster
        try:
            claims = await list_claims(self.guild_id, self.user_id)
            await self._apply_claim_state(claims, member_is_booster(interaction.user))
            self.set_page_state(page)
            embed = await _level_embed(interaction.guild, interaction.user, page,
                                       claims=claims)
        except Exception:
            # A deferred interaction that is never edited leaves the member
            # staring at a spinner forever, so say what happened. Re-raised so
            # discord.py still logs the traceback - reported, not swallowed.
            print(f"[LEVEL CARD] {page} page failed (guild={self.guild_id} "
                  f"user={self.user_id})")
            try:
                await interaction.edit_original_response(
                    content="Couldn't load this page. Please try again.")
            except Exception:
                pass
            raise
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Level", style=discord.ButtonStyle.primary, row=0)
    async def show_level(self, interaction, button):
        await self._refresh(interaction, "level")

    @discord.ui.button(label="Stats", style=discord.ButtonStyle.secondary, row=0)
    async def show_stats(self, interaction, button):
        await self._refresh(interaction, "stats")

    @discord.ui.button(label="Claim All", style=discord.ButtonStyle.primary, row=1)
    async def claim_all(self, interaction, button):
        await self._run_claim_pass(interaction)

    @discord.ui.button(label="Restore Level Role",
                       style=discord.ButtonStyle.secondary, row=1)
    async def sync_roles(self, interaction, button):
        """The exclusive-level-role reconcile, on its own labelled button.

        Present and enabled only when `_claim_button_state()` reports
        `reconcile_only` (see set_claimable). It runs the same claim-engine pass
        as Claim All - the engine is what restores a removed Level role from an
        already-fulfilled role claim, and no second delivery path is invented
        here - but it is named for what it actually does. Before this, that work
        was reachable only through a button called "Claim All" that stayed
        clickable after every reward had been delivered, with nothing to claim.
        """
        await self._run_claim_pass(interaction)

    async def _run_claim_pass(self, interaction):
        """Shared body: one lease-protected pass, one deferred interaction."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This level card belongs to someone else.", ephemeral=True)
            return
        if self._claim_running:
            # A second click while a pass is in flight. Delivery is already
            # lease-protected (the loser reserves nothing), so this only keeps
            # the member from stacking up "Claimed 0" replies.
            await interaction.response.send_message(
                "Your rewards are already being delivered — one moment.",
                ephemeral=True)
            return
        # Set before the first await, so two clicks landing in the same
        # event-loop step cannot both start a pass.
        self._claim_running = True
        try:
            # Role delivery is outside SQLite and can exceed the 3s ack window.
            await interaction.response.defer()
            from utils.level_claims import (
                claim_available, list_claims, member_is_booster,
            )
            result = await claim_available(
                self.guild_id, self.user_id, member=interaction.user,
                bot=self.bot)
            # Re-read once and reuse: the button state, the re-rendered card and
            # the confirmation must all describe the same post-delivery rows.
            claims = await list_claims(self.guild_id, self.user_id)
            booster_ok = member_is_booster(interaction.user)
            await self._apply_claim_state(claims, booster_ok)
            self.set_page_state("level")
            embed = await _level_embed(interaction.guild, interaction.user,
                                       "level", claims=claims)
            embed.set_footer(text=claim_result_footer(result))
            confirmation = await claim_confirmation(
                interaction.guild, result, claims, booster_ok)
            if confirmation:
                embed.add_field(name=confirmation[0], value=confirmation[1],
                                inline=False)
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            print(f"[LEVEL CARD] Claim All failed (guild={self.guild_id} "
                  f"user={self.user_id})")
            try:
                await interaction.edit_original_response(
                    content="Claiming failed before it could finish. "
                            "Nothing was lost — try Claim All again.")
            except Exception:
                pass
            raise
        finally:
            self._claim_running = False


# The approved anti-spam warning, sent as a REPLY to the first message of an
# incident. The XP deduction and warning are both incident-based; the warning
# also has an independent per-member window limit.
# The custom emoji is kept in Discord's `<:name:id>` form exactly as approved so
# it renders wherever the bot has that emoji.
SPAM_WARNING_TEXT = (
    "> مع كل احتراماتي لا تسبام <:brick:1556981905218478162>\n"
    "> -# من لفلك نيهاهاها   XP تم خصم (≖⩊≖)"
)


SPAM_BURST_SECONDS = 3
SPAM_BURST_MESSAGES = 6
SPAM_REPEAT_MESSAGES = 5
SPAM_REPEAT_SECONDS = 10


# ─── Admin XP corrections (/setxp, /setlevel) ───────────────────────────
# Both commands are the same operation - put ONE member's XP at a chosen value -
# so they share one writer. Keeping it in one place is what makes the
# entitlement side of a correction impossible to get inconsistent between the
# two: same transaction, same record_crossing call, same advisory.
#
# MAX_SETTABLE_LEVEL is an INPUT GUARD on one parameter, not a level cap in the
# game - nothing else in the bot has a maximum level, and this constant is not
# consulted by the engine, the curve, a reward or an entitlement. It exists for
# two technical reasons, both specific to /setlevel:
#
#   1. level_start_xp() sums the curve one level at a time, so the level number
#      is a loop bound driven by user input. A typo like 10**12 would spin an
#      O(level) loop on the interaction thread before doing anything.
#   2. That sum is then written to levels.xp, which SQLite stores as a signed
#      64-bit integer. level_start_xp(1000) is ~1.27e9 - comfortably inside it -
#      while an unbounded input can overflow the column and corrupt the row.
#
# The bound is declared to Discord with app_commands.Range so the client refuses
# the value before it is ever sent, and re-checked in the callback because the
# callback is also called directly (tests, dashboard, other cogs). Both read this
# one constant, so they cannot drift. Anything genuinely beyond it is what /setxp
# is for: it takes an XP total and has no level bound at all.
MAX_SETTABLE_LEVEL = 1000


def level_start_xp(level: int) -> int:
    """The XP total at which `level` begins, from the bot's own curve.

    `xp_progress()` consumes `xp_for_level(n)` to leave level n-1, so level L
    starts at the sum for n=1..L, and `xp_progress(level_start_xp(L))` is
    exactly `(L, 0, xp_for_level(L + 1))`: the first XP of that level, not the
    threshold of L-1 and not somewhere in the middle of L. One XP less is still
    level L-1. This is the same expression the leveling suites already use to
    place a member at a level, so /setlevel cannot drift from them.
    """
    from utils.xp_calculator import xp_for_level
    level = max(0, int(level))
    return sum(xp_for_level(n) for n in range(1, level + 1))


async def _entitlement_review(guild_id: int, user_id: int, new_level: int) -> dict:
    """What a DOWNWARD XP correction leaves behind in the claim ledger.

    Read-only. Nothing here deletes, revokes or re-opens an entitlement: a
    reward the member legitimately crossed stays owed, and a reward already
    delivered stays delivered. The point is to tell the admin the truth instead
    of leaving them to discover it later on the /level card - which rows now sit
    above the member's level, which of those were already paid (their effects
    stand, and an XP edit does not undo them), and which are still claimable.
    """
    from utils.level_claims import list_claims
    claims = await list_claims(guild_id, user_id)
    above = [c for c in claims if int(c["reward_level"]) > int(new_level)]
    return {
        "above": len(above),
        "delivered": len([c for c in above if c["status"] == "fulfilled"]),
        # Generous booster_ok=True on purpose: this is an advisory count of rows
        # that COULD be collected, not the button gate. Understating it would
        # hide a row the admin needs to look at.
        "claimable": len([c for c in above if _claimable_now(c, True)]),
    }


def _reduction_advice(member, old_level: int, new_level: int, review: dict) -> str:
    """The honest consequence of lowering a member's level ('' when it rose)."""
    if new_level >= old_level or not review["above"]:
        return ""
    lines = [
        "",
        f"⚠️ Level went **down** (L{old_level} → L{new_level}). Reward history "
        f"was **not** deleted — that is deliberate:",
        f"• {review['delivered']} reward(s) above L{new_level} were already "
        f"**delivered**. An XP edit does not undo coins, items, boosts or roles "
        f"that were paid.",
        f"• {review['claimable']} reward(s) above L{new_level} are still "
        f"**claimable**. If the member really crossed those levels they are "
        f"legitimately owed, and `/level` will keep offering them.",
    ]
    if review["claimable"]:
        lines.append(
            "• If they only exist because XP was raised by mistake, review them "
            f"with `/level_test_reset member:{member.id}` (preview first, one "
            "member). It will not drop a delivered reward's claim row unless you "
            "explicitly pass `include_delivered`.")
    return "\n".join(lines)


async def _apply_admin_xp(guild_id: int, member, xp: int, bot, *,
                          source: str) -> dict:
    """Set ONE member's XP inside one transaction, recording any crossings.

    Shared by /setxp and /setlevel so an admin correction behaves identically
    whichever knob was used: XP clamped non-negative, the stored level recomputed
    from `xp_progress()` (never taken from the request), entitlements following
    the real crossing rule, and the whole read-modify-write in one BEGIN
    IMMEDIATE transaction so a concurrent message-XP award cannot be clobbered.
    Returns the before/after state for the reply.
    """
    from utils.level_claims import (
        member_is_booster, record_crossing, snapshot_multipliers,
    )
    xp = max(0, int(xp))
    new_level, new_current, new_needed = xp_progress(xp)
    multipliers = await snapshot_multipliers(
        guild_id, member.id, member=member, bot=bot)
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT xp FROM levels WHERE guild_id=? AND user_id=?",
                (guild_id, member.id))
            row = await cursor.fetchone()
            old_xp = row[0] if row else 0
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = ?, level = ?
            """, (guild_id, member.id, xp, new_level, xp, new_level))
            await record_crossing(
                db, guild_id, member.id, old_xp, xp, multipliers, source=source,
                is_booster=member_is_booster(member))
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    # The stored level column is derived from the same curve, so report the
    # engine's answer rather than the column we just wrote.
    old_level = xp_progress(old_xp)[0]
    return {
        "old_xp": old_xp, "old_level": old_level,
        "new_xp": xp, "new_level": new_level,
        "current": new_current, "needed": new_needed,
        "review": await _entitlement_review(guild_id, member.id, new_level),
    }


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._xp_cooldowns: dict[tuple, float] = {}
        self._spam_tracker: dict[tuple, list[tuple[float, str]]] = {}
        # Incident identity, expiry, penalty claim, and warning throttle are
        # persisted with XP in SQLite; only this short detector history stays
        # process-local.
        # Phase 3 / E1: voice XP is now driven by
        # cogs/activity_engine.py's activity_voice_xp_tick event (see
        # on_activity_voice_xp_tick below) instead of this cog running
        # its own poll loop, so there's no task to start/cancel here
        # anymore.
        self.leaderboard_reset_task.start()

    def cog_unload(self):
        self.leaderboard_reset_task.cancel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Restore already-earned Level reward roles when a member rejoins.

        This is a membership reconciliation only: it uses the existing claim
        ledger/reconciliation function, never reserves or pays a reward, and
        never creates a second reward engine. With ``remove_old_reward_role``
        off, it restores every fulfilled role because Level roles accumulate;
        with it on, it restores the highest fulfilled set and replaces lower
        roles only after that set is present.
        """
        if getattr(member, "bot", False):
            return
        try:
            from utils.level_claims import reconcile_role_progression
            result = await reconcile_role_progression(
                member, member.guild.id, member.id)
            if result.get("failed"):
                print(f"[LEVEL ROLE REJOIN] guild={member.guild.id} "
                      f"user={member.id}: {result['failed']}")
        except Exception as exc:
            # Discord join processing must not fail because a role was deleted,
            # role hierarchy changed, or the claim database is temporarily down.
            print(f"[LEVEL ROLE REJOIN] reconciliation failed "
                  f"guild={member.guild.id} user={member.id}: {exc}")

    # ─── INCIDENT-BASED SPAM DETECTION ────────────────────
    # Track independently of the XP cooldown. Normal message counts are
    # evaluated over the separately configured 20s window; clear bursts and
    # repeated identical content are additional signals, not extra penalties.
    def _is_spamming(self, guild_id: int, user_id: int,
                      threshold: int, window_seconds: int,
                      content: str = "", *, now: float | None = None) -> bool:
        key = (guild_id, user_id)
        now = time.time() if now is None else now
        window = max(1, int(window_seconds))
        threshold = max(1, int(threshold))
        signature = " ".join(str(content or "").casefold().split())
        records = [
            (timestamp, prior_signature)
            for timestamp, prior_signature in self._spam_tracker.get(key, [])
            if 0 <= now - timestamp < window
        ]
        records.append((now, signature))
        self._spam_tracker[key] = records

        rapid_count = sum(now - timestamp <= SPAM_BURST_SECONDS
                          for timestamp, _ in records)
        repeated_count = sum(
            bool(signature) and prior_signature == signature
            and now - timestamp <= min(SPAM_REPEAT_SECONDS, window)
            for timestamp, prior_signature in records
        )
        return (len(records) >= threshold
                or rapid_count >= SPAM_BURST_MESSAGES
                or repeated_count >= SPAM_REPEAT_MESSAGES)

    # ─── SPAM WARNING ────────────────────────────────────
    # The incident transaction persists the warning claim and member-wide
    # cooldown before Discord is called, so an independent process cannot send
    # another warning for the same incident.
    async def _warn_spam(self, message, deducted: int) -> None:
        """Reply to the offending message with the approved anti-spam embed.

        The deduction and warning claim are committed before sending. A missing
        permission, deleted message, or Discord outage must never roll XP back;
        the durable claim also prevents every later message from retrying the
        same warning after a restart.
        """
        embed = discord.Embed(description=SPAM_WARNING_TEXT)
        embed.set_footer(text=f"-{deducted} XP")
        try:
            await message.reply(embed=embed)
        except Exception as e:
            print(f"[SPAM] Warning not sent (guild={message.guild.id} "
                  f"user={message.author.id}): {e}")

    # ─── MESSAGE XP (Phase 3 / E1: now driven by the Activity
    # Engine's activity_message event instead of its own on_message
    # listener — word_count used to be computed here independently
    # of cogs/activity_engine.py's identical computation; now it's
    # computed once, centrally, and passed in. Everything else below
    # — the cooldown gate, spam-penalty ordering, multiplier lookup,
    # XP award, level-up announce — is unchanged from before.) ─────
    @commands.Cog.listener()
    async def on_activity_message(self, message: discord.Message,
                                   word_count: int):
        if message.author.bot or not message.guild:
            return

        config = await get_leveling_config(message.guild.id)
        guild_id = message.guild.id
        user_id  = message.author.id
        key      = (guild_id, user_id)
        now      = time.time()

        # Spam detection is independent of both Message XP's switch and its
        # cooldown. The short detector history is used only before an incident
        # opens. Active incident identity and its quiet deadline are persisted;
        # each message refreshes that deadline without consulting local samples.
        if config.get("spam_detection_enabled", 1):
            from utils.xp_calculator import (
                apply_spam_penalty, calculate_max_message_xp,
                get_spam_incident_state,
            )
            threshold = int(config.get("spam_threshold", 10))
            window = max(1, int(config.get("spam_window_seconds", 20)))
            persisted = await get_spam_incident_state(
                guild_id, user_id, now=now)
            if persisted["active"]:
                # No local detector sample is needed once an incident is open.
                # Persist every in-window message as activity so the 20s quiet
                # deadline is the same after a restart or in another process.
                outcome = await apply_spam_penalty(
                    guild_id, user_id,
                    incident_detected=False,
                    incident_window_seconds=window,
                    warning_window_seconds=window,
                    now=now)
                if outcome["in_incident"]:
                    return
                # A concurrent setting change may have closed it after the
                # read. Refresh before collecting new pre-incident samples.
                persisted = await get_spam_incident_state(
                    guild_id, user_id, now=now)
            if persisted["expired"]:
                # Clear pre-expiry detector samples once, not on every message
                # while the last persisted row remains expired. Samples newer
                # than the stored deadline belong to a possible new episode.
                deadline = persisted["active_until"]
                prior_samples = self._spam_tracker.get(key, [])
                if deadline is not None and any(
                        sample_time <= deadline
                        for sample_time, _ in prior_samples):
                    self._spam_tracker.pop(key, None)
            detected = self._is_spamming(
                guild_id, user_id, threshold, window,
                getattr(message, "content", ""), now=now)
            if detected:
                penalty_cap = None
                if not persisted["active"]:
                    penalty_cap = await calculate_max_message_xp(
                        guild_id,
                        [role.id for role in getattr(message.author, "roles", [])],
                        user_id)
                outcome = await apply_spam_penalty(
                    guild_id, user_id,
                    config.get("spam_xp_penalty_divisor", 1000),
                    penalty_cap=penalty_cap,
                    incident_detected=True,
                    incident_window_seconds=window,
                    warning_window_seconds=window,
                    now=now)
                if outcome.get("needs_penalty_cap"):
                    penalty_cap = await calculate_max_message_xp(
                        guild_id,
                        [role.id for role in getattr(message.author, "roles", [])],
                        user_id)
                    outcome = await apply_spam_penalty(
                        guild_id, user_id,
                        config.get("spam_xp_penalty_divisor", 1000),
                        penalty_cap=penalty_cap,
                        incident_detected=True,
                        incident_window_seconds=window,
                        warning_window_seconds=window,
                        now=now)
                incident_started = outcome["incident_started"]
                if outcome["in_incident"]:
                    if (incident_started and outcome["applied"]
                            and outcome["warning_due"]):
                        await self._warn_spam(message, outcome["deducted"])
                    return
        else:
            self._spam_tracker.pop(key, None)
            from utils.xp_calculator import (
                apply_spam_penalty, get_spam_incident_state,
            )
            persisted = await get_spam_incident_state(
                guild_id, user_id, now=now)
            if persisted["active"]:
                # End any persisted suppression when spam detection is disabled;
                # this only updates incident expiry and never changes XP.
                await apply_spam_penalty(
                    guild_id, user_id,
                    incident_detected=False,
                    incident_window_seconds=max(
                        1, int(config.get("spam_window_seconds", 20))),
                    detection_enabled=False,
                    now=now)

        # The 10-second Message XP cooldown is independent of the 20-second
        # spam window and does not gate voice, claims, or other XP sources.
        if not config.get("message_xp_enabled", 1):
            return
        cooldown = max(0, int(config.get("xp_cooldown_seconds", 10)))
        last = self._xp_cooldowns.get(key, 0)
        if now - last < cooldown:
            return
        self._xp_cooldowns[key] = now

        role_ids  = [r.id for r in message.author.roles]
        xp_to_add = await calculate_message_xp(
            guild_id, role_ids, word_count, user_id=user_id)

        if xp_to_add <= 0:
            return

        # Phase 3 / E2: XP granting + level-up detection + level
        # reward roles now go through the shared Reward Engine
        # instead of this cog doing its own raw INSERT/UPDATE and its
        # own inline level comparison — the exact same logic used to
        # be duplicated again below for voice XP. Announcing the
        # level-up in-channel stays here since that's leveling's own
        # UI concern, not something the engine should know about.
        from utils.reward_engine import give_reward
        result = await give_reward(
            self.bot, guild_id, user_id, "xp", amount=xp_to_add,
            reason="Message XP", source="leveling")

        if result.get("leveled_up"):
            await self._announce_levelup(
                message, result["new_level"], config)

    async def _announce_levelup(self, message: discord.Message,
                                 new_level: int, config: dict):
        if not config.get("levelup_announce", 1):
            return
        channel_id = config.get("levelup_channel_id")
        channel    = (message.guild.get_channel(int(channel_id))
                      if channel_id else message.channel)
        if not channel:
            return
        custom_msg = config.get("levelup_message")
        if custom_msg:
            text = (custom_msg
                    .replace("{user}", message.author.mention)
                    .replace("{level}", str(new_level))
                    .replace("{name}", message.author.display_name))
            await channel.send(text)
        else:
            embed = discord.Embed(
                description=(f"🎉 {message.author.mention} reached "
                             f"**Level {new_level}**."),
                color=0x7c5cbf)
            await channel.send(embed=embed)

    # ─── VOICE XP (Phase 3 / E1: now driven by the Activity
    # Engine's activity_voice_xp_tick event instead of running its own
    # 60s poll loop over every guild/channel/member. The engine
    # reports the raw facts (alone, AFK channel, deafened) and this
    # listener applies the guild's `voice_farming_guard` to them;
    # what's left here is leveling's own POLICY on top of a valid
    # tick — voice_xp_enabled, the require_unmuted choice, the XP
    # blacklist, and the actual XP math — exactly as before, just
    # invoked once per tick instead of leveling running its own
    # duplicate poll. ──────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_activity_voice_xp_tick(self, guild: discord.Guild,
                                         member: discord.Member,
                                         flags: dict):
        try:
            config = await get_leveling_config(guild.id)
            if not config.get("voice_xp_enabled", 1):
                return

            # Voice-farming guard (one setting, three facts from the Activity
            # Engine): alone, deafened (self or server) or in the AFK channel.
            # ON (the default) = none of them earns Voice XP. OFF = they no
            # longer block by themselves. Independent of the mute rule below.
            if config.get("voice_farming_guard", 1) and (
                    flags.get("alone") or flags.get("afk")
                    or flags.get("self_deaf") or flags.get("deaf")):
                return

            require_unmuted = config.get("voice_require_unmuted", 1)
            if require_unmuted and (flags.get("self_mute") or flags.get("mute")):
                return

            # BUGFIX (dark-fixes pass #7): XP blacklist roles were
            # never checked here — only calculate_message_xp() (via
            # get_xp_multiplier) consulted leveling_blacklist_roles.
            # A member given a blacklist role to opt them out of the
            # leveling system entirely still silently earned XP from
            # every voice tick. Message XP and voice XP now share the
            # same blacklist gate; voice XP still does NOT apply
            # bonus-role multipliers, matching its existing (separate)
            # design.
            role_ids = [r.id for r in member.roles]
            if await is_role_blacklisted(guild.id, role_ids):
                return

            xp_per_min = config.get("voice_xp_per_minute", 3)
            xp_gain = calculate_voice_xp(1, xp_per_min)
            if xp_gain <= 0:
                return

            # Phase 3 / E2: same reward-engine handoff as message XP
            # above — no announcement on voice level-ups, matching the
            # pre-existing behavior (only chat XP announces).
            from utils.reward_engine import give_reward
            await give_reward(
                self.bot, guild.id, member.id, "xp", amount=xp_gain,
                reason="Voice XP", source="leveling")
        except Exception as e:
            print(f"[VOICE XP] Error for member {member.id} in "
                  f"guild {guild.id}: {e}")

    # ─── RANK COMMAND (Pillow Image Card) ───────────────
    @app_commands.command(name="rank",
                          description="View your rank card")
    async def rank(self, interaction: discord.Interaction,
                   member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.defer()

        # Cheap existence guard: distinguish "never earned XP" from "0 XP"
        # without pulling the full rank-card payload first.
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM levels WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, member.id))
            if not await cursor.fetchone():
                await interaction.followup.send(
                    f"{member.mention} has no XP yet.")
                return

        # The real rank-card pipeline (same code path the fidelity
        # previews use): aggregated payload -> 1280x853 Pillow render.
        from utils.rank_card_data import get_rank_card_data
        from utils.rank_card_renderer import render_rank_card

        try:
            data = await get_rank_card_data(
                interaction.guild.id, member.id, member=member)
            buf = await render_rank_card(data)
            file = discord.File(buf, filename="rank.png")
            await interaction.followup.send(file=file)
        except Exception as e:
            print(f"[RANK CARD] render failed for {member.id}: {e}")
            # Fallback: a plain embed so the command still answers if the
            # renderer (fonts/assets/network) fails for any reason.
            from utils.prestige import tier_label
            data = await get_rank_card_data(
                interaction.guild.id, member.id, member=member)
            embed = discord.Embed(
                title=f"Rank — {member.display_name}", color=0x7c5cbf)
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Rank", value=f"#{data['rank']}")
            embed.add_field(name="Level", value=str(data["level"]))
            embed.add_field(name="Total XP", value=f"{data['xp_total']:,}")
            if data["effective_prestige"] > 0:
                embed.add_field(name="Prestige",
                                value=f"★{tier_label(data['effective_prestige'])}")
            embed.add_field(
                name=f"Progress ({data['xp_current']:,}/{data['xp_needed']:,} XP)",
                value="", inline=False)
            await interaction.followup.send(embed=embed)

    # ─── LEADERBOARD RESET TASK (Phase 5 / Leveling expansion) ─────
    # Mirrors cogs/mvp.py's mvp_cycle_task pattern: poll every 30
    # minutes, compare elapsed time against a stored last_reset per
    # guild, only act once the configured period has actually passed.
    # Each guild is isolated in its own try/except so one bad row
    # can't stop the loop from checking the rest — same defensive
    # pattern used throughout (shop temp_role_cleanup,
    # reactionroles expiry_check, moderation scheduled_unban_check).
    @tasks.loop(minutes=30)
    async def leaderboard_reset_task(self):
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT guild_id, period, last_reset
                FROM leveling_reset_config WHERE enabled = 1
            """)
            configs = await cursor.fetchall()

        for guild_id, period, last_reset in configs:
            try:
                if not last_reset:
                    # An enabled schedule with no recorded last_reset has no cycle start
                    # to measure from. It used to fall through to
                    # perform_leaderboard_reset() and wipe the guild's XP on the next
                    # tick. Start the cycle now instead; the first reset is then one
                    # full period away.
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            UPDATE leveling_reset_config SET last_reset = ?
                            WHERE guild_id = ?
                              AND (last_reset IS NULL OR last_reset = '')
                        """, (now.isoformat(), guild_id))
                        await db.commit()
                    print(f"[LEVELING RESET] guild={guild_id} period={period} "
                          f"had no last_reset; cycle start recorded, no reset")
                    continue

                last_dt = datetime.fromisoformat(last_reset)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - last_dt).total_seconds() / 3600
                if elapsed_hours < _period_hours(period):
                    continue

                count = await perform_leaderboard_reset(guild_id, period)
                print(f"[LEVELING RESET] guild={guild_id} period={period} "
                      f"reset {count} members")

                guild = self.bot.get_guild(guild_id)
                config = await get_leveling_config(guild_id)
                channel_id = config.get("levelup_channel_id")
                if guild and channel_id:
                    channel = guild.get_channel(int(channel_id))
                    if channel:
                        embed = discord.Embed(
                            title="🔄 Leaderboard Reset",
                            description=(f"The {period} leaderboard has reset! "
                                         f"Last cycle's standings are archived — "
                                         f"everyone starts fresh."),
                            color=0x7c5cbf)
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[LEVELING RESET] Error for guild {guild_id}: {e}")

    @leaderboard_reset_task.before_loop
    async def before_reset_task(self):
        await self.bot.wait_until_ready()

    # ─── LEADERBOARD ────────────────────────────────────
    # Phase 5 / Prestige system: sort order is now
    # prestige DESC, xp DESC (STATUS.md locked decision) instead of
    # xp DESC alone, so a member who has prestiged always ranks above
    # a same-or-higher-XP member who hasn't, matching the intent of
    # prestige as a status tier above the raw XP race.
    @app_commands.command(name="leaderboard",
                          description="View the XP leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        # Finalized Prestige: sort on the clamped permanent tier so a legacy
        # levels.prestige above the max (old unbounded mechanic) ranks as V.
        from utils.prestige import MAX_PERMANENT_TIER
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level, prestige FROM levels
                WHERE guild_id = ?
                ORDER BY MIN(prestige, ?) DESC, xp DESC LIMIT 10
            """, (interaction.guild.id, MAX_PERMANENT_TIER))
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No XP data yet.", ephemeral=True)
            return

        embed = discord.Embed(title="⭐ XP Leaderboard",
                              color=0x7c5cbf)
        medals = ["🥇", "🥈", "🥉"]
        # Finalized Prestige: display each member's EFFECTIVE tier (VI for
        # an active Booster). Sort order remains permanent prestige DESC,
        # xp DESC. Display only — never the source of multipliers.
        from utils.prestige import get_effective_prestige, is_booster, tier_label
        for i, (uid, xp, level, prestige) in enumerate(rows, 1):
            medal  = medals[i-1] if i <= 3 else f"#{i}"
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            eff    = await get_effective_prestige(
                interaction.guild.id, uid,
                member=member, bot=self.bot)
            if eff > 0:
                name = f"★{tier_label(eff)} {name}"
            embed.add_field(
                name=f"{medal} {name}",
                value=f"Level {level} • {xp:,} XP",
                inline=False)
        await interaction.response.send_message(embed=embed)

    # ─── SET XP (admin) ─────────────────────────────────
    @app_commands.command(name="setxp",
                          description="Set XP for a member (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setxp(self, interaction: discord.Interaction,
                    member: discord.Member, xp: int):
        # HARDENING (dark-fixes pass): clamp admin-supplied XP to
        # non-negative — nothing previously stopped a negative value
        # from being passed through to xp_progress()/the rank card,
        # which assume xp >= 0. (The clamp now lives in _apply_admin_xp.)
        from utils.level_claims import SOURCE_SETXP
        await interaction.response.defer(ephemeral=True)
        result = await _apply_admin_xp(
            interaction.guild.id, member, xp, self.bot, source=SOURCE_SETXP)
        await interaction.followup.send(
            f"Set {member.mention}'s XP to {result['new_xp']:,} "
            f"(Level {result['new_level']})."
            + _reduction_advice(member, result["old_level"],
                                result["new_level"], result["review"]),
            ephemeral=True)

    # ─── SET LEVEL (admin) ──────────────────────────────
    # The companion to /setxp for the common case: an admin knows the LEVEL they
    # want, not the XP total that produces it. This places the member at the
    # exact start of that level using the bot's own curve (level_start_xp ->
    # xp_for_level -> xp_progress), never a second formula, so /setlevel 10 and
    # "level 10 with 0 XP into it" are the same state.
    #
    # It is an XP correction only. It never deletes or re-opens a reward
    # entitlement: crossings are recorded exactly as /setxp records them, and a
    # downward correction reports what it left behind instead of silently
    # dropping rewards the member may legitimately be owed.
    @app_commands.command(name="setlevel",
                          description="Place a member at the start of a level (admin)")
    @app_commands.describe(
        member="The member whose level is set",
        level=f"Target level, 0-{MAX_SETTABLE_LEVEL}. The member is placed at "
              "the first XP of that level. Use /setxp for anything beyond.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlevel(self, interaction: discord.Interaction,
                       member: discord.Member,
                       level: app_commands.Range[int, 0, MAX_SETTABLE_LEVEL]):
        from utils.level_claims import SOURCE_SETLEVEL
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True)
            return
        # Defence in depth behind the Range above: the callback is also called
        # directly, and the reason has to be stated so the bound is not mistaken
        # for a level cap the bot enforces on its members.
        if int(level) < 0 or int(level) > MAX_SETTABLE_LEVEL:
            await interaction.response.send_message(
                f"`level` must be a whole number between 0 and "
                f"{MAX_SETTABLE_LEVEL}. That bound is an input guard on the XP "
                f"curve sum, not a maximum level in the game - use `/setxp` for "
                f"anything beyond it. Nothing was changed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target_xp = level_start_xp(level)
        result = await _apply_admin_xp(
            interaction.guild.id, member, target_xp, self.bot,
            source=SOURCE_SETLEVEL)
        # The curve is authoritative: if the stored state does not come back at
        # the requested level, say so rather than reporting success.
        if result["new_level"] != int(level):
            await interaction.followup.send(
                f"⚠️ Could not place {member.mention} at Level {int(level)} — "
                f"the engine reports Level {result['new_level']} for "
                f"{target_xp:,} XP. Nothing else was changed.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{member.mention}: **Level {result['old_level']} → "
            f"Level {result['new_level']}**\n"
            f"XP: {result['old_xp']:,} → {result['new_xp']:,} "
            f"(start of L{result['new_level']}, "
            f"{result['current']} / {result['needed']} into it)"
            + _reduction_advice(member, result["old_level"],
                                result["new_level"], result["review"]),
            ephemeral=True)

    # ─── RESET XP (admin, Phase 5 / Leveling expansion) ─
    # Fixes the flagged gap: dashboard/app.py's COMMAND_CATEGORIES has
    # listed a Leveling "resetxp" entry (Commands dashboard page) with
    # no matching command anywhere in the codebase. This is that
    # command — resets one member's xp/level back to 0 in this guild
    # only. Deliberately does NOT touch leveling_leaderboard_history
    # (that's only ever written by the scheduled/forced full-guild
    # reset below) and does NOT archive the member's XP anywhere —
    # a single-member reset is a moderation correction, not a
    # leaderboard cycle event.
    @app_commands.command(name="resetxp",
                          description="Reset a member's XP and level back to 0 (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetxp(self, interaction: discord.Interaction,
                      member: discord.Member):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?, ?, 0, 0)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp = 0, level = 0
            """, (interaction.guild.id, member.id))
            await db.commit()
        await interaction.response.send_message(
            f"Reset {member.mention}'s XP and level to 0.",
            ephemeral=True)

    # ─── LEVEL TEST RESET (admin, scoped, preview-first) ──
    # `/resetxp` above zeroes XP but deliberately keeps `level_reward_claims`,
    # so a member who has already claimed cannot be used to test the claim flow
    # again: the ledger is a one-way ratchet and re-levelling never re-pays.
    # This is the testing tool for exactly that, and it is deliberately narrow -
    # one member, one guild, preview by default. See utils/level_test_reset.py
    # for what is reversible, what is only reported, and what is never touched.
    @app_commands.command(
        name="level_test_reset",
        description="Preview/reset ONE member's /level reward test state (admin)")
    @app_commands.describe(
        member="The single member whose leveling test state is reset",
        confirm="Required to actually apply; without it this only previews",
        include_delivered="Force-drop claim rows whose delivered value could "
                          "NOT be reversed (staging only; makes them claimable "
                          "again)",
        include_prestige="Also zero permanent Prestige and the booster VI "
                         "activation. Permanent tiers are Shop purchases and "
                         "are NOT refunded.")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_test_reset(self, interaction: discord.Interaction,
                               member: discord.Member, confirm: bool = False,
                               include_delivered: bool = False,
                               include_prestige: bool = False):
        import os
        from utils.level_test_reset import (
            LevelResetError, inspect_member, reset_member,
        )
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # Second key on production: an admin confirming in Discord is not enough
        # on its own, because this deletes reward history. Staging needs no flag.
        production_ok = os.getenv("ALLOW_LEVEL_TEST_RESET", "") == "1"
        try:
            if not confirm:
                # The preview is built with the same flags the apply would use,
                # so what an admin reads is what they would get - including
                # whether prestige is about to be destroyed.
                report = await inspect_member(
                    interaction.guild.id, member.id,
                    include_prestige=include_prestige,
                    include_delivered=include_delivered)
                embed = _level_reset_embed(member, report, applied=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            report = await reset_member(
                interaction.guild.id, member.id, apply=True,
                include_delivered=include_delivered,
                include_prestige=include_prestige,
                allow_production=production_ok, member=member,
                # Stamped on the ledger reversal rows, so the wallet history says
                # which admin's test reset took the value back.
                reversed_by=interaction.user.id)
        except LevelResetError as exc:
            await interaction.followup.send(
                f"Reset refused — nothing was changed.\n`{exc}`", ephemeral=True)
            return
        embed = _level_reset_embed(member, report, applied=report.get("applied"))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─── RESET LEADERBOARD (admin, Phase 5 / Leveling expansion) ─
    # Manual/forced equivalent of leaderboard_reset_task — same
    # perform_leaderboard_reset() call, same archive-then-zero
    # behavior, just triggered on demand instead of waiting for the
    # configured weekly/monthly period to elapse. Mirrors
    # cogs/mvp.py's /mvp_force pattern.
    @app_commands.command(name="resetleaderboard",
                          description="Force an immediate leaderboard reset for this server (admin)")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetleaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reset_config = await get_reset_config(interaction.guild.id)
        period = reset_config.get("period") or "weekly"
        count = await perform_leaderboard_reset(interaction.guild.id, period)
        await interaction.followup.send(
            f"{CHECK_EMOJI} Leaderboard reset — {count} member(s) archived "
            f"and zeroed.")

    # ─── LEVEL REWARDS ──────────────────────────────────
    @app_commands.command(name="level",
                          description="View and claim your level rewards")
    async def level(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from utils.level_claims import list_claims, member_is_booster
        # One claim read, shared by the card and the button state (they must
        # agree, and it is two fewer SQLite connections inside the ack window).
        claims = await list_claims(interaction.guild.id, interaction.user.id)
        claimable, reconcile_only = await _claim_button_state(
            interaction.guild.id, claims, member_is_booster(interaction.user))
        embed = await _level_embed(interaction.guild, interaction.user, "level",
                                   claims=claims)
        view = LevelRewardView(self.bot, interaction.guild.id, interaction.user.id,
                               claimable=claimable,
                               reconcile_only=reconcile_only)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ─── PRESTIGE STATE / READ-ONLY VIEW ─────────────────
    # The old XP/level-gated "/prestige reset" command has been retired
    # (the finalized Prestige system is Shop-purchased and never resets
    # XP/Level). This status command shows the member's current
    # Prestige: their permanent tier and their effective tier (which is
    # VI only with a valid activation and an active Discord boost).
    # It grants nothing; verified expiry may remove a stale VI activation.
    @app_commands.command(name="prestige",
                          description="View your Prestige state")
    async def prestige(self, interaction: discord.Interaction):
        from utils.prestige import (
            get_permanent_prestige, get_effective_prestige,
            is_booster, tier_label,
        )
        await interaction.response.defer()
        permanent = await get_permanent_prestige(
            interaction.guild.id, interaction.user.id)
        booster = is_booster(interaction.user)
        effective = await get_effective_prestige(
            interaction.guild.id, interaction.user.id, member=interaction.user)

        embed = discord.Embed(
            title="⭐ Prestige",
            color=0xFFD700)
        embed.add_field(
            name="Permanent Prestige",
            value=(f"**{tier_label(permanent)}**"
                   if permanent else "None"),
            inline=False)
        embed.add_field(
            name="Effective Prestige",
            value=f"**{tier_label(effective)}**",
            inline=False)
        if booster and effective == 6:
            explanation = (
                "Your free Prestige VI activation is active while you boost. "
                "When boosting ends, VI activation is removed and you return to "
                f"permanent Prestige {tier_label(permanent)}. After re-boosting, "
                "activate again for Free; VI does not return automatically.")
        elif booster:
            explanation = (
                "You're eligible for free Prestige VI activation in the Shop. "
                "Booster status alone does not activate VI. After re-boosting, "
                "activate again for Free; old VI activations do not return automatically.")
        else:
            explanation = (
                "Only active Discord Boosters can activate Prestige VI, for Free. "
                "When a boost ends, VI has ended and its activation is removed. "
                f"Your effective Prestige is your permanent Prestige {tier_label(permanent)}. "
                "If you boost again, activate VI again for Free; it does not return automatically.")
        embed.add_field(name="Booster Prestige VI", value=explanation, inline=False)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
