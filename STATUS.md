# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 0 Extension — Dashboard Server Permission Gating (Sapphire-style) — done.

## Verification note (read this first)
Prior handoff notes/memory claimed this feature ("GET /api/user/servers,
GET /api/invite-bot, fetch_discord_bot_guilds, guild_permissions_include_admin,
get_bot_invite_url, active/grayed server_select.html cards") was already
shipped. It was NOT present anywhere in the canonical ZIP this shift started
from — grepped dashboard/auth.py and dashboard/app.py, no matches. Built it
for real this shift; treat that prior claim as stale.

## This shift — Phase 0 Extension, Server Permission Gating

Economy v2 (Phase 5) from last shift is CODE-COMPLETE per Dark's note —
not touched this shift, no live verification done (bot offline).

### dashboard/auth.py — 3 new helpers, additive only
- `guild_permissions_include_admin(permissions)` — checks the ADMINISTRATOR
  bit (0x8) on a guild's permissions bitfield from `/users/@me/guilds`.
  Guild owners already carry this bit from Discord, no separate check needed.
- `fetch_discord_bot_guilds()` — bot-token call to `/users/@me/guilds`,
  paginated, returns the set of guild IDs the bot is currently in. Empty
  DISCORD_TOKEN or a failed call returns an empty set (safe default: shows
  "not installed" rather than crashing).
- `get_bot_invite_url(guild_id)` — builds a guild-locked
  (`disable_guild_select=true`) bot invite URL using `bot+applications.commands`
  scopes, matching what DEBUG_GUIDE.md already documents as required.

### dashboard/app.py — 2 new routes, additive only
- `GET /api/user/servers` (`login_required`, no guild session needed) —
  cross-references the user's OAuth guild list against
  `guild_permissions_include_admin` and `fetch_discord_bot_guilds()`, returns
  `{servers: [{id, name, icon, is_bot_member}]}`, bot-installed first.
- `GET /api/invite-bot/<guild_id>` (`login_required`) — returns
  `{url: ...}` for the client to redirect/open.
- Existing `/server-select` and `/select-guild/<id>` routes (the actual
  `dashboard_users`-gated authorization) are UNCHANGED. This feature is
  read-only discovery + an invite-URL generator sitting alongside them —
  it does not grant dashboard access by itself. A server still only becomes
  selectable once it has a `dashboard_users` row (bot join already
  auto-grants `OWNER_DISCORD_ID` via `database.add_guild_owner()`).

### dashboard/templates/server_select.html
- Added a second card, "➕ Manage Servers", below the existing
  dashboard-access list. Loads `/api/user/servers` client-side on page load.
  - `is_bot_member: true` → full-opacity card, green "Bot installed" badge.
  - `is_bot_member: false` → 55% opacity card, red "Not installed" badge,
    "Invite Bot" button → fetches `/api/invite-bot/<id>` → opens the URL in
    a new tab. No page reload required after inviting; refreshing
    `/server-select` re-fetches and flips the card to active once Discord
    reflects the bot join.
- Original dashboard-access list at the top of the page is untouched.

### Compiled clean this shift
`python3 -m py_compile` exit 0 on: `dashboard/auth.py`, `dashboard/app.py`.
Not live-tested against Discord (bot offline this shift, per your note) —
the OAuth/bot-token calls themselves are unverified beyond local syntax and
logic review.

## Files in this ZIP (delta only)
- `dashboard/auth.py`
- `dashboard/app.py`
- `dashboard/templates/server_select.html`
- `STATUS.md`

## NOT included in this ZIP (unchanged — pull from your last full ZIP)
Everything else: all `cogs/*`, `dashboard/api.py`, `dashboard/permissions.py`,
`dashboard/utils/*`, `utils/*`, all other `dashboard/templates/*`,
`dashboard/static/*`, `database.py`, `main.py`, `requirements.txt`,
`Dockerfile`, `start.sh`, `.gitignore`, `DEBUG_GUIDE.md`.

## Still needed / explicitly out of scope this shift
- No live verification against real Discord OAuth/bot API calls (bot
  offline) — logic is correct per Discord's documented API shape, but
  flagging this as unverified until the bot is back up.
- No changes to the internal `dashboard_users` permission model or to who
  can actually select a guild — this shift only added discovery + invite,
  not auto-provisioning access on invite. If you want "invite → auto-add as
  owner/admin in dashboard_users" that's a separate, deliberate decision
  (security-relevant: currently only `OWNER_DISCORD_ID` auto-gets access on
  bot join) — flag if you want that built next.
- Phase 5 next-system pick still open per last shift's note (Leveling /
  Shop / Events / Tickets) — not started.

## Next up
Confirm this Phase 0 Extension looks right once the bot is back online and
you can click through it live. Then either continue Phase 0 follow-ups or
move to the next Phase 5 system.
