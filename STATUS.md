# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #11)

## Fixed this pass: unbounded in-memory cooldown dicts (slow leak)

Flagged as "low priority, not touched" across passes #9/#10 — cleared now
since the higher-priority items were all already fixed (see verification
below) and Event Stack Builder / Trade System are blocked on Dark's input.

Three dicts grew forever with no eviction, each accumulating one permanent
entry per (guild, user[, command/trigger]) combination for the lifetime of
the process:

- `cogs/economy.py` — `_daily_cooldowns`, `_work_cooldowns`. The stored
  value is already the cooldown's *expiry* timestamp, so pruning is exact:
  drop anything whose expiry has already passed.
- `cogs/triggers.py` — `Triggers._last_fired` (per-cog instance dict, not
  module-level). Stores *last-fired* time, not expiry, and per-trigger
  cooldown length lives in the DB — so pruning uses a generous fixed
  24h age cutoff instead (trigger cooldowns are dashboard-configured in
  seconds/minutes, never days).
- `main.py` — `_command_cooldowns` (used by `NeroCommandTree.interaction_check`).
  Same last-used-time shape as triggers.py; same 24h age-cutoff approach.

All three prune opportunistically — only once a dict has grown past a
threshold (2000 for the per-cog dicts, 5000 for the global command-cooldown
dict) — so normal-traffic bots pay zero extra cost per command/trigger;
only a dict that has actually accumulated a lot of stale entries pays a
one-time O(n) sweep to shrink back down. No change to any cooldown's
externally-visible behavior (a fresh cooldown is still honored exactly as
before) — this only affects when old, already-expired entries get freed.

Verified: all three files `py_compile` clean, and the prune logic itself
was unit-tested in isolation (below-threshold = no-op, above-threshold =
correctly evicts only stale entries) before being wired into the actual
cogs.

`leveling.py`'s `_xp_cooldowns` / `_spam_tracker` were NOT touched this
pass (scope discipline — three files is enough for one change-set; same
fix pattern applies there if wanted next).

## Prior finding, still applies: 1 verified finding, no functional/behavioral changes

Continued the pass #10 pattern of auditing "carry-forward open items" against
actual ZIP contents before touching anything. Re-verified every item memory
listed as still-open:

- SECRET_KEY hardcoded fallback in dashboard/app.py: **already fixed**
  (fail-fast check present, no insecure fallback string in source)
- Stored XSS in dashboard/api/*.py partials (moderation_logs_partial,
  shop_items_partial, economy_leaderboard_partial, audit_log_partial):
  **already fixed** — all use `markupsafe.escape()` on every
  attacker-influenced field
- CSRF gap on /config/access + /config/commands form POSTs: **already
  fixed** — both routes check a hidden `csrf_token` form field against
  the session token, and both templates already carry the hidden input
- reaction_role_expiry sentinel collision: **already fixed** — message_id
  is part of the primary key, with a migration that rebuilds the table
  and backfills message_id=0 for legacy rows
- XP grant race condition in reward_engine.give_reward(): **already
  fixed** — runs inside a single BEGIN IMMEDIATE transaction

None of these needed work. Memory/handoff notes were stale relative to the
actual code — consistent with the project's own "ZIP = source of truth"
rule and pass #10's identical finding for a different set of items.

## New finding this pass — orphaned pre-split monolith, confirmed dead code

`dashboard/api.py` (the original 2159-line, 90+ route file) and
`dashboard/api/__init__.py` (the post-refactor package split into core.py /
economy_shop.py / leveling.py / misc.py / moderation.py / mvp.py /
tickets.py) **both currently exist** in the repo at the same path.

This is the same "leftover after refactor" class of bug as the orphaned
`commands.html` template deleted in pass #10 — the split's own header
comment in `dashboard/api/__init__.py` says the monolith was "split into
this package," implying the original file should have been removed at the
same time, but it wasn't.

Confirmed via a live Python import experiment (not just code reading) that
when a flat module and a same-named package coexist in one directory,
`import dashboard.api` **always** resolves to the package, never the flat
file. So `dashboard/api.py` has been fully unreachable dead code since the
split — `dashboard/app.py`'s `from dashboard.api import api_bp` has always
gotten the package's Blueprint, and every route defined inside the stale
`dashboard/api.py` has never once executed.

**Action: delete `dashboard/api.py`.** Zero behavior change (the code is
provably unreachable) — see `DELETE_INSTRUCTIONS.txt` in this delivery.
No code needed in the delta ZIP for a deletion, per the same convention
pass #10 used for `commands.html`.

## Still blocked — carried forward unchanged, need input before proceeding

1. **Event Stack Builder (Phase 6)** — still needs `cogs/minigames.py`
   from Dark to verify against before building anything (not present in
   any upload so far). Either provide it, or confirm greenfield build.
2. **Trade System** — blocked until E3 (ledger) + E4 (inventory) are
   verified LIVE in production. Code-side both are fully built and wired
   — this needs a runtime check on Dark's actual deployment, not
   something verifiable from a ZIP.
3. **Zero automated test coverage** — still open, unaddressed. No small
   next step without direction on what to prioritize first.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: blocked until E3 + E4 verified live in production.
- ZIP = source of truth. Always verify against actual code before
  scheduling work — this pass is a second confirmation of that rule
  paying off.
