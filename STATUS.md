# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Economy v2 (dual convertible currency) — core feature complete.

## This shift — Phase 5, Economy v2 (Option B: Convertible, 500:1 default, per-guild configurable)

Dual currency already existed at the data layer (economy.balance + economy.diamonds,
economy_safe.py already took a `currency` param). This shift wires the actual
feature Dark specced on top of that: conversion, diamond-priced shop items, admin
diamond grants, and dashboard visibility — not a rebuild of E2/E3.

### Database (database.py) — additive migrations only, no existing column touched
- `guild_settings.diamond_exchange_rate` (INTEGER DEFAULT 500) — per-guild rate
- `shop_items.price_diamonds` (INTEGER DEFAULT NULL) — nullable; item is
  diamond-priced when set, otherwise unchanged coins pricing
- `purchase_history.currency_paid` (TEXT DEFAULT 'balance') — records which
  currency a purchase was actually paid in, since price_paid alone became
  ambiguous once diamond pricing exists

### utils/economy_safe.py
- Added `safe_convert(guild_id, user_id, coin_amount, rate, ...)` — atomic
  (BEGIN IMMEDIATE) coins→diamonds conversion. Floors to the nearest multiple
  of `rate` so leftover coins that don't divide evenly aren't lost.
- Added `get_guild_exchange_rate(guild_id)` helper (falls back to 500)

### utils/ledger.py
- Added `convert_in` / `convert_out` to VALID_TYPES so conversions show up
  distinctly in the Ledger page instead of looking like a generic credit/deduct

### cogs/economy.py
- `/convert <coins>` — user-facing conversion command, reads the guild's
  configured rate
- `/adddiamonds` / `/removediamonds` (admin) — mirrors addcoins/removecoins,
  routed through safe_credit/safe_admin_deduct so they're ledgered identically
- `/balance` now shows both coins and diamonds

### cogs/shop.py
- `process_purchase()` now checks `price_diamonds` on the item; if set, charges
  diamonds instead of coins (currency-aware safe_deduct call, currency-aware
  insufficient-balance message)
- `/shop` listing shows 💎 price for diamond-priced items
- purchase_history rows now record `currency_paid`

### Dashboard
- `/economy` page: added Diamonds leaderboard tab + Exchange Rate config tab
  (coins-per-1-diamond, editable, ADMIN+)
- `/shop` page: add-item form has an optional "Price (diamonds)" field;
  item list shows 💎 or 🪙 price appropriately
- `dashboard/api.py`: added `GET/POST /api/economy/exchange-rate` (ADMIN+),
  `GET /api/economy/leaderboard-diamonds` (ADMIN+); `/api/shop/item` POST and
  `/api/shop/items` GET updated for `price_diamonds`; `/api/shop/purchase-history`
  now returns `currency_paid` and shows the right icon

### Compiled clean in this ZIP
`python3 -m py_compile` exit 0 on: database.py, utils/ledger.py,
utils/economy_safe.py, cogs/economy.py, cogs/shop.py, dashboard/app.py,
dashboard/api.py.

## NOT included in this zip (unchanged — pull from your last full ZIP)
Everything not listed above. Per-file shift pattern as usual:
- main.py, dashboard/auth.py, dashboard/permissions.py,
  dashboard/utils/async_utils.py
- utils/reward_engine.py, utils/inventory.py, utils/permissions.py,
  utils/xp_calculator.py, utils/formatters.py
- All other cogs/* (leveling, moderation, tickets, reactionroles, etc. —
  untouched)
- dashboard/templates/* other than systems/shop.html and systems/economy.html
  (base.html sidebar already has Ledger/Inventory links from last shift, no
  change needed here)
- dashboard/static/*
- requirements.txt, Dockerfile, start.sh, .gitignore

## Still needed / explicitly out of scope this shift
- Missions/quests that reward diamonds directly (Phase 6, New Major Engines —
  not started, per locked phase order)
- No new slash-command help text / `/economy_help` — not asked for
- No rate-change audit trail beyond the existing audit_log entry written on
  every `/api/economy/exchange-rate` save (that part IS done — flagging what's
  NOT extra)
- Trade System (Phase 6) remains BLOCKED — unaffected by this shift, dual
  currency doesn't change its prerequisites

## Phase 5 status
Economy v2 (dual convertible currency): ✅ core feature shipped this shift.
Other Phase 5 candidates (Leveling expansion, Shop expansion beyond diamond
pricing, Events expansion, Tickets overhaul) — not started, awaiting Dark's
next pick.

## Next up
Confirm with Dark: is Economy v2 considered feature-complete as shipped, or
are there specific follow-ups (e.g. a `/richest diamonds` command, missions
hook, or admin bulk-convert)? Otherwise, pick the next Phase 5 system per the
original menu (Leveling / Shop / Events / Tickets).
