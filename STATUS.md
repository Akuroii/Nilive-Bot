# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #15)

## Done this pass: Trade System v1 (unblocked — Dark verified E3/E4 live)

Files in this delta:

- `utils/trade_engine.py` — NEW. Core atomic trade engine.
  - `ensure_trade_table()` — creates `trade_history` (own schema, NOT
    added to `database.py`'s `init_db()` — same standalone pattern
    `cogs/minigames.py` already established for its 3 tables).
  - `execute_trade(guild_id, user_a, offer_a, user_b, offer_b, reason)`
    — validates AND moves both parties' coins/diamonds/items inside
    ONE `BEGIN IMMEDIATE` transaction (re-checks live balances/
    inventory at execution time, not just at UI-build time, since
    Discord button clicks can land minutes later). Either the whole
    swap lands or none of it does. Ledger logging (`source='trade'`,
    `related_user_id` cross-referenced) happens after commit, same
    pattern as `utils/economy_safe.py`'s `_log_ledger`.
  - `get_trade_history(guild_id, user_id=None, limit=50)` — read-only,
    used by `/trade_history`.
- `cogs/trade.py` — NEW. Discord-facing UI.
  - `/trade @member` — opens an interactive embed both parties can
    add to (Offer Coins / Offer Diamonds / Offer Item modals), each
    with a Ready button. Both Ready → `execute_trade()` fires
    atomically. Any offer change after either side is Ready clears
    BOTH Ready states (no last-second bait-and-switch after lock-in).
    10-minute timeout, one open trade per pair at a time (in-memory
    guard, same transient-session pattern as `ButtonRaceView` /
    `MinigameClaimView` — only the FINAL completed trade persists, to
    `trade_history`).
  - `/trade_history [member]` — recent trades for a member, ephemeral.
- `main.py` — added `"cogs.trade"` to `load_cogs()`'s `cog_files` list
  (placed right after `cogs.economy`).

All three `.py` files `py_compile` clean.

## Scope notes (v1, deliberately not built yet)

- No dashboard page for Trade — `/trade_history` command covers
  visibility for now; a read-only dashboard tab (same shape as
  `/ledger`) is a natural follow-up, not done this pass to fit budget.
- Item names are matched exact-string, case-sensitive, typed into a
  modal — no autocomplete/picker yet. Works, but a picker would be a
  nicer UX pass later.
- No trade cap/tax/fee — a straight 1:1 swap of what both sides put in.
- Not verified live yet — same as every other pass, needs a real
  Discord round-trip before being trusted in production.

## Verified NOT touched this pass (scope discipline)

- No changes to `utils/economy_safe.py`, `utils/inventory.py`,
  `utils/ledger.py`, `cogs/minigames.py`, or any Prestige/Leveling
  file — Trade only reads/writes `economy` and `inventory_items`
  directly inside its own atomic transaction, exactly like
  `utils/economy_safe.py::safe_convert()` already does across two
  currency columns on one user; this just extends that same pattern
  across two users and two tables.
- `dashboard/api.py` deletion — still pending, unchanged. Still needs
  someone with repo access, or the file uploaded here.

## Stopped at: Trade System v1 shipped (engine + Discord UI + main.py wiring). Not yet: dashboard page, item picker/autocomplete, live verification.

## Still needed, in order:

1. **Live verification of Trade** — `/trade`, add coins/diamonds/items
   both sides, Ready both, confirm atomic swap lands correctly and
   `transaction_ledger`/`inventory_items` reflect it. Also test the
   failure path (one side's balance changes between Ready clicks).
2. **Trade dashboard page** (optional, not blocking) — read-only
   `trade_history` view, same shape as `/ledger`'s dashboard page.
3. **Delete `dashboard/api.py`** — still queued since pass #12.
4. **Phase 6: Missions** — requires E1+E2, both shipped; next major
   feature after Trade is verified live.
5. **Zero automated test coverage** — still open.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: now built (this pass). Straight 1:1 swap, no fees, no
  cap on offer size beyond `MAX_ITEM_LINES_PER_SIDE=10` distinct items.
- Event Stack Builder: max 5 reward slots per event, hard currency
  caps per tier (bronze/silver/gold/diamond), max 3 active events.
- ZIP = source of truth. Always verify against actual code before
  scheduling work.
