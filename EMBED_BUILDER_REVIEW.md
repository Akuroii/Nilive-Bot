# Embed Builder redesign — architecture & UX review (pre-implementation)

**No application code has been changed for this document.** Working tree is clean apart from this
file. Measurements below were taken in this checkout on this machine, with the commands shown, so
every number can be reproduced.

> **Two corrections to my first proposal, found while doing this review. Both change the plan.**
>
> **1. The "moderator gets a 403 on bot identity" finding is wrong.** `utils/permissions.py` gates the
> Embed Builder *page* at `LEVEL_OWNER` (deliberately — the comment there explains that live sending is
> more powerful than the old tool). So only owners can open the builder, and owners always pass
> `LEVEL_ADMIN` on `/api/botprofile/config`. The unreachable-403 story is dropped; §7 keeps the real
> problem (a **blocking** Discord round-trip on init) and answers the permission question on the merits.
>
> **2. "`innerHTML` is the performance problem" is only half right.** Measured: rebuilding the whole
> *preview* costs **0.28 ms of JS and 75 DOM nodes** for a realistic 2-embed message. The heavy DOM
> churn is in the **editor**: `mountEditor.render()` at Discord's maximum (10 embeds × 25 fields) builds
> **230 KB of HTML / 1 870 elements / 830 form controls**, and it runs on every add, delete, duplicate and
> reorder. So the priority order in §3/§5 is: editor targeted updates > preview patching (which is mostly
> about *image identity and correctness*, not CPU) > everything else.

---

## 1. First: separate bugs from redesign

Classification of everything found. **A** = fix immediately, no design change. **B** = architecture.
**C** = UX. **D** = new functionality.

### A — Existing bugs (fix immediately, behaviour-preserving)

| ID | Bug | Evidence | Why it is a bug, not a preference |
|---|---|---|---|
| A1 | **The page never initialises after an htmx navigation.** Sidebar links swap only `#content-area`; `{% block scripts %}` (where the whole page script lives) renders *outside* that element, so it is never re-executed. | `base.html`: `hx-target="#content-area"`/`hx-select="#content-area"`; rendered base.html puts `#content-area` at line 461 and `{% block scripts %}` at 554 (verified by rendering the template with Jinja2 — see §4). `manage/embedbuilder.html` opens `{% block scripts %}` *inside* `{% block content %}` but it is still emitted after the swap target. | First load works, second visit is a dead page. Directly explains "I need to refresh". |
| A2 | **`openIdb()` can hang forever.** Only `onsuccess`/`onerror` are handled; no `onblocked`, no timeout. `init()` `await`s `restoreDraft()` before `renderAll()`. | `embedbuilder.html` `openIdb()`; `init()` at the bottom of the script. | A blocked upgrade (second tab on an older version) leaves the promise pending → permanently blank builder. Second, independent cause of "refresh fixes it". |
| A3 | **Init is not idempotent and has no teardown.** ~45 `addEventListener` calls plus two `document`-level ones (`click` for the emoji popover, `keydown` for undo/redo). | `embedbuilder.html`: lines 512, 1160 are document-level; 331–1160 element-level. | Any future path that re-runs init (which is exactly what the A1 fix requires) would double every handler. Must be fixed *with* A1, not after. |
| A4 | **Save/load silently drops fields.** `cleanEmbedForPayload()` emits only `author.name`, `footer.text`, `image.url`, `thumbnail.url`; `embedFromApi()` reads only those back. Author icon, author URL, footer icon, embed URL and timestamp have no UI at all. | `embed-composer.js` `cleanEmbedForPayload`/`embedFromApi`; editor markup in `embed-composer.js` `mountEditor`. | Round-trips lose data. `cogs/embedbuilder.py:build_embed()` already reads `footer_icon`/`author_icon` — keys the dashboard never writes. |
| A5 | **Attachment preview resolution forces a full second render.** `refreshAttachmentPreview()` calls `_queuePreviewRefresh()` twice (once on start, once on settle), each of which re-renders the grid *and* the preview. | `embedbuilder.html` `_queuePreviewRefresh`/`refreshAttachmentPreview`. | Every uncached image costs ≥2 whole-preview rebuilds; with several attachments these interleave while typing. |
| A6 | **Emoji grid rebuilds ~282 buttons with 3 listeners each on every search keystroke, undebounced.** | `renderEmojiGrid()` + `emojiSearch.addEventListener('input', …)`; 282 emoji in `UNICODE_EMOJI`, 8 categories. | ~850 `addEventListener` calls and a full innerHTML swap per keypress while typing in the emoji search box. |
| A7 | **IndexedDB draft writes clone attachment `Blob`s on a 500 ms typing debounce.** | `saveDraft` in `embedbuilder.html`. | A 20 MB image is structurally cloned in a storage transaction while the user types; blocks the main thread and can hit quota. |
| A8 | **Per-file upload size is never checked; the total constant is stale.** Server checks only the 25 MB total; there is no per-file check anywhere. | `dashboard/api/embedbuilder.py` (`MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024`); client checks only the total. | Discord's free-tier per-file cap is 20 MB; a 24 MB file passes our checks and dies at Discord with a generic error. |
| A9 | **Embed images never reference uploads.** `attachment://` is never produced, so a local file can never fill Image/Thumbnail/Footer icon/Author icon — it can only appear as a separate file under the message. | No `attachment://` anywhere in the repo; `/api/embedbuilder/send` sends `embeds` untouched. | The feature cannot work as intended today; this is the "correct behaviour" gap behind requirement 2 of the brief. |
| A10 | **Broken image URLs render as an empty box** — indistinguishable from "empty field" and "still loading". | `renderPreview()` emits `<img src="…">` with no error state. | Reported as a bug by anyone who ever typo'd a URL; costs one `onerror` handler everywhere. |
| A11 | **Preview header avatar can be blank for a while** (identity loads async, initial value `{name:'Nero', avatar:null}`), and the *same* logic is duplicated in `minigame_builder.html`. | `loadBotIdentity()` in both pages; `renderPreview` renders `<div class="eb-msg-avatar"></div>` when null. | Flash of empty avatar on every load; two implementations to keep in sync. |
| A12 | **Blocking Discord calls on the builder's init path.** `/api/botprofile/config` calls `get_live_bot_member()` → synchronous `requests.get(timeout=8)`; `/api/guild/roles` and `/api/guild/channels` each do a synchronous `requests.get(timeout=8)` with **no cache**. | `dashboard/api/botprofile.py`, `utils/bot_profile.py`, `dashboard/api/core.py`; `async_utils.run_async` is fine (persistent loop) — the blocking is the `requests` calls themselves. | Three serial Discord round-trips block a Flask worker per builder load; a slow/rate-limited Discord stalls page init for up to 24 s worst case. |
| A13 | **Two template APIs with different shapes on one table.** `app.py` writes a bare embed dict (`json.dumps(embed)`); `api/embedbuilder.py` writes `{content, embeds}`; both read the same `embed_templates` rows. | `dashboard/app.py:2578–2640` vs `dashboard/api/embedbuilder.py:150–240`; the cog copes with both. | Not user-visible today, but it is why "which save does what" is confusing and it must be handled by any migration. |

### B — Architecture problems (design-level, no user-visible bug by themselves)

| ID | Problem | Evidence | Consequence |
|---|---|---|---|
| B1 | **Mutable shared state with no owner.** `state.embeds` objects are mutated in place by DOM handlers inside the shared editor while the preview reads the same objects. Correctness depends on every call site remembering to call `renderPreview()`. | `mountEditor`'s `wire()`: `embeds[i][field] = el.value`; page handlers call `renderPreview(); saveDraft(); pushHistory()` by hand. | Nothing can be optimised, tested in isolation, or reasoned about; every new field is a new chance to forget a render. |
| B2 | **Preview is built as one HTML string and assigned with `innerHTML`.** | `embed-composer.js` `renderPreview()` last line. | Image nodes are destroyed per render (decode cache invalidated, flicker, lost scroll); no per-element state can persist. |
| B3 | **Editor is rebuilt wholesale** on any structural change (all cards, all fields, all listeners). | `mountEditor.render()` → `wire()`. | 230 KB / 1 870 nodes / 830 inputs worst case (measured); focus and caret must be worked around; `content-visibility` only hides paint. |
| B4 | **Attachment lifecycle is page-owned but preview-facing**, documented in long comments because the boundary is unclear. | `_blobUrlCache` in the composer vs `getAttachmentPreviewUrl`/`revokeAllAttachmentPreviews` on the page. | Two owners for one concern; blob URLs can outlive their attachment (leak) and the CSP probe exists as a workaround for a symptom rather than a boundary. |
| B5 | **Builder state and Discord payload are separate shapes with two conversion functions**, and the send path serialises directly from UI state instead of from a validated model. | `cleanEmbedForPayload` (UI→wire) and `embedFromApi` (wire→UI); `/api/embedbuilder/send` re-validates only counts. | Drift (A4) and no single place to validate.
| B6 | **Save/load model is "template by name"**, primary-keyed `(guild_id, name)`, `INSERT OR REPLACE`, no revision, no provenance, no separation between embed and message. | `dashboard/api/embedbuilder.py`; `database.py` `embed_templates`. | Renaming = data loss (REPLACE overwrites), no history, and no place for components/actions to live. |
| B7 | **No normalized model.** Editor objects *are* the model, and they carry UI-ish defaults (`color: '#7c5cbf'`) into payload conversions. | `blankEmbed()`. | Can't validate, can't version, can't reuse for components. |
| B8 | **IndexedDB is a single point of failure for first paint.** | A2. | Must become an optimisation, never a dependency. |

### C — UX improvements

| ID | Improvement | Note |
|---|---|---|
| C1 | Footer icon + author icon + author URL + embed URL + timestamp inputs (all verified-supported by the API). | Fixes A4 and completes the embed object. |
| C2 | One image control used by all four image slots: `URL | Upload`, with drag & drop, paste, replace, remove, test, and explicit per-field state. | Requirement 2 of the brief. |
| C3 | Structure rail (message → embeds → fields / rows → components) + contextual inspector instead of one long form. | Prevents the "giant form" failure mode. |
| C4 | Validation surfaced next to the field, with friendly copy and a jump-to-issue strip. | Requirement 13. |
| C5 | Broken/loading/blocked image states instead of empty boxes. | Fixes A10. |
| C6 | Emoji picker available in *every* text and component field (today it only targets the content textarea), with recents, search, guild/other-server/app sources, remove. | Reuse what exists; don't rebuild. |
| C7 | Role picker with search + chips (multi-select) as a reusable control. | Needed by every role-bearing action. |
| C8 | Preview device toggle, and clicking a preview element selects its node in the rail. | Keeps preview and configuration in one mental model. |
| C9 | Draft/status affordance: "Draft · saved 2m ago · unsaved changes", plus "discard draft?" on new-message. | Requirement 18. |
| C10 | Responsive behaviour: rail → dropdown, preview → toggle pane, modals → drawers. | Requirement 19. |

### D — New functionality

| ID | Feature | Depends on |
|---|---|---|
| D1 | Button components (5 styles incl. link, emoji, disabled, custom_id) | B7, C6 |
| D2 | Select menus (string/user/role/mentionable/channel) with options editor | B7, C6, C7 |
| D3 | Action system + action config (add/remove/toggle roles over **many** roles, URL, reply, custom slot) | D1, D2, B7 |
| D4 | Role sets (named reusable role groups) | D3 |
| D5 | Saved Messages (embed snapshot + components + actions + policies) with revisions and publications | B6, D1–D4 |
| D6 | Publish / update-live-message flow + CDN capture for uploaded images | D5, A9 |
| D7 | Components builder runtime (`cogs/components.py`): persistent views, action executor | D3, D5 |
| D8 | Emoji app-import inside component fields (reuses `/api/app-emojis/import`) | D1, D2 |

**Net:** A-items are ~12 concrete bugs, six of them user-visible today. B-items are the reasons A-items
keep coming back. C/D are the redesign, and they are only worth doing because B7/B6 make them possible
without new drift.

---

## 2. Protect the existing project — dependency / risk map

### 2.1 Shared artefacts (what else touches what the builder touches)

