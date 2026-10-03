# Embed Builder + Components Builder — redesign proposal (for review)

**Status:** proposal only. **No code has been changed for this document.** Working tree is
clean at `3874a0f`.

**What this is:** an audit of the current Embed Builder (`dashboard/templates/manage/embedbuilder.html`
+ `dashboard/static/js/embed-composer.js` + `dashboard/api/embedbuilder.py`), the Discord API
constraints that actually apply, the patterns mature builders use, and a phased plan to rebuild the
builder as a real dashboard feature — ending with an interactive-components builder and a reused,
revisioned save/load model.

Nothing here is implemented yet; section **9** lists the decisions I need from you before Phase 1.

---

## 0. TL;DR

1. **The “I have to refresh the page before it works” bug has a concrete cause.** The page’s JS lives
   in `{% block scripts %}`, which renders **outside** `#content-area`; the sidebar navigates with
   `hx-select="#content-area"`. So navigating back to the builder swaps in fresh markup but **never
   re-runs the page script** — the page is inert until a hard refresh. Nothing to do with timing,
   debounce or state.
2. **The preview’s cost problem is `innerHTML`.** Every keystroke serializes the whole message to an
   HTML string and replaces the preview subtree, so every `<img>` is destroyed and re-decoded. The fix
   is architecture (stable DOM + keyed patch), not a delay.
3. **One normalized model, one payload, three consumers.** Builder, preview, saved embed and send must
   all consume the same normalized document and the same `toDiscordPayload()` output. Today
   `cogs/embedbuilder.py` reads `footer_icon`/`author_icon` that the dashboard **never writes** — the
   exact drift this removes.
4. **Discord limits become data, defined once.** A single Python table
   (`utils/discord_limits.py`) is the authority, shipped to the client via
   `GET /api/embed-builder/limits`; the JS validator reads it instead of hard-coding numbers.
5. **Local uploads are possible — but only one way.** Discord embeds accept `http(s)` URLs and
   `attachment://filename` references, **nothing else**. Uploads must be sent as real attachments in
   the same (multipart) request and referenced as `attachment://name`; only `.jpg/.jpeg/.png/.webp/.gif`
   are allowed for embed media. A data: URI or a fake local URL silently fails.
6. **The bot’s banner is not part of a message.** The preview header shows the bot’s **avatar**
   (per-guild avatar → global avatar → default). A banner is shown on a profile card, never inside a
   message, so it is displayed as a *labeled asset*, not faked into the message.
7. **Markdown is context-sensitive in Discord.** Description and field *values* render full markdown;
   title, author name and footer text are **plain text**. The preview must apply the same per-field
   rules, otherwise it lies to you.
8. **Components: legacy rows are the right target for “embed + buttons/selects”.** Components V2
   (`IS_COMPONENTS_V2`) gives 40 components but **disables `content` and `embeds`** — i.e. it cannot
   carry an embed. It’s a separate lane, deliberately deferred.
9. **Buttons → actions → configuration, with many roles.** `Button → Action(type) → ActionConfig`,
   where role actions carry `roleIds[]` (plus optional named **Role Sets** for reuse). UI is chips
   (`[ Member × ][ Verified × ] [ + Add Role ]`), never a single-role dropdown.
10. **Save semantics: copy-on-load, append-only revisions.** Loading a saved embed or message copies
    by value; editing never mutates what a live message was sent with; “update the live message” is an
    explicit, separate action.

---

## 1. Research basis

### 1.1 Sources consulted (this session)

**Discord (official):**

- Message resource — embed object, embed limits, media/attachment URLs, message limit params:
  <https://docs.discord.com/developers/resources/message>
- API reference — uploading files, `files[n]` + `payload_json`, using attachments inside embeds,
  `attachment://` scheme, allowed embed media types:
  <https://docs.discord.com/developers/reference>
- Component reference — component types, action row, button, string select, limits, legacy vs
  Components V2 behaviour: <https://docs.discord.com/developers/components/reference>
- Emoji resource — emoji object, app-owned emoji (2 000, no `USE_EXTERNAL_EMOJIS`), 256 KiB cap:
  <https://docs.discord.com/developers/resources/emoji>
- User resource — `PATCH /users/@me` accepts `avatar` and `banner` (image data):
  <https://docs.discord.com/developers/resources/user>
- Markdown Text 101 (support article) — bold/italic/underline/strike/spoiler/headers/subtext/masked
  links/lists/code blocks/quotes:
  <https://support.discord.com/hc/en-us/articles/210298617>
- Role hierarchy (permissions topic, corroborated) — a bot can only manage roles **below** its highest
  role; Administrator does not bypass it; integration-managed roles can never be assigned by another
  app: <https://docs.discord.com/topics/permissions>

**Mature builders / dashboards (patterns only — no branding or UI copied):**

- **Discohook** — live preview + optional raw JSON, import/export, save/load of message payloads;
  webhook-first, no components → confirms “preview + save/load” as table stakes.
- **Carl-bot** — embeds as *living* messages (post once, edit in place later), embeds wired into the
  bot’s own systems (reaction roles) rather than existing in isolation.
- **Arcane** — reaction-role *types* (toggle, default, group-lock, persistent, reverse) and “pick one
  from a group”; roles are the unit of configuration, messages are containers.
- **mimBo’s Reaction Roles** — explicit behaviour modes (toggle / add-only / remove-only / pick-one),
  button-or-reaction per menu, “update posted menus in place”.
- **discord.builders / “Discord Webhook Builder” / Discord-Embed-Builder-V2** — drag-and-drop
  component rows, functional select dropdowns in preview, format toolbars in *every* text field,
  mention/timestamp/emoji insertion, character counters, send history (“load a sent message back into
  the editor”), JSON import/export.
- **Components-V2-era builders (incl. the legacy→V2 converter style tools)** — a **conversion report**
  that names every field that cannot be mapped exactly, instead of silently dropping data. That
  “nothing is silently dropped” principle is adopted below.

### 1.2 Verified Discord limits and behaviours (the constraint set)

| # | Constraint | Value | Source |
|---|---|---|---|
| 1 | Embeds per message | 10 | message resource |
| 2 | Embed title | 256 chars | embed limits |
| 3 | Embed description | 4 096 chars | embed limits |
| 4 | Embed fields | max 25 | embed limits |
| 5 | Field name / value | 256 / 1 024 chars | embed limits |
| 6 | Footer text / author name | 2 048 / 256 chars | embed limits |
| 7 | **Combined** across all embeds in one message (title+description+field name+field value+footer+author) | **6 000 chars** | embed limits |
| 8 | Embeds dedupe by `url` (only first shown) | — | embed limits |
| 9 | Embed media accepts | `http(s)` URLs **and** `attachment://filename` only | embed image / author / footer structures |
| 10 | Attachments usable inside an embed | `.jpg .jpeg .png .webp .gif` **only** | API reference, “Using Attachments within Embeds” |
| 11 | Attachment referenced in an embed | **hidden from the message body** (`attachments` = files *not* referenced) | message object |
| 12 | File uploads | multipart/form-data, `files[n]` + `payload_json`; the `n` is the id usable in `attachments[]` | API reference |
| 13 | Message content | 2 000 chars | message create params |
| 14 | Attachments per message | 10 | message create params |
| 15 | Per-file upload cap (free tier) | 20 MB, raised by Nitro / server boosts; treat as **runtime-verified**, not a constant | Discord upload-limit announcements; API default 20 MiB |
| 16 | Action row | 5 buttons **or** exactly 1 select | component reference |
| 17 | Legacy messages | **max 5 action rows** at top level | component reference |
| 18 | Components V2 (`IS_COMPONENTS_V2`) | up to 40 components, but **`content`/`embeds`/`poll`/`stickers` disabled** | component reference |
| 19 | Button label / custom_id / url | 80 / 100 / 512 chars | button structure |
| 20 | Button styles | 1 primary, 2 secondary, 3 success, 4 danger, 5 link (needs `url`, no `custom_id`), 6 premium | button styles |
| 21 | Button design guidance | ~34 chars with emoji / 38 without; one primary per group | button design guidelines |
| 22 | `custom_id` | 1–100 chars, **unique per message** | custom id section |
| 23 | String select: options / label / value / description / placeholder | 25 / 100 / 100 / 100 / 150 | string select |
| 24 | `min_values` / `max_values` | 0–25 (default 1); `min_values` must be ≥1 unless `required` is false (modal only) | string select |
| 25 | Select types | 3 string, 5 user, 6 role, 7 mentionable, 8 channel | component types |
| 26 | Select placement | alone in its own action row | action row |
| 27 | Custom emoji reachability | guild emoji needs bot guild membership (+ `USE_EXTERNAL_EMOJIS` on destination for foreign emoji); **app emojis need neither** (2 000/bot, 256 KiB each) | emoji resource |
| 28 | Bot avatar/banner | global via `PATCH /users/@me` (`avatar`, `banner`) **or** per-guild member avatar/banner via `PATCH /guilds/{id}/members/@me` | user / guild resources |
| 29 | Banners in messages | **not rendered** — profile card only | verified: no message surface exists for it |
| 30 | Markdown rendering surface | full markdown: **content, embed description, field values**; plain text: **embed title, author name, footer text** | Discord markdown docs + independent references |
| 31 | Role assignment | bot needs Manage Roles **and** the role must sit **below** the bot’s highest role; Administrator does not bypass; managed/integration roles can never be assigned by another app; `@everyone` is not assignable | permissions docs |
| 32 | Interaction ack | 3 s to acknowledge, else “This interaction failed” | interactions docs (existing bot behaviour) |

Two of these are load-bearing for the redesign and are easy to get wrong:

- **#9 + #10 + #11** are the entire local-image story. An uploaded file cannot appear inside an embed
  unless it is sent as an attachment in the same request and referenced as `attachment://name`, and only
  five image extensions are accepted for that. The upside of #11: a referenced attachment does **not**
  clutter the message with a raw file card — the correct answer and the nicest-looking one are the same.
