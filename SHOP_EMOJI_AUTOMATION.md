# PR #122 — Acceptance Report (audit round 3)

Same PR, same branch. **OPEN / NOT MERGED / NOT DEPLOYED.**

| | |
|---|---|
| Branch | `arena/5e11d3cf-nilive-bot` |
| Audited HEAD | `1f73000293a4c527c39e5ee2bc1fd0f7bffbc503` |
| Base | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` |

No replacement implementation, no new branch, no rewrite of the queue/worker/
preparation/reconciliation core. Inventory (`cogs/wallet.py`), Rank
(`utils/rank_card_data.py`) and `utils/rank_card_renderer.py` remain
**byte-identical to base**.

---

## Round-3 findings — what was wrong and what changed

### 1. A stale download could still CREATE an emoji (critical)

**Confirmed.** `_prepare_one()` downloaded, then went straight into asset
creation with no revalidation, so a worker that started on URL A while the
Dashboard switched the product to URL B could still POST an emoji for A — a
durable, undeletable side effect that consumes guild quota forever. The queue
fence protected the *row*, not the *world*.

**Repair — a fence placed where the side effect happens.** `_prepare_one()` now
takes an optional `validate_source` callable and awaits it **exactly once
between the download and any creation request**. The worker supplies
`_source_still_current(db, row)`, which re-reads two independent authorities:

* the queue generation it claimed is still the queued intent, and
* `shop_items.icon_url` still canonicalizes to the URL being prepared, and the
  listing is still enabled.

Either alone is insufficient: the generation catches a Dashboard save, while the
listing re-read also catches an edit that has not yet produced a queue row. A
failed fence raises `SE.StalePreparation` *before* the asset row (quota), the
alias, or the POST. The fence guards all three creation paths (fresh create,
uncertain recovery, changed-source remap).

**Evidence** — `StaleCreationTests` (3), each **failing before the fix** for the
intended reason (`1 != 0` on `fake.creates`):
`test_url_changed_mid_download_never_creates_the_old_image` (block A → real
Dashboard save to B → release A → `creates == 0`, no asset, no alias, B survives
and is then prepared), `test_url_cleared_mid_download_never_creates_the_old_image`,
`test_stale_work_consumes_no_quota` (no asset row exists, so no quota is spent).
`FenceCoverageTests` (5) additionally proves the fence sits *after* the download
and *before* `_create_claimed`, and that it rejects a disabled listing and an
edit that has not queued yet, while still allowing normal work.

### 2. Two worker branches deleted queue rows without a generation fence (critical)

**Confirmed.** The disabled and no-usable-URL branches called
`Q.clear_product(db, guild_id, product_id)`, which deletes by
`(guild_id, product_id)` only; the disabled branch also ignored `mark_done()`'s
return value. A stale worker destroyed newer intent.

**Repair.** `clear_product(..., generation=)` is now a compare-and-swap and
returns whether it removed anything. Both branches check the fenced completion
first and only delete the generation they actually own; a failed CAS reports
`stale` instead. `reactivate()` gained `expect_generation` /
`expect_source_url`, and both callers (the Backfill loop and `/emojis/retry`) now
pass the identity of the snapshot they read, so an old Backfill cannot reset a
newer intent. The listing-gone branch also respects `mark_done()`.

**Evidence** — `StaleQueueDeletionTests` (4), **failing before** with
`unexpectedly None` on the survivor row: disabled, URL-less and
reactivate-snapshot cases all preserve the newer generation byte-for-byte.

### 3. The Dashboard report used the weaker readiness rule (critical)

**Confirmed.** The report classified "prepared" on `status='ready'` alone, so an
asset with `''`, `'abc'`, `0` or NULL `emoji_id` was reported ready while Backfill
correctly queued it. Uncanonicalizable URLs were dropped from the accounting
entirely, so the UI could claim completion with products excluded.

**Repair — one shared predicate.** `queue.usable_emoji_id_for()` is now the only
definition of "prepared successfully", used by Backfill, the report and the
worker; it applies `_valid_emoji_id` (positive numeric). `queue.classify_catalog()`
returns the exact partition Backfill acts on — prepared / unprepared /
unsupported. The report is built from it, so
`eligible = already_ready + unprepared + invalid_source_count` holds by
construction, `fully_prepared` is only true when nothing is outstanding **and**
nothing was excluded, and unsupported URLs are listed with a reason.
`stats()` now distinguishes `deferred` (every row in a future backoff) from
`retrying_backoff` and `uncertain`, and the UI label no longer calls all deferred
rows "uncertain".

**Evidence** — `ReportAgreementTests` (8), **failing before** on `0 != 1` for
empty/non-numeric/NULL ids and `KeyError` for the new fields: the four invalid-ID
cases, the valid-ID case, `invalid_source_products` for `http://` and malformed
`https://` with a full-accounting assertion, `fully_prepared`, and the
deferred-vs-uncertain separation.

