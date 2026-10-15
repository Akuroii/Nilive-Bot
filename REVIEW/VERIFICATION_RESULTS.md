# Test results (run by the reviewer in throwaway worktrees; no test or script was modified)

Environment: Python 3.12.3, discord.py 2.7.1, aiosqlite 0.19.0, fake Discord objects, real DB code.
Unpatched control: pristine `99851bf`. Patched: `99851bf` + `REVIEW/P1_expired_processing.diff`.
The 15-check probe is a reviewer scratch script and is intentionally NOT in this ZIP.

## 15-check P-1 probe
| Tree | Result |
|---|---|
| Unpatched `99851bf` | 10/15 (5 failures = exactly the defect) |
| Patched | **15/15** |

Checks (all pass patched): stale-lease claim claimable; active (5 s) lease NOT claimable; NULL `processing_started_at`
claimable; pending and failed claimable; fulfilled not; Stats "Waiting" counts the stale claim; level-page footer offers it;
active lease not counted/offered; stale `booster:` row locked for a non-booster and claimable for a verified booster;
engine still refuses to reserve it for the non-booster (balance unchanged); stale claim fulfilled and credited exactly once;
two concurrent `claim_available` on one stale claim -> one winner, one credit; original dead `owner_token` cannot finalize after takeover.

## Existing suites (unmodified), unpatched vs patched: identical, all exit 0, 0 failures
| Suite | Result |
|---|---|
| test_booster_role_strip | 25 pass |
| test_booster_rewards | 41 pass |
| test_xp_safety_audit | 124 passed, 0 failed |
| test_level_reward_role_progression | 46 passed, 0 failed |
| test_rejoin_reconciliation | 24 pass |
| test_leveling_claim_all_d2 | 25 pass |
| test_leveling_runtime | 25 pass |
| test_leveling_reward_e2e | 11 pass |
| test_leveling_caller_integrations | 11 pass |
| test_slice2_level_claims | 26 pass |
| test_slice3_shop_claims | 21 pass |
| test_slice4_boost_claims | 15 pass |
| test_leveling_final_fixes | 21 pass |
| test_level_card_visuals | 52 pass |

## B5/P1 regression
Reviewer scratch harness: 19/19, plus 2 Exclusive-ON shared-role cases, on the patched tree.

## Not verified
Real Discord (button/embed rendering, permissions), Python 3.11, Railway runtime.