| Shared artefact | Other consumers | Touch it? | Why / risk |
|---|---|---|---|
| `utils/emoji.py` (`parse_emoji_input`, `emoji_cdn_url`, `is_custom_emoji_token`) | `dashboard/utils/check_icon.py`, `dashboard/utils/currency_ctx.py`, `rank_card_renderer.py`, `scripts/test_check_emoji.py`, `scripts/test_currency_icon.py` | **Additive only** — never change existing semantics | Currency icons and the rank card depend on the exact parser contract (`<:name:id>`, `<a:name:id>`, bare id). |
| `utils/app_emoji_cache.py` | `dashboard/api/economy_shop.py`, `dashboard/utils/check_icon.py` | **No change** | Currency form uses the same cache to name bare ids. |
| `/api/app-emojis`, `/api/app-emojis/import` (live in `api/embedbuilder.py`) | Builder page only today, but `economy_shop` uses the module | **Keep routes where they are, or move with an alias** | Moving a route is a breaking change for any bookmark; a 301/alias costs nothing. |
| `utils/bot_profile.py` (`get_live_bot_member`, `apply_bot_profile_via_rest`, `get_guild_bot_profile`) | `cogs/botprofile.py` (`/botprofile_set`, `/botprofile_view`), `dashboard/api/botprofile.py` (page + API) | **Additive**: add a cached, cheap "identity" read; do not alter the existing functions | The cog and the config page share them; a behavioural change to `apply_bot_profile_via_rest` would affect live bot profiles. |
| `embed_templates` table | `cogs/embedbuilder.py` (7 slash commands), `dashboard/api/embedbuilder.py` (5 routes), `dashboard/app.py` (4 legacy routes) | **Never migrated destructively**; new store alongside, importer reads old | Two other writers exist. A schema change or a "clean up" here breaks slash commands *and* the legacy page routes. |
| `/api/guild/roles`, `/guild/channels`, `/guild/emojis`, `/guild/emojis/external`, `/guild/resolve-user/<id>` | `NeroSelect` (`nero-select.js`) used across many pages (`members`, `moderation`, `tickets`, `leveling`, …), `nero-alias-input.js`, economy currency picker | **Do not change response shape**; extend with optional fields if needed | `NeroSelect` reads `id`, `text`/`name`, `color`, `type_icon`; changing a key silently breaks every picker in the dashboard. |
| `dashboard/static/js/embed-composer.js` public API | `systems/minigame_builder.html` uses `blankEmbed`, `renderPreview`, `mountEditor`, `cleanEmbedForPayload`, `embedFromApi` | **Freeze those five exports**; internals may change | `scripts/test_embed_composer.js` asserts them; the minigames builder is a shipped feature. |
| `scripts/test_embed*` + `scripts/test_embedbuilder_attachment_logic.js` + `npm test` / `scripts/run_js_tests.sh` | CI (see `ci-tests-workflow.yml.example`) | **Keep green in every phase** | The attachment harness reads the real template + module; it is the regression net for the CSP/blob work. |
| `database.py` `init_db()` | Everything (bot + dashboard start-up) | **Additive `CREATE TABLE IF NOT EXISTS` only** | `init_db()` runs on every start; a bad migration bricks both processes. |
| `dashboard/templates/base.html` | **All 41 pages** | **One narrowly-scoped change** (page lifecycle hook) | Every page depends on it; the change must be inert for pages that define no module. |
| `dashboard/static/js/dashboard.js` `reInitDashboardComponents` | Commands page, all pickers | **Reuse the existing hook; don't add a second one** | Two competing init hooks would be a new class of bug. |
| `cogs/reactionroles.py` + `reaction_roles`/`reaction_role_panels`/`reaction_role_expiry` | Reaction-roles page, live panels in Discord | **No change until Phase 5; then additive** | Live member-facing panels. `restore_views()` runs on every `on_ready`. |
| `rr_panels` + `/api/save-rr-panel`, `/api/rr-panels` | `manage/reactionroles.html` | Leave as-is until that page is replaced; then delete with the page | Each writer's "buttons JSON" is unused by the bot (verified: nothing reads `rr_panels` except the dashboard). |
| `dashboard/api/embedbuilder.py` `/embedbuilder/send` | Builder page only | **Keep working unchanged** through Phases 0–3; add the new publish path beside it | It is the only working "send to Discord" path today. |
| CSP in `dashboard/app.py` (`img-src 'self' data: blob: https:`) | Every page's images | **No change needed** (blob: already allowed) | The blob probe in the composer stays as defence-in-depth. |

### 2.2 Per-change risk table

| Planned change | What it changes | What depends on it | What could break | Regression prevention |
|---|---|---|---|---|
| Page lifecycle hook in `base.html` + module contract | Adds `window.NERO.pages.<name>.init/destroy` calls on `htmx:afterSwap`, `htmx:load`, `htmx:historyRestore`, plus `beforeSwap` teardown | All 41 pages (inert unless they register a module), `reInitDashboardComponents` | Double init if a page also self-inits; teardown running before a swap that then fails | Idempotency marker per root (`data-nero-init`, mirroring `NeroSelect`'s `dataset.nsInit`); `destroy()` only removes its own listeners (named functions kept in a registry); a Node harness simulates swap/return/back and asserts exactly one init per mount |
| Composer `renderPreview` patching | Internals of a shared module | Minigames builder, `test_embed_composer.js`, `test_embed_attachments.js` | Layout differences in the minigame preview; harness assertions on the HTML string | Keep `renderPreview(mount, data)` signature; add golden-HTML assertions for the minigame shape; run both harnesses in the same commit |
| New normalized model + payload builder | Adds `embed/model.js`, `utils/embed_schema.py` | Nothing yet | — (additive) | Payload harness compares against the *current* `cleanEmbedsForPayload` output for the fields that exist today (byte-for-byte), proving "no behaviour change for what already works" |
| Image/URL mode on the four image slots | Editor markup + payload for image fields | Send path (`attachment://`), minigames builder (URL-only) | Sends that now carry files the old path didn't; `MAX_*` constants | Server-side gate before the Discord call; per-file + total checks; the existing send route untouched until Phase 2 flags it on |
| New save/load store | New tables + new routes + new library UI | Builder page, later components builder | Old templates must still load; the cog must still see its rows | Importer is read-only against `embed_templates`; a Python harness loads every legacy shape (bare dict, `{content,embeds}`, corrupt JSON, missing keys) and asserts a valid v2 document or a stated conversion report |
| Reaction-roles → Components (Phase 5) | New cog + new tables; old tables untouched | Live panels, `expiry_check`, `on_member_update` | Duplicated role handling if both run; expired-role sweeps touching new rows | Both cogs loaded in a test guild; the executor reuses the *same* `reaction_role_expiry` sentinel semantics; a shared test asserts a role is never granted twice or swept twice |
| Emoji picker generalisation | Extracted from `embedbuilder.html` into a component | Currencies (read-only via `parse_emoji_input`), minigames | Emoji token format drift | Reuse `parse_emoji_input` server-side to validate every token the picker can produce (round-trip harness) |

---

## 3. Performance investigation (the whole chain)

### 3.1 What triggers rendering, and how often

| Trigger | Renders editor? | Renders preview? | Renders attachments? | Writes IDB? | Pushes history? |
|---|---|---|---|---|---|
| Content keystroke | no | **yes (full)** | no | yes (500 ms debounce) | yes (350 ms debounce) |
| Embed field keystroke | no | **yes (full)** | no | yes | yes |
| Colour picker drag | no | **yes (full, many per second)** | no | yes | yes |
| Add/duplicate/delete embed | **yes (all cards)** | yes | no | yes | yes |
| Add/delete field | **yes (all cards)** | yes | no | yes | yes |
| Attachment added/removed | no | **yes (×2 via `_queuePreviewRefresh`)** | yes | yes | *(not in history)* |
| Attachment preview resolves | no | **yes** | yes | no | no |
| Template load / clear / undo / redo | **yes** | yes | yes | yes | yes |
| Emoji popover search keystroke | — | no | — | no | no, but **rebuilds up to ~282 cells + ~850 listeners** |
| Emoji hover (`mouseenter`) | — | no | — | no | no, but rewrites the hover bar's innerHTML per hover |

Counts: 15 `renderPreview()` call sites, 6 `renderAll()`/structural paths, 2 document-level listeners,
~45 element listeners, 13 `fetch()` call sites.

### 3.2 Measured costs (this checkout, `node v22.22.3`)

Probe 1 — preview (`/tmp/perfprobe.js`, real module, window shim, payload = 2 embeds / 8 fields /
4 component buttons / 1.1 k-char description):

```
renderPreview JS time ....................... 0.28 ms per call
generated HTML .............................. 5 459 bytes
elements created per render ................. 75
renderDiscordMarkup (description) ........... 0.087 ms per call
40 simulated keystrokes (JS only) ........... 8.8 ms total
```

Probe 2 — editor (`/tmp/perfprobe2.js`, real `mountEditor`, stub container):

```
editor: 1 embed, 0 fields    →   3.6 KB ·   37 elements ·   8 inputs · 0.015 ms
editor: 3 embeds, 5 fields   →  21.4 KB ·  201 elements ·  69 inputs · 0.094 ms
editor: 10 embeds, 25 fields → 230.5 KB · 1 870 elements · 830 inputs · 0.719 ms  ← Discord maximum
```

Reading these honestly:

- **JS string-building is not the bottleneck anywhere.** 0.28 ms and 0.72 ms are noise.
- **The browser is.** Handing the DOM 230 KB of new markup and 1 870 fresh elements per structural edit
  (and 830 fresh form controls, which carry focus/IME state) costs tens to hundreds of milliseconds and
  destroys caret/scroll/IME state — that is why the current code *already* avoids re-rendering on plain
  field edits (a workaround, not a design).
- **The preview's cost is not CPU, it is image identity.** 75 elements rebuilt means every `<img>` is
  torn down: decode cache invalidated, requests re-issued, lazy state lost, scroll position reset — for
  a subtree the user is looking at while typing. Plus A5 doubles it whenever an attachment resolves, and
  A7 adds main-thread storage work into the same keystroke burst.

### 3.3 Specific questions answered

| Question | Answer | Evidence |
|---|---|---|
| Are images recreated unnecessarily? | **Yes.** Every preview render emits fresh `<img>` tags. Embed images also lack `loading="lazy"`/`decoding="async"` (attachments have them). | `renderPreview()`; `getAttachmentPreviewUrl` cache exists but the *element* is still new. |
| Are DOM nodes destroyed/recreated? | **Yes, wholesale** — preview always; editor on structural edits (up to 1 870 nodes). | Probe 2. |
| Are listeners accumulating? | **Not today** (page script never re-runs), **but the moment A1 is fixed naively they will** — ~45 element + 2 document listeners per init. | A3. |
| Are duplicate listeners present? | Per render, the emoji grid re-binds 3 listeners per cell on *new* nodes (so not a leak, but ~850 calls/keystroke). `mountEditor.wire()` re-binds everything on every structural render. | A6, B3. |
| Do components survive htmx swaps? | No page JS survives, but page *state* (blob URLs, IDB connection, timers) is not explicitly released; htmx's history cache (default 10 snapshots) can retain detached DOM, which in turn retains `blob:` URLs and therefore Blobs. | **Needs verification** in Phase 1 with a memory probe (add a 20 MB attachment → navigate away → back → count live object URLs). Listed as a test, not asserted as fact. |
| API calls during typing? | No. API calls happen at init (identity, roles, channels) and on explicit actions (emoji list, user resolve). | 13 `fetch()` sites; none on the typing path. |
| IndexedDB during typing? | **Yes** — 500 ms debounced save that includes attachment Blobs (A7). | `saveDraft`. |
| Attachment URLs resolved repeatedly? | No (one per attachment, cached) **but** resolution triggers two full re-renders (A5). | `_queuePreviewRefresh`. |
| Unnecessary layout/reflow? | Yes: full-subtree replacement forces reflow; the sticky preview (large replaced subtree) is the worst case; `content-visibility:auto` is applied to collapsed cards and attachments, not to the preview. | CSS + B2. |
| Expensive Discord calcs repeated? | Markdown/mention rendering is cheap (0.087 ms) but is re-run for every embed, field and keystroke with no memoisation keyed on `(text, lookupsVersion)`. | Probe 1. |
| Memory leaks? | Two candidates: (a) blob URLs outliving attachments across swaps (unverified, see above); (b) IDB connection kept open across pages. Both are cheap to make explicit in `destroy()`. | A2/B4. |

### 3.4 Performance model

**Current** (per content keystroke):

```
keystroke
 ├─ mutate state.embeds[i] in place                     (cheap)
 ├─ renderPreview(): build 5.5 KB string                (0.28 ms JS)
 │                   + DOM replace of ~75 elements      (browser: parse, layout, paint)
 │                   + N × <img> destroyed & re-decoded (network/decode, flicker, scroll reset)
 ├─ saveDraft() debounced 500 ms → Blob clone into IDB  (main-thread storage work)
 └─ pushHistory() debounced 350 ms → JSON.stringify     (cheap)

Structural edit (add/delete/duplicate/field add):
 └─ editor.render(): up to 230 KB / 1 870 nodes / 830 inputs  ← dominant cost
                      + full re-wire of every listener
                      + preview rebuild (as above)
```

**Proposed** (same keystroke, no framework):

```
keystroke
 └─ store.dispatch(embed.setField(...))                  (pure, structural share)
     ├─ validate() — memoised per node                    (limits from one table)
     ├─ preview patch: text nodes updated in place        (no element churn)
     │    ├─ images: unchanged src → untouched            (decode cache preserved)
     │    └─ markdown: memoised on (text, lookupsVersion) (only the edited field re-parses)
     ├─ inspector: the edited input is the source → no DOM write
     └─ persist: idle-scheduled (≥1.5 s) + pagehide, blobs written once at add-time

Structural edit:
 └─ store.dispatch(...) → rail adds/removes ONE card node
     + inspector re-points to the newly selected node
     + preview patches only the affected embed block
```

Expected effect: structural edits go from "rebuild everything" to "touch one subtree"; typing stops
touching images at all; the only remaining per-keystroke work is one text node update plus one memoised
markdown parse of the changed field (and the existing 0.087 ms cost stays bounded as a result).

---

## 4. HTMX lifecycle — robust initialisation

### 4.1 Why it breaks (verified, not inferred)

Rendering `base.html` with Jinja2 (all `url_for` calls stubbed) gives the real DOM order inside
`<div class="main-content">`:

```
448  <div class="main-content">
461      <div class="page-content" id="content-area">   ← htmx target/select
465  <div id="edit-modal" …>                              ← outside the target
…        <style>…</style>, <script>htmx config + nav</script>
554      {% block scripts %}                              ← outside the target
574  </body>
```

Sidebar links carry `hx-target="#content-area"`, `hx-swap="outerHTML"`, `hx-select="#content-area"`.
Therefore: **markup is replaced, page scripts are never re-executed**, and the only thing that re-runs
is `base.html`'s own `htmx:afterSwap` handler (which re-executes `<script>` tags found *inside* the
swapped fragment — a `.querySelectorAll('script')` on the swapped target; there are none). The
`reInitDashboardComponents` hook in `dashboard.js` proves the house pattern already exists for
DOM-walking components (`NeroSelect.initAll`/`NeroAlias.initAll`, idempotent via `dataset.nsInit`), but
there is no equivalent for *page* modules.

