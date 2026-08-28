"""Shared command gating: dashboard toggles, role/channel rules, cooldowns.

Single implementation on purpose. Before this existed there were two copies —
``NeroCommandTree.interaction_check`` in main.py for slash commands and
``check_command_toggles`` in cogs/command_aliases.py for message aliases — and
they had already started to drift (only the slash path pruned the cooldown
dict). Two copies of a security-relevant check is how "works as a slash
command, silently unrestricted as an alias" bugs happen, so both paths now call
in here and share one cooldown dict (``main._command_cooldowns``), keyed the
same way, so ``/kick`` and ``k`` rate-limit together.

No discord.py import is required at runtime here (only for typing), so both the
bot and, if ever needed, tooling can use it.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple

import aiosqlite

from database import DB_PATH

TOGGLE_SELECT = """
    SELECT enabled, allowed_roles, allowed_channels, owner_only,
           cooldown_seconds, bypass_cooldown_roles, error_message,
           enabled_roles, disabled_roles, enabled_channels,
           disabled_channels
    FROM command_toggles
    WHERE guild_id = ? AND command_name = ?
"""


def _parse_id_set(raw_val) -> set:
    if not raw_val:
        return set()
    if isinstance(raw_val, (list, set)):
        return {int(x) for x in raw_val if str(x).isdigit()}
    try:
        parsed = json.loads(raw_val)
        if isinstance(parsed, list):
            return {int(x) for x in parsed if str(x).isdigit()}
    except Exception:
        pass
    return set()


async def load_toggle_row(guild_id: int, command_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(TOGGLE_SELECT, (guild_id, command_name))
        return await cursor.fetchone()


def evaluate_toggle_row(
    row,
    guild_id: int,
    command_name: str,
    member,
    channel_id: int,
    guild_owner_id: Optional[int],
    cooldowns: Dict[tuple, float],
    now: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """Pure half of the gate: no I/O, so probes/tests can drive it directly.

    Returns ``(allowed, message_to_send)``. ``cooldowns`` is mutated when the
    call is allowed, exactly as discord.py's own bucket bookkeeping would.
    """
    if not row:
        return True, None

    (enabled, allowed_roles, allowed_channels, owner_only,
     cooldown_seconds, bypass_cooldown_roles, error_message,
     enabled_roles, disabled_roles, enabled_channels,
     disabled_channels) = row

    if not enabled:
        return False, (error_message
                       or f"`/{command_name}` is currently disabled on this server.")

    if owner_only and getattr(member, "id", None) != guild_owner_id:
        return False, "This command is restricted to the server owner."

    member_role_ids = {r.id for r in getattr(member, "roles", []) or []}

    # Role blacklist
    dis_roles = _parse_id_set(disabled_roles)
    if dis_roles and (member_role_ids & dis_roles):
        return False, "You don't have permission to use this command."

    # Role whitelist. The new column wins outright rather than being unioned
    # with the legacy one: the dashboard writes both from the same field, so a
    # union could only ever be more permissive than what is on screen.
    allow_roles = _parse_id_set(enabled_roles) or _parse_id_set(allowed_roles)
    if allow_roles and not (member_role_ids & allow_roles):
        return False, "You don't have permission to use this command."

    # Channel blacklist
    dis_channels = _parse_id_set(disabled_channels)
    if dis_channels and channel_id in dis_channels:
        return False, "This command can't be used in this channel."

    # Channel whitelist — same fallback rule as the roles above.
    allow_channels = (_parse_id_set(enabled_channels)
                      or _parse_id_set(allowed_channels))
    if allow_channels and channel_id not in allow_channels:
        return False, "This command can't be used in this channel."

    if cooldown_seconds and cooldown_seconds > 0:
        bypass_roles = set()
        if bypass_cooldown_roles:
            try:
                bypass_roles = {int(r) for r in json.loads(bypass_cooldown_roles)}
            except Exception:
                pass
        if not (member_role_ids & bypass_roles):
            if now is None:
                now = time.time()
            key = (guild_id, getattr(member, "id", None), command_name)
            last = cooldowns.get(key, 0)
            if now - last < cooldown_seconds:
                remaining = round(cooldown_seconds - (now - last), 1)
                return False, f"Slow down — try again in {remaining}s."
            cooldowns[key] = now

    return True, None


async def check_command_toggles(
    guild_id: int,
    command_name: str,
    member,
    channel_id: int,
    cooldowns: Dict[tuple, float],
    now: Optional[float] = None,
    guild_owner_id: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Read the guild's toggle row for ``command_name`` and evaluate it."""
    row = await load_toggle_row(guild_id, command_name)
    if guild_owner_id is None:
        guild = getattr(member, "guild", None)
        guild_owner_id = getattr(guild, "owner_id", None)
    if now is None:
        now = time.time()
    return evaluate_toggle_row(row, guild_id, command_name, member, channel_id,
                               guild_owner_id, cooldowns, now)


__all__ = (
    "TOGGLE_SELECT",
    "evaluate_toggle_row",
    "check_command_toggles",
    "load_toggle_row",
)
