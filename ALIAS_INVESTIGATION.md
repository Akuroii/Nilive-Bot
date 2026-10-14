# Alias system — full investigation + manual verification pass

Date: 2026-08-28. Branch: `arena/01a0467e-nilive-bot`. §0–§11 are the investigation itself:
measurement only, with reproducible probes under `tools/alias_probes/`. **§12 records what was
subsequently built** — the implementation, where it deviated from the recommendation in §10, and
which acceptance item is proved by which probe run.

---

## 0. How this was verified (not by "does it compile")

I installed the **exact pinned library** (`discord.py==2.7.1`, per `requirements.txt`) into a
scratch env and ran the repo's **real** modules against a **real** SQLite DB built by the real
`database.init_db()`:

| Probe | What it runs | Question it answers |
|---|---|---|
| `p1_ordering.py` | real `commands.Bot` + a cog `on_message` that mutates content | Does a cog listener run before `process_commands`? |
| `p2_real_cog.py` | real `cogs/command_aliases.py` + `cogs/moderation.py` | Does the alias register? What are its params? Does dispatch work? |
| `p4_flask.py` | real `dashboard/app.py` routes via Flask test client + real DB | Save → API → DB → sync flag → validation branches |
| `p5b_runtime.py` | **all 29 real cogs**, real dispatch | Listener order + what each system executes |
| `p5c_conflicts.py` | real moderation/triggers/customcommands/aliases | Alias vs trigger vs custom-command collisions |
| `p6_race.py` | triggers only, then triggers + alias cog | Does the content mutation leak into other listeners? |
| `p7_prototype.py` | throwaway "correct-shaped" dispatcher | Is the recommended architecture actually feasible on 2.7.1? |

Caveats, stated plainly: there is **no Discord gateway/HTTP** in these probes, so anything that
depends on the real network (e.g. `MemberConverter` hitting `query_members`, `bot.tree.sync()`)
is exercised with fakes. The probes are still decisive for the questions at hand, because every
one of the fatal defects is pure in-process logic.

I also read the library source rather than trusting docs: `discord/ext/commands/bot.py`,
`discord/client.py`, `discord/ext/commands/cog.py`, `core.py`, `parameters.py`,
`discord/app_commands/{commands,checks,transformers}.py`.

---

## 1. Stage-by-stage trace of the actual runtime path

| # | Stage | Status | Evidence / notes |
|---|---|---|---|
| 1 | Dashboard Aliases field → JS payload | ⚠️ works, wrong UX | `manage/commands.html:826-841` splits a comma string. Single char `k` survives. No chips. |
| 2 | `POST /api/commands/settings/<cmd>` validation | ✅ works | `app.py:1912-1935`: normalize/lowercase/dedupe, `[alnum + '-']`, 1–32 chars. **No minimum length** → `k` accepted (probe 4). |
| 3 | Conflict validation | ❌ incomplete | Blocks slash-name, builtin prefix cmds, cross-command alias, *exact* trigger word. **Does not** check custom commands when they are created *after*, `/trigger_add` (slash), global `guild_id=0` rows, substring/fuzzy trigger collisions (probe 4 §6/§8). |
| 4 | DB write | ✅ works | `command_toggles.aliases = '["k"]'` verified in the real DB (probe 4 §1). |
| 5 | Sync flag | ✅ works | `bot_settings('command_aliases_sync_needed','1')` written and read correctly (probe 4, probe 5b). Cleared by `UPDATE ... value='0'`. |
| 6 | Bot picks up the flag | ⚠️ works but slow/fragile | `command_aliases.py:599-627`: 30 s poll, so a save takes up to 30 s to take effect. Flag is cleared *before* syncing and the loop swallows all errors → a failed sync is silently consumed until the next save. |
| 7 | Alias registration into `bot.all_commands` | ✅ *nominally* works | Probe 2: `Registered 1 alias(es)`, `'k' in bot.all_commands`. This is the part the previous pass "verified" — and it is **not** what's broken, but it is also **not** what makes aliases work (see 8–11). |
| 8 | Bare-message detection | ✅ logic is right | `command_aliases.py:542-557`: first whitespace token, lowercased, matched against `_registered`. |
| 9 | Hand-off to `process_commands` (content mutation) | ❌ **FATAL — impossible** | `command_aliases.py:562` rewrites `message.content`, but `process_commands` has already run. Probe 1 + 5b: `process_commands` sees `'k <@111> rude'`; the mutation lands afterwards. |
| 10 | Argument parsing / converters | ❌ **FATAL — never happens** | `_make_alias_cmd` dies at `slash_cmd.params` (no such attribute in 2.7.1) and at `CmdParameter(converter=…, required=…)` (no such kwargs), both inside `try/except Exception: pass` (`:499`). Net: `cmd.params == {'kwargs': <VAR_KEYWORD>}`, zero args parsed, `ignore_extra=True` silently drops everything. |
| 11 | Permission / cooldown / toggle checks | ✅ logic sound, never reached | `check_command_toggles` + shared `main._command_cooldowns` + `_run_slash_checks` (verified parity; `has_permissions` only needs `interaction.permissions`, so the `PrefixInteraction` shim is genuinely sufficient for the 76 `@app_commands.checks.has_permissions` in the repo). |
| 12 | Invocation of the original callback | ❌ **FATAL — wrong call** | `:425` `await sc.callback(interaction, **kwargs)` omits the cog instance. Every app command here is a *method*, so the wrapper is consumed as `self`: `TypeError: Moderation.kick() missing 2 required positional arguments: 'interaction' and 'member'` (probes 2, 5c). |

