# Nero Dashboard — architecture & perceived-heaviness audit

Written against `095c1b0` (= `update` = `main` after PR #19). Every number below
was measured in this repo, not estimated.

---

## 0. First: the attachment bug was never an upload bug

`dashboard/app.py` set

```
img-src 'self' data: https:
```

…while every local attachment preview is a `URL.createObjectURL()` **`blob:`** URL
(`embedbuilder.html` → `getAttachmentPreviewUrl`, `embed-composer.js` →
`resolveAttachmentPreviewUrl`). So: the `<img src>` is set, **no request is ever made,
nothing paints, no error is shown to the user.** The comment sitting directly above that
policy already described the symptom class ("no visible error beyond *the image just
doesn't show up*") — it just never caught the one case it applied to.

That is why an attachment looked "not uploaded" in both the 📎 grid and the preview,
while the `File` object was in memory and would have been sent fine.

Fixed in `5e91af2`, in two layers so neither can re-break the other:
1. CSP now allows `blob:` (the correct, zero-copy path).
2. `EmbedComposer.canRenderBlobUrls()` **probes** whether a `blob:` URL actually loads
   and `previewUrlFor()` degrades: cached → remote → `blob:` → `data:` URI (≤5 MB) →
   an explicit reason. A stricter CSP in the future now costs memory, not the feature.

If you want to confirm it yourself in 10 seconds:

```bash
curl -sI https://<your-dashboard>/manage/embedbuilder/<guild> | grep -i content-security
# expect: img-src 'self' data: blob: https:
```

Three other real causes found while in there, all fixed:
* **`File.type` is empty for a lot of actual images** (HEIC screenshots, images dragged
  out of chat apps). The grid trusted `type` only, so those became a 📄 badge forever →
  now sniffed by extension.
* **"not yet decoded" and "no preview" drew identical pixels.** Now: a named
  *pending* slot, or a tile that says *why* plus a retry — the file is always still sent.
* **Two renders before the probe settled minted two object URLs** (one leaked, one
  orphaned so revoke couldn't see it) → resolutions de-dupe per attachment.

---

## 1. Why it feels heavy (measured, ordered by cost)

You have **no gradients and no `backdrop-filter`** (0 occurrences in 51 KB of CSS), so this
is not a "too many effects" dashboard. The weight is load-time and density:

| # | Cause | Evidence | Cheap fix |
|---|---|---|---|
| 1 | 3 synchronous third-party scripts in `base.html` **`<head>`** | htmx 1.9.10 (unpkg) + jQuery 3.7.1 + select2 (jsDelivr), plain `<script src>`, no `defer` — and the inline `htmx.config.…` block right after them also blocks parsing | add `defer`, or load per page: **only 7 of 41 templates use htmx**, 12 use the pickers |
| 2 | Same CDN CSS + `fonts.googleapis.com` blocking render | `base.html` head: Google Fonts stylesheet, select2 CSS, `main.css`, `embed-composer.css` | `font-display: swap` + `rel=preload` for the 2 CSS you truly need above the fold; self-host Inter (kills 2 network hops on every paint) |
| 3 | Every page pays for every library | `base.html` 576 lines, 2 `<style>` blocks, ~30 KB HTML shell before any content | opt-in asset blocks (see §3 Phase 3) |
| 4 | 453 inline `style="…"` attributes across 41 templates | grep count | convert the top 20 offenders into classes; inline styles are why "small visual change" costs an edit per template |
| 5 | One 119 KB `dashboard/app.py` rendering pages, enforcing auth, minting CSRF, defining CSP, rate limiting, session policy | single module | split (§3 Phase 1) |
| 6 | `commands.html` = 1253 lines, `embedbuilder.html` = 1173, `minigame_builder.html` = 768 in one template each | wc | extract page JS to `static/js/pages/*.js` |
| 7 | `embed-composer.js` is 32 KB, imported by only **2** pages | grep | fine as-is; do **not** split further before a 3rd consumer |
| 8 | All 10 embed cards are in the DOM at once; the accordion toggle calls `render()` which rebuilds **every** card and re-wires every listener | `mountEditor.render()` | render the active card only, keep the others as titles |

**Perceived** heaviness (the "a lot of information" feeling) is mostly #3 + density, not
effects. Concrete, low-risk visual de-heavying:
* group the left nav into collapsible sections (currently ~40 links, one flat list);
* one page title + one primary action per screen — pages like `botprofile`/`creator`
  currently stack several equal-weight card headers;
* raise line-height in dense tables from `1.5` to `1.6`, and cut border noise:
  `--border: #2a2a2a` on `--bg2: #141414` is visible on every card of every list;
* keep the "APP badge"-style chrome *inside* previews only — the editor UI is not Discord.

---

## 2. Is "Clean Architecture" the right frame? Mostly not — here is what is

The pattern in your own repo already tells you the answer: `dashboard/api/` is
**17 domain blueprints** (`minigames.py`, `leveling.py`, `moderation.py`, …) and
`embed-composer.js` was extracted once a second consumer appeared — and that worked.
That is the whole method. Full Clean Architecture (`usecases/`, `adapters/`, `ports/`,
domain models with injected gateways) is sized for a team shipping a product; for a
bot dashboard with one maintainer it costs more than it returns.

What actually buys you the benefits (modularity, testability, separation) at
~1/10 the ceremony:

* **One seam per layer, not four.** `route (thin) → service function → db access`.
  The service takes plain args and returns plain dicts — that alone is unit-testable.
* **SQL in exactly one place per domain.** Today ~11 API modules each open
  `aiosqlite.connect(DB_PATH)` inline. That is the real coupling, not the missing folders.
* **A response contract.** 110+ endpoints hand-roll `{"success": …, "error": …}`;
  one decorator gives consistent shape, status codes, and permission errors.
* **A JS convention instead of a framework.** `window.NERO.pages.<name> = {init}` —
  the composer already proves it works; it is exactly what `embed-composer.js` is.
* **Tests that run.** The harnesses exist (104 assertions right now); `npm test` +
  the CI workflow (see §4) is the only missing machinery — no framework needed.

Decision rule worth keeping: **extract on the second consumer, never the first.**
(`embed-composer.js` = 2 consumers → right call. A `components/` tree for one picker =
speculative.)

---

## 3. Phased plan — every phase ships alone, no big-bang

**Phase 1 — de-risk the server module (a day, zero behaviour change)**
* `dashboard/config.py` — env parsing + `_force_http` + `SECRET_KEY` validation.
* `dashboard/security.py` — CSP/headers, CSRF guard, `rate_limit`.
  → CSP policy becomes a *named, testable thing* instead of a string buried 130 lines
  into a 2.7 k-line file (which is how a preview-breaking policy stayed invisible).
* `app.py` keeps routes only.

**Phase 2 — one data seam (incremental, per domain)**
* `dashboard/db.py`: `def read(fn)` / `def write(fn)` context-manager helpers over
  `aiosqlite` + `DB_PATH`.
* `api/<domain>.py` moves its SQL into `utils/<domain>_store.py` as pure functions.
* Do it domain-by-domain, starting with the most-churned (`minigames`, `commands`,
  `embedbuilder`) — never as a sweep.

**Phase 3 — the asset budget (this is where "heavy" actually dies)**
* `{% block page_scripts %}` / `{% block page_styles %}`; `base.html` stops loading
  htmx/jQuery/select2 globally and pages opt in (7 of 41 need htmx, 12 the pickers).
* `defer` on everything in the head; self-host Inter.
* Extract page JS → `static/js/pages/embedbuilder.js` etc.; templates keep markup only.
* Kill the top inline-style offenders into `main.css` classes.

**Phase 4 — frontend modules, by pain not by plan**
* Next extraction candidate: the picker stack (`nero-select.js` + `nero-alias-input.js`,
  25 KB + jQuery dependency) into one module jQuery isn't required for; that alone
  removes jQuery from most pages.

**Phase 5 — only if/when it earns it**
* A response-contract decorator across all 110+ endpoints.
* `content-visibility: auto` on long lists (added in `embed-composer.css` for embed
  cards + attachment tiles in `5e91af2`) — extend to `commands`/`ledger` tables.
* A shared "state of a thing" widget: `pending / ok / degraded(reason) / failed(reason)`.
  The attachment tile is the first instance; every upload-ish surface wants it.

---

## 4. Graceful degradation — the actual pattern in this codebase

You already do this well in places; that is the house style to extend, not invent:

| Surface | Today | Pattern |
|---|---|---|
| pickers | `if (window.$ && $.fn.select2)` | feature-detect, degrade to a native `<select>` |
| drafts | IndexedDB wrapped in `try/catch` | storage is an optimization, never a dependency |
| uploads | `createObjectURL` in `try/catch` | renderer survives a quota error |
| mentions | preview falls back to raw IDs if role/channel fetch fails | partial data still renders |
| **previews (new)** | **probe `blob:` → `data:` → stated reason** | **degrade to slower, not broken** |
| **large previews (new)** | **≤5 MB inline, else "no preview — still sends"** | **bounded memory, never a freeze** |
| **low-end devices (new)** | **`loading=lazy decoding=async`, `content-visibility`, `prefers-reduced-motion`** | **main-thread stays interactive** |
| CDN outage | *(none)* | see below |

Three remaining gaps worth closing:
1. **CDN failure is currently silent.** If unpkg/jsDelivr are unreachable, `htmx` and
   `jQuery` are undefined and pages quietly lose behaviour. Cheap fix: a local copy of
   the 3 files + `onerror` fallback injection, or serve them from `/static` (also fixes
   CSP review, geo-latency and offline dev). A missing `defer`red script should
   `showToast('Enhanced pickers unavailable — using plain selects')`, not nothing.
2. **Degrade, then explain.** Every fallback should leave one console line and, where the
   user can act, an affordance (retry). "No preview (too large — it will still be sent)"
   beats a blank box; that single change is what turns an outage into a non-issue.
3. **A tiny capability registry** instead of scattered `if (window.X)`:
   `EC.supports = {blobPreviews, indexedDB, htmx, select2}` — computed once at boot,
   read everywhere, and it gives you an opt-out switch per enhancement (your "feature
   flag" ask, without a flags service):
   `NERO_DISABLE = ['select2']` from an env var → the page behaves as if absent.

Slow-network/low-device posture: no page should need more than 1 HTML + 2 CSS + 2 JS to
*render*; everything else (emoji lists, role/channel lookups, external emojis) is already
lazy — keep that rule, and never move it into page init.

---

## 5. Merge plan for the `update` workflow (what to change, minimal)

Observed risk, not hypothetical: this repo's history is orphaned single-commit snapshots
(`Add files via upload`) landing on `main`, with **zero CI** — which is precisely how a
CSP policy that breaks previews reached `main` and how PR #19 conflicted.

1. **One long-lived integration branch** — keep `update` as it is; `main` = released.
   Do not upload files through the GitHub web UI any more: each upload mints an
   unrooted snapshot and costs a merge like the one we just did.
2. **Unit of work = branch + PR into `update`** (`arena/*` already fits). Squash-merge is
   fine; the tree is what matters.
3. **Land the CI file** (already written, see `ci-tests-workflow.yml.example`) — a GitHub
   App can't create `.github/workflows/*`, so `git mv` it yourself. It runs: JS syntax
   check, the 3 Node harnesses, `compileall` import check, the Python harnesses.
   ~90 s. Add "tests must pass" as a required check on both branches.
4. **Test coverage worth adding next** (each = one small harness, same style):
   CSP policy shape (started), `/api/embedbuilder/send` multipart → Discord payload
   mapping, draft round-trip with attachments, picker behaviour with the CDN absent.
5. **Rollout for risky pieces** (asset budget in Phase 3): behind a per-page opt-out
   (`?legacy=1` for a week), verify 3 heavy pages (embedbuilder, commands, minigames),
   then delete the flag. Revert = one commit, because phases are separable.

---

## 6. Milestones — MVP first

| Week | Ship | Done when |
|---|---|---|
| 0 *(now)* | CSP + preview fallback + type sniffing + 3 harnesses + test runner | merged into `update`; `curl -I` shows `blob:`; upload of HEIC/big image shows a state, never a blank tile |
| 1 | `.github/workflows/tests.yml` + required check; `defer` on the 3 CDN scripts | PRs can't merge red; Lighthouse FCP drops on the heaviest page |
| 2 | self-host Inter, remove global htmx/jQuery/select2 → per-page | only 7/41 pages request htmx; 0 render-blocking CDN CSS |
| 3 | Phase 1 (`config.py`, `security.py`) | `app.py` < 1.5k lines, behaviour diff = 0 |
| 4–5 | Phase 2 for `minigames`, `commands`, `embedbuilder` | 3 domains with no inline `aiosqlite` |
| 6 | Phase 3 (page JS/CSS per page, inline-style cleanup) | no template > 600 lines; perceived "heaviness" re-checked with you |
| 7+ | Phase 4/5 as pain appears | picker stack without jQuery; capability registry in place |

Explicitly **not** doing: a build step/bundler, a rewrite to React/Vue, a generic
`components/` framework, per-page virtualized tables before they are slow in practice.
The repo's own `embed-composer.js` extraction is the correct amount of architecture here
— copy *that* shape (one module, second consumer, its own harness) everywhere.
