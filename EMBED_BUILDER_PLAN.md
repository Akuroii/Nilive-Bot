# Embed/Message Builder — locked implementation plan (v3)

**Status: awaiting approval. No application code has been changed.**
Working tree contains this document and the two earlier design documents only.

Companion documents: `EMBED_BUILDER_REDESIGN.md` (first proposal: Discord limits research, data model,
wireframes) and `EMBED_BUILDER_REVIEW.md` (the 25-point review: bug classification, measured performance
chain, dependency map, HTMX lifecycle design). This document **locks decisions and sequences the work**;
where it contradicts an earlier document, this one wins.

---

## 0. Two facts discovered while writing this plan that changed the design

**Fact 1 — Discord attachment CDN URLs are signed and expire (~24 h).** Since late 2023 every attachment
URL carries `ex` (expiry) / `is` (issued) / `hm` (HMAC). The Discord client and the API refresh them
transparently *inside* Discord, but a URL a bot stores and re-sends later will fail once expired
("This content is no longer available"). Deleting the source message deletes the bytes.
→ **Consequence:** "capture the CDN URL after publish" is **not** a durable portability mechanism. The
durable handle is the attachment's **identity** (`channel_id` + `message_id` + `attachment_id` +
`filename`), from which a **fresh signed URL can be obtained by fetching the message** via the
documented API. Case C is redesigned around that (§3.5).

**Fact 2 — the "moderator 403 on bot identity" issue I raised in the first proposal is not real**
(the builder page is `LEVEL_OWNER`-gated). Already corrected in the review; the Phase 0 work item is the
*blocking* fetch, not a permission fix.

---

## 1. Locked decisions (your 22 principles → what they mean for the code)

| # | Decision | Implementation consequence |
|---|---|---|
| 1 | Fix the HTMX lifecycle properly | Page-module registry (`init(root)`/`destroy()`), driven by htmx events; no page script relies on being re-executed |
| 2 | No page refreshes as a solution | No `location.reload()` anywhere in the new code; state is rebuilt from the store, never by reloading |
| 3 | No React/Vue/other framework | Plain ES5-safe JS modules on `window.NERO.*`, consistent with `nerо-select.js`/`dashboard.js` |
| 4 | Keep existing architecture & conventions | Jinja templates + htmx + `dashboard/api/*` blueprints + `utils/*` domain modules + Node harnesses; no build step, no npm runtime deps |
| 5 | One normalized message model | `MessageDocument` is the only shape the builder mutates; nothing else holds embed state |
| 6 | One payload transformation | `toDiscordPayload(model)` is used by preview **and** send; no second serializer |
| 7 | Centralize Discord limits | `utils/discord_limits.py` is the authority; served to the client; no literal limits in templates/components |
| 8 | Fix editor churn first | Phase 1 delivers the rail + single-card inspector before any preview-CPU work; measured baseline recorded |
| 9 | Stable/targeted preview updates | Keyed skeleton + patch; images are never re-created unless their resolved source changed |
| 10 | IndexedDB can never blank the builder | Never awaited before first paint; blocked/errored/timeout → "drafts disabled" degraded mode |
| 11 | Fix all existing save/load data loss | Phase 0 extends the current editor/payload additively (`author.icon`, `author.url`, `footer.icon`, `url`, `timestamp`) |
| 12 | Footer Icon + Author Icon support | Real fields in Phase 0 (URL mode) and full URL/upload in Phase 2 |
| 13 | URL + local upload modes | `ImageSource = { mode: 'url' } | { mode: 'asset' }` per slot |
| 14 | `attachment://filename` used correctly | Uploads travel as `files[n]` in the same multipart request; the slot URL is `attachment://<effective filename>` |
| 15 | Components V2 out of scope | No V2 flag, no container/section/text-display components; extension points documented (§5.4) |
| 16 | Buttons use an Action abstraction | `ButtonComponent.action: ActionConfig`; UI never talks to the backend directly |
| 17 | Actions support multiple role IDs | `roleIds: string[]` + optional role sets; no single-role shape anywhere in the model |
| 18 | Select menus: multiple options + actions | `SelectComponent.options[]`, each with its own `action` |
| 19 | Copy-on-load | Loading a saved embed/message copies by value with `source: {id, revision}` provenance |
| 20 | Backward compatibility | Legacy import is read-only, in-memory upgrade, conversion report; nothing rewritten in place |
| 21 | Validation on both sides | Same rule codes/messages from one table; client for feedback, server as the gate before Discord |
| 22 | Don't touch unrelated modules | Every shared-file change is listed in §10 with its justification; anything else is out of bounds |

### Adjustments you added

| Adjustment | How it changes the plan |
|---|---|
| **V2 route is temporary, not a second permanent builder** | `/embed-builder/v2` exists only from Phase 1 to the Phase 3 exit. `/embed-builder` becomes the new implementation at a defined switch point; legacy is rollback-only and is **deleted** in a defined removal step (§6). No feature is ever added to the legacy page after the switch. |
| **Explicit asset lifecycle** | New §3: four states (local → draft-local → saved-unpublished → published/Discord-backed), case-by-case behaviour (A–E), and one resolution function shared by preview and send. |
| **Message Builder, not Reaction-Role Builder** | The document is `Message { content, embeds[], components[] }`; `role.add/remove/toggle` are one category of action. Reaction roles become a *preset* (a role-menu starter), not the organising idea. Rail sections are Content / Embeds / Components (§4). |
| **Progressive UI** | The shell appears in Phase 1 with Content + Embeds only; Components are added in Phase 3. No phase builds UI for a later phase's data. |
| **Your phase order** | Adopted as-is (Phase 0 bugs → 1 model/preview → 2 images+save/load → 3 components → 4 actions → 5 saved messages/publishing). One deviation is proposed and justified in §7.1: the **server-side roles/channels TTL cache** moves to Phase 1 instead of Phase 0. |
| **Measurable performance criteria** | New §8: assertions in harnesses plus a dev HUD; targets are budgets with printed deltas, structural invariants are hard failures. |
| **No blocking init** | New §9: identity is injected with the page (SQLite read, zero Discord calls); roles/channels are prefetched after first paint, never awaited by init. |
| **Don't break shared APIs** | New §10: the consumer list I verified, plus the "backward-compatible extension only" rule and the tests that enforce it. |
| **Observability** | New §11: dev-only counters + HUD, disabled by default, no production noise. |
| **Error recovery** | New §12: every async operation has a failure state and a watchdog; nothing can stay "loading" forever. |

---

## 2. Changed from my previous proposal