**Conclusion: 4 independent fatal defects, stacked.** Fixing any one of them still yields a dead
alias. The user-visible symptom ("typing `k` does nothing at all, no error in the channel") matches
defect 9 alone, and defects 10/12 guarantee it stays dead even if 9 were fixed.

---

## 2. Defect 9 in detail — why the mutation trick can never work (library-level)

`discord/ext/commands/bot.py:237-242` (2.7.1):

```python
def dispatch(self, event_name, /, *args, **kwargs):
    super().dispatch(event_name, *args, **kwargs)      # -> getattr(self,'on_message') -> Bot.on_message
    ev = 'on_' + event_name
    for event in self.extra_events.get(ev, []):        # -> every @commands.Cog.listener()
        self._schedule_event(event, ev, *args, **kwargs)
```

and `discord/ext/commands/bot.py:1420`:

```python
async def on_message(self, message, /):
    await self.process_commands(message)
```

Cog listeners are *not* overrides of `Bot.on_message`; `Cog` registers them through
`bot.add_listener(...)` → `extra_events` (`cog.py:741-742`). Consequences, all confirmed by probe
1 / 5b:

1. `Bot.on_message` → `process_commands` is scheduled **first**, always, and every cog listener is
   scheduled **after** it. `main.py`'s load-order trick cannot change this — it is not a cog-order
   problem, it is an `extra_events`-after-base-handler problem.
2. Both are separate `loop.create_task(...)` calls, so `process_commands`' task runs to its first
   *real* suspension point first; `get_context()` reads `origin.content` (and
   `all_commands.get(invoker)`) before any await that can yield. So `process_commands`
   deterministically sees the **unmutated** content.
3. "MUST be last so all other on_message listeners see the original content first"
   (`main.py:288-291`, `command_aliases.py:509-540`) is inverted reasoning: they don't run
   sequentially to completion, they run as **concurrent sibling tasks**, each interleaving at its
   own `await`s.

