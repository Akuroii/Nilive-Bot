# Embed Builder — Phase 0 report (stability & correctness)

Branch `arena/01a0cf79-nilive-bot` · base commit **`3874a0fa5d217950e6bf2dfac6edd248f50c983b`** (`3874a0f`, "Update test_afk.py" —
the local HEAD; the earlier `a30dde2` in this report was the pre-reset planning-docs commit and is no longer the base) ·
**all Phase 0 edits are uncommitted**: **15 files = 8 modified + 7 new**, plus this report (16 entries; the three planning
documents are untracked separately and are *not* part of the Phase 0 commit — §1b).
It was put through the required real-browser gate (§5b) — one genuine regression was found and fixed; a later independent
review then raised exactly three before-commit blockers (F1–F3, §3.12–3.14), those were fixed, and the gate re-run now
passes **52/52**.
Phase 0 is the approved scope from `EMBED_BUILDER_PLAN.md` §7 — lifecycle correctness, IndexedDB degradation, non-blocking
initialisation, the save/load data-loss fixes, per-file validation, server-side validation, markdown context fixes.
No visual redesign, no broad rewrite, no schema migration.

---

## 1. Files changed

| File | Δ | What changed |
|---|---|---|
| `dashboard/static/js/nav-lifecycle.js` | **new** (428 L) | `window.NERO` page-module registry: `definePage(name, {init, destroy})`, mount/unmount on `htmx:beforeSwap` / `afterSwap` / `load` / `historyRestore` and `pagehide`, a per-page context (`ctx.on/off`, timers, `fetch`/`fetchJSON` with `AbortController`, object-URL + `cleanup` tracking, counters, `mark`), an ordered+deduped script loader, and `NERO.debug.report()` behind `?debug=1` / `localStorage.nero_debug` |
| `dashboard/static/js/embed-builder-page.js` | **new** (1540 L) | The builder page as a module — everything that used to be the 1 055-line inline IIFE: content card + toolbar, mention modal, emoji popover, attachments, IndexedDB draft, undo/redo, send, saved templates. Pure helpers exported as `NERO.embedBuilder.helpers` |
| `dashboard/templates/manage/embedbuilder.html` | +5 / −1 060 (1 192 → 137 L) | Markup only. Root is `#eb-root` with `data-page-module="embed-builder"` + `data-page-script="…/embed-composer.js …/embed-builder-page.js"`. The nested `{% block scripts %}` (and with it the double emission) is gone |
| `dashboard/templates/base.html` | +18 | One `<script src="…/js/nav-lifecycle.js">` include (after `nero-select.js`/`nero-alias-input.js`) and a conditional `window.__BOT_IDENTITY__` emission |
| `dashboard/app.py` | +44 / −1 | `_bot_identity_for_page(guild_id)` (SQLite-only read, 2 s bound, never raises) and the `/embed-builder` route passing `bot_identity` |
| `dashboard/static/js/embed-composer.js` | +304 / −9 | Additive only: `opts.extraFields` in `mountEditor`; `author_icon`/`author_url`/`footer_icon`/`url`/`timestamp` in the payload + `embedFromApi` (incl. the legacy flat-key fallback, §3.11); `mediaSrc`/`safeHref`/`imgSrc`; `fmtDiscordTimestamp`; and the code-span/fence handling of §3.12–3.14 (one left-to-right `scanCode` pass replacing the two sequential regexes) |
| `dashboard/api/embedbuilder.py` | +71 / −21 | New `GET /api/embedbuilder/limits` (LEVEL_OWNER); the send route validates through `utils/embed_schema` and returns Discord-shaped path errors + advisory file warnings; `_validate_embeds` restored on the save path (it had been lost during the Phase 0 edit — §6) with its numbers taken from `utils/discord_limits.py` |
| `utils/discord_limits.py` | **new** (181 L) | The single limits table: 25 MiB request ceiling, 25 MiB − 64 KiB file budget, advisory per-file cap (20 MiB, `NERO_ATTACHMENT_MAX_BYTES`), embed limits; `limits_payload()`, `human_bytes()` |
| `utils/embed_schema.py` | **new** (414 L) | Pure-stdlib validator: `validate_message`, `validate_for_discord → (payload, ok, errors)`, `advisory_file_warnings`, `strip_unknown_embed_keys`, `first_error_message`; rejects dangling `attachment://` |
| `scripts/test_nav_lifecycle.js` | **new** (534 L) | 36 checks on the registry with a hand-rolled DOM double |
| `scripts/test_embed_builder_boot.js` | **new** (721 L) | 86 checks: boots the three real JS files, asserts the six Phase 0 exit criteria + the §8.4 before/after column in its header |
| `scripts/test_embed_schema.py` | **new** (310 L) | 62 checks on the validator |
| `scripts/test_embedbuilder_attachment_logic.js` | +59 / −30 | Retargeted: its extracted helpers moved from the Jinja template into `embed-builder-page.js`. **Same assertions, no coverage deleted** |
| `dashboard/static/css/embed-composer.css` | +31 / −0 | Preview-only code styling (§3.13): `.eb-code-block` inside the content/description/field containers gets a real code-block box and wraps long lines instead of widening the preview; a `code` nested in bold/italic/underline keeps the surrounding font |
| `scripts/test_embed_composer.js` | +146 / −0 | Six checks on the legacy flat `author_icon`/`footer_icon` → editor icons path, nested-shape precedence, the no-icon case and the payload/omission shape; plus 30 checks asserting the **intended** code rendering of §3.12–3.14 (fences with/without an info string, long lines, single/double-backtick spans, code beside live markdown, ordering, and placeholder-leak cases) |

