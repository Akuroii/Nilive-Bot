# G — purchase grant ledger (design C)

Status: originally implemented on a local-only branch (`arena/pr3-delivery-fixes`)
that was never pushed and does not exist on GitHub. The delivery is preserved at
`review/g-purchase-grant-ledger` (c872aa29) and `archive/2026-10-31-design-c`; this
checkpoint re-applies the original patch to `candidate/2026-10-31-chain-design-c`
(base f9e413f).
Owner review pending. Replaces the snapshot-comparison rollback that
`reproduce_inventory_ambiguity.py` proved unsafe (newer units removed; ABA).

## The rule

A stackable Shop unit (potion / title / custom) may be taken back by a late
compensation **only through the `reward_grants` row that the purchase itself
wrote, in the same transaction as the inventory increment.** Nothing else is
evidence: not the item name, not the stack quantity, not the
`delivered_inventory` snapshot (it is kept in the descriptor for diagnostics
only — a same‑second re‑grant reproduces it byte for byte).

## Schema (additive, `utils/level_ops.py` → `ensure_tables`)

| change | detail |
|---|---|
| `reward_grants.grant_source TEXT` | `level_reward` \| `shop_purchase`. Added by `ALTER`; the **same call that adds the column** backfills `NULL → 'level_reward'` (every pre‑existing row was written by the level claim engine, the only writer at the time). Quantities/timestamps untouched. Re‑running `ensure_tables` later is read‑only (it is reached lazily from previews). Every reader treats `NULL` as a level grant. |
| `reward_grants.reversed_at / reversed_by TEXT` | set exactly once when a purchase grant is taken back — the idempotency guard. |
| `idx_rg_purchase_operation` | `UNIQUE (operation_id) WHERE grant_source='shop_purchase'`. One operation ⇒ one grant; a retried delivery fails the INSERT and its increment rolls back with it. Level operation ids may repeat, hence partial. |

`reward_reversals` is **not** written for purchases (it is keyed by `claim_id`,
which a purchase does not have; no fake claim ids). The purchase descriptor
plus the grant row are the journal.

## Write path (`utils/reward_engine.give_reward(..., op_id=)`)

`BEGIN IMMEDIATE` → snapshot → `give_item(db=)` → `record_purchase_grant`
(only when `op_id` given) → snapshot → `COMMIT`; rollback on any error or
cancellation. `cogs/shop.py` passes `op_id` for potion, title and custom
deliveries. Missions/minigames pass no `op_id`: no grant, unattributed, never
reversible by anyone.

## Consume order (`utils/level_ops.consume_grants`)

`ORDER BY (grant_source='shop_purchase'), id` — level grants first (by id),
then purchase grants (by id). No time‑FIFO across the two groups. Level‑reward
accounting is exactly as before purchase grants existed. Consumption and the
`remaining_quantity` update stay in one transaction (`_consume_provenance`).

## Reversal (`utils/level_ops.reverse_purchase_grant`, inside the caller's tx)

| state of the grant | result | writes |
|---|---|---|
| no row for `operation_id` | `refused` `legacy_no_provenance` | none |
| guild/user/item ≠ descriptor | `refused` `identity_mismatch` | none |
| `reversed_at` set | `already_applied` | none |
| `unattributed=1` (after `set_quantity`/admin edit) | `refused` `provenance_invalidated` | none |
| `remaining_quantity < 1` (used/traded/dropped) | `refused` `consumed_before_rollback` | none |
| otherwise | `applied` | `inventory_items.quantity -= take WHERE quantity>=take` (rowcount must be 1) and `reward_grants.remaining -= take, reversed_at, reversed_by WHERE remaining>=take AND reversed_at IS NULL` (rowcount must be 1); otherwise `LevelOpsError` → caller `ROLLBACK` |

`compensate_late_delivery` Branch 4 (stackables) now does only this. `applied`
/ `already_applied` → `inventory_rolled_back=True`, descriptor progress and the
movement commit together. `refused` → `ROLLBACK`, descriptor stays pending with
`refusal_code` + `last_error`; reconciliation replays never change the answer.

