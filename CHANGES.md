# Super-Admin Bypass — Delta ZIP

Developer ID: `704453350384730237`

## Files in this ZIP
Replace these paths wholesale in the repo — don't mix with the old versions,
several function signatures changed together across files:

- `database.py`
- `main.py`
- `dashboard/permissions.py`
- `dashboard/app.py`
- `dashboard/auth.py`
- `dashboard/templates/server_select.html`

## What changed

1. **Real per-guild server owners** are auto-granted a normal, visible
   `owner` row in Dashboard Access for their own server only — on join
   (`main.py`'s `on_guild_join`), and backfilled on every bot connect for
   servers already joined (`main.py`'s new `backfill_guild_owners()`).

2. **The developer** (`704453350384730237`) gets full `owner`-level access
   in every server, checked before any `dashboard_users` lookup happens —
   never written to that table, never shown in Current Access
   (`dashboard/permissions.py`'s new `is_trusted_super_admin`).
   `SUPER_ADMIN_USER_IDS` (optional env var, comma-separated) can add more
   trusted IDs later without another code change.

3. **"Select a Server"** now shows every server the bot is in when the
   logged-in user is the developer, not just servers they personally
   belong to on Discord (`dashboard/auth.py`'s new `fetch_bot_guilds_full`,
   `dashboard/app.py`'s `server_select()`).

4. **One-time migration**, runs automatically inside `init_db()` on the
   next boot of either process: removes the stale `auto-setup` row the old
   `ensure_owner_access()` (now deleted) had written for the developer in
   every guild. This is what clears the row visible in Current Access
   today — no manual "Remove" click needed.

## Deploy notes

- No env var changes required — `OWNER_ID` stays what it is.
- Restart both the bot and dashboard processes after deploying, so the
  migration (`init_db()`) and the owner backfill (`main.py`'s `on_ready`)
  actually run.
- All 6 files `py_compile`-clean, verified against the live
  `Akuroii/Nilive-Bot` repo before packaging.
