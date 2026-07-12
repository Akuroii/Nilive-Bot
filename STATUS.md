# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Critical Debug Fix shift complete, well under token limit.

## Critical Debug Fix (this shift)
Scope: startup/offline diagnosis on Railway. Did NOT touch Economy v2
(Dark's Phase 5 work) or any other system.

- **dashboard/app.py**: NO CHANGE NEEDED. Port binding already uses
  `port = int(os.getenv('PORT', 5000))` with `import os` present at
  module scope — this was already correct in the uploaded ZIP, verified
  by inspection before touching anything.
- **main.py**: added `validate_discord_token()`, called before
  `bot.start()`. Checks token is present/non-empty (hard fail + exit(1)
  with a clear log block if missing) and does a cheap shape check
  (3 dot-separated segments, length >= 50) that only warns, since it's
  not a real validity check — Discord itself still validates the token
  on connect. Also added an explicit `except discord.LoginFailure`
  branch around `bot.start()` with an actionable log block (previously
  fell through to the generic `except Exception` handler with a bare
  `FATAL ERROR: {e}` line). Added two startup log lines: token check
  result, and intents status (`message_content`, `members`,
  `presences`) before the bot attempts to connect.
- **DEBUG_GUIDE.md**: new. Covers enabling privileged intents in the
  Developer Portal, verifying `DISCORD_TOKEN` in Railway Variables,
  reading Railway logs for the new log lines above, and
  restart/verify steps.
- Compiled clean: `python3 -m py_compile main.py` → exit 0.

Files touched this shift: `main.py`, `STATUS.md`, `DEBUG_GUIDE.md` (new).
No patch subfolder used — per standing project rule, files map directly
to their real repo paths.

## Phase 3 status (unchanged this shift)
E1 ✅ E2 ✅ E3 ✅ E4 ✅ — all four shared engines verified complete,
per prior shift's audit. Not touched this shift.

## Trade System — still BLOCKED (unchanged)
Pending live production verification of E3 + E4. Not addressed this
shift — this was a startup/deploy debug fix only.

## NOT included in this ZIP (unchanged — pull from your last full ZIP)
Everything except main.py, STATUS.md, DEBUG_GUIDE.md:
- database.py, dashboard/app.py, dashboard/auth.py,
  dashboard/permissions.py, dashboard/api.py, dashboard/utils/*
- utils/* (reward_engine.py, ledger.py, inventory.py, economy_safe.py,
  permissions.py, xp_calculator.py, formatters.py)
- cogs/* (all cogs — untouched)
- dashboard/templates/*, dashboard/static/*
- requirements.txt, Dockerfile, start.sh, .gitignore