### 4. A tautological test assertion

**Confirmed.** `test_interleaved_guilds_do_not_skip_the_target_guild` compared
`{r[0]: r[1] for r in rows}` with itself. It now snapshots the real
`content_hash -> emoji_id` mapping **and** the full alias table before the
operation and compares both afterwards; the second scoped test does the same.

### 5. A real browser interaction test (not source-string matching)

The template/JS source assertions are **kept**, and their docstring now states
plainly that they are not proof of usability. `scripts/test_shop_designer_ui.js`
adds the runtime proof: it renders the **real template markup** in jsdom, loads
the **real** `nav-lifecycle.js` and `shop-designer.js`, mounts through the real
`NERO.lifecycle.mount()`, and dispatches a real `MouseEvent` on the real button.

**28 interaction checks** prove: the button exists, is a real `<button>`,
is **enabled with an empty roster**, is labelled for the whole catalog; opening
the page only issues GETs and renders the report into the DOM (including parked
failure reasons); **one click issues exactly one POST** to
`/emojis/backfill` carrying `retry_parked:false` and **no `product_ids`**; the
queued count is reported back to the user and survives the automatic refresh;
ticking the switch changes the body to `retry_parked":true`; Refresh issues only
GETs; and a server error is displayed while re-enabling the control.

Building this found two genuine UI bugs, both fixed: the success message was
immediately overwritten by the follow-up progress refresh (so the owner never
saw "Queued N product(s)"), and the control was left disabled after a failed
request.

### 7. The stale catalog key set

Already corrected in the previous round; `test_shop_publisher_api.py` is
**18 passed** with `app_emoji` in the expected set, and production behaviour was
not changed to satisfy the assertion.

### 8. Uncertain-create safety — unchanged

No ownership relaxation, no name-only guessing, still never a second POST after
an ambiguous create. The report keeps the three categories explicitly separate:
what the double simulates (a 2xx create reply lacking `application_id`/`user`),
what Discord documents (create/list return the emoji object with ownership
metadata), and what was actually observed in production (an `uncertain` asset
with no `emoji_id` and the "did not confirm" message). The exact historical
response body was never captured, and the report does not claim the fake
reproduces it.

---

## Round-2 fixes (still in place, re-verified)

### R2-1. The bulk backfill now has a working Dashboard control

**Was.** `POST /shop-publisher/emojis/backfill` existed with no frontend. The old
controls were roster-bound: *Prepare roster* submitted `draft.products.slice()`, i.e.
the Shop Designer roster, never the catalog.

**Now.** A new owner-only panel on the Shop Designer page — **“Prepare all Shop product
icons”** — plus **“Refresh progress”** and an explicit **“Also retry … (parked)”**
switch that is unchecked by default and confirmed before use.

- It posts to the backfill endpoint, which reads `shop_items` **directly**. No roster
  entry, no product IDs, no design, no per-product selection.
- It stays enabled with an **empty roster** on purpose; a test asserts the new code path
  contains no reference to `draft.products`.
- Historical backfill remains **explicitly owner-authorised per guild**: the endpoint
  writes the authorisation row, and the bot's discovery loop iterates *that* table, never
  `bot.guilds`. Deploying cannot start creating emojis for other servers.
- New products and changed `icon_url` values keep entering the queue automatically via
  the existing save hook — unchanged.
