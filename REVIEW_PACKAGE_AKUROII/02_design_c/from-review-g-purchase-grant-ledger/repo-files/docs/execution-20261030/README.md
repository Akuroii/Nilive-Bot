# Execution checkpoint — NOT ready for a delivery PR

## References and preservation

- Fixed branch: `arena/c014328f-nilive-bot`.
- Audited candidate: `8eb8e3d4fc18c1e96cf9ec3e6d6b55aa63fc0436`.
- Parent of this execution checkpoint: `6c04021ef5b422998a80bddcf5d4635a8b529039` (archive-only commit).
- Original dirty checkout remains at `a6b812be26d35f3048db39d0425374d23cd804ea`.
- Its 30 changed/untracked files were copied to `/home/user/nilive-preservation-20261030/local-work.tar.gz`, with a binary tracked patch, per-file SHA256 manifest and saved status. Each archive member was verified against its source. The two purchase_identity demonstration files remain there and in the original checkout; they are not integrated production fixes.
- Execution uses `/home/user/Nilive-execution`, a separate clone on the SAME branch. No destructive reset, clean, branch switch or archive overlay was used.
- Recovery tarball and manifest were not modified. All 28 archived payload hashes match their manifest. See [three-way comparison](ARCHIVE_COMPARISON.md) and [machine-readable content hashes](archive-three-way.json).

## Implemented bounded changes

| Requirement | Implementation | Regression / acceptance |
|---|---|---|
| Permanent agent rules | root AGENTS.md and CLAUDE.md, byte-identical | `cmp -s AGENTS.md CLAUDE.md` |
| A: no wallet Drop | cogs/wallet.py and legacy root wallet.py: remove button, confirmation classes, builder, opener, imports | WalletDeletionSurfaceTests plus both inventory-UI/wallet harness copies assert absence. Internal drop_item retained; provenance consumers still tested. |
| B: foreign descriptor identity | Existing production boundary retained, not reimplemented | DescriptorIsolationTests calls actual compensate_late_delivery with foreign guild/user; snapshots inventory, equipment, boosts, holdings and a separate purchase; checks no Discord lookup/mutation and durable descriptor remains. |
| C: atomic potion transaction | utils/potion_engine.use_potion reads metadata, consumes quantity/provenance, inserts boost and commits on one connection; utils/inventory.remove_item gains optional caller-owned db | PotionTransactionTests: success; real provenance decrement/restore; effect failure after insert; cancellation; last-copy concurrency; caller rollback; commit failure; actual use_potion child-process exit both before and after effect insertion. No schema change. |
| D: reproduced Level progression/equip race only | enforce_role_progression holds existing slot_lock before ownership read through Discord removal | LevelRemovalSynchronizationTests: real enforce/equip callers, real SQLite; only superseded-role selection and Discord boundary are fakes. Confirmed failing before lock, passing after. This does NOT cover all role writers. |
| E: required entry_key migration | init_db raises RuntimeError with chained cause instead of logging and continuing | EntryKeyMigrationTests: fresh initialization, legacy row preservation/NULL identity, repeated boot, injected ALTER failure aborts. |
| F: Booster harness repair only | scripts/test_booster_rewards.py supplies followup and inspects actual ephemeral followup result | Real Claim All callback runs; asserts locked != failed, disabled button expectation, ephemeral thinking defer, and no edit of original card. Product behavior not changed. |
| Regression CI | ownership job also runs test_delivery_safety.py | Workflow remains PR-only; no CI pass is claimed from a branch push. |

Potion scope: no new idempotency key. Separate legitimate activations may consume separate owned copies, as before. The demonstrated guarantee is SQLite all-or-nothing across inventory, reward provenance and effect, not distributed/external exactly-once delivery.

## Confirmed blockers — deliberately not hidden

### G: old rollback can remove newer purchase units (data loss)

> **Update (PR‑3, 2026‑10‑30):** resolved by the purchase grant ledger (design C) —
> see [G-purchase-grant-ledger.md](G-purchase-grant-ledger.md). The reproducer
> below now exits **0** and additionally covers the provenanced ABA case; the
> `rollback_item_inventory_grant` helper is removed. The text that follows is
> the original finding, kept as the record of what was reproduced on f9e413f.

Run:

```bash
OWNER_ID=1 PYTHONPATH=. /home/user/nilive-test-env/bin/python docs/execution-20261030/reproduce_inventory_ambiguity.py
```

Exit **1** confirms four unsafe cases: helper and durable compensate_late_delivery, each with a newer two-unit stack and an ABA return to the same snapshot. The old unit was consumed before the new grant. The journal fixture identifies distinct old/new operations. These are real SQLite/compensation calls, not full Discord purchase interactions. ABA explicitly models a same-timestamp snapshot restoration. In the durable path compensation reports complete=True after removing newer units.

This diagnostic is a required negative delivery gate, not a passing test/expected failure/skip. Its location under docs does NOT exclude this failure from release acceptance.

**Decision required before schema/legacy-policy changes:**