## 1b. Exactly what the Phase 0 commit would contain

19 entries are in the working tree. **15 of them are the Phase 0 change set** (8 modified + 7 new) plus this report = **16**.
The other **three are the planning documents**, and they are a separate question:

| Group | Entries | Note |
|---|---|---|
| **Phase 0 code (15)** | `M dashboard/api/embedbuilder.py` (+71/−21) · `M dashboard/app.py` (+44/−1) · `M dashboard/static/css/embed-composer.css` (+31/−0) · `M dashboard/static/js/embed-composer.js` (+304/−9) · `M dashboard/templates/base.html` (+18/−0) · `M dashboard/templates/manage/embedbuilder.html` (+5/−1060) · `M scripts/test_embed_composer.js` (+146/−0) · `M scripts/test_embedbuilder_attachment_logic.js` (+59/−30) · `?? dashboard/static/js/nav-lifecycle.js` (428 L) · `?? dashboard/static/js/embed-builder-page.js` (1540 L) · `?? scripts/test_nav_lifecycle.js` (534 L) · `?? scripts/test_embed_builder_boot.js` (721 L) · `?? scripts/test_embed_schema.py` (310 L) · `?? utils/discord_limits.py` (181 L) · `?? utils/embed_schema.py` (414 L) | This is the set the plan's §7 file table authorises, nothing else |
| **Report (1)** | `?? EMBED_BUILDER_PHASE0_REPORT.md` | Historical record — include it |
| **Planning documents (3, NOT part of the Phase 0 commit)** | `EMBED_BUILDER_PLAN.md` · `EMBED_BUILDER_REVIEW.md` · `EMBED_BUILDER_REDESIGN.md` | **Unmodified.** Each blob hash in the working tree is byte-identical to its committed blob in `a30dde2` (`EMBED_BUILDER_PLAN.md` `6ade0e8b…`, `EMBED_BUILDER_REVIEW.md` `82ad5131…`, `EMBED_BUILDER_REDESIGN.md` `7681914e…`). They are untracked here only because this session's branch was reset to `3874a0f`; they are unchanged from the approved planning commit and are needed for the historical record **only if the reset is meant to be undone**. Re-adding them would restore `a30dde2`'s exact bytes — a decision for you, not for this pass, which touched none of them |

## 2. Files intentionally not changed

`database.py`, `cogs/*` (incl. `cogs/embedbuilder.py`, `cogs/reactionroles.py`), `utils/emoji.py`, `utils/app_emoji_cache.py`,
`utils/bot_profile.py`, `dashboard/api/core.py`, `/reaction-roles`, `systems/minigame_builder.html`, every other template,
`package.json`, `scripts/run_js_tests.sh` (it auto-discovers `scripts/test_*.js`, so the two new harnesses run in CI without an edit).
Nothing outside the plan's Phase 0 file table was touched — the stop condition in §7 was not triggered.

## 3. What was actually fixed

1. **The page ran twice.** `manage/embedbuilder.html` declared `{% block scripts %}` *inside* `{% block content %}`; Jinja renders
   a nested override where the child puts it **and** where the parent declares it (verified with a minimal repro: one inline
   script, two emissions). The whole 1 055-line IIFE executed twice per load: two states, two listener sets, two draft restores,
   **8 API calls before the first paint** instead of 4. Now: one module, registered once; re-definition replaces quietly.