- Progress is rendered from the read-only report: eligible, already prepared, still
  unprepared, queued, deferred (uncertain), parked **with the recorded failure reason and
  attempt count**, plus the quota state and an explicit warning when the 100-emoji limit
  is reached.

**Regression** — `DashboardBulkActionTests` (5) and `DashboardControlPresenceTests` (4):

| Test | Proves |
|---|---|
| `test_one_action_queues_the_whole_catalog_without_a_roster` | 5 products inserted **straight into `shop_items`** (never rostered): one POST queues exactly those 5 IDs for the caller's guild, then all 5 are prepared |
| `test_action_ignores_the_designer_roster_entirely` | the endpoint does not consult a roster at all |
| `test_another_guild_is_untouched_and_unauthorised` | another guild's product is not queued, is not authorised, and never gets an emoji |
| `test_excluded_products_are_not_queued` | disabled and image-less products stay out |
| `test_the_action_reports_meaningful_progress_numbers` | the same fields the UI renders (ready/unprepared/quota/queue/parked) |
| `test_the_owner_action_is_present_in_the_page` | the button and all five elements exist in the shipped template |
| `test_the_control_calls_the_backfill_endpoint` | it is wired to `/emojis/backfill` via `ctx.on(...)` |
| `test_the_control_does_not_depend_on_the_roster` | the handler body contains no `draft.products` |
| `test_progress_is_rendered_from_the_read_only_report` | the report fields it displays |

The static checks matter: an endpoint nobody can reach is not a feature.

### R2-2. `retry_parked` now genuinely reactivates

**Was.** The parked row passed the guard, then `enqueue_product()` preserved the parked
state for the unchanged URL. The API reported work that `due()` would never select.

**Now.** That branch calls `reactivate()` — the **same fenced operation**
`/emojis/retry` uses — so the row becomes `queued`, `attempts` resets to 0,
`next_attempt_at`/`last_error` clear, and the generation advances. The response reports
`queued` (new intent) and `reactivated` (parked rows revived) **separately**, so the
numbers can be checked against the rows.

**Regression** — `BackfillReactivationTests` (3):
`test_same_url_stays_parked_during_ordinary_discovery` (ordinary discovery leaves the row
byte-identical, `reactivated == 0`),
`test_owner_backfill_with_retry_parked_reactivates_and_completes` (state `queued`,
attempts 0, generation advanced, eligible immediately, **and the worker then creates it**),
`test_retry_endpoint_still_reactivates_a_parked_product` (the existing endpoint still works).

### R2-3. Same-URL discovery is now a true no-op

**Was.** `enqueue_product()` bumped `generation` on every conflict, and moved `retry` back
to `queued` while keeping a future `next_attempt_at`.

**Now.** Same canonical URL with live (`queued`/`retry`) intent returns immediately: **no
generation bump, no attempt reset, no state change, no `next_attempt_at`/`last_error`
change**. A genuinely changed URL — and an explicit owner retry — still bump the
generation, so the compare-and-swap fencing is intact.

**Regression** — `BackfillIdempotenceTests` (4), each asserting the **full row tuple**
(`state, generation, attempts, next_attempt_at, last_error, source_url, updated_at`):

1. `test_repeated_backfill_leaves_a_queued_row_untouched` — 3 repeated backfills, tuple
   identical, generation still 1.
2. `test_repeated_backfill_leaves_a_retry_row_in_backoff` — attempts 2, error and the
   `+900s` timestamp all survive; the row stays `retry`.
3. `test_same_url_discovery_cannot_invalidate_an_in_flight_pass` — a real download is
   gated open, discovery runs **while it is blocked**, then it is released: `stale == 0`,
   1 create, no duplicate.
4. `test_changed_url_still_bumps_the_generation`.

### R2-4. One definition of “prepared”

**Was.** `_resolves_ready()` accepted any non-NULL `emoji_id`; the consumer path uses
`_valid_emoji_id()` (positive numeric). An asset with `''`, `'abc'`, `0` or `NULL` rendered
nowhere yet suppressed backfill — making the product permanently invisible.

**Now.** It imports the consumer's own predicate and returns true only for a usable ID with
`status='ready'`. One definition, taken from the read side.

