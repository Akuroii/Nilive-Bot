# NILIVE BOT — SHIFT CHECKPOINT (dark-fixes pass #2)

Stopped at: `scheduled_messages` feature complete and tested. This is
the last item of the "3 half-built dead tables" cleanup group. Nothing
further from the original audit list has been started yet (see
"Next objectives" below).

## ⚠️ Read this first — how this zip was assembled

This zip is **pass #1 + pass #2 merged**, not just pass #2 in
isolation. Context: pass #1's fixes (SECRET_KEY fail-fast, stored XSS
escaping, CSRF hardening, XP/prestige race conditions, WAL mode) were
delivered as a standalone delta zip and were verified correct against
the actual code at the time — but they had **not yet been merged into
the full project repo** when pass #2 started. Before doing any pass #2
work, that merge was done first (diffed pass #1's files against the
full repo to confirm no unrelated drift, then merged). If you're
diffing this zip against an older copy of the full repo, expect to see
both passes' changes together.

## Verification methodology (both passes)

Every fix in this checkpoint was checked against the **actual code**,
not assumed from a prior shift's notes — a couple of the original
audit's claims turned out to be wrong on inspection (see below), and
one already-shipped comment was found to be technically inaccurate.
Where a claim couldn't be verified by reading, it was tested by
running the actual code against a throwaway SQLite DB with fake
Discord objects (bot/channel stubs) — not just `py_compile`.
**This caught two real bugs that code review alone did not catch**
(see "Bugs caught by testing" below). Recommendation for future shifts
handling this codebase: run it, don't just read it, whenever the logic
has more than one branch.

## Built/fixed this shift (pass #2)