2. **Blocking initialisation.** The old init was `await restoreDraft(); await Promise.all([loadBotIdentity(), loadMentionLookups()]);
   renderAll();` — the composer could not paint until `/api/botprofile/config` (a live Discord round-trip with an 8 s timeout)
   and the two roles/channels calls came back. Now: identity is rendered with the page and everything else runs after the first
   paint; measured **0 API calls before paint**.
3. **IndexedDB could blank the page.** With `open()` never calling back, the shipped page **never painted at all**. Now: 1.5 s
   open timeout, `onblocked`, absent-`indexedDB` and write-failure paths all land in a degraded mode — page paints in ~3.4 ms,
   `idbUnavailable` is counted once, the status line says drafts are not being saved and nothing spins forever.
4. **Re-entry did nothing (htmx).** The old script lived in base's `{% block scripts %}` (outside `#content-area`), so a sidebar
   navigation never ran it, and the inline code that depended on `window.EmbedComposer` threw because the dynamically inserted
   `embed-composer.js` loaded async. Now the registry owns init **and** destroy: 5 enter/leave cycles are flat, document/window
   listener counts constant, and every held resource (listeners, timers, fetches, object URLs) is released.
5. **The emoji filter rebuilt the grid per keystroke.** 275 cells × 3 listeners, undebounced: **+2 440 listener registrations**
   for 5 fast keystrokes. Now: exactly 3 delegated listeners on the scroll container + a 150 ms debounce (harness: 3 listeners).
6. **Data loss on save→load→save.** `author_icon`, `author_url`, `footer_icon`, `embed url` and `timestamp` were dropped by
   `cleanEmbedForPayload`/`embedFromApi`. Fixed additively and emitted **only when set** (harness: five-field round trip, blank
   payloads filtered).
7. **Markdown context.** Code spans/blocks are stashed before the markdown pass and restored before mention/emoji rendering, so
   `` `**not bold**` `` stays literal. Previews also refuse `javascript:`-style `src`/`href` (`mediaSrc`/`safeHref`), resolve
   images through one seam (`imgSrc`, `data.resolveImageSrc` for `attachment://`) and render timestamps the way Discord does.
8. **Per-file size + server-side validation.** `utils/discord_limits.py` is now the single source for the UI **and**
   `GET /api/embedbuilder/limits`; `utils/embed_schema.py` validates the send server-side with Discord-shaped error paths,
   strips unknown keys and rejects dangling `attachment://` references before a request is made.
9. **Dev-only instrumentation.** Counters (`editorRenders`, `attachmentRenders`, `previewUpdates`, `firstPaintMs`, `initMs`,
   `postPaintMs`, `limitsMs`, `lookupsMs`, `idbRestoreMs`, `idbUnavailable`, `imagePreviewResolutions`) behind the debug flag —
   silent in production.

10. **The save path threw a 500** (found by the browser gate, §6): `api_embedbuilder_save_template` called `_validate_embeds`, a
   helper that the Phase 0 rewrite of that file had dropped, so every *Save template* click returned
   `NameError: name '_validate_embeds' is not defined` — the toast said "Connection error" and nothing was saved. Restored, with
   its three limits read from `utils/discord_limits.py` so the save path, the send path and the page cannot drift apart; it
   deliberately stays weaker than `validate_for_discord` (a half-finished draft may be slightly over Discord's limits and must
   still be *savable*).
11. **Legacy templates lost their icons on load.** Rows written before this change (and by `/api/save-embed-template`) are bare
   embed dicts using the flat keys the Discord-side cog reads — `author`, `author_icon`, `footer`, `footer_icon`. `embedFromApi`
   only understood the nested API shape, so loading one of those templates silently dropped the icons. It now accepts both
   (`nested icon_url` first, flat `author_icon`/`footer_icon` as the fallback); covered by six new composer checks. The nested
   shape still wins, and the payload the send route posts is still Discord-shaped.

### 3.12 F1 — a fence's info string was rendered as content (fixed)

