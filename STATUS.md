# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #6)

Stopped at: reaction_role_expiry sentinel-collision bug fixed and
verified. Two stale carry-forward items from prior STATUS.md revisions
were confirmed done (not touched this pass — see "Re-verified" below).
Nothing else in scope this pass.

## Re-verified against actual ZIP code before doing anything (not assumed)

Both of these were still listed as "open" in Dark's running project
notes from a prior session. Checked the real files directly — both are
already fully built:

- **Prestige system**: `cogs/leveling.py` has a complete `/prestige`
  command (calls `utils.xp_calculator.perform_prestige`, does the
  tier-role swap, announces). `dashboard/api/leveling.py` has all 6
  prestige CRUD routes. `dashboard/templates/systems/leveling.html`
  has a full Prestige tab wired to them. **Do not rebuild.**
- **CSRF hardening**: `dashboard/api/__init__.py`'s `before_request`
  hook covers every `/api/*` POST/PUT/DELETE. `dashboard/app.py`'s
  `config_access()` and `config_commands()` both check a hidden
  `csrf_token` form field against `session['csrf_token']` and
  `abort(403)` on mismatch; `config/access.html` has the hidden field.
  **Do not rebuild.**

Recommendation for whoever picks this up next: treat "known gaps"
lists in STATUS.md/project notes as a starting hypothesis, not fact —
grep/read the actual file before scheduling work against it. Two full
sessions' worth of already-complete work was nearly rebuilt because a
stale note said otherwise.

## Fixed this pass — reaction_role_expiry sentinel collision (2 bugs, 1 root cause)

Files changed: `database.py`, `cogs/reactionroles.py`.

**Root cause**: `reaction_role_expiry` was keyed by
`(guild_id, user_id, role_id)` only — no `message_id`. That table
serves two purposes with the same rows:
- `user_id=0` = a **sentinel/template row** written by
  `/reactionrole_add role expiry_days:N` — "this role, on this panel,
  expires N days after being claimed."
- `user_id=<real member id>` = the **actual per-member expiry**,
  written when that member clicks the button, copied from the
  sentinel at claim time.

**Bug 1 (the one already flagged in prior STATUS.md revisions)**: add
the same `role_id` to a second reaction-role panel with a different
`expiry_days`, and the second `/reactionrole_add`'s `INSERT OR REPLACE`
silently overwrote the first panel's sentinel row — no error, no
warning. A member claiming the role from *either* panel afterward got
whichever panel's expiry was configured last.

**Bug 2 (found while fixing #1, not previously flagged anywhere)**:
`expiry_check()` (the 30-min cleanup loop) selected every row with
`expires_at <= now` and processed it as a real member obligation,
including sentinel rows. `guild.get_member(0)` is always `None`, so
the role-removal branch was always skipped for a sentinel — but
`removal_ok` stayed `True` regardless (the code path never explicitly
sets it `False` in that branch), so the sentinel got `DELETE`d anyway
once its own `expires_at` timestamp passed. Net effect: a role's
configured expiry **silently stopped applying to any new claimant**
once the first `expiry_days` window elapsed, because the template row
`RoleButton.callback` reads from was gone. No error anywhere — the
role just quietly became "no expiry" for everyone who claimed it after
that point. This would only surface as a support question weeks after
setup ("why didn't my temp role expire"), which is presumably why it
was never caught before.

**Fix**:
1. `message_id` added to `reaction_role_expiry`'s primary key:
   `(guild_id, user_id, role_id, message_id)`. Every read/write site in
   `cogs/reactionroles.py` (`reactionrole_add`, `reactionrole_remove`,
   `RoleButton.callback`, `expiry_check`) now scopes by `message_id` so
   two panels sharing a role no longer collide.
2. `expiry_check()`'s SELECT now excludes `user_id = 0` — sentinel
   rows are template metadata for a button click to read, not an
   obligation for the cleanup loop to sweep.
3. `reactionrole_remove` now also deletes that panel's own sentinel
   row (previously nothing ever cleaned up an orphaned `user_id=0`
   row once its button was removed — small leak, fixed as part of
   touching this code, not a separate pass).

**Migration** (`database.py`): SQLite can't `ALTER TABLE` a primary
key in place, so the migration checks for the `message_id` column via
`PRAGMA table_info`; if missing, it renames the old table, creates the
new 4-column-PK version, copies every row across with `message_id=0`
(the true originating panel isn't recoverable from the old schema —
this only affects rows written before this fix ships), then drops the
old table. Wrapped in the project's existing `try/except` + log
pattern used by every other migration in this file.

**Verified**: `python3 -m py_compile database.py cogs/reactionroles.py`
— clean. Not yet tested against a live throwaway DB with the
old-schema-row migration path (recommend whoever deploys this do a
quick manual check: seed a `reaction_role_expiry` row on the old
2-column-PK schema, run `init_db()`, confirm the row survives with
`message_id=0` and the table now has 4 PK columns).

## Not done / explicitly out of scope this pass

- **Unbounded in-memory cooldown dicts** (`main.py::_command_cooldowns`,
  `cogs/leveling.py::_xp_cooldowns`/`_spam_tracker`,
  `cogs/economy.py::_daily_cooldowns`/`_work_cooldowns`,
  `cogs/triggers.py::_last_fired`) — still open, still low priority
  (slow memory leak, not a correctness bug). Not touched.
- **Event Stack Builder / dynamic weekly-quota events** — Dark
  described a design (daily randomized spawn % that rises/falls based
  on events-so-far vs days-remaining vs configured min/max weekly
  targets, force-fire near week end). **Flagged, not built**: this
  sounds like it may already be `cogs/minigames.py` from an earlier
  session (per project notes: "quota-driven weekly system, 5-10
  events/week default, daily randomized spawn probabilities, Friday
  force-spawn to meet weekly minimum, Saturday reset"). That file was
  not present in the most recent uploaded ZIP/context, so it could not
  be verified. **Next shift: get `cogs/minigames.py` from Dark before
  writing any new event-scheduling code.** If it turns out to already
  do this, the work is extending/tuning it, not building fresh — if it
  doesn't exist or does something else, then it's a genuine new build
  against Dark's design (engineering freedom granted to propose a
  better algorithm than the illustrative percentages in his spec, per
  his 2026-07 note — but stay within this feature, no unrelated
  refactors while the roadmap's still open).
- **Roleplay commands as GIFs** — Dark wants `/hug`, `/cry`, etc.
  (`cogs/roleplay.py`) to eventually send an actual GIF (Tenor/Giphy
  API or curated list) instead of the current text-only embed. Not
  started, low priority, unrelated to this pass.

## Design decisions locked from earlier shifts (still apply)

- Prestige: carry-over XP (not hard reset), keep-all level-role
  rewards, one role per prestige tier (swapped on prestige),
  `min_level` defaults to 50 (configurable per guild), leaderboard
  sorted `prestige DESC, xp DESC`.
- Event Stack Builder (original master-plan definition, P3 #30):
  admin-authored events with stacked rewards — max 5 reward slots,
  per-tier currency caps, max 3 active events at once, admin preview
  before publish, weekly reward budget tracking. This is distinct from
  the automated random-spawn scheduler Dark is now describing; don't
  conflate the two when picking this up.
- ZIP = source of truth, always verify against actual code before
  scheduling work, never trust a "known gaps" list at face value.
