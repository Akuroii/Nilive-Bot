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

Method: each script runs in its own process from the repository root with
`OWNER_ID=1 PYTHONPATH=.`; exit status is the result. At code head `31d2fcc`
(the documentation commit that follows changes no code) that is **57 exit 0,
6 exit 1**. The base commit `02fbfee` (58 scripts;
five were added by this PR) is **54 exit 0, 4 exit 1**, and every base failure is
still a failure at the head:

| Suite | Code head `31d2fcc` | Base `02fbfee` | Classification |
|---|---|---|---|
| test_afk | 169/170 | 169/170 (same failure) | pre-existing |
| test_leveling_voice_hard_blocks | 9 guard checks fail | same 9 | pre-existing |
| test_phase2_backend | exit 1 | exit 1 | pre-existing |
| test_xp_safety_audit | 113 passed, 5 failed | 108 passed, 3 failed | 3 pre-existing static checks; 2 additional static grep checks at this head |
| test_setlevel_admin | 8 checks fail (§6) | suite added by this PR | PR-added test asserting the old policy |
| test_slice2_level_claims | 1 check fails | passes | check encodes the old dashboard clamp — see below |

`test_shop_publications` is a pytest module: executed directly it is a silent no-op
(exit 0, no checks), so the loop above proves nothing about it. Run with
`python -m pytest -q scripts/test_shop_publications.py` it is **23 passed**, stable
across repeated runs, at both the base and this head.

`test_setlevel_admin` §6 asserts that a downward `/setlevel` must delete nothing and
leave above-level rewards claimable. That is the behaviour this PR is required to
replace, so the divergence is deliberate; its eight check names are listed in the PR
report. `test_slice2_level_claims`'s "negative Dashboard member edit clamps existing
XP" check asserts that the dashboard may lower XP to 0 through
`/api/edit-member`; that route now refuses every decrease because it cannot run the
reward correction. The check pre-exists on the base commit (where it passes) and
encodes the old unsafe behaviour, so it is a deliberate divergence, not a regression
in the correction engine. The five `test_xp_safety_audit` failures are static source
checks: three fail identically on the base commit and two more are grep-based checks
that fail at this head because `utils/level_test_reset.py` mentions
`reconcile_role_progression`; none was changed by this round.

All other leveling suites pass, including `test_xp_rollback`,
`test_level_ops_provenance`, `test_level_test_reset`, `test_level_card_fixes`,
`test_level_card_visuals`, `test_leveling_reward_e2e`,
`test_leveling_claim_all_d2`, `test_slice1`, `test_slice3`, `test_slice4`,
`test_booster_rewards`, `test_level_reward_role_progression`,
`test_rejoin_reconciliation` and the wallet suites.

## Fix round: `/xp_rollback` role safety and the dashboard decrease gate

No test file was edited. `utils/level_test_reset.py::rollback_xp_operation` now:

* classifies every delivered role through the same
  `utils.level_claims._role_granted_elsewhere` predicate the correction engine uses,
  before the plan is built, and moves roles another recorded entitlement still
  grants into `plan["roles_left_on_member"]` instead of `remove_discord_roles`;
* journals one applied role reversal per claim behind each removed role and adds
  those claim ids to the same `delete_ids` transaction as the XP write, so a
  removed role's fulfilled claim cannot be restored by a rejoin's
  `reconcile_role_progression()`;
* reports retained roles in `/xp_rollback`'s preview and result, and names the
  competing grant in `claims_preserved`.

`utils/level_claims.py::reconcile_role_progression` additionally skips a role claim
whose journal already says `applied`/`already_reversed`, so a crash between the
Discord removal and the claim delete cannot be undone by a rejoin.

`dashboard/app.py::api_edit_member` reads the member's XP inside its
`BEGIN IMMEDIATE` transaction and, when the request would lower it, rolls back and
returns HTTP 409 with a message directing the administrator to `/setlevel` or
`/setxp`. Nothing is written: no XP, wallet, claim or mutation row. XP increases
are unchanged. The member-profile page shows the server's explanation.

