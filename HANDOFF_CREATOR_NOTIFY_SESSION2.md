# CREATOR HUB / CREATOR NOTIFY — SESSION HANDOFF
Continuation of `akuroi-handoff-creator-notify.zip` (HANDOFF_TO_AKUROI.md +
PREVIOUS_PASS_NOTES_creator_hub_crud.md). Read those first for the full
history — this note covers only what changed in THIS session.

## 0. Regression found and fixed before anything else

Before touching the requested work, I diffed every file the incoming ZIP
claimed was "unchanged from pass 1" against the actual current project.
Five of them silently dropped the **Bot Profile** feature (nickname/
avatar/banner/bio config) — `cogs.botprofile` missing from `main.py`'s
cog list, the `botprofile` blueprint import missing from
`dashboard/api/__init__.py`, the `/config/botprofile` Flask route missing
from `dashboard/app.py`, the nav link missing from `base.html`, and the
`"botprofile"` entry missing from `utils/permissions.py`'s
`PAGE_PERMISSIONS`. `dashboard/api/botprofile.py` itself (the actual
route handlers) was never touched — only the wiring that registers it
was lost, almost certainly because whichever pass first built the
Creator Hub delta edited full-file copies of those 5 files that predated
Bot Profile landing in the main project.

All five are restored in this delta, alongside the requested Creator Hub
work — Bot Profile and Creator Hub now coexist normally.

## 1. Original scope (Steps 1–3) — done

- **`cogs/youtube.py`** — full rewrite. Added `get_video_live_status()`
  (tri-state YouTube Data API v3 check, matching `cogs/twitch.py`'s
  contract exactly), `is_short()` (unofficial redirect-based Shorts
  classification, fails safe to "not a Short"), and restructured
  `check_videos()` into the two-phase loop from the handoff spec (Phase
  A: is a tracked live video still live; Phase B: did a new RSS video
  show up, and if so is it Live/Short/plain Video). `cog_load()` now
  calls `engine.ensure_tables()`. `/youtube_setup` unchanged in shape,
  now also sets `video_mention_type`.
- **`dashboard/api/creator.py`** — added the three per-type YouTube
  settings endpoints (`/video`, `/shorts`, `/live`), and a
  `/twitch/<id>/settings` edit endpoint (previously the only way to
  change an existing Twitch watch's channel/message/mention was delete
  + re-add).