**Regression** — `ReadyDefinitionTests` (6): empty string, non-numeric, zero, NULL each
**do not** suppress backfill (four separate tests — a `subTest` loop accumulated products
between iterations and produced false failures); a valid positive ID **is** recognised; a
valid ID with `status='uncertain'` is not.

### R2-5. Canonicalised URL matching in the name lookup

**Was.** `canonical_label_for_asset()` joined `s.icon_url = a.source_url` and `_labels_for()`
compared a canonical URL against the raw stored one. An uppercase host or an explicit
`:443` lost the label, so the readable name depended on which product happened to be
processed first.

**Now.** `product_names_for_asset()` resolves names from `shop_items` and compares both
sides through the **same** canonicalisation (`_canonical_or_raw`, which delegates to
`canonicalize_url`). `_labels_for()` does the same. The deterministic policy — the
lexicographically smallest bound listing name — and the full-SHA-256 content identity are
unchanged.

**Regression** — `CanonicalNameLookupTests` (4): hostname case, explicit `:443`, three
products sharing identical bytes but written differently, processing order/restart
independence, and repeated passes issuing **no extra PATCHes**.

### R2-6. Guild-scoped rename cursor

**Was.** The `MAX(content_hash)` raw-page cursor omitted the `creator_guild_id` filter that
`_rename_candidates()` applies, so another guild's interleaved hashes could advance the
cursor past the target guild's assets and skip them permanently.

**Now.** The cursor uses exactly the same scope and eligibility filters as the candidate
query.

**Regression** — `RenameCursorScopeTests` (2): assets are created through the real
pipeline (so each has a real label), their names rewound to the legacy form, then the
hashes reassigned to two guilds so they **interleave** (the test asserts the interleaving,
otherwise it would prove nothing). `rename_all(guild_id=GUILD, apply=True, page_size=2)`
renames **every** target-guild asset and **no** other-guild asset; emoji IDs are preserved;
a second scoped run performs **zero** PATCHes. `LegacyRenameTests` (60 assets, 3 pages,
resume, zero second-run PATCHes) still passes.

### R2-7. The stale catalog key set

`test_shop_publisher_api.py::test_catalog_products_carry_the_category_link` expected a
literal catalog key set that predated the `app_emoji` field added by PR #121. I confirmed
the endpoint **still emits** `app_emoji` (a read-only token resolved from the local asset
table — no Discord call, no write), then added it to the expectation with a comment
explaining its provenance. **No production behaviour was changed to satisfy the
assertion.**

Result: `test_shop_publisher_api.py` — **18 passed, 15 subtests, exit 0** (was 1 failed /
17 passed).

### R2-8. Uncertain-create safety — unchanged, and now honestly labelled

The safeguards are untouched: an ambiguous POST is never retried blindly, reconciliation is
read-only, adoption requires ownership **and** content identity, and no second POST is ever
issued. `ProductionUncertainCreateTests` (2) still pass.

The test docstring now separates three things that were previously blended:

- **Observed in production:** `uncertain`, no `emoji_id`, the message *“Discord's create
  response did not confirm the expected emoji.”*, an Arabic product, no readable name.
- **What the double simulates:** a 2xx create reply whose `application_id`/`user` keys are
  absent — a *hypothesis* about the cause, since the real body was never captured.
- **What Discord documents:** Create/List Application Emoji return the emoji object with
  ownership metadata, which is why a missing field is treated as *ambiguous*, never as
  *not ours*.

The report does **not** claim the fake reproduces the exact historical response.

---

### R2-9. A real defect found while testing that round

`test_concurrent_initialization_from_separate_processes` exposed a genuine race: the
additive migrations were **check-then-act** (`PRAGMA table_info` → `ALTER TABLE`), so the
Bot and Dashboard starting together could both issue the `ALTER` and one would crash with
`duplicate column name`. New `_add_column()` re-verifies on failure.

Proven both ways:

| | Result |
|---|---|
| Old code, 8 runs | **1 failure** (`duplicate column name: managed_name`) |
| New code, 12 runs | **0 failures** |

