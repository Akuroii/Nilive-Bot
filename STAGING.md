# Nilive Bot — Staging vs. Production Database

Lets you point the bot and/or dashboard at a second, separate SQLite
file for testing — so you can try changes without touching real
member data.

## How it works

`database.py` resolves `DB_PATH` from environment variables, checked
in this order:

1. `DATABASE_PATH` (if set) — explicit override, always wins.
2. `NERO_ENVIRONMENT=staging` + `STAGING_DATABASE_PATH` (if set) —
   staging points at that exact path.
3. `NERO_ENVIRONMENT=staging`, no `STAGING_DATABASE_PATH` set — falls
   back to `/app/data/nero_staging.db`.
4. Default (`NERO_ENVIRONMENT` unset, or `production`) —
   `/app/data/nero.db`, same as before this feature existed.

If you never set `NERO_ENVIRONMENT`, nothing changes — the current
deploy behaves exactly as it did before this.

## Running in staging mode

Set one env var before starting either process:

```bash
export NERO_ENVIRONMENT=staging
python3 main.py            # bot, now reads/writes nero_staging.db
python3 dashboard/app.py   # dashboard, same
```

Or point staging at a specific file:

```bash
export NERO_ENVIRONMENT=staging
export STAGING_DATABASE_PATH=/app/data/staging/test1.db
```

A local `.env` file works too (both processes call `load_dotenv()`
before reading any of this).

Both processes print which environment and path they're using on
startup:

```
[DB] Environment: staging — using database at /app/data/nero_staging.db
```

The dashboard also shows it visually:
- A yellow banner across the top of every page while logged in.
- A "🧪 Staging" badge on the login page.
- An "Environment" row on the System Health page's Database card.

## Safety guard

If `NERO_ENVIRONMENT=staging` ever resolves to the production path
(e.g. `STAGING_DATABASE_PATH` set to the prod file by mistake), both
processes refuse to start with a `FATAL` error instead of silently
reading/writing real data:

```
FATAL: NERO_ENVIRONMENT=staging but DB_PATH resolves to the
  production database path (/app/data/nero.db).
```

If you see that, check `STAGING_DATABASE_PATH` and `DATABASE_PATH`.

## Getting realistic test data into staging

A brand-new staging file starts empty. To test against a real
snapshot of production instead:

```bash
python3 scripts/clone_to_staging.py
```

This uses SQLite's online backup API (same approach as
`cogs/backup.py`'s automated backups) — safe to run while the
production bot/dashboard are live and actively writing. It's a
one-time snapshot, not a live link; re-run it whenever you want
staging refreshed.

```bash
python3 scripts/clone_to_staging.py --force              # overwrite existing staging file
python3 scripts/clone_to_staging.py --source X --dest Y  # explicit paths
```

The script always defaults to the two well-known default paths
(`/app/data/nero.db` -> `/app/data/nero_staging.db`), regardless of
your shell's `NERO_ENVIRONMENT`/`DATABASE_PATH` — it doesn't read
either of those, so running it can't accidentally pick up the wrong
source/destination from your current environment.

## What this does NOT cover

This is database-only separation. It does not:

- Give you a second Discord bot/token. Running a staging *bot
  process* with the same `DISCORD_TOKEN` as production, at the same
  time, means two processes both connect to the gateway and both try
  to handle the same events/commands — that will misbehave. To test
  bot-side behavior (not just dashboard behavior) against staging
  data, use a second Discord application + bot token pointed at a
  private test server, with `DISCORD_TOKEN` set to that second token
  alongside `NERO_ENVIRONMENT=staging`.
- Anonymize member data. A clone is an exact copy — real Discord user
  IDs, balances, warning reasons, everything. Treat a staging file
  that came from a real clone with the same care as production.
