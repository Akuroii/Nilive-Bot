# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #9)

Stopped at: pass #8's 3 changes + 1 more this pass (`reopen_ticket()`
fix, requested explicitly by Dark after pass #8's check-in flagged
it). Delta ZIP = 2 files (cogs/tickets.py, STATUS.md) + 1 deletion
instruction below (no code to ship for a deletion).

## Re-verified against actual code before doing anything (not assumed)

Confirmed pass #7's 3 fixes are real and present in the uploaded ZIP —
not re-verifying them further this pass (dashboard_users unique index,
per-guild economy cooldowns, voice-XP blacklist check all read exactly
as pass #7's STATUS.md described).

## Fixed this pass (picked up pass #7's flagged "not done" item)

### Ticket permission bypass when `staff_role_id` is null — cogs/tickets.py

Pass #7 flagged this as needing investigation before touching it:
"no staff role configured = anyone can claim/close/delete" — bug or
intentional fallback? Investigated by comparing against
`close_ticket()`, which already has a documented, deliberate fallback
chain (`is_owner or is_staff or is_category_closer or is_admin` —
`is_admin` = `manage_channels`) for exactly this "no staff role
configured" case. `claim_ticket()` and `delete_ticket()` had NO such
fallback: `if row and row[0]:` skipped the entire permission check
when `staff_role_id` was falsy, meaning any member who could see the
ticket channel could claim it or **permanently delete it** — not an
intentional design decision, just missing the same fallback
`close_ticket()` already has.

**Fix**: both now `elif not interaction.user.guild_permissions.manage_channels:`
after the existing staff-role branch, denying with the same message as
the staff-role case. Matches `close_ticket()`'s existing `is_admin`
fallback exactly — no new permission model introduced.

**Not touched (scope discipline)**: `reopen_ticket()` (in
`ClosedTicketView`) has zero permission check at all — not flagged in
pass #7's note (which named claim/close/delete specifically), and
reopening is lower-severity than claim/delete (visible, reversible via
Close again). Flagging it here rather than fixing it now to stay
inside this pass's scope — pick up next if wanted.
`add_member()`'s button has the same "no fallback" shape, but the
actual privileged action (`/ticket_add`) is already gated by
`manage_channels` at the command level, so the button merely lets a
non-staff member see a text hint — cosmetic, not a real bypass, left
alone.

**Verified**: `python3 -m py_compile cogs/tickets.py` clean.

## Fixed this pass (#9) — the item flagged, not fixed, in pass #8

### `reopen_ticket()` had zero permission check — cogs/tickets.py

Flagged in pass #8 as lower-severity and deliberately left alone to
keep that pass small; Dark asked to fix it, so: any member who could
see a closed ticket channel could click **Reopen** with no gate at
all — no owner check, no staff check, no admin check, nothing.

**Fix**: now runs the exact same fallback chain `close_ticket()`
already uses — ticket owner, guild-wide `support_role_id`, the
ticket's category's own `closer_roles`, or `manage_channels` — instead
of inventing a separate permission model for reopening. Needed one
extra column in the existing `tickets` SELECT (`category`, alongside
`user_id`/`staff_role_id` it already fetched) to look up the
category's closer roles the same way `close_ticket()` does.

**Verified**: `python3 -m py_compile cogs/tickets.py` clean (checked
after both this and the pass #8 edits landed in the same file).

## Action needed — not shippable as a code change

`dashboard/templates/config/commands.html` — re-confirmed against the
latest uploaded context: still present, still zero references in
`dashboard/app.py` (`/commands` renders `manage/commands.html`;
`/config/commands` GET redirects to `commands_dashboard`, never
renders this file directly). HANDOFF_NOTES.md's claim that this was
already deleted does not match what's actually in the repo Dark
uploaded. This is a template file with nothing to diff/merge — just
delete it:

```
rm dashboard/templates/config/commands.html
```

## Not done / still open

- `add_member()`'s ticket button still has no fallback check when no
  staff role is configured — left alone in both passes #8 and #9
  since the actual privileged action (`/ticket_add`) is already gated
  by `manage_channels` at the command level; the button only lets a
  non-staff member see a text hint, cosmetic not a real bypass.
- Unbounded in-memory cooldown dicts (main.py, triggers.py) — still
  low priority, not touched.
- Event Stack Builder / dynamic weekly-quota events — still needs
  `cogs/minigames.py` from Dark to verify before building anything.
- Roleplay GIFs — low priority, unrelated.
- Zero automated test coverage — still open.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: blocked until E3 (ledger) + E4 (inventory) verified
  live in production — both already built per earlier passes.
- ZIP = source of truth. Always verify against actual code before
  scheduling work.
