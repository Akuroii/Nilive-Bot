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

---

# Round 4 — the B1 fence regression, its rc=1, and the fix

Head: `5ea9791` (`arena/077d2ec7-nilive-bot`), on top of `bc4e28c`. Base `main`
is `02fbfee`. PR #98 stays open; nothing merged, nothing deployed.

## Why `test_correction_lease_fence.py` returned rc=1

Two independent causes. **Neither is a product defect** — no production file was
changed in this round (`git diff bc4e28c --stat` is one test file).

**1. The environment had no dependencies.** The sandbox had no packages
installed, so the script died at import:

```
ModuleNotFoundError: No module named 'aiosqlite'   (database.py line 1)
```

Zero assertions ran; `main()` was never reached. After
`pip install -r requirements.txt` the script passes. This is the most likely
cause of the reported exit code, since rc=1 with no `[FAIL]` line can only come
from an exception before the first `check()`.

**2. Case E had a genuinely nondeterministic assumption.** It `asyncio.gather`s
a correction and an award with no synchronisation and branches on the outcome,
but `final_xp == xp_at(12)` is reachable by **two legal interleavings**:

* **(a)** the award lands inside the sweep, so the sweep must keep the original
  row and the role;
* **(b)** the award is **DEFERRED**, the correction finishes legitimately at
  Level 11, and the deferred award is **retried afterwards** (`give_reward`
  defaults `defer_retry_seconds` to 3.0 s). The original row is correctly gone
  and `record_crossing` has opened a **fresh, claimable** row.

The assertion only described (a). An instrumented timeline of a failing
iteration (patching `utils.level_ops.{xp_write_barrier,fence_live_writers,
acquire_lock,release_lock}`, every production call site uses `ops.<name>`):

```
 209.395ms CORRECTION-START
 234.776ms LEASE-ACQUIRE   setlevel-8a0ef761e11545da
 241.283ms CORRECTION-FENCE  deferred_count=0
 246.901ms AWARD-BARRIER   live_lease=setlevel-8a0ef761e11545da -> DEFERRED
 273.699ms LEASE-RELEASE
 273.736ms CORRECTION-END  new_level=11
 754.248ms AWARD-BARRIER   live_lease=None -> ALLOWED
 763.047ms AWARD-END       success, new_level=12
```

The fence worked: the first attempt was refused while the lease was live, and
the retry landed only after the sweep had released. That is branch (b), and it
is correct behaviour — the member ends at Level 12 with a fresh `pending` row
they can claim. Measured frequency: **5 of 30 iterations unloaded, 1 of 3 under
CPU contention**.

## The fix

Assert the invariant the file exists to prove, which holds in **both**
branches: *the member is never left at an eligible level holding neither the
reward nor a claim that can restore it*. Not a weakening — the vulnerable
baseline fails it:

| | `a8ef28f` (vulnerable) | `bc4e28c`+ (fixed) |
|---|---|---|
| final level | 12 | 12 |
| role worn | no | no |
| `level_reward_claims` rows | **`[]` — empty** | `[(2, 12, role, pending)]` |

The baseline deletes the row the re-cross opened, leaving Level 12 with no role
and no claim: permanent loss. The fixed head keeps a recoverable row.

Sensitivity after the change went **up**, not down: the corrected file reports
**41 failed checks on `a8ef28f`** versus 39 before, and **0 on the fixed head**.

Added for coverage the audit found missing:

* **case M — cancellation.** `CancelledError` is a `BaseException`, so it passes
  through the `except Exception` that turns a reconciliation failure into a
  report, and reaches the `finally` that releases the lease. Locks: the lease is
  released, the reward stays recoverable, ordinary XP writes resume, and a retry
  still completes the correction.
* **case N — lease expiry.** A lease past its TTL defers no writer, and a sweep
  that has lost its authority deletes no claim.
* **case A** gained one deterministic assertion (gated by `allow`, so no timing
  can reorder it) that the re-cross reopens a claimable Level 12 row.

67 -> 82 checks.

## Verification

