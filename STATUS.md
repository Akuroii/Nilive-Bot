# NILIVE BOT — SHIFT CHECKPOINT
Stopped at: Emergency fix — bot startup crash resolved.

## Root cause (confirmed via Railway logs, not guessed)
```
Traceback (most recent call last):
  File "/app/main.py", line 80, in <module>
    @bot.tree.check
     ^^^^^^^^^^^^^^
AttributeError: 'CommandTree' object has no attribute 'check'
```
`discord.py`'s `app_commands.CommandTree` has no `.check()` decorator —
that method only exists on `commands.Bot` (for prefix commands). This
raised an `AttributeError` at **import time**, before the bot ever
attempted `bot.start()`. Exit code 1, `start.sh` still launched the
dashboard afterward (separate process, unconditional), which is why the
dashboard looked fully healthy (login, server-select, all APIs 200) while
the bot itself was never online — there was no bot process for Discord to
show as online.

This was a real code bug in `main.py`, not a token, intents, or Railway
config issue — token check passed, intents logged correctly, and both
printed successfully *before* the crash line in the log.

## Fix — main.py only
- Removed the invalid `@bot.tree.check` decorator + standalone
  `global_command_gate` function.
- Added `class NeroCommandTree(discord.app_commands.CommandTree)`
  overriding `async def interaction_check(self, interaction) -> bool`,
  containing the **exact same gating logic** (per-guild enable/disable,
  owner-only, allowed_roles, allowed_channels, cooldown + bypass roles) —
  nothing was changed behaviorally, only which discord.py hook it's
  attached to.
- `bot = commands.Bot(...)` now passes `tree_cls=NeroCommandTree` so the
  tree subclass is used instead of the default `CommandTree`.
- `_command_cooldowns` dict moved above the class (same module-global,
  referenced by the method — no behavior change).

Verified: `python3 -m py_compile main.py` — exit 0.

Not touched: intents, token validation, cog list, `on_ready`,
`on_app_command_error`, `on_guild_join`, `sync`/`reload` owner commands,
`rotate_status` — all unchanged from the last canonical ZIP.

## Files in this ZIP (delta only)
- `main.py`
- `STATUS.md`

## NOT included (unchanged — pull from your last full ZIP)
Everything else: all `cogs/*`, `dashboard/*`, `utils/*`, `database.py`,
`requirements.txt`, `Dockerfile`, `start.sh`, `.gitignore`,
`DEBUG_GUIDE.md`, `HANDOFF_NOTES.md`.

## Next steps for Dark
1. Merge `main.py` into the canonical project, redeploy on Railway.
2. Watch the log for `Nero is online as <bot name>` with no traceback
   right after `Attempting Discord connection...`.
3. Confirm the bot shows green/online in Discord's member list.
4. Once online, re-verify `/select-guild`, invite-bot flow, and slash
   commands end-to-end — those were blocked purely by the bot never
   starting, not by any logic bug in them.

## Still open (unrelated to this fix, from prior shift's notes)
- Phase 3: E3 Transaction Ledger / E4 Inventory — already implemented per
  the ZIP this session started from (utils/ledger.py, utils/inventory.py
  present and wired into economy_safe.py, reward_engine.py, shop.py). Not
  touched or re-verified this shift — scope was strictly the crash.
- Security findings from the earlier static analysis sweep (stored XSS in
  moderation dashboard, missing CSRF, ledger/inventory API↔page privilege
  mismatch, warn/add_role/remove_role hierarchy gaps) remain unresolved.