Scratch harness `/tmp/verify_fixes.py` (not a repo test), all checks pass:
role removed **and** its claim deleted, rejoin restores nothing; a role also granted
by a `temp_roles` row is kept and reported; the crash state (role gone, journal
applied, claim row left) is not restored by a rejoin and a re-run finishes it; a
temp-role reward is removed with its own inventory marker excluded; the dashboard
decrease returns 409 and changes nothing, while an increase still succeeds.

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
vs. correction, role-kept-because-otherwise-granted, `/xp_rollback` deleting a
reversed role's claim, and the dashboard's refusal of an XP decrease.

## Not verified

Real Discord (live role add/remove, button/embed rendering), Railway deployment, and
million-scale timing. Role removal is a Discord API call outside SQLite and is
therefore reported per role, not claimed as atomic.


---

# Closure round — head on top of `b6b0dfa` (2026-10-17)

Current branch: `arena/adb559e5-nilive-bot`. No test file, fixture, helper or assertion
was edited. Scratch harnesses are outside the repository and are listed below; they
are not repo tests.

## Fixes in this round

1. **Dashboard members edit no longer clears diamonds when the field is absent.**
   `dashboard/app.py::api_edit_member` now distinguishes an absent `diamonds` key
   (preserve the balance read inside the same `BEGIN IMMEDIATE` transaction) from an
   explicit value (including `0`, which still overwrites). The members modal sends
   only `{user_id, xp, coins}`, so it now preserves diamonds; the member-profile page
   still sends all three fields and behaves as before. The audit row records the
   effective value rather than a defaulted `0`.
2. **Claim All renders a blocked correction lease as a refusal, not an empty pass.**
   `claim_result_footer` appends a specific Arabic blocked message when
   `claim_available` returns `blocked`; `_run_claim_pass` disables Claim All (and the
   Restore Level Role button when present) on that card. Eligibility and the lease
   mechanism are unchanged.
3. **Resolved reversals leave the open queue.**
   `utils/level_ops.py::outstanding_reversals` excludes `resolved:<decision>` rows as
   well as `applied`. The journal rows remain intact. `/level_reconcile` now refuses
   a second resolution of the same case as "No open case", and a new case is still
   listed.
4. **Rejoin withholds a role whose reversal is still open.**
   `utils/level_claims.py::reconcile_role_progression` treats `outstanding`/`refused`
   role cases as open. It suppresses restoration only when **every** fulfilled claim
   for that role is in the open set **and** `_role_granted_elsewhere` finds no other
   recorded grant (another claim, a `temp_roles` unit, or an inventory role marker).
   The suppression is reported in `result["withheld"]` and logged by
   `on_member_join`; it is not silent. Closed (`resolved:*`) cases do not suppress
   restoration.

## Verification (real DB/code, fake Discord objects)

* `fix1_diamonds.py`: absent diamonds → `(1200, 7)` and audit `7->7`; explicit `3`
  → `(1300, 3)`; explicit `0` → `(1400, 0)`; a decrease still returns 409 and writes
  nothing. All checks pass.
* `fix2_blocked_response.py`: with a correction lease, the exact footer is
  `تم الاستلام: 0. إخفاقات قابلة لإعادة المحاولة: 0. ⏸️ عملية تصحيح XP قيد التنفيذ لهذا العضو — لم يُستلم أي شيء. أعد المحاولة بعد انتهائها.`,
  Claim All is disabled, nothing is paid; an ordinary empty pass is unchanged; after
  the lease ends the same card delivers `1`. All checks pass.
* `fix3_resolved_queue.py`: a resolved case disappears from
  `outstanding_reversals`, the journal row remains, a second `/level_reconcile`
  resolution is refused, and a new case is still listed. All checks pass.
