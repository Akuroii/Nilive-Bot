# PR #122 — Acceptance Report (review round 2)

Same PR, same branch. **OPEN / NOT MERGED / NOT DEPLOYED.**

| | |
|---|---|
| Branch | `arena/5e11d3cf-nilive-bot` |
| Final HEAD | `325e65ede308f2e7ef5e0a8d4a03aa65aca0375a` |
| Base | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` |
| Previous reviewed HEAD | `ecf60aad0e8ffb07c807eb9f3264ce027d47d54f` |

No replacement implementation, no new branch, no refactor of the queue/worker/
preparation/reconciliation core. Inventory (`cogs/wallet.py`), Rank
(`utils/rank_card_data.py`) and `utils/rank_card_renderer.py` are **byte-identical to
base** — verified with `git diff --quiet e0ca738 -- <file>`.

---

## 1. The bulk backfill now has a working Dashboard control

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

## 2. `retry_parked` now genuinely reactivates

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

## 3. Same-URL discovery is now a true no-op

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

## 4. One definition of “prepared”

**Was.** `_resolves_ready()` accepted any non-NULL `emoji_id`; the consumer path uses
`_valid_emoji_id()` (positive numeric). An asset with `''`, `'abc'`, `0` or `NULL` rendered
nowhere yet suppressed backfill — making the product permanently invisible.

**Now.** It imports the consumer's own predicate and returns true only for a usable ID with
`status='ready'`. One definition, taken from the read side.

**Regression** — `ReadyDefinitionTests` (6): empty string, non-numeric, zero, NULL each
**do not** suppress backfill (four separate tests — a `subTest` loop accumulated products
between iterations and produced false failures); a valid positive ID **is** recognised; a
valid ID with `status='uncertain'` is not.

## 5. Canonicalised URL matching in the name lookup

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

## 6. Guild-scoped rename cursor

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

## 7. The stale catalog key set

`test_shop_publisher_api.py::test_catalog_products_carry_the_category_link` expected a
literal catalog key set that predated the `app_emoji` field added by PR #121. I confirmed
the endpoint **still emits** `app_emoji` (a read-only token resolved from the local asset
table — no Discord call, no write), then added it to the expectation with a comment
explaining its provenance. **No production behaviour was changed to satisfy the
assertion.**

Result: `test_shop_publisher_api.py` — **18 passed, 15 subtests, exit 0** (was 1 failed /
17 passed).

## 8. Uncertain-create safety — unchanged, and now honestly labelled

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

## A real defect found while testing this round

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

## Test evidence (commands, counts, real exit codes)

Each command was run from the repository root. Exit codes are the **test process's own**
(no `tail`), read directly with `$?` immediately after the command.

| Command | Result | Exit |
|---|---|---|
| `PYTHONPATH=. python scripts/test_shop_emoji_automation.py` | **Ran 104 — OK** | **0** |
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

### Automation suite composition (104)

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

### New test → FIX map (each failed before its fix)

| Fix | Tests that failed before it |
|---|---|
| 1 Dashboard control | the panel/JS/wiring assertions did not exist and the endpoint had no caller |
| 2 `retry_parked` | `KeyError: 'reactivated'`; state stayed `parked` |
| 3 idempotence | generation `1 → 4` after 3 no-op backfills; `retry → queued`; in-flight pass reported `stale: 1` |
| 4 readiness | `''`/`'abc'`/`0`/`NULL` all suppressed backfill (`queued == 0`) |
| 5 canonical naming | label lost → `zebra_item_…`, `zeta_item_…`, `mike_item_…` instead of the canonical minimum |
| 6 rename cursor | target-guild assets skipped; second run still produced 4 PATCHes |
| 7 stale assertion | `1 failed, 17 passed` at base and at `ecf60aa` |
| schema race | 1/8 concurrent-init runs failed with `duplicate column name` |

---

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
  proven by `test_one_action_queues_the_whole_catalog_without_a_roster`, where every product
  is inserted directly into `shop_items` and never rostered.

## Changed files

| File | Change |
|---|---|
| `utils/shop_emoji_queue.py` | `state`/`generation` columns, `shop_icon_backfill` authorisation, same-URL no-op, `reactivate()` from `retry_parked`, dict return `{queued, reactivated}`, `_resolves_ready()` via `_valid_emoji_id`, CAS writers, race-safe migration |
| `utils/shop_emoji.py` | 8-char suffix, readable naming, `canonical_product_label`, `product_names_for_asset`/`_canonical_or_raw` (canonicalised name lookup), `emoji_name_conflict`, `_add_column` race-safe migration |
| `utils/shop_emoji_worker.py` | `_process_row` fencing, guild-scoped rename cursor, `_labels_for` canonicalisation, rename results carry identity, `rename_all`/`rename_pass` |
| `cogs/shop_emoji_worker.py` | NEW — worker + authorised-guild discovery loops |
| `dashboard/api/shop_emoji.py` | backfill (authorise + bulk queue), read-only queue/retry/names/report; no DDL on GET |
| `dashboard/api/economy_shop.py` | auto-queue hooks on create / `icon_url` change |
| `dashboard/templates/manage/shopdesigner.html` | NEW owner-only “Prepare all Shop product icons” panel |
| `dashboard/static/js/shop-designer.js` | backfill handler, progress renderer, parked-retry switch |
| `main.py` | load the new cog |
| `test_shop_publisher_api.py` | stale catalog key set now includes `app_emoji` |
| `scripts/test_shop_emoji_automation.py` | 104 tests |
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
