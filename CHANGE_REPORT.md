# CHANGE_REPORT — Prestige I free purchase price

Result: **PASS**
Starting commit: `721262a0bc7ceba4cf2e352e8566c7c8af7c1c43` (main, "Revert Add files via upload (#134) (#135)")
Resulting commit: none — changes are uncommitted in the working tree (no commit, push, merge or deploy was made). Patch is against the starting SHA.

## Decision applied (option B)
Prestige I costs 0 Coins / 0 Diamonds, but the existing I–V Prestige reset (Coins -> 0) is unchanged. `purchase_prestige()` was NOT modified.

## Root cause
Only the Dashboard Shop form rejected Prestige I at price 0, in `dashboard/templates/systems/shop.html`:
1. Submit check `if (!freeVI && (!payload.price || payload.price <= 0))` blocked every non-VI Prestige at 0, including I (create and edit share this path).
2. `#item-price` had `min = '1'` for every non-VI Prestige tier.

Layers traced and found already correct (no change): `utils/shop_validation.py` (`prestige_terms`: I 0..max, II–V >=1, VI exactly 0), `/api/shop/item` POST/PUT, SQLite storage/retrieval, `Free` display (`/shop`, `/api/shop/items`), `cogs/shop.py` purchase + confirm (confirm text already says Coins reset to 0), `purchase_prestige()` (transaction, history, ledger, rollback).

## Files changed
- `dashboard/templates/systems/shop.html` — Prestige I accepts a typed `0` (blank/non-digit/negative/decimal still rejected: `priceValue === null`); price input `min` is `0` for tier I, `1` for II–V; II–V check unchanged; VI lock unchanged. +12/-2 lines.
- `scripts/test_prestige_one_free_form.js` (new) — real-template JS harness.
- `scripts/test_prestige_one_free.py` (new) — API/DB/display/purchase tests.
- Confirmation wording: not changed (already accurately says Coins reset to 0).
- Stale root-level copies (`/shop.html`, `/shop.py`, `/prestige.py`, …) are not used by the app and were left untouched.

## Behavior before / after
- Before: Dashboard refused Prestige I at 0 (toast "Prestige items need a Coins price"); admins could not create/edit it from the UI.
- After: typed 0 accepted for I; blank still refused; II–V still refuse 0/blank/negative; VI unchanged.

## Tests (run from repo root, venv with requirements.txt installed; exit codes captured directly)
| Command | Exit | Result |
|---|---|---|
| `python scripts/test_free_shop_items.py` | 0 | Ran 24 tests, OK |
| `python scripts/test_prestige_one_free.py` (new) | 0 | Ran 11 tests, OK |
| `node scripts/test_prestige_one_free_form.js` (new) | 0 | PASS |
| `python scripts/test_prestige.py` | 0 | 74 passed, 0 failed |
| `python scripts/test_phase1_prestige.py` | 0 | Ran 37, OK |
| `python scripts/test_prestige_rank_and_item_identity.py` | 0 | Ran 24, OK |
| `python scripts/test_vi_shop_api.py` | 0 | Ran 6, OK |
| `PYTHONPATH=. python tests/test_prestige_phase2.py` | 0 | Ran 19, OK |
| `node scripts/test_vi_shop_form.js` | 0 | PASS |
| `node scripts/test_vi_legacy_edit_form.js` | 0 | 29 passed, 0 failed |
| `node scripts/test_dashboard_streak_prestige_mvp.js` | 0 | 20 JS checks passed |
| `python scripts/test_shop_product_management.py` | 0 | Ran 50, OK |
| `python scripts/test_dashboard_page_scripts.py` | 0 | 148 passed, 0 failed |
| `node scripts/test_dashboard_frontend_regressions.js` | 0 | All checks passed |

Regression proof: `test_prestige_one_free_form.js` FAILS on the unpatched template (Prestige I typed 0 not sent) and passes with the fix. The Python tests pass before and after, confirming backend layers were already correct. Baseline of all suites was green before the change.

Fact 1 (costs nothing): empty-wallet buyer takes I; Diamonds unchanged; `price_paid` 0.
Fact 2 (still resets): 500 Coins -> 0, Diamonds/XP/Level untouched, tier set; II keeps its reset and minimum balance.
Also covered: duplicate purchase refused with no state change; forced mid-transaction failure rolls back fully and retry succeeds.

## Notes
- Existing behavior left as is: `purchase_history.price_paid` records the pre-reset balance for I–V (tier I with a non-empty wallet records that amount). Flag if you want it to show 0 for I.
- Repo tracks `__pycache__/*.pyc`; test runs modify them. They were restored and are not in the ZIP.
- Dependencies installed in a throwaway venv; `requirements.txt` untouched. `AGENTS.md` and `CLAUDE.md` remain byte-identical.
