# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Phase 5 — XP boost shop items complete, compiled clean.

## This round added
- **database.py** — `leveling_active_boosts` table (guild_id, user_id,
  multiplier, expires_at, source) + `shop_items.xp_boost_multiplier`
  column migration (nullable, only used when type='xp_boost').
- **utils/xp_calculator.py**:
  - `get_active_boost_multiplier(guild_id, user_id)` — MAX across
    non-expired rows, "highest wins" (same rule as bonus roles).
  - `grant_xp_boost(guild_id, user_id, multiplier, duration_hours, source)`.
  - `calculate_message_xp()` now takes optional `user_id`; when given,
    multiplies role_multiplier × boost_multiplier. Voice XP path
    intentionally NOT touched — boosts only apply to message XP, kept
    scope-limited to avoid changing voice XP balance.
- **cogs/leveling.py** — `on_activity_message` now passes `user_id` so
  boosts actually apply.
- **cogs/shop.py** (full file) — new `xp_boost` purchase branch:
  validates multiplier+duration before charging, grants via
  `grant_xp_boost()`, shows boost info in the purchase-success embed,
  `/shop` and `/inventory` both display active/available boosts.
  `temp_role_cleanup` loop now also sweeps expired
  `leveling_active_boosts` rows each tick (10 min).
- **dashboard/api.py** — `add_shop_item` accepts `xp_boost_multiplier`;
  `shop_items_partial` displays it in the items table.
- **dashboard/templates/systems/shop.html** — item type dropdown gets
  "⚡ XP Boost"; form swaps role-ID field for a multiplier field and
  relabels duration when that type is selected; client-side validation
  before POST.

Compile-checked: `python3 -m py_compile database.py utils/xp_calculator.py
utils/reward_engine.py dashboard/api.py cogs/leveling.py cogs/shop.py`
— exit 0.

## Design decisions worth flagging
- Multiple simultaneous boosts don't stack additively — MAX multiplier
  wins, matching the existing bonus-role rule. Buying a second boost
  while one is active doesn't waste the purchase (still grants its own
  row/expiry), it just won't out-multiply a stronger one already active.
- Boosts apply to message XP only, not voice XP (voice XP currently
  has no role-multiplier support either — out of scope to add both).

## Flagged, not fixed (carried over)
- `dashboard/app.py`'s `COMMAND_CATEGORIES["Leveling"]` still doesn't
  list `resetleaderboard`. Cosmetic only (Commands dashboard page);
  command itself works. Deferred again — full `app.py` rewrite not
  justified for a one-line list edit under context pressure.

## Still needed for Phase 5
- Prestige system — blocked on design questions (min level to
  prestige, does XP/rewards reset or carry over, prestige
  roles/badges, leaderboard sort order). Ask before building.

## Files in this ZIP (cumulative — supersedes both prior Phase 5 ZIPs)
- `database.py`
- `utils/xp_calculator.py`
- `utils/reward_engine.py`
- `dashboard/api.py`
- `dashboard/templates/systems/leveling.html`
- `dashboard/templates/systems/shop.html`
- `cogs/leveling.py`
- `cogs/shop.py`
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all other `cogs/*`, `dashboard/*` (except api.py,
leveling.html, shop.html), `utils/*` (except xp_calculator.py,
reward_engine.py), `main.py`, `requirements.txt`, `Dockerfile`,
`start.sh`, `.gitignore`, `DEBUG_GUIDE.md`, `HANDOFF_NOTES.md`.
