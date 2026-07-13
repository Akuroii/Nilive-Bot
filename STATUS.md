# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 0 server permission gating shift complete.

## Phase 0: Server Permission Gating (this shift)

### Internal design answers (kept out of chat, recorded here)
1. Dashboard already does OAuth2 (`dashboard/auth.py: fetch_discord_guilds`),
   used by `/server-select`. That call returns the user's own guild list
   only — no bot-membership info, no admin-bit info exposed to the
   frontend. Both were missing and are added this shift.
2. Server list template: `dashboard/templates/server_select.html`. Left
   its existing "Select a Server" card (backed by `dashboard_users`,
   Nilive's own per-guild access-approval table) untouched, and added a
   second, independent card below it for this feature.
3. No existing Invite Bot flow anywhere in the repo. Built from scratch.
4. Invite URL is generated in `dashboard/auth.py::get_bot_invite_url()`,
   same file/pattern as the existing `get_discord_oauth_url()` (user
   login OAuth) — kept URL-building logic in one place rather than
   inlining it in app.py.
5. `/api/user/servers` and `/api/invite-bot` are gated with
   `@login_required` (from `dashboard/auth.py`), NOT
   `@require_api_permission` (from `dashboard/permissions.py`). The
   latter calls `get_session_guild_id()` internally and 400s if no guild
   is selected — these two routes run BEFORE guild selection by design,
   so they live as plain `app.route`s in `dashboard/app.py`, not in the
   `api_bp` blueprint (`dashboard/api.py`).

### Design decision: filtering / state model
The spec's three rendering states ("active", "gray + invite", "hidden")
and its filtering note ("only include servers where user_admin ...")
overlap in a way that only resolves cleanly one way: `is_admin=false`
guilds are filtered out server-side (Hidden) since a non-admin user has
no action available for that guild either way. Of the remaining
admin-only guilds: bot present → Active (full opacity, no button); bot
absent → Gray (0.5 opacity, Invite Bot button). This is implemented and
documented inline in `dashboard/app.py::api_user_servers`.

This is intentionally decoupled from `dashboard_users` — that table
still gates who can actually operate the dashboard for a guild once Nero
is in it. This feature only answers "which of your Discord servers could
Nero be added to, and is she there yet."

### Endpoints added
- `GET /api/user/servers` (`dashboard/app.py`, `@login_required`)
  Returns `{"servers": [{id, name, icon, is_bot_member, is_admin}, ...]}`
  for every guild the logged-in user owns or has ADMINISTRATOR in.
  Non-admin guilds are omitted. Sorted bot-missing-first.
- `GET /api/invite-bot?guild_id=<id>` (`dashboard/app.py`, `@login_required`)
  Returns `{"invite_url": "..."}`, a guild-locked
  (`disable_guild_select=true`) Discord bot-invite OAuth2 URL.

### Supporting helpers added (`dashboard/auth.py`)
- `fetch_discord_bot_guilds()` — bot-token call to
  `GET /users/@me/guilds`, returns guild IDs the bot is currently in.
  Fails soft (`[]`) on any error — a network hiccup here just means an
  already-invited server shows an unnecessary Invite button, never a
  hidden active server.
- `guild_permissions_include_admin(permissions_str, is_owner)` — decodes
  Discord's per-guild `owner` bool + `permissions` bitfield string into
  a single is-admin check (owner OR `ADMINISTRATOR` bit `0x8`).
- `get_bot_invite_url(guild_id)` — builds the invite URL. Requested
  permission integer defaults to Administrator (`8`), overridable via
  `DISCORD_BOT_PERMISSIONS` env var — see inline comment in auth.py for
  why Administrator was chosen for this deployment model (small,
  trusted, approval-based friend servers; cogs span moderation/roles/
  channels already).

### Frontend (`dashboard/templates/server_select.html`)
Added a new "➕ Manage Servers" card below the existing server-select
list. JS fetches `/api/user/servers` on load, renders each guild:
- Bot present → full-opacity row, no button, "Nero is active here".
- Bot absent → 0.5-opacity row + "Invite Bot" button, "Nero isn't in
  this server yet".
`inviteBotTo(guildId)` hits `/api/invite-bot`, opens the returned URL
in a new tab, and shows a toast prompting the user to refresh once
they're done — refreshing re-runs `loadUserServers()` on page load,
which re-fetches bot membership and moves the guild into the active
group.

### Not done / explicitly out of scope this shift
- No automated tests added (no test harness exists yet in the repo to
  hook into — flagging for a future shift rather than inventing one
  ad hoc).
- No change to `dashboard_users` or the existing dashboard-access
  request flow — an admin who invites the bot still needs to be added
  under Dashboard Access separately, same as before.
- Did not touch Economy v2 or any Phase 5 work.

### Files changed this shift
- `dashboard/auth.py` — added `fetch_discord_bot_guilds`,
  `guild_permissions_include_admin`, `get_bot_invite_url`,
  `ADMINISTRATOR_PERMISSION_BIT`, `DEFAULT_BOT_INVITE_PERMISSIONS`.
- `dashboard/app.py` — added `/api/user/servers`, `/api/invite-bot`,
  updated the `dashboard.auth` import line. No other route changed.
- `dashboard/templates/server_select.html` — added the "Manage
  Servers" card + `{% block scripts %}` (new to this template).
- `STATUS.md` — this section.
- Compiled clean: `python3 -m py_compile dashboard/app.py
  dashboard/auth.py` → exit 0.

## NOT included in this ZIP (unchanged — pull from your last full ZIP)
Everything except `dashboard/auth.py`, `dashboard/app.py`,
`dashboard/templates/server_select.html`, `STATUS.md`:
- database.py, main.py, dashboard/permissions.py, dashboard/api.py,
  dashboard/utils/*
- utils/* (reward_engine.py, ledger.py, inventory.py, economy_safe.py,
  permissions.py, xp_calculator.py, formatters.py)
- cogs/* (all cogs — untouched)
- dashboard/templates/* (all other templates — untouched)
- dashboard/static/*
- requirements.txt, Dockerfile, start.sh, .gitignore

## Phase 3 status (unchanged this shift)
E1 ✅ E2 ✅ E3 ✅ E4 ✅ — untouched. Trade System still BLOCKED pending
live production verification, per standing rule.