- **#30** is why the current preview is optimistic in the wrong direction: it renders markdown in the
  footer and title where Discord shows asterisks, and lacks code/spoilers/links in the description
  where Discord shows formatting.

### 1.3 What can and cannot be previewed locally

| Previewable faithfully | Reason |
|---|---|
| Message content, description, field values markdown | Same renderer rules; implemented locally |
| Mentions, channels, roles, users | Real names/colors via `/api/guild/roles`, `/guild/channels`, `/guild/resolve-user` |
| Custom + unicode + app emojis | CDN image URLs are public; local storage/import state is known |
| Embed layout, colour stripe, fields grid, footer, author | Pure CSS mirror of Discord’s structure |
| Action rows / buttons / select menus (visual) | Rendered as Discord-style components; no interaction |
| Local uploaded images | `blob:`/`data:` preview (existing CSP-probe pipeline) |
| Discord timestamps `<t:…>` | Rendered from the same epoch using the reader’s locale — same as Discord’s client |
| Attachment images & file chips | Local blobs or remote URLs |

| Not previewable locally | Honest handling |
|---|---|
| Exact Discord typography/metrics, mobile layout | Approximate with tokens; document as “close, not pixel-identical” |
| Whether the channel grants `USE_EXTERNAL_EMOJIS` | Mark foreign emoji “may not render here”; check on send, surface Discord’s error |
| Whether a *member* will actually receive a role (hierarchy at click time) | Validate role position vs bot’s top role + warn; the click-time decision is the bot’s |
| Whether a role is managed by another integration | Detect `managed` flag at config time and block with an explanation |
| Link unfurls / bot-suppressed embeds | Out of scope; note in UI |
| Bot banner inside a message | Impossible by design — shown as a labeled asset instead |

### 1.4 Patterns adopted from mature builders (and what we deliberately reject)

**Adopted**

- Live, WYSIWYG preview that is the *same data* that gets sent (Discohook, discord.builders).
- Save/load of message payloads, plus raw JSON only as an **escape hatch**, never as the UI (Discohook,
  Discord-Embed-Builder-V2 “import/export”).
- “Living” saved configurations: reopen later, edit, republish, update the live message in place
  (Carl-bot).
- Explicit behaviour modes on a component’s action: add / remove / toggle / pick-one, booster-only,
  required-role, expiry (Arcane, mimBo).
- Role groups / sets as first-class reusable objects so one button can grant or revoke **many** roles
  (Arcane “group lock”, generic “role groups” in role bots).
- Visual component rows with drag/keyboard reorder and real dropdown rendering in preview
  (discord.builders, Discord-Embed-Builder-V2).
- Contextual inspector instead of one giant form; a structure tree that mirrors Discord’s own object
  tree (message → embed → fields / components → rows → components).
- Character counters + friendly limit explanations next to the field, at all times.
- “Nothing is silently dropped”: when importing legacy data, report what could not be mapped.

**Rejected**

- JSON editors as the primary interface (the user must not need to learn the payload shape).
- Webhook-only design (this is a bot dashboard; interactions, role actions and persistent views are the
  point).
- React/Vue/introducing a build step: the dashboard convention is hand-written ES5-safe JS with
  `window.NERO.pages.<name> = { init }` and Node harnesses. We stay with that.