* `fix4_rejoin_withheld.py`: an open-case-only role is withheld; an independent
  fulfilled claim or `temp_roles` grant still restores it; a resolved case restores
  normally. All checks pass.
* `fix4_rollback_rejoin.py`: after a failed `/xp_rollback` role removal, a real
  `on_member_join` call withholds the role (`withheld` logged) and keeps the claim;
  after resolution a rejoin still does not restore it. All checks pass.
* `fix2_rollback_repro.py`, `fix2_command_repro.py`, `audit_100_to_10.py`,
  `audit_failures.py`, `audit_race.py`, `fix1_legacy_repro.py`, `fix1_below.py`,
  `interactions.py`, `role_fail_claim.py`, `dup_claims.py`, `level_restore_buttons.py`
  all pass after the changes (the rejoin expectation in `fix2_rollback_repro.py` was
  updated to the new withheld behaviour).

## Full suite, 63 scripts

Re-run at code head `31d2fcc` after the four fixes: **57 exit 0, 6 exit 1** (base `02fbfee`:
54/58, four failures). The failing set is the four base failures plus the two
PR-authored checks that encode the old policy (`test_setlevel_admin` §6,
`test_slice2_level_claims`'s dashboard clamp); details and the exact check names are
in the table above. No failure was introduced by this round: `test_afk`,
`test_leveling_voice_hard_blocks`, `test_phase2_backend`,
`test_xp_safety_audit`, `test_setlevel_admin` and `test_slice2_level_claims` fail
identically at `b6b0dfa`, before these four fixes.

Targeted suites that pass: `test_xp_rollback`, `test_level_ops_provenance`,
`test_rejoin_reconciliation`, `test_leveling_claim_all_d2`, `test_leveling_reward_e2e`,
`test_level_test_reset`, `test_leveling_runtime`, `test_leveling_runtime_contract`,
`test_level_card_fixes`, `test_level_card_visuals`, `test_booster_rewards`,
`test_leveling_caller_integrations`, `test_level_reward_role_progression`,
`test_dashboard_page_scripts`.

## Remaining findings / unverified behavior

* The member-facing card does not display `withheld`; only the join log and the open
  `/level_reconcile` case show it. (A resolved case can no longer be re-resolved,
  directly or through the command - see the discovery-race fix below.)
* An expired `LevelRewardView` (180 s, non-persistent, no `on_timeout`) still becomes
  inert; Discord shows "This interaction failed". No repo test covers expiry.
* A duplicate dashboard save still creates a second `xp_mutations` row with a
  zero delta (no value corruption).
* Not verified: real Discord role add/remove/permissions and 429 handling, Railway,
  multi-process SQLite contention, browser-executed CSRF, million-scale timing.
  The Flask route was exercised through the real test client with an
  `X-CSRF-Token` header; the views/buttons use fake Discord objects.


---

# Discovery-race fix - `/level_reconcile` (2026-10-18)

The closure audit found one reproducible correctness blocker: two administrators
could act on the same open-case listing, and the losing attempt could delete the
claim behind `drop_claim` after the winner had recorded `value_kept`, overwriting
the decision and making the reward claimable again. This round fixes that race
only. No test file, fixture or assertion was edited.

## Change (2 files, +120/-49)

* `utils/level_ops.py::resolve_outstanding` is now a **one-way transition**. The
  read-then-write of the case status became a single guarded `UPDATE ... WHERE
  status != 'applied' AND status NOT LIKE 'resolved:%'`, so only a still-open case
  matches; a retry, a stale listing or a concurrent administrator updates zero rows
  and leaves the first decision intact. The function reports whether it closed the
  case, and accepts an optional `db` so a caller can run the decision inside its own
  transaction.
* `cogs/leveling.py::level_reconcile` decides the case **first**, inside the same
  `BEGIN IMMEDIATE` transaction that performs the `drop_claim` deletion, and deletes
  nothing unless the decision was won. The decision and the deletion therefore
  commit together or not at all: a failed deletion rolls the decision back and the
  case stays open and retryable. Losing attempts reply with `case_closed_reply()`
  ("already resolved ... nothing was deleted ... the recorded decision is
  unchanged") and are logged as a failed `level_ops` row; a fresh listing that no
  longer contains the case says the same thing in the existing "No open case" reply.
  The `drop_claim`-refused path (`value_kept`) resolves first and no longer reports
  a recorded decision when it did not record one.

Behaviour change worth noting: an unmigrated `temp_roles` (no `claim_id` column)
used to leave the claim deletion rolled back but still resolved the case. Now the
whole action rolls back and is reported, which is the safe direction - the case
stays open and the reward cannot be re-earned while its claim survives.

## Verification (scratch harnesses, real command callbacks and real SQLite)

* `fix5_reconcile_race.py` - 34 checks, ALL PASS:
  * the exact audit race (A `value_kept`, B stale `effect_gone` + `drop_claim`):
    B is refused with the already-resolved reply, A's decision and note remain,
    B's note never reaches the journal, the claim survives, one journal row, the
    losing attempt is a failed operation;
  * a direct second `resolve_outstanding` returns False and overwrites nothing;
  * ordinary resolution, `drop_claim` + `effect_gone` (claim deleted, operation
    `completed`), repeat resolution, `drop_claim` + `value_kept` (claim kept);
  * an injected deletion failure (a `BEFORE DELETE` trigger on
    `level_reward_claims`): the command reports the failure, the case is still
    `outstanding`, no claim is deleted, the operation is `failed`, and the retry
    succeeds.
* `fix5_reconcile_concurrent.py` - 8 rounds of two commands started with
  `asyncio.gather`, ALL PASS. Both outcomes were observed (7 `value_kept` wins,
  1 `effect_gone` win); every round had exactly one decision, exactly one refusal,
  a claim state consistent with the winning decision, and no loser note.
* `fix5_100_to_10.py` - the acceptance scenario re-run through the real `/setxp`
  and `/setlevel`: 100 -> claim L10-L13 -> down to 10. L11 coins taken back, L10
  kept, L12 role and call removed, L13 boost removed, L40/L50 kept and dormant,
  Claim All pays nothing, repeating the correction moves nothing, re-progress to 12
  pays L11 once and restores L12, reaching 40 pays the kept row once, no negative
  wallet. ALL PASS.
* `fix3_resolved_queue.py` (rebuilt) - 10 checks, ALL PASS.
* `fix5_rejoin_after_resolve.py` - 5 checks, ALL PASS: an open case still withholds
  the role on rejoin, and a decided case restores it from the fulfilled claim.

## Full suite, 63 scripts

**57 exit 0, 6 exit 1** - identical to the pre-fix head `21afce1` and to
`b6b0dfa` before it; the failing scripts and their checks are unchanged
(`test_afk` 169/170, voice 9 guard checks, `test_phase2_backend` `errors=1`,
`test_xp_safety_audit` 113/5, `test_setlevel_admin` 8 old-policy checks,
`test_slice2_level_claims` 1 old-policy check). Targeted suites:
`test_level_ops_provenance` 69 checks, `test_xp_rollback` 59,
`test_level_test_reset` 87, `test_rejoin_reconciliation` 24,
`test_leveling_claim_all_d2` 25, `test_leveling_reward_e2e` 11 - all pass.
`test_shop_publications` under pytest: 23 passed.

## Remaining limitations

* The fix serialises decisions through SQLite's write lock; it is verified in one
  process and with concurrent asyncio tasks, not with two separate OS processes
  hitting the same database file. `BEGIN IMMEDIATE` plus the guarded `UPDATE` is the
  same mechanism the rest of the level engine already relies on for that case.
* A closed case still cannot be re-opened; a decision that later proves wrong is
  corrected by reviewing the member's rewards, exactly as the new reply says.
* Existing unverified areas are unchanged: real Discord role/permission/429
  handling, Railway, browser CSRF, million-scale timing.