1. **CDN Subresource Integrity** (`dashboard/templates/base.html`)
   Added `integrity`/`crossorigin` attributes to the select2 CSS,
   htmx, jquery, and select2 JS `<script>`/`<link>` tags. Hashes were
   **computed from the actual published npm packages** (`npm pack` +
   `openssl dgst -sha384`), not guessed or copied from a search
   result — guessing here would silently break the dashboard's JS/CSS
   if wrong. Google Fonts link left alone (standard practice; Google's
   font URLs aren't stable SRI targets).

2. **`Triggers.ensure_table()` hot path** (`cogs/triggers.py`)
   Was running on every single guild message. Moved to `cog_load`
   (runs once). Verified `triggers` table is already created by
   `database.py::init_db()` before the bot comes online, so the
   per-message call was pure overhead.

3. **`CustomCommands.ensure_table()` hot path** (`cogs/customcommands.py`)
   Same fix, same reasoning — was running on every `!`-prefixed
   message.

4. **`EmbedBuilder.ensure_table()` duplication** (`cogs/embedbuilder.py`)
   Not a hot-path issue (only in slash commands, not `on_message`), so
   this was cleanup, not a perf fix. Consolidated to `cog_load`; left
   the per-command calls in place as free defense-in-depth since
   `IF NOT EXISTS` makes them no-ops now.

5. **`Sticky.ensure_table()` — intentionally NOT touched.**
   The original audit claimed all these tables were already created
   centrally by `database.py::init_db()`, making the per-cog calls
   pure duplication. **That claim was false for `sticky_messages`** —
   it does not exist anywhere in `database.py`. `sticky.py`'s
   `ensure_table()` (in `on_ready`) is the *only* place that table
   gets created. Following the audit's original recommendation here
   would have broken the sticky-message feature on next fresh deploy.
   Left completely alone. If someone wants this cleaned up too, the
   correct fix is to add `sticky_messages` to `database.py`'s central
   schema first, then remove the cog-level call — not the other way
   around.

6. **Orphaned template deleted**: `dashboard/templates/config/commands.html`
   Confirmed zero references in any `.py` route or `.html` include
   before deleting (real `manage/commands.html` is what's actually
   rendered).

7. **Dead schema removed from `database.py`**: `voice_sessions`,
   `ticket_config`, `disabled_commands`. All three confirmed
   genuinely dead (only comments referencing the old names remained,
   no actual queries) before removal. Not touching any already-
   deployed DB file's existing tables — this only stops a fresh DB
   from creating them going forward.

8. **`boost_color_roles` feature — built** (extended `cogs/boost.py`,
   did not create a new file since this belongs with the existing
   boost-tier logic).
   - `/boostcolor_add`, `/boostcolor_remove`, `/boostcolor_list`
     (admin) — configure which roles are pickable and at what boost
     level.
   - `/boostcolor` (booster-facing, autocomplete filtered to roles
     the invoking member is currently eligible for by boost count).
   - Single-select: picking a new color swaps out any other
     configured color role the member currently holds.
   - Auto-strips the color role on unboost, matching how the existing
     boost1/boost2 tier roles already behave (same
     `auto_remove_on_unboost` guild setting).

9. **`backup_log` feature — built** (new `cogs/backup.py`).
   - Daily automated backup via `aiosqlite.Connection.backup()` (the
     real SQLite online-backup API) — deliberately not a raw file
     copy, since copying a live WAL-mode DB file directly can capture
     the main file and WAL out of sync and produce a corrupt backup.
   - Prunes to the most recent 7 backups (files + `backup_log` rows).
   - `/backup_now` (manual trigger), `/backup_list` (recent backups)
     — both owner-only via the codebase's existing `bot.is_owner()`
     check (same mechanism `/sync` and `/reload` already use in
     `main.py`), not a new ad-hoc permission check.
   - Backups land in `<DB_PATH's dir>/backups/`, i.e. on Railway's
     persistent volume alongside the live DB — survives process
     restarts/redeploys, does NOT survive the volume itself being
     deleted (would need off-volume storage for that; out of scope).

10. **`scheduled_messages` feature — built** (new `cogs/scheduler.py`).
    - Background loop checks every 60s for due messages.
    - `/schedule_message` — relative (`30m`/`2h`/`3d`) or absolute
      (`YYYY-MM-DD HH:MM`) UTC time; optional repeat (hourly/daily/
      weekly + interval).
    - `/schedule_list`, `/schedule_cancel`.
    - If the bot was offline past a repeating message's slot, it
      rolls forward to the next future occurrence instead of
      burst-firing every missed one on reconnect.
    - `embed_data` column is wired up (stored as JSON, rendered via
      `discord.Embed.from_dict` at send time) but nothing currently
      writes to it — no command yet builds a scheduled embed. Text
      messages are fully functional today.

## Bugs caught by testing (not by reading the code)

Both of these compiled fine and looked correct on read-through — they
only surfaced by actually running the logic:

- **`backup.py`**: filenames used 1-second-resolution timestamps.
  Two backups in the same second (e.g. `/backup_now` fired twice
  quickly) collided on disk, and — worse — the prune logic then
  deleted a surviving row's file because an older row shared its
  filename, wiping every backup instead of just the old ones. Fixed
  with a uuid suffix; re-tested and confirmed 7-in/7-out with every
  logged row matching a real file on disk.
- **`scheduler.py`**: a failed send (deleted channel, missing perms,
  etc.) still advanced the row's state — a one-off got marked
  "sent"/disabled and a repeating message rolled its `send_at`
  forward, even though nothing was actually delivered. The message
  was silently lost with only a console log line as a trace. Fixed so
  state only advances on a confirmed successful send; a failed send
  now retries next tick instead of disappearing.

## Known inaccuracy corrected (pass #1 → pass #2)

`database.py`'s WAL-mode comment claimed `busy_timeout` persists
file-wide like `journal_mode` does. It doesn't — `busy_timeout` is
per-connection in SQLite, so the other dozens of `aiosqlite.connect()`
call sites elsewhere in the codebase don't actually inherit it. Not a
live bug only because `aiosqlite`/`sqlite3` already default new
connections to a 5-second timeout on their own. Comment corrected in
place to explain the real mechanism rather than the wrong one.

## Next objectives (not started)

Roughly in priority order:

1. **`run_async` per-request event loop** (dashboard side). Flask
   routes that hit the DB currently spin up a fresh asyncio event loop
   per request to call the async `aiosqlite` code. Under real
   concurrent dashboard traffic this is wasteful and has known
   footguns (event loop churn, potential leaks). Needs a decision on
   approach before touching code — options roughly are: (A) a
   persistent background event loop the Flask app dispatches onto,
   (B) a thread pool + `asyncio.run` per call (current approach,
   basically), (C) drop `aiosqlite` on the dashboard side entirely and
   use synchronous `sqlite3` there instead, since Flask routes don't
   need to be async. (C) is probably the least risky and most
   mechanical, but it touches every DB call site in `dashboard/`
   (`app.py` at 1763 lines, `api.py` at 2159 lines) — this is real
   scope, not a quick fix. Get a decision on approach before starting.

2. **Split `dashboard/api.py` (2159 lines) into blueprints.**
   Structural refactor, not a bug fix — pure maintainability. Higher
   risk of introducing a regression than anything done so far this
   shift just by virtue of size. Should be its own dedicated pass with
   care taken to test each moved route, not something to rush through
   inside a larger batch of unrelated fixes.

3. Everything else from the original audit not explicitly listed here
   is either done (see above) or was never part of the list.

## Design decisions locked from earlier shifts (still apply)

- Prestige: carry-over XP (not hard reset), keep-all level-role
  rewards, one role per prestige tier (swapped on prestige),
  `min_level` defaults to 50 (configurable per guild), leaderboard
  sorted `prestige DESC, xp DESC`. See prior STATUS.md history if this
  file gets truncated again — these were deliberate product decisions,
  not defaults to second-guess.