| Area | Previous plan | Now | Why |
|---|---|---|---|
| Phase order for save/load | Own phase after preview rework | Merged into Phase 2 with images | Assets and save/load are inseparable: a saved embed that references local bytes is meaningless without the asset model |
| Saved Messages / publishing | Phase 5–6 (before components in one draft) | **Last** (Phase 5), after components + actions | A Saved Message contains components; saving messages before components exist forces a schema change later |
| Reaction-roles replacement | "Replace the page with a preset once verified" | Same, but now explicit: role actions are *one action category* in the Message Builder; the old page is retired only after Phase 5 publish/update is verified, and it is never the conceptual base | Your adjustment 3 |
| CDN capture | "Capture the URL → config becomes portable" | Identity-based: capture `{channelId, messageId, attachmentId, filename}`; resolve a fresh URL via message fetch; the raw URL is a short-lived convenience only | Discord signs attachment URLs with a ~24 h expiry; the previous design would have shipped a silent breakage |
| Identity loading | Remove the admin gate / add an endpoint | Inject with the page (`__BOT_IDENTITY__`); no gate change, no new endpoint | Verified: the page is already OWNER-only, so there is no gate to fix; the real problem was a blocking HTTP call |
| Roles/channels fetch | "Add a server-side cache" (Phase 0) | Client-side non-blocking prefetch in Phase 0; server TTL cache in Phase 1 | Keeps Phase 0's blast radius on the builder itself instead of a shared endpoint |
| Preview rewrite | "Rewrite `renderPreview` with keyed patch" | Same, but sequenced after the editor work (your principle 8), and with the minigames consumer asserted byte-for-byte | Measured: editor churn is the bigger cost |
| Markdown | Full Discord renderer in Phase 1 | Phase 0 fixes the *lying* cases (title/author/footer are plain text; code-span shielding) using the existing engine; the full module lands in Phase 1 with the golden corpus | Your Phase 0 list includes "existing preview correctness bugs"; the two that actively mislead are cheap and isolated |
| Components V2 | "Deferred with extension points" | Same, and now explicitly: no V2 code, no V2 flags, no V2 branches in the model | Your decision 15 |

---

## 3. Asset model and lifecycle

### 3.1 The distinction that makes this work

Two different questions must never be merged:

- **What is this image?** → an *asset* (bytes + identity + provenance). Durable, deduped.
- **How do we satisfy this image slot right now?** → a *resolution* (fresh upload, Discord-hosted URL, or
  an error). Changes with context (which browser, whether the message was published, whether the source
  message still exists).

So the model stores a **source** per slot and keeps **availability** on the asset:

```ts
type ImageSource =
  | { mode: 'url';   url: string }                    // pasted URL (user's host; durable by definition)
  | { mode: 'asset'; assetId: string };               // uploaded file (bytes owned by the browser)

interface AssetRecord {
  assetId: string;              // 'a_' + sha256.slice(0,16) — content-addressed, so dedupe is free
  sha256: string;
  mime: 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif';
  bytes: number;
  width?: number; height?: number;
  originalName: string;
  filename: string;             // sanitised: lowercase, [a-z0-9._-], ≤ 64 chars, no leading dot
  availability: 'bytes-local' | 'bytes-missing';
  createdAt: string;
  // Filled only by a successful publish:
  discord?: {
    channelId: string;
    messageId: string;          // the message the attachment was uploaded with
    attachmentId: string;
    filename: string;           // as Discord stored it
    lastResolvedUrl?: string;   // fresh signed URL, cached
    lastResolvedExpiresAt?: string;  // from the URL's `ex` parameter
    resolvedAt?: string;
    sourceMessageState?: 'unknown' | 'present' | 'deleted';
  };
}
```

**States (your four, named):**

| State | Meaning | Where the bytes are | Can this send? |
|---|---|---|---|
| `local` (transient) | File just selected, before the first draft write (typically < 1.5 s) | Memory | Yes |
| `draft-local` (durable in browser) | Referenced by a draft; bytes in IndexedDB | This browser's IDB | Yes, in this browser |
| `saved-unpublished` | Referenced by a Saved Embed/Message; server knows *about* the asset (id, filename, mime, bytes, sha256) but never holds the bytes | This browser's IDB (or gone) | Yes if bytes are local; otherwise see §3.3 |
| `published` / Discord-backed | The asset was uploaded with a message that still exists; `discord.{channelId,messageId,attachmentId}` is recorded | This browser's IDB, **and** resolvable from Discord | Yes, from any browser, while the source message exists |

The server-side row (`asset_library`) stores metadata + the `discord` handle — **never bytes**. That keeps
the project's existing "no file hosting" property.

### 3.2 One resolution function (shared by preview and send)

```
resolveImageSource(source, ctx) → { kind, url?, file?, filename?, reason? }

  mode 'url'                          → { kind: 'url', url }
  mode 'asset', bytes present         → { kind: 'upload', file, filename }            // fresh attachment
  mode 'asset', bytes absent,
      discord handle present          → { kind: 'discord-ref', channelId, messageId, attachmentId }
                                        // server resolves a fresh signed URL at send time
  mode 'asset', bytes absent, no ref  → { kind: 'unavailable', reason: 'bytes-missing' }
```

`ctx` carries `debugTag`/timing hooks. The **preview** uses the same function, with two differences: for
`upload` it prefers the cached `blob:`/`data:` URL, and for `discord-ref` it shows the last resolved URL if
it is still unexpired, otherwise an "unavailable" tile. The **send** path turns `discord-ref` into a fresh
URL via one server round-trip (§3.5) and never falls back to an expired URL.

### 3.3 Your cases, resolved

**Case A — upload → Save → close → return.**
The bytes are written to IndexedDB **once, at add time** (never on the typing path). Draft restore brings
back the document and the asset. On return the field shows the image, and validation passes.
Opening the *saved embed* in a different browser shows the `saved-unpublished` state: the field renders a
neutral "Discord copy not available yet — local image required" tile, and validation raises a **warning**
(nothing is lost; the config is simply not sendable from there yet).

**Case B — upload → Save → later edit.**
Editing text never touches assets. Replacing the image creates a new content-addressed asset
(`a_<newhash>`); the old asset's reference count drops. GC runs at idle and removes a record only when
**no** draft, current document, or stored revision references it (see §3.6). Saved revisions keep working
because their asset is still referenced by that revision; if a user restores an old revision whose asset
was manually deleted, that restore shows the Case D state, never a silent broken image.

**Case C — publish succeeded; the config should become portable.**
On success we capture `{channelId, messageId, attachmentId, filename}` (from the send response) and store
it on the asset (client) and in `asset_library` (server, per guild). Portability then works like this:

- Bytes local → send uploads again (always correct, no dependency on Discord state).
- Bytes absent → the send path asks the server to **resolve a fresh signed URL** for that attachment by
  fetching the recorded message (`GET /channels/{channelId}/messages/{messageId}`) and reading the
  attachment's current `url`. Fresh URLs are always returned by the API for a message you can read.
- Source message deleted, or the bot lost read access → resolution fails → the field flips to
  `unavailable` with an explanation and a "reattach image" action.
- The raw captured URL is kept **only** as an immediately-usable convenience (cached with its `ex`
  deadline and a safety margin); it is never treated as durable, and the UI never claims otherwise.

This is honest about what Discord guarantees, and it is strictly better than "store the URL and hope".

**Case D — saved config, local asset no longer available.**
Three outcomes, in priority order, all explicit:

1. `discord` handle present and resolvable → send proceeds using the resolved URL (no re-upload needed).
2. Handle present but unresolvable (message deleted / no read access) → **error**, blocks send:
   *"The Discord copy of `rules.png` is no longer available (its message was deleted). Reattach the image
   or remove it from this embed."* — with `[Reattach]` / `[Remove image]` actions.
3. No handle at all → **error**, blocks send: *"Local image unavailable — reattach `rules.png` before
   sending. (It was uploaded from another browser/device.)"*

In both error cases the **preview** already shows the unavailable tile before the user tries to send, and
the validation strip counts it. Nothing is ever sent with a placeholder or a broken `attachment://`.

**Case E — one image used by Image + Thumbnail + Footer Icon + Author Icon.**
One `assetId` (same sha256), therefore **one** `files[n]` entry and **one** upload, referenced four times
as `attachment://<filename>`. Filenames are made unique *per message, per distinct asset*: identical
assets share one filename; two different assets that happen to share an original filename get a
deterministic prefix (`a1b2c3-rules.png`). The preview shows the same image in all four slots and the send
summary says "1 file attached". *(Verify with a live test in Phase 2 — see §13.)*

### 3.4 Filename rules (deterministic, testable)

```
sanitise(name)      → lower, strip directories, replace [^a-z0-9._-] with '-', collapse '--', keep ext
uniquify(assets[])  → same filename for the same assetId; prefix '<sha6>-' when two assetIds collide
validate            → extension ∈ {jpg,jpeg,png,webp,gif}; resulting filename ≤ 64 chars; never empty
```

The effective filename is part of the preview's "files to be attached" summary so the user can see exactly
what is being uploaded.

### 3.5 Server side (the only place bytes are ever handled)

```
POST /api/embed-builder/publish            (multipart)
  payload_json  : the document payload, with upload-mode slots already rewritten to attachment://<filename>
  files[n]      : the bytes (only for assets whose resolution kind is 'upload')
  attachments   : [{ id: n, filename }]
  → response: { message_id, channel_id, attachments: [{ assetKey, attachmentId, filename, url, expiresAt }] }

POST /api/embed-builder/assets/resolve     (json)
  { refs: [{ channelId, messageId, attachmentId }] }
  → { results: [{ key, ok, url, expiresAt } | { key, ok:false, reason: 'message-missing' | 'no-access' | 'attachment-gone' }] }
  Implementation: GET the message once per (channel,message) pair, then map attachment_ids → fresh URLs.
  Cached server-side until (expiresAt − 5 min).
```

Both routes are additive; `/api/embedbuilder/send` is untouched and keeps working (§10).

### 3.6 Storage, GC, and honesty about limits

- IndexedDB stores asset bytes in their own object store, written once per asset (never on the typing
  path). The document stores only `assetId`s.
- Refcount = {draft} ∪ {current document} ∪ {every stored revision}; GC at idle removes only records with
  refcount 0. A "Local image cache: 12 MB · [Clear]" control lives in the builder's settings popover.
- If IndexedDB is unavailable, everything still works **in memory** for the session; the UI states that
  images and the draft will not survive a reload (degraded mode, §12).
- The UI never says an upload is "stored on the server".

---

## 4. Message Builder framing (not a reaction-role builder)

The conceptual model, locked:

```
Message
├── Content                     (markdown text above the embeds)
├── Embeds[]                    (title, description, fields, images, footer, author, timestamp, url)
└── Components[]                (rows)
    ├── ButtonComponent  → ActionConfig
    └── SelectComponent  → options[] → ActionConfig (per option)

ActionConfig (extensible by data, not inheritance)
├── role.add      { roleIds[], roleSetIds[], policies }
├── role.remove   { roleIds[], roleSetIds[], policies }
├── role.toggle   { roleIds[], roleSetIds[], policies }
├── url.open      { url }
└── custom        { handlerKey, params }        // slot only; no UI, no behaviour in this pass
```

Consequences:

- The workspace is **one document** with three rail sections (Content / Embeds / Components). There is no
  separate "reaction role builder" page in the new architecture; there is a **Role menu preset** (a
  starter that creates a message with an embed and buttons carrying role actions) offered from the
  library, not a different data model.
- Role actions are *one category* among several; nothing in the model, the store, the validation or the
  preview special-cases reaction roles.
- The existing `/reaction-roles` page and `cogs/reactionroles.py` keep working untouched until Phase 5's
  publish/update flow is verified; then the page is retired in favour of the preset, with the cog's
  behaviour preserved (see §7 P5).

---

## 5. Architecture summary (recap + what changes here)

### 5.1 Layers (unchanged from the review, one addition)

```
dom  →  store (document + ui)  →  model (normalize)  →  validate(limits)  →  toDiscordPayload
                                            ↘                                  ↙
                                     resolveImageSource(ctx)  ────────────────┘
                                               preview.patch() · send.request()
```

### 5.2 Files (target layout)

| File | Purpose |
|---|---|
| `static/js/embed/model.js` | normalization, patches, `toDiscordPayload`, asset wiring |
| `static/js/embed/assets.js` | asset store, IDB persistence, dedupe, GC, preview URLs, resolution |
| `static/js/embed/validate.js` | rule evaluation against `discord_limits` |
| `static/js/embed/preview.js` | keyed skeleton + patch (includes the minigames compatibility mode) |
| `static/js/embed/discord-markdown.js` | context-aware Discord renderer |
| `static/js/embed/store.js` | dispatch/subscribe/history/dirty |
| `static/js/embed/components/*.js` | `image-field`, `role-picker`, `emoji-picker`, `repeater`, `button-card`, `select-card`, `action-editor` |
| `static/js/embed/message-builder.js` | the workspace page module (`window.NERO.pages.messageBuilder`) |
| `static/js/embed/library.js` | Saved Embeds / Messages library (Phase 2+) |
| `static/js/embed/debug.js` | dev-only instrumentation (§11) |
| `static/js/nav-lifecycle.js` | page-module registry (§1.1) |
| `dashboard/api/embed_builder.py` | limits, identity helper, publish, assets/resolve, library CRUD |
| `utils/discord_limits.py` | the single limits/messages authority |
| `utils/embed_schema.py` | server-side normalization, validation gate, payload builder |

