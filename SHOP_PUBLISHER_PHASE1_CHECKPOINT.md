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

*(completed after implementation — §4–§7 below are filled on the final worktree)*

### 3.1 Files changed vs baseline `4e2f6c5c`

*(filled in §4)*

---

## 4. Files changed vs baseline

*(filled after implementation)*

## 5. Behavior implemented

*(filled after implementation)*

## 6. Validation evidence (fresh runs on the final worktree)

*(filled after implementation)*

## 7. Compatibility / regression / performance

*(filled after implementation)*

## 8. Remaining Phase 2 work

*(filled after implementation)*

## 9. Commit / checkpoint reference

*(filled after implementation)*