### 4.2 Design: a page-module registry with init/destroy, driven by htmx events

Where things live:

- **`base.html`** (once, never swapped): the hooks, emitting events — not page logic.
- **`static/js/nav-lifecycle.js`** (new, loaded once next to `dashboard.js`): the registry. It owns
  `window.NERO.pages`, calls `destroy()` before a swap and `init()` after one, and keeps the previous
  page module as the *only* current one.
- **Each page** (e.g. `static/js/embed/embed-builder.js`): registers
  `window.NERO.pages.embedBuilder = { init(root), destroy() }` and exposes no globals. The template keeps
  markup plus a single `<script src>` tag (loaded in `{% block scripts %}`, i.e. outside the swap — so it
  loads once); the *page* script is what the registry runs.

Contract (all four points are the answer to "where should init/cleanup live"):

```js
// nav-lifecycle.js — conceptual
window.NERO = window.NERO || { pages: {} };

let current = null;                       // { name, module, root }
const MODULE_FOR_PATH = {                 // explicit, no guessing from the DOM
  '/embed-builder':      'embedBuilder',
  '/components-builder': 'componentsBuilder',
  '/reaction-roles':     'reactionRoles',   // register later, when it moves
};

function mount(evt) {
  const root = document.getElementById('content-area');
  if (!root) return;
  const name = root.dataset.pageModule || MODULE_FOR_PATH[location.pathname];
  if (!name || !window.NERO.pages[name]) return;      // inert for pages without a module
  if (current && current.name === name && current.root === root) return;  // idempotent
  unmount();                                          // never two live modules
  const module = window.NERO.pages[name];
  try {
    module.init(root);                                // root-scoped, may be hydrated
    root.dataset.neroModule = name;                   // marker for harnesses
    current = { name, module, root };
  } catch (err) { reportInitFailure(name, err); }      // A2-style failure must be visible
}

function unmount() {
  if (!current) return;
  try { current.module.destroy && current.module.destroy(); } catch (e) { console.error(e); }
  current = null;
}

document.body.addEventListener('htmx:beforeSwap',   unmount);          // leaving / replacing
document.body.addEventListener('htmx:afterSwap',    mount);
document.body.addEventListener('htmx:load',         mount);            // non-afterSwap loads
document.body.addEventListener('htmx:historyRestore', () => {         // back/forward
  unmount(); mount({});
});
document.body.addEventListener('htmx:responseError', () => {          // failed request
  unmount(); showToast('Could not load that page — try again', 'error');
});
window.addEventListener('pagehide', () => { if (current) current.module.persistNow && current.module.persistNow(); });
document.addEventListener('DOMContentLoaded', mount);                  // hard load / direct open
```

Rules that make the nine required scenarios work:

| Scenario | Mechanism |
|---|---|
| 1. Direct open | `DOMContentLoaded` → `mount()` (also fires on hard refresh, scenario 9) |
| 2. Sidebar navigation to the page | `htmx:afterSwap` → `mount()` |
| 3. Leaving | `htmx:beforeSwap` → `unmount()` (removes document listeners, timers, revokes blob URLs, closes IDB) |
| 4. Returning | fresh `mount()` on the new root — one module instance, new listeners |
| 5. Opening it multiple times | `unmount()` before every `mount()`; module holds no document-level state between instances |
| 6. HTMX swap | hooks above; module never binds outside its `root` except through a small, tracked list |
| 7. Back/forward | `htmx:historyRestore` → unmount + mount (htmx restores cached DOM, so a fresh init is required) |
| 8. Failed request | `htmx:responseError` → unmount + toast; the swap never happened, so nothing half-initialised remains |
| 9. Hard refresh | same as 1; module must tolerate an already-populated DOM (draft restore is async) |

Anti-duplication rules (explicitly required by the brief):

1. **Element listeners** are bound to nodes inside `root` and die with them — no bookkeeping needed.
2. **Document/window listeners** must be created through `module.on(target, type, fn)` which records them
   and removes them in `destroy()`. Direct `document.addEventListener` inside a page module is a review
   rejection (the two existing ones, emoji-popover dismissal and undo/redo keys, are converted).