### 5.3 What stays exactly as it is

`embed-composer.js`'s five public exports (`blankEmbed`, `renderPreview`, `mountEditor`,
`cleanEmbedForPayload`, `embedFromApi`) for the minigames builder; `/api/embedbuilder/*` routes;
`embed_templates`; `cogs/embedbuilder.py`; `utils/emoji.py`; `utils/app_emoji_cache.py`;
`utils/bot_profile.py`'s existing functions; the role/channel/emoji endpoint shapes; the CSP policy;
`cogs/reactionroles.py` and its tables.

### 5.4 Components V2 extension points (documented, not built)

`MessageDocument` gains `layout: 'legacy'` in pass 1; a future V2 mode is `layout: 'v2'` with `blocks[]`
(container/section/text display/media gallery) and `content`/`embeds` empty. `toDiscordPayload` gains one
branch; the store, validation entry point, preview skeleton, component model (buttons/selects are the same
component types in both), action system, persistence and revisions are unaffected. No V2 code ships now.

---

## 6. Route strategy and legacy removal

| Stage | URL | State |
|---|---|---|
| Phase 0 | `/embed-builder` | Existing page, bug-fixed. No redesign. |
| Phase 1 start | `/embed-builder/v2` | New workspace, Content + Embeds only. `/embed-builder` still the daily driver. |
| Phase 1–2 | both | V2 is tested against the same data (legacy templates imported on demand). |
| Phase 3 exit | `/embed-builder` → V2, `?legacy=1` → old page | Switch. Old page reachable for **one release** as rollback only; no new features are added to it. |
| Phase 5 exit (or one release after the switch, whichever is later) | `?legacy=1` removed | Delete `manage/embedbuilder.html`'s legacy script, the legacy branch, and the transitional `renderPreview` compatibility path once the minigames builder has migrated to the new renderer. |

Rules that keep this temporary: the switch is a **defined exit criterion**, not a preference; no data is
written by two paths (the new library writes new tables; legacy templates are read-only after import);
and the legacy page is frozen at the moment of the switch (bug fixes only, and only if a rollback is
actually needed).

---

## 7. Phases

Each phase lists **files changed**, **files untouched**, **migration**, **rollback**, **exit criteria**,
**tests**. Order is yours; the single deviation is flagged in §7.1.

### 7.1 Deviation from your order (one item)

Your Phase 0 includes "blocking builder initialization". I split it:

- **Phase 0** fixes the *builder's* side: identity injected with the page; roles/channels prefetched after
  first paint (parallel, abortable, failure-tolerant); nothing in init awaits the network.
- **Phase 1** adds the server-side TTL cache for `/api/guild/roles` and `/api/guild/channels`.

Reason: those two endpoints are shared by every page that uses `NeroSelect`, so changing them belongs with
the phase that owns the shared-contract tests, not with the emergency bug phase. The Phase 0 client fix
already removes every blocking call from the builder's init path, which is the actual complaint. If you
prefer the cache in Phase 0, it is a one-file addition (`dashboard/api/core.py`) with the same tests —
say the word and I will move it.

### Phase 0 — Foundation / bug fixes (no visual redesign)

**Files changed**

| File | Change |
|---|---|
| `dashboard/templates/base.html` | Register nothing new itself; add `data-page-module` support and the `nav-lifecycle.js` include |
| `dashboard/static/js/nav-lifecycle.js` *(new)* | Page-module registry: `init/destroy`, idempotency marker, htmx event hooks |
| `dashboard/templates/manage/embedbuilder.html` | Convert init to a module with `destroy()`; IDB guards + timeout + degraded mode; server-injected identity; roles/channels prefetch after paint; attachment double-render removed; emoji grid → delegated listeners + filter debounce; per-file size check; markdown **context** fix (title/author/footer plain text) + code-span shielding |
| `dashboard/static/js/embed-composer.js` | Additive only: `opts.extraFields` support in `mountEditor`, `author_icon`/`author_url`/`footer_icon`/`url`/`timestamp` in `cleanEmbedForPayload`/`embedFromApi` (emitted only when set), and context-aware rendering passed to the existing markup function |
| `dashboard/app.py` | Builder route passes `bot_identity` context (SQLite read) |
| `dashboard/templates/base.html` *(context processor, edited file above)* | `window.__BOT_IDENTITY__` emission |
| `dashboard/api/embedbuilder.py` | Per-file size check on send; `GET /api/embed-builder/limits` |
| `utils/discord_limits.py` *(new)* | The limits table + friendly messages |
| `utils/embed_schema.py` *(new)* | Server-side validation used by the send route (counts + per-file + totals) |
| `scripts/test_nav_lifecycle.js`, `scripts/test_embed_builder_boot.js` *(new)* | Harnesses |

**Untouched:** `database.py`, `cogs/*`, `utils/emoji.py`, `utils/app_emoji_cache.py`,
`utils/bot_profile.py` functions, `dashboard/api/core.py`, `/reaction-roles`, minigames builder, all other
templates.

**Migration:** none.

**Rollback:** revert the commit. The registry is inert unless a page declares a module; the composer
additions are opt-in via `opts.extraFields`.

**Exit criteria:** the existing builder opens and works when opened directly, via the sidebar, after
navigating away and back, after browser back/forward, and after a refresh; no listener growth across
repeated enter/leave; IDB blocked/unavailable no longer blanks the page; the four missing image/metadata
fields survive save → load → save; no unrelated page regresses (spot-check: commands, tickets, members,
leveling, minigames builder — all picker-bearing pages).

**Tests:** §13 rows N1–N6, B1–B3, B7.

### Phase 1 — New Embed Builder foundation (`/embed-builder/v2`)

**Files changed:** new `static/js/embed/{store,model,validate,preview,discord-markdown,debug}.js`;
new `static/js/embed/components/{repeater,image-field(url-only)}.js`; new
`static/js/embed/message-builder.js`; new template `manage/embed_builder_v2.html`; new route in
`dashboard/app.py`; `dashboard/api/embed_builder.py` (limits, identity, validate); `embed-composer.js`
reduced to a facade over the new pure modules (five exports preserved); `dashboard/api/core.py` (TTL cache
for roles/channels).

