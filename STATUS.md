# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 0 — Server Select bugs fixed (bot startup crash was already fixed last shift).

## This shift — Phase 0 Server Select fixes
Scope was strictly the 4 user-tested bugs. Did NOT touch engines, Economy v2,
main.py crash logic, or anything else already marked done.

### 1) Server icons not loading — FIXED
- Old code always requested `.png` regardless of icon hash. Animated
  server icons (hash prefixed `a_`) are `.gif` on Discord's CDN — a `.png`
  request for those 404s. Both the "Select a Server" list (server-rendered)
  and the "Manage Servers" list (JS-rendered) now pick `.gif` vs `.png`
  based on the `a_` prefix.
- Added an `onerror` fallback on every server icon `<img>` in both lists —
  a stale/broken hash now swaps to the existing letter-avatar placeholder
  instead of showing a broken image icon.

### 2) Invite bot — VERIFIED, no change needed
- `/api/invite-bot/<guild_id>` (dashboard/app.py) already builds a correct
  single-guild-locked invite URL (`client_id`, `guild_id`,
  `disable_guild_select=true`, `permissions`, `scope=bot+applications.commands`)
  via `dashboard/auth.py get_bot_invite_url()`.
- Frontend already opens it in a new tab (`window.open(..., '_blank', 'noopener')`).
- Does NOT touch `dashboard_users` — inviting the bot never grants dashboard
  access to the inviter. Confirmed no auto-seeding path exists here.

### 3) Bot already IN server but cannot SELECT it — FIXED (highest priority)
Root cause: the "Manage Servers" list's bot-installed card was 100% static —
no `<a>`, no `onclick`, nothing. A server showing "Bot installed" had
literally no way to be entered from that card. The only working entry
point was the separate "Select a Server" list above it, which only shows
guilds that already have a `dashboard_users` row — a fresh invite with no
row yet was a dead end.

Fix, two parts:
- `dashboard/templates/server_select.html`: bot-installed cards in
  "Manage Servers" are now real links to `/select-guild/<id>` — same
  destination as the accessible-servers list. Server-side gating is
  unchanged; a regular admin without an existing access row still gets
  the 403 page (correct — "NOT all guild admins" auto-granted, per spec).
- `dashboard/app.py` `select_guild()`: if the requester is
  `OWNER_DISCORD_ID` (the one global super-admin, from `database.py`) and
  a new `bot_is_in_guild()` check (`dashboard/auth.py`, single-guild
  `GET /guilds/{id}` with the bot token) confirms the bot is actually a
  member, `add_guild_owner(guild_id)` is called on the fly (same helper
  `on_guild_join` already uses) before re-checking access. This is scoped
  to `OWNER_DISCORD_ID` only — no other admin/user gets auto-seeded,
  matching "Do NOT auto-seed dashboard_users on invite for everyone".

### 4) STATUS.md — this file, updated now.

## Files in this ZIP (delta only)
- `dashboard/auth.py` — added `bot_is_in_guild()`
- `dashboard/app.py` — `select_guild()` auto-grant path for
  `OWNER_DISCORD_ID`; import of `OWNER_DISCORD_ID` + `add_guild_owner`
  from `database.py`
- `dashboard/templates/server_select.html` — icon fixes + clickable
  bot-installed servers
- `STATUS.md`

Verified: `python3 -m py_compile dashboard/auth.py dashboard/app.py` — exit 0.

## NOT included (unchanged — pull from your last full ZIP)
Everything else: `main.py` (crash fix already merged per last shift),
all `cogs/*`, `dashboard/api.py`, `dashboard/permissions.py`, `utils/*`,
`database.py`, `requirements.txt`, `Dockerfile`, `start.sh`,
`.gitignore`, `DEBUG_GUIDE.md`, `HANDOFF_NOTES.md`.

## Next steps for Dark
1. Merge these 3 files into the canonical project, redeploy.
2. Test as OWNER_DISCORD_ID: click a "Bot installed" card in Manage
   Servers for a guild with no prior dashboard_users row → should land
   in the dashboard, not 403.
3. Test as a non-owner admin whose server has the bot but who has no
   dashboard_users row → should still 403 (expected, by design).
4. Confirm icons render (including any animated server icons) with no
   broken-image placeholders.
5. After verify: Economy v2 live-verify → Leveling (per existing plan).

## Still open (unrelated to this shift)
- Security findings from the earlier static analysis sweep (stored XSS in
  moderation dashboard, missing CSRF, ledger/inventory API↔page privilege
  mismatch, warn/add_role/remove_role hierarchy gaps) remain unresolved.
- Phase 3: E3 Transaction Ledger / E4 Inventory — already implemented per
  the ZIP this shift started from; not touched or re-verified this shift.
