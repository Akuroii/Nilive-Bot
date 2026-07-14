# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 5 — Leveling expansion (currency level rewards) complete.

## Scope this shift
User-specified scope only: level-up rewards via the existing Reward
Engine (no rebuild) — coins and/or diamonds per level, guild-isolated,
ledger-logged. Role rewards at configured levels already existed
(leveling_rewards + check_and_award_level_rewards) and were verified
working, not rebuilt.

## What was built
- **database.py** — new table `leveling_currency_rewards`
  (guild_id, level, currency, amount). Kept separate from
  `leveling_rewards` since a level can carry a role reward AND a
  currency reward, and `leveling_rewards.role_id` is NOT NULL so it
  can't represent a currency-only row.
- **utils/xp_calculator.py** — new
  `check_and_award_level_currency_rewards(bot, member, guild_id,
  old_level, new_level)`. Reads all currency rewards for levels in
  `(old_level, new_level]`, grants each via
  `utils.economy_safe.safe_credit()` — same atomic/ledgered path as
  every other coin/diamond grant in the project (shop, /give, events).
  No new logging code needed; safe_credit already writes the
  transaction_ledger row with source='leveling'.
- **utils/reward_engine.py** — `give_reward()`'s `xp` branch now calls
  the new function right alongside the existing
  `check_and_award_level_rewards()` role-grant call, same trigger
  point (real level-up), same resolved guild/member.
- **dashboard/api.py** — 3 new ADMIN+ routes, guild-scoped via
  `get_session_guild_id()` like every other route in the file:
  - `GET /api/leveling/currency-rewards`
  - `POST /api/leveling/currency-reward`
  - `DELETE /api/leveling/currency-reward/<id>`
- **dashboard/templates/systems/leveling.html** — Rewards tab now has
  a second add-form + table for currency rewards, alongside the
  existing role-reward form/table. New JS: `loadCurrencyRewards()`,
  `addCurrencyReward()`, `deleteCurrencyReward()`.

## Verified, not touched
- `leveling_rewards` (role rewards) — confirmed working, left as-is.
- `cogs/leveling.py` — no changes needed; the xp reward path already
  routes through `reward_engine.give_reward()` for both message and
  voice XP, so currency rewards fire automatically with zero cog
  changes.
- E1–E4 (activity engine, reward engine, ledger, inventory) — all
  confirmed present and wired from the uploaded ZIP; not rebuilt.
- Economy v2 (dual currency, exchange rate) — confirmed present; not
  rebuilt.

Compile-checked: `python3 -m py_compile database.py utils/xp_calculator.py
utils/reward_engine.py dashboard/api.py` — exit 0.

## Flagged, not fixed (out of scope this shift)
- `/resetxp` is listed in `dashboard/app.py`'s `COMMAND_CATEGORIES`
  (Leveling section) but no such command exists in `cogs/leveling.py`.
  Dead entry in the Commands dashboard page until either the command
  is built or the entry is removed.

## Still needed for Phase 5 (not started, awaiting direction)
- Weekly/monthly leaderboard resets
- XP boost items (shop-integrated)
- Prestige system
- `/resetxp` command (see flag above)

## Files in this ZIP
- `database.py`
- `utils/xp_calculator.py`
- `utils/reward_engine.py`
- `dashboard/api.py`
- `dashboard/templates/systems/leveling.html`
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all other `cogs/*`, `dashboard/*` (except api.py and
leveling.html), `utils/*` (except xp_calculator.py, reward_engine.py),
`main.py`, `requirements.txt`, `Dockerfile`, `start.sh`, `.gitignore`,
`DEBUG_GUIDE.md`, `HANDOFF_NOTES.md`.