````
```js
const a = 1;
```
````
produced `<pre class="eb-code-block"><code>js
const a = 1;</code></pre>` — the language showed up as the first line of the
block. The old regex was ``/```([\s\S]+?)```/`` and swallowed everything after the opening fence. Discord treats the rest of
the opening line as the language and renders none of it. The info line is now dropped — with the newline after it and one
trailing newline, so the block does not start or end on a blank line — **only when the fence opens a line**, which is the
case the old code could ever have been right about: ```` ```x``` ```` typed mid-sentence has no info line and keeps its body.

### 3.13 F2 — fenced blocks were unstyled and could push the preview sideways (fixed)

`.eb-code-block`/`.eb-code` existed in **no** stylesheet: `main.css` styles the bare `code` element, so a fence fell back to
browser defaults — transparent background, no padding, `white-space:pre` and no overflow rule. Measured in the real browser
on a 400-character line: the block's `scrollWidth` was 729 px inside a 520 px box (the pre-fix gate had confirmed
`previewOverflows: true`). `.eb-code-block` now has the code-block box (background `#2b2d31`, radius 4, padding 8/10,
JetBrains Mono 12 px) and wraps long lines (`white-space:pre-wrap` + `overflow-wrap:anywhere`), keeping `overflow-x:auto` as
a safety net for an unbreakable string. Post-fix, in Chromium at 1440 px: the preview's `scrollWidth` **496 ≤ 496 px** and
document overflow **0 px** — the screenshot `gate-code.png` shows the long line wrapping inside the box.

### 3.14 F3 — the code stash could leak its own placeholder characters (fixed)

The markdown pass stashed code as `\u0003` + index + `\u0004` and ran fences **then** inline spans as two sequential
regexes. For ```` `` ``` `**x**` ``` `` ```` the inline pass matched the *placeholder* a fence had just left behind
(`` `\u00030\u0004` `` is a one-character body between two backticks), re-stashed it, and the output contained the literal
control characters: `<code class="eb-code"> \u0003 0 \u0004 </code>`. The restore pass could never repair it —
`String.replace` does not rescan its own replacement text — and it used `codeChunks[idx] || ''`, which silently swallowed a
miss instead of surfacing it. Three changes, none of them a behaviour change outside code handling:

* **One left-to-right scan** (`scanCode`) replaces both regexes. A run of backticks opens code and closes at the next run of
  the *same* length — with the CommonMark relaxation that a fence may close on a longer run, which is what makes
  ```` `` `x` `` ```` one span holding a backtick instead of two broken halves. A scan cannot mistake its own output for
  input, so the whole class of re-stashing bug is gone. Unclosed runs are left exactly as typed.
* **Fixed-width placeholders**: token 5 is now `\u0001 0005 \u0002` and code chunk 5 `\u0003 0005 \u0004`, so the two
  alphabets can no longer share an index (the old code index was one character short, which is precisely what let the inline
  pattern match it).
* **C0 control characters are stripped from the source text** before anything else, and a miss on a placeholder lookup now
  returns the placeholder itself rather than empty markup — no silent empties, and a placeholder-shaped string typed by a
  user (`\u0003 0 \u0004`) renders as its visible remainder (`a0b`) instead of a mystery glyph. A final scrub is the
  backstop.

Discord-accurate markdown only, as required: the info-string rule matches the client, and the double-backtick behaviour
follows CommonMark, which is what the client implements.

## 4. Before/after measurements

Node, identical DOM double and fixtures, same phases (`/tmp/phase0-measure.txt`, harness-run). §8.4's Performance-panel
recordings could not be reproduced in this sandbox (no browser); these are the equivalent instrumented numbers, and the
before-column is also recorded in `scripts/test_embed_builder_boot.js`'s header as §8.4 requires.

