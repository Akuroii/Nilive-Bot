#!/usr/bin/env python3
"""Safe operator CLI for inspecting and resolving `uncertain` Shop purchases.

Read-only by default:
  * `python scripts/recover_shop_purchases.py`
      Lists all `uncertain` purchase operations.
  * `python scripts/recover_shop_purchases.py --op-id <op_id>`
      Displays full diagnostic evidence (operation journal, shop listing,
      wallet balance, ledger rows, inventory items, timed holdings, temp_roles,
      equipped role) without mutating any state.

Explicit operator resolution (requires BOTH `--resolve` and `--yes`):
  * `python scripts/recover_shop_purchases.py --op-id <op_id> --resolve refund --yes`
      Atomically and idempotently refunds charged currency and restores reserved
      stock (via `refund_purchase()`), then records the resolution in the
      purchase journal audit trail.
  * `python scripts/recover_shop_purchases.py --op-id <op_id> --resolve delivered --yes`
      Marks the operation as `delivered` after the operator has verified that
      the member holds the purchased product, preserving the audit trail.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils.timed_ownership import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