`utils/reward_engine.rollback_item_inventory_grant` is **deleted** (it was the
unsafe snapshot comparator; no remaining caller).

## Legacy policy (owner decision, fail‑closed)

A purchase delivered before the ledger has no grant → it stays pending as
`legacy_no_provenance`; the runtime never removes a unit for it and never
fabricates provenance. Operator path:

```
python -m utils.timed_ownership --op-id <op> --check-rollback
python -m utils.timed_ownership --op-id <op> --retire-rollback --yes --accept-unprovable \
    --note "<what was decided and why, ≥20 chars>"
```

`--accept-unprovable` closes only `legacy_no_provenance`,
`consumed_before_rollback`, `provenance_invalidated`; it still refuses
`effects_live` (the runtime can finish that one — run a reconciliation pass).
It removes nothing and writes an audit line `Rollback closed by operator
decision [...] WITHOUT automatic removal` into `detail`.
`rollback_retirement_evidence` now reads the grant first; a legacy op whose
item has **no units left** is trivially retirable.

Prod count (document only; a low number does not license snapshot rollback):

```sql
SELECT count(*) FROM shop_purchase_operations
 WHERE delivery_rollback IS NOT NULL AND delivery_rollback <> ''
   AND json_extract(delivery_rollback,'$.internal_type') IN ('potion','title','custom','shop_custom','item');
```

## Behaviour changes to be aware of

1. An emptied stack is left as a `quantity=0` row (like `remove_item`), not deleted; the old path deleted the row when no prior row existed. All readers already filter `quantity>0`.
2. The old restore of `prior_inventory` metadata/source/timestamps on rollback is gone; a reversal only decrements.
3. Pending stackable rollbacks on prod that were recorded **before** this change have no grant and will become `legacy_no_provenance` on the next reconciliation pass instead of being rolled back by snapshot. This is the approved policy.
4. `reward_grants` now contains rows with `claim_id NULL`, `track='shop'`. Level readers filter by `claim_id`/`kind` (`reverse_inventory`, `grant_for_claim`, `_role_granted_elsewhere`) or by `grant_source` (reset report), so they are unaffected.

## Tests (commands and exit codes recorded in `review-materials/clean-3768d96-tests.log`)

- `test_purchase_grant_ledger.py` (new, 29): one‑tx delivery incl. exception / cancellation / `os._exit` between increment and grant; unique op; concurrent purchases; the six consume‑order cases incl. NULL `grant_source`; group isolation both directions (`reverse_inventory` vs purchase compensation); overlapping purchases; ABA; partial consumption; retry after success (durable + stale spec + direct ledger); concurrent consume vs compensate (exactly one unit moves); identity mismatch (runtime + ledger); invalidated provenance; descriptor+ledger atomicity; legacy pending + replay; retire refusals; `--accept-unprovable` closes without removal; `effects_live` cannot be forced; migration from a pre‑ledger table (idempotent, quantities intact, index present, level op ids may repeat); the reproducer exits 0.
- `test_timed_ownership.py` (241): 8 tests encoded the snapshot semantics and were updated — P1 (`quantity=0` row), P2 (level provenance filter + purchase grant reversed once), P19/P33‑control/P48/legit‑stackable (fixtures now deliver through the ledger), P33/P49/CLI/D1 (message → `legacy_no_provenance` / `identity_mismatch`). No guard was weakened; the protected invariants are asserted unchanged.
- `test_delivery_safety.py` (15) unchanged.
- `scripts/test_level_ops_provenance.py`, `test_level_test_reset.py`, `test_xp_rollback.py`, `test_currency_reversal_consistency.py`, `test_correction_lease_fence.py`, `test_inflight_role_correction.py`, `test_lease_expiry_role_recovery.py`, `test_one_shot_xp_durability.py`, `test_xp_safety_audit.py`: exit 0 (needs `flask` installed for two of them).
- `reproduce_inventory_ambiguity.py`: exit 0 (legacy + provenance paths, 2 newer units and ABA each). `reproduce_booster_obligation_gap.py`: still exit 1 — F is not in this change.
