# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 5 — prestige system DESIGN LOCKED + DB layer built. Bot
command, reward-engine wiring, and dashboard UI NOT built yet.

## Design decisions (confirmed by Akuroi this shift — do not re-ask)
1. Prestige XP handling: **carry over excess.** On prestige, subtract the
   cumulative XP required to reach `min_level` from the member's current
   total XP; the remainder becomes their new XP (level recalculated from
   that remainder via `calculate_level_from_xp`), not a hard reset to 0.
2. Level-role rewards (`leveling_rewards`) on prestige: **keep all.** Do
   NOT strip roles already earned via `check_and_award_level_rewards`.
3. Prestige badge: **one role per tier**, swapped — remove the previous
   tier's role (if any) and add the new tier's role, same "one role
   represents current standing" pattern as MVP's role.
4. Min level to prestige: not explicitly answered — defaulted to a
   per-guild configurable value, default **50**, stored in
   `prestige_config.min_level`. Confirm/change with Akuroi if wrong.
5. Leaderboard sort: not explicitly answered — implement as
   **prestige DESC, then xp DESC** (natural pairing with "carry over
   excess" + "role per tier", since prestige is now a visible tier not
   a full reset). Confirm/change with Akuroi if wrong.

## Built this round
- **database.py** — two new tables, both additive/safe migrations:
  - `levels.prestige` column (ALTER, default 0) — prestige tier lives on
    the same per-member row as xp/level, no join needed anywhere that
    already reads `levels`.
  - `prestige_config` (guild_id PK, enabled, min_level) — per-guild gate
    + threshold, same shape as `leveling_reset_config`.
  - `prestige_roles` (id, guild_id, tier, role_id, UNIQUE(guild_id,tier))
    — tier → role mapping, same shape as `leveling_rewards`.

Compile-checked: `python3 -m py_compile database.py` — exit 0.

## NOT built yet — next session should do these in order
1. **utils/xp_calculator.py**:
   - `total_xp_for_level(level)` — cumulative sum of `xp_for_level(1..level)`,
     needed to compute the "excess above min_level" carry-over amount.
     (`xp_for_level` currently only returns the *marginal* cost of one
     level, not cumulative — don't reuse it directly for this.)
   - `get_prestige_config(guild_id)` — same pattern as
     `get_leveling_config`, falling back to `{"enabled":1,"min_level":50}`
     when no row exists.
   - `perform_prestige(guild_id, user_id)` — validates current level >=
     min_level, computes excess XP via `total_xp_for_level(min_level)`,
     writes new xp/level/prestige+1 to `levels`, returns old/new tier +
     new xp/level for the caller to use in role swap + announce.
2. **cogs/leveling.py** — new `/prestige` slash command:
   - Calls `perform_prestige`.
   - Role swap: look up `prestige_roles` for old tier (remove if member
     has it) and new tier (add it) — reuse
     `utils.permissions.check_bot_role_position` before assigning, same
     guard every other role-grant path in this codebase already uses.
   - Does NOT touch `leveling_rewards` roles (keep-all, per decision #2).
   - Leaderboard query (`leaderboard` command + dashboard `/leveling`
     page + `/api/leveling/leaderboard`) needs `ORDER BY prestige DESC,
     xp DESC` and to display the prestige tier (e.g. a small "P{n}"
     badge next to the level badge).
3. **dashboard/api.py** — CRUD routes for `prestige_config` (GET/POST,
   ADMIN+) and `prestige_roles` (GET/POST/DELETE, ADMIN+), mirroring the
   existing `leveling/reset-config` and `leveling/reward` routes exactly.
4. **dashboard/templates/systems/leveling.html** — new "Prestige" tab or
   a section in the existing Config tab: min-level input, enable toggle,
   tier→role add/list/delete table (reuse the `nero-role-picker` pattern
   already used for bonus roles / rewards on this same page).
5. Update `COMMAND_CATEGORIES["Leveling"]` in `dashboard/app.py` to
   include `"prestige"` once the command exists (same one-line pattern
   as the `resetleaderboard` fix from the prior round).
6. Compile-check everything touched, re-zip cumulative.

## Already confirmed complete (do not rebuild)
- XP Boost Shop Items — full stack (cogs/shop.py, xp_calculator.py,
  leveling.py, dashboard UI + api.py) confirmed complete in the ZIP.
- `resetleaderboard` in `COMMAND_CATEGORIES` — fixed previous round.
- E1–E4 shared engines — all live, do not rebuild.

## Still correctly blocked
- **Trade System** — technically unblockable now (E3 ledger + E4
  inventory both confirmed live), but Master Plan v2.0 says finish
  Phase 5 (prestige) before starting Phase 6 work. Don't jump ahead.
- **Event Stack Builder** (Phase 6) — same reason, wait for Phase 5 close.

## Files in this ZIP (delta — supersedes only database.py from prior ZIPs)
- `database.py`
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all `cogs/*`, all `dashboard/*` (app.py already has the
resetleaderboard fix from the prior delta ZIP — merge that in if working
from an older base), all `utils/*`, `main.py`, `requirements.txt`,
`Dockerfile`, `start.sh`, `.gitignore`, `DEBUG_GUIDE.md`,
`HANDOFF_NOTES.md`.