**Untouched:** `manage/embedbuilder.html` (the live page), database tables, cogs, reaction roles.

**Migration:** none for data. Documents are built in memory only; nothing is saved yet.

**Rollback:** delete the route + template; the live page is untouched, so rollback is a no-op for users.

**Exit criteria:** typing in content/title/description/field value does **not** rebuild the editor (0
card renders); preview updates text without re-creating images/fields; the payload produced for all
currently-supported fields is **byte-identical** to `cleanEmbedsForPayload` output; markdown matches the
golden corpus; 10 embeds × 25 fields remains usable (structural edit < 1 card rebuild; interaction latency
budget met); validation runs client-side from the served limits table.

**Tests:** §13 rows B4–B6, B8–B12, P1–P5.

### Phase 2 — Images + save/load

**Files changed:** `embed/assets.js`; `embed/components/image-field.js` (URL + upload); new tables in
`database.py` (`saved_embeds`, `saved_embed_revisions`, `asset_library`) — additive only;
`api/embed_builder.py` (library CRUD, publish-with-attachments for a single embed, `assets/resolve`);
`utils/embed_store.py` (new); `utils/embed_schema.py` (asset integrity, filename rules, `attachment://`
rewriting); `embed/library.js` + template for the library screen.

**Untouched:** `embed_templates` (read-only via the importer), `cogs/embedbuilder.py`, legacy routes,
reaction roles.

**Migration:** legacy → v2 importer (in memory, on read) covering: bare embed dict, `{content, embeds}`,
`footer_icon`/`author_icon` keys the cog already expects, four colour representations, placeholder
`\u200b` fields, missing keys, corrupt JSON, unknown keys (preserved and reported). Nothing is rewritten in
place; saving always writes a new v2 row.

**Rollback:** feature flag on the library UI; the legacy builder and `embed_templates` are untouched, so
disabling the flag restores the previous behaviour exactly. New tables are ignored if unused.

**Exit criteria:** all four image slots accept URL and upload; one binary used four times uploads once;
`attachment://` verified live (attachment hidden, image renders); Case A–E behaviour matches §3.3
(including the two blocking error states); legacy templates load with a conversion report; saved embeds
load as copies with provenance.

**Tests:** §13 rows I1–I9, S1–S6, plus the live Discord verification checklist.

### Phase 3 — Components

**Files changed:** `embed/model.js` (rows/components), `embed/validate.js` (component rules),
`embed/preview.js` (action rows + select rendering), new `components/{button-card,select-card,emoji-picker}.js`,
`embed/message-builder.js` (rail gains Components; reorder), limits table (component entries already
present), template + route wiring.

**Untouched:** actions behaviour (a button can be configured but its action is inert until Phase 4 —
enforced by validation: "this button has no action yet" is a warning, and the document cannot be published
in this phase), reaction roles, cogs.

**Migration:** none (older documents normalize with `rows: []`).

**Rollback:** the Components rail section is flag-gated; Phase 2 documents are unaffected because
`rows: []` is valid.

**Exit criteria:** ≤ 5 buttons/row, 1 select/row, ≤ 5 rows, ≤ 25 options, unique `custom_id`s, label/value
limits — all enforced from the served limits; preview renders buttons/selects with correct styles/emoji;
reorder works by drag **and** keyboard; adding the 6th button is impossible with a friendly reason.

**Tests:** §13 rows C1–C7.

### Phase 4 — Actions / role system

**Files changed:** `embed/model.js` (action configs), `embed/validate.js` (action rules incl. hierarchy),
new `components/action-editor.js`, `components/role-picker.js`, role-set support (table `role_sets` +
`api/embed_builder.py` endpoints), preview (disabled/link styling hints).

**Untouched:** `cogs/reactionroles.py` and its tables (the runtime executor arrives in Phase 5; nothing
acts on the new configurations yet).

**Migration:** importing a legacy panel maps `role_id` → `roleIds: [role_id]` with the same policies
(`booster_only`, `required_role_id`, `expiry_days`, exclusivity, max roles).

**Rollback:** the action editor is flag-gated; documents with actions are still valid documents (they are
simply not sendable until Phase 5).

**Exit criteria:** multi-role add/remove/toggle; role picker meets §14 of the review (search, chips,
colour, paging, loading/error/missing states, not-assignable roles shown disabled with the reason);
validation warns on `@everyone`, integration-managed roles, and roles at/above the bot's top role with the
exact fix; role sets are in **only if** they fit in one component file and one table (otherwise deferred).

**Tests:** §13 rows A1–A6.

### Phase 5 — Saved Messages / publishing

**Files changed:** new tables (`saved_messages`, `saved_message_revisions`, `message_components`,
`message_actions`, `message_publications`); `api/embed_builder.py` (message CRUD, publish, update, assets
resolve), `utils/embed_store.py`; `embed/library.js` (message library, revisions, publications);
`embed/message-builder.js` (publish/update UI); new `cogs/components.py` (persistent views + action
executor) + `main.py` (load); `manage/reactionroles.html` (retired → preset entry).

**Untouched:** `cogs/reactionroles.py` until the new executor passes its live smoke test; then it is left
loaded but unused for one release, then removed in a separate, explicitly-approved change.

**Migration:** a per-panel "Migrate to message" action creates a Saved Message from existing
`reaction_roles` rows; the live message keeps its old view until the user republishes. No row is deleted.

**Rollback:** config flag to unload the new cog; publications are rows (nothing to undo); the old panel
keeps working either way.

**Exit criteria:** publish creates a new message and records a publication; update-live edits exactly the
recorded message after naming channel + message in the confirmation; a role button with 4 roles works in a
test guild (add/remove/toggle); CDN identity captured and re-resolution verified after the raw URL
expires; "never double-grant / never double-sweep" invariants hold.

**Tests:** §13 rows M1–M6, X1–X4, plus the live role-action smoke test.

---

## 8. Performance requirements (measurable)

### 8.1 Hard invariants (asserted in harnesses — a failure fails the build)