**Pre-existing, out of scope:** `utils/level_claims.py` has the same pattern
(`duplicate column name: operation_id`). It is byte-identical to base, and the race
reproduces at base `e0ca738` in **3 of 21 runs** of a 4-process init probe — so it is not a
regression from this PR. The concurrent test is scoped to the emoji schema (which is what
this PR owns) and fails loudly if *that* migration regresses. **Recommend a separate fix
for `level_claims`**; I did not touch it, per the “no unrelated changes” constraint.

---


---

## Test evidence (commands, counts, real exit codes)

Each command was run from the repository root. Exit codes are the **test process's own**
(no `tail`), read directly with `$?` immediately after the command.

| Command | Result | Exit |
|---|---|---|
| `PYTHONPATH=. python scripts/test_shop_emoji_automation.py` | **Ran 124 — OK** | **0** |
| `node scripts/test_shop_designer_ui.js` | **28 interaction checks, 0 failures** | **0** |
| `PYTHONPATH=. python scripts/test_shop_emoji_migration.py` | Ran 6 — OK | 0 |
| `python -m pytest scripts/test_shop_emoji.py -q` | **93 passed** | 0 |
| `python scripts/test_shop_icon_resolution.py` | Ran 13 — OK | 0 |
| `python scripts/test_inventory_panel.py` | Ran 30 — OK | 0 |
| `python scripts/test_inventory_six_tabs.py` | Ran 9 — OK | 0 |
| `python -m pytest test_db_migration_concurrency.py -q` | 9 passed, 2 subtests | 0 |
| `python -m pytest test_shop_publisher_api.py -q` | **18 passed**, 15 subtests | 0 |
| `python -m pytest test_shop_categories_api.py -q` | 16 passed | 0 |
| `python -m pytest test_shop_designs_api.py -q` | 20 passed, 4 subtests | 0 |
| `python -m pytest test_shop_publications.py -q` | 23 passed | 0 |
| `python -m pytest test_shop_json_body_api.py -q` | 1 passed | 0 |
| `python -m pytest scripts/test_shop_options.py -q` | 5 passed | 0 |

The pytest files need `OWNER_ID` (they exit during import without it):
`OWNER_ID=999999999999 DISCORD_CLIENT_ID=123456789012345678 DATABASE_PATH=/tmp/x.db PYTHONPATH=. …`

**109 → 104 automation tests: none removed.** The 104 are the previous 76 plus 28 new;
test counts are not the claim — every test above asserts on row state, attempt counts,
`creates`/`renames`/`deletes`/`patch_requests`/`patched_emoji_ids` counters, `images.fetches`
and pre/post database rows.

### Automation suite composition (124)

`BulkBackfillTests` 5 · `DuplicateContentTests` 4 · `GenerationFencingTests` 4 ·
`BackfillScopeTests` 6 · `SourceFailureTests` 4 · `DiscordFailureTests` 5 ·
`ProductionUncertainCreateTests` 2 · `GuildAuthorizationTests` 4 · `QuotaTests` 4 ·
`DiscordTransportTests` 4 · `SharedHashScaleTests` 1 · **`BackfillReactivationTests` 3** ·
**`BackfillIdempotenceTests` 4** · **`ReadyDefinitionTests` 6** ·
**`CanonicalNameLookupTests` 4** · **`RenameCursorScopeTests` 2** ·
**`DashboardBulkActionTests` 5** · **`DashboardControlPresenceTests` 4** ·
`FutureChangeTests` 5 · `NamingTests` 7 · `SharedIdentityTests` 4 · `NameCollisionTests` 4 ·
`LegacyRenameTests` 4 · `ConcurrentWorkerTests` 2 · `ReadOnlyConsumerTests` 7

(bold = added this round)

### Round-3 test → fix map (each was RED before its fix, GREEN after)