Defect 9 therefore has a second, nastier side effect — see §5 (the mutation is a shared-object
side effect that corrupts the other listeners' input).

---

## 3. Defects 10 and 12 — the "register a real `commands.Command`" half is broken

`command_aliases.py:431-506` (the `try: ... except Exception: pass` block):

* `for name, param in slash_cmd.params.items()` — `app_commands.Command` in 2.7.1 has
  **`_params`** (private) and a `parameters` property returning `Parameter` proxies that expose
  `name/required/description/default` but **not** `.type` or `.channel_types`. The public
  `Command.params` does not exist → `AttributeError` → swallowed. (Probe 7 shows the correct
  source of truth is `cmd._params.values()` → `CommandParameter` with `.name/.type/.required/.default/.channel_types/_annotation`.)
* `CmdParameter(..., converter=None, displayed_default=None, description=..., required=...)` —
  `discord.ext.commands.Parameter.__init__` (2.7.1, `parameters.py:92`) accepts only
  `(name, kind, default, annotation, description, displayed_default, displayed_name)`.
  `converter` and `required` are **read-only properties**, not constructor args → `TypeError` →
  swallowed. `converter` is *derived* from `annotation`.
* Even if both were fixed, the wiring contradicts itself: params are built as
  `POSITIONAL_OR_KEYWORD`, and `Command._parse_arguments` (`core.py:877-901`) appends those
  converted values to `ctx.args` positionally, while `Command.invoke` calls
  `self.callback(*ctx.args, **ctx.kwargs)` (`core.py:1075`). The callback is
  `async def alias_callback(ctx, **kwargs)` → `TypeError: too many positional arguments`.
  `KEYWORD_ONLY` is the *only* kind that lands in `kwargs`, and it also carries discord.py's
  "consume rest" semantics — which is exactly what a trailing `reason: str` wants.
* Defect 12: `sc.callback(interaction, **kwargs)` must be
  `sc.callback(sc.cog, interaction, **kwargs)` (or bind via `functools.partial`/`MethodType`).

Probe 7 proves the corrected combination works mechanically: params derived from
`_params` (`member` → POSITIONAL_OR_KEYWORD `discord.Member`, `reason` → KEYWORD_ONLY `str`),
command resolved through `bot.get_context`/`bot.invoke` (`ctx.command: k`), discord.py selecting
the real `MemberConverter`, the shared `_command_cooldowns` gate reached, and the original message
**left untouched**. The only remaining error was `MemberNotFound`, caused by my fake `Guild` not
implementing `query_members` semantics — a probe limitation, not an architecture one.

---

## 4. What the previous implementation got right (keep this)

These are genuinely sound and should be preserved by any redesign:

1. **Storage**: JSON array in `command_toggles.aliases`, per `(guild_id, command_name)` row, plus
   `UNIQUE(guild_id, command_name)` index — clean, no new table needed.
2. **Save path**: normalization + validation without a minimum length (`k` is a legal alias and
   the API proves it), duplicates removed, `''`/`'[]'` treated as "no aliases", `aliases = excluded.aliases`
   only in the settings route, and the toggle / bulk-restrict routes correctly **do not** clobber
   aliases (probe 4 §4).
3. **Sync-flag signalling** (`bot_settings.command_aliases_sync_needed`) — the right shape for a
   two-process (bot + Flask) deployment.
4. **`check_command_toggles` extracted as shared logic** and reusing `main._command_cooldowns`:
   so `/kick` and `k` share one cooldown key `(guild, user, 'kick')`. That parity idea is correct
   and worth keeping (better: don't duplicate at all — see §7, Option R).
5. **`PrefixInteraction`** is adequate for this codebase: I checked all 76 app-level checks are
   `has_permissions`, which in 2.7.1 reads only `interaction.permissions` (`checks.py:332-335`),
   and `ctx.channel.permissions_for(author)` is the right computation. Fail-loud `__slots__` is a
   good property to keep.
6. **Detection rule** (first whitespace token, case-insensitive) matches ProBot's model.
7. Registering/unregistering idempotently (`remove_command` before re-sync, skip on conflict with
   real prefix commands) is correct hygiene.

The parts that are flawed: everything about **handing the message to the dispatcher** (§2),
everything about **argument parsing** (§3), the **cog-self omission** (§3), treating aliases as
**global** (`all_commands` can't be guild-scoped — §5), the **swallowed exceptions** (`except
Exception: pass` is why this looked "ready for manual testing"), and validation being **one-way** (§6).

---

## 5. Hidden interactions the current design creates (measured, not theorised)

1. **The mutation leaks into other listeners — real, deterministic-looking, but racy.**
   There are 5 `on_message` listeners: `activity_engine`, `sticky`, `triggers`, `customcommands`,
   `command_aliases` (in that order; observed in probe 5b). Probe 6 is decisive:

   * alias cog **not** loaded: message `k` → `TRIGGER[exact]-FIRED` ✅
   * alias cog loaded: same message `k` → **nothing** ❌

   because `Triggers.on_message` awaits a DB read and *then* calls `_matches(message.content, ...)`,
   by which time content is `'!k'` — so `exact`/`startswith` triggers involving an alias word
   **silently stop working**, while `contains` keeps firing. Word counts for XP/activity
   (`activity_engine.py:38`) can also be computed from the mutated text (3 words → `!k ...` 4
   words) depending on interleaving. Any implementation that mutates `message.content` is
   therefore unsafe *regardless* of load order.
2. **`bot.all_commands` is not a sufficient source of truth** (the exact question you asked). It
   contains only *prefix* commands (`['help','k','reload','sync']` in probe 2) — bare aliases are
   not represented anywhere in the tree, and slash commands are not in `all_commands`. So:
   * `customcommands.py:63`'s guard `first_word in self.bot.all_commands` only protects names that
     were **successfully registered** by the alias cog, in **any** guild, and only for the
     `!`-prefixed form. An alias skipped at sync time (name conflict, parent not in tree,
     DB unreadable) leaves `!k` free for a custom command to hijack.
   * Triggers never consult `all_commands` at all → no protection whatsoever.
3. **Aliases are effectively global.** `_sync_aliases` selects `FROM command_toggles` with **no
   guild filter** (`:304-307`) and registers into the single global command table. Probe 5c/§G: a
   bare `k` in guild `999` (which has no rows at all) gets rewritten and — once defects 9–12 are
   fixed — would execute `/kick` there. This is structural, not a patchable bug: `bot.all_commands`
   has no notion of guild.
4. **`commands.Command` objects pollute `!help`** and the command namespace; a removed/renamed
   alias can linger until the next successful re-sync (the loop only re-syncs when the flag is set).
5. **`custom_commands` is broken today, independent of aliases.** `cogs/customcommands.py:69`
   unpacks 14 values from `SELECT *`, but `database.init_db()` creates that table with **16**
   columns (`enabled`, `created_at`) → `ValueError: too many values to unpack (expected 14)` on
   every `!`-message in a guild that has ≥1 custom command (caught live in probe 5c §D). Also:
   `enabled` is never consulted by the bot (a "disabled" custom command still fires) and matching
   is `startswith(f"!{trigger}")` with **no word boundary** → trigger `k` also swallows `!kk`, `!kick`.
   Check your production DB with
   `sqlite3 data/nero.db "PRAGMA table_info(custom_commands);"` — if it lists 16 columns, custom
   commands are currently dead in prod too.
6. **Dead configuration** inherited by both paths: `delete_user_msg`, `delete_bot_reply`,
   `delete_bot_after`, `custom_cooldown`, `dm_response`, `require_permission`, `hide_from_help`,
   `cmd_emoji`, `category_color`, `ephemeral` are written by the dashboard and read by **no** bot
   code (grep: 0 hits under `cogs/`, `main.py`, `utils/`). So an alias can never be "ephemeral" and
   the alias/slash parity concern here is moot — but the dashboard UI implies otherwise.
7. **Two copies of the gating logic** (`NeroCommandTree.interaction_check` vs
   `check_command_toggles`) already differ: the cooldown-prune call only happens on the slash path,
   and any check added to the tree later (`bot_has_permissions`, `guild_only`, `nsfw`,
   `@app_commands.checks.cooldown`, transformers/`Range`/`Choices`) is silently **not** applied to
   aliases, because the alias path calls `sc.callback` directly instead of going through the tree.

---

## 6. Custom Commands vs Aliases — the concrete answer

Today, with defects in place, **nothing runs** for a bare `k`. Once implemented naively:

| Input | Alias path | Custom command path | Verdict |
|---|---|---|---|
| `k @u rude` (alias `k`, custom trigger `k`) | runs (after fixes) | skipped — gate `content.startswith("!")` fails on the original text; but if the mutation lands first it becomes `!k ...` and the `all_commands` guard is the only thing saving it | **accidentally safe, structurally unsafe** |
| `!k @u rude` | runs | `customcommands.py:63` returns because `k in all_commands` | single execution, but only by luck of ordering |
| `!k @u rude`, alias registration skipped | nothing | custom command `k` runs (and `startswith` also swallows `!kk`, `!kick…`) | **hijack** |
| custom trigger created **after** the alias | runs | runs | **double execution** — probe 4 §6 shows the API happily creates trigger `k` while alias `k` exists |
| `trigger_add` slash command (`cogs/triggers.py`) | runs | n/a | no validation at all: conflicts can always be created from Discord |

Custom-command matching rules I verified in code: **prefix only** (`startswith("!"+trigger)`),
**case-insensitive**, **no** `exact/contains/endswith` modes, no `enabled` check, `guild_id = ?
OR guild_id = 0` (so global rows apply everywhere), first match wins via `break`, and the row is
the DB order (no explicit `ORDER BY` → insertion order).

## 7. Triggers vs Aliases — the concrete answer

* Trigger matching ignores commands entirely: `Triggers.on_message` (`:147`) queries
  `enabled = 1` rows for `(guild_id = ? OR guild_id = 0)` and matches with `contains` /
  `startswith` / `exact` / `endswith` / `fuzzy` (`thefuzz.partial_ratio`), case-insensitive by
  default, **first match wins** (`break`), per-trigger `cooldown_seconds`, per-trigger
  `allowed_channels`, `%` chance.
* Both systems act on the *same* message with **no coordination**: they are sibling tasks; neither
  knows about the other, and there is no "handled" flag anywhere.
* So: alias `k` + trigger word `k` (`contains`) → **both** the command and the trigger response
  execute. With `fuzzy_match=1`, collisions get worse than exact-word equality: I measured
  `fuzz.partial_ratio('kick', 'k') == 100`, i.e. a fuzzy trigger of a 4-letter word matches a
  1-char alias message completely. Dashboard validation never looks at fuzzy/substring semantics
  at all (probe 4 §8 accepted exactly that combination).
* Ordering today: `process_commands` → activity → sticky → **triggers** → customcommands →
  alias-mutator, but "after" only means "scheduled later", not "runs later to completion", which
  is why the exact/startswith corruption in probe 6 is nondeterministic in production and
  deterministic in the probe.

---

## 8. Answers to your ten questions

1. **What is actually broken right now?** The bare-alias hand-off (defect 9), argument parsing
   (defect 10), and callback invocation (defect 12); plus a global-not-per-guild registration model
   (defect 4/§5.3) and one-way validation (§6/§7). Storage, API validation, DB write and the sync
   flag are all fine.
2. **Why does the manually tested alias not execute?** Because `process_commands` parses the message
   **before** any cog listener can rewrite it (library-guaranteed order), so `k @user` is parsed as
   plain chat and nothing is invoked. The alias *is* registered — that's why code review and
   "it compiled / it registered" looked green while the feature is dead.
3. **Which parts of the previous implementation are correct?** Everything in §4 (storage, API
   validation incl. no min-length, flag signalling, shared toggle/cooldown logic, the
   `PrefixInteraction` shim, the first-word detection rule, idempotent re-registration).
4. **Which parts are flawed?** The mutation-based dispatch, the params-building block (dead code
   that throws twice into a `pass`), `sc.callback(...)` without the cog, global alias registration,
   `all_commands` as the sole conflict oracle, `except Exception: pass`, and save-time-only
   validation.
5. **Custom Commands?** Not executed today (nothing executes). Naively fixed: single execution by
   luck, double execution whenever a trigger is created after the alias or via `/trigger_add`,
   plus a live `ValueError` bug that currently breaks all custom commands (§5.5). Needs an explicit
   precedence rule, not a `all_commands` sniff.
6. **Triggers?** Never blocked by anything. Alias + trigger on the same word both fire; and the
   mutation actively *breaks* `exact`/`startswith` triggers (measured). Also needs the precedence
   rule + removal of the mutation.
7. **Normal messages?** Unaffected by the alias feature **except** when their first token happens
   to equal a registered alias — e.g. someone chatting `k` or `k? nah` gets rewritten (probe 5b
   §A/§E) and, once fixed, would run `/kick` with no arguments. There is no "only if the bot is
   mentioned / only in allowed channels / only with permission" guard on the *detection* step.
8. **Permissions/cooldowns?** The logic is right and shared (single `_command_cooldowns` dict keyed
   by `(guild, user, parent)`; same role/channel/owner/enabled rules; `has_permissions` covered by
   `PrefixInteraction.permissions`), but it is **unreachable** today because execution never gets
   that far. It is also a duplicate of `interaction_check`, which will drift.
9. **What should the Alias UI architecture be?** §9 below: a dedicated `NeroAliasInput` component
   (chips, Space-to-commit) with the panel payload derived from the component's state, and the
   server remaining the single validator.
10. **Recommended architecture?** §10 below: drop `message.content` mutation and
    prefix-command registration entirely; resolve aliases per-guild at message time in **one**
    router that owns precedence for aliases/custom commands/triggers, and reuse discord.py's own
    context/invoke pipeline for parsing.

---

## 9. Alias UI — ProBot-like chip field (design, no code written yet)

Note: **the screenshot did not reach me** (nothing was attached to the workspace), so this is
based on your written spec — `[ × k ] [ × o ] [ × p ] |` with type → Space → chip — which I treat
as unambiguous. If the screenshot adds constraints (colors, ordering, drag-to-sort?), re-send it.

**Interaction contract**

| Trigger | Behaviour |
|---|---|
| type `k`, press `Space` | commit → chip `× k`, input cleared and focused |
| `Enter` | commit pending token; never submits the panel |
| `Backspace` on empty input | remove last chip |
| `×` click on a chip | remove that chip |
| paste `k, o p` | split on `,`/whitespace, commit each |
| blur, or Save clicked with text in the box | commit pending token first (never lose input) |
| duplicate | reject with inline hint, keep existing chip |
| `Escape` | clear pending text (don't close panel) |
| IME/dead keys | `compositionstart/end` guard so CJK/Arabic input isn't committed mid-composition |

**Validation must mirror the server rule, not a stricter one.** Today the server accepts any
Unicode letter/digit plus `-` (`alias.replace('-','').isalnum()`), 1–32 chars, lowercased — probe 4
confirms single-char `k` and even `ض` pass. So the client check should be
`/^[\p{L}\p{N}-]+$/u` after trim/lowercase; a `[a-z0-9-]`-only regex would newly break Arabic
aliases that the backend supports (and this repo explicitly cares about Arabic — see
`cogs/triggers.py` "Arabic support"). No minimum length, per your requirement.

**Implementation options**

| | A. tiny vanilla component (recommended) | B. `select2` in `tags` mode | C. htmx-rendered chips |
|---|---|---|---|
| Code | `dashboard/static/js/nero-alias-input.js`, ~120 lines, `window.NeroAliasInput = { attach(el, {values, onChange}), get(el) }` | ~30 lines config | server round-trip per keystroke |
| Fits repo conventions | yes — same pattern as `nero-select.js` (IIFE on `window`, loaded in `base.html`) | yes, jQuery+select2 already global | no |
| Space-to-commit, Backspace, paste-split, IME | full control | select2 commits on `Enter`/comma; needs `keydown` overrides + `tokenizer` surgery | awkward |
| Theming into this dark UI | trivial; reuse `.cmd-chip` / `.cmd-chip-accent` (`manage/commands.html:510-525`) | needs select2 CSS overrides (the recurring pain in this file) | n/a |
| Risk | low | medium (already wrapping select2 for role/channel pickers; alias semantics — free text — are select2's weakest area) | high |

**Markup/state**: replace the bare `<input id="edit-aliases-{{cmd}}">`
(`manage/commands.html:181-185`) with a `.alias-input` container holding a chip list + a real
`<input>` for the pending token, plus a hidden source of truth (`dataset`/`<input type=hidden>`) so
`loadCommandSettings()` (`:786-802`) seeds from `parseArr(data.aliases)` and
`saveCommandSettings()` (`:826-841`) reads `NeroAliasInput.get(el)` instead of
`.value.split(',')`. Keep the row-level affordance the current code half-implements: alias chips
**in the collapsed row** (not just the "or `k [member] [reason]`" hint) and the conflict error from
the API rendered **on the offending chip** (today `ajaxSave`'s toast is the only feedback — the
generic `❌ <error>` in `#edit-status-…`).

**Nice-to-have worth deciding now:** client-side "already used by `/ban`" preview needs an
`GET /api/commands/alias-registry` (single source of truth, §10 option V) — otherwise the chip can
only be validated on Save.

---

## 10. Backend architecture — three options, then my recommendation

**Option P — patch the prefix-command approach** (keep `all_commands` registration, fix 9/10/12:
call `bot.process_commands` ourselves after mutating, fix params via `_params`, pass `sc.cog`).
*Pros*: minimal diff, keeps `!help` integration. *Cons*: still needs a mutation *or* a second
`process_commands` call per message (double-dispatch risk for every other listener), aliases stay
global (defect §5.3), still re-implements parameter mapping, still bypasses tree-level checks.
**Rejected: it preserves the four structural problems we just measured.**

**Option B — full custom parser** (no `commands.Command`, parse tokens by hand, hand
`PrefixInteraction` a kwargs dict).
*Pros*: total control, easy guild scoping. *Cons*: we own `Member/Role/Channel/Attachment`
resolution, quoting, defaults, optional params, `Greedy`, error messages — i.e. we re-implement
`_parse_arguments` and diverge from prefix-command behaviour forever. **Rejected.**

**Option R — single per-message router (recommended).** One module owns "which system, if any,
executes this message", every subsystem asks it, nobody mutates the message:

1. `utils/message_router.py` (bot-side):
   * `alias_index: dict[int guild_id, dict[str alias, str command_name]]` rebuilt on the existing
     `command_aliases_sync_needed` flag (and on `on_ready`), **scoped per guild** — fixes the global
     leak, no `all_commands` involvement at all, and `k` stays legal.
   * `async def decide(bot, message) -> Route` where `Route ∈ {PREFIX_COMMAND, ALIAS,
     CUSTOM_COMMAND, TRIGGER, NONE}`, memoised per `message.id` in a bounded LRU so the 3-4 sibling
     listeners agree on one answer instead of racing. Precedence, fixed and documented:
     `PREFIX_COMMAND (!k, real prefix cmds) → ALIAS (bare first token) → CUSTOM_COMMAND (exact
     token match only) → TRIGGER (only if nothing above claimed the message)`.
   * each subsystem's `on_message` becomes: `if await router.decide(...) is not MINE: return`.
2. Alias execution reuses discord.py instead of mimicking it: build a **synthetic** message view
   (`types.SimpleNamespace(content=f"{prefix}{content}", author, guild, channel, _state, …)`) →
   `ctx = await bot.get_context(synthetic)` → `await bot.invoke(ctx)`. Original message untouched;
   converters, `TooManyArguments`/`MissingRequiredArgument`, `on_command_error`, `enable_on_edits`
   — all free. (probe 7 verified this half end-to-end: `ctx.command: k` resolved, real
   `MemberConverter` selected, original content unchanged.)
3. The alias `Command` object, if we still want `!k` to work, is generated **once per alias name**
   with params derived from `slash_cmd._params` (`POSITIONAL_OR_KEYWORD` for all but a trailing
   `string` param, which becomes `KEYWORD_ONLY` = consume-rest; `ignore_extra=True`), and its
   callback does `check_command_toggles` → `_run_slash_checks` → `sc.callback(sc.cog,
   PrefixInteraction(ctx), **kwargs)`. Better still: have the router call the same `_run_alias()`
   helper for both bare and prefixed forms, and register nothing in `all_commands` at all — then
   `!help`, cooldowns and gating all come from one place.
4. **Never swallow setup errors**: replace `except Exception: pass` with a real log + a
   `bot_settings` "alias_status" error surface the dashboard can display (that alone would have
   turned this from "looks ready" into "here is the traceback").
5. Cooldown/toggle/permission checks: make `NeroCommandTree.interaction_check` and the alias path
   call **the same function** (the extraction is already 90% done) — ideally the alias path invokes
   the tree's own machinery rather than a copy.

**Validation redesign (answers 5/6/7 properly).** Save-time cross-checks can never be complete —
`/trigger_add`, `/trigger_remove`, `guild_id=0` global rows, other guilds, and the custom-command
route (which does zero alias checking today) all bypass it. So:

* **Runtime precedence is the guarantee** (Option R, step 1) → at most one system per message,
  always, for conflicts created anywhere.
* Dashboard validation becomes **advisory**, computed from **one shared query**
  (`/api/commands/alias-registry`) that returns, for the guild, all alias owners + custom-command
  triggers + trigger words with their `match_type`/`fuzzy` flags. Every writer (settings save,
  `/api/save-trigger`, `/api/save-custom-command`) uses the same helper, checks **bidirectionally**
  and with the *actual* matching semantics (substring/prefix/fuzzy overlap, not just string
  equality), and returns `{"success": true, "warnings": [...]}` (yellow chip outline: "trigger `kick`
  (contains) will be suppressed by alias `k`") instead of hard-blocking. Single-character aliases
  remain fully allowed, exactly as you require — the collision problem is solved by precedence +
  visibility, not by banning short aliases.
* Also worth deciding here: whether `/trigger_add`-style slash commands should keep bypassing
  validation (they'd emit the same warning at runtime instead).

**Two side-fixes this pass uncovered that are *not* alias-related but block the same UX** — flag for
your call, I have not touched either: `cogs/customcommands.py`'s 14-vs-16 column unpack (custom
commands currently crash), and `custom_commands.enabled` being ignored by the bot.

---

## 11. Acceptance checklist for the next implementation (manual + probe)

1. `tools/alias_probes/p1_ordering.py`-style probe asserts `process_commands` is **not** relied on
   for bare aliases (i.e. the router decides, and the original `message.content` is byte-identical
   after all listeners).
2. `k @user` in the configured guild → `/kick` runs with `member` = a real `Member` and
   `reason` = rest of line (and `k` alone → the command's own "missing argument" behaviour, not a
   silent no-op).
3. `k` in a **different** guild with no alias row → nothing happens.
4. `K`, `k,`, `k` + trailing spaces → still works (case/space insensitivity), and `k.` / `ok k` /
   `!k` behave per the documented rule.
5. Disabled command via toggle → alias replies with the configured `error_message`; alias and slash
   share one cooldown (second call within window says "Slow down", whichever was used first).
6. Member without `kick_members` → the alias path replies with the missing-permission message
   (`has_permissions` parity).
7. Alias `k` + trigger `k` (`contains`, `exact`, `fuzzy`) + custom command `k`: exactly **one**
   outbound action per message, and the `exact` trigger for `k` is *not* corrupted (regression test
   for probe 6).
8. Create the trigger **after** the alias, and via `/trigger_add` → no double execution (warning is
   advisory).
9. Save → effect latency: after removing the 30 s poll in favour of an immediate in-process re-read
   (or keeping the poll but clearing the flag only after a successful sync), document the number.
10. `PRAGMA table_info(custom_commands)` on production, and `SELECT command_name, aliases FROM
    command_toggles WHERE aliases IS NOT NULL` — to see the real current state before/after.
11. Dashboard: chip flow (Space/Enter/Backspace/×/paste/blur-pending-commit), Arabic + single-char
    aliases accepted, duplicate + conflict shown on the chip, panel reload shows persisted chips.

---

## 12. What was built, and how each acceptance item was proved

Implementation date 2026-08-28, same branch. Full suite: `./tools/alias_probes/run_all.sh`
→ **p2 35/35 static guards · p4 39/39 dashboard · p6 18/18 router consistency ·
p8 62/62 acceptance · p9 42/42 UI · p10 11/11 full boot** (207 assertions, all green, and p8 is
idempotent — two runs in a row on the same DB both pass; p1 is printed evidence, not an assertion
script).

### 12.1 The shape that shipped (Option R, as chosen)

| file | responsibility |
|---|---|
| `utils/message_router.py` | **the single precedence authority.** `decide(message)` → `Decision(route, alias, custom_command, …)` with the fixed order `PREFIX_COMMAND > ALIAS > CUSTOM_COMMAND > TRIGGER`. Memoised per `message.id` in a bounded `OrderedDict` of *shared* `asyncio.Task`s (`asyncio.shield`), so the three cog listeners cannot disagree and the work is done once. Reads `custom_commands` through an explicit `CUSTOM_COMMAND_COLUMNS` list + `enabled = 1`. No mutation, no side effects. |
| `cogs/command_aliases.py` | owns only the ALIAS route: syncs the per-guild word→command index from `command_toggles.aliases`, publishes it to the router, and on a matching message builds a **throwaway** `commands.Command` and runs it through `bot.get_context()` + `bot.invoke()` on a synthetic message view. Never touches `bot.all_commands`, never edits the original message. |
| `utils/command_gating.py` | the dashboard row evaluation (enabled / owner_only / role + channel blacklists and whitelists / `cooldown_seconds` + bypass roles / `error_message`), extracted so `main.py`'s `interaction_check` and the alias gate share **one** implementation and **one** cooldown dict. |
| `cogs/customcommands.py` | asks the router for `Route.CUSTOM_COMMAND` instead of sniffing content; explicit 14-column read; honours `enabled`; exact whole-token match; `ALTER TABLE … ADD COLUMN enabled` migration for pre-existing DBs. |
| `cogs/triggers.py` | runs only on `Route.TRIGGER`. |
| `main.py` | `interaction_check` now delegates to `command_gating`; the "alias cog MUST be loaded last" comment is replaced by the reason order no longer matters. |
| `dashboard/app.py` | conflicts are **advisory**: `alias_warnings()` returns notes that ride along on a successful save (`{"success": true, "warnings": […]}`) for `/api/commands/settings/<cmd>`, `/api/save-trigger` and `/api/save-custom-command`; new `GET /api/commands/alias-registry`; the custom-command insert now writes `enabled`. |
| `dashboard/static/js/nero-alias-input.js` + `commands.html` + `main.css` + `base.html` + `dashboard.js` | the ProBot-style chip field, `.na-*` styles, `NeroWarnings` (parked in `sessionStorage` so the note survives the reload these pages do), plus an alias status line under the page title fed by the registry. |

### 12.2 Where it deviated from §10, and why

Each deviation was found *by running the probes*, not in advance — which is the argument for the
probes existing.

1. **The permission/toggle gate became a `Command` check instead of a step inside the callback.**
   As a callback step, argument parsing ran first, so `ta` on a disabled command answered
   "Missing argument: `response`" instead of the configured "this command is disabled".
   `Command.can_run` runs checks before `_parse_arguments`, so the order now matches the slash path.
2. **`sc.callback(sc.cog, …)` is wrong on app-commands; it is `sc.binding`.** `app_commands` objects
   have no `cog` attribute at all in 2.7.1, so the call raised `AttributeError` before touching the
   command. (`commands.Command.cog` is a different, prefix-side API.)
3. **Errors raised by checks are *not* wrapped in `CommandInvokeError`.** `Command.invoke` calls
   `prepare()` outside the wrapping, so treating `AliasGateError` like a generic `CheckFailure`
   swallowed the disabled-command reply entirely.
4. **The transient command is built lazily, at first use, not at sync time.** Building at sync meant
   "the parent cog is not in the tree yet" silently dropped aliases — visible the moment p8 loaded
   the cogs in reverse order (`cogs.command_aliases` first ⇒ `ta` was never registered and the
   *trigger* answered instead). Now the index is tree-independent; if a word's command is missing at
   the moment of use, the alias says "`/trigger_add` isn't loaded at the moment" instead of vanishing,
   and works again as soon as the cog returns (p8 §11b). This also made the load-order claim in
   `main.py` actually true rather than aspirational.
5. **The "drop aliases that shadow a prefix command" filter was removed.** Router precedence already
   handles it (`!k` → prefix, bare `k` → alias), so dropping the alias only created dead words.
6. **`set_alias_table()` invalidates the memo, and eviction never cancels an in-flight decision.**
   A save landing while a message is being decided must not be stuck behind that memo (p6 §3); and
   cancelling a shared decision surfaces as `CancelledError` inside *another* cog's `on_message`, so
   `_trim()` only evicts finished entries and lets the cache grow briefly during a >1024-message burst
   (p6 §4–5).
7. **`custom_commands` needed the migration, not just the fix.** Honouring `enabled` on a DB created
   by the cog's old 14-column schema would have raised `no such column`, so `ensure_table()` does a
   `PRAGMA table_info` check + `ALTER TABLE`.
8. **Sync latency: 30 s poll → 5 s, flag cleared only after success, result recorded.**
   `bot_settings['command_aliases_last_sync']` holds `{at, registered, skipped, error}`; the commands
   page shows it. A failed sync leaves the flag set so the next poll retries instead of half-applying
   a dashboard save.

### 12.3 Acceptance item → proof

| §11 item | status | where |
|---|---|---|
| 1 · router decides, original content untouched | ✅ | p8 §2 (`message.content was NOT mutated`), p6 §1–2 |
| 2 · `k @user` → real `Member` + rest-of-line reason; bare `k` alone → the command's own missing-argument reply | ✅ | p8 §3 (`KICKS == [(260100000000004242, 'being rude')]` through the real `MemberConverter`), p8 §9 |
| 3 · nothing in a guild with no alias row | ✅ | p8 §5 (guild 999: zero outbound, zero rows touched) |
| 4 · `K` / `k,` / `(ta)` / trailing spaces work; `ok k` and `!ta` do not | ✅ | p8 §13 (six typed forms, exact expectations) |
| 5 · disabled → configured `error_message`; one shared cooldown | ✅ | p8 §8 + §11 (slash path says "slash says no", alias path says "nope, disabled", second alias use says "Slow down — try again in 29.4s"); p8 §15 drives `evaluate_toggle_row` directly across 14 rule combinations (blacklist before whitelist, `bypass_cooldown_roles`, `owner_only`, expiry, and the legacy `allowed_roles` column being *replaced* rather than unioned, so a row can never become more permissive than what the dashboard shows) |
| 6 · `has_permissions` parity | ✅ | p8 §14 ("You need the following Discord permissions: Manage Guild", and the command did not run) |
| 7 · alias + triggers + custom command, exactly one action; `exact` trigger not corrupted | ✅ | p8 §3, §4, §6; p6 §6 (real cogs, one computation, one actor) |
| 8 · trigger created *after* the alias, incl. `/trigger_add` | ✅ | p6 §6 (alias `kick` vs exact trigger `kick`), p4 §5 (advisory warning, save still allowed) |
| 9 · save → effect latency documented | ✅ | 5 s poll + memo invalidation on table swap; status line + `command_aliases_last_sync` (p4 §6, p8 §10) |
| 10 · production `PRAGMA table_info(custom_commands)` | ⚠️ not run here | needs the live `data/nero.db`; command is in §12.5 |
| 11 · chip flow end-to-end | ✅ | p9 (all 42) + p4 §8 (page serves the component/styles and renders the host) |
| *(new) nothing unrelated broke* | ✅ | p10 — all 29 cogs in `main.py` load, the set unloads and reloads **in reverse order** with the alias index still correct, the other four `on_message` listeners each receive the untouched text, and an alias-free sentence runs no command |

### 12.4 Behaviour the UI now states out loud

* Aliases are **bare**: `k @user`, `help ban @user`. There is no `!k` alias form — that prefix belongs
  to the prefix parser — and no `@Nero` mention syntax.
* No minimum length (`k`, `ض`, `!`-pasted words are normalised or refused on *characters*, never on
  length); over-long words are truncated to 32 with a visible note.
* Collisions never block a save. Runtime precedence decides; the chip carries a ⚠ and the toast says
  what will change.

### 12.5 Still needs a live Discord pass (the probes cannot reach it)

Everything above is real code with a stubbed gateway; these three need a human:

1. `./start.sh`, then in the configured guild: `k <@someone> testing`, `ta mytrigger my reply`,
   `!cc` (a custom command), and the same words in a guild with no alias row.
2. `PRAGMA table_info(custom_commands);` and
   `SELECT guild_id, command_name, aliases FROM command_toggles WHERE aliases IS NOT NULL;`
   against the production DB — confirm the `enabled` column exists (the migration adds it on cog load
   if not) and see whether any guild-0 rows exist (they are inert for command settings).
3. Confirm the ⚠ chip + warning toast appear for a deliberate collision (alias `kick` while a trigger
   named `kick` exists), and that the trigger still answers sentences containing `kick`.
