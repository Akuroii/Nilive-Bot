# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: small-task shift complete, well under token limit.

## This shift — 1 task
Added `GET /api/rewards/<user_id>` (dashboard/api.py), ADMIN+ gated.
Reads existing `transaction_ledger` table WHERE guild_id=session guild AND
user_id=<param> AND source IN ('leveling','shop','event','admin'). Did NOT
touch or rebuild `utils/reward_engine.py` — that request was declined and
flagged (see below). guild_id always comes from `get_session_guild_id()`,
never the client — matches every other route in this file.

Returns:
```json
{
  "guild_id": ...,
  "user_id": ...,
  "rewards": [
    {"id":.., "currency":.., "amount":.., "balance_after":.., "type":..,
     "reason":.., "source":.., "related_user_id":.., "reversed":..,
     "reversed_at":.., "created_at":..}, ...
  ]
}
```
`?limit=N` (default 50, capped 200), optional `?date_from=`/`?date_to=`
(YYYY-MM-DD), same pattern as `/api/moderation/export`.

Note on scope: this reflects coins/diamonds/xp reward grants only, since
that's what `transaction_ledger` tracks. Role/temp_role rewards live as
Discord role state (see Shop → Temp Roles); item rewards live in
`inventory_items` (see `/inventory`). Not in scope here — flag if you want
a combined view later.

## Declined this shift — flagged, not built
Prior instruction asked for a new `utils/reward_engine_v2.py` to replace
E2. Declined: `utils/reward_engine.py` (E2) already handles coins,
diamonds, xp, role, temp_role, and item rewards, is guild-isolated, and
already logs to `transaction_ledger` (coins/diamonds/xp) or
`inventory_items` (items) via `economy_safe.py` / `utils/inventory.py`.
Building a parallel engine would fork the reward path that
`cogs/leveling.py`, `cogs/shop.py`, and `cogs/events.py` all call into —
directly against "AKUROI VERIFIED BYTE-IDENTICAL" and the standing
"E2: verify exists, DO NOT REBUILD" rule. Also declined a `reward_queue`
table (no current consumer) and a `missions.py` hook (missions.py doesn't
exist yet — that's Phase 6, New Major Engines, out of order).

## E2 Reward Engine — VERIFIED, NOT REBUILT (again)
- `utils/reward_engine.py` — `give_reward()` handles:
  - `coins` / `diamonds` → routed through `economy_safe.safe_credit` /
    `safe_deduct`, ledgered automatically
  - `xp` → direct `levels` table update + `_log_xp_ledger()` +
    level-up detection → `check_and_award_level_rewards()`
  - `role` / `temp_role` → `check_bot_role_position()` guard, then
    `add_roles()`, temp roles written to `temp_roles` with expiry
  - `item` → routed through `utils/inventory.give_item()`
- Callers already using it: `cogs/leveling.py` (message + voice XP),
  `cogs/shop.py` (purchases), `cogs/events.py` (button-race rewards)
- No changes made to this file this shift.

## E1 / E3 / E4 — unchanged, still verified from last shift
- E1 `cogs/activity_engine.py` — verified complete, untouched.
- E3 `utils/ledger.py` + `transaction_ledger` — complete, untouched.
- E4 `utils/inventory.py` + `inventory_items` — complete, untouched.
- Dashboard `/ledger` and `/inventory` pages + sidebar nav links —
  present and wired in this ZIP (base.html already has both links).

## Confirmed done + compiled clean in THIS zip
- dashboard/api.py — added `/api/rewards/<user_id>`, verified
  `python3 -m py_compile` exit 0. Rest of file is byte-identical to
  last known-good (previous shift's `/api/activity/<user_id>` addition
  is preserved unchanged).

## NOT included in this zip (unchanged — pull from your last full ZIP)
Everything else. This was a 1-file-change shift by design:
- database.py, main.py, dashboard/app.py, dashboard/auth.py,
  dashboard/permissions.py, dashboard/utils/async_utils.py
- utils/* (reward_engine.py, ledger.py, inventory.py, economy_safe.py,
  permissions.py, xp_calculator.py, formatters.py)
- cogs/* (all cogs, including activity_engine.py — untouched)
- dashboard/templates/* — no changes
- dashboard/static/*
- requirements.txt, Dockerfile, start.sh, .gitignore

## Phase 3 status
E1 ✅ E2 ✅ E3 ✅ E4 ✅ — all four shared engines verified complete and
wired into their consuming cogs/dashboard routes. Phase 3 (Shared
Engines) is done per Master Plan v2.0.

## Next up — Phase 5 (Extend Systems)
Per Master Plan v2.0 locked order: Phase 4 (Foundation Repairs / legacy
P1 #10-17) is already complete. Trade System (Phase 6) stays BLOCKED
until explicitly scoped — E3+E4 being live satisfies its prerequisite,
but trade/escrow itself hasn't been designed yet and shouldn't be
started speculatively. Next session should confirm with Akuroi which
Phase 5 system to extend first before writing any code.
