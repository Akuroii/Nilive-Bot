# Test baseline

Recorded on `4f6d8f2716c7b0ea83fdebf0c2c4d03da0f8b2d5` (the commit these results
were measured against), from a clean clone with:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

Nothing in this file is a result of the current change: these files were already
red (or already silent) before it. A failure that is **not** in this file is a new
regression. Re-run the tables below with the exact commands shown; the per-file
exit codes are the baseline to compare against.

## How the matrices are run

| matrix | command | count |
| --- | --- | --- |
| root (pytest collection) | `PYTHONPATH=. python -m pytest -q <file>` for every root `test_*.py` | 41 files |
| root (script mode) | `PYTHONPATH=. python <file>` for every root `test_*.py` | 41 files |
| `scripts/` Python | `PYTHONPATH=. python scripts/<file>` | 72 files |
| `scripts/` Node | `node scripts/<file>` (or `npm test`, which runs all of them) | 18 files |

`PYTHONPATH=.` and a working directory of the repo root are required; the root
copies of the `scripts/` suites resolve `__file__` two levels up and therefore
fail from the repo root (see "Root duplicates" below).

## Totals on the baseline

| matrix | passing |
| --- | --- |
| root, pytest collection | 11 / 41 |
| root, `python <file>` | 27 / 41 |
| `scripts/`, Python | 68 / 72 |
| `scripts/`, Node | 18 / 18 |

## `scripts/` Python — the 4 baseline failures

| file | exit |
| --- | --- |
| `test_afk.py` | 1 |
| `test_booster_rewards.py` | 1 |
| `test_leveling_voice_hard_blocks.py` | 1 |
| `test_phase2_backend.py` | 1 |

Details:

* `test_afk.py` — `169/170 passed`: one pre-existing check fails in the voice-AFK
  rewrite; unrelated to ownership/rollback.
* `test_booster_rewards.py` — exits 0 while printing
  `AttributeError: 'types.SimpleNamespace' object has no attribute 'followup'`
  (its `claim_all` path calls `interaction.followup`, which the harness's stub
  does not provide). A *noisy pass*, not a gated failure.
* `test_leveling_voice_hard_blocks.py` — 9 `VOICE GUARD` checks fail with the
  guard OFF (legacy behaviour not preserved).
* `test_phase2_backend.py` — fails on its own assertions in this environment.

## Root `test_*.py` — baseline exit codes under pytest collection

`5` means "no tests collected": those files are script-style harnesses with a
`main()`, so pytest finds nothing to run. They are expected to be `5` here and
are covered by the script-mode matrix instead.

| file | exit |
| --- | --- |
| `test_afk.py` | 5 |
| `test_afk_dashboard.py` | 5 |
| `test_booster_rewards.py` | 5 |
| `test_check_emoji.py` | 5 |
| `test_currency_icon.py` | 5 |
| `test_free_shop_items.py` | 0 |
| `test_heartbeat.py` | 5 |
| `test_inventory_ui.py` | 5 |
| `test_lock.py` | 5 |
| `test_minigame_api.py` | 5 |
| `test_minigame_engines.py` | 1 |
| `test_minigame_migration.py` | 5 |
| `test_minigame_spawn.py` | 1 |
| `test_minigame_store.py` | 1 |
| `test_missions_v2.py` | 5 |
| `test_phase1_api.py` | 0 |
| `test_phase1_prestige.py` | 0 |
| `test_phase1_runtime.py` | 1 |
| `test_phase2_backend.py` | 1 |
| `test_prestige.py` | 5 |
| `test_prestige_api.py` | 5 |
| `test_purge.py` | 5 |
| `test_rank_integration.py` | 5 |
| `test_shop_categories_api.py` | 0 |
| `test_shop_designs_api.py` | 0 |
| `test_shop_json_body_api.py` | 0 |
| `test_shop_options.py` | 1 |
| `test_shop_publications.py` | 0 |
| `test_shop_publisher.py` | 5 |
| `test_shop_publisher_api.py` | 1 |
| `test_shop_select_dispatch.py` | 0 |
| `test_slice1_leveling_gate.py` | 5 |
| `test_slice2_level_claims.py` | 5 |
| `test_slice3_shop_claims.py` | 5 |
| `test_slice4_boost_claims.py` | 5 |
| `test_timed_ownership.py` | 0 |
| `test_tree_error.py` | 5 |
| `test_vi_lifecycle.py` | 0 |
| `test_vi_shop_api.py` | 0 |
| `test_wallet.py` | 5 |
| `test_wallet_phase2.py` | 5 |

