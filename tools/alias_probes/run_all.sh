#!/usr/bin/env bash
# Run the whole alias verification suite. Any failure exits non-zero.
#
#   PROBE_DIR=~/.cache/probe-db PYTHON=~/.cache/nero-probe-env/bin/python \
#       ./tools/alias_probes/run_all.sh
set -u
cd "$(dirname "$0")/../.." || exit 2

PYTHON="${PYTHON:-python3}"
NODE="${NODE:-node}"
FAILED=0

run() {
    local label="$1"; shift
    printf '\n\033[1m──── %s\033[0m\n' "$label"
    if "$@"; then
        printf '\033[32m✓ %s\033[0m\n' "$label"
    else
        printf '\033[31m✗ %s\033[0m\n' "$label"
        FAILED=$((FAILED + 1))
    fi
}

# p1 is a demonstration, not an assertion script: it prints the ordering that
# makes content mutation impossible. Everything else self-checks.
run "p1  dispatch ordering (evidence)"   "$PYTHON" tools/alias_probes/p1_ordering.py
run "p2  static guards"                   "$PYTHON" tools/alias_probes/p2_real_cog.py
run "p4  dashboard api + page + assets"    "$PYTHON" tools/alias_probes/p4_flask.py
run "p6  router consistency"              "$PYTHON" tools/alias_probes/p6_race.py
run "p8  alias acceptance (real cogs)"     "$PYTHON" tools/alias_probes/p8_acceptance.py
run "p10 full boot + reverse reload"       "$PYTHON" tools/alias_probes/p10_full_boot.py

if [ -n "${JSDOM_PATH:-}" ] || node -e "require('jsdom')" >/dev/null 2>&1 \
   || [ -d "$HOME/.cache/jstest/node_modules/jsdom" ]; then
    run "p9  chip input in a real DOM" "$NODE" tools/alias_probes/p9_chips_dom.js
else
    printf '\n\033[33m! skipping p9 (jsdom not found — npm i jsdom, or set JSDOM_PATH)\033[0m\n'
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
    printf '\033[1;32mall probes passed\033[0m\n'
else
    printf '\033[1;31m%d probe(s) failed\033[0m\n' "$FAILED"
fi
exit "$FAILED"
