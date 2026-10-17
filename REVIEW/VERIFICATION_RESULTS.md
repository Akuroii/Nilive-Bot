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

---

# PR #97 follow-up — correction-engine verification (2026-10-17)

No test file, fixture, helper or assertion was edited. The 100→10 harness below is a
scratch script outside the repository (`/tmp/verify_correction.py`); it is not shipped
and is not part of the suite.

## Full suite, 63 scripts (`scripts/test_*.py`, this branch)

57 exit 0, 6 exit 1. Every failure is pre-existing or a recorded divergence:

| Suite | This branch | Baseline (`02fbfee`, separate worktree) |
|---|---|---|
| test_afk | 169/170 | 169/170 (same failure) |
| test_leveling_voice_hard_blocks | 9 guard checks fail | same 9 |
| test_phase2_backend | exit 1 | exit 1 |
| test_shop_publications | pytest collection `INTERNALERROR` | same |
| test_xp_safety_audit | 115 passed, 3 failed | 108 passed, **same 3** |
| test_setlevel_admin | 8 checks fail (§6) | passes (asserts the old policy) |

`test_setlevel_admin` §6 asserts that a downward `/setlevel` must delete nothing and
leave above-level rewards claimable. That is the behaviour this PR is required to
replace, so the divergence is deliberate; its eight check names are listed in the PR
report. The three `test_xp_safety_audit` failures are static checks about the older
reset/reconciler helpers and fail identically on the base commit.

All other leveling suites pass, including `test_xp_rollback`,
`test_level_ops_provenance`, `test_level_test_reset`, `test_level_card_fixes`,
`test_level_card_visuals`, `test_leveling_reward_e2e`,
`test_leveling_claim_all_d2`, `test_slice1..4`, `test_booster_rewards`,
`test_level_reward_role_progression`, `test_rejoin_reconciliation` and the wallet
suites.

## 100→10 scenario (`/tmp/verify_correction.py`, all checks pass)

Real `_apply_admin_xp`, `claim_available`, `level_ops` and `inventory` helpers on a
throwaway database; only Discord objects are doubles.

* 100→10 reversal of 3 currency rewards, 2 inventory rewards (exact attributable
  units; an unrelated quest stack is preserved), 2 reward roles and 1 boost.
* Unclaimed admin-created rows above 10 stay in the ledger, are reported as dormant,
  and pay nothing from Claim All or `/level`. Claim All at Level 10 pays only rows
  at or below 10.
* Re-progression to 12 pays each dormant row exactly once (the row is reused, never
  duplicated).
* A role also justified by another temp-role grant is kept and reported.
* Spent currency: wallet is not forced negative; the case is recorded `outstanding`.
* An elapsed boost: claim stays terminal; case recorded `outstanding`.
* A lease held by a claim pass refuses the correction without changing XP.
* Simulated crash mid-reversal: reported in `correction["error"]`, XP correction
  stands, nothing above the level is claimable, re-running at the same level
  completes the sweep and refunds exactly once.
* Provenance write failure aborts the inventory removal / absolute quantity write.

## Missing test coverage (documented, not added)

No automated suite covers the production 100→10 path end to end:
`test_setlevel_admin` still encodes the old policy, and the harness above is not a
repo test. Scenarios without a committed test: dormant-row repayment after legitimate
re-progression, crash/retry idempotency at the command layer, concurrent claim pass
vs. correction, and role-kept-because-otherwise-granted.

## Not verified

Real Discord (live role add/remove, button/embed rendering), Railway deployment, and
million-scale timing. Role removal is a Discord API call outside SQLite and is
therefore reported per role, not claimed as atomic.
