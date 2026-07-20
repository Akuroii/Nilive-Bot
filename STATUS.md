# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #13)

## Done this pass: Minigames (Event Stack Builder) dashboard UI

`cogs/minigames.py` was previously Discord-command-only. This pass adds
the dashboard CRUD surface for it, reusing the cog's own
`ensure_tables()` / `get_config()` / `get_tiers()` rather than
duplicating schema or query logic.

New/changed files in this delta:

- `utils/permissions.py` — added `"minigames": LEVEL_ADMIN` to
  `PAGE_PERMISSIONS`.
- `dashboard/api/minigames.py` — NEW. Routes:
  `GET/POST /api/minigames/config`, `GET /api/minigames/tiers`,
  `POST /api/minigames/tier`, `DELETE /api/minigames/tier/<id>`,
  `GET /api/minigames/log`. All gated `LEVEL_ADMIN`, all guild-isolated.
- `dashboard/api/__init__.py` — registered the new `minigames`
  submodule alongside the existing seven.
- `dashboard/app.py` — two changes:
  1. `COMMAND_CATEGORIES["Minigames"]` added
     (`minigames_setup`, `minigames_tier_add`, `minigames_tier_list`,
     `minigames_tier_remove`, `minigames_force`, `minigames_stats`) —
     these now show up in the Commands page's per-command toggle list,
     which they never did before.
  2. New `/minigames` route (`systems/minigames.html`) — pulls config,
     tiers, and recent winner log for the page.
- `dashboard/templates/systems/minigames.html` — NEW. Three tabs:
  Config (channel/min-max events/claim window), Reward Tiers (add/list/
  delete, weighted by tier), Recent Winners (read-only log).
- `NAV_LINK_SNIPPET.html` — NOT YET APPLIED. `dashboard/templates/base.html`
  was not re-pasted into this session's context this pass, so I did not
  regenerate the whole file from memory to avoid risking a silent
  diff against your actual live version. This snippet is the exact
  block to paste into base.html's "Systems" nav section (right after
  the `/events` link) — one-line-pattern match to every other nav
  link already there. **Until this is pasted in, `/minigames` is only
  reachable by typing the URL directly** — the page and API both work,
  it's just not in the sidebar yet.

All five new/edited `.py` files `py_compile` clean.

## Verified NOT touched this pass (scope discipline)

- `dashboard/api.py` deletion — still pending, unchanged from pass #12.
  Same zero-risk action as before: delete that one file, nothing else
  changes. Not repeated here since nothing new to say about it.
- No changes to `cogs/minigames.py` itself — it was uploaded again this
  session (`nero_minigames_delta.zip`) and `py_compile` clean; treated
  as unchanged/current, not re-verified line-by-line against the
  version already on record from pass #11/#12 since no diff tool
  against a live repo was available this session (chat-pasted files
  only, no ZIP of the full project).

## Stopped at: Minigames dashboard UI (config/tiers/log + Commands-category entry)
## Still needed, in order:

1. **Apply `NAV_LINK_SNIPPET.html` into `dashboard/templates/base.html`**
   — 30-second manual paste, or hand me the current base.html next
   session and I'll do it directly.
2. **You verify E3 (ledger) + E4 (inventory) live on Railway** — still
   the actual blocker on Trade System, not code.
3. **Live verification of Minigames** — both the original spawn/claim
   mechanics (pass #11/#12) AND this pass's new dashboard page (config
   save round-trips correctly, tier add/delete reflects in Discord's
   next spawn, log populates after a real claim).
4. **Delete `dashboard/api.py`** — queued since pass #12, zero risk,
   just needs someone with repo access to actually remove the file.
5. **Zero automated test coverage** — still open, no small next step
   without direction on what to prioritize first.

## Reminder for next session

This chat has no live copy of the full repo — only whatever gets
pasted into context each session, plus small delta ZIPs like
`nero_minigames_delta.zip` this time (which only contained `main.py`
+ `cogs/minigames.py`, not the dashboard files touched above). If you
want the next session to directly edit `dashboard/templates/base.html`,
`dashboard/api.py` (for deletion), or verify this pass's app.py
against what's actually deployed, paste/upload those specific files —
per project rule, uploaded content is canonical truth over any
handoff note, including this one.

## Design decisions locked (unchanged)

- Prestige: carry-over XP, keep-all level-role rewards, one role per
  tier (swapped), `min_level` default 50, leaderboard sorted
  `prestige DESC, xp DESC`.
- Trade System: blocked until E3 + E4 verified live in production.
- Event Stack Builder: max 5 reward slots per event, hard currency
  caps per tier (bronze/silver/gold/diamond), max 3 active events.
- ZIP = source of truth. Always verify against actual code before
  scheduling work.