| suite | `a8ef28f` | fixed head |
|---|---|---|
| `test_correction_lease_fence.py` | n/a (file added at `bc4e28c`); copied in: **rc=1, 27 pass / 41 fail** | **rc=0, 82 pass / 0 fail** |
| `test_inflight_role_correction.py` | rc=0, 134/134 | rc=0, 134/134 |
| `test_level_ops_provenance.py` | rc=0, 70 | rc=0, 70 |
| `test_xp_rollback.py` | rc=0, 59 | rc=0, 59 |
| `test_level_test_reset.py` | rc=0, 87 | rc=0, 87 |
| `test_rejoin_reconciliation.py` | rc=0, 24 | rc=0, 24 |
| `test_setlevel_admin.py` | rc=0, 38 | rc=0, 38 |
| `test_level_card_fixes.py` (dashboard) | rc=0, 60 | rc=0, 60 |
| `test_leveling_runtime.py` / `_contract` | rc=0, 26 / 31 | rc=0, 26 / 31 |
| `test_slice2/3/4_*_claims.py` | rc=0, 26 / 21 / 15 | rc=0, 26 / 21 / 15 |
| `test_shop_publications.py` (pytest) | **23 passed** | **23 passed** |
| `npm test` | 17/17 harnesses | 17/17 harnesses |

**Full Python suite, same runner** (`/tmp/run_suite.sh`: every
`scripts/test_*.py` in its own process, 240 s timeout, same venv):

* `a8ef28f` — 64 scripts, **60 exit 0, 4 exit 1**
* fixed head — 65 scripts, **61 exit 0, 4 exit 1**

The 4 failures are **identical on both** and identical on `main` (`02fbfee`),
so none is introduced by this work:

1. `test_afk.py` — 169/170 (one pre-existing check).
2. `test_leveling_voice_hard_blocks.py` — the same 9 "guard OFF" checks.
3. `test_phase2_backend.py` — `TimeoutError` in
   `test_single_canonical_reset_loop_runs_once_after_ready_and_cancels`
   (a 5 s `wait_for`; this sandbox has 2 CPUs).
4. `test_shop_publications.py` — a **pytest module**, not a standalone script;
   executed directly it fails on `import pytest`. Under pytest it is 23/23.

**Flakiness proof.** Case E re-sampled 120 times under 8 busy-loops on 2 CPUs
with the corrected assertion: **120/120 pass**, with the AWARD-WON branch
exercised 52 times. The whole fence file: rc=0 on 5 consecutive unloaded runs.

**Note on running these scripts.** They need `pip install -r requirements.txt`
(`aiosqlite` is imported by `database.py` at module scope) and, for the pytest
modules, `OWNER_ID` set — `database.py` calls `sys.exit(1)` without it.

## The B1 invariant

An XP award arriving during a level correction cannot leave the member at an
eligible level with both the reward and its recoverable claim missing. Verified
across: the correction lease (A, G), ordinary XP writers including message,
voice and mission awards (A, J), the dashboard member edit (B), one-shot awards
that wait rather than lose the reward (L), an award committing between the XP
write and the fence (K), the final claim-deletion transaction under lease
supersession (G), lease expiry (N), exceptions and interrupted removal (F),
cancellation (M), and retry/idempotency (F, J).

All of this is single-process SQLite with fake Discord objects. It says nothing
about live Discord role/permission/429 behaviour or Railway.

## Remaining risks

* Case E is still an **unsynchronised race**; the assertion now holds in both
  outcomes, but which outcome occurs remains timing-dependent. The
  deterministic guards for the same invariant are cases A, C, K, M and N.
* `test_phase2_backend.py`'s reset-loop test is timing-sensitive and fails in a
  2-CPU sandbox; it fails identically on `main`, so it is not a regression here,
  but it is also not proven by this run.
* Multi-process SQLite contention is still unverified (see the round-3 note).
* PR #98's head branch is `arena/adb559e5-nilive-bot`; this round was committed
  to the session branch `arena/077d2ec7-nilive-bot` from the exact PR head
  `bc4e28c`. PR #98 has not been retargeted or merged.

---

# Round 5 — 2026-10-20

Base for this round: PR head `29af13c` (round 4). Committed to the session
branch `arena/077d2ec7-nilive-bot`. `main` (`02fbfee`) untouched; PR #98 open,
not marked ready, nothing merged or deployed.

Akuroi's round-4 review held `29af13c` with four open items. This round
addresses **#1 (one-shot XP reward permanently lost)** and **#4 (case E oracle
is wrong)**. Items **#2 (P1, lease expiry leaves a role permanently missing)**
and **#3 (P2, currency reversal leaves claim and balance inconsistent) are NOT
implemented** — see "Not done" below. This is stated rather than glossed: the
PR is not ready and is not claimed to be.

## #1 — One-shot XP reward permanently lost (FIXED)

**Root cause.** `give_reward()`'s wait is a fixed `wait_for = 3.0` s. A
correction lease has a 90 s TTL. When the lease outlived the wait, `give_reward`
returned a *deferred failure* and wrote nothing. Every one-shot caller had
already committed the work that earned the reward, so nothing ever retried it:

* `mission_engine.record_activities` sets `completed=1` and commits **before**
  the grant, so re-recording the activity produces no second completion.
* `cogs/events.py` inserts the `event_winners` row and commits **before** the
  grant, and a second claim is refused with "You already claimed this reward".

A fixed timeout cannot fix this: leases expire, renew and can outlive any
assumed number.

**Fix — make the award durable and idempotent rather than patient.**

* `utils/level_ops.py` — new table `level_xp_pending_grants` (UNIQUE
  `idem_key`) plus `enqueue_xp_grant()` / `grant_row()` / `drain_xp_grants()` /
  `drain_xp_grants_sweep()`. Each grant applies in its own `BEGIN IMMEDIATE`
  that opens with `xp_write_barrier`, so a grant can never land inside a fence;
  the XP write, `record_crossing` and `status='applied'` commit together, which
  is what makes it exactly-once.
* `utils/reward_engine.py` — `give_reward(..., idempotency_key=None)`. With a
  key the award is recorded *before* it is attempted and retried; without one,
  behaviour is unchanged. Recurring message/voice XP must NOT pass a key — the
  next tick earns it again, so queueing it would only build a backlog.
* `utils/mission_engine.py` — passes `mission:{guild}:{user}:{def_id}:{period}`
  and drains at the very top of `record_activities`, **before** the
  `if not amounts: return []` early exit. That placement is required: the
  earning mission is already `completed`, so its own grant path never runs
  again and only a fresh activity can settle it.
* `cogs/events.py`, `cogs/tagmissions.py`, `cogs/tagpartners.py`,
  `utils/minigame_engine.py` — each now passes an idempotency key
  (`event:`, `tag_mission:`, `tag_partner:`, `minigame:` + `log_id`).
  `cogs/events.py` has a thin local `give_reward` wrapper; the key is forwarded
  through it rather than added to the wrapper's callers.
* `scheduler.py` — the 60 s loop calls `drain_xp_grants_sweep()`, so a quiet
  member and anything left pending by a restart is also settled.