| Metric | Before (as shipped) | After |
|---|---|---|
| First paint | 12.5 ms **after 8 API calls** (4 unique × 2 emissions) | 7.5–11.9 ms **after 0 API calls** |
| API calls before paint | 8 | **0** |
| API calls after paint | 0 | 5 (limits, templates, roles, channels, botprofile) |
| Listeners live at boot | 82 (two copies of the page) | 57 |
| 20 content keystrokes | 40 preview writes (18 KB), 0 nodes | 20 preview writes (9 KB), 0 nodes |
| …`<img>` re-created by those keystrokes | 80 | 40 (still re-created — see §9) |
| 10 embed-title keystrokes | 10 writes, editor untouched | 10 writes, editor untouched (`editorRenders` stays 1) |
| Emoji filter, 5 fast keystrokes (20 ms) | 846 nodes, 10 grid writes (68 KB), **+2 440 listeners** | 58 nodes, **1** grid write, **+0 listeners** |
| Emoji filter, 5 slow keystrokes (180 ms) | 124 nodes, +346 listeners | 62 nodes, 5 writes, +0 listeners |
| 5 × enter/leave | removes **nothing**; +41 listeners & +4 API calls per visit; live DOM 134 → 142; document 6 → 14 | 32 removed / 49 re-added per cycle; live DOM **56**, document **9**, window **1** — identical every cycle |
| IndexedDB blocked | first paint **NEVER** (8 calls issued, page dead) | paints in 3.4–3.9 ms, 0 pre-paint calls, degraded status |
| Real browser, same harness (HEAD vs Phase 0) | 4 API calls (8 with the double execution) started before anything painted; htmx navigation to the builder throws `Cannot read properties of undefined (reading 'esc')` | module paints at 232 ms with **0 calls of its own before it**; the builder page produces **no** console error on any path |
| `window.indexedDB` missing | paints, then 4 idle `idbGet`/`idbSet` errors | paints in 2.5 ms, `idbUnavailable: 1`, no errors |
| Rendered builder page | 2 823 lines / 118 527 B (script block twice) | **717 lines / 28 981 B** (−74 %), 0 inline JS |
| §8.4 probes | `renderPreview` 0.28 ms / 5 459 B / 75 el; `renderDiscordMarkup` 0.087 ms; `mountEditor.render()` 10 × 25 fields → 230.5 KB / 1 870 el / 830 inputs / 0.719 ms | unchanged (the editor rebuild path was not modified) |

## 5. Tests performed

* `npm test` → **7/7 harnesses green**: `test_embed_builder_boot` 86, `test_nav_lifecycle` 36, `test_embed_composer` **83** (53 + 30 code-rendering checks),
  `test_embed_attachments` 31, `test_embedbuilder_attachment_logic` 26, `test_currency_icon` 146, `test_vi_shop_form` pass.
* `node --check` on every file in `dashboard/static/js/` and every `scripts/test_*.js`; `python3 -m compileall` on the touched
  Python; `python3 scripts/test_embed_schema.py` → 62/62.
* Render sweep of **all 41 templates** with the working base.html vs HEAD's base.html: 5 identical, **35 changed by exactly
  +8/−0 lines** (one comment + one `<script>` tag), 1 (`general/member_profile.html`) cannot render under the stub context and
  fails identically with the old base — a harness limit, not a regression.
* Builder render proof: exactly **1** `nav-lifecycle.js` include, 0 `<script>` tags inside `#content-area`, no Jinja leftovers,
  and all **36** ids the module asks for are present in the markup; `window.__BOT_IDENTITY__` is emitted when the route passes
  identity and absent when it does not.
* Lifecycle coverage in `test_nav_lifecycle.js`: partial swap leaves the module mounted, `pagehide` tears down, a throwing init
  is torn down and recorded, a failed script load is recorded and retried, two mounts of the same DOM never double-init, a stale
  mount is dropped when a newer one wins, scripts execute in declared order.
* IndexedDB coverage in `test_embed_builder_boot.js`: restore, blocked, absent, write-failure; per-file limits; five-field round
  trip; typing; teardown.

## 5b. Real-browser gate (required before commit) — 52/52 checks

Run in Chromium 153 (post-fix harness `/tmp/pwtest/gate.mjs`, Playwright-core + the app served by Flask on `127.0.0.1:8090`
under a seeded scratch DB and a real signed session cookie). The pre-fix gate was a separate 47-check artefact; it and its
`/tmp` scratch directory were lost when this session's sandbox was recycled, so it was rebuilt as the 52-check harness below —
the same walks, plus the checks the F1–F3 fixes required, an identity call forced to HTTP 500, and a template round trip
through a hard reload. htmx, jQuery and select2 are served from the real npm packages by the harness
because the sandbox has no internet; the document is served with their `integrity` attributes stripped, since those only ever
prove CDN bytes this sandbox never fetches. The browser runs in `Africa/Cairo` so the timestamp round-trip is a real one.

