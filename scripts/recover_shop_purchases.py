#!/usr/bin/env python3
"""Safe operator CLI for inspecting and resolving `uncertain` Shop purchases.

Requires an explicit SQLite database target via `--db <path>` or `DATABASE_PATH`
so the CLI can never silently target or create a default production database.

Runs with no environment configuration beyond the database target: the
`OWNER_ID` the bot runtime requires is not needed here and must not be exported.

Exit codes (stable, so this can be scripted):
    0  success / the requested check passed (`--check-rollback` found it SAFE)
    1  the requested check failed, the operation was not found, or performance
       was refused; nothing was changed unless the output says otherwise
    2  usage error: a required flag (`--yes`, `--note`) was missing

Read-only by default:
  * `python scripts/recover_shop_purchases.py --db /path/to/nilive.db`
      Lists `uncertain` purchase operations AND every operation whose
      late-delivery rollback is still pending, each with its durably recorded
      refusal reason. `--pending-rollbacks` lists only the latter;
      `--all-statuses` lists everything; `--status <s>` filters to one status.
  * `python scripts/recover_shop_purchases.py --db /path/to/nilive.db --op-id <op_id>`
      Displays full SQLite diagnostic evidence (operation journal, shop listing,
      wallet balance, ledger rows, inventory items, timed holdings, temp_roles,
      equipped role) without mutating any state.

Pending late-delivery rollbacks (`delivery_rollback`):
  `--op-id` inspection prints the operation's `Delivery Rollback` line. Three
  states are possible, and they are NOT interchangeable:
    * `(none recorded)`   - no late-delivery compensation was ever needed.
    * `completed`         - compensation finished; the descriptor was cleared.
    * `PENDING (...)`     - compensation is unfinished. `reconcile_delivery_rollbacks`
                            retries it automatically, including after a restart.
  A descriptor that is not an EXECUTABLE descriptor - unparseable JSON, a
  non-object, an empty object, or one missing its identity - is preserved
  byte-for-byte. The runtime keeps it as the only surviving record of what the
  operation was doing, so `--retire-rollback` refuses it and says so; inspect the
  raw row and resolve by hand. A descriptor that already completed was cleared to
  the `''` sentinel, which is also not retirable (there is nothing left to do).

  A descriptor stays PENDING on purpose when the rollback cannot be reconciled
  with the grant it claims - for example a `role_id` that disagrees with the
  timed holding or with the delivered inventory row's `metadata.role_id`, an
  `item_name` that addresses a different inventory row than the delivered
  snapshot, a holding that names another operation as its origin, or a
  malformed `entry_key`. The diagnostic in the journal's last error names the
  conflict. Resolve the CONFLICT (fix the recorded grant, or compensate the
  member by hand and record why), then let reconciliation retry; do not blank
  `delivery_rollback` with SQL, which would silently mark uncompensated state
  as done.
  Note: `--resolve` applies only to operations in status `uncertain` or
  `failed`. An operation whose rollback is pending is usually already
  `refunded`; `--resolve` is refused for it (see the retirement procedure
  below), and the runtime reconciliation above keeps retrying instead.

Retiring a rollback that cannot be reconciled (evidence-based, audited):
  1. `--op-id <op_id> --check-rollback`
       Read-only. Prints SAFE / NOT SAFE plus the evidence the verdict is based
       on: the delivered inventory row (present or absent), any rows still
       carrying the item name, live boosts recording this operation, holdings
       recording it, and the last recorded refusal. Exit code 0 = SAFE, 1 = not.
       A permanent role can never be SAFE here: proving its Discord membership
       needs live state this tool does not read, so it must be resolved by hand.
  2. If the outstanding effects really are gone (or you compensated the member
     by hand), retire the descriptor:
       `--op-id <op_id> --retire-rollback --yes --note "verified: nothing left to compensate"`
       This REFUSES unless the operation is settled (`refunded` or `failed`) and
       the evidence above proves the operation's own effects are absent; a
       `pending`/`uncertain` operation may still have a delivery in flight, so
       let the runtime finish and retry first. It always refuses without
       `--note`. It appends the evidence
       and your note to the operation's `detail` (the audit trail) and records
       durable completion, so compensation never runs again for it.
     There is deliberately no blanket "clear descriptor" / "mark complete"
     switch, and no flag that skips the evidence check. Evidence is gathered
     from the OPERATION's own guild/member, never from the descriptor's, so a
     descriptor naming someone else cannot hide the real operation's boost,
     holding or inventory row. Effects whose provenance cannot be established
     (a legacy boost or holding with no `origin_op_id`) count as "not proven
     absent" and block retirement: verify those on Discord first.
  `--resolve delivered` refuses while any `delivery_rollback` state exists
  (pending, unreadable, or already completed). Marking such an operation
  delivered would move it out of the retry scan while leaving the descriptor on
  the row, making it unreachable from both reconciliation and this tool.

Explicit operator resolution (requires `--op-id`, `--resolve`, `--yes`, and `--note`):
  * `python scripts/recover_shop_purchases.py --db /path/to/nilive.db --op-id <op_id> --resolve refund --yes --note "verified role not granted"`
      Atomically and idempotently refunds charged currency and restores reserved
      stock (via `refund_purchase()`), then records the resolution and operator
      note in the purchase journal audit trail.
  * `python scripts/recover_shop_purchases.py --db /path/to/nilive.db --op-id <op_id> --resolve delivered --yes --note "verified role present on Discord"`
      Marks the operation as `delivered` after the operator has verified that
      the member holds the purchased product (including any live Discord role
      state, which this offline SQLite tool does not query directly), preserving
      the audit trail.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# `database.py` refuses to import without OWNER_ID because the RUNTIME must not
# boot with an unknown owner. This tool is an offline operator console: it never
# acts as (or on behalf of) the owner and never touches `dashboard_users`, so the
# documented `--db <path>` invocation must work without export OWNER_ID=…
# A sentinel is used rather than a real-looking id, and it is only ever read by
# `dashboard` owner-gating code the CLI does not call.
if not os.environ.get("OWNER_ID", "").strip():
    os.environ["OWNER_ID"] = "0"


def main(argv: list[str] | None = None) -> int:
    from utils.timed_ownership import main as _to_main
    return _to_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