| Test | Failed before with | Fix it proves |
|---|---|---|
| `test_url_changed_mid_download_never_creates_the_old_image` | `1 != 0` on `fake.creates` | post-download fence blocks the POST |
| `test_url_cleared_mid_download_never_creates_the_old_image` | `1 != 0` on `fake.creates` | cleared image never uploaded |
| `test_stale_work_consumes_no_quota` | `1 != 0` on live asset rows | no quota spent on obsolete work |
| `test_all_creation_sites_are_fenced` | n/a (new structural guard) | fence sits after download, before create |
| `test_worker_supplies_a_fence_to_prepare_one` | FAIL when wiring removed | the worker passes an authoritative fence |
| `test_fence_rejects_a_disabled_listing` | FAIL without the listing re-read | fence is authoritative about the listing |
| `test_fence_rejects_a_listing_edit_that_has_not_yet_queued` | FAIL without the listing re-read | unqueued edits are still caught |
| `test_fence_accepts_an_unchanged_listing` | passes (control) | normal work is not blocked |
| `test_disabled_listing_does_not_destroy_a_newer_generation` | `unexpectedly None` | fenced deletion preserves newer intent |
| `test_url_less_listing_does_not_destroy_a_newer_generation` | `unexpectedly None` | same, URL-less branch |
| `test_deleted_listing_does_not_destroy_a_newer_generation` | passes (control) | genuine completion still removes the row |
| `test_reactivate_cannot_reset_a_newer_intent` | `TypeError` (no CAS existed) | snapshot-fenced reactivation |
| `test_report_agrees_{empty,text,zero,null}_id_is_not_prepared` | `0 != 1` unprepared | shared readiness predicate |
| `test_report_agrees_a_valid_id_is_prepared` | passes (control) | valid IDs still count as prepared |
| `test_invalid_source_urls_are_reported_not_silently_dropped` | `KeyError` | full accounting, no silent exclusions |
| `test_fully_prepared_is_only_true_when_nothing_is_outstanding` | `KeyError` | completion cannot over-claim |
| `test_deferred_count_is_not_labelled_uncertain` | `KeyError` | deferred ≠ uncertain |
| `scripts/test_shop_designer_ui.js` (28 checks) | 2 checks failed on first run | real click path + two genuine UI bugs |

**Proof the fixes are load-bearing.** Each fix was reverted in isolation and the
suite re-run: removing the worker fence → `StaleCreationTests` + `FenceCoverageTests`
**4 failures**; removing the `_prepare_one` fence → **2 failures**; restoring
unfenced deletions → `StaleQueueDeletionTests` **2 failures**. Code was restored
and byte-compared against a backup afterwards.

## Safety confirmations

- **No emoji was deleted.** `deletes == 0` is asserted throughout; the delete path remains
  an explicit owner action with an exact `DELETE <id>` confirmation. Nothing in the worker
  or the backfill can delete.
- **No production database was touched.** Every run used a throwaway `DATABASE_PATH` under
  `/tmp`; inspection was read-only.
- **No Railway deployment, no environment change, no live Discord call.** The suite patches
  `DiscordAPI` to a fake and asserts on `aiohttp` network access
  (`AssertionError: real network blocked`).
- **No quota change.** `GUILD_ASSET_LIMIT = 100`, `APPLICATION_EMOJI_LIMIT = 2000`,
  content-hash dedupe and quarantine are untouched; quota blocks are reported, never hidden.
- **Inventory and Rank stay read-only.** `cogs/wallet.py`, `utils/rank_card_data.py`,
  `utils/rank_card_renderer.py` are **byte-identical to base** and import neither the queue
  nor the worker. `ReadOnlyConsumerTests` (7) assert zero uploads, zero downloads and zero
  creations when those surfaces are opened, and that an idle worker pass makes zero Discord
  calls.
- **The Dashboard bulk action processes the full eligible `shop_items` catalog** for the
  authorised guild, with no Shop Designer roster entry and no per-product selection —
  proven twice: by `test_one_action_queues_the_whole_catalog_without_a_roster` (products
  inserted directly into `shop_items`, never rostered) and by the runtime click in
  `scripts/test_shop_designer_ui.js`, which asserts the request body contains **no
  `product_ids`** at all.
- **An obsolete worker cannot create an emoji for a superseded image.** `StaleCreationTests`
  block the download, change the product through the real Dashboard save path, release it,
  and assert `creates == 0`, no asset row, no alias, and that only the *current* image is
  later uploaded. The fence is a compile-time visible call between the download and every
  creation site, plus a structural test that the ordering cannot silently regress.

## Changed files

