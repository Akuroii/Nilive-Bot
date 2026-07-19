# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #10)

## This pass: verification only, no functional changes

Cross-checked every item in the "outstanding known issues" list (memory /
prior handoffs) against the actual uploaded ZIP before doing anything —
per project rule, ZIP is truth over notes. Result: everything except the
orphaned template was already fixed in prior passes and is confirmed
present in this codebase:
- CSRF gap on /config/access + /config/commands: fixed (session csrf_token checked)
- dashboard_users missing UNIQUE constraint: fixed (idx_du_unique + dedup migration)
- Cross-guild cooldown leakage (economy /daily, /work): fixed (guild_id in cooldown key)
- Ticket permission bypass when staff_role_id null: fixed (claim/close/delete/reopen)
- XP blacklist roles not applying to voice XP: fixed (is_role_blacklisted gate added)
- run_async() new event loop per request: fixed (persistent background loop)
- COMMAND_CATEGORIES["Leveling"] missing resetleaderboard/prestige: both present
- Triggers fuzzy_threshold + anti-repeat cooldown: fully built, not 80%

## Action taken this pass

`dashboard/templates/config/commands.html` — deleted. Re-confirmed zero
references in dashboard/app.py (`/commands` renders manage/commands.html;
`/config/commands` POST/GET never renders this file). This closes the
loop flagged across three prior STATUS.md revisions and HANDOFF_NOTES.md's
premature "already deleted" claim.

## Still blocked — need input before proceeding

1. **Event Stack Builder (Phase 6)** — blocked per this file's own prior
   note: needs `cogs/minigames.py` from you to verify existing patterns
   before building anything. Not in this upload. Either provide it, or
   confirm it doesn't exist yet (greenfield build).
2. **Trade System** — blocked until E3 (ledger) + E4 (inventory) are
   verified LIVE in production. Code-side both are fully built and wired
   (utils/ledger.py, utils/inventory.py, integrated into economy_safe.py,
   reward_engine.py, shop.py, events.py, dashboard pages) — but "wired in
   code" isn't the bar your own rule sets. This needs a runtime check on
   your VM (real ledger rows / inventory rows populating correctly), not
   something verifiable from a ZIP.
3. **Zero automated test coverage** — still open, unaddressed. Large
   scope, no small next step without direction on what to prioritize
   (economy transactions? permission gates? ticket flow?).

## Previous checkpoint (dark-fixes pass #9) — preserved for reference

Stopped at: pass #8's 3 changes + 1 more (`reopen_ticket()` fix). Delta
ZIP = 2 files (cogs/tickets.py, STATUS.md) + 1 deletion instruction (no
code to ship for a deletion).

### Fixed pass #9 — the item flagged, not fixed, in pass #8

`reopen_ticket()` had zero permission check — cogs/tickets.py. Fixed to
run the same fallback chain `close_ticket()` uses — ticket owner,
guild-wide `support_role_id`, the ticket's category's own `closer_roles`,
or `manage_channels` — instead of inventing a separate permission model.

### Not done / still open (pre-existing, carried forward)

- `add_member()`'s ticket button still has no fallback check when no
  staff role is configured — left alone deliberately; the actual
  privileged action (`/ticket_add`) is already gated by `manage_channels`
  at the command level, so the button only lets a non-staff member see a
  text hint — cosmetic, not a real bypass.
- Unbounded in-memory cooldown dicts (main.py, triggers.py) — low
  priority, not touched.
- Roleplay GIFs — low priority, unrelated.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: blocked until E3 (ledger) + E4 (inventory) verified
  live in production — both already built per earlier passes.
- ZIP = source of truth. Always verify against actual code before
  scheduling work.
