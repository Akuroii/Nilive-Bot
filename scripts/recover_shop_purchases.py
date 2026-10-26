#!/usr/bin/env python3
"""Safe operator CLI for inspecting and resolving `uncertain` Shop purchases.

Requires an explicit SQLite database target via `--db <path>` or `DATABASE_PATH`
so the CLI can never silently target or create a default production database.

Read-only by default:
  * `python scripts/recover_shop_purchases.py --db /path/to/nilive.db`
      Lists all `uncertain` purchase operations.
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
  `refunded`; it is not resolvable here and needs the runtime reconciliation
  above.

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


def main(argv: list[str] | None = None) -> int:
    from utils.timed_ownership import main as _to_main
    return _to_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