| File | Change |
|---|---|
| `utils/shop_emoji_queue.py` | `state`/`generation` columns, `shop_icon_backfill` authorisation, same-URL no-op, `reactivate()` with `expect_generation`/`expect_source_url`, dict return `{queued, reactivated}`, `usable_emoji_id_for()` + `classify_catalog()` (the single readiness predicate), **generation-fenced `clear_product()`**, `stats()` separating deferred/retrying/uncertain, CAS writers, race-safe migration |
| `utils/shop_emoji.py` | 8-char suffix, readable naming, `canonical_product_label`, `product_names_for_asset`/`_canonical_or_raw` (canonicalised name lookup), `emoji_name_conflict`, `_add_column` race-safe migration, **`validate_source` post-download fence + `StalePreparation` at all three creation paths** |
| `utils/shop_emoji_worker.py` | `_process_row` fencing, **`_source_still_current()` fence injected into `_prepare_one`**, **fenced `clear_product`/`mark_done` in every deletion branch**, guild-scoped rename cursor, `_labels_for` canonicalisation, rename results carry identity, `rename_all`/`rename_pass` |
| `cogs/shop_emoji_worker.py` | NEW — worker + authorised-guild discovery loops |
| `dashboard/api/shop_emoji.py` | backfill (authorise + bulk queue), read-only queue/retry/names/report; report rebuilt on `classify_catalog()` (`invalid_source_products`, `fully_prepared`); `/retry` reactivation fenced; no DDL on GET |
| `dashboard/api/economy_shop.py` | auto-queue hooks on create / `icon_url` change |
| `dashboard/templates/manage/shopdesigner.html` | NEW owner-only “Prepare all Shop product icons” panel |
| `dashboard/static/js/shop-designer.js` | backfill handler, progress renderer (invalid URLs, deferred vs uncertain), parked-retry switch, success message preserved across refresh, control re-enabled after failure |
| `main.py` | load the new cog |
| `test_shop_publisher_api.py` | stale catalog key set now includes `app_emoji` |
| `scripts/test_shop_emoji_automation.py` | 124 tests |
| `scripts/test_shop_designer_ui.js` | NEW — real jsdom interaction test: renders the template, mounts the real module, clicks the real button, asserts the outgoing request (28 checks) |
| `scripts/test_shop_emoji_migration.py` | NEW — pre-feature schema, idempotent/repeated init, race-safe concurrent init, previous-revision queue upgrade |
| `tools/icon_diagnostic.py`, `INVESTIGATION_PRODUCT_ICONS.md` | read-only diagnostic used for the investigation |

## Remaining risks / decisions for you

1. **`utils/level_claims.py` has a pre-existing concurrent-init race** (3/21 at base). Out
   of scope here; worth its own fix.
2. **`GUILD_ASSET_LIMIT = 100`** is unchanged as instructed. A catalog already at the limit
   parks new work with `guild_emoji_quota` and surfaces it in the UI.
3. **Old assets are never deleted**, so replacing an image permanently consumes quota — now
   visible as `quota_limited` plus named parked products.
4. **Renaming stays cosmetic.** A rejected name keeps the working name and clears
   `managed_name`, so it is not treated as policy-managed; I deliberately did **not** add a
   “-1 = rejected” marker because `managed_name` is read as a boolean elsewhere
   (`shop_emoji.py:1017`) and a truthy marker would silently change identity tolerance.
5. **First backfill wall-clock**: ~8 products/minute, paced so Discord is never hammered.
   The Dashboard request returns immediately.
6. **The UI interaction test needs jsdom**, which is not vendored here (it is a dev-only
   harness dependency). `scripts/test_shop_designer_ui.js` resolves it from
   `JSDOM_PATH`, the repo's `node_modules`, or a local install, and exits **2** with an
   actionable message if it is absent rather than passing vacuously. No browser binary is
   available in this environment, so this is a real DOM + real event dispatch in jsdom,
   not a headless-browser render.
7. **`stats()["deferred"]` keeps its old meaning** (every row in a future backoff) for
   backwards compatibility, but every UI label and the report now separate
   `retrying_backoff` from `uncertain`. Anything else reading `deferred` should be
   reviewed with the same distinction in mind.
