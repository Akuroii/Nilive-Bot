# `tools/alias_probes` — the alias system's verification suite

Started as the throwaway scripts behind the 2026-08-28 investigation
(`ALIAS_INVESTIGATION.md`); the ones that describe behaviour that is now fixed
are kept as the regression check for it. They are **not** pytest tests and are
**not** imported by the bot. Each one boots the real objects — `main.py`'s bot,
the real cogs, the real `database.init_db()` schema, the real Flask app via
`app.test_client()`, the real chip component in a real DOM — and stubs only the
Discord network layer.

The rule behind this directory: "it compiles" and "ready for manual testing" are
not the same statement. Every claim in §11 of `ALIAS_INVESTIGATION.md` has a
line here that either passes or fails.

## Setup

```bash
python3 -m venv ~/.cache/nero-probe-env
~/.cache/nero-probe-env/bin/pip install "discord.py==2.7.1" \
    aiosqlite python-dotenv thefuzz flask requests Pillow
export PROBE_DIR=~/.cache/probe-db      # scratch DBs; NOT under /tmp (wiped between turns)
```

`discord.py` must be **2.7.1** — several findings are version-specific
(`app_commands` commands have `binding`, not `cog`; `Command.params` vs
`_params`; `extra_events` dispatch order; `MemberConverter`'s
`isinstance(result, discord.Member)` gate).

`p9_chips_dom.js` needs `jsdom` (the repo deliberately ships no `package.json`):

```bash
npm i jsdom            # or: JSDOM_PATH=/path/to/node_modules/jsdom node tools/alias_probes/p9_chips_dom.js
```

## Run

```bash
./tools/alias_probes/run_all.sh          # everything, exits non-zero on any failure

python tools/alias_probes/p1_ordering.py # why a cog cannot rewrite message.content for process_commands
python tools/alias_probes/p2_real_cog.py # static guards: the 4 fatal defects stay dead
python tools/alias_probes/p4_flask.py    # dashboard: save -> DB -> sync flag, advisory warnings, registry, page + assets
python tools/alias_probes/p6_race.py     # one decision per message, shared by sibling listeners, bounded memo
python tools/alias_probes/p8_acceptance.py # THE acceptance run: bare `k`, args, converters, scoping, gates, reloads
node   tools/alias_probes/p9_chips_dom.js  # the chip field itself: Space commits, × removes, paste splits, conflict ⚠
python tools/alias_probes/p10_full_boot.py # all 29 cogs load; reverse-order reload; other listeners untouched
```

`legacy/` holds the three probes that measured the *old* implementation
(`p5b_runtime.py`, `p5c_conflicts.py`, `p7_prototype.py`). They are kept as
evidence for §1–§5 and §7 of the report and are **expected to fail** against the
current tree — their headers say so.

## What each current probe is for

| file | the claim it defends |
|---|---|
| `p0_env.py` | shared harness: env injection, `Fake{Message,Guild,Channel,Member}`, `make_bot()`, `patch_context_send()` |
| `p1_ordering.py` | `Bot.dispatch` runs `process_commands` **before** every cog `on_message`, so mutating content from a cog can never work (this is why the old system was dead on arrival, and why "load the cog last" was never a fix) |
| `p2_real_cog.py` | source-level guardrails: no `message.content =`, no `bot.all_commands[...] =`, no `sc.cog`, no `SELECT *`, `CUSTOM_COMMAND_COLUMNS` and the cog's unpack stay the same length, no minimum-alias-length rule anywhere, one gating implementation shared by `main.py` and the cog, conflicts are advisory in the dashboard |
| `p4_flask.py` | real HTTP: single-char and non-Latin aliases save; malformed ones 400; every collision returns `warnings` instead of blocking; `/api/commands/alias-registry`; page renders the chip host and serves the new JS/CSS |
| `p6_race.py` | `MessageRouter.decide()` is computed once per message id and shared by all three cog listeners; `set_alias_table` invalidates the memo so a save isn't stuck behind it; eviction never cancels an in-flight decision; with the real cogs, exactly one system acts |
| `p8_acceptance.py` | end-to-end: `ta hello some long response text` runs `/trigger_add`; `k <@id> being rude` really kicks (real `MemberConverter`, real `discord.Member`, only `Member.kick` stubbed); no double execution with triggers/custom commands; guild scoping (an alias in A does nothing in B); `enabled` + `error_message` + cooldown parity with the slash path; missing-argument and permission errors surface instead of vanishing; sync flag consumed and reported; aliases survive their parent cog being reloaded; load order irrelevant |
| `p10_full_boot.py` | nothing unrelated broke: every cog in `main.py`'s list loads, the whole set unloads and reloads **in reverse order** with the alias index still correct, the other four `on_message` listeners each still receive the untouched text, and a sentence with no alias word runs no command |
| `p9_chips_dom.js` | the ProBot-style field: type + `Space` → chip, `×` removes one, `Backspace` pops the last, click re-edits, paste splits, duplicates don't destroy the existing chip, over-long names truncate with a note, invalid words stay editable in the box, and the hidden input the page already posts holds the joined value |

## Expected noise (not failures)

* `RuntimeError: Client has not been properly initialised` from `@tasks.loop`
  `before_loop` hooks (`wait_until_ready()` is never satisfied without a gateway),
  and `Task exception was never retrieved` for those loops.
* `Sticky.on_message` → `no such table: sticky_messages` (created in `on_ready`).
* The probes never call `get_self()`, so anything needing `bot.user` is stubbed
  out or avoided.

## Environment the probes inject

`DISCORD_TOKEN` (fake, shape-valid — never sent anywhere), `DATABASE_PATH` from
`PROBE_DIR`, `OWNER_ID`, `SECRET_KEY`, `NERO_ENVIRONMENT=test`. Nothing is
written inside the repo.
