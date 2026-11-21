# Batch 1 manifest (deletion only, NOT committed)

- Base: `main` @ `5a9eec491ac70790c077a4702981a0befdc5b371` (recovery point: unchanged clone `Nilive-Bot`, plus pristine export)
- Working branch: `cleanup/batch1-pyc-and-stray-init` (local, staged, not committed)
- Patch: `batch1-deletions.patch`  sha256 `6b11e90aacf9b34780eef07f276b6ab2872d137d8e6c643a48e1ef5d07a6fc4d`  (apply with `git apply batch1-deletions.patch` or `git am`-free `git apply --index`)
- `git diff --check`: clean (exit 0). Changed paths: exactly 4, all status `D`. Unstaged/untracked: 0.

| Path | Bytes | Blob | SHA-256 | Reason |
|---|---|---|---|---|
| `__pycache__/database.cpython-312.pyc` | 51067 | `d97ab77f27` | `fed195baccece16b…` | bytecode for py3.12; Docker runs python:3.11-slim; .gitignore already lists __pycache__/ and *.pyc; no code references it |
| `__pycache__/main.cpython-312.pyc` | 14811 | `fd2198febe` | `5c0fb1bf0f436e3e…` | same |
| `dashboard/__pycache__/app.cpython-312.pyc` | 98314 | `61deb746de` | `eb36dfa0a8d9733b…` | same |
| `__init__ (1).py` | 0 | `e69de29bb2` | `e3b0c44298fc1c14…` | 0-byte upload artifact (space+(1) in name is not importable); zero importers; only mentioned in legacy README_INSTALL.txt and an old patch text (both on HOLD) |

## Verification (pristine 5a9eec4 export vs cleanup, same harness, same env)

- 123 checks each: tests/ + scripts/ py (118), JS suite, import database/main/dashboard.app, syntax of all .py (388 vs 387 files).
- Result per check identical in both trees: 116 pass, 7 fail, **0 new failures, 0 fixed**.

| Failure (both trees) | Class | Evidence |
|---|---|---|
| scripts/test_afk.py | pre-existing | 169/170, same check |
| scripts/test_free_shop_items.py | pre-existing | 3 failures |
| scripts/test_vi_shop_api.py | pre-existing | 5 failures |
| scripts/test_phase2_backend.py | pre-existing | 1 failure |
| scripts/test_leveling_voice_hard_blocks.py | pre-existing | 9 'guard OFF' checks |
| scripts/test_channel_grouping_acceptance.py | harness artifact | fails with 403 only when OWNER_ID env is set; passes in both trees without it |
| scripts/run_js_tests.sh (test_moderation_smooth_navigation.js) | environmental | needs Playwright Chromium, not installed; same in both |

Not verified: Railway/production, real Discord, browser flows (no Playwright browser).