1. Recommended bounded design: additive inventory revision sidecar keyed by guild/user/item; transactional triggers for insert/update/delete so direct writers and ABA cannot bypass revision increments; a purchase certificate written in the same transaction as the grant, binding operation/member/product/row and delivered revision. Automatic rollback only when the certificate and current revision match; ambiguous mutation leaves durable rollback pending. Existing stacks receive neutral baseline zero, NEVER fabricated purchase certificates. Legacy rollback descriptors without a certificate need explicit conservative/manual-recovery policy approval. Triggers, migration repeat/failure tests, all writer coverage and rollback idempotency tests are mandatory.
2. Smaller no-schema containment: refuse automatic stackable rollback when provenance cannot be proven and preserve a durable pending obligation for manual resolution. This changes old recovery behavior too; it is NOT silently adopted.
3. Per-unit grant attribution: broader design, not recommended as an unreviewed deadline change.

No option was implemented without approval. Snapshot comparison alone is not a safe alternative: ABA is reproduced.

### F: unknown Booster/source-ended retry obligation missing

Run:

```bash
OWNER_ID=1 PYTHONPATH=. /home/user/nilive-test-env/bin/python docs/execution-20261030/reproduce_booster_obligation_gap.py
```

Exit **1**: actual Twitch source-ended path preserves a worn shared role as an active Booster reward when boost count is unknown, but durable obligations are empty. Notification/Discord external boundaries are faked; entitlement and SQLite paths are real. Since the initial obligation is absent, creation/restart/retry-failure lifecycle is NOT proven. temp_role_cleanup is NOT certified as equivalent to system_cleanup.

Remaining implementation: distinct unknown vs verified eligibility, durable non-entitling source-ended obligations before losing the source record, retry that retains obligations on error/unknown, clears only after verified resolution, and fairness/restart tests. This work is authorized in scope but NOT implemented at this checkpoint.

### D: synchronization is still incomplete

See [all concrete role call sites](ROLE_SYNCHRONIZATION.md) / [CSV](role-writers.csv). Level delivery/Claim All, Prestige, Booster, reset/rollback, Twitch and MVP require a shared boundary and lock-order review. Existing operation leases are not slot_lock. Do not blindly nest the non-reentrant lock. The fixed progression/equip race does not establish universal serialization or multiprocess coordination.

### E: recovery audit is not fully closed

Only required equipped_roles.entry_key migration was repaired here. Previously identified recovery preflight, uncompensated listing without descriptors and offline timed-role absence-evidence questions remain outside this implemented batch. Do not describe the entire recovery tool as newly certified.

## Test evidence and scope

- Isolated environment: `/home/user/nilive-test-env`, Python **3.11.2**. Installed unchanged production requirements plus pytest; [exact dependency freeze](test-dependencies.txt). No requirements.txt modification.
- Complete precommit logs and JSON records (52 files, preserved unmodified at tag `evidence-20261030-f9e413f`, branch `evidence/execution-20261030`, path `docs/execution-20261030/precommit-logs/`; deliberately kept out of the delivery PR) record argv, HEAD, dirty status, duration, real child exit code and output hash. These ran at HEAD `6c04021` WITH the explicitly recorded worktree modifications. They are NOT clean-SHA results and are NOT attributed to unmodified 8eb8e3d.
- Baseline ownership was run before product edits (only agent documents untracked): 241 passed, 5 subtests, exit 0. After product edits: also 241 passed, 5 subtests, exit 0.
- Final precommit safety suite: 15 passed, 14 subtests, exit 0.
- API/free-shop/VI/Phase1 selected pytest suites: 88 passed, 31 subtests, exit 0.
- scripts/test_inventory_ui.py: 161 passed, 0 failed, exit 0.
- scripts/test_wallet_phase2.py: 54 passed, 0 failed, exit 0.
- scripts/test_level_reward_role_progression.py: 46 passed, 0 failed, exit 0.
- scripts/test_booster_rewards.py, scripts/test_inflight_role_correction.py, scripts/test_level_ops_provenance.py, scripts/test_level_test_reset.py: exit 0; use raw summaries. Do not invent test counts for harnesses that do not report a total.
- npm test: all 18 harnesses passed, exit 0; includes actual Shop form JavaScript harness.
- An initial new descriptor test used an unsupported member= argument; an initial race fixture used an unhashable role; an initial F diagnostic used a nonexistent helper. Corrected test defects, not claimed product defects. Raw attempts are retained. The valid Level race subsequently failed on its safety assertion before the production fix.
- The historical Booster harness failure from TEST_BASELINE.md was a mock/output-contract defect. It was repaired without replacing the real Claim All callback.
- The initial safety-first output is preserved losslessly in safety-first.raw.json (stdout_stderr field), since its literal trailing whitespace fails Git whitespace checks; decode the JSON field to recover the exact output matching its recorded hash. Generated CSV uses LF line endings.
- Outputs captured directly with subprocess.run, not Python piped to tail. Per-command recorded exit codes are authoritative, not the exit code of the last command in a shell batch.
- Exact clean-commit reruns are to be captured AFTER committing this checkpoint, outside the worktree to avoid changing the tested SHA. No final-delivery claim is made here.

NOT RUN / NOT CERTIFIED: the full 41-file root matrix in both modes, all 72 Python script harnesses, live Discord, browser end-to-end UI, production database migration/deployment, universal permission audit, all shared-role races, new certificate/revision lifecycle, durable system_cleanup lifecycle. Known historical baseline failures are not waived.

## PR gate

**Do not open a delivery PR yet.** G, F and remaining D work prevent the user's acceptance conditions. Selected green suites do not erase reproduced data loss. This checkpoint preserves reviewable safe changes and reproducible blockers; it is not approval to merge or deploy. No deployment, PR merge or production data change has occurred.
