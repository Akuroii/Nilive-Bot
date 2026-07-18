# NILIVE BOT — SHIFT CHECKPOINT
Updated: this shift — "Dark fixes" pass #1 (security-critical items from
the full-project audit). ZIP delivered contains ONLY changed files —
merge into your existing tree, do not treat this as a full project drop.

## ⚠️ Read this first
- This ZIP is a DELTA. Files not listed under "Files changed this
  shift" below are UNTOUCHED — pull them from your last full ZIP.
- A full-project audit was done this shift (no ZIP/repo access — audit
  was performed against pasted file contents, see caveat below) and
  produced a 🔴/🟠/🟡/🟢 prioritized list. This shift closed the
  highest-severity items from that list. Remaining items are in
  "Next Steps" below, already re-prioritized per Dark's review.
- **Correction to this shift's own earlier claim** (Dark caught this,
  logging it so it doesn't get miscited later): the audit originally
  described `perform_leaderboard_reset` as able to leave the DB
  "half-archived, half-zeroed" on a mid-loop crash. That's WRONG.
  aiosqlite/sqlite3 opens an implicit transaction on the first write
  and holds it until `db.commit()` — nothing in that function was
  durable until the single commit at the end, so a crash rolled back
  everything, not partial data. There was no corruption risk. The
  real, correctly-identified issue was: no explicit transaction
  boundary means no fail-fast on lock contention, and relying on
  implicit-transaction behavior instead of stating the boundary
  explicitly is fragile / non-obvious to future readers. The fix
  applied (explicit `BEGIN IMMEDIATE` + `ROLLBACK`) is still correct
  and worth keeping, just don't repeat the "data corruption" framing.
- **Unverified, do not act on yet**: the audit flagged root-level
  `dashboard.js` / `base.html` files as possibly containing swapped/
  mismatched content vs. their real counterparts under
  `dashboard/static/js/` and `dashboard/templates/`. This was NOT
  confirmed against your actual repo — it may just be an artifact of
  how files were pasted into the audit conversation. **Next shift:
  run `git status` and `git show HEAD:dashboard.js` /
  `git show HEAD:base.html` (or just check the working tree) BEFORE
  spending any time on this.** If those two root-level files don't
  actually exist in your repo, ignore this entirely.

## Files changed this shift (delta — this ZIP)
- `database.py` — added WAL mode + busy_timeout pragma (bonus, low-risk perf fix)
- `utils/reward_engine.py` — fixed XP-grant race condition (lost-update bug)
- `utils/xp_calculator.py` — fixed matching race condition in `perform_prestige`
- `cogs/leveling.py` — wrapped `perform_leaderboard_reset` in explicit transaction; clamped `/setxp` to non-negative
- `dashboard/app.py` — SECRET_KEY fail-fast at startup; CSRF token check added to `/config/commands` and `/config/access` POST routes; clamped `/api/edit-member` xp/coins to non-negative
- `dashboard/api.py` — escaped all user-controlled strings in hand-built HTML htmx-partial responses (stored XSS fix, several endpoints); hardened `/moderation/quick-action` (type/range validation on target_id, duration, delete_days) and capped `massban` at 25 users/request
- `dashboard/templates/config/access.html` — added hidden `csrf_token` field to both forms (add-user, remove-user), matching the app.py check above
- `STATUS.md` — this file

## 🔴 Critical items CLOSED this shift
1. **Hardcoded fallback `SECRET_KEY`** (`dashboard/app.py`) — was falling
   back to a source-controlled literal string when the env var wasn't
   set, letting anyone who read the source forge a valid signed session
   cookie (including CSRF token and identity) for ANY user, no login
   required. Now fails hard at startup if `SECRET_KEY` is unset, warns
   if it's under 32 chars. **You must set `SECRET_KEY` in Railway's
   Variables tab before this will boot** — generate one with:
   `python -c "import secrets; print(secrets.token_hex(32))"`
