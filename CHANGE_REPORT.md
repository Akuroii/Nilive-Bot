# CHANGE_REPORT — Prestige I free purchase (price, form, receipt)

Result: **PASS**
Starting commit: `721262a0bc7ceba4cf2e352e8566c7c8af7c1c43` (main, "Revert Add files via upload (#134) (#135)")
Resulting commit: none — changes are uncommitted in the working tree (nothing committed, pushed, merged or deployed). Patch is against the starting SHA.

## Decisions applied
- Prestige I costs 0 Coins / 0 Diamonds **and its history receipt records `price_paid = 0`**.
- The existing I–V Coins reset (Coins -> 0) is unchanged; Prestige II–V history semantics unchanged (`price_paid` = pre-reset balance); VI unchanged.

## Root causes
1. **Dashboard form** (`dashboard/templates/systems/shop.html`) rejected Prestige I at price 0: submit check `!payload.price || payload.price <= 0` and `min="1"` on the price input applied to every non-VI Prestige tier.
2. **Receipt** (`utils/prestige.py::purchase_prestige`) wrote `price_paid = old_balance` for every I–V purchase, so a free Prestige I showed the wiped Coins as its price.

Layers traced and already correct (no change): `utils/shop_validation.py::prestige_terms`, `/api/shop/item` POST/PUT, SQLite storage/retrieval, `Free` listing display, `cogs/shop.py` purchase + confirm text, reset/transaction/rollback logic.

## `price_paid` trace (done before changing it)
Writers: `utils/prestige.py` (I–V: the line changed; VI activation writes 0), `cogs/shop.py::_record_purchase` (non-Prestige items), schema `database.py` (`INTEGER NOT NULL`, unchanged).
Readers: `dashboard/api/economy_shop.py` purchase-history table (`Free` when 0), `dashboard/app.py` member snapshot (display), `utils/timed_ownership.py` (diagnostic evidence dict only), `scripts/verify_runtime_path.py` (script).
Not used for: the reset (uses `economy.balance`/`old_balance`), rollback (SQL transaction), tier progression/idempotency (`levels.prestige` check), VI reconciliation (`prestige_vi_activations`), ledger (written separately as `-old_balance`, so the Coins wipe stays auditable). Conclusion: informational-only column; safe to change for tier I.

## Files changed
- `dashboard/templates/systems/shop.html` — Prestige I accepts a typed `0` (blank/junk/negative/decimal still rejected); price `min` is 0 for I, 1 for II–V.
- `utils/prestige.py` — `price_paid = 0 if target_tier == 1 else old_balance` (7 added lines incl. comment). No change to reset, ledger, return value or schema.
- `scripts/test_phase1_prestige.py` — one assertion updated: tiers I–V receipts were `[9000]*5`, now `[0] + [9000]*4` (it encoded the old I behavior).
- `scripts/test_prestige_one_free.py` (new, 13 tests) and `scripts/test_prestige_one_free_form.js` (new).
Stale root-level copies of repo files are unused by the app and untouched. No schema change.

## Behavior before / after
- Form: refused Prestige I at 0 -> accepts typed 0; blank still refused.
- Receipt/history: Prestige I recorded 500 (the wiped balance) -> `0`, shown as `Free`; II–V unchanged (e.g. 35); Coins still reset to 0 for I–V.

## Tests (repo root, venv from requirements.txt; exit codes captured)
| Command | Exit | Result |
|---|---|---|
| `python scripts/test_free_shop_items.py` | 0 | 24 tests OK |
| `python scripts/test_prestige_one_free.py` (new) | 0 | 13 tests OK |
| `node scripts/test_prestige_one_free_form.js` (new) | 0 | PASS |
| `python scripts/test_prestige.py` | 0 | 74 passed, 0 failed |
| `python scripts/test_phase1_prestige.py` | 0 | 37 OK |
| `python scripts/test_prestige_rank_and_item_identity.py` | 0 | 24 OK |
| `python scripts/test_vi_shop_api.py` | 0 | 6 OK |
| `python scripts/test_vi_lifecycle.py` | 0 | 38 OK |
| `python scripts/test_phase2_backend.py` | 0 | 39 OK |
| `PYTHONPATH=. python tests/test_prestige_phase2.py` | 0 | 19 OK |
| `python tests/test_f01_f03_role_reconciliation_and_purchase.py` | 0 | 13 OK |
| `node scripts/test_vi_shop_form.js` | 0 | PASS |
| `node scripts/test_vi_legacy_edit_form.js` | 0 | 29 passed, 0 failed |
| `node scripts/test_dashboard_streak_prestige_mvp.js` | 0 | 20 checks passed |
| `python scripts/test_shop_product_management.py` | 0 | 50 OK |
| `python scripts/test_dashboard_page_scripts.py` | 0 | 148 passed, 0 failed |
| `node scripts/test_dashboard_frontend_regressions.js` | 0 | passed |

Regression proof: with the `utils/prestige.py` change reverted, the two new receipt/display tests FAIL; with it they pass. The form test fails on the unpatched template and passes patched. Baseline of all suites was green before any change.

## Notes / flagged, not changed
- `prestige_terms` still allows a positive price for Prestige I (0..max) — existing validator behavior I was told to keep. Tier I receipts record 0 regardless of its listing's minimum balance. If I must be strictly price 0, say so; that's a validator change.
- Purchase-history "expires" column shows `—` for any 0-price row (existing generic rule), so Prestige I shows `—` instead of `Permanent`.
- Historical Prestige I receipts already stored with the old amount are not rewritten.
- Repo tracks `__pycache__/*.pyc`; test runs modify them; restored, not in the ZIP. `requirements.txt` untouched; deps were in a throwaway venv.