| # | Invariant | How it is asserted |
|---|---|---|
| P1 | Typing in a text field performs **zero** editor-card renders | Instrument `mountEditor`/rail render counter; dispatch 20 keystrokes; assert counter unchanged |
| P2 | Typing never replaces the focused input | Capture the element reference before/after; assert identity (`===`) and that `document.activeElement` is unchanged |
| P3 | Typing never creates `<img>` elements | Snapshot the image element set; assert same references after a keystroke burst |
| P4 | Typing performs at most one markup parse per changed field | Memoisation counter keyed by `(text, lookupsVersion, context)` |
| P5 | Structural edit (add field/row/option) rebuilds **exactly one** card/row | Per-key render counters |
| P6 | Leaving the page removes every document/window listener, timer and object URL it created | Registry size + `URL.revokeObjectURL` call count return to baseline after `destroy()` |
| P7 | Entering/leaving 5× does not accumulate anything | Registry/listener counts flat across cycles; `NERO.pages` holds one module |
| P8 | A draft write never carries Blobs on the typing path | Writer instrumentation: bytes written during a keystroke burst = 0 |
| P9 | Emoji grid binds a bounded number of listeners regardless of cell count | Listener count ≤ 2 per grid, independent of 282 cells |

### 8.2 Budgets (measured, printed, warned — not hard failures, because they are machine-dependent)

| Scenario | Budget | Instrumentation |
|---|---|---|
| Keystroke → preview text updated (10 embeds × 25 fields, 4 images) | p95 ≤ 16 ms | `performance.now()` around dispatch + patch, reported by the dev HUD |
| Structural edit (add field) to painted | ≤ 50 ms | same |
| First meaningful paint of the workspace (warm cache, no draft) | ≤ 1.0 s | `performance.mark`/`measure` around init phases |
| Init without network (identity injected, roles prefetch deferred) | ≤ 250 ms to interactive | init timing breakdown in the HUD |
| IndexedDB restore of a draft with 3 assets (≤ 6 MB total) | ≤ 400 ms | async timing in the HUD |
| Image preview resolution (first paint of a 2 MB image) | ≤ 300 ms | HUD + `PerformanceObserver` on image resources |
| Memory after attach-20 MB → leave page → return | no growth beyond one live copy | manual checklist + `performance.memory` snapshot |

### 8.3 Browser-level verification (manual checklist, run per phase)

1. Network panel, filter `Img`: typing in content/title/description produces **no** image requests.
2. Performance panel: record a 10-second typing burst on the maximum document; the flame chart must show
   patching work, not `parseHTML` of a large subtree per keystroke.
3. Elements panel: with 10 embeds expanded, the DOM node count must be stable while typing.
4. Repeated sidebar round-trips: `NERO.debug.report().listeners` flat; no orphan blob URLs.
5. Low-end simulation (CPU 4× throttle): typing remains responsive at the maximum document size.

### 8.4 The baseline problem

The current builder cannot be measured with the new instrumentation (it has none), so Phase 0 records a
manual baseline first — the two Node probes from the review (`.28 ms / 5 459 B / 75 elements` for the
preview; `3.6 KB → 230.5 KB / 1 870 elements / 830 inputs` for the editor) plus a Performance-panel
recording of a 10-second typing burst and of "add embed" ×9 on the untouched page. Those numbers go into
the harness header as the "before" column so the improvement is demonstrable rather than asserted.

---

## 9. Backend initialisation (the three calls)

| Data | Today | Plan |
|---|---|---|
| Bot name + global/per-guild avatar + banner + top-role position | `fetch('/api/botprofile/config')` → `LEVEL_ADMIN` route → **blocking** `requests.get(..., timeout=8)` on the request path, plus a second fetch duplicated in the minigames builder | **Injected with the page** as `window.__BOT_IDENTITY__` from a SQLite-only read (`get_guild_bot_profile`) + the top-role position computed from the already-available role list. Zero Discord calls at init. A background refresh (fired after first paint) updates the stored row so a Discord-side change is picked up on the next load |
| Roles | `fetch('/api/guild/roles')` → blocking Discord call, no cache | Client: prefetch **after** first paint, parallel, `AbortController`, failure-tolerant (raw IDs render meanwhile). Server: 60 s TTL cache (Phase 1) so it stops hitting Discord per load. Response shape unchanged |
| Channels | same | same |

Safety review of what is exposed (your constraint): name, avatar, banner and top-role position are all
visible to any member of that guild, and the payload is rendered only into the builder page (already
OWNER-gated). No token, no application secret, no bio, no other guild's data, and **no new public
endpoint** — the data rides on the page render, exactly like `__CURRENCY__` and `__CHECK_ICON__` already do.

---

## 10. Shared contracts — consumers and the change rule

| Contract | Verified consumers | Change allowed? |
|---|---|---|
| `utils/emoji.py` (`parse_emoji_input`, `emoji_cdn_url`, `is_custom_emoji_token`) | `dashboard/utils/check_icon.py`, `dashboard/utils/currency_ctx.py`, `rank_card_renderer.py`, 2 test harnesses | **None** (additive helpers only if unavoidable) |
| `utils/app_emoji_cache.py` | `dashboard/api/economy_shop.py`, `dashboard/utils/check_icon.py` | **None** |
| `utils/bot_profile.py` | `cogs/botprofile.py`, `dashboard/api/botprofile.py` | **Additive** (a cached read; existing functions untouched) |
| `/api/guild/roles`, `/guild/channels` | `NeroSelect` across members/moderation/tickets/leveling/economy pages, embed builder, minigames builder | **Same shape.** A TTL cache is a pure latency change; any new field must be additive and unused by old callers |
| `/api/guild/emojis`, `/guild/emojis/external`, `/guild/resolve-user/<id>` | Embed builder, minigames builder, economy currency form | Same shape |
| `embed-composer.js` exports (5) | `systems/minigame_builder.html`, 2 harnesses | **Signature frozen**; internals may change; new capabilities are opt-in options |
| `embed_templates` table | `cogs/embedbuilder.py` (7 commands), `dashboard/api/embedbuilder.py` (5 routes), `dashboard/app.py` (4 legacy routes) | **Read-only for the new code**; no schema change, no destructive write |
| `cogs/reactionroles.py` + its 4 tables | Live panels; `on_ready` view restore; expiry loop; member-update listener | **No change** in this plan (Phase 5 adds a *new* cog alongside) |
| `/api/embedbuilder/send`, `/template/*`, `/app-emojis*` | Builder page, economy (module), check-icon (module) | Send route: additive validation only; app-emoji routes stay where they are |
| `dashboard/static/js/dashboard.js` `reInitDashboardComponents` | All picker pages | **Reuse it**; the new registry complements it rather than replacing it (it stays the owner of `NeroSelect`/`NeroAlias` init) |
| CSP (`img-src 'self' data: blob: https:`) | Every page's images | **No change** |

Rule: if a change to a shared contract is genuinely required, it must be **backward-compatible extension**
plus a harness asserting the old shape still satisfies old callers. No exceptions.

---

## 11. Observability (development only)

`static/js/embed/debug.js` — counters are plain integers (always compiled in, no I/O); **output** is
strictly gated:

```
enable via:  window.__NERO_DEBUG__ = true   (dev console)
             ?debug=1                       (any environment)  → panel
             localStorage.nero_debug = '1'  (sticky, dev)

report():    {
  init:      { total, phases: { restore, identity, roles, channels, firstPaint } },
  renders:   { editor, card, preview, previewPatch, rail },
  payloads:  { built }, validates: { runs, ms, issues },
  api:       [ { name, ms, ok, status } ],
  storage:   { idbOpenMs, idbRestoreMs, writes, bytesWritten, blocked },
  images:    [ { assetId, resolveMs, kind, ok } ],
  listeners: { document, window, element },
  objectUrls:{ created, revoked, live },
  events:    ring buffer (200) of { t, type, detail }
}
```

- The HUD is a small collapsible panel (10 lines, top-right, dark, monospace) showing the live counters;
  `NERO.debug.report()` prints a table; `NERO.debug.reset()` clears.
- **No production noise:** with no flag set, nothing is printed, no panel is created, no timers run. The
  only always-on cost is integer increments.
- The same counters are what the Node harnesses assert on (`P1–P9`), so the instrumentation is not
  throwaway: it is the test surface.
- Server side: the existing `log_action` audit entries cover publish/update; no new logging framework.

---

## 12. Error recovery matrix (nothing stays "loading" forever)

| Operation | Loading state | Failure state | Degrade / retry | Watchdog |
|---|---|---|---|---|
| Page init | Skeleton rail + inspector | Inline error banner with the failing step | Preview + validation still render from whatever loaded | init phases each log a budget warning at 1 s |
| Identity (injected) | n/a (server-rendered) | Fallback name "Bot" + generated avatar | Background refresh retries once, silently | 5 s on the refresh only |
| Roles / channels prefetch | Mentions show raw IDs (no spinner) | Muted note "role names unavailable — [Retry]" | Picker falls back to id entry; retry on demand | 8 s, then degrade |
| Emoji list | Skeleton cells | "Couldn't load emoji — [Retry]" | Unicode + recents keep working; app-import still available | 8 s |
| Image URL test | Per-field spinner | `broken` + status code + [Retry]/[Remove] | Never automatic; explicit test only | 10 s |
| Image upload (local read) | Per-field progress | `unsupported` / `too-large` / `decode-failed` with the reason | File never partially stored | 15 s |
| IndexedDB open/restore | none (never blocks paint) | "Drafts are unavailable in this browser" (muted, once) | Full in-memory operation | 2 s → degraded, `onblocked` handled |
| Draft write | none | Muted "draft not saved" + reason | Retry on next idle; attachments dropped first on quota | per-write try/catch |
| Saved-embed/message load | Inline spinner in the library row | Row-level error text | Retry button; other rows unaffected | 10 s |
| Asset resolve (Discord ref) | Field shows "resolving…" badge | Case D error with [Reattach] | Retry on demand; never auto-loop | 8 s |
| Validate (server) | Save/publish button shows "checking…" | Issues rendered per field | Client-side issues still shown | 10 s |
| Publish / send | Button disabled + progress text | Discord's own message mapped to the offending field, plus a "check the channel" note when the outcome is unknown | **No automatic retry on timeout** (would risk a double post); explicit retry only | 30 s |
| Role action at click time (bot) | ephemeral "Working…" | Ephemeral explanation (hierarchy, missing permission, managed role) | Bot-side, existing behaviour | Discord's 3 s ack rule handled by deferring |

General rules: every async op has exactly one of
`idle | loading | ok | degraded(reason) | failed(reason, retryable)`; every loading state has a watchdog
that converts it to `failed` with a retry affordance; no spinner without a timeout; no automatic retry of
non-idempotent operations (send/publish).

---

## 13. Testing plan (mapped to phases)

| ID | Harness | Phase | Asserts |
|---|---|---|---|
| N1–N3 | `test_nav_lifecycle.js` | 0 | direct open / swap-in / leave / return / back-forward / 5× cycles: exactly one init per mount, registry empty after destroy, no listener growth, no-op for undeclared pages |
| N4–N6 | `test_embed_builder_boot.js` | 0 | IDB blocked → degraded mode in < 2 s and the page still renders; IDB absent → in-memory mode; identity present without any network call |
| B1–B3 | `test_embed_fields.js` | 0 | all existing fields incl. the four new ones survive save → load → save; empty/whitespace/unicode/RTL; max-length boundaries |
| B4–B6 | `test_discord_markdown.js` | 0→1 | context matrix (title/author/footer literal), code shielding, escapes, ~80-case golden corpus (Phase 1) |
| B7 | `test_legacy_import.py` | 2 | legacy corpus → valid v2 + conversion report |
| B8–B12 | `test_message_model.js` | 1 | normalization, patch semantics, history, dirty hash, payload equality vs. current output for existing fields |
| P1–P5 | `test_preview_patch.js` | 1 | hard invariants §8.1 (no image re-creation, one parse per changed field, one card per structural edit) + timing report |
| P6–P9 | `test_lifecycle_hygiene.js` | 0→1 | listeners/timers/object URLs return to baseline; draft bytes on typing = 0; emoji listener bound |
| I1–I9 | `test_image_field.js` + `test_assets.js` | 2 | URL/upload, sniffing, size, unsupported, dedupe, filename rules, GC refcount, Case A–E state transitions, unavailable → error |
| S1–S6 | `test_embed_store.py` | 2 | saved-embed CRUD, revisions, copy-on-load isolation, publication capture, `assets/resolve` mapping, cross-guild isolation |
| C1–C7 | `test_components_limits.js` | 3 | row/button/select composition, options limits, custom_id uniqueness, reorder (incl. keyboard), preview rendering |
| A1–A6 | `test_actions_model.js` + `test_role_picker.js` | 4 | multi-role configs, policies, legacy panel mapping, picker states (loading/error/missing/not-assignable), hierarchy warnings |
| M1–M6 | `test_message_store.py` | 5 | saved-message CRUD, revisions, publications, update-live mapping, failure mapping |
| X1–X4 | `test_components_executor.py` | 5 | multi-role add/remove/toggle, exclusive/max/booster/required/expiry, never double-grant/sweep, persistent view restore |
| — | `npm test` (existing 5 harnesses) | every phase | must stay green; the minigames + attachment harnesses are the frozen-contract net |

**Live Discord verification (manual, per phase where relevant):** `attachment://` in all four slots with
one file; attachment hidden from the message body; expired-URL re-resolution via message fetch; a role
button with 4 roles in a test guild; hierarchy failure surfaces an ephemeral explanation.

---

