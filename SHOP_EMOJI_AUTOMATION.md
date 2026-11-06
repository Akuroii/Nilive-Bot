# PR #122 — Critical Review Response & Acceptance Report

Same PR, same branch. No replacement implementation, no new PR, no merge, no deploy.

| | |
|---|---|
| Branch | `arena/5e11d3cf-nilive-bot` |
| Base | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` |
| Previous head | `165802293212e65a6b709e94ebda4bb5ee8924ff` (PR #122, reviewed) |
| §0 recovery | see below — local clone had to be re-pointed at that commit; no work was lost |

---

## 0. Branch recovery (transparency)

The workspace was re-cloned mid-session, so local `HEAD` had moved back to `e0ca738`
while the reviewed commit lived on the remote. Before touching anything I proved the
working tree was byte-identical to `1658022` (five new files `cmp`-identical, four
modified files with an empty `git diff FETCH_HEAD`), then restored refs with
`git update-ref` + `git reset --mixed` — never `reset --hard`, never a clean, never a
discard. `HEAD` was `165802293212e65a6b709e94ebda4bb5ee8924ff` before the first edit of
this round. AGENTS.md rule 2/§safeguards honoured.

---

## 1. Critical — terminal retry states were not terminal

**Cause.** `mark_failed()` wrote `next_attempt_at = NULL` to mean "parked", while
`due()` treated every NULL as immediately eligible. A parked product therefore
re-entered the queue on the next tick. The old test only asserted the NULL.

**Fix.** `shop_emoji_queue` rows now carry an explicit `state`
(`queued` / `retry` / `parked`). `due()` selects **only** `queued`/`retry`.
`MARK_AUTO_ATTEMPTS` exhaustion or a non-retryable failure writes `state='parked'`.
Reactivation is deliberate: a **new URL** (generation bump) or an explicit owner
retry (`POST …/emojis/retry`, or `reactivate()`). Ordinary new entries are inserted
as `queued` and stay immediately eligible.

Also fixed while here: the attempt was being counted **twice** (once at claim, once
at failure). `mark_failed(count_attempt=…)` makes it exactly once, so the budget is
honest. *(Found by test, not review — the old bug surfaced as `attempts == 2` after a
single failure.)*

**Evidence** — `test_permanently_dead_url_is_terminal_over_many_passes`:
4 failures → `state='parked'`, then **5 further passes**: `images.fetches` unchanged,
`fake.lists` unchanged (zero Discord requests), `creates == 0`, `attempts` still 4,
`last_error` identical. Then a new URL reactivates → 1 create.
`test_owner_retry_reactivates_a_parked_product`: discovery alone does **not**
resurrect it; owner retry resets `attempts` to 0 and it then succeeds.

## 2. Critical — protect newer intent from stale completions

**Cause.** Progress writes keyed only on `(guild_id, product_id)`.

**Fix.** Every row has a `generation`. `record_attempt` claims it, and
`mark_done` / `mark_failed` / `defer_uncertain` are all
`… AND generation = ?` compare-and-swaps that return `False` when the row moved on.
`_process_row` re-reads the row after claiming and abandons (`report["stale"]`) if the
URL or generation changed. No reliance on `_pass_lock` — the Dashboard and Bot are
separate processes; the CAS is the protection.

**Evidence**
- `test_stale_generation_cannot_mark_done_or_failed` — all four writes refused
  against a superseded generation; the newer row keeps its URL, `state='queued'`,
  `last_error IS NULL`.
- `test_url_changed_during_download_keeps_newer_intent` — download of URL A blocked,
  **real Dashboard PUT** to URL B, A released: B's queue row survives, a later pass
  prepares B, Inventory resolves B's emoji, `deletes == 0`.
- `test_url_cleared_during_download_is_honoured` — no intent re-created.
- `test_product_deleted_during_download_is_honoured` — no resurrection.
- `test_dashboard_and_bot_writes_do_not_interleave_lossily`.

## 3. Critical — backfill skipped on any ready alias

**Cause.** `NOT EXISTS (… status='ready')` joined on product identity only.

**Fix.** `enqueue_guild_products()` now canonicalizes each product's **current**
`icon_url` with the same function the alias table is keyed on and asks
`_resolves_ready(app_id, guild_id, canonical_url)`: exact URL **and** active
application **and** this guild **and** `status='ready'` **and** a bound `emoji_id`.
Anything less counts as unprepared.

**Evidence** — `BackfillScopeTests` (6):
current-URL-ready → skipped; URL changed while old URL still ready → queued;
alias under another `application_id` → queued for the active app; alias under another
guild → never cross-linked; non-ready alias → preserved reconciliation path;
duplicate listings → each keeps its own URL, nothing guessed.

## 4. Naming — one stable identity for shared content

**Cause.** `_process_row` named the emoji after whichever product it happened to
process, so a shared emoji oscillated with queue order; `rename_pass` used
`updated_at DESC LIMIT 1` with no tie-break.

**Fix.** `canonical_product_label()` — the **lexicographic minimum** of the listing
names bound to that content hash — is the single deterministic label.
`canonical_label_for_asset()` resolves it from the alias table, so the create-time name
and every later rename agree, regardless of order or restart. Pure function, no state.

**Evidence** — `SharedIdentityTests` (4): three differently-named products with
**identical bytes** → 1 create, name starts `alpha_item_`; repeated passes → 0 renames;
re-processing in **reverse order** → same name; simulated restart → same name, 0
creates, 0 renames; `rename_pass` picks the same label.
`test_100_products_one_image_create_one_emoji` — 100 products, one image: 1 emoji,
`renames == 0` (the create-time name was already canonical).

## 5. Naming — suffix strength and collision safety

**Fix, three layers.**
1. Suffix widened **6 → 8 hex** (24 → 32 bits) within Discord's 32-char limit.
2. `_name_matches_content()` — a remote name is only a legitimate name for a content
   hash if it is `shop_<hash24>` **or** ends in that hash's own 8-char suffix. A
   shared slug with a different hash can never match.
3. `emoji_name_conflict()` — an emoji ID already bound to a **different** content hash
   refuses adoption (`_adopt_asset`), so one Discord emoji can never serve two images.

> Bug found by test while implementing this: my first `emoji_name_conflict()` query did
> not exclude the asset's own hash, so it always "conflicted". Fixed to
> `content_hash <> ?`. The test that caught it is
> `test_emoji_id_cannot_be_bound_to_two_content_hashes`.

**Evidence** — `NameCollisionTests` (4): two hashes sharing slug **and** first six
characters produce different names; `_name_matches_content` rejects the cross-hash
name; a planted remote emoji whose name matches the slug but not the hash is **never
adopted** while the real content still gets prepared; an ID cannot be bound twice.

## 6. Legacy rename must cover the whole catalog

**Cause.** `LIMIT 25` was applied **before** filtering, ordered by `updated_at DESC` —
and a successful rename updates `updated_at`, so the same newest page was reselected
forever.

**Fix.** `rename_all()` walks the catalog by **keyset pagination on `content_hash`**
(a stable PK component), filtering in SQL against the canonical name, so a page with
nothing to do costs no Discord calls. Cursor advances on the raw page, so correct rows
cannot stall it. One bulk owner operation; page size 25; resumable by construction
(already-correct assets are simply absent from the next run's work).

**Evidence** — `LegacyRenameTests` (4), 60 legacy emojis: 3 pages, 60 renamed,
`creates` unchanged, `deletes == 0`, **all emoji IDs unchanged**; second complete run →
`patch_requests == 0`; preview lists all 60 with 0 PATCHes; interrupted after page 1
then resumed → 25 + 35 = 60; aliases and hashes byte-identical before/after.

## 7. Reproduce the real production uncertain-create case

**Fix.** `FakeDiscord` no longer returns an always-populated create object. It now
**mirrors `DiscordAPI.create_emoji`'s real validation contract** (`_valid_emoji_id`,
name, animated, `_ownership_supported`) and can omit `application_id`/`user` from the
create response, or from the list. That is exactly the shape that produced
`عصير المجرات: uncertain / "Discord's create response did not confirm the expected emoji."`

**Evidence** — `ProductionUncertainCreateTests` (2):
- `test_uncertain_then_reconciled_by_listing`: create → `status='uncertain'`,
  `emoji_id IS NULL`, message "did not confirm"; next pass reconciles, **`creates`
  stays 1**, adopts the real ID, `status='ready'`; Inventory renders
  `:<that id>`; Rank's grid carries the same prepared token.
- `test_uncertain_stays_uncertain_when_ownership_cannot_be_proven`: 4 passes → still
  `uncertain`, `emoji_id IS NULL`, `creates == 1`, one remote emoji, `deletes == 0`,
  row still deferred (not parked, not lost).

## 8. Backfill scope and guild safety

**Fix.** Discovery is **owner-gated per guild** via a new `shop_icon_backfill`
authorisation table, written only by the owner backfill action. The cog iterates
`authorized_guilds()`, **not** `bot.guilds`, and skips guilds the bot has left. Deploying
this build can therefore never start creating application emojis for connected servers.
New/changed products remain automatic (an authorised Dashboard save). No per-product
selection anywhere.

**Evidence** — `GuildAuthorizationTests` (4): no guild authorised by default; owner
backfill authorises **only** their guild and queues only its products; an unauthorised
guild gets **no** queue row and **no** asset; two guilds sharing one URL each get their
**own** alias row (no cross-guild forging).

## 9. Quotas and operational safety

Limits **unchanged**: `GUILD_ASSET_LIMIT = 100`, `APPLICATION_EMOJI_LIMIT = 2000`,
content-hash dedupe, ownership proof, quarantine, no automatic deletion.

**Fix (reporting).** `GET …/emojis/report` now separates: eligible products,
already-ready, unprepared, distinct URLs, `assets_to_attempt_max`, shared-URL groups,
**content-reuse groups** (the real dedupe axis), quota used/limit + `quota_limited`,
uncertain, failed/missing, queue depth, and parked rows with reasons.
A Discord outage (429/5xx) is **not** charged to the product's retry budget: the pass
returns `discord_unavailable` and leaves rows untouched.

**Evidence** — `QuotaTests` (4): 101 distinct assets → exactly 100 created,
`guild_emoji_quota` recorded; repeated passes after quota → **no** further uploads and
`attempts <= MAX_AUTO_ATTEMPTS`; application-wide cap blocks new uploads with
`emoji_capacity`; URL change at the limit is quota-blocked **and reported while the old
asset/alias stay intact**. `DiscordTransportTests` (4): 429 → `discord_unavailable`,
state `queued`, `attempts == 0`, 0 creates; 5xx → same, 0 work lost; create timeout →
`uncertain`, never `ready`.

## 10. API isolation and migrations

- `GET …/emojis/queue` **no longer runs DDL.** Added `_table_exists()`; a missing table
  returns `schema_ready: false` instead of creating it. Schema belongs to `init_db` /
  dashboard import.
- `next_attempt_at()` is **guild-scoped**, matching every other number in the payload.
- New `scripts/test_shop_emoji_migration.py` (6 tests) reconstructs the **actual
  pre-feature schema** from `e0ca738`'s DDL: `managed_name` and `shop_emoji_queue` land
  exactly once; old asset IDs, names, hashes, statuses and all aliases unchanged;
  `managed_name` defaults to 0 for pre-existing rows; repeated init is idempotent;
  **4 concurrent processes** initialise the same file cleanly; the *previous revision's*
  queue table (no `state`/`generation`) is upgraded in place with its rows and attempts
  preserved; the dashboard process initialises identically.

> Real defect found here: the `state`-based index was being created **before** the
> additive column migration, so upgrading a previous-revision queue table failed with
> `no such column: state`. Index creation moved after the migration.

**Evidence** — `ReadOnlyConsumerTests`: `test_queue_endpoint_is_read_only_and_guild_scoped`
(other guild's retry time does not leak, counts are guild-scoped, `sqlite_master`
unchanged by the GET); `test_queue_endpoint_on_a_database_without_the_table` (200,
`schema_ready:false`, and the GET did **not** create the table);
`test_report_counts_are_honest` (3 products sharing one image → `quota.used == 1`,
one `content_reused_groups` entry with 3 products).

---

## 11. Test evidence (commands + results, all run for real)

| Command | Result |
|---|---|
| `PYTHONPATH=. python scripts/test_shop_emoji_automation.py` | **Ran 76 tests — OK**, process exit **0** |
| `PYTHONPATH=. python scripts/test_shop_emoji_migration.py` | **Ran 6 tests — OK** |
| `PYTHONPATH=. python -m pytest scripts/test_shop_emoji.py -q` | **93 passed** |
| `PYTHONPATH=. python scripts/test_shop_icon_resolution.py` | **Ran 13 — OK** |
| `PYTHONPATH=. python scripts/test_inventory_panel.py` | **Ran 30 — OK** |
| `PYTHONPATH=. python scripts/test_inventory_six_tabs.py` | **Ran 9 — OK** |
| `PYTHONPATH=. python -m pytest test_db_migration_concurrency.py -q` | **9 passed**, 2 subtests |
| `PYTHONPATH=. python -m pytest test_shop_categories_api.py -q` | 16 passed |
| `PYTHONPATH=. python -m pytest test_shop_designs_api.py -q` | 20 passed, 4 subtests |
| `PYTHONPATH=. python -m pytest test_shop_publications.py -q` | 23 passed |
| `PYTHONPATH=. python -m pytest scripts/test_shop_options.py -q` | 5 passed (from repo root) |
| `PYTHONPATH=. python -m pytest test_shop_json_body_api.py -q` | 1 passed |
| `PYTHONPATH=. python -m pytest test_shop_publisher_api.py -q` | **1 failed, 17 passed** — pre-existing, see below |

`scripts/test_shop_emoji.py` and `test_db_migration_concurrency.py` are pytest files
that need `OWNER_ID` (they exit during import otherwise); run as
`OWNER_ID=999999999999 DATABASE_PATH=/tmp/x.db DISCORD_CLIENT_ID=123456789012345678 PYTHONPATH=. …`.

**Pre-existing failure, not caused by this PR.**
`test_shop_publisher_api.py::test_catalog_products_carry_the_category_link` asserts the
catalog key set equals a literal list that does **not** include `app_emoji`, which
PR #121 added to that endpoint. Proven pre-existing by running it in a scratch worktree
at base `e0ca738`: **1 failed, 17 passed** — identical. I did not change that test
(it is unrelated to this task and passing it by editing it would hide a real staleness
bug in the publisher catalog contract). **Your call:** update that expectation to include
`app_emoji`, or track it separately.

### Acceptance scenario → test map

| Scenario | Test |
|---|---|
| 100 existing unique products | `test_100_products_bounded_batches_and_full_coverage` |
| 100 products with shared image hashes | `test_100_products_one_image_create_one_emoji` |
| 101 distinct vs 100 limit | `test_101_distinct_assets_against_the_100_limit` |
| Application-wide exhaustion | `test_application_wide_capacity_is_enforced` |
| Two+ guilds, strict isolation | `test_owner_backfill_authorises_only_their_own_guild`, `test_other_guild_stays_untouched_until_it_is_authorised`, `test_cross_guild_url_is_never_reused` |
| Backfill interrupted + resumed | `test_restart_mid_backfill_resumes_without_duplicates` |
| Worker restart mid-request | `test_restart_during_in_flight_download_resumes` |
| URL changed during download | `test_url_changed_during_download_keeps_newer_intent` |
| URL cleared during download | `test_url_cleared_during_download_is_honoured` |
| Product deleted during download | `test_product_deleted_during_download_is_honoured` |
| Old+new aliases coexist | `test_icon_url_change_queues_the_new_image`, `test_url_change_consumes_quota_and_is_reported` |
| Wrong-application / wrong-guild aliases | `test_alias_under_another_application_does_not_suppress`, `test_alias_under_another_guild_is_never_cross_linked` |
| Permanent 404 + recovery after replacement | `test_expired_url_is_recorded_and_retried_with_backoff`, `test_permanently_dead_url_is_terminal_over_many_passes` |
| Four-attempt parking | `test_permanently_dead_url_is_terminal_over_many_passes` |
| Owner reactivation | `test_owner_retry_reactivates_a_parked_product` |
| 429 / 5xx / timeout | `test_429_on_list_is_reported_and_backed_off`, `test_5xx_on_list_does_not_lose_the_intent`, `test_transport_timeout_on_create_is_uncertain_not_ready` |
| Uncertain creation + reconciliation | `test_uncertain_then_reconciled_by_listing` |
| No blind repeated POST | `test_ambiguous_create_is_never_retried_blindly`, `test_uncertain_stays_uncertain_when_ownership_cannot_be_proven` |
| Duplicate content, stable naming | `SharedIdentityTests` (4) |
| >25 renames | `test_sixty_legacy_emojis_across_multiple_pages` |
| Inventory/Rank read-only rendering | `test_inventory_and_rank_resolve_prepared_icons` |
| No uploads on open | `test_opening_inventory_or_rank_never_creates_or_downloads`, `test_idle_pass_makes_zero_discord_calls` |
| Restart/idempotency, concurrent workers | `test_two_concurrent_passes_never_double_create`, migration suite |

Every test above asserts on **state**, not on "exited 0": row state, attempt counts,
`creates`/`renames`/`deletes` counters, `images.fetches`, `fake.lists`, DB rows before
and after. No test was weakened to go green; the two that changed did so because the
*behaviour* changed for the better (6→8 char suffix; backfill now reports new intent
rather than re-upserts).

---

## 12. Files

**Modified:** `utils/shop_emoji.py`, `utils/shop_emoji_queue.py`, `utils/shop_emoji_worker.py`,
`cogs/shop_emoji_worker.py`, `dashboard/api/shop_emoji.py`, `dashboard/api/economy_shop.py`,
`main.py`, `scripts/test_shop_emoji_automation.py`, this report.
**Added:** `scripts/test_shop_emoji_migration.py`.
**Deleted:** none. **Unrelated code touched:** none — `cogs/wallet.py`,
`utils/rank_card_data.py`, `utils/rank_card_renderer.py` are byte-identical to the
verified production version.

## 13. Remaining risks / decisions for you

1. **`GUILD_ASSET_LIMIT = 100`** is unchanged as instructed. A catalog that already
   has ≥100 active/uncertain assets will park new work with `guild_emoji_quota` and
   report it. Raising it is a policy change I will not make silently.
2. **Old assets are never deleted**, so replacing an image consumes quota permanently —
   now visible in the report (`quota_limited`) and in `parked` rows.
3. **`test_shop_publisher_api.py` staleness** (§11) — your call.
4. **First backfill wall-clock**: 8 products/minute by default. Intentional; the
   dashboard request returns immediately.
5. Rename is cosmetic only; a rejected name leaves the working name in place and
   clears `managed_name`.
