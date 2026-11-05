#!/bin/sh
# Run from the repository root AFTER extracting the ZIP there (unzip -o).
# Verifies every payload file, then applies the 22 deletions that a plain overlay cannot.
set -e
git rev-parse --show-toplevel >/dev/null 2>&1 || { echo "Run inside the git checkout of the repository."; exit 1; }
BASE=dd338082e40471495e78e7751af5bfcce7731bf6
git merge-base --is-ancestor "$BASE" HEAD 2>/dev/null || echo "WARNING: HEAD does not contain base $BASE; review 'git status' carefully."
echo "== verifying payload checksums =="
sha256sum -c MANIFEST.sha256 >/dev/null && echo "all checksums OK" || { echo "CHECKSUM FAILURE - aborting before any deletion"; exit 1; }
echo "== applying deletions =="
n=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  git rm -q --cached -- "$f" 2>/dev/null || true       # untrack (tracked files only)
  case "$f" in *.pyc) ;; *) rm -f -- "$f" ;; esac       # .pyc stay on disk (they are gitignored build output)
  n=$((n+1))
done < DELETED_FILES.txt
echo "processed $n deletions"
echo "== done. Review with: git status --short =="
echo "Package metadata (README_INSTALL.txt MANIFEST.sha256 DELETED_FILES.txt install.sh nilive-bot-session-updates.patch) is not part of the repo; delete it before committing."
