# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 5 — Leveling expansion, round 2. Stopping now (context
budget) per instruction — next session picks up from "Still needed" below.

## This round added (on top of the currency-rewards ZIP already delivered)
- **database.py** — 2 new tables:
  - `leveling_reset_config` (guild_id PK, enabled, period, last_reset)
  - `leveling_leaderboard_history` (snapshot of the full leaderboard
    taken immediately before every reset — resets archive, they don't
    destroy)
- **cogs/leveling.py** (full file, rebuilt from the canonical ZIP + additions):
  - `/resetxp` — fixes the flagged gap: `dashboard/app.py`
    `COMMAND_CATEGORIES` has listed this command with nothing behind
    it. Now implemented: admin-only, resets one member's xp/level to 0,
    guild-isolated.
  - `/resetleaderboard` — admin, forces an immediate reset (mirrors
    `/mvp_force`'s pattern).
  - `leaderboard_reset_task` — 30-min poll loop, same shape as
    `cogs/mvp.py`'s `mvp_cycle_task`: compares elapsed time since
    `last_reset` against the configured weekly/monthly period, only
    fires once it's actually due. Each guild isolated in its own
    try/except (matches temp_role_cleanup / expiry_check pattern).
  - `perform_leaderboard_reset(guild_id, period)` — shared by the task,
    `/resetleaderboard`, and the dashboard's force-reset button. Reads
    current `levels` for that guild only, writes a ranked snapshot to
    `leveling_leaderboard_history`, zeroes `levels.xp/level`, updates
    `last_reset`.
- **dashboard/api.py** — 3 new ADMIN+ routes (guild-scoped, same
  pattern as every other route in the file):
  - `GET /api/leveling/reset-config`
  - `POST /api/leveling/reset-config`
  - `POST /api/leveling/force-reset`
- **dashboard/templates/systems/leveling.html** — Config tab gets a
  "Leaderboard Resets" card: enable toggle, weekly/monthly period,
  last-reset timestamp, Save + Force Reset Now (wired to the real API
  route, not a stub).

Compile-checked: `python3 -m py_compile database.py utils/xp_calculator.py
utils/reward_engine.py dashboard/api.py cogs/leveling.py` — exit 0.

## Verified, not touched
- Everything from the previous checkpoint (currency level rewards,
  role rewards, E1–E4, Economy v2) — unchanged.
- `cogs/mvp.py` — pattern-matched for the reset task shape, not modified.

## Flagged, not fixed
- `dashboard/app.py`'s `COMMAND_CATEGORIES["Leveling"]` list doesn't
  include `resetleaderboard` yet (it already had `resetxp`, which is
  now real). Purely cosmetic on the Commands dashboard page — the
  slash command itself works regardless. Not touched this round to
  avoid rewriting the full `app.py` file under context pressure;
  one-line list edit next session.

## Still needed for Phase 5 (not started)
- XP boost items (shop-integrated) — e.g. a shop item type that grants
  a temporary XP multiplier, likely via a new `leveling_active_boosts`
  table + a check inside `calculate_message_xp`/voice XP path.
- Prestige system.

## Files in this ZIP (cumulative — supersedes the first Phase 5 ZIP)
- `database.py`
- `utils/xp_calculator.py`
- `utils/reward_engine.py`
- `dashboard/api.py`
- `dashboard/templates/systems/leveling.html`
- `cogs/leveling.py`
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all other `cogs/*`, `dashboard/*` (except api.py and
leveling.html), `utils/*` (except xp_calculator.py, reward_engine.py),
`main.py`, `requirements.txt`, `Dockerfile`, `start.sh`, `.gitignore`,
`DEBUG_GUIDE.md`, `HANDOFF_NOTES.md`.
