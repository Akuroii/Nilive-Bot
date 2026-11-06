# Automatic Shop icon preparation — implementation report

Read this before reviewing the diff. It states what already existed, what was
missing, what changed, what was proven by test, and what remains risky.

| | |
|---|---|
| Branch | `arena/5e11d3cf-nilive-bot` |
| Base / HEAD at start | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` (PR #121 merge) |
| Application code modified | 4 files |
| New modules | 4 files |
| New tests | 1 file, 33 tests |

---

## 1. What already existed (reused, not replaced)

The preparation core was already correct and hardened. **None of it was
rewritten.** Everything below is untouched except the two marked rows.

| Piece | Location | Reused how |
|---|---|---|
| Content identity (SHA-256 of the exact uploaded PNG) | `shop_emoji.content_identity` | unchanged; still the dedupe key |
| URL canonicalization | `shop_emoji.canonicalize_url` | unchanged; queue stores the same canonical form |
| Alias table `(application_id, guild_id, source_url)` | `shop_emoji_url_aliases` | unchanged |
| Create fencing (`attempt_id`, `preparing`) | `_insert_asset_or_claim`, `_claim_existing`, `_create_claimed` | unchanged; the worker calls them |
| Uncertain/quarantine semantics | `_mark_uncertain`, `_retry_allowed` | unchanged |
| Read-only recovery | `_reconcile_asset`, `_adopt_asset`, `_refresh_ready_asset` | reused by the worker's sweep |
| Ownership proof | `_ownership_supported` | unchanged; still required for adopt/rename/delete |
| Duplicate protection | `_matching_name`, `_duplicates`, `delete_confirmed_duplicate` | unchanged; worker never deletes |
| Per-guild + app-wide quota | `_enforce_guild_quota`, `APPLICATION_EMOJI_LIMIT` | unchanged |
| Explicit owner workflow | `prepare_product_emojis`, `reconcile_product_emojis`, `delete_confirmed_duplicate` + `/api/shop-publisher/emojis/*` | kept intact and still the manual path |
| Startup reconciliation | `schedule_startup_reconciliation` (from `init_db`) | kept; the worker adds a periodic sweep |
| Read-only consumers | `cogs/wallet.py::_attach_prepared_app_emojis`, `utils/rank_card_data.py` | **not modified at all** |

That last row matters most: Inventory and Rank resolution is byte-identical to
what you verified working in production.

## 2. What was missing

1. **No durable intent.** Preparation was pull-only: an owner named ≤25 product
   IDs per request. Nothing recorded "this product still needs an icon", so a
   restart lost the intent and 100 products meant 100 manual selections.
2. **No discovery.** Nothing enumerated `shop_items` to find unprepared
   products.
3. **No automatic trigger.** Saving a product or changing its `icon_url` wrote
   the live row and never touched the emoji pipeline.
4. **Recovery was startup-only.** `reconcile_uncertain_at_startup()` ran once
   per process boot, capped at 25 assets, so an extended incident could stay
   unresolved until the next restart.
5. **Names were opaque** (`shop_3ef701f9213ab1742536266a`) and there was no
   rename capability at all.

## 3. What was added

### `utils/shop_emoji_queue.py` — durable intent (new)
`shop_emoji_queue` (`PRIMARY KEY (guild_id, product_id)`) with
`source_url`, `content_hash`, `attempts`, `last_error`, `next_attempt_at`.
Intent only: this module never touches Discord.
- `enqueue_product` — idempotent upsert; a *new* URL replaces pending work, the
  *same* URL keeps existing retry state (so a scheduler tick cannot defeat a
  backoff).
- `enqueue_guild_products` — the bulk backfill, reading `shop_items` directly.
  Skips products that already resolve to a ready asset.
- `prune_orphans` — drops rows whose listing vanished or lost its URL.
- `due` / `stats` / `next_attempt_at` — bounded, indexable reads.

### `utils/shop_emoji_worker.py` — the only automatic caller (new)
`run_pass()` = one bounded pass: prune → **idle fast path** → replay any legacy
`guild_id=product_id` duplicates → reconcile ambiguous assets (read-only) →
process ≤`limit` rows within a time budget.
Per row it does exactly one of: reuse a ready asset, `_prepare_one()` it, or
record why not. Ambiguity → `defer_uncertain` (never a blind POST).
`rename_pass()` = preview/apply readable names.

### `cogs/shop_emoji_worker.py` — scheduling (new)
`worker_loop` (60 s) and `backfill_loop` (300 s), both `wait_until_ready()`.
Idle pass = **zero Discord requests** (asserted by test).

### Dashboard — triggers + owner controls
- `dashboard/api/economy_shop.py`: add → queue; edit → queue **only** when
  `icon_url` actually changed; delete → drop intent. `_enqueue_icon_work` never
  lets a queue problem fail the admin's save.
- `dashboard/api/shop_emoji.py`: `POST …/emojis/backfill`,
  `GET …/emojis/queue`, `POST …/emojis/names` (preview | apply).

### `utils/shop_emoji.py` — naming + rename (additive)
- `slugify_name`, `readable_emoji_name`, `safe_emoji_name`, `rename_target_name`.
- `DiscordAPI.rename_emoji()` → `PATCH /applications/{app}/emojis/{id}`.
- `_prepare_one` / `_insert_asset_or_claim` accept an optional `emoji_name`
  (str or `hash -> name` resolver) so a **new** emoji is created with the
  readable name directly. Default is unchanged.
- `shop_emoji_assets.managed_name` (additive column + base DDL + migration).

## 4. Readable names

`readable_emoji_name(name, content_hash)` = `slug(hash[:6])`, ASCII, ≤32 chars.

| Product | Proposed |
|---|---|
| `عصير المجرات` | `asir_almjrat_3ef701` |
| `سهران السيرفر` | `shran_alsirfr_3ef701` |
| `عنتيل السيرفر` | `antil_alsirfr_3ef701` |
| `شوكولاته الجلاكسي` | `shukulath_aljlaksi_3ef701` |

The suffix is derived from the **content hash**, so identity never depends on
the name. A name with no surviving ASCII falls back to `shop_<hash24>`.

**Renaming existing emojis: yes, supported and safe.** Discord's Modify
Application Emoji (`PATCH …/emojis/{id}`) changes the name in place; the emoji
ID — which is what every alias, `emoji_id` column and rendered token uses —
does not change. So a rename cannot break a mapping. Verified in test:
`rename_pass(apply=True)` performs one PATCH, creates nothing, preserves the
ID, and a second run is a no-op. Preview mode performs no Discord call.

*Caveat discovered during design:* `_refresh_ready_asset` treats a name
difference from `emoji_name` as a metadata conflict and marks the asset
`failed`. A rename crash between PATCH and the local write would therefore have
bricked the asset. `managed_name` marks a name as policy-managed, and the
refresh accepts a name difference **only** when ownership and animation still
match. That is the single riskiest interaction in this change, and it has a
dedicated test.

## 5. Risks

**Production database**
- Additive only: one `ALTER TABLE … ADD COLUMN managed_name INTEGER NOT NULL
  DEFAULT 0` plus `CREATE TABLE IF NOT EXISTS shop_emoji_queue`. No column
  dropped, no row rewritten, no data migration.
- Verified against a *pre-feature* schema copy: column added, existing
  `emoji_id`/`emoji_name` preserved, queue created.
- No manual DDL at deploy time; `init_db()` performs it, as it already does for
  the other emoji tables.

**Discord**
- Quota: `GUILD_ASSET_LIMIT` (100) and `APPLICATION_EMOJI_LIMIT` (2000) are
  enforced by the existing code the worker calls. **A 100-product guild that is
  already at its per-guild allowance will see `guild_emoji_quota`**, one row at
  a time, recorded in `last_error` — not a silent stall. Raise the constant only
  with your decision; it is not changed here.
- Rate limits: one list per pass, ≤8 items, 45 s budget. Creates are the only
  mutating calls.
- Deletion: the worker has **no** delete path. Duplicates still require the
  existing owner-confirmed endpoint.
- Rename volume: `rename_pass` defaults to 25 assets per call.

**Operational**
- The first backfill of a large catalog will take several minutes of wall clock
  (8 per minute by default). That is intentional; the dashboard request that
  starts it returns immediately.
- `next_attempt_at` backoff is 15 min × attempt, parking after 4 attempts.

**Not addressed (out of scope by request)**
- No automated deletion of duplicate emojis.
- No chunked/parallel upload; passes are sequential.
- Renames are not attempted for assets whose bound emoji is absent from
  Discord — those are left for reconciliation.

---

## 6. Testing

```bash
# the new behavioural suite (33 tests)
PYTHONPATH=. python scripts/test_shop_emoji_automation.py

# pre-existing suites that cover the code this change touches
OWNER_ID=999999999999 DATABASE_PATH=/tmp/a.db DISCORD_CLIENT_ID=123456789012345678 \
  PYTHONPATH=. python -m pytest scripts/test_shop_emoji.py -q            # 93 passed
PYTHONPATH=. python scripts/test_shop_icon_resolution.py                 # 13 passed
PYTHONPATH=. python scripts/test_inventory_panel.py                      # 30 passed
PYTHONPATH=. python scripts/test_inventory_six_tabs.py                   # 9 passed
OWNER_ID=999999999999 DATABASE_PATH=/tmp/b.db DISCORD_CLIENT_ID=123456789012345678 \
  PYTHONPATH=. python -m pytest test_shop_categories_api.py test_shop_designs_api.py \
  test_shop_json_body_api.py -q         # 16 + 20 + 1 passed
```

The new suite drives the **real** Flask endpoints, the real schema, the real
queue, the real worker and the whole `utils/shop_emoji` core. Only two
boundaries are faked — the Discord HTTP API and the source-image download — and
both fakes are strict: an unexpected Discord call, or any real network access,
fails the test. `delete_emoji` raises if ever reached.

Scenario coverage:

| Requirement | Test |
|---|---|
| 100 products, bounded batches | `test_100_products_bounded_batches_and_full_coverage` |
| Resume after restart | `test_restart_mid_backfill_resumes_without_duplicates` |
| No roster entry needed | `test_backfill_from_database_requires_no_roster_entry` |
| Duplicate images / shared hash | `test_three_products_sharing_one_image_create_one_emoji`, `test_shared_hashes_are_not_duplicated_across_a_restart` |
| Dead / expired URLs | `test_expired_url_is_recorded_and_retried_with_backoff`, `test_permanently_dead_url_stops_after_max_attempts` |
| Ambiguous create | `test_ambiguous_create_is_never_retried_blindly` |
| Discord timeout / rejection | `test_transport_failure_marks_failed_without_creating` |
| Never deletes | `test_worker_never_deletes` |
| New product / URL change | `test_new_product_is_queued_automatically`, `test_icon_url_change_queues_the_new_image` |
| Unrelated edit is inert | `test_unrelated_edit_does_not_requeue` |
| Inventory + Rank end-to-end | `test_inventory_and_rank_resolve_prepared_icons` |
| Consumers stay read-only | `test_opening_inventory_or_rank_never_creates_or_downloads` |
| Idle costs nothing | `test_idle_pass_makes_zero_discord_calls` |
| Rename in place | `test_existing_emoji_is_renamed_in_place_not_recreated` |

### One regression found and fixed during this work
Adding `managed_name` to `_ASSET_SELECT` broke `scripts/test_shop_emoji.py`
(46 failures) because that fixture builds tables from `SHOP_EMOJI_SCHEMA`
directly and never runs the additive migration. Fixed properly by adding the
column to the base DDL, not by editing the test. All 93 pass again.

---

## 7. Files

**Modified**
- `utils/shop_emoji.py` — naming, rename API, `managed_name`, name resolver
- `utils/shop_emoji_queue.py`, `utils/shop_emoji_worker.py`, `cogs/shop_emoji_worker.py` — new
- `dashboard/api/economy_shop.py` — enqueue triggers (+49)
- `dashboard/api/shop_emoji.py` — backfill/queue/names endpoints (+95)
- `main.py` — one cog registration
- `scripts/test_shop_emoji_automation.py` — new suite
- `INVESTIGATION_PRODUCT_ICONS.md`, `tools/icon_diagnostic.py` — the previous
  investigation deliverables, carried on this branch
- `SHOP_EMOJI_AUTOMATION.md` — this report

**Deleted:** none.

## 8. Suggested rollout (not performed)

1. Merge only after your review; deploy is yours to trigger.
2. `GET /api/shop-publisher/emojis/queue` → confirm depth.
3. `POST /api/shop-publisher/emojis/backfill` → watch `queue.total` fall and
   `created`/`reused` rise in the bot log (`shop emoji pass: …`).
4. `POST /api/shop-publisher/emojis/names` with `{"apply": false}` to review the
   proposed names before any PATCH is sent.
