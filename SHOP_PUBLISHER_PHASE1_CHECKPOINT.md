# Shop Publisher — Phase 1 checkpoint (template + product → token resolution → Publisher preview)

**Checkpoint (baseline):** branch `arena/01a0e9df-nilive-bot` at `4e2f6c5c52d5c558288f7bd73ff7c89f22c04833`
(merge of PR #64 = the completed Embed Builder boundary from PR #63) · **Date:** 2026-09-28 ·
**Protocol:** same checkpoint protocol as `EMBED_BUILDER_PHASE0_REPORT.md` / the PR #63 update record —
base ref + date + state, file inventory with deltas, content hashes, fresh validation evidence, explicit
boundary, explicit deferrals.

**MVP boundary (locked):** the MVP ends at Phase 2 completion. Phase 1 = Template + Product → token
resolution → Publisher preview. Phase 2 = Preview → Publish → Discord message → existing purchase
button/mechanism. Phase 3 (Free + Prestige) is the next controlled extension. Phase 4 (publication
management / staleness / unpublish / republish) does NOT block delivery. Automatic synchronization,
`/shop` curation, Select Menus and advanced publication policies remain explicitly deferred.

---

## 1. Phase 1 baseline — recorded BEFORE any Phase 1 edit

State: working tree clean at `4e2f6c5c`; 300 tracked files; staged 0 · untracked 0 · `git diff --check` clean.

Tracked-files manifest hash (sha256 over the sorted per-file sha256 listing):

```
24c17760a5c2029e6cc32ede88f63fa6e490e14fd954443c096715e3c18cec36
```

Baseline hashes of the boundary-relevant files (sha256):

| File | sha256 (baseline) | Fate in Phase 1 |
|---|---|---|
| `dashboard/api/__init__.py` | `b3226a81bd1eee4e0dbf34e07858e8535ac27035eba2244b391fc97af3128c3d` | additive import only |
| `dashboard/app.py` | `1c43b553d68c59c13f2cad6855291a94cb13fc9eeeb82b3946bcb7f47c495332` | additive route only |
| `utils/permissions.py` | `c9fa93e5af5e22e854ac2757b0729d67b014de41357b13d89f68885dba717774` | additive page key only |
| `dashboard/templates/base.html` | `8668725d6d48d1725ebc3739f02e7c001ea20073be9bd33b077986509378bf1b` | additive nav entry only |
| `cogs/shop.py` | `f033f62de57196e4e1780ef0157a9a47c318cb903a8e5a4b19fa588b04c284d0` | **NOT touched** (purchase engine) |
| `utils/shop_validation.py` | `e0b16c5ff877e2b6c3e332acaec67c52f7df6516769ea59d0934e4fb3fad5fbe` | **NOT touched** (paid-product validation) |
| `utils/embed_schema.py` | `582064751a0efd102c8aab7fbab9cd68bcd8e250a77be2d4ca7abb744bf3df59` | **NOT touched** (reused read-only) |
| `utils/prestige.py` | `f3a842a26592d57f584170911c905408d1af29e26c399f644a68f5f5ec6f6a44` | **NOT touched** (reused read-only) |
| `database.py` | `fefdf6857e54230f988d69f3a33df231f13a82c4ea541ca2a8735f9997c12999` | **NOT touched** (no `shop_publications` in Phase 1) |

All Embed Builder files (PR #63/#64 boundary — `dashboard/static/js/embed/*`, `embed-composer.js`,
`embed-builder-page.js`, `nav-lifecycle.js`, `dashboard/api/embedbuilder.py`,
`dashboard/templates/manage/embedbuilder.html`, `message_builder.html`, `utils/discord_limits.py`) are
**NOT touched** in Phase 1. The Publisher page *loads* `embed/model.js`, `embed/discord-markdown.js` and
`embed/preview.js` read-only via the page-script loader (same include order the Message Builder uses);
no byte of any of them changes.

### Baseline validation evidence (fresh runs on `4e2f6c5c`, before edits)

| Suite | Result |
|---|---|
| `bash scripts/run_js_tests.sh` (node syntax + harnesses) | **22/22 harnesses PASS**, exit 0 |
| `scripts/test_*.py` (28 files, CI loop) | **27/28 PASS** |
| `scripts/test_afk.py` | **169/170** — one pre-existing red: `cancel reports the 2 mentions counted`; deterministic (3/3 runs identical), unrelated to Shop/Embed code (AFK mention counting) |
| everything else Python | PASS (`test_phase1_api`, `test_vi_shop_api`, `test_prestige*`, `test_embed_schema` 62/62, `test_minigame*`, `test_wallet*`, …) |

The single pre-existing failure is recorded here as **baseline noise**; Phase 1 must neither fix it
(out of scope) nor add to it.

---

## 2. Approved Phase 1 scope (this checkpoint's delivery)

1. **Template selection** — pick a reusable presentation template from `embed_templates`
   (the whole-message `{content, embeds}` shape; legacy single-embed rows normalize to that shape).
2. **Product selection** — pick a product from `shop_items` (existing product source of truth).
   No new category system: the picker groups by the existing `type` column only (V1 grouping).
3. **Fixed token resolution** — a fixed, deterministic, non-programmable token catalog resolved by a
   pure server-side resolver. No expressions, no nesting, no conditions, no filters; resolved values are
   never re-scanned; unknown tokens stay verbatim and are reported.
4. **Publisher preview** — shows the resolved product presentation **plus the purchase action that will
   actually be published** (the existing `shop_buy_<id>` mechanism).
5. **Preview warnings** — deterministic, pathed warnings (unknown token, empty value, Discord-limit
   breaches after resolution, disabled product, out-of-stock product).
6. **Required Phase 1 tests** — resolver suite, API suite, DOM harness.

### Locked decisions honored in Phase 1

- `shop_items` remains the product source of truth; `embed_templates` remains the presentation layer.
- No publication layer yet — `shop_publications` does not exist in Phase 1 (Phase 2).
- No new category system (`type` for V1 picker grouping only); no new purchase engine; `/shop` (Discord
  command **and** dashboard page) unmodified; existing purchase mechanism + Prestige logic reused as-is.
- Token resolver fixed/deterministic/non-programmable.
- Preview shows resolved presentation + the real purchase action (`custom_id shop_buy_<id>`, handled by
  the existing `cogs/shop.py` interaction → `process_purchase`).
- **Free behavior is NOT implemented in Phase 1.** `{{product.price}}` renders the mechanical price for
  every row (including zero-price rows, e.g. `0 🪙 Coins`); the `𝐅𝐫𝐞𝐞` rendering is Phase 3. Paid-product
  validation (`utils/shop_validation.py`) is untouched — no weakening.
- Embed Builder stays stable: zero edits to any builder file. The Phase 2 send-helper extraction question
  cannot arise yet — Phase 1 has no send path at all.

### Explicitly NOT in Phase 1 (and not started)

Publishing/send routes · `shop_publications` table · purchase-flow changes · Free/Prestige behavior ·
`/shop` changes · Phase 2 work of any kind.

---

## 3. Implementation record

Phase 1 is implemented and validated on top of baseline checkpoint commit
`4c6489a` (this document's baseline section, committed before any code edit).
The implementation commit and the commit carrying this completed record are
listed in §9 — 4 modified files (42 pure additive lines) + 8 new files.
Nothing outside the Phase 1 scope was touched.

---

## 4. Files changed vs baseline `4e2f6c5c`

### New files (8)

| File | Lines | What it is |
|---|---|---|
| `utils/shop_publisher.py` | 336 | THE fixed token resolver + preview assembly. Pure: no Flask/Discord/DB. `TOKEN_CATALOG` (14 fixed tokens), `normalize_template_doc`, `product_token_values`, `resolve_message`, `purchase_action`, `collect_warnings`, `preview_message`. |
| `dashboard/api/shop_publisher.py` | 164 | Read-only API: `GET /api/shop-publisher/catalog` (templates + products pre-grouped for the V1 picker + the fixed token catalog) and `POST /api/shop-publisher/preview` (runs the shared resolver). No writes, no audit rows. |
| `dashboard/templates/manage/shoppublisher.html` | 113 | The Publisher page shell (template picker, product picker, preview mount, purchase-action row, warnings, resolved tokens, token catalog). No inline script; page module via nav-lifecycle. |
| `dashboard/static/js/shop-publisher.js` | 322 | The page module: pickers (product `<optgroup>`s by the existing `type` only), preview requests with a stale-response guard, purchase action/warnings/token rendering. Interprets NO token. |
| `dashboard/static/css/shop-publisher.css` | 143 | `sp-*` styles only; reuses the frozen `.eb-preview-box` frame class for the Discord-style preview. |
| `scripts/test_shop_publisher.py` | 350 | Resolver suite — 59 checks. |
| `scripts/test_shop_publisher_api.py` | 244 | API suite (real Flask app + permissions + CSRF + scratch DB) — 15 tests. |
| `scripts/test_shop_publisher_form.js` | 452 | DOM harness booting the REAL page module + REAL frozen preview engine — 47 checks. |

### Modified files (4 — additive only, +42/−0)

| File | Δ | What changed |
|---|---|---|
| `dashboard/api/__init__.py` | +6 | One import line + comment registering `dashboard.api.shop_publisher` on the shared blueprint (same pattern as every other submodule). |
| `dashboard/app.py` | +18 | One route: `GET /shop-publisher` → `manage/shoppublisher.html`, `@require_page("shoppublisher")`, `bot_identity` from the existing helper. |
| `utils/permissions.py` | +8 | One page key: `"shoppublisher": LEVEL_OWNER` (+ rationale comment). |
| `dashboard/templates/base.html` | +10 | One nav entry (Systems → Shop Publisher), same shape as every sibling link. |

### Files verified NOT changed (hashes match §1)

`database.py` (no `shop_publications`), `cogs/shop.py` (purchase engine), `utils/shop_validation.py`
(paid-product validation), `utils/embed_schema.py`, `utils/prestige.py`, every Embed Builder file
(`dashboard/static/js/embed/*`, `embed-composer.js`, `embed-builder-page.js`, `nav-lifecycle.js`,
`dashboard/api/embedbuilder.py`, `manage/embedbuilder.html`, `manage/message_builder.html`,
`utils/discord_limits.py`), `systems/shop.html`, `dashboard/api/economy_shop.py`. `git diff` contains
only the 4 files above; the untracked set is exactly the 8 new files.

## 5. Behavior implemented

1. **Template selection** — the picker lists `embed_templates` names for the session guild
   (whole-message `{content, embeds}` rows; legacy bare-embed rows normalize to one embed via
   `normalize_template_doc`, the same rule the Embed Builder read path applies).
2. **Product selection** — the picker lists `shop_items` (the product source of truth), grouped by
   the existing `type` column into `<optgroup>`s and nothing else. Disabled products stay visible and
   marked, so their preview warnings are reachable. No new category system anywhere.
3. **Fixed token resolution** — server-side, one resolver (`utils/shop_publisher.py`):
   - FIXED: exactly the 14 `product.*` tokens of `TOKEN_CATALOG` (id, name, description, price,
     price_amount, currency_name, currency_emoji, type, duration, stock, required_level, rarity,
     icon_url, prestige_tier); the UI's token reference renders the same table.
   - DETERMINISTIC: pure functions; same template + product + currency → byte-identical output;
     input never mutated; `{{product.price}}` follows the same charging rule `cogs/shop.py` uses
     (diamond price wins, else coin price) and the guild's configured currency names/emojis.
   - NON-PROGRAMMABLE: bare `{{ name }}` lookups only — no expressions, no nesting, no conditions,
     no filters; resolved values are never re-scanned (no injection); unknown tokens stay verbatim
     and are reported; resolution is confined to the documented message text surfaces (embed_schema's
     paths).
4. **Publisher preview** — the preview shows the resolved content + embeds (rendered by the FROZEN
   `embed/preview.js` engine, loaded read-only) **plus the purchase action that will actually be
   published**: a green `🛒 Buy <name>` button carrying `custom_id shop_buy_<id>` — the exact family
   `cogs/shop.py`'s `on_interaction` already dispatches to `process_purchase()`. The row also names
   the published style + custom_id so the contract is visible. `purchase_action()` is the descriptor
   Phase 2 publishes verbatim.
5. **Preview warnings** — deterministic, pathed, in fixed order: `product_disabled`,
   `product_out_of_stock`, then per-occurrence `unknown_token` / `empty_value` in document order,
   then `validation` (Discord-rule breaches of the RESOLVED payload via `utils/embed_schema` —
   these become Phase 2 publish blockers). The resolved-token table shows every occurrence with its
   path, value, and unknown/empty markers.
6. **Phase 1 discipline (the NOTs)** — no publish/send route (no Discord call exists in the
   Publisher), no `shop_publications`, no purchase-flow change, no `/shop` change (command or admin
   page), no Prestige change (`tier_label` reused read-only), and **no Free behavior**: zero-price
   rows render the mechanical `0 <icon> <name>`; the `𝐅𝐫𝐞𝐞` rendering stays Phase 3 and is pinned
   by an explicit Phase-1-state test.

## 6. Validation evidence (fresh runs on the final worktree)

| Suite | Result |
|---|---|
| `bash scripts/run_js_tests.sh` (node syntax + all harnesses) | **23/23 harnesses PASS** (22 baseline + `test_shop_publisher_form.js`), exit 0 |
| `scripts/test_shop_publisher.py` (new) | **59/59** |
| `scripts/test_shop_publisher_api.py` (new) | **15/15** |
| `scripts/test_shop_publisher_form.js` (new) | **47/47** |
| `scripts/test_*.py` full CI loop (30 files) | **29/30 PASS** — the only red is the recorded baseline `test_afk.py` **169/170** (`cancel reports the 2 mentions counted`), byte-identical to the baseline failure, deterministic, untouched scope |
| `python3 -m compileall` (CI import gate) | clean |
| `git diff --check` | clean |

New-suite highlights: resolver determinism + non-programmability (incl. the no-rescan injection
proof), byte-identical preview == shared-resolver equality through the real route, purchase-action
contract (`shop_buy_<id>`), warning order/paths, legacy template rows, CSRF + LEVEL_OWNER gates,
read-only proof (`mutation_snapshot` + audit_log unchanged), type-only picker grouping, stale-response
guard, teardown/re-mount cleanliness.

## 7. Compatibility / regression / performance

**Compatibility (the locked boundaries):**
- **Embed Builder stability:** zero byte changes to any builder file (§4 list); `test_embed_schema`
  62/62 and all message-builder/composer/nav-lifecycle/preview harnesses pass unchanged. The
  Publisher page loads `embed/model.js`, `embed/discord-markdown.js`, `embed/preview.js` read-only
  via the page-script loader — the same include order the Message Builder uses.
- **Send-helper extraction (behavior-identity check): NOT TRIGGERED in Phase 1** — there is no send
  path in Phase 1 at all. The question belongs to Phase 2; the plan's fallback (isolated publisher
  send path) remains the default unless a behavior-identity check proves extraction safe at that
  point.
- **Shop/purchase:** `cogs/shop.py`, `utils/shop_validation.py`, `systems/shop.html`,
  `dashboard/api/economy_shop.py` untouched; `test_phase1_api`, `test_vi_shop_api`,
  `test_vi_lifecycle`, `test_vi_shop_form.js`, `test_phase2_backend`, `test_wallet*` all pass.
  Paid-product validation unchanged (zero-price still rejected outside the existing Prestige VI
  exception — `test_vi_shop_api`'s zero-price tests pass untouched).
- **Prestige:** `utils/prestige.py` untouched; `test_prestige`, `test_prestige_api`,
  `test_phase1_prestige` pass. The resolver reads `tier_label()` only.
- **Schema:** `database.py` untouched — no migration, no `shop_publications`, no writes of any kind.

**Performance (in-process, scratch DB, median over 200 runs):**

| Path | Median |
|---|---|
| `preview_message()` pure resolver | **~24 µs/op** (10k runs; ~30 µs with the prestige-tier token warm) |
| `GET /api/shop-publisher/catalog` (50 products) | 5.2 ms |
| `POST /api/shop-publisher/preview` | 7.3 ms |
| `GET /shop-publisher` (page render) | 6.7 ms |

Page boot costs one catalog call; each selection change costs one preview call. No polling, no
timers, no background work — same class as the existing dashboard routes.

## 8. Remaining Phase 2 work

Phase 2 = Preview → Publish → Discord message → existing purchase button/mechanism:
1. A publish action on this page (channel picker + confirmation) and a
   `POST /api/shop-publisher/publish` route.
2. The isolated publisher send path (or the shared Discord send helper **only if** the
   behavior-identity check proves extraction safe against `dashboard/api/embedbuilder.py`'s send
   route — fallback is isolation).
3. Publish must call the SAME `utils/shop_publisher.preview_message()` (preview == publish) and
   attach the SAME `purchase_action()` descriptor (`shop_buy_<id>`, green `🛒 Buy <name>`) so the
   existing `cogs/shop.py` `on_interaction` → `process_purchase()` mechanism serves purchases with
   zero changes to the purchase flow.
4. `shop_publications` (the minimal Product + Template + Discord Message link) — created in Phase 2
   only; `CREATE TABLE IF NOT EXISTS`, no migration of existing data.
5. Blocking policy over the existing warning codes (unknown_token / validation block; product-state
   warnings policy), audit-log row per publish, and Phase 2 tests (publish contract, publication row
   shape, purchase-button end-to-end wiring).

## 9. Commit / checkpoint reference

| Ref | What |
|---|---|
| `4e2f6c5c52d5c558288f7bd73ff7c89f22c04833` | Baseline boundary (merge of PR #64; PR #63 = completed Embed Builder boundary) |
| `4c6489a` | Baseline checkpoint record (this document's §1–§2, committed BEFORE any code edit) |
| `7d4a6e5` | Phase 1 implementation (4 modified + 8 new files) |
| record commit | This completed record (the commit that carries this document) |

Branch: `arena/01a0e9df-nilive-bot`. State at record time: all Phase 1 suites green, no new
regression vs baseline, worktree clean apart from the change set above. **Awaiting review before
Phase 2 begins.**
