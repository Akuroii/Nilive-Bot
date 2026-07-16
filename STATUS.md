# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Security pass complete (CSRF gap + custom-command privilege
escalation). Prestige system (Phase 5 tail) still mid-build from prior
session — NOT touched this shift.

## ⚠️ TRACKER CORRECTION (read this first)
STATUS.md going into this shift claimed E3 (Ledger) and E4 (Inventory)
were "⏳ BUILDING" and Phase 5 was "not started." **Both were wrong** —
the uploaded ZIP already contains full, working implementations:

- **E3 Ledger**: `utils/ledger.py` (log/get/reverse, all currencies),
  wired into `utils/economy_safe.py` (every credit/deduct/transfer/convert
  logs automatically), dashboard `/ledger` page + `/api/ledger` +
  `/api/ledger/reverse/<id>` — all present and functional.
- **E4 Inventory**: `utils/inventory.py` (give/remove/set/has/get),
  wired into `utils/reward_engine.py`'s "item" branch, `/inventory` slash
  command in `cogs/shop.py`, dashboard `/inventory` + `/inventory/<uid>`
  pages + API — all present and functional.
- **Ledger/Inventory sidebar nav links**: already in `dashboard/templates/base.html`.
  Not a gap.
- **Phase 5** (Economy v2 dual-currency, `/convert`, diamond shop items,
  leveling currency rewards, XP boost shop items, leaderboard resets):
  all shipped and wired end-to-end.

**Do not rebuild any of the above.** If a future session's STATUS.md
still says "building," trust the ZIP contents over the note.

## Fixed this shift (security)

1. **CRITICAL — CSRF bypass on app-level `/api/*` routes.**
   `dashboard/api.py`'s CSRF check only runs via `api_bp.before_request`,
   which ONLY fires for routes registered on that blueprint. Several
   state-changing `/api/*` routes are registered directly on `app` in
   `dashboard/app.py` instead (`api_edit_member`, `api_command_toggle`,
   `api_commands_bulk_toggle`, `api_command_settings_save`,
   `api_save_embed_template` + delete, `api_save_trigger` +
   `api_delete_trigger`, `api_save_custom_command` +
   `api_delete_custom_command`, `api_save_rr_panel`) — none of them were
   ever covered. Session-cookie-only auth = classic CSRF: a page an
   already-logged-in admin merely visits could silently disable every
   bot command, rewrite a member's XP/coins, or plant a malicious custom
   command with ban/kick actions.
   **Fix**: added `_enforce_csrf_app_level()` — an `@app.before_request`
   hook in `dashboard/app.py` that mirrors `api_bp`'s check (same
   session/header token compare, same safe-method exemption) but scoped
   to any `/api/*` path on the app object. Covers the routes above; is a
   harmless no-op duplicate for `api_bp`'s own routes.
   **File**: `dashboard/app.py` (delta — full file, only this hook + its
   docstring-comment added, nothing else changed).

2. **HIGH — privilege escalation via custom commands.**
   `cogs/customcommands.py`:
   - `warn` was missing from the `destructive` action set that gates on
     `can_moderate()` hierarchy check — a member with access to a
     `!trigger` mapped to `warn` could warn someone ABOVE them in role
     rank, bypassing the same rule `/warn` itself enforces. Added.
   - `add_role:<id>` / `remove_role:<id>` actions only ever checked
     whether the BOT could assign the role (`check_bot_role_position`)
     — never whether the ACTOR typing the command was allowed to grant
     it. A mod with access to a trigger configured with
     `add_role:<admin-role-id>` could hand out roles above their own
     rank. Added an actor-hierarchy check (guild owner, or actor's top
     role strictly above the target role) before either action fires.
   **File**: `cogs/customcommands.py` (delta — full file).

Both files compile-checked: `python3 -m py_compile dashboard/app.py
cogs/customcommands.py` — exit 0.

## Still open (not done this shift, flagged not forgotten)

- **`/config/access` and `/config/commands` classic `<form>` POSTs** are
  still uncovered by any CSRF check (the hook above only fires on
  `/api/*` paths; these are plain form routes). Lower blast radius than
  item #1 (owner-only page, single add/remove-user and toggle actions,
  not "wipe your whole command config" scale) but still a real gap.
  Needs a hidden `csrf_token` input in `config/access.html` and
  `config/commands.html` (or its POST handler) checked server-side.
  Deferred to avoid touching untested form flows in the same pass as
  the `/api/*` fix — do this next.
- **`dashboard/templates/config/commands.html`** confirmed orphaned by
  inspection (no route renders it — `config_commands()` in `app.py`
  only POST-processes + redirects; `manage/commands.html` at `/commands`
  is the live page). Safe to delete. Not deleted this shift since it's
  inert either way; low priority.
- **Reaction-role expiry sentinel collision**: `reaction_role_expiry`'s
  template row (`user_id=0`) is keyed by `(guild_id, role_id)` only —
  if the same role is attached via `reactionrole_add` to two different
  messages with different `expiry_days`, the second call silently
  overwrites the first's template expiry. Needs `message_id` added to
  the sentinel key, or a different dedup strategy. Not touched.
- **Unbounded in-memory cooldown dicts** (`_command_cooldowns` in
  main.py, `_daily_cooldowns`/`_work_cooldowns` in economy.py,
  `_xp_cooldowns`/`_spam_tracker` in leveling.py, `_last_fired` in
  triggers.py) — slow memory leak, restart-cleared, low priority. Not
  touched.

## Prestige system (Phase 5 tail — from prior session, still incomplete)
DB layer built (`levels.prestige` column, `prestige_config`,
`prestige_roles` tables — all in `database.py`, already in this ZIP).
**Not built yet**, same as last handoff:
1. `utils/xp_calculator.py`: `total_xp_for_level()`,
   `get_prestige_config()`, `perform_prestige()`.
2. `cogs/leveling.py`: `/prestige` command, tier-role swap, leaderboard
   `ORDER BY prestige DESC, xp DESC` + UI badge.
3. `dashboard/api.py`: CRUD for `prestige_config` / `prestige_roles`.
4. `dashboard/templates/systems/leveling.html`: Prestige tab/section.
5. `dashboard/app.py` `COMMAND_CATEGORIES["Leveling"]`: add `"prestige"`.
See prior STATUS.md section (superseded below) for confirmed design
decisions (carry-over XP, keep level-role rewards, one-role-per-tier
swap, min_level default 50, sort prestige DESC then xp DESC) — those
are still locked, do not re-ask.

## Best next step for the next session
1. Finish the `/config/access` + `/config/commands` CSRF gap (form-based
   token), OR
2. Resume Prestige system build (items 1–5 above) — whichever Akuroi
   prioritizes. Recommend asking, don't assume.
3. Either way: re-verify E3/E4/Phase 5 are NOT rebuilt.

## Files in this ZIP (delta — supersedes only these from prior ZIPs)
- `dashboard/app.py` — CSRF hook added
- `cogs/customcommands.py` — hierarchy fixes (warn, add_role, remove_role)
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all other `cogs/*`, all other `dashboard/*` (including
`dashboard/api.py`, `dashboard/auth.py`, `dashboard/permissions.py`, all
templates, static JS/CSS), all `utils/*`, `database.py`, `main.py`,
`requirements.txt`, `Dockerfile`, `start.sh`, `.gitignore`,
`DEBUG_GUIDE.md`, `HANDOFF_NOTES.md`, `SUMMARY.txt`.
