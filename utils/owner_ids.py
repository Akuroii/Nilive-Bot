"""Identity tiers for the bot, parsed from Railway/env variables.

Two tiers, two env vars (both accept a single ID or a comma-separated list):

  OWNER_ID  The bot's real owner(s). Full bot-wide trust (every installed server,
            backups, owner-only Discord commands) AND a visible 'owner' row in each
            server's dashboard Access page.
  DEV_ID    The developer(s). Same trust as OWNER, but never shown in Access (the
            existing guild-blind bypass), plus the dangerous operator actions
            (make the bot leave a server, !sync, !reload).

Backward compatible: when DEV_ID is unset, the OWNER_ID value(s) are ALSO the
developer(s), which is exactly how a deploy that only has OWNER_ID behaved before
this module existed.

This module imports nothing from the project so database.py can use it at import
time. Callers must import the resulting sets from `database`, not re-parse.
"""
from __future__ import annotations

from typing import Mapping, Optional


class OwnerConfigError(ValueError):
    """OWNER_ID / DEV_ID is missing or malformed. Startup must stop, not guess."""


def parse_id_list(raw: Optional[str], var_name: str) -> tuple[int, ...]:
    """'1, 2,3' -> (1, 2, 3). Order kept, duplicates dropped, blanks ignored.

    Raises OwnerConfigError on any non-numeric or out-of-range entry instead of
    silently skipping it: a typo in an ID that grants trust must not be ignored.
    """
    seen: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not (part.isascii() and part.isdigit()):
            raise OwnerConfigError(f"{var_name} has a non-numeric entry: {part!r}")
        value = int(part)
        # 0 is allowed on purpose: read-only maintenance scripts use OWNER_ID=0 as a
        # placeholder, and the pre-existing int() parse accepted it.
        if not 0 <= value <= 2**63 - 1:
            raise OwnerConfigError(f"{var_name} has an out-of-range entry: {part!r}")
        if value not in seen:
            seen.append(value)
    return tuple(seen)


def load_tiers(environ: Mapping[str, str]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return (owner_ids, dev_ids) from an environment mapping.

    OWNER_ID is required. DEV_ID is optional; when absent, dev_ids == owner_ids.
    """
    owners = parse_id_list(environ.get("OWNER_ID", ""), "OWNER_ID")
    if not owners:
        raise OwnerConfigError("OWNER_ID is missing or empty")
    devs = parse_id_list(environ.get("DEV_ID", ""), "DEV_ID") or owners
    return owners, devs
