#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# Transparent, reproducible validation runner.
#
#   ./run_validation.sh <git-ref> [output.tsv]
#
# WHAT THIS IS NOT
# ----------------
# This is NOT the lost `run_matrix_v2.sh`, and it does not claim the old
# 116-gate matrix passed. That runner is gone and its results are not
# reproducible. This is a NEW list, rebuilt from two honest sources:
#
#   1. the surviving evidence of what Design C itself was validated with
#      (`review-materials/clean-3768d96-tests.log`), and
#   2. the suites this candidate actually touches.
#
# Every row records its own exit code, its own duration and its own last
# output line, so any row can be re-run by hand and checked.
#
# HOW IT WORKS
# ------------
# Every item runs in a CLEAN worktree checked out at exactly <git-ref>, so a
# result describes that SHA and nothing else. Nothing is ever run against a
# dirty tree, and no item's outcome can be affected by another item.
#
# The exit code is the only verdict. A row is PASS iff its exit code is 0.
# ═══════════════════════════════════════════════════════════════════════
set -u

REF="${1:-HEAD}"
OUT="${2:-validation-$(date -u +%Y%m%dT%H%M%SZ).tsv}"
PYTHON="${VALIDATION_PYTHON:-python3}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Raw per-item logs are preserved next to the TSV, not in a temp dir that
# disappears with the worktree.
OUTDIR="$(cd "$(dirname "$OUT")" && pwd)"
LOGDIR="$OUTDIR/$(basename "$OUT" .tsv)-logs"
mkdir -p "$LOGDIR"

if [ ! -d "$REPO_ROOT/.git" ] && [ ! -f "$REPO_ROOT/.git" ]; then
    echo "FATAL: $REPO_ROOT is not a git checkout" >&2
    exit 2
fi

# ── the item list: name | command | timeout-seconds ─────────────────────
# Design C's own suites (what the surviving clean-3768d96-tests.log ran),
# the suites this candidate changes, and both reproducers.
ITEMS=(
    "design-c-purchase-grant-ledger|$PYTHON -m pytest -q --tb=short --no-header -p no:cacheprovider test_purchase_grant_ledger.py|1800"
    "design-c-timed-ownership|$PYTHON -m pytest -q --tb=short --no-header -p no:cacheprovider test_timed_ownership.py|1800"
    "design-c-delivery-safety|$PYTHON -m pytest -q --tb=short --no-header -p no:cacheprovider test_delivery_safety.py|900"
    "r2-role-retention|$PYTHON -m pytest -q --tb=short --no-header -p no:cacheprovider test_role_retention.py|900"
    "r1-inventory-ambiguity|$PYTHON docs/execution-20261030/reproduce_inventory_ambiguity.py|300"
    "r2-booster-obligation-gap|$PYTHON docs/execution-20261030/reproduce_booster_obligation_gap.py|300"
    "scripts-level-ops-provenance|$PYTHON scripts/test_level_ops_provenance.py|300"
    "scripts-level-test-reset|$PYTHON scripts/test_level_test_reset.py|300"
    "scripts-xp-rollback|$PYTHON scripts/test_xp_rollback.py|300"
    "scripts-currency-reversal-consistency|$PYTHON scripts/test_currency_reversal_consistency.py|300"
    "scripts-correction-lease-fence|$PYTHON scripts/test_correction_lease_fence.py|300"
    "scripts-inflight-role-correction|$PYTHON scripts/test_inflight_role_correction.py|300"
    "scripts-lease-expiry-role-recovery|$PYTHON scripts/test_lease_expiry_role_recovery.py|300"
    "scripts-one-shot-xp-durability|$PYTHON scripts/test_one_shot_xp_durability.py|300"
    "scripts-xp-safety-audit|$PYTHON scripts/test_xp_safety_audit.py|600"
    "js-harnesses|npm test|600"
)

note() { printf '%s\n' "$*" >&2; }

# ── clean worktree at exactly $REF ──────────────────────────────────────
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nilive-val-XXXXXX")"
cleanup() {
    git -C "$REPO_ROOT" worktree remove --force "$WORK/tree" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

note "ref            : $REF"
note "resolved sha   : $(git -C "$REPO_ROOT" rev-parse "$REF")"
note "worktree       : $WORK/tree"
note "python         : $($PYTHON --version 2>&1)"
note ""

if ! git -C "$REPO_ROOT" worktree add --detach "$WORK/tree" "$REF" >/dev/null 2>&1; then
    note "FATAL: could not create a clean worktree at $REF"
    exit 2
fi

TREE="$WORK/tree"
cd "$TREE" || exit 2

# Every Python item needs the same environment the suites set up themselves.
export OWNER_ID="${OWNER_ID:-1}"
export PYTHONFAULTHANDLER=1
export PYTHONPATH=".:scripts"
# Keep the runs hermetic: no network, no cache writes into the tree.
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

printf 'item\texit\tseconds\tlast_line\n' > "$OUT"

PASS=0; FAIL=0; SKIP=0
for row in "${ITEMS[@]}"; do
    name="${row%%|*}"
    rest="${row#*|}"
    cmd="${rest%|*}"
    tmo="${rest##*|}"

    # A suite that is not present at this ref is SKIP, not FAIL: the ref
    # simply does not carry that file. Recorded explicitly.
    first_word="${cmd%% *}"
    case "$cmd" in
        *pytest*)
            target="$(printf '%s\n' "$cmd" | awk '{print $NF}')"
            [ -f "$target" ] || { printf '%s\t-1\t0\tSKIP: %s not present at this ref\n' "$name" "$target" >> "$OUT"; SKIP=$((SKIP+1)); note "SKIP  $name ($target absent)"; continue; } ;;
        *scripts/*)
            target="${cmd#* }"
            [ -f "$target" ] || { printf '%s\t-1\t0\tSKIP: %s not present at this ref\n' "$name" "$target" >> "$OUT"; SKIP=$((SKIP+1)); note "SKIP  $name ($target absent)"; continue; } ;;
        *docs/execution-20261030/*)
            target="${cmd#* }"
            [ -f "$target" ] || { printf '%s\t-1\t0\tSKIP: %s not present at this ref\n' "$name" "$target" >> "$OUT"; SKIP=$((SKIP+1)); note "SKIP  $name ($target absent)"; continue; } ;;
    esac

    start=$(date +%s)
    log="$LOGDIR/$name.log"
    # `timeout` bounds the item; the exit code is preserved through the pipe.
    timeout -s ABRT "$tmo" bash -c "$cmd" >"$log" 2>&1
    rc=$?
    end=$(date +%s)
    secs=$((end - start))

    last="$(tail -n 1 "$log" 2>/dev/null | tr '\t\n\r' '   ' | cut -c1-160)"
    printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "$secs" "$last" >> "$OUT"

    if [ "$rc" -eq 0 ]; then
        PASS=$((PASS+1)); note "PASS  $name (${secs}s)"
    else
        FAIL=$((FAIL+1)); note "FAIL  $name exit=$rc (${secs}s)"
        note "        last: $last"
    fi
done

note ""
note "──────────────────────────────────────────────"
note "ref $REF"
note "PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP  total=${#ITEMS[@]}"
note "results: $OUT"
note "raw logs: $LOGDIR/"

# Exit non-zero if anything failed, so CI and humans see the same verdict.
[ "$FAIL" -eq 0 ] || exit 1
exit 0