3. **Timers/intervals/rAF** go through the same registry.
4. **Object URLs** for local files are created and revoked by an asset registry owned by the module.
5. **Idempotency marker** `root.dataset.neroModule` (mirroring `NeroSelect`'s `nsInit`) makes double-init
   a no-op even if two hooks fire for the same swap.
6. `init()` must **never** assume an empty DOM: it reads what the server rendered (`data-*` seeds,
   server-injected JSON) and only adds behaviour.

Nothing here touches the 41 other pages: if a page's root has no `data-page-module` and its path is not
in the map, the registry does nothing.

---

## 5. Preview architecture — which of the four options

| Option | Fit for this project | Cost | Verdict |
|---|---|---|---|
| **A. Targeted DOM updates** (build skeleton once, patch text/attrs by key) | **High.** Plain DOM API, no build step, ~250 lines, fully testable in Node with a tiny stub; keeps the existing "one renderer, two consumers" story | Medium (one-time rewrite of `renderPreview`, keyed nodes) | **Recommended** |
| **B. DOM diffing** (tiny vdom + diff, e.g. a hand-rolled h/preact-like) | Medium. Gets you declarative rendering, but you now own a diff engine and its bugs | High | No — the same benefit is available with keyed patches and far less surface |
| **C. Framework** (React/Vue/Alpine + build step) | **Low.** The dashboard is server-rendered Jinja + htmx + hand-written ES5-safe JS with no bundler; introducing a framework means a build pipeline, a second state system, and a rewrite of the page shell. Violates "respect the current stack" | Very high | No |
| **D. Current system optimised** (keep `innerHTML`, add debounce/memoisation) | Low-medium. Leaves image churn, keyed state, and drift in place; debouncing makes the preview *lag* the input, which the brief explicitly calls a bug ("preview should immediately represent what I type") | Low | **No** — it is the "add a delay" solution the brief forbids |

**Recommendation: A**, with B's discipline (a keyed, declarative description of the message) but no diff
engine.

Design:

```
renderPreview(mount, payload, ctx)
  1. ensureSkeleton(mount, payload)         // creates/removes top-level blocks, keyed by node id
  2. for each keyed block:
       patchText(el, text)                  // textContent — never innerHTML, never re-parsed
       patchMarkup(el, markupHtml)          // only when the memoised markup string changed
       patchImage(el, src)                  // only when src changed → unchanged images are reused
       patchComponents(rowEl, children)     // keyed by component id
  3. write nothing else. No timers. No async.
```

- **Keyed by stable node ids** that live in the model (`EmbedDocument.id`, `EmbedField.id`,
  `ButtonComponent.id`) — already required by the data model for other reasons, so this costs nothing.
- **Memoised markup**: `markupCache` keyed on `(fieldText, lookupsVersion, context)` with a bounded LRU
  (say 200 entries). One keystroke re-parses one field.
- **The renderer takes `ctx.now`** so timestamp output is deterministic (testable in Node).
- **The minigame builder keeps working**: `EC.renderPreview(box, data)` keeps its signature; internally it
  delegates to the new patcher in "replace everything" mode when the mount is empty, which is what that
  page always does.

This is also what makes the *correctness* guarantee real: because the renderer consumes
`toDiscordPayload(model)` and not the editor objects, "preview shows what will be sent" becomes an
assertion (`scripts/test_embed_preview.js`: payload in → DOM string out) rather than a promise.

---

## 6. Discord preview accuracy — the rendering model

### 6.1 Per-surface matrix (this is the core of the "not a generic markdown preview" requirement)

| Surface | Discord behaviour | Preview must do | Notes |
|---|---|---|---|
| Message `content` | Full markdown + mentions + custom emoji + timestamps; grouped into one block above embeds | Full renderer | Also emoji-only sizing when the whole content is emoji+whitespace |
| Embed **description** | Full markdown (headings, lists, quotes, code, spoilers, masked links, mentions only if `allowed_mentions`/bot can resolve) | Full renderer | 4 096 chars |
| Embed **field value** | Full markdown | Full renderer | No headings rendering quirk in Discord (headings do work in field values); lists and quotes do |
| Embed **field name** | Plain text, rendered bold by Discord itself | Plain text (escaped), styled bold | Do **not** apply markdown |
| Embed **title** | **Plain text** — markdown is *not* rendered (asterisks show literally). Discord renders it bold+larger already | Plain text + bold styling | This is the single most common wrong assumption |
| Embed **author name** | **Plain text** | Plain text, smaller | May be a link if author.url is set |
| Embed **footer text** | **Plain text** | Plain text | |
| Embed **url** | Link target of the title | Not rendered as text; show a link affordance on the title in preview | http(s) only |
| Embed **timestamp** | Rendered in the *reader's* timezone (Discord formats it) | Render with the local formatter, marked as such | ISO8601 in payload |
| Select **placeholder / option label / description** | Plain text | Plain text | 150/100/100 |
| Button **label** | Plain text | Plain text | 80 |
| Modal/modals | N/A in a message | — | |

### 6.2 Markdown features to implement (Discord's actual set)

| Feature | Syntax | Notes for the renderer |
|---|---|---|
| Bold / italic / bold-italic | `**x**`, `*x*`, `***x***` | Discord also accepts `__` for **underline**, unlike CommonMark |
| Underline | `__x__` | Not standard Markdown |
| Strikethrough | `~~x~~` | |
| Spoiler | `\|\|x\|\|` | Rendered as a black box until clicked; must not be swallowed inside code |
| Inline code | `` `x` `` | **Formatting is disabled inside** — the current regex pass gets this wrong |
| Code block | ```` ```lang\n…\n``` ```` | Preserves whitespace; disables every other token; Discord shows a language label |
| Blockquote | `> x`, multi-line `>>> x` | `>>>` quotes the rest of the message |
| Headers | `# x`, `## x`, `### x` | Only 1–3; requires the space |
| Subtext | `-# x` | Small grey text; requires the space; line start only |
| Lists | `- x`, `* x`, `1. x`, indented by 2 spaces | No nesting beyond indentation |
| Masked link | `[text](url)` | Works in content and description/field values; **not** in title/footer |
| Bare URL | `https://…` | Auto-links (and Discord may unfurl — we do not simulate unfurls) |
| Escape | `\*` etc. | Escapes the next formatting character |
| Mentions | `<@id>`, `<@!id>`, `<@&id>`, `<#id>`, `@everyone`, `@here` | Resolved via lookups; unresolved → raw id (exactly like Discord) |
| Custom emoji | `<:name:id>`, `<a:name:id>` | CDN image when the bot can reach it; otherwise Discord shows the literal token |
| Timestamps | `<t:epoch>`, `<t:epoch:R/D/T/d/f/F/t>` | Render from `ctx.now`; seven styles |
| *Not supported (must render literally)* | tables, images, HTML, footnotes, math, `---` rules, nested quotes, `####`+ | Show as text, never "fix" them — the user must see what Discord will show |
| ANSI colour blocks | ```` ```ansi ```` | Optional later; not in pass 1 (documented) |

### 6.3 Where the preview must *not* claim parity

- Typography, spacing and avatar pixel sizes: approximate by design.
- Link unfurls, reply previews, "edited" markers, interaction states.
- Whether a *foreign* custom emoji will render in the destination channel (`USE_EXTERNAL_EMOJIS`) —
  the preview shows the emoji; the UI badges it as "may not render here" and the server surfaces
  Discord's error on send.
- Role hierarchy outcomes at click time.
- Whether the *reader* will see a spoiler as revealed.

Each of these becomes a line in a visible "what the preview cannot show" panel (collapsible, once),
rather than a silent mismatch.

---

## 7. Bot avatar / banner / server icon — terminology and permissions

### 7.1 Corrected terminology (names the UI will use, and what each one is)

| UI label | Discord object | Where it appears | Editable from |
|---|---|---|---|
| **Bot Avatar** (also "server avatar" when a per-guild one is set) | user `avatar` / member `avatar` | Message header avatar in a channel | `PATCH /users/@me` (global) or `PATCH /guilds/{id}/members/@me` (per guild) |
| **Server Icon** | guild `icon` | Server list, invite cards, **not** inside a message | Server Settings (not this dashboard) |
| **Discord Profile Banner** | user `banner` / member `banner` | **Profile card only** — never inside a message or embed | `PATCH /users/@me` or the guild member endpoint (already implemented in `utils/bot_profile.py`) |
| **Embed Author Icon** | embed `author.icon_url` | Top-left of the embed, next to the author name | Embed builder (new field) |
| **Embed Thumbnail** | embed `thumbnail.url` | Top-right of the embed | Embed builder (exists) |
| **Embed Image** | embed `image.url` | Full-width under the embed content | Embed builder (exists) |
| **Footer Icon** | embed `footer.icon_url` | Left of the footer text | Embed builder (new field) |

Consequences for the UI:

- No control labelled "Bot Banner" inside the message builder. The banner gets its own, clearly separate
  section titled **"Bot profile assets (not part of this message)"** with a one-line explanation and a
  link to the Bot Profile page — it is shown because users *do* ask where their banner went, and showing
  it with the correct label is better than showing nothing. It is never rendered into the message preview.
- The message preview header uses the **Bot Avatar** only (per-guild → global → generated fallback),
  which is exactly what Discord renders there.
- If someone wants a "banner-like" image inside the message, the honest answer is the **Embed Image**
  field, and the UI says so in that section.

### 7.2 Identity: what is sensitive, and the permission question

Facts found in this checkout:

- The builder **page** is gated `LEVEL_OWNER` (`utils/permissions.py`, with a comment explaining that
  live sending is powerful). So no moderator can currently reach the page or the identity call — the
  403 scenario I previously described is **unreachable**. Corrected.
- The identity data that matters for the preview is: generic bot username, global avatar URL, per-guild
  nickname, per-guild avatar URL, and (for role validation) the bot's highest role position. All of this
  is public information *inside the guild the user is already authenticated for*: any member can see the
  bot's name and avatar; the role position is visible in Server Settings to anyone who can see roles.
- Nothing else is needed. **Not** needed: the bot token, the application id/sec, webhook URLs, the
  bio, global banner, or any other guild's data. The current `/api/botprofile/config` returns more than
  the preview needs (`stored` + `live` blobs, including bio), and does a blocking Discord call.

**Recommendation (two parts):**

1. **Serve the identity with the page, not with a second request.** Render it into the template the same
   way `window.__CURRENCY__`/`window.__CHECK_ICON__` already are (`base.html` context processors), e.g.
   `window.__BOT_IDENTITY__ = { name, avatar, guildAvatar, banner, topRolePosition }`. Benefits: zero
   client round-trips, no blocking Discord call on init, no permission question at all, no flash of
   empty avatar, and one implementation shared by the embed builder and the minigames builder.
   Source: a small cached read (`utils/bot_profile.py`: `get_guild_bot_profile` from SQLite +
   `get_live_bot_member` with a short TTL) and the existing role list for the position.
2. **Do not lower the page gate now.** Sending live messages is a genuinely powerful capability and the
   gate was chosen deliberately. If admin access is wanted later, split the gate rather than lowering it:
   *page* (build/preview/save) at `LEVEL_ADMIN`, *publish/update-live* at `LEVEL_OWNER`, with the publish
   button visibly disabled plus an explanation for admins. That is a product decision, not a bug fix, and
   it is not in scope for this redesign.

Also worth fixing while in here (A12): `/api/guild/roles` and `/api/guild/channels` do a blocking
Discord fetch per call with no cache. The builder asks for both at init. A short server-side TTL cache
(e.g. 60 s, keyed by guild) removes two blocking round-trips per load and also speeds up every other
page that uses `NeroSelect` — without changing any response shape.

---

## 8. Image upload architecture — full lifecycle and failure matrix

### 8.1 The verified constraint that shapes everything

Discord accepts **only two** things in an embed image slot (`image.url`, `thumbnail.url`,
`author.icon_url`, `footer.icon_url`):

1. an `http(s)` URL, or
2. `attachment://<filename>` referring to a file uploaded **in the same request** (`files[n]`).

And for embed media, only `.jpg .jpeg .png .webp .gif` are usable (`.pdf`, `.webp` animated caveats,
`.svg`, `.avif` are not). A referenced attachment is **hidden** from the message body — the file card does
not appear, which is both correct and better looking. A `data:` URI, a `blob:` URL, or a
`http://localhost/...` URL **cannot** work: Discord's servers have no access to the browser and reject
`data:` for media. So: no fake local URLs, ever.

### 8.2 Lifecycle

```
① User selects image (browse / drop / paste)
     └─ validate BEFORE anything else:
          extension ∈ {jpg,jpeg,png,webp,gif}  and  bytes ≤ 20 MB (configurable, from limits)
          sniff magic bytes to catch a .png that is really a .pdf
        fail → field shows the reason, nothing is stored
② Stored as a local asset, keyed by content hash (SHA-256, computed incrementally)
     └─ duplicate hash ⇒ reuse the existing asset (thumbnail + footer icon of the same file = 1 upload)
     └─ preview URL: blob: (CSP-allowed) → data: fallback (≤5 MB) → explicit "no preview" reason
③ Draft state (IndexedDB, best-effort)
     └─ the Blob is written ONCE here (not per keystroke); the document stores only the assetId
④ Save (Saved Embed / Saved Message)
     └─ the document stores the asset REFERENCE { assetId, filename, mime, bytes }
     └─ plus, if this config was ever published, `capturedUrl` (the CDN URL Discord returned)
⑤ Send / publish
     └─ multipart: payload_json has image.url = "attachment://<filename>",
        files[n] carries the bytes, attachments[n] = { id: n, filename }
     └─ sanitise + uniquify filenames (a2f9c1-rules.png) so two assets never collide in one request
⑥ Response
     └─ record message_id + publication row
     └─ capture the resolved CDN URLs from the sent message's attachments into the document
        (this is what makes a saved config portable to another device/browser)
⑦ Re-send later
     └─ if the local Blob is present → upload again (fresh attachment, fresh URL)
     └─ if it is absent but capturedUrl exists → send the URL directly (no upload)
     └─ if neither exists → validation error: "re-attach <filename>" (never a silent broken image)
```

### 8.3 Failure matrix (the questions asked, answered)

| Situation | Behaviour |
|---|---|
| Uploads an image but never sends | Asset lives in memory + IndexedDB; the draft keeps it; nothing was ever uploaded to Discord (no orphan on Discord's side — a real advantage of the attachment approach: no public bucket, no cleanup job). |
| Saves the embed before sending | Saved config stores the asset reference (+ `capturedUrl` if previously published). It is sendable from this browser; other devices see "local image missing — re-attach". The UI states this *at save time*, not at send time. |
| Closes the page | IndexedDB draft restore brings back the asset if the Blob was written (it was, at add time). Blob URLs are revoked on teardown; a fresh one is minted on restore. |
| Edits the image later | Replace = new asset keyed by hash + the reference updated in one atomic patch; the old asset is garbage-collected once no field references it (reference counting, not timers). |
| Deletes the attachment | The field goes back to `empty`; the asset's refcount drops; if it hits zero the Blob is dropped and the object URL revoked. No dangling reference is possible because the field *is* the reference. |
| The Discord CDN URL changes | Non-issue for local uploads: we send the attachment again and Discord mints a new URL; `capturedUrl` is only a *fallback* and is overwritten on the next publish. For pasted URLs, the URL is the user's own and we cannot know it rotted — hence the "image didn't load" state and a re-test action. |
| Saved embed reused in another message | The asset reference is copied; both configs reference the same assetId (dedupe by hash means one Blob locally). Sending each message uploads its own attachment — Discord has no notion of a shared attachment across messages. |
| The same image is thumbnail **and** footer icon | One asset, one upload, two `attachment://<same-filename>` references. Discord accepts the same attachment referenced from multiple embed slots. |
| The image upload fails (network, 413, Discord 400) | The send response is parsed per-field: the UI re-shows the failure against the offending field, keeps the local file, and offers retry; nothing is marked as sent. If Discord rejects the media type at send time, we say which file and why (server-side pre-validation should have caught it — that is the point of the shared limits table). |
| Broken pasted URL | Field state `broken` with the HTTP status (on-demand "Test" only — never automatic, to avoid a request per keystroke), plus a retry and a "remove" affordance. |
| Very large file | Per-file check before storing; a >20 MB file is refused with the real number and a suggestion (resize locally — offered as an explicit action, never silent re-encoding). |
| Unsupported format (svg, pdf, heic, avif) | Refused at step ① with a specific message and, where honest, an alternative ("attach it as a normal file instead" / "convert to PNG"). |

**No dangling references** is enforced structurally: the document references assets *only* via
`assetId`s that exist in its own `assets` map, the map is rebuilt from the document on save (so orphan
entries vanish), and validation fails loudly if a referenced asset is missing rather than sending a
broken `attachment://`.

---

## 9. Saved Embed vs Saved Message — and where assets belong

**Recommendation:**

| Object | Contains | Does *not* contain |
|---|---|---|
| **Draft** | the whole working document + asset blobs | any server state |
| **Saved Embed** | one normalized `EmbedDocument` (title, url, description, colour, author+icon, thumbnail, image, footer+icon, timestamp, fields) — **value copy** | content, components, actions, channel, publication |
| **Saved Message** | content + embed snapshot(s) + rows/components + actions + policies + publications | ownership of the assets' bytes |
| **Asset Library** (new, thin) | per-guild index of *known* assets: `id`, `filename`, `mime`, `bytes`, `sha256`, last-known `capturedUrl`, `createdAt`, `refCount` | the bytes themselves for local-only assets |
| **Temporary draft assets** | IndexedDB object store keyed by assetId | server |

**Where do attachments belong?** Three-layer answer, because "belongs to one object" is the wrong frame:

1. **The bytes belong to the browser** (draft scope). We do not host user uploads: there is no public
   bucket, no signed-URL surface, no cleanup job, and nothing to leak. This matches how the project
   already works (the existing comment on `/embedbuilder/send` states attachments are never persisted).
2. **The reference belongs to whatever document uses the slot** — a Saved Embed stores it when a
   thumbnail/image/icon is an upload; a Saved Message stores it for every image in its own snapshot.
   References are copied with the document (copy-on-load), never shared mutable pointers.
3. **The durable identity belongs to the Asset Library** — after a publish, the CDN URL Discord returned
   is recorded against the asset, so the config remains usable when the local file is gone. That row is
   per guild, shared by any config that uses the asset, and is the only server-side state about assets
   (metadata + a URL, never bytes).

Why this split: it keeps a *Saved Embed* purely visual (so it can be reused inside any message), keeps a
*Saved Message* self-contained (so what you send is what you saved), and makes assets reproducible without
turning the dashboard into a file host.

---

## 10. Versioning / copy vs reference

### 10.1 The three models, evaluated against this project's failure modes

| | Model A — Copy-on-load | Model B — Reference | Model C — Versioned |
|---|---|---|---|
| Edit Embed A after 2 messages use it | Messages unchanged | **Both messages change** (live panels silently alter) | Messages unchanged; a new revision exists |
| Live-panel safety | Safe | **Unsafe** — one edit changes member-facing panels | Safe |
| Duplication | Yes (visible, manageable) | None | None |
| "Fix a typo everywhere" | Manual per message, or "pull latest" | Automatic | Automatic if messages opt into "track latest" |
| Storage | grows with copies | smallest | grows with revisions |
| Mental model for a non-developer | "my copy" | "the shared one" | "versions + a copy" |
| Implementation cost | low | low | medium (revision tables, diffs, restore) |

### 10.2 Recommendation

**Copy-on-load (A) is the default, with the revision table from (C) as storage for history, and (B)
available only as an explicit, opt-in link that is never implicit.**

Concretely:

- **Load** (into the embed builder, or into a Saved Message) creates an independent editable copy and
  records `source: { id, revision }` for provenance. The UI shows "from Rules v3" with a
  **[Pull latest changes]** action so the copy can be refreshed deliberately. This is exactly your
  stated preference, and the analysis agrees with it: the failure mode of B is *silent change to
  member-facing content*, which is the worst possible failure for a bot dashboard; the failure mode of
  A is duplication, which is visible and cheap.
- **Every save appends a revision** (never overwrites), so history exists without coupling configs:
  "v1: created", "v2: footer text changed", "v3: image replaced". Restore = load an old revision into
  the editor and save it as a new head. This gives C's benefits with A's safety.
- **Live messages point at a revision**, not at "whatever the embed says today": a publication stores
  `{ messageId, revision, channelId, discordMessageId }`. "Update the live message" is the only thing that
  changes what members see, and it says which revision it is publishing.
- **Explicit reference mode**, if ever wanted, is a checkbox with consequences spelled out ("editing this
  embed will change 2 live messages"), plus a usage count computed server-side. Not in the first pass —
  it is listed as a future extension, not a hidden mode.

---

## 11. Components V2 — why not now, and how it stays a future extension point

**Why not now (verified from the component reference):** enabling `IS_COMPONENTS_V2` (`flags: 1 << 15`)
**disables `content`, `embeds`, `poll` and `stickers`** on that message — they must be empty/null or the
request 400s. Everything this project needs for the current requirements is an *embed* plus interactive
components: a message that has an embed cannot be a V2 message, and a V2 message cannot have an embed.
Building on V2 in pass 1 would mean either dropping embeds (regressing a shipped feature) or maintaining
two incompatible message formats and twice the validation, preview and save/load surface. V2 also brings
containers, sections, text displays, media galleries and separators — a different authoring model, not a
new field on the current one. Rejected for pass 1 with reasons, not by omission.

**How the architecture keeps the door open (cheaply, no speculative code):**

| Already-designed piece | Why it survives V2 |
|---|---|
| `MessageDocument { content, embeds[], rows[], assets }` | V2 is an additional `layout: 'legacy' \| 'v2'` discriminator plus a `v2Blocks[]` array; `content`/`embeds` become empty in that mode. The document is already a union-friendly container. |
| `toDiscordPayload(model)` | One function with two branch implementations; everything above it (store, validation entry point, persistence, preview mount) is unchanged. |
| Component model (`ComponentRow` → `ButtonComponent`/`SelectComponent`) | V2 reuses exactly the same interactive components (button/select are type 2/3 in both); only their *container* changes (Action Row vs Container/Section). |
| Action system (`Button → Action → ActionConfig`) | Completely orthogonal to layout: `custom_id`-keyed resolution and the executor do not care whether the component sits in an action row or a container. |
| Preview renderer skeleton/patcher | Same keyed-block approach; a container is one more block kind. |
| Validation | Same rule table plus a few V2-specific rules (40-component cap, no embeds alongside V2). |
| Persistence | The same `data_json` document with a `schemaVersion` bump; revisions already handle the change. |

The only decisions to make later: whether V2 is a *third builder mode* (recommended) and whether the
existing embed builder offers a one-way "convert this embed to a V2 container" action with a conversion
report (the pattern mature tools use). Both are additive.

---

## 12. Button architecture

Model: **Button → Action(s) → Action configuration**, with the action list being the extension point.

```
ButtonComponent {
  id, label, emoji, style ∈ {primary, secondary, success, danger, link},
  disabled,
  behaviour: { mode: 'interactive', customId } | { mode: 'link', url },
  action?: ActionConfig          // required for interactive, forbidden for link
}
```

Design rules that keep it honest and small:

1. **`action` is a single object, not an array, in pass 1.** Discord gives one interaction per component;
   "multiple actions" is really *one action with multiple targets* — which is exactly the
   `roleIds: string[]` in the requirement. If a genuine need for chained actions appears later
   (e.g. "add role + send a DM"), the field can become `actions: ActionConfig[]` without touching the UI
   structure (the action editor is already a list of action blocks). Documented as the known extension
   point rather than built now.
2. **Only the actions the current requirements need, plus one slot:**
   `role.add`, `role.remove`, `role.toggle` (the three real modes: pick, add-only, remove-only —
   matching what mature role bots expose as Default/Toggle/Add-only), `url.open` (link buttons),
   and `custom { handlerKey, params }` as an explicit, unused-by-the-UI slot so a future bot-side handler
   can be attached without a schema change. No 20 planned actions.
3. **Role actions carry `roleIds: string[]` plus optional `roleSetIds: string[]`** (reusable named groups).
   The config also carries the policies that already exist in `cogs/reactionroles.py` — `exclusiveGroup`,
   `maxRoles`, `requireConfirmation`, `boosterOnly`, `requiredRoleId`, `expiresAfterDays` — because they
   are already understood by users and already implemented in the bot.
4. **The UI is target-first, not API-first**: choose what the button *does* in plain words
   ("Give roles" / "Remove roles" / "Switch roles on and off" / "Open a link"), then choose roles as
   chips. `custom_id` is generated (`c:<configId>:<componentId>`) and never shown unless the user opens
   "Advanced", so nobody has to learn raw identifiers.
5. **Validation is action-aware**: a role action with zero roles is an error; a link button with no URL is
   an error; an interactive button with an empty label is an error; a role above the bot's top role is a
   warning with the exact fix ("move Nero's role above @X in Server Settings → Roles"), mirroring
   `utils/permissions.check_bot_role_position`'s wording.

---

## 13. Select menu UX

Recommended flow (no giant form): **create → general block → option list**, with the general block
collapsed to a one-line summary once options exist.

```
┌ Select menu ──────────────────────────────────────────────┐
│ Type        [ Roles ▾ ]   (String / Users / Roles / …)     │
│ Placeholder [ Choose your roles… ]            138/150      │
│ Selection   ( ) One  (•) Several     Min [0]  Max [3]      │
│ State       [ ] Disabled                                   │
│ Identifier  Advanced ▸ (auto-generated: sel_roles)         │
├ Options (4 / 25) ────────────────────────────  ⠿ drag ⋮ ⋮ ┤
│ ⠿ 1  🎮  Gaming      value: gaming      [Action ▸ Toggle]  │
│ ⠿ 2  📰  News        value: news        [Action ▸ Add]     │
└────────────────────────────────────────  [ + Add Option ]  ┘
```

- Each option row is **collapsed to a single line** (emoji, label, value, action summary, reorder handle,
  overflow menu) and expands in place into: Label, Value, Description, Emoji, Action configuration
  (roles as chips). Editing one option never re-renders the others (keyed patch).
- **Non-string select types** (Users/Roles/Mentionables/Channels) have no options list at all — Discord
  supplies the choices. Their inspector shows the general block + a single action, and the options
  section is replaced with an explanatory line. This is a real footgun in other builders and is worth
  handling explicitly.
- **Constraints enforced in the UI, not discovered at send time**: 25 options max, 100-char label/value/
  description, unique values, `min ≤ max`, `max ≥ 1` unless min is 0, one select per row (adding a second
  is refused with the reason and an offer to create a new row).
- **Reorder: yes, but not drag-only.** Recommendation: a `⠿` handle with drag reorder *plus*
  keyboard-accessible "move up/down" in the overflow menu (and `Alt+↑/↓`). Native HTML5 drag-and-drop on
  a list of form rows is ~60 lines and needs no library, but pointer-based drag with a keyboard fallback
  is the only version that is accessible — and the keyboard path doubles as the test surface. If the
  drag part proves fiddly, the move-up/down buttons ship alone (that is the fallback, not a blocker).
- Adding an option never grows the page: it inserts one row and focuses the new Label input.

---

## 14. Role selector UX (reusable control)

Requirements and how each is met:

| Requirement | Design |
|---|---|
| Search | Fuzzy-ish substring filter over name **and** id (typing an id works), case-insensitive, diacritic-tolerant; results ranked: exact prefix > substring > id match |
| Multiple selection | Chips below/inside the control; each chip = colour dot (role colour, neutral for `@everyone`) + name + `×` |
| Remove | Chip `×` plus `Backspace` on an empty query, plus "Clear all" |
| Role colour | Small dot; also used in preview |
| Large role lists | **Yes, virtualised by paging, not by a virtual-scroller**: render the first 50 matches, *n* more on scroll/`Show more`. Discord caps a guild at 250 static roles since 2024 anyway, so the realistic max is a few hundred rows — 50-at-a-time keeps the DOM tiny without hand-writing a virtual scroller (which would be the "overengineering" the brief warns about) |
| Loading state | Skeleton rows + "Loading roles…"; the control is usable (chips already chosen stay) if the fetch fails |
| Permission errors | The roles API returns `{results: [], error}` on failure — the control shows "Couldn't load roles (permission/Discord error) — [Retry]" and keeps manual id entry as a fallback |
| Missing/deleted roles | A role id that resolves to nothing renders as a **ghost chip**: dashed border, "Unknown role (id)" + "missing" badge, removable but not addable. This matters because saved configs outlive roles |
| Not-assignable roles | Roles Discord will never let the bot assign (`@everyone`, `managed` roles from other integrations, roles at/above the bot's top role) are shown **disabled with the reason** instead of being hidden — hiding them creates "why can't I pick my Admin role?" support issues |
| Reuse | One module (`embed/components/role-picker.js`) used by: role actions, select-option actions, policy fields (required role, exclusive group), reaction-role presets. Server data comes from the existing `/api/guild/roles` (already returns `position`, `managed`, `color`) — extended with the bot's top-role position via the identity payload, so no new endpoint is needed |

Data-flow note: the picker reads the role list from a **shared, cached client store** (fetched once per
page init, invalidated on demand) so five pickers on one screen cost one request, and the server-side
60 s TTL cache (§7.2) means concurrent users do not each hammer Discord.

---

## 15. Emoji UX (practical, reusing what exists)

**Reuse, don't rebuild.** The current emoji picker is already good: unicode categories (282 emoji),
frequently-used in `localStorage`, guild emoji, lazily-loaded other-server emoji, an app-emoji import
path, hover preview with name, and search. The work is to (a) make it a reusable module, (b) fix its
performance (A6), and (c) generalise it to component fields.

Plan:

| Aspect | Decision |
|---|---|
| Sources | Unicode (client table), current-guild custom, other-bot-guild custom (`/api/guild/emojis/external`, lazy), application emoji (`/api/app-emojis`, + import from a pasted id/token) |
| Search | One input; filter across all loaded sources; unicode search by **name keywords** (a small built-in keyword map for the top ~300 emoji; no need for a full emoji dictionary) |
| Recents | Keep `localStorage` (already implemented), but key it per guild-user and cap at 20, and only write on use (not on open) |
| Performance | Persist the grid once, patch on filter (same patcher as the preview); render at most ~120 cells with "show more"; **one** delegated listener on the container (mouseover/focus/click) instead of ~850 listeners |
| Invalid/deleted custom emoji | When a saved config references an emoji id that no longer exists (not in guild/app lists and the CDN 404s), show a **ghost chip** in the field ("emoji unavailable") and a validation warning at save time: "this emoji no longer exists — replace or remove it". The payload keeps the literal `<:name:id>` token, matching what Discord would show |
| Preview | Hover shows the emoji + `:name:`; the field shows the emoji inline; component previews show it on the button/option exactly as Discord does |
| Removing | Click the chip's `×`. Clearing sets `undefined` (payload omits `emoji` entirely — Discord rejects `{name: null, id: null}` in some positions) |
| What we do **not** build | Our own emoji image set, animated WebP conversion, keyword dictionaries for every emoji, or a Discord-style "emoji picker window" with skins. Twemoji-style artwork is unnecessary: unicode emoji render natively in the browser, and custom emoji come from Discord's CDN |

---

## 16. Discord limits as data

Single authority: **`utils/discord_limits.py`** (Python), mirrored to the client at runtime via
`GET /api/embed-builder/limits`. Nothing else defines a number.

```python
DISCORD_LIMITS = {
  "message": {
    "embeds": 10, "content": 2000, "attachments": 10,
    "combined_embed_chars": 6000, "action_rows": 5,
    "components_v2_components": 40,          # documented, unused in pass 1
  },
  "embed": {
    "title": 256, "description": 4096, "fields": 25,
    "field_name": 256, "field_value": 1024,
    "footer_text": 2048, "author_name": 256,
    "url": 2048, "media_url": 2048,
  },
  "button": {"label": 80, "custom_id": 100, "url": 512, "per_row": 5,
             "guidance_label_with_emoji": 34, "guidance_label_plain": 38},
  "select": {"options": 25, "label": 100, "value": 100, "description": 100,
             "placeholder": 150, "min_values": 25, "max_values": 25, "per_row": 1},
  "emoji":  {"unicode_name": 32, "app_emoji_bytes": 262144, "app_emoji_count": 2000},
  "upload": {"embed_media_ext": ["jpg","jpeg","png","webp","gif"],
             "per_file_bytes": 20971520,   # free tier default; overridable per guild tier
             "total_bytes": 26214400},
  "flags":  {"is_components_v2": 1 << 15},
}
```

Consumption rules:

- **Python validator** (`utils/embed_schema.py`) and the **JS validator** (`embed/validate.js`) both read
  this table; the JS one gets it from the endpoint (cached in the page, with a bundled fallback copy so
  the builder still works if that request fails).
- **UI affordances are generated from the table**: `maxlength` on inputs, counters, "+ Add Field" being
  disabled at 25, "5 of 5 rows used". No literal number appears in a template or a component.
- **Friendly messages live beside the numbers** (`MESSAGES[code]`) so wording is reviewable in one place
  and both layers say the same sentence.
- **A test asserts they match**: a Python harness compares `GET /api/embed-builder/limits` with the
  in-browser fallback constant, so the two copies cannot silently diverge.
- **Runtime-verified numbers are marked as such**: the per-file upload cap depends on the guild's boost
  tier and on Discord's current policy (it changed from 25 MB → 10 MB → 20 MB in recent memory), so the
  value is a *configured default*, the server treats Discord's rejection message as authoritative, and
  the UI says "your server's limit may be higher" rather than pretending to know.

---

## 17. Validation strategy (four layers, one rule set)

| Layer | Where | Runs when | Job | Never does |
|---|---|---|---|---|
| **1. Field/UI** | input component | on input/change (debounced ~150 ms for text, immediate for structural) | `maxlength`, counters, numeric ranges, required-ness, immediate "this will be rejected" | decide final validity; never blocks typing |
| **2. Model** | `validate(model, limits)` in `embed/validate.js` (client) and `utils/embed_schema.py` (server) | on every dispatched change (client, memoised) and on save/publish (server) | Whole-document rules: counts, combined budget, identifier uniqueness, composition rules, action completeness, asset integrity | touch the network |
| **3. Structural/semantic** | part of layer 2, separate pass | same | Cross-object: duplicate `custom_id`, duplicate embed URL, select+button in one row, role sets referencing deleted roles, `attachment://` with no matching asset, role hierarchy feasibility | guess at Discord's private rules |
| **4. Discord/API** | the API call itself | on send/update | authoritative acceptance; returns structured field errors | be the *only* check (too slow, too late, too vague for a user) |

Interaction contract:

- Layers 1–3 run **before** any write to the server and produce the same `Issue[]` shape
  (`severity`, `code`, `path`, `nodeId`, `message`, `limit`). The inspector renders issues whose `path`
  matches the node it is showing; the rail badges nodes that have issues; the strip summarises.
- **Save is blocked by errors only.** Warnings are shown, never silently swallowed: a saved config with
  warnings gets a small "⚠ 2 warnings" badge in the library.
- **Publish is blocked by errors**; warnings require an explicit confirmation listing them ("This button
  removes a role that's above my role — members will see a failure. Send anyway?").
- Layer 4's response is parsed: Discord returns `errors` keyed by field path (e.g.
  `embeds.0.fields.1.value`), which we map back to a node id and show against the right input, in
  Discord's own words, above our own message. This is the "never trust the frontend" backstop and also
  the mechanism that surfaces *unknown* future limits we did not model.
- The rule table is **idempotent and pure**, so the same function is used by tests, by the client, by the
  save endpoint, and by the publish endpoint — four call sites, one implementation, no drift.

---

## 18. Draft recovery

Behaviour matrix (each row is a required scenario):

| Situation | Behaviour |
|---|---|
| Accidental refresh / navigate away / close tab | Draft restored on return: content, embeds, components, assets, selection, and the last saved-id/revision. A subtle banner: "Restored draft from 12:04 · [Discard]" |
| Intentional "New message" while a draft exists | Confirm dialog listing what will be lost ("1 embed, 2 buttons, 1 local image added"), with **Save as…** available inline. Never a silent wipe |
| Draft vs saved configuration | The status chip distinguishes `Draft` (never saved), `Unsaved changes` (a saved config with edits), `Saved`. Navigating away with unsaved changes prompts once |
| IndexedDB unavailable (private mode, disabled) | The builder works fully in memory. One muted line: "Drafts won't be kept if you close this tab" — no blocking, no error styling, no retry loop |
| IndexedDB blocked/hung (the A2 bug) | `openIdb()` gains `onblocked` + a 2 s timeout + `onupgradeneeded` handling → resolves to "unavailable", the builder proceeds, and the user sees the muted line above. **An `await` on storage can never sit between page load and first paint again** |
| Quota exceeded while writing | Caught per write; the draft writer drops attachments first (they are the big part), keeps the text document, and tells the user which part is no longer being saved |
| Multiple tabs | A `BroadcastChannel` (with a `storage`-event fallback) warns: "Another tab has this draft open — last write wins." Cheap, prevents the classic silent clobber |
| Draft belongs to a different guild/server | Key drafts by `guildId`; switching servers never mixes drafts. The restore banner names the guild |
| After a successful publish | The draft is cleared (with the sent message id recorded), so reopening the builder offers a clean slate |

Two implementation rules that preserve the "IndexedDB is never a single point of failure" requirement:
the draft write is **scheduled** (idle ≥1.5 s, plus `pagehide`) instead of run inside the keystroke path,
and the restore is **not awaited** by init — it is applied as a patch after first paint (A2's second
half).

---

## 19. Mobile / responsive

Desktop-first, but nothing may become unusable:

| Breakpoint | Layout |
|---|---|
| ≥ 1280 px | 3 panes: rail (220 px) · inspector (fluid) · preview (sticky, ~460 px). This is the primary experience |
| 1024–1279 px | rail collapses to a compact node list (icons + counts, expandable); inspector + preview share the width |
| 768–1023 px (tablet portrait) | Segmented control: **Edit ⇄ Preview** (one pane at a time, state preserved); rail becomes a "Jump to…" select at the top of the inspector |
| < 768 px (phone) | Same segmented control; inspector fields stack to one column; the validation strip docks to the bottom (tap to expand); the preview renders at a reduced scale with Discord's own mobile proportions (Discord's own mobile embed layout differs — the preview shows a "mobile layout is approximate" note once) |
| Modals | Center dialogs ≥ 768 px; full-height bottom sheets below that, with a visible close affordance |
| Component/option lists | Rows become stacked cards; the reorder handle stays (touch drag) and the move-up/down menu items remain the accessible fallback |
| Touch targets | ≥ 40 px for chips, delete buttons and row handles; hover-only affordances (emoji hover bar, image test button) always have a click/tap equivalent |

Explicitly **not** doing: a mobile-specific builder, a separate mobile preview fidelity mode, or gesture
shortcuts. The goal is "usable in a pinch on a tablet", not a parallel mobile product.

---

## 20. Visual design

Research basis (patterns, not looks): Discord's own client chrome (message cards, embed stripe, action
rows, button colour roles), Discohook/Carl-bot/discord.builders (live preview beside the editor, plain
labels, sectioned panels), modern SaaS builders (contextual inspector + object tree, e.g. Webflow/Figma
panels), and the existing dashboard's own language (`main.css` tokens, `--accent: #7c5cbf`, card/table
patterns) which the builder must match rather than replace.

Rules:

1. **One page, one purpose, one primary action.** Header: `Embed Builder` · status chip · `[Preview]`
   (mobile) · `[Save ▾]` · `[Send]`. Everything else is secondary.
2. **Hierarchy by structure, not by colour.** Three panes; inside the inspector, collapsible groups
   (`Content`, `Author`, `Fields`, `Images`, `Footer`, `Timestamps`) whose headers summarise state
   ("Fields · 3", "Images · 1 set"). A group with problems carries a badge, never a red border.
3. **Density:** 1.45–1.6 line-height, 8 px spacing scale, section headers in the existing muted-uppercase
   style; inputs are consistent height and never full-width inside a grid cell (they currently are).
4. **Empty states** are informative and actionable: "No fields yet — fields are Discord's two-column
   blocks. [Add the first field]" ; "No image — the embed shows text only. [Add an image]".
5. **Loading states**: skeletons for the rail/inspector (structural), inline spinners for images, a
   top-right subtle progress line for save/publish. Never a blank pane.
6. **Error states**: field-level red text + the strip's count; a persistent, dismissible banner for
   publish failures with Discord's own words.
7. **No unnecessary modals**: role selection, emoji, image upload, actions, and option editing are inline
   (popovers/expanding rows). Modals are reserved for destructive/irreversible confirmations and the
   save dialog.
8. **The preview stays legible while editing**: it is sticky on desktop, never scrolls to a different
   position because of a re-render (a benefit of patching), dims slightly (opacity .96) while a HMR-like
   state is pending, and highlights the element corresponding to the focused field.
9. **Motion**: 120–160 ms transitions only for state change (row expand, chip add/remove); everything
   respects `prefers-reduced-motion` (already handled in `embed-composer.css` — keep that).
10. **Dark and light themes**: use the existing CSS variables; Discord preview chrome keeps fixed Discord
    colours in both themes (that is what Discord looks like), separated by a labelled "preview" frame so
    it never reads as dashboard UI.

---

## 21. Don't overengineer — the explicit budget

| Abstraction | Justification | Not doing |
|---|---|---|
| `model.js` (normalization + payload) | Required to stop builder/preview/payload drift (B5) | No schema library, no validation framework, no class hierarchy |
| `store.js` (~150 lines: `dispatch`, `subscribe`, structural patches, history) | Required to remove mutable shared state (B1) and enable targeted updates | No middlewares, no devtools, no immutability library, no selector memoisation framework |
| `validate.js` + limits table | Required by four call sites (UI, model, save, publish) | No generic rule engine; a list of small named checks |
| `preview.js` (skeleton + keyed patch) | Required for image identity and correctness (B2) | No virtual DOM, no JSX, no template engine |
| Component modules (`image-field`, `role-picker`, `emoji-picker`, `repeater`) | Each has ≥2 real consumers today | No component framework, no plugin registry, no slots/portals |
| Action system | Required by the brief; extensible by data, not by inheritance | No action plugins, no scripting, no pipeline/before-after hooks |
| REST endpoints | Thin, per-domain, following `api/tickets.py` | No generic CRUD generator, no GraphQL, no response-wrapper framework |

Also explicitly **not** doing: a build step, a package manager dependency for the dashboard runtime, a
second state container, event buses, or deep inheritance. Every new file must answer "who else consumes
this today?" — if the answer is "nobody yet", it does not get built now.

---

## 22. Migration strategy (phases, with files/rollback/risk)

Adjusted from your list in three ways: (i) Phase 0 absorbs the *page-lifecycle* fix because it is a bug
that also unblocks every later phase; (ii) Components V2 is explicitly out; (iii) each phase ends with the
tests that lock it, and no phase starts before the previous one is green.

### Phase 0 — Bugs only (no redesign, no new UI)

- **Files changed:** `dashboard/templates/base.html` (lifecycle hook, inert for other pages),
  **new** `dashboard/static/js/nav-lifecycle.js`, `dashboard/templates/manage/embedbuilder.html` (init
  converted to a module + `destroy()`, IDB guards, identity from server, per-file upload check, emoji-grid
  delegation + debounce, attachment double-render removed), `dashboard/api/embedbuilder.py` (limits
  endpoint, per-file check), `dashboard/api/botprofile.py` (non-blocking identity read reused by the
  server-side inject), `dashboard/app.py` (identity context value), `utils/bot_profile.py` (additive cached
  read; blocking calls moved off the request path).
- **Files untouched:** `embed-composer.js`, `database.py`, `cogs/*`, all other templates and APIs.
- **Migration:** none.
- **Rollback:** revert the commit; the hook is inert without a registered module.
- **Regression risks:** a bug in the lifecycle registry could affect other pages → mitigated by making the
  registry a no-op unless a page declares a module, and by a harness that asserts no-op behaviour for a
  plain page.
- **Tests:** nav/back/refresh harness; IDB-blocked harness; emoji-grid listener count; upload-size check;
  identity injection (no fetch on load).

### Phase 1 — Preview + state architecture

- **Files:** **new** `static/js/embed/{store,model,validate,preview,discord-markdown}.js`;
  `embed-composer.js` reduced to a compatibility facade over them (five public exports preserved);
  `manage/embedbuilder.html` (editor markup split into small templates/components);
  `api/embed_builder.py` (limits + validate endpoints).
- **Untouched:** `database.py`, `cogs/*`, reaction-roles page, minigames builder HTML.
- **Migration:** none (payload output for existing fields must be byte-identical — asserted).
- **Rollback:** `?legacy=1` renders the old page (kept for one release); the facade keeps the minigames
  builder on the old code path until its own harness passes.
- **Risks:** the shared module's consumers; markdown behaviour changes visible in the minigame preview.
- **Tests:** model round-trip, payload equality vs. the current output, markdown golden corpus, preview
  patch identity (image nodes reused), rapid-typing performance budget.

### Phase 2 — Images/attachments

- **Files:** new `embed/components/image-field.js`, `embed/assets.js`; `api/embed_builder.py`
  (publish-with-attachments, CDN capture); `utils/embed_schema.py` (asset integrity + filename rules).
- **Untouched:** existing `/api/embedbuilder/send` (still works; the new path is additive), bot profile,
  uploads elsewhere.
- **Migration:** when an old saved template is loaded, image fields become `{kind:'url'}` — no change.
- **Rollback:** the new publish path is a feature flag; URL-mode-only remains fully functional.
- **Risks:** filename collisions, per-file limits, confusing "local image missing" states.
- **Tests:** multipart mapping (`files[n]` ↔ `attachments[n]` ↔ `attachment://`), dedupe by hash,
  unsupported type, oversized file, missing local asset, capture-after-publish.

### Phase 3 — Save/load migration

- **Files:** new `api/embed_builder.py` routes + `utils/embed_store.py`; `database.py` (additive tables:
  `saved_embeds`, `saved_embed_revisions`, `asset_library`); `manage/embedbuilder.html` library UI.
- **Untouched:** `embed_templates` (read-only via an importer), `cogs/embedbuilder.py`, legacy `app.py`
  routes.
- **Migration:** lazy import of legacy rows into the new shape via `legacy_to_v2()`, exercised by a
  harness for: bare embed dict, `{content, embeds}`, corrupt JSON, missing keys, unknown keys, oversized
  values, and a conversion report shown to the user for anything unmappable.
- **Rollback:** the new store is additive and its UI is flag-gated; legacy templates keep working either
  way.
- **Risks:** import mis-mapping; the cog's `_doc_to_content_and_embeds` contract changing (it must not).
- **Tests:** save → load → edit → save; revision append; copy semantics; legacy corpus; corrupt data.

### Phase 4 — Components builder (buttons/selects, no actions yet)

- **Files:** new `static/js/embed/components-builder.js` + component modules; `embed/model.js` extended
  with rows/components; validation rules; new page route + nav entry.
- **Untouched:** reaction-roles page/cog (still the only live interaction path), saved-message store.
- **Migration:** none (new object kinds; older documents normalize with `rows: []`).
- **Rollback:** the new page is a separate route; nothing else links to it until Phase 6.
- **Risks:** limits enforcement bugs producing Discord 400s on first real send.
- **Tests:** row/button/select composition, custom_id uniqueness, option limits, emoji token round-trip,
  preview of components, keyboard reorder.

### Phase 5 — Actions + runtime (bot side)

- **Files:** new `cogs/components.py`; `embed/components/action-editor.js`; role sets
  (`role_sets` table); `utils/embed_store.py` for the component→action resolution; publish/update paths.
- **Untouched:** `cogs/reactionroles.py` and its tables (kept loaded; the new cog writes its own rows).
- **Migration:** a "migrate this panel" action creates a Saved Message from `reaction_roles` rows without
  touching the live message; the old panel keeps working until republished.
- **Rollback:** unload the new cog (config flag in `main.py`); old path unaffected.
- **Risks:** two systems granting the same role; expiry semantics; hierarchy failures at click time.
- **Tests:** executor unit tests (multi-role add/remove/toggle, exclusive/max/booster/required/expiry),
  persistent-view restoration, "never double-grant" and "never double-sweep" integration tests.

### Phase 6 — Saved Messages + send/update workflow

- **Files:** `saved_messages`/`message_components`/`message_actions`/`message_publications` tables;
  library UI; publish/update UI in the components builder.
- **Untouched:** legacy template routes until the builder page stops offering them (one release later).
- **Migration:** none beyond Phase 3's importer, extended to wrap an embed in a message.
- **Rollback:** feature-flagged publish/update entries; the raw `/embedbuilder/send` path remains.
- **Risks:** publishing to the wrong channel / editing the wrong message — mitigated by recording
  publications and requiring an explicit "Update live message" confirmation that names the channel and
  the message.
- **Tests:** publish payload mapping, publication records, update-live edit, failure mapping, permissions.

**Sequencing note:** the order differs from your list only in that the lifecycle fix (in your "Phase 0"
spirit) also *enables* the rest: without it, every later phase inherits the "dead page" bug and the
idempotency requirement grows more expensive to retrofit.

---

## 23. Backward compatibility

| Legacy shape | Handling |
|---|---|
| `embed_templates.data` = bare embed dict (written by `dashboard/app.py`) | `legacy_to_v2()` wraps it as `{content:'', embeds:[embed]}`; unknown keys preserved under `extra` (not sent to Discord, shown in the conversion report) |
| `embed_templates.data` = `{content, embeds}` (written by `api/embedbuilder.py`) | Mapped directly; `footer`/`author` string-or-object forms both accepted (`footer_icon`/`author_icon` from the cog's expectations are read if present and surfaced as real icon fields) |
| Missing `footer.icon_url`, `author.icon_url`, `author.url`, `url`, `timestamp` | Default to *absent*, never `""`. `toDiscordPayload` omits empty keys, so no `icon_url: null` is ever sent (Discord rejects some null fields) |
| `color` as `"#7c5cbf"`, `"7c5cbf"`, `"blurple"`, or an integer | All four normalized to an integer `0xRRGGBB`; an unparseable value falls back to the default and is reported as a conversion note rather than silently dropped |
| Fields with `name: ""` / `value: ""` | Kept in the editor (so the user can fix them) and filtered from the payload — the current behaviour, preserved |
| Fields with placeholders (`name || '\u200b'`) | Kept as-is on import (do not "clean" user data) |
| Corrupt JSON / non-dict `data` | Treated as "empty template", reported in the library as "couldn't be read", never crashes the list |
| `reaction_roles` single-role buttons | Import maps `role_id` → `roleIds: [role_id]` with the same policies; **one role in, one role out** — a migrated panel behaves identically |
| `rr_panels.buttons` JSON (dashboard-only, unused by the bot) | Ignored, with a note |
| Live messages sent by the *old* builder (content-only, no components) | Untouched. The new publish flow never edits them unless the user explicitly chooses a message |
| New `schemaVersion` | Every read path runs `normalize(document)` which upgrades v1 → v2 in memory; saving writes v2 only, so old rows are upgraded on next save, not in place (no destructive migration) |

Hard rule: **no legacy row is ever rewritten by the upgrade path.** Upgrades happen in memory on read and
in a *new* row on save. That makes the whole migration reversible by reverting the code.

---

## 24. Testing plan

Extends the existing convention: `scripts/test_*.js` harnesses run by `scripts/run_js_tests.sh` via
`npm test`, plus Python harnesses. The CI workflow already exists as `ci-tests-workflow.yml.example`
(needs to be `git mv`'d to `.github/workflows/` since a GitHub App cannot create it) — landing it is part
of Phase 0.

### Navigation
- `test_nav_lifecycle.js`: direct load; htmx swap to the page; swap away; swap back; two consecutive
  swaps; `historyRestore`; failed request (no half-init); init called twice on the same root is a no-op;
  a page with no module is untouched; listener/timer registries are empty after `destroy()`.
- Manual smoke (documented checklist): hard refresh, back/forward, and sidebar round-trips with a local
  image attached, watching for orphaned object URLs.

### Builder
- `test_embed_builder_fields.js`: every field individually (title, url, description, colour trio, author
  name/url/icon, footer text/icon, thumbnail, image, timestamp, 25 fields, inline toggles, reorder,
  delete), including empty values, whitespace-only, unicode/RTL/Arabic (the project has Arabic fonts and
  users), emoji-only content, and max-length boundaries at `max-1`, `max`, `max+1`.
- `test_discord_markdown.js`: golden corpus (~80 cases) covering every feature in §6.2, code-shielding
  (`**` inside backticks must stay literal), escapes, nested combinations, spoilers around code, mentions
  inside code blocks, ZWJ/keycap emoji, `<_em>_`-style pathologies, and the plain-text surfaces (title/
  author/footer must show literal asterisks).

### Images
- `test_image_field.js`: URL valid/invalid/broken/redirect; upload by extension and by sniffed bytes;
  wrong extension with right bytes and vice versa; oversized; unsupported type with a helpful message;
  duplicate hash dedupe (thumbnail + footer icon = 1 asset); replace; remove; missing local asset on a
  saved config; paste and drop paths; state machine transitions.
- `test_embed_builder_upload_payload.py`: multipart mapping, filename sanitisation/uniqueness,
  `attachment://` references matching `files[n]`, `attachments[n]` ids, CDN capture parsing, and the error
  paths (413, 400 field error, network failure).

### Save/load
- `test_embed_store.py`: create/save/list/load/delete, revision append, restore-as-new, copy-on-load
  (asserting the sibling is untouched), publication records, cross-guild isolation.
- `test_legacy_import.py`: the full legacy corpus in §23 (bare dict, `{content,embeds}`, corrupt,
  partial, unknown keys, placeholder fields, string/object footer+author, four colour forms) asserting a
  valid v2 document plus a conversion report.

### Components
- `test_components_limits.js`: 6th button refused, second select in a row refused, select with buttons
  refused, 26th option refused, duplicate option values, duplicate `custom_id`, empty label, link button
  with/without `custom_id` and `url`, min > max, non-string select type hiding options.
- `test_actions_model.js`: role action with 0/1/many roles, role sets expansion, policy round-trip,
  the custom-action slot, and normalization of legacy single-role data.
- `test_components_executor.py` (Phase 5): multi-role add/remove/toggle, exclusive group, max roles,
  booster-only, required role, expiry write/read/sweep, hierarchy failure reporting, "never double-grant"
  and "never double-sweep" invariants, persistent view rebuild after restart.

### Performance
- `test_preview_perf_budget.js` (assertion-based, not a benchmark): rendering an unchanged payload a
  second time must touch **zero** new image elements and produce **zero** new listener bindings; typing
  N characters must perform exactly N markup parses of the changed field (memoisation proof); the editor
  must create ≤ 1 card per structural change (targeted-update proof); the emoji grid must bind a bounded
  number of listeners regardless of cell count (event delegation proof).
- Node-level timing probes (the two used in §3) are kept as a script for manual comparison, with the
  measured baselines recorded in the harness header so regressions are visible.
- Manual checklist for the browser-side truths Node cannot assert: decode/flicker behaviour while typing
  into a large embed, memory after navigating away with a 20 MB attachment, and 10 embeds × 25 fields
  editing responsiveness.

---

## 25. Final decision report

### 1. Definitely broken (fix first, no design change)
A1 page never initialises after htmx navigation · A2 IndexedDB can hang first paint · A3 init not
idempotent/no teardown (must be fixed with A1) · A4 image/metadata fields silently dropped on
save/load · A5 attachment preview triggers double renders · A6 emoji grid rebuild per keystroke ·
A7 draft writes clone Blobs into IDB on a typing debounce · A8 no per-file upload check, stale total cap ·
A9 uploads can never fill an embed image slot (`attachment://` never produced) · A10 broken images show
as empty boxes · A11 identity fetch duplicated in two pages, blank avatar flash · A12 three blocking
Discord round-trips on init (identity, roles, channels) with no cache · A13 two template APIs writing
two shapes into one table.

### 2. What is causing the slowness (measured, in order)
1. **Editor rebuilds**: up to 230 KB / 1 870 elements / 830 inputs per structural change (0.72 ms of JS,
   tens–hundreds of ms of browser work). *This is the biggest one, and it is not `innerHTML` in the preview.*
2. **Blocking Discord calls** on init (three synchronous `requests` with 8 s timeouts, no cache).
3. **Storage work inside the keystroke path** (500 ms debounced Blob clone into IndexedDB).
4. **Image churn in the preview** (75 elements replaced per keystroke; every `<img>` re-created) plus the
   double render on each attachment resolution.
5. **Emoji grid**: ~282 cells × 3 listeners rebuilt per search keystroke.
6. Preview JS itself (0.28 ms) — *not* a bottleneck; it only looks like one because it runs on every key.

### 3. What should NOT be changed
`utils/emoji.py` semantics · `utils/app_emoji_cache.py` · `utils/bot_profile.py`'s existing functions ·
the response shape of `/api/guild/roles|channels|emojis(|/external)|resolve-user` · `NeroSelect` /
`NeroAlias` contracts · `cogs/embedbuilder.py` commands · the `embed_templates` table shape and its
`_doc_to_content_and_embeds` contract · `cogs/reactionroles.py` and its tables · the existing
`/embedbuilder/send` route (until Phase 6 flags it out) · the CSP policy (already correct) · the
`embed-composer.js` public API (5 exports) · `database.py`'s existing schema (additions only).

### 4. What should be refactored
The embed-builder page's inline JS (≈1 000 lines) → page module + small components · `renderPreview`'s
`innerHTML` → keyed patch · `mountEditor`'s rebuild-everything → rail + single-card inspector ·
mutable `state.embeds` → store with structural patches · `cleanEmbedForPayload`/`embedFromApi` → one
normalized model + `toDiscordPayload` · template-by-name storage → Saved Embed/Message with revisions ·
attachment preview ownership → an asset registry · identity loading → server-injected context value.

### 5. Recommended architecture
Store + normalized model + one payload builder + one validator (shared rule table) feeding a keyed-patch
preview renderer, with a page-module registry for lifecycle, and a per-domain REST layer
(`api/embed_builder.py`) over additive tables. No framework, no build step. (§3, §5, §17)

### 6. Recommended UX
Three-pane workspace (structure rail · contextual inspector · sticky preview), inline everything
(images, roles, emoji, actions, options), one primary action per screen, silent-but-present validation
strip, library screens for Saved Embeds/Messages, mobile as a segmented Edit/Preview toggle. (§5, §13,
§14, §19, §20)

### 7. Recommended data model
`MessageDocument { schemaVersion, content, embeds[], rows[], assets }`, `EmbedDocument` with id-keyed
fields, `ComponentRow` → `ButtonComponent | SelectComponent`, `ActionConfig` union with role actions
holding `roleIds[]`, `SavedEmbed`/`SavedMessage` wrappers with `revision` + provenance, references to
assets by `assetId` with an optional captured CDN URL. (§6 of the previous document, §9, §10)

### 8. Recommended image strategy
Two modes per slot (`url` | `upload`); uploads validated locally (extension + sniffed bytes + size),
stored once by content hash, previewed via `blob:`/`data:`; sent as real attachments with
`attachment://filename` references in the same multipart request; CDN URLs captured after publish so
saved configs stay portable; references are owned by the document, bytes never leave the browser. No
data-URI or fake-local-URL tricks, ever. (§8)

### 9. Recommended save/version strategy
Copy-on-load as the default (safe, predictable), append-only revisions for history, publications pin the
revision that was sent, explicit "pull latest" for refreshing a copy, reference mode deferred with a
defined shape. (§10)

### 10. Recommended component/action architecture
`Button → Action → ActionConfig` (`role.add|remove|toggle` with `roleIds[]` + role sets + existing
policies, `url.open`, `custom` slot), execution via `custom_id = c:<configId>:<componentId>` and
persistent views generalized from `cogs/reactionroles.py`. (§12)

### 11. Migration strategy
Read-only legacy import with a conversion report; no destructive schema changes; additive tables; upgrades
in memory on read, new rows on write; feature flags + `?legacy=1` for one release per phase; old
reaction-role panels keep running until explicitly republished. (§22, §23)

### 12. Testing strategy
Nine harness families mapped to the existing `npm test` convention (navigation, fields, markdown, images,
save/load, legacy import, components, actions/executor, performance budgets), plus a short manual
checklist for the browser-only truths. (§24)

### 13. Risks (ranked)
1. Changing the shared `embed-composer.js` breaks the **minigames builder** (mitigation: freeze the five
   exports, keep both harnesses green, byte-compare payloads).
2. Lifecycle hook regressions on **other pages** (mitigation: inert registry, no-op unless declared,
   harness asserts no-op).
3. Legacy import mis-mapping (mitigation: corpus tests + conversion report + no destructive writes).
4. Two role systems running at once in Phase 5 (mitigation: separate rows, one executor, "never
   double-grant/sweep" tests, config flag).
5. Scope creep in the component builder (mitigation: §21's budget, per-file justification rule,
   V2 explicitly deferred).
6. Publish/update mistakes on live member-facing messages (mitigation: explicit confirmations naming
   channel + message, publications recorded, revisions pinned).

### 14. Decisions that still require your approval
The six below.

---

## Your six open questions — recommendation + alternatives

### 1. Components V2: in this pass?
**Recommendation: no, not in pass 1** — because enabling the flag disables `content` and `embeds`, so a
"visual embed + buttons" message is impossible in V2, and every current requirement is an embed + buttons.
**Alternatives:** (a) V2 as a separate third builder mode in a later pass (recommended future step, with a
one-way "convert embed → container" action and a conversion report); (b) V2-first and drop classic embeds
(rejected: regresses a shipped feature and doubles the surface); (c) V2 support hidden behind a per-guild
beta flag (acceptable later, unnecessary now). The architecture is compatible with all three (§11).

### 2. Local uploads in *saved* configs?
**Recommendation: yes, first-class, with CDN capture after publish** — local files for the current
browser, plus the Discord-hosted URL recorded from the send response so the config becomes portable
without us hosting anything. **Alternatives:** (a) uploads allowed only for one-off sends, URLs required
for saved configs (simplest, but blocks the main use case for anyone without an image host); (b) store
bytes server-side and serve them (needs a public URL, storage, cleanup, and a CSP/abuse story — real
scope, rejected); (c) re-upload on every send only (works, but a saved config is unusable from another
device). Recommendation (2) keeps the "no public bucket" property the codebase already has.

### 3. Publish semantics: does "Send" ever edit an existing message?
**Recommendation: no. "Send" always creates a new message; "Update live message" is the only path that
edits one, and it names the channel and message it will change.** Publications are recorded so this is
always explicit. **Alternatives:** (a) "Send" overwrites the last message from that config (dangerous —
silent edits to member-facing panels); (b) no update path at all (forces re-sending and breaks pinned
panels); (c) auto-update on save (rejected: saving would change live content).

### 4. Reaction-roles page: keep side by side or replace?
**Recommendation: keep it working, then replace it with a "Role menu" preset inside the components
builder once the new path is verified, and delete the old page in a later release.** The underlying
`reaction_roles` data and the running cog stay untouched until an explicit per-panel migration. **Alternatives:**
(a) replace immediately (risk to live member-facing panels — rejected); (b) keep both forever (two
divergent role systems — bad); (c) keep the page but have it write the new format (nice, but the visual
preset supersedes it).

### 5. Route strategy for the rewrite
**Recommendation: build the new workspace at `/embed-builder/v2` (new page module, old page untouched),
verify with you, then switch `/embed-builder` to it with `?legacy=1` available for one release.** This
matches the audit's own rollout guidance and keeps rollback to a one-line change.
**Alternatives:** (a) in-place rewrite behind a feature flag (faster to converge, but the old and new
templates share one URL and one `{% block content %}`, so a broken new render takes the page down with it);
(b) parallel forever (rejected: two truth sources for the same objects).

### 6. Permission levels
**Recommendation: keep the builder page at `LEVEL_OWNER`; make the *identity* problem disappear by
serving it with the page instead of via an `LEVEL_ADMIN` endpoint.** If you later want admins to build
but not publish, split the gate — page at `LEVEL_ADMIN`, publish/update-live at `LEVEL_OWNER`, with the
publish button visibly disabled for admins and an explanation. **Alternatives:** (a) lower the page to
`LEVEL_ADMIN` now (weakens a deliberately narrow gate without a product reason); (b) add a new public
"bot identity" endpoint at moderator level (works, but a second endpoint and a second round-trip for data
the page render already has); (c) include the bot's top-role position in the identity payload so role
warnings can be computed client-side (recommended detail, see §7.2).

---

## Appendix — how to reproduce the numbers

```bash
# 1. Render base.html to see the real DOM order (block scripts vs #content-area)
python3 -m venv /tmp/jenv && /tmp/jenv/bin/pip install jinja2
# (script in the session log: renders base.html with permissive Undefined, then grep line numbers)

# 2. Preview cost (needs no dependencies)
node /tmp/perfprobe.js      # 0.28 ms / 5 459 B / 75 elements for a 2-embed message

# 3. Editor rebuild cost
node /tmp/perfprobe2.js     # 3.6 KB → 230.5 KB / 1 870 elements / 830 inputs

# 4. Existing suite (must stay green in every phase)
npm test                    # scripts/run_js_tests.sh — 5 harnesses today
```
