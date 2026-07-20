# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #14)

## Done this pass: applied NAV_LINK_SNIPPET.html into base.html

`dashboard/templates/base.html` was pasted into context this session, so
the pending nav link from pass #13 is now applied directly instead of
staying a manual TODO. Added the `/minigames` link into the "Systems"
nav-section, right after `/events` and before `/ledger` — exact same
`hx-get`/`hx-target`/`hx-swap`/`hx-select`/`hx-push-url` pattern as
every other nav-link in that block. No other part of base.html touched.

`/minigames` (Event Stack Builder) is now reachable from the sidebar,
not just by typing the URL. This closes STATUS.md item #1 from pass #13.

File in this delta:
- `dashboard/templates/base.html` — nav link added (HTML only, no
  Python changes, nothing to py_compile).

## Verified NOT touched this pass (scope discipline)

- No changes to `dashboard/api/minigames.py`, `cogs/minigames.py`,
  `dashboard/templates/systems/minigames.html`, or `dashboard/app.py` —
  all confirmed already correct from pass #13, re-verified by reading
  (not rebuilt).
- `dashboard/api.py` deletion — still not done. Not attempted this pass
  because that file was not present anywhere in this session's context
  (only the new `dashboard/api/*.py` package files were), so there is
  nothing to safely diff/delete against. Needs someone with repo access
  to remove it directly, or upload it so a session can confirm it's
  dead before deleting.

## Stopped at: nav link applied — Minigames/Event Stack Builder dashboard work is now fully complete end-to-end (backend routes, tables, page, nav entry).

## Still needed, in order (all blocked on Dark, not on code):

1. **You verify E3 (ledger) + E4 (inventory) live on Railway** — the
   actual blocker on Trade System. Nothing to build until this is
   confirmed.
2. **Live verification of Minigames** — spawn/claim mechanics (pass
   #11/#12) AND the dashboard page (config save round-trips, tier
   add/delete reflects in Discord's next spawn, log populates after a
   real claim, and now that it's in the nav — that it actually renders
   from the sidebar link).
3. **Delete `dashboard/api.py`** — queued since pass #12, zero risk,
   just needs someone with repo access to actually remove the file (or
   upload it here so a session can do it directly).
4. **Zero automated test coverage** — still open, no small next step
   without direction on what to prioritize first.

No further code work is actionable right now without one of the above
from you — everything else in the locked plan (Trade System, Missions)
is explicitly gated on item 1.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: blocked until E3 + E4 verified live in production.
- Event Stack Builder: max 5 reward slots per event, hard currency
  caps per tier (bronze/silver/gold/diamond), max 3 active events.
- ZIP = source of truth. Always verify against actual code before
  scheduling work.
