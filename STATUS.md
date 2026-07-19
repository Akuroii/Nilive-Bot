# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #7)

Stopped at: 3 verified bug fixes shipped this pass (1 big DB migration,
2 small). Stopped deliberately at ~70% context, per Dark's rule —
nothing left half-edited. Delta ZIP = 4 files.

## Re-verified against actual ZIP before doing anything (not assumed)

Confirmed already fully done, NOT rebuilt:
- Prestige system (cogs/leveling.py `/prestige`, dashboard CRUD, UI tab)
- CSRF hardening (`dashboard/api/__init__.py` before_request hook +
  `config_access()`/`config_commands()` hidden-token checks)
- `COMMAND_CATEGORIES["Leveling"]` in dashboard/app.py already lists
  `resetxp`, `resetleaderboard`, `prestige` — no change needed

## Fixed this pass (3 bugs, verified against real code, not notes)

### 1. `dashboard_users` missing UNIQUE(guild_id, user_id) — database.py
**Root cause**: table had no unique constraint beyond the surrogate
`id` column. `ensure_owner_access()` (called from `init_db()` on
EVERY bot/dashboard process start) and `add_guild_owner()` both used
`INSERT OR IGNORE`, which had nothing to conflict against — so the
owner got a brand-new duplicate row on every single restart.

**Downstream break**: `config_access()`'s remove_user() deletes by a
single surrogate `id`. With duplicates present, removing one row left
other enabled duplicates (same guild_id+user_id) still granting
access — "Remove User" silently failed to actually revoke access.

**Fix**: migration dedupes existing rows (keeps 'owner' > 'admin' >
'moderator', highest id as tiebreaker), then creates
`CREATE UNIQUE INDEX idx_du_unique ON dashboard_users(guild_id, user_id)`.
From here on `INSERT OR IGNORE` actually ignores true duplicates.

**Tested**: seeded a throwaway DB with 3 duplicate owner rows +
1 legit moderator row, ran `init_db()`, confirmed:
- dedupe log fired, exactly 1 row per (guild_id, user_id) remained
- unique index exists
- a subsequent `INSERT OR IGNORE` for the same pair correctly no-ops
(full python3 test script + output captured in this session).

### 2. Cross-guild cooldown leak — cogs/economy.py
`_daily_cooldowns` / `_work_cooldowns` were keyed by `user_id` alone.
A member in 2+ of Dark's controlled servers claiming `/daily` in
Guild A put them on cooldown in Guild B too (and vice versa) — no DB
involved, pure in-memory leak across the isolation boundary every
other piece of state in this project respects.
**Fix**: both dicts now keyed by `(guild_id, user_id)`.

### 3. XP blacklist roles not applying to voice XP — cogs/leveling.py + utils/xp_calculator.py
`on_activity_voice_tick()` granted voice XP directly and never
consulted `leveling_blacklist_roles` — only `calculate_message_xp()`
(via `get_xp_multiplier()`) ever checked it. A member with a
blacklist role (meant to fully opt them out of leveling) still
silently earned voice XP.
**Fix**: added `is_role_blacklisted(guild_id, member_role_ids)` to
`utils/xp_calculator.py` (deliberately NOT reusing
`get_xp_multiplier()` wholesale — that also applies bonus-role
multipliers, which voice XP has never used; fixing blacklist
shouldn't silently change voice XP payout math too). Wired into
`on_activity_voice_tick()` before the XP grant.

**Verified**: `python3 -m py_compile` clean on all 4 changed files
(database.py, cogs/economy.py, cogs/leveling.py, utils/xp_calculator.py).

## Not done / still open (unchanged from pass #6, re-confirmed real)

- **Ticket permission bypass when `staff_role_id` is null**
  (cogs/tickets.py claim/close/delete) — flagged in project notes,
  NOT yet investigated this pass. Next shift should check whether
  "no staff role configured = anyone can claim/close/delete" is
  actually a bug or an intentional fallback before changing anything.
- **Orphaned `dashboard/templates/config/commands.html`** —
  HANDOFF_NOTES.md from a prior shift claims this was deleted, but
  the most recent uploaded ZIP still contains it, and nothing in
  `dashboard/app.py` renders it (`manage/commands.html` is the live
  template at `/commands`). Contradiction between handoff notes and
  actual ZIP — **verify against the next real ZIP upload before
  trusting either claim**, then delete if still orphaned.
- Unbounded in-memory cooldown dicts (main.py, triggers.py) — still
  low priority, not touched.
- Event Stack Builder / dynamic weekly-quota events — still needs
  `cogs/minigames.py` from Dark to verify before building anything.
- Roleplay GIFs — low priority, unrelated.
- Zero automated test coverage — still open.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: blocked until E3 (ledger) + E4 (inventory) verified
  live in production — both already built per earlier passes.
- ZIP = source of truth. Always verify against actual code before
  scheduling work; STATUS.md and HANDOFF_NOTES.md can be stale (see
  the commands.html contradiction above — proof of exactly this).