- **`dashboard/templates/config/creator.html`** — restructured into
  **Uploads** (YouTube channels, each with independent Video/Shorts/Live
  settings panels) and **Live** (Twitch, inline-editable). One deviation
  from the spec worth flagging: YouTube's Live settings panel lives
  inside the same Uploads accordion as Video/Shorts rather than under
  the Live tab — keeps all of one channel's settings in one place rather
  than split across two tabs. The Live tab's YouTube presence is
  implicit (a channel's Live panel is right there in Uploads); happy to
  split it out for real if you'd rather match the spec literally.

## 2. New this session: Creator Groups (consolidated live notifications)

Mid-session you asked for an additional, opt-in notification style:
one shared message per creator identity across platforms, edited in
place as each linked platform goes live/offline, instead of each
platform posting its own message —

```
@everyone 🔴 MeowlyVA is LIVE!
🟣 Twitch — https://twitch.tv/meowlyva
🔴 YouTube — https://youtube.com/live/XVJpU9UgXY4
```

Built as a fully additive layer — nothing about the existing solo
notification path changed or was restructured:

- **`utils/creator_notify_engine.py`** — new `ensure_group_tables()`
  (creates `creator_groups`, `creator_group_sessions`,
  `creator_group_session_lines`, plus a nullable `creator_group_id`
  link column on `youtube_config`/`twitch_config`; wired into the main
  `ensure_tables()`), `get_watch_group()`, `start_watch_tracking()` /
  `stop_watch_tracking()` (group-mode equivalents of
  `start_live_session()`/`end_live_session()`'s row bookkeeping, minus
  sending an independent message), `note_platform_live_grouped()` /
  `note_platform_offline_grouped()` (send-or-edit the shared message,
  add/remove a line). Reuses the *exact same* tri-state detection and
  offline-debounce machinery every cog already had — grouping only
  changes what happens at the two transition points, not how "is it
  live" gets decided.
- **`cogs/twitch.py` / `cogs/youtube.py`** — `_go_live`/`_go_offline`
  (Twitch) and the Phase A/B live branches (YouTube) now check
  `engine.get_watch_group(...)` first. Linked → grouped path. Unlinked
  (`None`, the default for every watch that exists today) → the exact
  same `start_live_session()`/`end_live_session()` call as before this
  feature existed, byte-for-byte.
- **`dashboard/api/creator.py`** — new `/creator/groups` CRUD
  (GET/POST/update/DELETE/toggle) plus `/creator/<platform>/<id>/group`
  to link/unlink an existing watch. Linking is the *only* thing that
  changes a watch's notification style — no separate style dropdown to
  keep in sync with it.
- **`dashboard/templates/config/creator.html`** — new "🔗 Creator
  Groups" tab (create groups, see linked watches), plus a Creator Group
  picker added to every YouTube/Twitch card.

Plain message content (not an embed), matching the reference screenshot.
On full offline (last linked platform drops), the message is edited to
`⚫ **{name} was live**` rather than deleted — same "never delete Discord
history" choice the solo Twitch/YouTube ended-states already make.

## 3. Kick — removed per your direction mid-session

Dropped entirely: `cogs/kick.py` deleted, `cogs.kick` removed from
`main.py`'s load list, the whole Kick section removed from
`dashboard/api/creator.py` (routes + `ensure_kick_table()`), the Kick
card/list/JS removed from `creator.html`, `_migrate_kick_config()` and
every `kick_config` reference removed from the engine, and the now-dead
`kick_setup`/`kick_remove`/`kick_list` entries removed from
`dashboard/app.py`'s `COMMAND_CATEGORIES`. Left alone on purpose: the
unrelated `/kick` **moderation** slash command (kicking a Discord
member, from `cogs/moderation.py`) — same word, nothing to do with the
streaming platform, still listed under `COMMAND_CATEGORIES["Moderation"]`
exactly as before.

`kick_config` was never added to `database.py`'s `init_db()` (it was
always its own `ensure_kick_table()`, following the same pattern as
`utils/trade_engine.py`), so there's no schema entry to unwind there
either.

## 4. What's verified vs. not

Verified this session: every `.py` file `py_compile`-clean, the
template Jinja-parses clean, the embedded JS is syntax-clean (Node
`--check`), and I traced the data flow by hand (ID collisions across
rendered cards, escaping — caught and fixed one real bug myself: an
attribute-escaping helper that only handled `&`/`<`/`>` and would've let
a `"` in a custom message break out of a `value="..."` attribute — every
interpolated field into HTML now goes through a proper 5-character
escaper).

**Not verified: nothing has been run against a live bot or dashboard.**
No Discord gateway, no real `YOUTUBE_API_KEY`/Twitch credentials, no
Flask process in this sandbox. Please run through, in order:

1. Restart the bot once so `database.py`'s `init_db()` + the various
   `ensure_tables()`/migration calls actually execute against your real
   DB (adds the new columns/tables).
2. Confirm `/config/botprofile` loads and the "🪪 Bot Profile" nav link
   is back.
3. Creator Hub → add a YouTube channel, a Twitch streamer; toggle,
   delete, edit settings on each; confirm Video/Shorts/Live panels
   save correctly.
4. Set `YOUTUBE_API_KEY` (Data API v3, `videos.list` scope) if not
   already set, enable Live on a real channel, confirm live detection
   actually fires — this is genuinely new code with no prior live
   testing.
5. Create a Creator Group, link a Twitch watch and a YouTube watch to
   it, go live on both (one after another) — confirm one message gets
   sent then edited to add the second line, and edited again down to
   the "was live" state when both end.

## 5. Files in this delta

```
main.py
utils/permissions.py
utils/creator_notify_engine.py
dashboard/api/__init__.py
dashboard/api/creator.py
dashboard/app.py
dashboard/templates/base.html
dashboard/templates/config/creator.html
cogs/twitch.py
cogs/youtube.py
```

`dashboard/templates/config/announcements.html` is now dead (the route
redirects to `/creator`) — harmless to leave on disk, safe to delete
whenever.
