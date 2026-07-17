# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Prestige system (Phase 5 tail) — all 5 items from prior
STATUS.md's next-step list are now built. Nothing else touched this
shift (no CSRF/form-token work, no other feature work).

## ⚠️ Read this first
Previous STATUS.md's design decisions are LOCKED and were followed
exactly, no re-asking:
- carry-over XP (not hard reset) — excess above the level-50 (default)
  threshold becomes the member's new xp/level
- keep-all level-role rewards — prestige does NOT strip
  leveling_rewards roles
- one role per prestige tier, swapped on prestige (old tier role
  removed if held, new tier role added)
- min_level defaults to 50, configurable per guild
- leaderboard sorted `prestige DESC, xp DESC`

## Built this shift

1. **`utils/xp_calculator.py`**
   - `total_xp_for_level(level)` — cumulative XP to reach a level from
     0 (sum of the existing per-level `xp_for_level`), needed because
     "carry over excess" requires a cumulative threshold, not a
     marginal one.
   - `get_prestige_config(guild_id)` — reads `prestige_config`,
     defaults to `{enabled: 1, min_level: 50}` when no row exists.
   - `get_prestige_roles(guild_id)` — reads `prestige_roles` ordered
     by tier.
   - `perform_prestige(guild_id, user_id)` — validates (enabled,
     level >= min_level), computes new xp/level via the carry-over
     rule, increments `levels.prestige`, writes the row. Raises
     `PrestigeError` with a user-facing message on any failed check
     so the cog doesn't duplicate validation text.
   - No changes to existing `xp_for_level` / `calculate_level_from_xp`
     / `xp_progress` — reused as-is.

2. **`cogs/leveling.py`**
   - `/prestige` command: calls `perform_prestige()`, then does the
     tier-role swap (remove old tier role if held, add new tier role,
     using the existing `check_bot_role_position` guard the same way
     `check_and_award_level_rewards` does), then announces
     old→new tier + level + carried XP.
   - `/rank` and `/leaderboard` now read/display `prestige` and sort
     `prestige DESC, xp DESC`; rank card and embed show a `★N` prefix
     when prestige > 0.
   - Import list extended: `get_prestige_config`, `get_prestige_roles`,
     `perform_prestige`, `PrestigeError` from `utils.xp_calculator`;
     `check_bot_role_position` from `utils.permissions`.

3. **`dashboard/api.py`**
   - `GET/POST /api/leveling/prestige-config` — on/off + min_level,
     same `LEVEL_ADMIN` gate as every other leveling route.
   - `GET /api/leveling/prestige-roles`,
     `POST /api/leveling/prestige-role`,
     `DELETE /api/leveling/prestige-role/<id>` — tier→role_id CRUD.
   - `leveling_leaderboard_partial` (the HTMX-loaded leaderboard
     partial) updated to sort `prestige DESC, xp DESC` and prefix
     `★N` on the user_id cell, matching the cog's slash-command
     leaderboard so the dashboard and Discord never disagree.

4. **`dashboard/templates/systems/leveling.html`**
   - New "🏵️ Prestige" tab: config card (enable toggle, min level),
     add-tier-role form, tier-role table. Leaderboard tab's user cell
     now shows the `★N` prefix (row now carries `prestige` as index 3
     from the updated `/leveling` route + `/api/leveling/leaderboard`
     query).

5. **`dashboard/app.py`**
   - `COMMAND_CATEGORIES["Leveling"]` now includes `"prestige"` —
     this was the explicitly flagged carried-over gap from two
     sessions back.
   - `/leveling` route's query updated to `SELECT ... prestige FROM
     levels ... ORDER BY prestige DESC, xp DESC` so the template's
     initial (pre-HTMX) render matches the live partial.

All five compile-checked: `python3 -m py_compile utils/xp_calculator.py
cogs/leveling.py dashboard/api.py dashboard/app.py` — exit 0.
(Jinja template isn't Python-compiled; hand-verified tag balance —
no server available in-sandbox to render it.)

## Not done this shift (carried forward, not forgotten)
- **`/config/access` and `/config/commands` classic `<form>` CSRF
  gap** — still open, same as the last two STATUS.md notes. Needs a
  hidden `csrf_token` input + server-side check on those two form
  routes specifically (the `/api/*`-scoped hook doesn't cover them).
- **`dashboard/templates/config/commands.html`** — still confirmed
  orphaned (no route renders it). Still safe to delete, still not
  deleted (inert either way, low priority).
- **Reaction-role expiry sentinel collision** (same role attached to
  two messages with different `expiry_days` → second overwrites
  first's template expiry) — not touched.
- **Unbounded in-memory cooldown dicts** — not touched.
- **Trade System** — still STRICTLY BLOCKED pending explicit go-ahead
  (E3 Ledger + E4 Inventory are both live, per two-shifts-ago
  verification, so the technical blocker is gone — this is now a
  scope/design gate, not a readiness gate).

## Suggested next step
Dark, your message said you'll continue with "dark fixes" next — no
specific list given yet. Best default next steps in priority order:
1. `/config/access` + `/config/commands` CSRF token gap (small, clearly
   scoped, flagged three shifts running).
2. Whatever "dark fixes" refers to — please specify in the next
   session opener so the next chat doesn't have to guess.
3. Trade System design (Phase 6) once you give the go-ahead — E3/E4
   prerequisites are satisfied.

## Files in this ZIP (delta — supersedes only these from prior ZIPs)
- `utils/xp_calculator.py`
- `cogs/leveling.py`
- `dashboard/api.py`
- `dashboard/app.py`
- `dashboard/templates/systems/leveling.html`
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all other `cogs/*`, `dashboard/permissions.py`,
`dashboard/auth.py`, `dashboard/utils/*`, all other templates, static
JS/CSS, `utils/economy_safe.py`, `utils/ledger.py`, `utils/reward_engine.py`,
`utils/inventory.py`, `utils/formatters.py`, `utils/permissions.py`,
`database.py` (prestige tables already exist from the shift before this
one — no schema changes needed here), `main.py`, `requirements.txt`,
`Dockerfile`, `start.sh`, `.gitignore`, `DEBUG_GUIDE.md`,
`HANDOFF_NOTES.md`, `SUMMARY.txt`.
