# Leveling_fix — manifest

Base: `Akuroii/Nilive-Bot` `main` @ `d6feb2f`. Extract at the repository root (paths below are repo-relative). Nothing committed or pushed.

| File | Type | Why |
|---|---|---|
| `utils/level_claims.py` | production | `reconcile_role_progression` (rejoin + Claim All) targets the highest **fulfilled** Level reward role. A higher pending/failed Level is never granted and never costs the member the role they already earned. OFF-mode behaviour (incl. the `blocked` flag) is unchanged. |
| `dashboard/api/leveling.py` | production | Reject `xp_min_per_message > xp_max_per_message`; spam threshold floor `1 -> 2` (default stays 10, spam math untouched). |
| `dashboard/templates/systems/leveling.html` | production | `spam_threshold` input `min="1"` -> `min="2"` to mirror the server floor. |
| `scripts/test_xp_safety_audit.py` | test | Two source-text assertions matched the old reconciler lines (`highest = max(by_level)`, `if exclusive_roles and not keep:`); updated to the new lines, same intent. |
| `scripts/test_leveling_voice_hard_blocks.py` | test (new) | Toggle OFF + AFK / server-deaf / alone -> no Voice XP (real ActivityEngine tick -> real Leveling listener), with positive controls. No production change. |
| `scripts/test_leveling_claim_all_d2.py` | test (new) | `_claim_button_available` semantics + full D2 Claim All: restores the missing fulfilled role, removes the old role only after delivery, whole-DB diff proves no claim/XP/level/ledger/economy change, idempotent second pass, OFF accumulates. No production change. |
| `scripts/test_leveling_final_fixes.py` | test (new) | Rejoin highest-fulfilled cases (ON + OFF, pending/failed/partial/none) and the validation rules (min>max, threshold floor 2, custom >= 2 kept, template min=2). |

## Commands run (from repo root) and results

    python scripts/test_leveling_voice_hard_blocks.py       PASS
    python scripts/test_leveling_claim_all_d2.py            PASS
    python scripts/test_leveling_final_fixes.py             PASS
    python scripts/test_xp_safety_audit.py                  PASS (111 passed, 0 failed)
    python scripts/test_level_reward_role_progression.py    PASS (46/0)
    python scripts/test_rejoin_reconciliation.py            PASS
    python scripts/test_leveling_reward_e2e.py              PASS
    python scripts/test_leveling_runtime.py                 PASS
    python scripts/test_slice1_leveling_gate.py             PASS
    python scripts/test_slice2_level_claims.py              PASS
    python scripts/test_slice3_shop_claims.py               PASS
    python scripts/test_slice4_boost_claims.py              PASS
    python scripts/test_leveling_config_migration.py        PASS
    python scripts/test_leveling_reset_config.py            PASS
    python scripts/test_leveling_caller_integrations.py     PASS
    for t in scripts/test_*.py; do python "$t"; done        52 files: 50 pass, 2 fail (pre-existing, see caveats)
    python -c "import compileall..."                        OK

Verified by extracting this ZIP onto a pristine clone of `main` @ `d6feb2f` and re-running the suites there.

Test-strength checks: with the production patch reverted, `test_leveling_final_fixes` fails 5 checks. Voice/D2 tests guard already-correct behaviour, so they were mutation-checked instead: deleting the AFK skip, deleting the deaf skip, forcing `_claim_button_available` to False, and disabling `enforce_role_progression` removal each make the relevant test fail.

## Caveats

- `scripts/test_afk.py` fails (`cancel reports the 2 mentions counted`) and `scripts/test_shop_publications.py` errors (`No module named 'pytest'`, not in requirements.txt). Both fail identically on pristine `main`; unrelated to Leveling.
- Running the suites rewrites tracked `__pycache__/*.pyc` and some `preview_*.png` files in the repo; these are not included here. Discard them with `git checkout -- .` before committing.
- Rejoin with nothing fulfilled and the toggle ON reports `blocked=True` (previous behaviour, unchanged); no code outside tests reads that flag.