| Check | Result |
|---|---|
| Direct open, hard refresh | 200; page module paints **2 ms** after its own init with **0 of its own API calls before it** (HEAD, same harness: 4 calls — 8 with its double execution — all before any paint) |
| Post-paint calls | 5: `limits`, `templates`, `roles`, `channels`, `botprofile/config` |
| Bot identity | Correct name + avatar from the page; **the identity is still correct with `/api/botprofile/config` forced to HTTP 500** |
| Typing | Preview updates; editor DOM **byte-identical** (4 749 → 4 749 B); **0** `addEventListener` during the burst |
| Emoji search/filter | 274 → 56 cells; **0** per-cell listeners |
| Image URL / `javascript:` URL | URL renders; `javascript:` never becomes an `href`; a real URL becomes the title link |
| Local upload validation | 21 MiB file accepted **with** the advisory warning; 30 MiB refused against the 24.94 MiB budget |
| Save → reload → load (draft cleared first) | All five fields round-trip: author icon, author URL, footer icon, embed URL, timestamp (Cairo TZ) |
| Loaded embed renders | Author icon + footer icon + title link all present in the preview |
| Legacy (pre-Phase-0, bare embed JSON) row | Loads, **icons included** (the §3.11 fix) |
| Draft restore | Survives a hard refresh |
| Leave/return ×4 via htmx | Mounted every time, `editorRenders` constant (2), document listeners **23 / 23 / 23 / 23**, **0 errors**, and the in-page instrumentation survived every hop (proves real swaps, not full loads) |
| Browser Back / Forward | Back → working builder, Forward → other page, Back → re-mounted builder |
| Unrelated pages (`/commands`, `/members`, `/leveling`, `/minigames/builder`, `/tickets`) | All 200, content renders, `nav-lifecycle.js` loaded, **no page module mounted** (nothing initialises by accident) |
| Storage blocked (IDB `open` never calls back) | Paints, stays usable, status line explains degraded mode, no unhandled errors |
| `indexedDB` missing entirely | Same: paints, usable, explained, no errors |
| Permanent loading state | 0 visible spinner/loading elements |
| Invalid `attachment://` | Refused server-side with `error_path: embeds.0.image.url` before any Discord call |
| Code rendering, 8 cases (§3.12–3.14) | Info-string fence → `const a = 1;` and **no `js` in the markup**; no-info fence; `**not bold**` stays literal; inline span; ```` `` `x` `` ```` → `` `x` ``; code beside text; three spans in order; ```` `` ``` `**x**` ``` `` ```` → the whole run, **0 control characters** |
| Long fenced line | Block's `scrollWidth` **496 ≤ 496 px** in the preview box, document overflow **0 px** (pre-fix: 729 px in a 520 px box) |
| Code block styling | `background rgb(43,45,49)`, `padding 8px 10px`, `font "JetBrains Mono"`, `white-space pre-wrap`, `overflow-wrap anywhere` |
| Emoji popover | Closes on an outside click; **Escape does not close it** (pre-existing, out of scope — §9) |
| Console errors on the builder | **None** (and none against the app: every failed request is an off-sandbox image/CDN host) |

