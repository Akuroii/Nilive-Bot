# Project agent rules

1. Before any edit, inspect Git status, current branch, and HEAD.
2. Never delete previous work or files whose purpose is unknown. Preserve and document them first.
3. Do not perform broad changes or rewrites without approval.
4. Every repair claim must identify its implementation and a regression test.
5. Claim a test passed only after actually executing it and recording the command, exit code, and output.
6. Test-function counts are not passing-test counts.
7. Run script harnesses using their documented `python file.py` entry point; pytest collection is not a substitute.
8. Test pipelines must retain Python's exit code, not the exit code of `tail` or another consumer. Prefer direct subprocess capture.
9. Distinguish product failures from test-environment and mock failures.
10. Do not change production requirements just to run tests. Install test dependencies in an isolated environment.
11. Do not modify production data, deploy, or merge pull requests.
12. Do not silently continue after a required schema migration or security check fails.
13. Absence of tests is not evidence of correctness.
14. If requirements conflict, or a decision needs a schema change or a change to legacy ownership policy, explain alternatives and risks and ask before choosing.
15. If a repair cannot be implemented safely, do not invent a workaround or weaken the condition. Describe the safe alternative and what must be deferred; obtain approval for policy-changing alternatives.

## Session safeguards

- Work and publish only on `arena/c014328f-nilive-bot` for this session.
- No destructive reset, clean, force push, or checkout that discards local work.
- Recovery archives are unreviewed references, never bulk overlays or approved source merges.
- Keep AGENTS.md and CLAUDE.md byte-identical (`cmp -s AGENTS.md CLAUDE.md`).
- Record exact tested HEAD and any uncommitted patch; never attribute dirty-tree results to a clean SHA.
- A PR is allowed only after the user's acceptance gates are met; do not merge it or deploy.