**Wording.** `xp_write_barrier` no longer promises a retry ("the XP award was
deferred and nothing was written"). `give_reward`'s non-keyed deferral tail
distinguishes recurring XP ("the next message or voice tick earns this XP
again") from one-shot ("should be retried with idempotency_key to be safe").
Only a genuinely queued award says it will be applied automatically.

**Regression** — `scripts/test_one_shot_xp_durability.py` (new, 19 checks,
exit 0). Holds a *fenced* lease (a bare `acquire_lock` does not stop an ordinary
writer) and asserts: the mission completes while fenced; XP is not written; the
award is queued `pending`; re-recording does not re-complete or double-pay;
after release the next activity applies it exactly once; the row is `applied`;
further activity never duplicates it. Also covers the quiet-member/sweep path and
an event winner's keyed award.

## #4 — Case E oracle was wrong (FIXED)

Two independent defects, both confirmed:

1. **It accepted `fulfilled` as recoverable.** Claim All re-pays only
   `pending`/`failed`/`processing`; `reconcile_role_progression` restores only
   `fulfilled` rows and deliberately refuses one whose reversal is
   `applied`/`already_reversed`. So a `fulfilled` claim behind a terminal
   reversal is unrecoverable by *every* production path — which is exactly what
   P1 produces. The new `role_recoverable()` helper encodes both paths.
2. **It never discriminated.** The old case raced a correction and an award with
   `asyncio.gather` and asserted on whichever branch the scheduler produced;
   40/40 runs landed in the Level-11 branch, so the discriminating assertion
   never executed and the vulnerable implementation passed.

**Fix.** Case E is split into two deterministic interleavings, each driven by
hooking a boundary instead of hoping for one:

* `e_award_wins` — hooks `ops.fence_live_writers`, the exact boundary between
  "awards may still land" and "awards are refused", so the award commits on the
  winning side every run.
* `e_correction_wins` — injects the award from inside the sweep's role removal,
  i.e. after the fence closed, so it is deferred; the correction wins, and the
  retried award must reopen a claimable row.

On a pre-fence baseline (which has no `fence_live_writers`) the first hook
degrades to injecting at the role removal, so the sensitivity run reports failed
checks instead of crashing.

**Discrimination verified.**

| | fixed head | `a8ef28f` |
|---|---|---|
| `E-award-wins` | 7/7 PASS | 4 FAIL |
| `E-correction-wins` | 7/7 PASS | 2 FAIL |

The failing checks on `a8ef28f` are the ones that matter:
`E-award-wins: a production path can still deliver or restore this reward — the
claim row is gone; nothing can restore from it`, and
`E-correction-wins: ... no Level-12 claim row exists - the re-cross opened
nothing` (the audit probe's Level 12 with an empty `level_reward_claims` table).
The old oracle reported PASS for the first of these.

Cases A, B, C, D, F, G, H, I, J, K, L, M, N are preserved unchanged.

## Not done — #2 (P1) and #3 (P2)

Both remain open and both are real. Root causes from the round-4 reproductions:

* **P1** — lease expiry inside the Discord `remove_roles` await of
  `_reverse_correction_claim` (`utils/level_claims.py:1885`). The role comes off,
  authority is lost before DB cleanup, and the result is `claim=fulfilled` +
  journal `applied`: `reconcile_role_progression` skips that pair and
  `outstanding_reversals` is empty, so nothing restores it. The claim loop
  re-checks the lease only at the top of each iteration, which does not cover
  time spent awaiting Discord.
* **P2** — the currency branch of `_reverse_correction_claim`
  (`utils/level_claims.py:1921–1969`) debits first and appends to
  `deleted_claims` on `applied`/`already_reversed`; at expiry the final guard
  reports the reward kept while the wallet is already debited. It does not
  reproduce with a live fenced lease and all writers normal — it is a specific
  expiry/finalization interleaving.

Neither is addressed by this commit. Fixing P1 needs lease renewal across the
Discord await plus a post-side-effect recovery path that leaves an *open* case
rather than a terminal one; that is a change to the correction's authority
handling, not a local patch, and it was not going to be done carefully in the
budget left in this round. It is listed here so the gap is explicit rather than
implied.

`probe2_lease_expiry_role.py` and `probe3_currency_expiry.py` remain ad-hoc
probes for this reason: promoted as rc=0 regressions they would fail, and a
regression that fails by design is worse than an honest gap.

## Commands and results

Runner for the suite comparison (one runner, one environment, both trees):

```
OWNER_ID=1 SECRET_KEY=test DISCORD_TOKEN=test NERO_ENVIRONMENT=test \
  python /home/user/scratch/run_suite.py <repo-root>
```

| suite | result |
|---|---|
| `scripts/test_correction_lease_fence.py` | 14 cases, all PASS (exit 0) |
| `scripts/test_one_shot_xp_durability.py` | 19 checks, all PASS (exit 0) |
| `scripts/test_inflight_role_correction.py` | all PASS (exit 0) |
| `scripts/test_xp_safety_audit.py` | 121 passed, 0 failed |
| `scripts/test_leveling_caller_integrations.py` | all PASS |
| full Python suite, fixed head | 65 scripts: **62 OK, 3 BAD** |
| full Python suite, `a8ef28f` | 65 scripts: 61 OK, 4 BAD |
| `pytest scripts/test_shop_publications.py` | 23 passed |
| `npm test` | 17 harnesses, 148/148 checks |

The 3 BAD on the fixed head are pre-existing and fail identically on `a8ef28f`:

* `test_afk.py` — 169/170, one legacy-snapshot check.
* `test_leveling_voice_hard_blocks.py` — 9 voice-guard checks.
* `test_phase2_backend.py` — `CancelledError`/`TimeoutError` in the reset-loop
  test; timing-sensitive on a 2-CPU box.

The 4th baseline failure (`test_shop_publications.py`) passes once `OWNER_ID`,
`SECRET_KEY`, `DISCORD_TOKEN` and `NERO_ENVIRONMENT` are exported; it was an
environment gap, not a defect.

Two failures were introduced and fixed during this round:

* `test_leveling_caller_integrations.py` — `TypeError: give_reward() got an
  unexpected keyword argument 'source'`, because `cogs/events.py` has its own
  thin `give_reward` wrapper. Fixed by forwarding `idempotency_key` through the
  wrapper.
* `test_xp_safety_audit.py` — `drain_xp_grants() is classified INSERT INTO
  levels is not in the audit inventory`. Correctly caught: the new function
  writes `levels`. Added to both `LEVEL_WRITERS` and `LEVEL_CONSISTENCY`
  (it uses `xp_progress(new_xp)` in the same transaction as the write).

## Remaining risks

* P1 and P2 are open, as above.
* Multi-process SQLite contention is still unverified; everything here is
  single-process with fake Discord objects. Nothing in this round speaks to live
  Discord role/permission/429 behaviour or to Railway.
* The sweep's lease is checked at the top of each claim iteration only. On a
  slow worker with many claims, a correction spanning more than the 90 s TTL can
  still lose authority mid-pass; that is P1's mechanism and is unfixed.
