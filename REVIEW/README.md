# P-1 fix: expired `processing` claims on `/level` (REVIEW PACKAGE)

Status: **for review and testing only.** Not committed, not pushed, not merged, not deployed.
Base: branch `arena/b5a56831-nilive-bot` at commit `99851bf` (verified). `main` is untouched.

## What this fixes
A reward claim abandoned in `processing` (for example, the bot restarted after the claim was
reserved but before it was finalized) was recoverable by the claim engine once its 60 s lease
expired, but `/level` hid it and left Claim All disabled. `list_claims()` did not expose
`processing_started_at`, so the UI could not tell an expired lease from a live one.

## Change (one file, one function)
`utils/level_claims.py` -> `list_claims()` only: **+11 / -3 lines**.
- Selects `processing_started_at`.
- Uses the same expiry rule as the reservation SQL (`NULL` or older than `LEASE_SECONDS`, same ISO-string comparison).
- An expired-lease row is reported with `status = "pending"`; new key `stale_processing` carries the raw fact.
- Active leases keep `status = "processing"` and stay hidden/unavailable.
- Read-only. Untouched: `claim_available`, `_reserve_sql`, `_mark`, `owner_token`, eligibility, Booster gating,
  delivery, schema, `/level` card code, B5/P1 restore code, dashboard, tests, scripts.

## Manifest
| Path in this ZIP | Deploy? | Base blob (99851bf) | Result blob | SHA-256 |
|---|---|---|---|---|
| `utils/level_claims.py` | **Yes (only production file)** | `66c42f0e212efb9c14fea38ba62fe86c4ca08629` | `ae3dcdd0f4061e44077661991c8798031ed4a9f1` | `1011fc47b1cb29393fd69972d09d780acf7ca816bd3e3a171b63984820f454e4` |
| `REVIEW/P1_expired_processing.diff` | No (documentation) | | | |
| `REVIEW/VERIFICATION_RESULTS.md` | No (documentation) | | | |
| `REVIEW/README.md` | No (documentation) | | | |

No tests, scripts, `.pyc`, dashboard files, or root-level duplicates are included.

## How to apply (when you decide to)
Only onto a branch whose `utils/level_claims.py` is exactly the base blob:
```
git hash-object utils/level_claims.py      # must print 66c42f0e212efb9c14fea38ba62fe86c4ca08629
git apply --check REVIEW/P1_expired_processing.diff
git apply REVIEW/P1_expired_processing.diff
git hash-object utils/level_claims.py      # must print ae3dcdd0f4061e44077661991c8798031ed4a9f1
```
If the first hash differs, do NOT overwrite the file with the ZIP copy. The diff is built on
`99851bf`; current `main` has an older `utils/level_claims.py` and will not take it as-is.

## Rollback
Reverse the diff, or restore the base file:
```
git apply -R REVIEW/P1_expired_processing.diff
# or
git checkout 99851bf -- utils/level_claims.py
```
No schema or data change exists, so rollback needs no migration.

## Remaining risks
1. **Not verified in real Discord.** Embed/button rendering was exercised only with fake Discord objects.
   Also unverified on Python 3.11 (the Dockerfile image); all runs used 3.12.3.
   Suggested staging check: set a test user claim to `processing` with an old `processing_started_at`,
   open `/level`, confirm Claim All is enabled and fulfils it; repeat with a fresh timestamp and expect it disabled.
2. **Semantics:** `list_claims()` now returns an effective `"pending"` for expired leases
   (`stale_processing` has the raw fact). No repo consumer or existing test depends on a raw `processing` status from it.
3. **Slow-but-alive worker:** a delivery that truly takes over 60 s can now be overtaken by a click.
   The engine already allowed this on any Claim All; `owner_token` blocks the old worker from finalizing and
   role/temp-role delivery is idempotent. The fix makes this existing recovery easier to reach.
4. **Cosmetic footer count:** for a non-booster with a stale `booster:` row, the card shows it as locked but the
   Claim All footer "Locked (Boosters only): n" (engine `lock_sql`, ignores `processing`) omits it. Left alone: engine code.
5. **Existing string-compare quirk:** `isoformat()` drops fractional seconds when microseconds are exactly 0.
   The UI mirrors the engine on purpose so they cannot disagree; not changed.
6. Still open and separate from this fix: the B5/P1 real-Discord staging toggle test, and Railway runtime configuration.