## Root `test_*.py` — baseline exit codes in script mode (`python <file>`)

| file | exit |
| --- | --- |
| `test_afk.py` | 1 |
| `test_afk_dashboard.py` | 1 |
| `test_booster_rewards.py` | 1 |
| `test_check_emoji.py` | 1 |
| `test_currency_icon.py` | 1 |
| `test_free_shop_items.py` | 0 |
| `test_heartbeat.py` | 1 |
| `test_inventory_ui.py` | 0 |
| `test_lock.py` | 0 |
| `test_minigame_api.py` | 0 |
| `test_minigame_engines.py` | 0 |
| `test_minigame_migration.py` | 0 |
| `test_minigame_spawn.py` | 0 |
| `test_minigame_store.py` | 0 |
| `test_missions_v2.py` | 0 |
| `test_phase1_api.py` | 0 |
| `test_phase1_prestige.py` | 0 |
| `test_phase1_runtime.py` | 1 |
| `test_phase2_backend.py` | 1 |
| `test_prestige.py` | 1 |
| `test_prestige_api.py` | 0 |
| `test_purge.py` | 0 |
| `test_rank_integration.py` | 0 |
| `test_shop_categories_api.py` | 0 |
| `test_shop_designs_api.py` | 0 |
| `test_shop_json_body_api.py` | 0 |
| `test_shop_options.py` | 1 |
| `test_shop_publications.py` | 0 |
| `test_shop_publisher.py` | 1 |
| `test_shop_publisher_api.py` | 1 |
| `test_shop_select_dispatch.py` | 0 |
| `test_slice1_leveling_gate.py` | 0 |
| `test_slice2_level_claims.py` | 0 |
| `test_slice3_shop_claims.py` | 1 |
| `test_slice4_boost_claims.py` | 1 |
| `test_timed_ownership.py` | 0 |
| `test_tree_error.py` | 0 |
| `test_vi_lifecycle.py` | 0 |
| `test_vi_shop_api.py` | 0 |
| `test_wallet.py` | 0 |
| `test_wallet_phase2.py` | 0 |

Root-only failures worth knowing about, because they are environmental rather
than behavioural: `test_afk.py`, `test_afk_dashboard.py`, `test_check_emoji.py`,
`test_currency_icon.py`, `test_heartbeat.py`, `test_prestige.py` raise
`FileNotFoundError` for `/tmp/cogs/...` — they compute the repo root as the
parent of their own directory, which is correct only from `scripts/`. The real
suites are the `scripts/` copies.

## `scripts/` Node — 18 / 18 passing

`npm test` runs `scripts/run_js_tests.sh`, which also `node --check`s every
`dashboard/static/js/*.js`. Baseline: no failures.

## Root duplicates

40 root `test_*.py` files have a same-named `scripts/` counterpart. On this
baseline 32 are byte-identical and 8 differ — and in every differing pair the
`scripts/` copy is the newer one:

| file | root last touched | `scripts/` last touched |
| --- | --- | --- |
| `test_booster_rewards.py` | 2026-10-14 `6419ade` | 2026-10-20 `b883bd4` |
| `test_shop_designs_api.py` | 2026-10-04 `9ec4ef0` | 2026-10-09 `a277537` |
| `test_shop_publisher.py` | 2026-10-04 `9ec4ef0` | 2026-10-23 `d527858` |
| `test_shop_publisher_api.py` | 2026-10-04 `9ec4ef0` | newer |
| `test_slice1_leveling_gate.py` | 2026-10-04 `9ec4ef0` | newer |
| `test_slice2_level_claims.py` | 2026-10-04 `9ec4ef0` | newer |
| `test_slice3_shop_claims.py` | 2026-10-04 `9ec4ef0` | 2026-10-21 `33039ef` |
| `test_slice4_boost_claims.py` | 2026-10-04 `9ec4ef0` | newer |

Docstrings and module references point at the `scripts/` paths
(`python scripts/test_booster_rewards.py`, `utils/shop_publisher.py`), and the
root copies cannot even locate the repo when run from the root. **The
`scripts/` copies are authoritative; the root copies are stale duplicates.** They
are left in place here — deleting 40 files is not part of this change — but a
follow-up should remove the root copies (or the 8 stale ones first).