- Components V2 containers in the first pass: they would replace embeds (limit #18), which breaks the
  requested flow. Deferred as an explicit, documented lane.
- A “fake” preview of anything Discord does not do (banners in messages, data-URI images, footer
  markdown, >5 rows, etc.).

### 1.5 The three findings that shaped this design

1. **Page lifecycle.** `{% block scripts %}` is emitted *after* `#content-area` (base.html line 554),
   while every sidebar link swaps only `#content-area` (`hx-select="#content-area"`). The builder’s
   init never runs again after an htmx navigation — the page is dead until a hard refresh. This
   single fact explains the reported “sometimes I need to refresh” symptom and it will keep causing it
   for **any** future version of this page until init is bound to the swap lifecycle.
2. **`attachment://` is the only bridge** between a local file and an embed image. It also happens to
   hide the raw attachment. So the local-upload system is: validate → preview locally → include in the
   multipart send → reference by filename.
3. **Payload-first preview.** If the preview renders *the payload that will be POSTed*
   (`toDiscordPayload(model)`), the three copies of the truth collapse into one, and the Node harnesses
   get something exact to assert on.

---

## 2. A — Current problems (evidence-based)

References are `file:line` where it matters. Grouped by root cause, ordered by user impact.

### P1 — Page lifecycle: the builder dies after any htmx navigation  *(“I need to refresh”)*

- `dashboard/templates/base.html`: `#content-area` is the swap target (`hx-target="#content-area"`,
  `hx-select="#content-area"`, `hx-swap="outerHTML"`), and `{% block scripts %}` sits **outside** it
  (line 554). `manage/embedbuilder.html` puts its entire page script (≈1 000 lines) in
  `{% block scripts %}`.
- Consequence: first load works; navigate to Tickets and back → fresh markup, **no init** → no editor,
  no preview, no listeners, dead buttons. Only a full reload fixes it.
- The `htmx:afterSwap` handler in `base.html` only re-executes `<script>` tags found *inside the swapped
  target*, which is never where these scripts are.
- Secondary effects: `document`-level listeners (undo/redo keybindings, emoji popover dismissal) from an
  earlier visit still exist and point at detached DOM; timers (draft save, history) keep firing against
  removed nodes; the IndexedDB connection stays open across pages.

### P2 — Preview cost: full `innerHTML` replacement on every keystroke

- `embed-composer.js` → `renderPreview()` ends with `box.innerHTML = html` — a complete
  destroy/rebuild of the whole message preview (avatar, text, every embed, every field, every image,
  every component row) on **each input event** (content, title, description, field, colour…).
- Every rebuild recreates `<img>` elements → the browser re-decodes images and re-issues requests; the
  preview flickers, loses scroll/selection context, and any in-flight image load is thrown away.
- No `loading="lazy"` / `decoding="async"` on embed images (attachments have them).
- Attachment preview URL resolution intentionally triggers a *second* full render when it resolves
  (`_queuePreviewRefresh()` → `renderAttachments(); renderPreview();`), so a single dropped image costs
  at least two whole-preview rebuilds.
- The editor side is heavier still: `mountEditor.render()` rebuilds **all ten** embed cards and rewires
  every listener on any structural change (add/delete/duplicate/reorder/load), and
  `content-visibility: auto` merely hides the paint cost of the collapsed ones.

### P3 — State architecture: mutable arrays, no store, no dirty tracking

- `state = { content, embeds: [ … ] }` is mutated **in place** by DOM handlers inside the shared
  editor (`embeds[i][field] = el.value`), while the preview reads the same objects. It works today only
  because every mutation is immediately followed by a manual `renderPreview()` call; there is no
  single place that knows what changed, so nothing can be optimized (or reasoned about) later.
- The colour field has two inputs with duplicated validation logic (picker + hex) that can drift.
- Undo/redo snapshots only `{ content, embeds }` (`pushHistory`, 350 ms debounce) — attachments,
  component configuration and emoji choices are outside history.
- There is no notion of “draft vs saved vs published”, therefore no way to answer “is this the version I
  sent?” — the root of the save/load requirements in §3.4.

### P4 — Missing image fields and dropped data (the “footer icon” class of bug)

- Editor fields are exactly: title, colour, author (name), footer (text), thumbnail URL, image URL,
  description, fields. **There is no author icon, author URL, footer icon, embed URL or timestamp.**
- `cleanEmbedForPayload()` only emits `author {name}`, `footer {text}`, `image/thumbnail {url}` —
  `author.icon_url`, `author.url`, `footer.icon_url` are silently dropped, and `embedFromApi()` drops
  them again on load. Save → load → save **loses data**.
- `cogs/embedbuilder.py:build_embed()` already reads `footer_icon` and `author_icon` from the stored
  dict — keys the dashboard never writes. Proof of the drift this redesign must end.
- **Local files can never fill an embed image slot today.** `/api/embedbuilder/send` uploads
  attachments, but nothing in the payload references them (`attachment://…` is never produced), so an
  uploaded image can only appear as a separate file under the message — not as the embed’s image,
  thumbnail or icon. This is exactly the gap §3 of your brief describes.
- No image error states: a broken image URL renders as an empty box, indistinguishable from “field
  empty” or “still loading”.

### P5 — Markdown fidelity: generic, not Discord’s

- `renderDiscordMarkup()` handles `**bold**`, `__underline__`, `*italic*`, `~~strike~~` plus the
  mention/emoji token classes, and nothing else. Missing (all real Discord features): inline code, code
  blocks, spoilers `||…||`, masked links `[text](url)`, headers `#`/`##`/`###`, subtext `-#`, lists,
  blockquotes `>` / `>>>`, escapes (`\*`), Discord timestamps `<t:…(:style)?>`, `@everyone`/`@here`.
- The regex pass runs **after** escaping over the whole string with no code shielding, so
  `` `**x**` `` renders bold-in-code (Discord would not) and a fenced block containing `*` corrupts the
  rest of the message. Nested/edge cases (`***bold italic***`, `**a *b* c**`) are approximate.
- The same renderer is applied to **every** field, including footer and title, where Discord renders
  plain text — so the preview shows formatting that will never appear (and hides the literal asterisks
  the user will actually see).
- Emoji-only sizing uses a Unicode regex that mis-handles ZWJ sequences and keycaps, and the mention
  token regex rewrites text inside code spans it should not touch.

### P6 — Validation: counts only, discovery at send time

- Present: embeds ≤10 (both client and server), fields ≤25 (client, with a toast), content ≤2 000
  (maxlength), attachments ≤10 and ≤25 MB total (client and server).
- Missing: title 256, description 4096 (maxlength exists on the textarea but not in the server),
  field name 256 / value 1024, footer 2 048, author 256, the **6 000-char combined budget**, URL
  validity for `url`/`image`/`thumbnail`/icons, image extension/size for embed-media uploads,
  duplicate/empty `custom_id`s, action-row composition rules, select option limits, role hierarchy
  feasibility (limit #31), managed-role assignment, and "nothing to send" edge cases.
- `/api/embedbuilder/send` validates embed *count and type* only; everything else surfaces as Discord’s
  `400 Invalid Form Body`, which the UI shows as one generic string.
- `MAX_TOTAL_ATTACHMENT_BYTES = 25 MB` is a stale constant (Discord’s free tier per-file cap is 20 MB,
  and it is per file, not only total), and there is no per-file check at all.

### P7 — No components builder; the only interactive path is single-role reaction roles

- `componentRowsHtml()` in `embed-composer.js` renders component rows for the **minigame builder’s**
  engine JSON — read-only mock buttons, no `custom_id`, no link buttons, no selects, no configuration.
- Interactive configuration today = `manage/reactionroles.html`: a flat form that mostly **generates
  slash-command text** for the operator to paste into Discord (`/reactionrole_create`, then one
  `/reactionrole_add` per button). Its button model is `{ label, role, emoji, color, booster, required,
  expiry }` — **one role per button** (`#btn-role` is a single input), exactly the coupling your brief
  rules out.
- Backend stores panels as `reaction_roles` (PK `message_id, role_id`) + `reaction_role_panels` +
  `reaction_role_expiry`, with persistent views rebuilt on `on_ready` (`restore_views`), and
  `custom_id = f"rr_{role_id}_{message_id}"`. The design is sound for single-role buttons and is a good
  foundation for a generalized version (see §3.5) — but it cannot express “this button removes four
  roles”.
- There is no saved-message concept: `rr_panels.buttons` is an unused JSON blob written by
  `dashboard/app.py` (`/api/save-rr-panel`), and nothing sends a saved panel to Discord.

### P8 — Persistence/limits drift and drafts

- Two template APIs exist (`/api/embedbuilder/template*` for `{content, embeds}` and
  `/api/save-embed-template` + `/api/embed-templates` in `app.py`), i.e. two stores with different
  shapes — a legacy of the “single embed template” era.
- The draft (`IndexedDB` db `nero_embedbuilder`) serializes **attachment `File` objects** on a 500 ms
  debounce while typing. With a 20 MB image that is a repeated structured-clone of megabytes per typing
  burst; it is also the reason `onblocked`/quota failures matter.
- `openIdb()` only handles `onsuccess`/`onerror`; a blocked upgrade (another tab on an old version) or a
  hung request leaves the promise pending forever, and `init()` **awaits** `restoreDraft()` before
  `renderAll()` — so the page can come up completely blank with no error. (Second, independent cause of
  “refresh fixes it”.)
- `loadBotIdentity()` calls `GET /api/botprofile/config`, which requires `LEVEL_ADMIN`. A moderator
  opening the builder gets a 403 → `botIdentity.avatar = null` → the preview header renders an empty
  circle forever (`<div class="eb-msg-avatar"></div>`). This is the “blank area until I upload
  something” half of the image complaint: the profile-picture in the message header is not the embed
  image at all.
- The guild banner *is* fetched (`get_live_bot_member()` → `guild_banner_url`) but never rendered
  anywhere, and its CDN path is an inference (documented as such in `utils/bot_profile.py`).

### P9 — UX: one long form, no structure

- Ten embed cards in one accordion, a separate attachments card, a separate send/save card, a sticky
  preview — the user scrolls between configuration and preview, and each accordion re-render rebuilds
  every card.
- No component tree, no drag/reorder beyond duplicate+delete, no per-field “what Discord allows here”
  affordance, no distinction between “draft”, “saved”, “sent”.
- Emoji tooling is good (frequently-used, guild, other servers, app-import) but is bound to the message
  content textarea only; it is unavailable for buttons/select options (which don’t exist yet).
- Role/entity pickers exist (`NeroSelect`) but the embed builder still asks for raw IDs for mentions
  (channel/role/user) and has no role picker for role-bearing components.

---

## 3. B — Recommended architecture

### 3.1 Layer diagram (single source of truth)

```
                    ┌──────────────────────────────────────────────────┐
   UI (DOM) ──────► │  Builder state  (one store per workspace)        │
                    │  { document: EmbedMessageDocument, ui: {…} }     │
                    └───────────────┬──────────────────────────────────┘
                                    │ pure
                    ┌───────────────▼──────────────────────────────────┐
                    │  Normalized model  (model.js)                    │
                    │  createEmbedMessageDocument / normalize / patch  │
                    └───────┬───────────────────────┬──────────────────┘
                            │                       │
            ┌───────────────▼─────────┐   ┌─────────▼──────────────────┐
            │ validate(model, limits) │   │ toDiscordPayload(model)    │  ← the ONE
            │ → issues[]              │   │ → { content, embeds,       │    payload used by
            └───────────────┬─────────┘   │     components, files[] }  │    preview AND send
                            │             └─────────┬──────────────────┘
                            │                       │
                    ┌───────▼───────────┐   ┌───────▼────────────────────┐
                    │ Validation UI     │   │ Preview renderer           │
                    │ (per-field, strip)│   │ renderDiscordMessage(payload)
                    └───────────────────┘   │ + mention/emoji context    │
                                            └────────────────────────────┘
                            │
                    ┌───────▼────────────────────────────────────────────┐
                    │ Persistence: drafts (IndexedDB) · saved embeds /   │
                    │ messages (server, revisioned) · publish targets    │
                    └────────────────────────────────────────────────────┘
```

Rules that keep it honest:

- **The preview never reads the builder state directly.** It reads the payload
  (`toDiscordPayload(model)`), plus a read-only *presentation context* (bot identity, mention lookups,
  emoji resolution, local-file preview URLs). If the payload is wrong, the preview is wrong — and the
  send is wrong too, in the same way. No third copy can drift.
- **Limits are data, not literals.** `utils/discord_limits.py` owns every number and the friendly
  message per violation; `GET /api/embed-builder/limits` ships the same table to the browser, so the
  client-side validator has no hard-coded Discord constants.
- **Validation runs in both places, from one rule set.** Client = instant feedback on the same numbers;
  server = authoritative gate before the Discord call (the client can be stale or bypassed).

### 3.2 Module map (files, responsibilities, target size)

| File | Responsibility | Notes |
|---|---|---|
| `dashboard/static/js/embed/model.js` | `blankEmbed`, `blankMessageDocument`, normalization (`fromWire`, `toWire`), immutable patch helpers, `toDiscordPayload`, `cloneDocument`, content hash for dirty checks | pure, no DOM |
| `dashboard/static/js/embed/discord-markdown.js` | Context-aware markup renderer (`renderMarkup(text, { context })`), token parser (mentions/emoji/timestamps), escape handling, code shielding, emoji-only detection | pure; replaces the regex block in `embed-composer.js` |
| `dashboard/static/js/embed/validate.js` | `validate(model, limits, ctx)` → `Issue[]`; path-mapped; severities; message catalog lookups | pure; server mirrors the rules |
| `dashboard/static/js/embed/preview.js` | `renderPreview(mount, payload, ctx)` with a **stable DOM skeleton + keyed patch**, image registry, per-image states (loading/ok/broken/blocked) | replaces `renderPreview` in `embed-composer.js` |
| `dashboard/static/js/embed/store.js` | `createStore(reducer, initial)`, `dispatch(action)`, `subscribe(fn)`, history (undo/redo), dirty flag, persist scheduling | ~150 lines |
| `dashboard/static/js/embed/components/*.js` | `ImageField`, `EmojiPicker`, `RolePicker`, `Repeater`, `FieldEditor`, `ColorField`, `ButtonCard`, `SelectCard`, `ActionEditor` | one file per reusable control |
| `dashboard/static/js/embed/embed-builder.js` | Embed workspace page: store wiring, structure rail, inspector, preview, save/load | `window.NERO.pages.embedBuilder` |
| `dashboard/static/js/embed/components-builder.js` | Components workspace page | `window.NERO.pages.componentsBuilder` |
| `dashboard/static/js/embed/library.js` | Saved embeds / saved messages library views | shared by both pages |
| `dashboard/static/js/embed-composer.js` | **Kept** for the minigame builder, re-pointed at the new pure modules, public API preserved | no consumer breakage |
| `dashboard/api/embed_builder.py` | REST: limits, saved embeds (+revisions), saved messages, publish/send, asset capture, drafts (optional) | follows `api/tickets.py` conventions |
| `utils/embed_schema.py` | Server-side model ↔ wire conversion, validation gate, payload builder | the authority before Discord |
| `utils/discord_limits.py` | Every limit + friendly message + capability matrix | single source of truth |
| `cogs/components.py` *(Phase 6)* | Runtime: persistent views, `custom_id` → component config, action execution | generalized from `cogs/reactionroles.py` |

Why not one big `embed-composer.js`: it already has 2 consumers and 811 lines. The extraction rule from
`DASHBOARD_ARCHITECTURE_AUDIT.md` §2 (“extract on the second consumer”) is satisfied here by *both*
consumers needing the same model/markdown/validation modules; the page-specific chrome stays per page.

### 3.3 State management

```js
// One store per workspace. State is a plain, JSON-serializable document + UI slice.
const store = createStore({
  document: blankMessageDocument(),      // the model (see §6)
  ui: {
    selectedNodeId: null,                // structure rail selection
    mode: 'embeds',                      // 'embeds' | 'components'
    previewDevice: 'desktop',
    issues: [],                          // from validate()
    save: { status: 'dirty', lastSavedAt: null, savedId: null, revision: null },
  },
  history: { past: [], future: [], limit: 60 },
});
```

- **Actions are the only mutators** (`embed/setTitle`, `field/add`, `field/move`, `button/setStyle`,
  `action/addRole`, `document/loadSaved`, `ui/selectNode`, …). Reducers are pure; patching is
  structural (spread) so subscribers can cheaply detect which slice changed.
- **Selectors keep renders narrow**: the preview subscribes to `document`; a field editor subscribes to
  its own node; the validation strip subscribes to `issues`.
- **Undo/redo** covers everything JSON-serializable (embeds, components, actions, content) — file blobs
  are excluded by design but their *references* (filename, mode, validation state) are included, so undo
  never produces a dangling image reference.
- **Dirty tracking**: `hash(document)` vs `hash(lastSavedDocument)` → `ui.save.status` ∈
  `clean | dirty | saving | error`. Drives the top-bar indicator, “unsaved changes” guards, and the
  publish flow.
- **Drafts**: persisted to IndexedDB, but only (a) on idle (≥1.5 s after the last mutation) and
  (b) on `visibilitychange`/`pagehide`. Attachments are stored as a separate object store keyed by
  asset id, written **once when the file is added** and never re-cloned on typing. If storage fails
  (quota/blocked), the UI says so once and keeps working in memory.
- **Page lifecycle (the P1 fix)**: every page exposes `window.NERO.pages.<name> = { init(root), destroy() }`;
  `base.html`'s `htmx:afterSwap` calls the initializer after every swap and the previous page’s
  `destroy()` before it. `init()` must be idempotent and tolerant of being called with a
  partially-hydrated DOM; `destroy()` removes document-level listeners and timers.

### 3.4 Preview architecture

```js
// 1. Build what will be sent — once.
const payload  = toDiscordPayload(model);       // {content, embeds, components, files}
// 2. Describe how to present the parts Discord will resolve for us.
const ctx = {
  bot:   { name, globalAvatar, guildAvatar, appBadge: true },
  looks: { roles: roleMap, channels: channelMap, users: userCache, onUserResolve },
  emoji: { resolve(id) → cdnUrl, isPorted: id => appEmojiIds.has(id) },
  files: { previewUrl(assetId) → blob:/data:/null + reason },
  now:   () => Date.now(),                      // for <t:…> tokens
};
// 3. Render into a stable skeleton; patch only what changed.
renderPreview(mountEl, payload, ctx);
```

Implementation notes:

- **Stable skeleton, keyed patch.** The renderer writes the outer chrome (avatar/name/APP/time) and one
  container per embed and per action row **once**, keyed by a stable node id, then patches text nodes and
  attributes on subsequent calls. Images are keyed by URL: an unchanged `src` is never reassigned, so no
  re-decode, no flicker, no request churn.
- **Per-image state machine** (reusing the spirit of the attachment work already in the repo):
  `idle → loading → ok | broken(reason) | blocked(reason)`. Broken images show a labelled placeholder
  (with the URL and a retry), never a silent empty box.
- **Deterministic rendering.** No timers inside render; the `<t:…>` renderer takes `now` from context so
  outputs are testable. `fmtPreviewTime()` (the message header timestamp) is computed once per mount,
  not per render.
- **Interaction affordances**: clicking a button/select/field in the preview dispatches
  `ui/selectNode` (click-to-edit); the preview never mutates state itself.
- **What the preview renders from the payload, not from UI state**: everything. A Node harness asserts
  `renderPreview(toDiscordPayload(model))` for golden documents, so “the preview lies” becomes a test
  failure rather than a bug report.
- **Degradation path**: if `blob:` previews are blocked (existing CSP probe), images fall back to
  `data:` ≤5 MB, then to an explicit “no preview — will still be sent” tile. Keep the existing probe,
  WeakMap cache and `NO_PREVIEW_HINT` text (they are good) — just move them behind `files.previewUrl`.

### 3.5 Component / action architecture

```
Component (Row child)                     Action                         Configuration
──────────────────────                    ──────                         ─────────────
Button { style, label, emoji,             role.add          → { roleIds: [...] , source?: 'roleset:<id>' }
         disabled, customId }             role.remove       → { roleIds: [...] }
        └─ action: ActionConfig           role.toggle       → { roleIds: [...], exclusiveGroup?: 'group-a',
Select { kind, placeholder,                          maxRoles?: n, requireConfirmation?: bool,
         minValues, maxValues,                         boosterOnly?: bool, requiredRoleId?: id,
         disabled, options[] }                         expiresAfterDays?: n }
  └─ SelectOption { label, value,         url.open          → { url }
       description?, emoji?, default? }   message.reply     → { templateId, ephemeral: bool }
                                          custom            → { handlerKey, params }   (future-proof slot)
```

- A **component** owns presentation; an **action** owns behaviour; **configuration** is data. The UI
  never calls backend operations directly, so new behaviours (DM the user, open a ticket, grant a
  temporary role) are added as new action types with an editor panel — without touching button/select
  UI, the preview, or the persistence layer.
- **Many roles per action is the default shape**: `roleIds: string[]`. The UI renders chips with
  remove buttons and `[ + Add Role ]`; a **Role Set** (`roleSets[]`) is a named, reusable group
  (“Verification”, “Events”) that can be attached/detached as one chip — the Arcane-style “role group”
  pattern, which is how mature dashboards keep repeated multi-role configurations manageable.
- **Runtime mapping (bot side)**: generalize the existing, proven pattern from `cogs/reactionroles.py`
  (persistent `discord.ui.View`, `timeout=None`, rebuilt on `on_ready`) rather than the current
  `custom_id = rr_<role_id>_<message_id>` scheme. New scheme: `custom_id = c:<messageConfigId>:<componentId>`
  (documented, unique per message, well under 100 chars). The bot resolves the component → action config
  from the store, so behaviour changes require no re-send and one config can carry many roles.
- **Policies that already exist stay first-class** (they’re good): `exclusive` (pick-one),
  `maxRoles`, `requireConfirmation`, `boosterOnly`, `requiredRoleId`, `expiresAfterDays` — modelled as
  action configuration/policies, with the current `reaction_role_expiry` sentinel mechanism preserved
  (including the per-message keying fix that is already in the tree).

### 3.6 Save / load architecture (embeds, messages, publish targets)

Three distinct objects, matching your §14 naming:

| Object | Meaning | Stored | Mutable after save |
|---|---|---|---|
| **Draft** | work in progress, not named | IndexedDB (client) | yes, continuously |
| **Saved Embed** | a reusable embed *definition* (title/description/fields/images/footer/author), no components | `saved_embeds` (+ `saved_embed_revisions`) | new revision on save |
| **Saved Message** | a complete Discord message: embed snapshot + content + components + actions + policies | `saved_messages` (+ `saved_message_revisions`), `message_components`, `message_actions`, `role_sets` | new revision on save |
| **Publication** | which channel/message a saved message was actually sent to | `message_publications` | edited in place / republished |

**Decision: copy-on-load with append-only revisions.**

- Loading a saved embed into either builder **copies by value** and remembers
  `source: { id, revision }` for provenance (“edited from Saved Embed › Rules v3”) and for a
  “compare/restore” affordance.
- A Saved Message stores its own snapshot of the embed. Editing the reusable embed later **never**
  changes an already-saved or already-sent message. (A future explicit `linkToEmbed: true` mode can
  opt into references, but then the UI must show “N messages use this embed” and warn before saving a
  new revision.)
- Saving never overwrites history: each save appends a revision row and updates `current_revision`.
  Reverting = loading an older revision into the editor and saving it as the new head.
- **Sending vs updating is explicit.** “Send” creates a new message and records a publication.
  “Update the live message” edits the recorded `message_id` (Discord edit keeps components working,
  because persistent views key off `custom_id`, not message identity). Accidental edits to live panels
  are impossible without choosing Update.

**Why copy is the safe default:** the failure mode of references is silent breakage of live, member-facing
panels (change one shared embed → five servers’ rule panels change). The failure mode of copies is
duplication, which is visible, cheap to fix (a “pull latest from source embed” button), and never
affects what members already see.

### 3.7 Server architecture

- **Routes (new `dashboard/api/embed_builder.py`, blueprint `api_bp`, following `api/tickets.py`):**

| Method + path | Purpose | Level |
|---|---|---|
| `GET /api/embed-builder/limits` | the limits/capability table (§7.1) | moderator |
| `GET /api/embed-builder/bot-identity` | name, global avatar, guild avatar, banner, bio (best-effort, cached 5 min); never admin-gated | moderator |
| `GET/POST /api/embed-builder/embeds` | list/create saved embeds (name, tags, revision) | moderator / admin |
| `GET /api/embed-builder/embeds/<id>?rev=` | fetch a revision | moderator |
| `DELETE /api/embed-builder/embeds/<id>` | archive (soft) | admin |
| `GET/POST /api/embed-builder/messages` | saved messages (embed snapshot + components + actions) | moderator / admin |
| `POST /api/embed-builder/messages/<id>/validate` | authoritative validation of a draft (no send) | admin |
| `POST /api/embed-builder/messages/<id>/publish` | send (multipart: payload + files) → records publication | admin |
| `POST /api/embed-builder/messages/<id>/republish` | edit the recorded live message | admin |
| `POST /api/embed-builder/assets/capture` | after a send, map local uploads → CDN URLs for durability | admin |
| `GET /api/embed-builder/role-sets` `POST` | reusable role groups | admin |
| `GET /api/embed-builder/emoji` | unified emoji payload (unicode sets stay client-side; guild + external + app emojis, with reachability flags) | moderator |

- **Legacy compatibility, read-only:** `/api/embedbuilder/*` keeps working (templates, app-emoji import)
  and the new library imports `embed_templates` rows on demand (“Import legacy template…”, with a
  conversion report). `cogs/embedbuilder.py` keeps its slash commands; `build_embed()` gains the
  missing keys it already expects.
- **Publish path** (multipart, one request):
  `payload_json` = `toDiscordPayload(model)` with `attachment://` URLs for upload-mode assets,
  `files[n]` = the local files, `attachments[n]` = `{ id: n, filename, description? }`.
  Response → record `message_id`, `channel_id`, and the resolved CDN URLs per asset (for capture).
- **Validation gate**: `utils/embed_schema.validate_payload(payload)` re-checks every limit from
  `utils/discord_limits.py` **before** the HTTP call and returns structured issues
  (`path`, `code`, `message`). Discord’s own 400 is parsed for `errors.<path>` and surfaced
  field-mapped; it never replaces our own checks, it corroborates them.
- **Storage (new tables, all additive; nothing existing is dropped):**

```sql
saved_embeds(id, guild_id, name, tags, current_revision, created_by, created_at, archived_at)
saved_embed_revisions(embed_id, revision, data_json, created_by, created_at, PRIMARY KEY(embed_id, revision))

saved_messages(id, guild_id, name, current_revision, created_by, created_at, archived_at)
saved_message_revisions(message_id, revision, data_json, created_by, created_at,
                        PRIMARY KEY(message_id, revision))

message_components(message_id, revision, row_index, component_index, type, data_json, PRIMARY KEY(...))
message_actions(message_id, revision, component_ref, action_type, config_json, PRIMARY KEY(...))
role_sets(id, guild_id, name, role_ids_json, created_at)
message_publications(id, message_id, revision, channel_id, discord_message_id,
                     published_at, published_by, last_edited_at)
```

`data_json` is the normalized wire document from §6.3 — the same shape the payload builder consumes, so
what is stored is exactly what is sent (plus provenance fields).

---

## 4. C — UX flow (complete journey)

```
①  Create Embed            /embed-builder
   └─ Structure rail: Message → Embed 1 → (Author, Content, Fields, Images, Footer)
      Inspector edits the selected node; Preview updates live; issues appear inline.

②  Preview                 same screen, right pane (desktop) / toggle (mobile)
   └─ WYSIWYG for content, description, fields, images, footer, colour.
      Broken images/markdown-only-context problems are called out in place.

③  Save                    “Save” → name it → Saved Embed (revision 1)
   └─ Optional: “Save as draft” keeps it local; “Save as new embed” always allowed.
      The status chip reads: Draft · Unsaved/Unsaved changes/Saved 2m ago.

④  Load                    Library (Saved Embeds) → Load
   └─ Copies into the builder, provenance shown: “from Rules v3” + “Pull latest”.

⑤  Components              /components-builder (separate page, separate store)
   └─ “Load Saved Embed” → pick Rules v3 → embed snapshot appears in the workspace,
      now with a Components section: Rows → Buttons / Selects.

⑥  Configure Actions       select a button/option → Actions inspector
   └─ Add Role / Remove Role / Toggle Role / Open URL / (future actions);
      roles chosen as chips (one or many), optional Role Set, optional policy panel
      (pick-one group, max roles, require confirmation, booster-only, required role, expiry).

⑦  Preview                the whole message: embed + action rows, exactly as Discord shows it
   └─ Validation strip shows live counts, hierarchy warnings, custom_id collisions, etc.

⑧  Save Message            Saved Message (revision 1): embed snapshot + components + actions
   └─ Reopen later from the library; edit; revisions grow; nothing sent is affected.

⑨  Send                    choose channel → “Send” (records a publication)
   └─ Local images travel as attachments referenced by attachment://; CDN URLs are captured
      afterwards so the config stays sendable from any device.
      Later: “Update the live message” edits that message; “Send as new” posts a copy.

⟲  Reopen later             Library → Saved Messages → Load → edit → Save (new revision) →
                            Publish/Update. Everything stays reproducible.
```

Cross-cutting states the UI must always answer:

- **Empty**: “Start with a title or a description” (not a blank grey box).
- **Loading**: skeleton chrome in the preview with the real avatar as soon as it resolves (never a blank
  circle; a fallback generated avatar is used if the bot’s avatar is genuinely unavailable).
- **Partial failure**: mentions unresolved → raw IDs; emoji unreachable → token shown with a warning
  chip; image broken → labelled placeholder.
- **Dirty/guards**: leaving with unsaved changes prompts; publishing requires a clean, validated document.
- **Errors**: field-level red text + the strip’s summary; Discord’s own error text is shown verbatim
  under the field it names.

---

## 5. D — UI wireframe

### 5.1 Workspace shell (both builders)

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ Embed Builder            Draft · saved 2m ago          ↩ ↪   [Preview]  [Save ▾] [Send]│
├──────────────────────┬────────────────────────────────────┬──────────────────────────┤
│ STRUCTURE            │ INSPECTOR                          │ DISCORD PREVIEW          │
│                      │                                    │                          │
│ ▾ Message            │  ┌ Embed · Rules ────────────────┐  │  ┌────────────────────┐  │
│   ▾ Embed 1 ⚠2      │  │ Title        [______________] │  │  │ Nero  APP   Today   │  │
│     Author           │  │ URL          [https://______] │  │  │ ┌────────────────┐ │  │
│     Content          │  │ Description  [____________]  │  │  │ │ ▍ Rules        │ │  │
│     Fields (3)       │  │              [ 1234 / 4096 ]  │  │  │ │   **Welcome**  │ │  │
│       • Rule 1       │  │ Colour       [■ #7c5cbf] [▾]  │  │  │ │   …            │ │  │
│       • Rule 2       │  │ Timestamp    [☑ now] [……]     │  │  │ │ Name  Value    │ │  │
│     Images           │  │ Fields       [+ Add Field]    │  │  │ │ footer         │ │  │
│       Image      ⚠1  │  └────────────────────────────────┘  │  │ └────────────────┘ │  │
│       Thumbnail      │                                    │  │ [ Button ] [ Button]│
│     Footer           │                                    │  └────────────────────┘  │
│ ▾ Components         │                                    │  Desktop │ Mobile       │
│   ▾ Row 1            │                                    │                          │
│     • Button “Rules” │                                    │  click any element →     │
│     • Select “Roles” │                                    │  selects its node        │
│   + Add Row          │                                    │                          │
├──────────────────────┴────────────────────────────────────┴──────────────────────────┤
│ ⚠ 2 warnings · ⛔ 1 error      “3 of 5 rows used” · “Field 2 value is 1 210/1 024”    │
│                                              [Jump to first issue]   [Copy JSON]     │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

Why this shape (rather than the “left form / right preview” default):

- The **structure rail** is a 1:1 map of Discord’s own object tree, so nothing is hidden and nothing is
  a “mystery field”; it doubles as an error list (badges) and as navigation.
- The **inspector** shows only the selected node’s fields — this is what keeps the UI from becoming the
  “giant complicated form” your brief forbids. Adding a button never grows the *inspector*; it grows the
  *rail*.
- The **preview is interactive and bidirectional**: clicking an element selects it; hovering a node
  highlights its element. This is how discord.builders and modern component editors stay comprehensible.
- The **validation strip** is always present but quiet (a single line when clean).
- On narrow screens: rail collapses to a dropdown (“Jump to…”) and preview moves behind a
  Preview/Edit toggle (the same store, no logic forks).

### 5.2 Embed inspector — Images group (the local-upload system, field by field)

```
Images
┌──────────────────────────────────────────────────────────────────────────┐
│ IMAGE (large, under the embed)                          Disc size: 1024×512│
│ ┌──────────────┬──────────────┐                                          │
│ │ ● URL        │   Upload     │   ← one control, two modes, per field    │
│ └──────────────┴──────────────┘                                          │
│ [ https://example.com/banner.png                        ] [ Test ] [ ✕ ] │
│   ⤷ ✅ loads · 1280×640 · image/png · will show immediately              │
│                                                                          │
│ Thumbnail   [ URL | Upload ]  ⤷ “No image — the embed shows text only”   │
│ Footer icon [ URL | Upload ]  ⤷ ⚠ “URL didn’t load (404) — fix or remove”│
│ Author icon [ URL | Upload ]  ⤷ ⤒ “Drop a file, or paste an image”       │
└──────────────────────────────────────────────────────────────────────────┘
```

Upload mode specifics:

```
[ ⤒ Drop a file, click to browse, or paste from clipboard ]
   accepted: .png .jpg .jpeg .webp .gif   ·   ≤ 20 MB (Discord, free tier)

┌───────────────┐  uploaded-1.png        ✅ ready · 412 KB · 1200×630
│  [preview]    │  Animated GIF  → plays in preview and in Discord
└───────────────┘  [Replace] [Remove]

┌───────────────┐  rules.pdf            ⛔ Discord cannot use .pdf inside an
│   📄          │                          embed — use a URL, or attach it as
└───────────────┘                          a normal file instead.  [Details]
```

Rules the UI enforces here (verified in §1.2): accepted types only, one file per field, dedupe by
content hash (the same file used as image *and* thumbnail is uploaded once and referenced twice),
filename sanitisation, per-file size limit, clear state for each field
(`empty / testing / ok / broken / unsupported / too-large / uploading`), remove & replace, drag-and-drop,
clipboard paste, and an explicit badge when a saved config contains a local file that is not present in
this browser (`⚠ 1 local image missing — re-attach to send`).

### 5.3 Components mode (rows, buttons, selects)

```
STRUCTURE                              INSPECTOR — Button “Remove Roles”
▾ Row 1  (3/5 buttons)                 ┌──────────────────────────────────────────────┐
│  • Button  “Verify”       (success)  │ Label   [ Remove Roles            ]  74/80   │
│  • Button  “Remove Roles” (danger)   │ Style   [ Primary ▾ ]                        │
│  • Button  “Docs”         (link)     │ Emoji   [ 🎮 ▾ ]  (Unicode · Server · App)   │
+ Add Button                           │ Disabled [ ]                                 │
▾ Row 2  (select)                      │ Behaviour  ● Interactive  ○ Link (URL)       │
│  • Select  “Choose a role” (4 opts)  │                                              │
+ Add Row   + Add Select                │ ACTION ───────────────────────────────────── │
                                        │ Type: [ Remove Roles ▾ ]                     │
                                        │ Roles: [ Member × ] [ Verified × ] [ + Add ] │
                                        │        ⚠ “Verified” is above my role — the   │
                                        │          bot cannot assign it. [Why?]        │
                                        │ Options: [ ] Pick-one group                  │
                                        │          [ ] Require confirmation            │
                                        │          [ 3 ] Max roles  [ 0 ] Expiry days  │
                                        └──────────────────────────────────────────────┘
```

Select menu inspector (own screen, options visually separated):

```
Select “Choose a role”                                   Custom ID: sel_roles (auto)
┌ General ───────────────────────────────────────────────────────────────────┐
│ Placeholder  [ Choose your roles… ]  138/150   Min [0] Max [1]  Disabled [ ]│
└────────────────────────────────────────────────────────────────────────────┘
OPTIONS  (4 / 25)                              ⠿ drag to reorder
┌───────────────────────────────────────────────────────────────────────────┐
│ ⠿ 1   Label [ Gaming ]  Value [ gaming ]  Emoji [ 🎮 ]                     │
│       Description [ Access to game nights ]                               │
│       Action: [ Toggle Roles ▾ ]  Roles: [ Gamer × ] [ Events × ] [ + ]    │
│       [ Remove option ]                                     [ Duplicate ] │
├───────────────────────────────────────────────────────────────────────────┤
│ ⠿ 2   Label [ News ]    Value [ news   ]  Emoji [ 📰 ]                     │
│       Description [ … ]                                                    │
│       Action: [ Add Roles ▾ ]     Roles: [ News × ] [ + ]                  │
└───────────────────────────────────────────────────────────────────────────┘
[ + Add Option ]                                              Discord: max 25
```

Preview of components matches Discord's chrome: buttons in rows with the correct styles/emoji, selects as
a Discord-style select with the real placeholder and option list (a native-like dropdown, not a fake
button), disabled states dimmed, link buttons styled primary-secondary with an external-link hint, and
the design guidelines surfaced as *warnings* (e.g. “two primary buttons in this row — Discord recommends
one”).

### 5.4 Library screens

```
Saved Embeds                                   Saved Messages
┌───────────────────────────────────────┐      ┌───────────────────────────────────────┐
│ [ Search… ]   [ All ▾ ]  [ + New ]    │      │ [ Search… ]                  [ + New ]│
│ ───────────────────────────────────── │      │ ───────────────────────────────────── │
│ ▍Rules            v3 · 2 fields · 🖼1 │      │ ▍Rules + Roles     v4 · 2 rows · 5 roles│
│    Used by 2 messages · edited 2d ago │      │    Sent to #rules (msg 123…) · 2d ago │
│    [Load] [Duplicate] [Revisions] [🗑]│      │    [Load] [Update live] [Revisions] [🗑]│
│ ▍Welcome          v1 · 1 field        │      │ ▍Ticket Panel      v1 · 1 select      │
│ ▍Giveaway Card    v7 · 4 fields       │      │ ▍Boost Perks       v2 · 3 buttons     │
└───────────────────────────────────────┘      └───────────────────────────────────────┘
```

Revisions panel (both): a read-only list with diffs (“v3: footer text changed, image replaced”), plus
`[Restore as new revision]` and `[Export JSON]`. Import accepts the same JSON shape (and legacy
`embed_templates`) with a conversion report.

---

## 6. E — Data model

### 6.1 Core TypeScript interfaces

```ts
// ─────────────────────────── assets ───────────────────────────
/** How an image slot is filled. `upload` assets are sent as real attachments
 *  and referenced as `attachment://<filename>` — Discord accepts nothing else. */
export type ImageAsset =
  | { kind: 'url'; url: string; alt?: string }
  | {
      kind: 'upload';
      assetId: string;          // id of the local Blob in the asset store
      filename: string;         // final filename used for attachment://
      mime: 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif';
      bytes: number;
      width?: number; height?: number;
      /** Set after a successful publish: the CDN URL Discord returned for this
       *  attachment, so the config keeps working without the local file. */
      capturedUrl?: string;
    };

export interface EmojiRef {
  /** Unicode emoji (literal), or a custom emoji token. */
  kind: 'unicode' | 'custom';
  name?: string;                // custom: emoji name
  id?: string;                  // custom: snowflake
  animated?: boolean;
  /** Provenance for UI badges; never sent to Discord. */
  source?: 'guild' | 'external' | 'application' | 'imported';
}

// ─────────────────────────── embed ────────────────────────────
export interface EmbedField {
  id: string;                   // stable UI id (never sent)
  name: string;                 // ≤ 256
  value: string;                // ≤ 1024, full markdown
  inline: boolean;
}

export interface EmbedAuthor {
  name: string;                 // ≤ 256, plain text
  url?: string;                 // http(s) only
  icon?: ImageAsset;
}

export interface EmbedFooter {
  text: string;                 // ≤ 2048, plain text
  icon?: ImageAsset;
}

/** Normalized embed document — the single source of truth. */
export interface EmbedDocument {
  id: string;                   // stable UI id
  title?: string;               // ≤ 256, plain text (no markdown)
  url?: string;                 // title link, http(s)
  description?: string;         // ≤ 4096, full markdown
  color?: number;               // 0xRRGGBB (integer, as Discord expects)
  author?: EmbedAuthor;
  thumbnail?: ImageAsset;
  image?: ImageAsset;
  footer?: EmbedFooter;
  timestamp?: string;           // ISO8601, or 'now' at publish time
  fields: EmbedField[];         // ≤ 25
}

// ─────────────────────── message + components ─────────────────
export type ButtonStyle = 'primary' | 'secondary' | 'success' | 'danger' | 'link';

export interface ButtonComponent {
  id: string;                   // stable UI id
  type: 'button';
  label?: string;               // ≤ 80 (guidance: ~34 with emoji)
  emoji?: EmojiRef;
  style: ButtonStyle;
  disabled: boolean;
  /** interactive (custom_id) vs link (url) — mutually exclusive. */
  behaviour:
    | { mode: 'interactive'; customId: string }   // auto-generated, unique per message
    | { mode: 'link'; url: string };              // ≤ 512
  action?: ActionConfig;        // required for interactive buttons
}

export interface SelectOption {
  id: string;
  label: string;                // ≤ 100
  value: string;                // ≤ 100, unique within the select
  description?: string;         // ≤ 100
  emoji?: EmojiRef;
  default?: boolean;
  action?: ActionConfig;
}

export interface SelectComponent {
  id: string;
  type: 'select';
  kind: 'string' | 'user' | 'role' | 'mentionable' | 'channel';  // 3,5,6,7,8
  customId: string;             // ≤ 100
  placeholder?: string;         // ≤ 150
  minValues: number;            // 0–25
  maxValues: number;            // 1–25, ≥ minValues
  disabled: boolean;
  options: SelectOption[];      // string kind only; ≤ 25
  action?: ActionConfig;        // for non-string kinds (no options to hang it on)
}

export type RowChild = ButtonComponent | SelectComponent;

export interface ComponentRow {
  id: string;
  children: RowChild[];         // 1–5 buttons, OR exactly 1 select
}

// ─────────────────────────── actions ──────────────────────────
export type RoleActionType = 'role.add' | 'role.remove' | 'role.toggle';

export interface RoleActionConfig {
  type: RoleActionType;
  roleIds: string[];            // MANY roles per action — never a single role
  roleSetIds?: string[];        // reusable named groups
  /** Behaviour policies (mirror the existing reaction-role settings). */
  exclusiveGroup?: string;      // "pick one from this group"
  maxRoles?: number;            // 0 = unlimited
  requireConfirmation?: boolean;
  boosterOnly?: boolean;
  requiredRoleId?: string;
  expiresAfterDays?: number;    // 0 = never
}

export interface UrlActionConfig { type: 'url.open'; url: string }
export interface ReplyActionConfig { type: 'message.reply'; templateId: string; ephemeral: boolean }
export interface CustomActionConfig { type: 'custom'; handlerKey: string; params: Record<string, unknown> }

export type ActionConfig =
  | RoleActionConfig | UrlActionConfig | ReplyActionConfig | CustomActionConfig;

export interface RoleSet { id: string; name: string; roleIds: string[]; }

// ───────────────────── the whole message document ─────────────
export interface MessageDocument {
  schemaVersion: 2;
  content: string;              // ≤ 2000, full markdown
  embeds: EmbedDocument[];      // ≤ 10
  rows: ComponentRow[];         // ≤ 5 legacy rows
  /** Publish-time only: assets referenced by attachment:// in this document. */
  assets: Record<string, ImageAsset>;
}

// ─────────────────── saved / persisted wrappers ───────────────
export interface SavedEmbed {
  id: string; name: string; tags: string[];
  revision: number;             // current head
  embed: EmbedDocument;         // value copy (never a reference)
  source?: { id: string; revision: number };  // provenance when copied
  createdAt: string; updatedAt: string; createdBy: string;
}

export interface SavedMessage {
  id: string; name: string;
  revision: number;
  document: MessageDocument;    // embed snapshot + components + actions, by value
  source?: { embedId?: string; embedRevision?: number };
  publications: Publication[];
  createdAt: string; updatedAt: string; createdBy: string;
}

export interface Publication {
  channelId: string;
  discordMessageId: string;
  revision: number;             // which revision was sent
  publishedAt: string; publishedBy: string;
  lastEditedAt?: string;
}

// ───────────────────────── validation ─────────────────────────
export type IssueSeverity = 'error' | 'warning' | 'info';
export interface Issue {
  severity: IssueSeverity;
  code: string;                 // e.g. 'embed.title.too_long'
  path: string;                 // 'embeds[0].fields[2].value' → maps to a node id
  nodeId?: string;              // structure-rail node for click-to-jump
  message: string;              // friendly, e.g. "Discord allows 256 characters."
  limit?: { actual: number; max: number };
}
```

### 6.2 Persisted JSON (what a save writes)

```json
{
  "schemaVersion": 2,
  "name": "Rules + Roles",
  "document": {
    "content": "",
    "embeds": [ { "id": "e1", "title": "Rules", "color": 8158399,
                  "description": "**Be kind.**",
                  "fields": [ { "id": "f1", "name": "1. Respect", "value": "…", "inline": false } ],
                  "footer": { "text": "Nilive", "icon": { "kind": "upload", "assetId": "a1",
                            "filename": "nilive.png", "mime": "image/png", "bytes": 12480,
                            "capturedUrl": "https://cdn.discordapp.com/attachments/…/nilive.png" } } } ],
    "rows": [
      { "id": "r1", "children": [
        { "id": "c1", "type": "button", "label": "Verify", "style": "success",
          "behaviour": { "mode": "interactive", "customId": "c:msg42:c1" },
          "action": { "type": "role.add", "roleIds": ["111","222"], "boost": false } },
        { "id": "c2", "type": "button", "label": "Docs", "style": "link",
          "behaviour": { "mode": "link", "url": "https://example.com/docs" } } ] }
    ],
    "assets": { "a1": { "kind": "upload", "assetId": "a1", "filename": "nilive.png",
                        "mime": "image/png", "bytes": 12480 } }
  }
}
```

### 6.3 Normalized model → Discord wire (the one conversion)

| Model | Wire (POST /channels/{id}/messages) |
|---|---|
| `MessageDocument.content` | `content` (omitted when empty) |
| `EmbedDocument.title/url/description/color/timestamp/fields` | `embeds[i].{title,url,description,color,timestamp,fields[{name,value,inline}]}` |
| `author{name,url,icon}` | `embeds[i].author.{name,url,icon_url}` |
| `thumbnail/image` (url asset) | `embeds[i].thumbnail.url` / `image.url` |
| `thumbnail/image` (upload asset) | `embeds[i].….url = "attachment://<filename>"` + the file in `files[n]` |
| `footer{text,icon}` | `embeds[i].footer.{text,icon_url}` |
| `ButtonComponent` interactive | `components[r].components[c] = {type:2, style:1..4, label, emoji:{name,id,animated}, custom_id, disabled}` |
| `ButtonComponent` link | `{type:2, style:5, label, emoji?, url, disabled}` (no `custom_id`) |
| `SelectComponent` string | `{type:3, custom_id, placeholder, min_values, max_values, disabled, options:[{label,value,description,emoji,default}]}` |
| Select kinds | `{type:5|6|7|8, custom_id, placeholder, min_values, max_values, disabled}` (no `options`) |
| assets | `attachments: [{id: n, filename, description?}]` + `files[n]` multipart parts |

`toDiscordPayload()` produces exactly this; the preview consumes it; the server re-validates it; the
Node harnesses assert it.

---

## 7. F — Validation model

### 7.1 One rule set, two execution points

`utils/discord_limits.py` (Python) holds every number, the message templates, and the capability matrix:

```python
LIMITS = {
  "message":   {"embeds": 10, "content": 2000, "attachments": 10,
                "combined_embed_chars": 6000, "action_rows": 5},
  "embed":     {"title": 256, "description": 4096, "fields": 25,
                "field_name": 256, "field_value": 1024,
                "footer_text": 2048, "author_name": 256},
  "button":    {"label": 80, "custom_id": 100, "url": 512, "per_row": 5,
                "guidance_label_with_emoji": 34},
  "select":    {"options": 25, "label": 100, "value": 100, "description": 100,
                "placeholder": 150, "min_values": 25, "max_values": 25, "per_row": 1},
  "upload":    {"embed_media_ext": ["jpg","jpeg","png","webp","gif"],
                "per_file_bytes_default": 20 * 1024 * 1024},
}
MESSAGES = {
  "button.per_row.too_many": "Discord allows a maximum of 5 buttons in one action row.",
  "select.per_row": "A select menu must be alone in its own row — put it on a new row.",
  "custom_id.duplicate": "Two components share the same identifier. Each one must be unique.",
  "role.hierarchy": "“{role}” sits above my role, so the bot cannot assign it. Move my role above it.",
  "upload.embed_unsupported_type": "Discord can only use .png, .jpg, .webp and .gif images inside an embed.",
  # …
}
```

The same table is served by `GET /api/embed-builder/limits`; `static/js/embed/validate.js` consumes it
and never hard-codes a number. The Python gate (`utils/embed_schema.py`) runs the identical rules on the
serialized payload before the Discord call — so a stale client or a hand-crafted request cannot bypass
them.

### 7.2 Severity tiers and where messages appear

| Tier | Meaning | UI | Blocks |
|---|---|---|---|
| **Error** | Discord will reject it, or it is structurally invalid (empty custom_id, 6 rows, 26 options, 6 001 combined chars, duplicate custom_id, `min_values > max_values`, missing label on an interactive button, non-http URL where http(s) is required) | Red, inline under the field; rail badge; strip count | Save & Send |
| **Warning** | Discord accepts it but it will not do what you expect, or it breaks a design guideline (role above the bot’s top role, managed role, select mixed with buttons, 2+ primary buttons in a row, foreign emoji that may not render, description with markdown that the *title* field would render literally, link button with no URL, unused empty embed) | Amber, inline + rail badge; strip count | Send (with explicit “Send anyway”) |
| **Info** | Budgets and helpful facts (characters remaining, fields used, “discord will show only the first embed with this URL”, “timestamp renders in each reader’s timezone”) | Muted counters, tooltips | nothing |

Nothing is ever reported only at the bottom: **every issue carries a `path`/`nodeId`**, so the strip’s
“Jump to first issue” also works, and clicking a rail badge scrolls+focuses the field.

### 7.3 Server-side / runtime errors

- Discord’s `400` body is parsed for `errors.<path>._errors[]` (Discord returns structured field paths,
  e.g. `embeds.0.fields.1.value.BASE_TYPE_MAX_LENGTH`) and each entry is mapped back to a field
  (`embeds[0].fields[1].value`) so the user sees **Discord’s own message under the right input**,
  not a wall of JSON at the bottom.
- Capability failures that only Discord can decide (channel missing `USE_EXTERNAL_EMOJIS`, missing
  Manage Roles, role hierarchy at click time) are surfaced with actionable copy and a link to the exact
  server setting. The bot also logs them to the audit log via the existing `log_action` path.
- Per-file size cap: validated against the configured value, and if Discord rejects with the real limit,
  the message says the real number (“Discord’s limit here is {n} MB”) and offers a one-click re-encode
  (canvas downscale to WebP/JPEG) as a *suggestion*, never silently.

### 7.4 Validation checklist (implements every item in your §13)

Counts & composition: >10 embeds · >5 rows · >5 buttons/row · select + buttons in one row · >25 fields ·
>25 options · empty option label/value · duplicate option values · empty interactive button label ·
missing action on an interactive component · publish with no content/embeds/components.

Lengths: every limit in §1.2 #2–#7 + the combined 6 000 budget across embeds (#7) — shown as a live
“message budget” bar.

Identifiers & actions: `custom_id` length ≤100 and unique per message · link button with
`custom_id` (invalid) · interactive button without `custom_id` (auto-filled, warned if manual) ·
duplicate `url` across embeds (#8) · role not assignable: `@everyone`, managed/integration role,
or positioned above the bot’s highest role · role set containing a removed role · expiry set with no
expiry-capable action · min/max values out of range.

Assets: URL scheme (`http(s)` only) · URL reachability test on demand (not automatic) · upload mode:
extension in `#10`, MIME sniffed from bytes, size ≤ cap, duplicate content deduped, filename
sanitised/unique · missing local file for a saved config · image referenced twice → uploaded once.

State gates: **Save** blocks on errors; **Send** blocks on errors and requires an explicit confirm for
warnings; **Update live** additionally requires the publication to be editable (message still exists and
is owned by the bot).

---

## 8. G — Implementation plan

Every phase ships alone, keeps the current page working, and has its own tests. Phases 0–3 are pure fixes
and can go out quickly; 4–6 are the redesign; 7 is optional polish.

| Phase | Deliverable | Files (new ✚ / changed ✎) | Tests | Risk / revert |
|---|---|---|---|---|
| **0. Rules & safety net** | `utils/discord_limits.py` + `GET /api/embed-builder/limits`; `utils/embed_schema.py` (payload builder + validator); legacy import conversion report | ✚ limits, ✚ schema, ✎ `api/embed_builder.py` (new module) | Python harness: every limit + friendly message; JS harness: table shape | None (no UI change). Revert = delete module |
| **1. Lifecycle & preview fixes** (fixes the reported bugs without redesign) | `window.NERO.pages.*` init/destroy hooked into `htmx:afterSwap`; embed-builder init made idempotent; `openIdb` blocked/timeout guards + draft save on idle/pagehide; identity endpoint not admin-gated; preview patching for text (no full `innerHTML`), keyed images; broken-image states; missing fields (`author.url/icon`, `footer.icon`, `embed.url`, timestamp) end-to-end; per-file size check + refreshed caps | ✎ `base.html`, ✎ `embedbuilder.html`, ✎ `embed-composer.js`, ✚ `api/embed_builder.py` routes (`bot-identity`, `limits`) | JS: no-init-after-swap regression, image node identity across renders, identity fallback for non-admin; Python: identity/limits routes | Medium (touches the shared composer) → keep `?legacy=1` escape hatch + minigame harness green |
| **2. Image system** | `ImageField` (URL/Upload, DnD, paste, test, replace, remove, dedupe, validation) wired to **all four** slots; `attachment://` end-to-end (payload + multipart + capture CDN URLs after publish); local-preview pipeline reused | ✚ `embed/components/image-field.js`, ✎ composer/preview, ✎ `api/embed_builder.py` publish + capture | JS: filename sanitisation/dedupe, state machine; Python: multipart mapping (`files[n]` ↔ `attachments[n]` ↔ `attachment://`) — the audit’s “coverage worth adding next” item | Medium; send path is additive (existing `/api/embedbuilder/send` untouched) |
| **3. Markdown parity** | `discord-markdown.js` (context-aware, code-shielded, escapes, spoilers, links, headers, lists, quotes, timestamps, mentions, emoji-only sizing) + toolbar for every text field + live counters | ✚ `embed/discord-markdown.js`, ✎ composer (delegates), ✎ templates (toolbar/attrs) | Golden corpus harness (≈80 cases incl. adversarial nesting) + a bot-side parity command (`/embed_parity`) that posts the corpus to a private channel so we can eyeball real rendering once per release | Low-medium; old renderer kept behind a flag for one release |
| **4. Builder workspace** | Store + normalized model + structure rail/inspector/preview shell; save/load of **Saved Embeds** with revisions; library screen; dirty-state guards; copy-on-load | ✚ `embed/store.js`, `embed/model.js`, `embed/validate.js`, `embed/preview.js`, `embed/embed-builder.js`, `embed/library.js`, template rewrite, ✚ SQLite tables + API routes | JS: store/history/patch selection; Python: revision append + copy semantics; e2e smoke: save → load → edit → save | Highest-effort phase; ships behind a new route (`/embed-builder/v2`) until you sign off, then becomes the default with `?legacy=1` for a week |
| **5. Components builder** | Rows/buttons/selects, emoji picker (unicode/guild/external/app) generalized to every component field, role picker with chips + role sets, action editor, full-message preview, validation for every component rule; **Saved Messages** with revisions | ✚ `embed/components-builder.js`, `embed/components/{emoji-picker,role-picker,repeater,button-card,select-card,action-editor}.js`, ✚ tables (`saved_messages`, `message_components`, `message_actions`, `role_sets`, `message_publications`), ✚ API routes | JS: limits enforcement (6 buttons rejected, select+button rejected, duplicate custom_id), action model round-trip; Python: save/publish payloads | Medium-high; entirely additive until the old reaction-roles page is retired |
| **6. Runtime actions (bot side)** | `cogs/components.py`: persistent views from saved messages, `custom_id = c:<configId>:<componentId>`, action executor (add/remove/toggle, bulk via role sets, policies incl. expiry/reuse of `reaction_role_expiry`), audit logging | ✚ `cogs/components.py`, ✎ `main.py` (load), ✎ `cogs/reactionroles.py` (delegate or deprecate), ✎ `dashboard/templates/manage/reactionroles.html` (becomes a preset view) | Python harness for the executor (multi-role, policies, hierarchy failures) + a live smoke test in a test guild | Medium; keep `cogs/reactionroles.py` loaded until the new path is verified |
| **7. Polish (optional)** | Drag reorder (mouse + keyboard) for fields/rows/options, duplicate component, keyboard shortcuts, JSON import/export with conversion report, send history (“load a sent message back into the editor”), full-screen preview, Components V2 exploration document | ✚ small modules | JS interaction harness | Low |

**Cross-cutting test strategy** (matching the repo’s harness style, `npm test` = `bash scripts/run_js_tests.sh`):

1. `scripts/test_embed_model.js` — normalization round-trips, copy-on-load, hash/dirty.
2. `scripts/test_embed_payload.js` — model → wire for every component/action combination (incl. uploads).
3. `scripts/test_embed_markdown.js` — golden corpus incl. code shielding, escapes, emoji-only, context matrix.
4. `scripts/test_embed_validate.js` — one case per rule in §7.4, asserting message text and `path`.
5. `scripts/test_embed_preview.js` — payload → DOM string equality for golden documents; image node identity across re-renders.
6. `scripts/test_components_limits.js` — row/button/select composition enforcement.
7. `scripts/test_embedbuilder_attachment_logic.js` (existing) — must keep passing unchanged.
8. Python: `scripts/test_embed_builder_api.py` — limits endpoint, save/revision append, copy semantics, multipart publish mapping, error-path mapping, publish capture.

**Migration**

| Legacy | New | Compatibility |
|---|---|---|
| `embed_templates.data = {content, embeds}` | Saved Embed (+ optionally Saved Message when components exist) | Read via existing `_doc_to_content_and_embeds`; importer converts and reports unmappable fields |
| `embed_templates.data = <single embed dict>` | Saved Embed v1 | Same conversion path (already handled by the cog) |
| `rr_panels` (+ `reaction_roles`, `reaction_role_panels`, `reaction_role_expiry`) | Saved Message with buttons + role actions | Keep rows; add a “Migrate to Components” action that creates a Saved Message and leaves the live message/rows untouched until a republish |
| `/api/embedbuilder/template*`, `/api/save-embed-template` | New library APIs | Old routes keep working (read/write legacy store) for one release; UI shows “legacy template” badge |

---

## 9. Decisions I need from you before coding

1. **Components V2 (containers / text displays / media galleries)** — my recommendation is **not** in
   this pass, because enabling the flag disables `content` + `embeds` (so an “embed + buttons” workflow
   is impossible in that mode). Do you want a V2 lane as a separate builder later, or should the embed
   format stay the primary target indefinitely?
2. **Local-upload durability** — proposal: local files are first-class for **send/edit**, and after a
   successful publish we capture the resulting CDN URLs back into the saved config so it becomes
   sendable from any device. Acceptable, or do you want uploads restricted to “one-off sends only”
   (URLs required for anything saved)?
3. **Publish vs new message** — proposal: “Send” always creates a new message; “Update live” edits the
   recorded message and is the only way to change a live panel. Confirm.
4. **Reaction-roles page** — proposal: retire it into the Components builder (same underlying data,
   a “Role menu” preset) once the new path is verified. Keep it side-by-side, or replace?
5. **Route strategy for the rewrite** — proposal: build the new workspace at `/embed-builder/v2`,
   verify, then switch `/embed-builder` to it with `?legacy=1` for a week (matches the audit’s §5 rollout
   plan). Fine, or do you prefer an in-place rewrite with a feature flag?
6. **Permission levels** — proposal: read/preview/limits at moderator, saving at admin, publishing
   (sending/updating live messages) at owner, matching the current `/api/embedbuilder/send`
   (`LEVEL_OWNER`). Confirm or adjust.

---

## 10. Appendix

### A. What “parity” means here (and how we prove it)

Local rendering cannot be pixel-identical to Discord’s client, and it should not pretend to be. The
commitment is **semantic** parity, verified three ways:

1. **Golden corpus** — one Node harness with ~80 inputs (nesting, escapes, code spans containing
   markdown, ZWJ emoji, mentions in code blocks, 6 000-char budgets, empty fields, 26 options, …),
   asserting our renderer/validator output.
2. **Live parity command** — a bot command that posts the same corpus to a private channel, so real
   Discord rendering can be compared side by side once per release; deviations become documented
   exceptions (with a comment in the capability matrix), never silent behaviour.
3. **Payload equality** — the payload the preview rendered is byte-compared with the payload the server
   sent (logged in the audit trail), so “the preview showed something else” is detectable in production.

### B. Explicit non-goals (first pass)

- Components V2 / containers / media galleries (see decision 1).
- Modal-based builders (`Text Input`, `Label` components) — modals are not messages.
- Webhook-only publishing, or publishing to channels the bot cannot see.
- Reactions-based role menus (the legacy `reaction_roles` path keeps working; new configuration is
  buttons/selects).
- Rich text WYSIWYG (a real editor with an inline cursor) — Discord markdown is a text format, and a
  toolbar + live preview is the honest interface.
- Storing user-uploaded images on our server (no public hosting story; uploads travel with the message).

### C. Known limitations we will state in the UI

- Preview typography/spacing is close, not identical; mobile layout is approximate.
- Discord decides at click time whether a member can receive a role (hierarchy at that moment) — we can
  only pre-validate.
- Foreign-server custom emoji may not render in a destination channel without `USE_EXTERNAL_EMOJIS`;
  application emojis always render (and can be imported from the existing app-emoji pipeline).
- A saved config containing local files needs those files present in the browser at send time **until**
  a publish captures their CDN URLs.
- The bot’s banner is profile-only; it will never appear in a message — the UI says so rather than
  faking it.

### D. File-by-file impact (summary)

| File | Action | Note |
|---|---|---|
| `dashboard/static/js/embed-composer.js` | refactor internals, **freeze public API** | minigame builder must keep working; its harness stays green |
| `dashboard/templates/manage/embedbuilder.html` | rewrite to markup + init call | logic moves to `static/js/embed/*` |
| `dashboard/templates/manage/reactionroles.html` | thin preset over the new message library (Phase 6) | after the new path is verified |
| `dashboard/api/embedbuilder.py` | keep routes; add new module alongside | no breaking changes |
| `dashboard/api/embed_builder.py` | new | follows `api/tickets.py` conventions |
| `utils/discord_limits.py`, `utils/embed_schema.py` | new | one source of truth + server gate |
| `cogs/components.py` | new (Phase 6) | generalizes `cogs/reactionroles.py` |
| `cogs/embedbuilder.py` | small fix | `build_embed` already expects keys the dashboard must start writing |
| `dashboard/templates/base.html` | page-lifecycle hook | `window.NERO.pages.*` init/destroy on swap |
| `database.py` | additive migrations only | new tables listed in §3.7 |
| `scripts/*` | new harnesses | listed in §8 test strategy |