## 14. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Composer changes break the **minigames builder** | Medium | High | Additive-only options; 5 exports frozen; both existing harnesses must pass in the same commit; payload equality assertion |
| R2 | Lifecycle registry regresses **other pages** | Low | High | Inert unless declared; complements `reInitDashboardComponents`; harness asserts no-op for undeclared pages; only two files touched |
| R3 | Legacy import mis-maps a saved embed | Medium | Medium | Corpus tests + conversion report + read-only import + no destructive writes |
| R4 | Asset GC deletes something still referenced | Low | High | Refcount includes revisions; GC only at idle; harness asserting a revision restore after GC |
| R5 | Discord attachment identity becomes unresolvable (message deleted) | Medium | Medium | Explicit error + reattach action; never a silent broken send; the UI states the dependency before sending |
| R6 | Two role systems running in Phase 5 | Medium | High | Separate rows and tables; new cog flagged; "never double-grant/sweep" tests; old cog left loaded but unused for one release |
| R7 | Scope creep in phases 3–5 | High | Medium | Each phase's exit criteria gate the next; V2 explicitly out; action list limited to the four real actions |
| R8 | Performance work regresses correctness (patch misses an update) | Medium | High | Preview assertions compare the patched DOM against a full render of the same payload (string equality) on every harness case |
| R9 | Adding a 4th/5th phase of "polish" before stability | Medium | Medium | Priority order fixed: stability → correctness → performance → maintainability → UX → features |

---

## 15. Implementation checkpoint (the short version)

### Approved

Phase 0 bug fixes on the existing builder (lifecycle, idempotent init/destroy, IDB guards, non-blocking
init with injected identity, save/load data-loss fixes incl. author/footer icons + embed url + timestamp,
per-file upload check, markdown context fix, limits table + server validation endpoint).
Then Phase 1 (normalized model, one payload, keyed-patch preview, rail + inspector shell, dev
instrumentation), Phase 2 (assets + URL/upload + `attachment://` + library + legacy import), Phase 3
(components), Phase 4 (actions/roles), Phase 5 (saved messages/publish/update). No framework, no build
step, Components V2 out, shared contracts extended only additively.

### Changed from my proposal

Asset portability is now identity-based (`{channelId, messageId, attachmentId}` + fresh-URL resolution)
rather than "store the CDN URL", because Discord signs attachment URLs with a ~24 h expiry — the original
plan would have shipped a silent failure. Phase order follows yours (save/load merged with images before
components; saved messages last). The server-side roles/channels cache moves to Phase 1 (one deviation,
flagged in §7.1). Identity is injected with the page instead of fetched, and the "moderator 403" premise
is dropped as incorrect. The workspace is a **Message Builder** (content + embeds + components) with role
actions as one action category, not an embed builder bolted to reaction roles. Preview rework is sequenced
*after* editor churn (your principle 8), and markdown gets a two-step treatment (Phase 0 correctness
patch, Phase 1 full renderer).

### Deferred

Components V2 (all of it); chained/multi-actions beyond one action per component; role sets (Phase 4, only
if they stay within one file + one table); auto-update-on-save; reference-mode embeds; a server-side asset
host; drag-and-drop as the *only* reorder path (keyboard always ships); emoji keyword dictionary; mobile
layout beyond the segmented Edit/Preview toggle; the reaction-roles page retirement (Phase 5, after
verification).

### Risky files (and why each is touched)

| File | Why | Containment |
|---|---|---|
| `dashboard/static/js/embed-composer.js` | Phase 0 must fix save/load data loss, which lives here | Additive options + frozen exports + both existing harnesses green + payload equality |
| `dashboard/templates/base.html` | One include + page-module marker | Inert for all 40 other pages; harness asserts no-op |
| `dashboard/templates/manage/embedbuilder.html` | Phase 0 fixes live here; later it is replaced | Legacy page frozen after the switch; `?legacy=1` rollback |
| `dashboard/api/embedbuilder.py` | Additive validation + new v2 routes beside the existing send route | Existing routes unchanged; no response-shape changes |
| `dashboard/api/core.py` | TTL cache for `/guild/roles` + `/guild/channels` (Phase 1, flagged) | Same response shape; cache is bypassable; separate commit |
| `database.py` | New tables (Phase 2, 4, 5) | `CREATE TABLE IF NOT EXISTS` only, no migrations of existing data |
| `dashboard/app.py` | New v2 route + identity context value; later the switch | Additive until the switch, which is a one-line change |
| `main.py` | Loads `cogs/components.py` (Phase 5) | Behind a config flag; old cog unaffected |

Everything else in the repository stays untouched unless a dependency genuinely requires it — and if that
happens, it comes back to you as a question before the code is written.

### Migration

Old saved embeds keep loading: the importer reads `embed_templates` **read-only**, in memory, and maps
both shapes (bare embed dict from `app.py`; `{content, embeds}` from the API; plus `footer_icon`/
`author_icon` keys the cog already expects), defaulting every missing field to *absent* rather than empty
string, normalising all four colour forms, preserving unknown keys with a conversion report, and never
rewriting a row. Saving always writes a new v2 row with a revision. New tables are additive. Reaction-role
panels migrate per-panel, on request, without touching the live message.

### Rollback

Phase 0: revert the commit (registry inert, composer additions opt-in). Phase 1: delete the v2 route and
template — the live page was never modified, so users see no change. Phase 2: disable the library flag;
`embed_templates` and the legacy builder are untouched. Phase 3/4: disable the rail section/action editor
by flag; documents remain valid. Phase 5: flag the new cog off; old panels keep running; publications are
plain rows with nothing to undo. The route switch itself is reversible with `?legacy=1` for one release,
after which the legacy branch is deleted deliberately.

---

## 16. Items I still need from you before code

1. **`?legacy=1` rollout window** — one release (my recommendation) or two? This date also decides when the
   legacy page and the transitional composer path get deleted.
2. **Nav label** — keep "Embed Builder" (users know it) or rename to "Message Builder" at the Phase 3
   switch? The page will contain components either way.
3. **Asset GC policy** — my proposal: refcount over {draft, current document, every stored revision};
   never auto-evict referenced assets; surface cache size with a manual Clear. Confirm, or tell me to
   simplify to "never GC" (simpler, grows unbounded).
4. **Phase 5 legacy removal** — confirm that `cogs/reactionroles.py` may be retired (one release after the
   new executor is verified), or state that it must stay permanently.

Nothing else is blocking. On your approval I start with Phase 0, and I will report back with the measured
before/after numbers from §8.4 as the evidence that the original "heavy / needs a refresh" complaint is
actually fixed.
