#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Runs every Node test harness in scripts/. No dependencies, no build
# step — the dashboard ships hand-written JS, so "the frontend test
# suite" is literally these files. Kept in one place so CI and humans
# run the same thing (`npm test`).
#
# Each harness is self-contained and prints its own summary; this only
# aggregates exit codes, because a suite that silently skips a file is
# worse than no suite at all.
# ═══════════════════════════════════════════════════════════════
set -uo pipefail
cd "$(dirname "$0")/.."

STATUS=0
RAN=0
FAILED=()

note() { printf '  %s\n' "$*"; }

# ── 1. syntax ───────────────────────────────────────────────────
# Cheaper to say "unparsable" than to let a harness die on a confusing
# vm error three files deep.
echo "▶ node --check (dashboard static JS)"
for f in dashboard/static/js/*.js; do
    if out=$(node --check "$f" 2>&1); then
        note "ok   $f"
    else
        note "FAIL $f"
        printf '%s\n' "$out"
        STATUS=1; FAILED+=("syntax: $f")
    fi
done
echo

# ── 2. harnesses ────────────────────────────────────────────────
for t in scripts/test_*.js; do
    [ -e "$t" ] || continue
    RAN=$((RAN + 1))
    echo "▶ $t"
    echo
    if node "$t"; then
        echo "✓ $t"
    else
        echo "✗ $t FAILED"
        FAILED+=("$t")
        STATUS=1
    fi
    echo
done

echo "════════════════════════════════════════════════════════════"
if [ "$STATUS" -eq 0 ]; then
    echo "$RAN harness(es) passed."
else
    echo "$RAN harness(es), ${#FAILED[@]} failure(s):"
    for f in "${FAILED[@]}"; do echo "  - $f"; done
fi
exit $STATUS