**Console errors elsewhere in the app are pre-existing.** The gate walks `/commands`, `/leveling`, `/minigames/builder` and the
Back/Forward path, which produce `Identifier 'X' has already been declared` / `currencyLabelText is not defined`. To attribute
them I checked out an unmodified HEAD (`git worktree`, served on `127.0.0.1:8091`, same DB, same cookie) and ran the identical
walks: **HEAD produces the same errors** (full loads: `/commands`, `/leveling`, `/minigames/builder` identical; Back/Forward:
4 errors at HEAD vs 3 at Phase 0, plus one htmx `TypeError` that only HEAD shows). Root cause: those pages' inline scripts run
twice (htmx `load` + `dashboard.js`'s `reInitDashboardComponents`) and htmx's history restore re-executes `dashboard.js`
(`CURRENCY_KEY_MAP`). They are not builder errors, and Phase 0 neither adds nor widens them; the extracted builder page is
clean on every path (HEAD's builder throws `Cannot read properties of undefined (reading 'esc')` on htmx navigation).

## 6. Regressions found

* **One real regression, found by the browser gate and fixed: template saving returned HTTP 500** (`NameError: name
  '_validate_embeds' is not defined`) — the helper was lost while the Phase 0 edit rewrote `dashboard/api/embedbuilder.py`, so
  *Save template* looked like a connection error and saved nothing. Restored (§3.10); the gate's save → reload → load check now
  passes, and the direct `curl` to the endpoint returns `{"success": true}`.
* **One pre-existing data-loss gap closed** while verifying backward compatibility: legacy flat `author_icon`/`footer_icon` keys
  were dropped on load (§3.11), also at HEAD — six new composer checks pin it.
* The only harness that broke was `scripts/test_embedbuilder_attachment_logic.js`, which
  extracted its subjects from the template that Phase 0 emptied — retargeted to `embed-builder-page.js`, same assertions, 26/26.
* **Second pass (the F1–F3 fix), regressions found: none in the product.** The fixes were re-verified against the whole
  composer contract: 41 payload/preview comparisons vs HEAD still show zero payload differences and the export surface is
  unchanged (the minigames consumer is unaffected); the only deltas are the intended markdown ones, and a preview diff traced
  the single remaining difference to §3.6/§3.7's footer work, not to this pass. Three harness bugs surfaced while writing the
  gate and were fixed in the harness, not the app: the title field was never filled before asserting a title link; a
  `/mono/` font assertion was matched against the quoted computed family `"JetBrains Mono"`, `"Fira Code", monospace`; and a
  stray browser-global reference. One app-side detail was recorded as out of scope: the emoji popover does not close on
  Escape (pre-existing).
* **Two pre-existing bugs were found and deliberately NOT fixed** (both outside F1–F3 and both unchanged at HEAD):
  `renderToken` emits an **unterminated `alt` attribute** on inline emoji images
  (`<img class="eb-inline-emoji" src="…" alt=":name:">` with no closing `"` — identical at HEAD, so an attribute-parsing
  difference, not a Phase 0 change), and a **space inside a bold run disables it** (`**x**` renders bold, `** x **` does not —
  Discord's marker rule is about the character after the opening marker, not a run of spaces). Both belong to the Phase 1
  markdown corpus.
* The one trade-off is honest to state: a **cold** load of the builder now fetches `embed-composer.js` +
  `embed-builder-page.js` as separate static files (browser-cached, 304-revalidated) instead of parsing an inline block — one
  extra round trip on the very first visit. Every later navigation, every htmx return and every reload is a cache hit, and the
  old inline block was transmitted **and parsed twice** on every hard load, so the page is smaller (−74 %) on balance.

## 6b. Known deviations left in place (deliberate, for the Phase 1 corpus)

These are outside the three approved fixes, reproduce identically at HEAD, and are recorded rather than changed — they all live
in the markdown engine that Phase 1 replaces with a corpus-tested one:

* A mention/emoji **token inside a code span still resolves** (Discord shows the token as literal text). Token stashing happens
  before the code pass; changing it would alter rendering for every consumer of this module. The tests assert what the fix
  owns — the code span stays one span and nothing leaks.
* `** x **` is not bold (a space after the opening marker disables it), and the `srcset` attribute keeps its leading whitespace
  because the attribute regex captures it — both observable only as attribute-level detail.
* The **unterminated `alt`** on inline emoji images (§6).

## 7. Migration impact

**None.** No schema change, no new table, no route or response-shape change. `GET /api/embedbuilder/limits` is additive and
read-only; `/api/embedbuilder/send` accepts the same bodies and only *rejects* what Discord would have rejected anyway (plus a
few genuinely broken payloads). Saved embeds/messages load unchanged — the five new fields are emitted only when set, so old
`embed_templates` rows round-trip byte-identically. Draft storage keeps the same database/version/store/key
(`nero_embedbuilder` / `draft` / `composer`), so drafts survive in both directions, including a rollback.
`?legacy=1` is **not** implemented in Phase 0 — it only becomes meaningful in Phase 1, when a second builder exists.

## 8. Rollback

* **Now (uncommitted):** `git checkout -- dashboard/api/embedbuilder.py dashboard/app.py dashboard/static/css/embed-composer.css
  dashboard/static/js/embed-composer.js dashboard/templates/base.html dashboard/templates/manage/embedbuilder.html
  scripts/test_embed_composer.js scripts/test_embedbuilder_attachment_logic.js` and delete the seven new files.
* **Once committed:** `git revert <phase-0-commit>` — the commit is self-contained.
* Why the revert is clean: the registry is inert unless a page declares `data-page-module` (only the builder does), and every
  `embed-composer.js` addition is opt-in (`opts.extraFields`, extra payload keys emitted only when set) — the other consumer
  (`systems/minigame_builder.html`) behaves exactly as before, as `test_embed_composer.js` (83) asserts. The one *shared*
  behavioural change is the markdown pass (§3.12–3.14): it can only alter text that contains backticks, and it makes that text
  match Discord instead of the old renderer.
* There is no data to migrate back: nothing writes a new format, and the IDB keys are unchanged.

## 9. Remaining risks

1. **Typing still rewrites the whole preview DOM** (measured: 40 `<img>` elements re-created for 20 keystrokes, down from 80).
   The §8 invariant "typing must not reload images" is only *halved*, not met — the preview is rebuilt from scratch on every
   keystroke. For `blob:` previews there is no network reload, but it is a re-decode per keystroke. **This is the first item for
   Phase 1** (differential/patched preview updates). It is not a correctness bug, and it is strictly better than before.
2. **The browser gate is done (§5b), with two things it cannot cover here.** (a) The real Discord round-trip — sending a
   message, an `attachment://` image actually rendering, and how the markdown fixes look in the client — needs the bot token and
   a live guild; the server-side validation and payload shape are tested, the send itself is not. The same applies to the
   code-rendering fixes: their rules come from Discord's own markdown spec and are pinned by 30 unit checks plus the browser
   gate, but no fence has been rendered by the real client from inside this sandbox. (b) The pre-existing console
   errors on `/commands`, `/leveling`, `/minigames/builder` and the Back/Forward path (duplicate inline-script declarations)
   are out of Phase 0's file table; they are reproduced identically at HEAD and are a good candidate for a small, separate
   cleanup pass (they will keep showing up in any console-error check until then).
3. **`nav-lifecycle.js` loads on every page** (one extra script + 6 document/window listeners) and does nothing without a page
   root. Bounded and measured, but it is a new global cost that did not exist before this phase.
4. **`_bot_identity_for_page` reads SQLite directly** (`sqlite3.connect(DB_PATH, timeout=2)`) instead of going through
   `utils/bot_profile.get_guild_bot_profile`, which is `async`/aiosqlite and would need either an event-loop hop on the page
   render or an edit to a shared file (`async_utils.py`) — I stopped rather than touch shared code. Behaviour is identical; the
   duplication is two columns and fails safe (no identity → default preview chrome).
5. **Advisory caps move.** The 20 MiB per-file number is Discord's current free-tier policy and is env-overridable
   (`NERO_ATTACHMENT_MAX_BYTES`); the UI warns rather than blocks, and the server reports the same number so the two cannot drift.
6. **`?legacy=1` / legacy-branch removal** is deferred to the phase that introduces the second builder (per your decision), so
   Phase 0 has no flag to remove later.
7. **Small follow-up, not done (outside the approved file table):** add `nav-lifecycle.js` and `embed-builder-page.js` to
   `package.json`'s `lint:js` list. They are already `node --check`ed by `scripts/run_js_tests.sh`, so this is cosmetic.
8. **Pre-established UI gaps confirmed in the browser, deliberately untouched** (they are the review's non-blockers, not this
   pass's): the emoji popover does **not** close on Escape (an outside click does); the mention modal has no `role`/`aria-modal`/
   `aria-labelledby`; `#eb-status` is not an `aria-live` region; the field inputs have no `maxlength`; 13 labels have no `for`;
   no builder input has an `id`; at 1024/820/420 px the page overflows horizontally by 170/374/106 px; and *Send* sits ~750 px
   below the fold at 1440×1000.
9. **Required Phase 1 prerequisite — the v1 draft store must not be shared blindly.** The v1 builder uses a **single**
   IndexedDB key: database `nero_embedbuilder`, store `draft`, key `composer` (`embed-builder-page.js` L237–241; `saveDraft`
   L443 also persists attachment blobs under it). Before `/embed-builder/v2` writes *any* draft state it must define an explicit
   **draft namespace/version plus a migration/read-old/write-new strategy**, so a v2 draft cannot silently overwrite a v1 draft
   (and a user who opens v1 and v2 alternately cannot lose either). Phase 0 deliberately leaves the store exactly as it was —
   drafts keep working across the change — but this is a constraint on Phase 1, not a detail to rediscover later.

---

**Phase 0 is complete: the three approved blockers (F1–F3) are fixed, the real-browser gate passes 52/52, and the change set
is awaiting your go-ahead to commit.** Nothing has been committed — the tree is exactly as described above, and `?legacy=1`,
the roles/channels TTL cache and the differential preview stay in Phase 1. Phase 1 has not been started; no Phase 1 file has
been touched, and the planning documents remain untracked and unmodified.
