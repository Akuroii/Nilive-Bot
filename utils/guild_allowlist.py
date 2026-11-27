"""Optional server allowlist: keep the bot out of servers nobody approved.

Configured only through Railway/env variables, nothing hard-coded:

  ALLOWED_GUILD_IDS      comma-separated server IDs that are approved.
                         EMPTY  => the feature is OFF (the bot stays in every server it is in).
  GUILD_ALLOWLIST_MODE   off | log | enforce
                         log     (default when the list is set) only prints what it WOULD do.
                         enforce the bot leaves any server that is not on the list.

Fail-safe rules, because a typo here must never remove the bot from a real server:
  * an unparsable list turns the feature OFF and prints a loud warning;
  * an unknown mode value falls back to `log`, never `enforce`;
  * a startup sweep in `enforce` mode refuses to run when none of the allowed servers is
    one the bot is currently in (that looks like a typo), instead of leaving everything;
  * a server on the list can never be left with /dev_leave_guild.

The pure functions below are what tests exercise; the two async helpers at the bottom only
translate a decision into `guild.leave()`.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from utils.owner_ids import OwnerConfigError, parse_id_list

OFF = "off"
LOG = "log"
ENFORCE = "enforce"
_MODES = (OFF, LOG, ENFORCE)

ALLOW = "allow"
LOG_ONLY = "log_only"
LEAVE = "leave"


@dataclass(frozen=True)
class AllowlistConfig:
    ids: frozenset
    mode: str                 # effective mode: off | log | enforce
    problems: tuple = ()      # human-readable configuration problems (already fail-safe)


def load_allowlist(environ: Mapping[str, str]) -> AllowlistConfig:
    problems: list[str] = []
    try:
        ids = frozenset(parse_id_list(environ.get("ALLOWED_GUILD_IDS", ""), "ALLOWED_GUILD_IDS"))
    except OwnerConfigError as exc:
        return AllowlistConfig(frozenset(), OFF, (f"{exc}; allowlist DISABLED",))

    if not ids:
        return AllowlistConfig(frozenset(), OFF, ())

    raw_mode = (environ.get("GUILD_ALLOWLIST_MODE", "") or "").strip().lower()
    if raw_mode == "":
        mode = LOG
    elif raw_mode in _MODES:
        mode = raw_mode
    else:
        mode = LOG
        problems.append(f"GUILD_ALLOWLIST_MODE={raw_mode!r} is not one of {_MODES}; using 'log'")
    return AllowlistConfig(ids, mode, tuple(problems))


def decide(cfg: AllowlistConfig, guild_id: int) -> str:
    """ALLOW, LOG_ONLY (would leave, but mode is log) or LEAVE."""
    if cfg.mode == OFF or int(guild_id) in cfg.ids:
        return ALLOW
    return LOG_ONLY if cfg.mode == LOG else LEAVE


@dataclass(frozen=True)
class SweepPlan:
    leave: tuple = ()
    log_only: tuple = ()
    refused: Optional[str] = None


def sweep_plan(cfg: AllowlistConfig, current_guild_ids: Iterable[int]) -> SweepPlan:
    current = [int(g) for g in current_guild_ids]
    unlisted = tuple(g for g in current if decide(cfg, g) != ALLOW)
    if not unlisted:
        return SweepPlan()
    if cfg.mode == LOG:
        return SweepPlan(log_only=unlisted)
    if cfg.mode == ENFORCE:
        if not any(g in cfg.ids for g in current):
            return SweepPlan(
                log_only=unlisted,
                refused=("none of ALLOWED_GUILD_IDS matches a server the bot is in; "
                         "refusing to leave every server (check the list for a typo)"),
            )
        return SweepPlan(leave=unlisted)
    return SweepPlan()


@functools.lru_cache(maxsize=1)
def get_config() -> AllowlistConfig:
    """Read once per process; env changes need a restart anyway."""
    cfg = load_allowlist(os.environ)
    for problem in cfg.problems:
        print(f"[ALLOWLIST] WARNING: {problem}")
    return cfg


async def handle_join(guild) -> bool:
    """Call from on_guild_join. Returns False when the bot left, so the caller skips setup."""
    cfg = get_config()
    verdict = decide(cfg, guild.id)
    if verdict == ALLOW:
        return True
    if verdict == LOG_ONLY:
        print(f"[ALLOWLIST] (log mode) would leave unapproved guild {guild.id} ({guild.name})")
        return True
    print(f"[ALLOWLIST] leaving unapproved guild {guild.id} ({guild.name})")
    try:
        await guild.leave()
    except Exception as exc:  # the startup sweep retries on the next boot
        print(f"[ALLOWLIST] could not leave guild {guild.id}: {exc!r}")
    return False


async def run_sweep(bot) -> SweepPlan:
    """Call once after the gateway is ready. Never raises."""
    cfg = get_config()
    plan = sweep_plan(cfg, [g.id for g in bot.guilds])
    if cfg.mode == OFF:
        return plan
    print(f"[ALLOWLIST] sweep mode={cfg.mode} allowed={len(cfg.ids)} "
          f"leave={len(plan.leave)} log_only={len(plan.log_only)}")
    if plan.refused:
        print(f"[ALLOWLIST] REFUSED: {plan.refused}")
    for gid in plan.log_only:
        g = bot.get_guild(gid)
        print(f"[ALLOWLIST] (no action) unapproved guild {gid} ({getattr(g, 'name', '?')})")
    for gid in plan.leave:
        g = bot.get_guild(gid)
        if g is None:
            continue
        print(f"[ALLOWLIST] leaving unapproved guild {gid} ({g.name})")
        try:
            await g.leave()
        except Exception as exc:
            print(f"[ALLOWLIST] could not leave guild {gid}: {exc!r}")
    return plan
