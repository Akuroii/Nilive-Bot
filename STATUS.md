# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #16)

## Done this pass: Missions v1 (built ahead of the Trade-verification gate — Dark's explicit override)

**Gate note:** prior STATUS.md locked "Missions comes after Trade is
verified live." Trade shipped last pass but is still unverified. Dark
was asked directly this pass and chose to override and build Missions
now anyway. Flagged in `utils/mission_engine.py`'s header too, so a
future session doesn't mistake this for the gate being forgotten.

Files in this delta:

- `utils/mission_engine.py` — NEW. Own standalone schema (own
  `ensure_tables()`, not in `database.py`'s `init_db()` — same
  pattern `cogs/minigames.py`/`utils/trade_engine.py` already
  established).
  - `missions_definitions` — per-guild mission config: type
    (messages/words/voice_minutes), target, period
    (daily/weekly/once), reward_type/value/duration.
  - `mission_progress` — per (guild, user, mission, period_key) row.
    Daily/weekly reset is implicit: period_key changes every
    day/week, so a new period is just a fresh row — no cron job.
  - `record_activity()` — atomic (`BEGIN IMMEDIATE` per mission
    definition, same concurrency pattern as
    `utils/reward_engine.py`'s xp branch) progress increment +
    completion check. Reward is granted the instant a mission
    completes via `utils/reward_engine.give_reward()` — no separate
    claim step.
- `cogs/missions.py` — NEW. Listens on the SAME events
  `cogs/activity_engine.py` already dispatches
  (`on_activity_message`, `on_activity_voice_tick`) — no new
  tracking hooks added anywhere.
  - `/missions` — member-facing progress view.
  - `/mission_create`, `/mission_list`, `/mission_remove` — admin
    Discord-side CRUD (dashboard CRUD also shipped, see below).
- `dashboard/api/missions.py` — NEW. CRUD routes
  (`/api/missions/list`, `/definition` POST/DELETE, `/toggle`,
  `/completions`), same shape as `dashboard/api/minigames.py`.
- `dashboard/templates/systems/missions.html` — NEW. Config +
  completions-log page, same tab/style pattern as
  `systems/minigames.html`.
- `dashboard/api/__init__.py` — added `from dashboard.api import
  missions` import.
- `utils/permissions.py` — added `"missions": LEVEL_ADMIN` to
  `PAGE_PERMISSIONS`.
- `main.py` — added `"cogs.missions"` to `load_cogs()`'s
  `cog_files` list (right after `cogs.trade`).

All six `.py` files `py_compile` clean.

**NOT included in this ZIP (needs 5 min manual wiring — see
`MISSIONS_WIRING.md` in this delta):** the `/missions` Flask route in
`dashboard/app.py`, the `"Missions"` entry in `app.py`'s
`COMMAND_CATEGORIES`, and the sidebar nav link in `base.html`. Those
three files are too large to re-ship whole for this small an addition
— same reasoning `NAV_LINK_SNIPPET.html` already used for minigames.
`MISSIONS_WIRING.md` has the exact snippets to paste in.

## Scope notes (v1, deliberately not built yet)

- No channel announcement on mission completion — `/missions` shows
  completed state, but nothing posts to a channel. A natural follow-up
  if wanted, not done this pass to fit budget.
- Only 3 trackable types (messages/words/voice_minutes) — an
  `xp_earned` type was considered but skipped for v1 since XP is
  granted by `cogs/leveling.py` via the reward engine already, and
  hooking a second listener onto that grant path risked double-
  counting against message/voice XP's own cooldown logic. Can be
  added later as its own listener on `give_reward`'s result if wanted.
- No per-mission max-completions cap beyond one per period_key
  (already enforced structurally — a "once" mission can't be
  re-completed, a "daily" one resets once per day).

## Verified NOT touched this pass (scope discipline)

- No changes to `utils/reward_engine.py`, `utils/trade_engine.py`,
  `cogs/trade.py`, `cogs/minigames.py`, `utils/economy_safe.py`, or
  any Prestige/Leveling file — Missions only reads mission-progress
  state it owns and calls `give_reward()` the same way every other
  reward source already does.
- `dashboard/api.py` deletion — still pending, unchanged. Still needs
  someone with repo access, or the file uploaded here.

## Stopped at: Missions v1 shipped (engine + Discord UI + dashboard CRUD + main.py/permissions.py wiring). Not yet: app.py route + base.html nav link (manual, see MISSIONS_WIRING.md), live verification of Missions OR Trade.

## Still needed, in order:

1. **Apply `MISSIONS_WIRING.md`** — 3 small manual edits to
   `dashboard/app.py` (x2) and `base.html` (x1).
2. **Live verification of Trade** — still outstanding from last pass;
   `/trade`, add coins/diamonds/items both sides, Ready both, confirm
   atomic swap lands and ledger/inventory reflect it.
3. **Live verification of Missions** — send messages / sit in voice,
   confirm progress increments and reward grants on completion; check
   a "daily" mission actually resets the next day (period_key rolls).
4. **Delete `dashboard/api.py`** — still queued since pass #12.
5. **Zero automated test coverage** — still open.

## Design decisions locked (unchanged + new)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: built pass #15. Straight 1:1 swap, no fees, max 10
  distinct items per side. Still unverified live.
- Missions: built this pass (#16), ahead of the original "after Trade
  verified" gate — Dark's explicit override, documented above and in
  `utils/mission_engine.py`. Auto-granted reward on completion, no
  claim step. 3 trackable types: messages, words, voice_minutes.
- Event Stack Builder: max 5 reward slots per event, hard currency
  caps per tier (bronze/silver/gold/diamond), max 3 active events.
- ZIP = source of truth. Always verify against actual code before
  scheduling work.