2. **Stored XSS in `dashboard/api.py`'s hand-built HTML responses** —
   several htmx-partial endpoints (`moderation_logs_partial`,
   `audit_log_partial`, `shop_purchase_history`, `shop_items_partial`,
   `tickets_partial`) interpolated user-controlled strings (Discord
   display names, moderation reasons, shop item names/descriptions,
   audit log details) directly into f-string HTML with no escaping —
   these bypass Jinja's autoescaping entirely since they're returned as
   raw strings, not rendered templates. A malicious/compromised display
   name or reason could execute arbitrary JS in an admin's browser the
   next time they viewed that tab. All identified call sites now run
   values through `markupsafe.escape()` before interpolation.
3. **XP-grant race condition** (`utils/reward_engine.py`) — the xp
   branch of `give_reward()` did an unguarded read-then-write; two XP
   grants landing close together (message XP racing a voice-XP tick,
   or either racing an XP-boost purchase) could silently lose one
   grant (classic lost-update) and potentially skip a level-up's
   role/currency reward. Now wrapped in `BEGIN IMMEDIATE`, same pattern
   `utils/economy_safe.py` already uses for coins/diamonds.
4. **Same race condition in `perform_prestige`** (`utils/xp_calculator.py`,
   found while fixing #3, same class of bug) — two `/prestige` calls
   (or a `/prestige` racing a dashboard XP edit) could both read stale
   xp/level and one could silently overwrite the other's result. Same
   `BEGIN IMMEDIATE` fix applied.
5. **`perform_leaderboard_reset` explicit transaction boundary**
   (`cogs/leveling.py`) — see correction above re: actual risk (lock
   contention / fail-fast, not corruption). Fixed anyway since it's the
   right pattern for a function that destroys XP data.

## 🟠 High-priority items CLOSED this shift (pulled forward, not originally critical)
6. **Form-based CSRF on `/config/access`** — this was flagged as open
   across 3+ prior STATUS.md revisions. Closed: hidden `csrf_token`
   field added to both forms in `config/access.html`, checked
   server-side in `app.py`'s `config_access()` POST handler.
7. **Form-based CSRF on `/config/commands`** — same fix applied
   defensively at the route level, though note: the *live* commands
   page (`manage/commands.html`, rendered by `/commands`) already uses
   `fetch()`/JSON (already covered by the existing `/api/*` CSRF hook).
   The plain-form POST route `/config/commands` itself still exists
   and is directly reachable, so it's now hardened too, even though no
   current template posts to it via a classic form. (See "orphaned
   template" note in Next Steps — this is related but not fully
   resolved yet.)
8. **`/moderation/quick-action` input validation** — `target_id`,
   `duration_seconds`, `delete_message_days` are now type/range checked
   before being formatted into Discord API calls or DB writes.
9. **`massban` unbounded batch size** — capped at 25 user IDs per
   request to avoid hammering Discord's ban endpoint and risking a
   rate-limit ban on the bot's own IP.
10. **Admin-supplied negative XP/coins** — `/api/edit-member` and
    `/setxp` now clamp to non-negative before writing, since
    `xp_progress()`/the rank card assume `xp >= 0`.

## Re-prioritized per Dark's review (do these next, in this order)
Dark's read, which I agree with: the newly-found SECRET_KEY fallback
and the api.py stored XSS were more urgent than the form-CSRF work that
was next-in-queue going into this shift — both are directly exploitable
today with no third party required, whereas CDN SRI (item below) needs
an actual CDN compromise first. Re-ranked accordingly:

1. ~~SECRET_KEY fail-fast~~ ✅ done this shift
2. ~~Stored XSS in api.py~~ ✅ done this shift
3. ~~Form CSRF on /config/access, /config/commands~~ ✅ done this shift
4. **CDN Subresource Integrity** (downgraded High→Medium per Dark —
   real gap, but requires a CDN compromise to exploit, unlike #1/#2
   above). Add `integrity=`/`crossorigin=` to the jQuery/htmx/Select2
   `<script>`/`<link>` tags in `base.html`, or self-host them.
5. **Verify the dashboard.js/base.html root-file question** (see
   "Unverified" note above) — 5-minute `git status` check, do this
   before anything else on the list below since it's either a non-issue
   or something that needs immediate attention depending on what's
   actually in the repo.
6. Move `Triggers.ensure_table()` out of the per-message hot path
   (currently runs a full `CREATE TABLE IF NOT EXISTS` on every single
   guild message — call it once in `cog_load`/`on_ready` instead).
7. Delete duplicate `ensure_table()` calls in `customcommands.py`,
   `sticky.py`, `embedbuilder.py` — `database.py::init_db()` already
   creates all of these; having two schema sources per table is a
   maintenance trap even though `IF NOT EXISTS` makes it harmless today.
8. Delete confirmed-orphaned `dashboard/templates/config/commands.html`
   (no route renders it — flagged 3+ sessions running, still not
   deleted). NOTE: the `/config/commands` POST *route* in `app.py` is
   still live and reachable directly even with the template gone — it's
   now CSRF-hardened (item #3 above) regardless of what happens to the
   template.
9. Drop or finish half-built dead tables: `voice_sessions`,
   `ticket_config`, `disabled_commands` (confirmed unreferenced — pick
   delete), vs. `boost_color_roles`, `scheduled_messages`, `backup_log`
   (schema exists, no read/write path anywhere — pick finish-or-drop
   per table, don't leave them in limbo).
10. `run_async()` creates a new event loop per Flask request — real
    but lower-urgency perf issue. Options compared in the original
    audit: (A) persistent background loop + `run_coroutine_threadsafe`,
    (B) migrate dashboard to Quart/FastAPI, (C) drop aiosqlite on the
    dashboard side entirely since Flask here is sync anyway. Recommend
    (C) short-term, (B) if the project grows.
11. Split `dashboard/api.py` into per-domain blueprints (moderation/
    leveling/shop/tickets) — it's ~1400 lines and growing.

## What's Actually Done (Verify against ZIP, not notes) — unchanged from prior shift
✅ E1 Activity Tracking, E2 Reward Engine, E3 Ledger, E4 Inventory
✅ Economy v2 (dual currency, /convert)
✅ Leveling rewards (coins/diamonds/roles), XP boost items, leaderboard resets
✅ Server permission gating, bot invite flow (super-admin only)
✅ Prestige system (schema + /prestige command + dashboard UI) — now also
   race-condition-hardened this shift, see item #4 above
⏳ CSRF: app-level /api/* done (prior shift), form-based /config/access +
   /config/commands done (THIS shift)

## DO NOT REBUILD (Trust ZIP over notes)
Activity engine, Reward engine, Ledger, Inventory, Economy v2, Leveling
core, Prestige core. If in doubt, diff against this delta ZIP's file
list first — anything not listed under "Files changed this shift" is
exactly what it was in the last full ZIP.

## Hosting
Railway. Dockerfile-based. **Action required after this ZIP**: set
`SECRET_KEY` in Railway's Variables tab (see item #1 above) or the
dashboard process will refuse to start — this is intentional, not a bug.

## Rules (unchanged)
- ZIP = canonical truth. This delta supersedes only the files listed above.
- Do NOT rebuild anything under "DO NOT REBUILD."
- Real repo paths only (no patch subfolders) — this ZIP mirrors real
  paths (e.g. `dashboard/api.py`, `cogs/leveling.py`) so it can be
  dropped straight into the tree.
- py_compile everything before shipping (not run this shift — no
  Python/discord.py/flask environment was available for verification;
  next shift should `python3 -m py_compile` all five changed .py files
  before deploying, especially `dashboard/api.py` given its size).
- ZIP at ~70-80% usage with a "Stopped at / Still needed" note — this
  note is that note.

## Suggested next step for next shift
Work the re-prioritized list above in order, starting with the
`git status` verification (#5) since it's fast and unblocks knowing
whether item is real. Then CDN SRI (#4), then the maintainability
cleanup items (#6-9), then the bigger structural ones (#10-11) if time
allows. Full original audit (all 🔴🟠🟡🟢 items, architecture read,
creative suggestions, health scorecard) is preserved in this shift's
conversation history if the next shift needs the complete list —
ask Dark for a copy if it's not already saved somewhere durable.
