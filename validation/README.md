# Validation — candidate `8e409f8bfca73df839a28e1420c95242279a351d`

Branch `candidate/2026-10-31-chain-design-c`, based on
`f9e413f72a026ac09e33104f809ea2345753eee7` + Design C + Decision E + R2.

## What this is, and what it is not

**This is not the lost `run_matrix_v2.sh`, and it does not claim the old
116-gate matrix passed.** That runner is gone and its results are not
reproducible. This is a **new** list, rebuilt from two honest sources:

1. the surviving evidence of what Design C itself was validated with
   (`review-materials/clean-3768d96-tests.log`), and
2. the suites this candidate actually changes.

Every item runs in a **clean worktree checked out at exactly the ref under
test**, so a result describes that SHA and nothing else. Every row records
its own exit code, duration and last output line, so any row can be re-run
by hand. **The exit code is the only verdict: PASS iff exit 0.**

## How to reproduce

```bash
# against the candidate
./validation/run_validation.sh 8e409f8bfca73df839a28e1420c95242279a351d out.tsv

# against main, to see what is inherited
./validation/run_validation.sh a6b812be26d35f3048db39d0425374d23cd804ea out.tsv
```

`VALIDATION_PYTHON` selects the interpreter (default `python3`). Each run
writes `<name>.tsv` plus a `<name>-logs/` directory of raw output.

## Result

| ref | PASS | FAIL | SKIP |
|---|---|---|---|
| candidate `8e409f8` | **16** | **0** | 0 |
| main `a6b812be` | 10 | 0 | 6 |

The 6 SKIPs on `main` are the items that simply do not exist at that ref —
they are the Design C suites, the R2 suite and the two reproducers. They are
recorded as SKIP, not silently dropped, so the comparison cannot hide them.

**Zero regressions.** All 10 items present on *both* refs pass on *both*
refs. There are no inherited failures inside this list, so nothing green on
`main` turned red here, and nothing red on `main` is being relabelled as new.

## Per-item detail

| item | candidate | main | note |
|---|---|---|---|
| design-c-purchase-grant-ledger | PASS 29 passed | SKIP | new file from Design C |
| design-c-timed-ownership | PASS 241 passed, 5 subtests | SKIP | new file from Design C |
| design-c-delivery-safety | PASS 18 passed, 14 subtests | SKIP | new file from Design C |
| r2-role-retention | PASS 25 passed | SKIP | new in this candidate |
| r1-inventory-ambiguity | PASS exit 0 | SKIP | reproducer, Design C doc |
| r2-booster-obligation-gap | PASS exit 0 | SKIP | reproducer, Design C doc |
| scripts-level-ops-provenance | PASS | PASS | inherited |
| scripts-level-test-reset | PASS | PASS | inherited — this candidate changes the reader it exercises |
| scripts-xp-rollback | PASS | PASS | inherited |
| scripts-currency-reversal-consistency | PASS | PASS | inherited |
| scripts-correction-lease-fence | PASS | PASS | inherited |
| scripts-inflight-role-correct | PASS | PASS | inherited |
| scripts-lease-expiry-role-recovery | PASS | PASS | inherited |
| scripts-one-shot-xp-durability | PASS | PASS | inherited |
| scripts-xp-safety-audit | PASS 121 passed | PASS | inherited |
| js-harnesses | PASS | PASS | inherited |

## R1 and R2 on the integrated candidate

**R1** — `reproduce_inventory_ambiguity.py` exits 0 on the candidate. Both
paths refuse to take a unit they cannot prove is theirs
(`legacy_no_provenance`, `consumed_before_rollback`).

**R2** — `reproduce_booster_obligation_gap.py` exits 0, and the durable
obligation is present:

```
{'role_preserved_under_unknown': True,
 'durable_obligations': [('booster_retention', 557)]}
```

Before this change the same script printed `durable_obligations: []` and died
with `AssertionError: BLOCKER: source ended under unknown Booster eligibility
without a durable retry obligation`. The red form is preserved verbatim in
`archive/2026-10-31-design-c:review-materials/reproduce_booster_obligation_gap.output.txt`.

## Decision E

The concurrent-migration tests live in
`test_delivery_safety.py::EntryKeyMigrationTests` (6 passed), including
`test_concurrent_duplicate_column_is_treated_as_success`, which was verified
**red without the fix** (it failed with exactly `sqlite3.OperationalError:
duplicate column name: entry_key` → `RuntimeError: Required migration
equipped_roles.entry_key failed; startup aborted`).

## GitHub CI

`.github/workflows/tests.yml` is `on: pull_request` only, by design ("a
branch push must not start a build"). It therefore **cannot** be triggered by
pushing this branch; it needs a pull request. See the handoff note in
`PRESERVATION_VERIFICATION_REPORT.md` / the final report — CI on this exact
SHA is pending that decision and was not started unilaterally.
