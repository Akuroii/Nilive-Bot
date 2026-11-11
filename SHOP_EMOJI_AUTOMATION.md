# PR #122 — Acceptance Report (audit round 3, extended by round 5)

Same PR, same branch. **OPEN / NOT MERGED / NOT DEPLOYED.**

| | |
|---|---|
| PR head branch | `arena/5e11d3cf-nilive-bot` |
| PR head (round 5 started here) | `bb15cd88d89e6a960959bf23a5b70f50892839a1` |
| Round-5 commits live on | `arena/d6852d6a-nilive-bot`, branched from `bb15cd88…` — opening a PR back into the PR head branch keeps PR #122 reviewable without rewriting its history |
| HEAD audited in round 3 (now stale) | `1f73000293a4c527c39e5ee2bc1fd0f7bffbc503` |
| Base | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` |
| Round 3 report written against | `1f73000` — statements below that depend on a line number or a count were re-checked in round 5 where they matter |

Everything below is history plus the current contract. **Round 5 adds a status
table that distinguishes what is proven from what is not, and it is the section
to trust first.**

No replacement implementation, no new branch, no rewrite of the queue/worker/
preparation/reconciliation core. Inventory (`cogs/wallet.py`), Rank
(`utils/rank_card_data.py`) and `utils/rank_card_renderer.py` remain
**byte-identical to base**.

---

## Round 5 — verification status (audited `bb15cd8`)

Status vocabulary, used strictly: **FIXED AND PROVEN** (code change + a test that
fails without it), **PARTIALLY FIXED** (a proven part, a proven-open remainder),
**CONFIRMED OPEN** (reproduced, not fixed), **DESIGN REQUIRED** (needs a product
decision, not a patch), **UNVERIFIED** (no honest way to test it here),
**NOT APPLICABLE**.

| Area | Status | Evidence |
|---|---|---|
| Shared-resource deletion safety (an emoji ID bound to *another* guild's asset) | **FIXED AND PROVEN** | `emoji_name_conflict` check immediately before `DELETE`; `test_duplicate_delete_refuses_an_id_bound_to_another_guilds_asset` fails with `DID NOT RAISE` when the check is removed |
| Publication preflight: a list omission is not absence | **FIXED AND PROVEN** | `get_emoji` decides `present`/`absent`/`unknown` per missing ID; new code `product_emoji_presence_unknown`; 2 new tests, both red without the fix |
| `/api/*` JSON error contract (404/405/500, no HTML, no internals) | **FIXED AND PROVEN** | `dashboard/app.py` handlers switch on the `/api` prefix; `scripts/test_api_error_contract.py` — 9 tests, 5 failures + 1 error without the change |
| Multi-process SQLite/WAL behaviour (queue CAS, `BEGIN IMMEDIATE`, busy timeout) | **TESTED AND PROVEN — no code change needed** | `scripts/test_shop_emoji_multiprocess.py` — 7 tests over independent processes; stale generation cannot complete newer intent; one winner per generation/content hash; no lost writes |
| Application capacity (`len(remote)`) and internal guild quota | **VERIFIED, no change** | Discord documents 2000 app-owned emoji and an unpaginated `items` list; boundary tests already existed at `LIMIT` and `LIMIT-1` |
| 429 during create (ambiguity, budget, scheduling) | **VERIFIED, already covered** | `PrASafetyWorkerTests` asserts `uncertain` asset + `retry` row + `attempts == 0` + schedule ≥ `retry_after` + no further request in the pass |
| Cross-guild name/privacy (a shared emoji's name carries one guild's product label) | **CONFIRMED OPEN / DESIGN REQUIRED** | Reproduced by reading `product_names_for_asset` (all guilds) + `canonical_label_for_asset`; the trade-offs are listed under "Round-5 open items" — the fix changes visible names, so it needs your decision |
| Delivery-overlay file `__PR-A_MANIFEST__.txt` in the repository | **FIXED** | Removed; the file itself said "not part of the project: do not commit it" and named the older base `64e4871` / PR-A commit `4d00a95` |
| `utils/level_claims.py` concurrent `init_db` | **CONFIRMED OPEN (pre-existing, out of scope)** | Reproduced from two real processes: `duplicate column name: operation_id`. Byte-identical to base; not touched here |
| Live Discord API / Railway runtime | **UNVERIFIED** | No real emoji is created or deleted from this environment, by instruction. Every Discord statement above is a mocked-API statement |

### Round-5 code changes

| File | Change |
|---|---|
| `utils/shop_emoji.py` | `delete_confirmed_duplicate()` now asks whether the target emoji ID is bound to **any** asset row of the application (any content hash, any creator guild) immediately before `DELETE`, and refuses with `emoji_in_use`. `preflight_prepared_emoji_ids()` now resolves every list-omitted ID with Get Application Emoji: `present` continues, `absent` is `product_emoji_missing`, `unknown` is the new `product_emoji_presence_unknown`. Comment at the create boundary records *why* `len(remote)` is the authoritative capacity measure. |
| `dashboard/app.py` | One central error-contract switch: `/api/*` gets `{"success": false, "code", "error"}` for 400/403/404/405/500 (500 is generic and logged); HTML pages keep their rendered pages byte-for-byte. |
| `scripts/test_shop_emoji.py` | +7 tests: application-wide binding guard (refused / allowed / still guild-gated / recorded ID in a non-ready row) and publication preflight presence semantics (present-by-get, unknown, 429). |
| `scripts/test_api_error_contract.py` | New file, 9 tests. |
| `scripts/test_shop_emoji_multiprocess.py` | New file, 7 tests over real subprocesses + the deterministic two-connection WAL case. |
| `__PR-A_MANIFEST__.txt` | Deleted. |

### Round-5 open items (not fixed here, and why)

1. **Shared-emoji names can carry another guild's product label** (C3). The alias
   table is content-keyed and shared, and `product_names_for_asset()` returns the
   names of *every* guild bound to that content, so `canonical_product_label()`
   can pick another tenant's listing name. Severity is low (an emoji *name* is
   visible only where the emoji itself is already used, and the name is
   cosmetic — identity is the content hash), but it is a real cross-tenant
   detail. Options, all of which change visible names: (a) hash-only names for
   content bound to more than one guild; (b) names from the creator guild's
   labels only (rejected by instruction as an automatic default); (c) leave as
   is and document. Needs your decision.
2. **Residual race on the deletion guard.** The binding check, the fresh list and
   the ownership check run before the `DELETE`; another process could adopt that
   exact ID in between. The window is one HTTP round trip and requires an owner
   to have confirmed `DELETE <id>`; closing it completely would need an
   application-wide lease, which this feature does not have. Documented, not
   fixed.
3. **`utils/level_claims.py`** concurrent-init race (see the table).
4. **Everything Discord-facing is mocked.** Nothing here proves live Discord
   behaviour; the round-5 tests prove *our* handling of documented responses.
5. **Cross-process capacity.** `len(remote)` is read from one list snapshot;
   two processes can each believe there is one slot left. Discord's own 400 is
   the authority, and it is classified as a definite rejection, so the outcome is
   a reported failure, not a duplicate.

---

## Behaviour reference (PR-A safety hardening)

This section describes what the code does now. The audit rounds below record history
and some of their statements predate this section. Where they differ, this section wins.

### Asset states

An application emoji asset is one row per (application, content hash). It has one of
five states.

| State | Meaning |
|---|---|
| `preparing` | A process holds a claim (`attempt_id`) and may be about to send, or has sent, a create request. |
| `uncertain` | A create request may have reached Discord and the outcome is unknown. Duplicate risk. |
| `ready` | Bound to a syntactically valid emoji ID (see "Malformed rows"). |
| `failed` | Nothing was created. Safe to claim again. |
| `missing` | Discord confirmed that the bound emoji ID does not exist. |

| From | To | Condition |
|---|---|---|
| (none) | `preparing` | Insert plus claim under `BEGIN IMMEDIATE`. |
| `failed` / `missing` | `preparing` | Claim by compare-and-swap, after the name and bound-ID checks below. |
| `uncertain` | `preparing` | Only an explicit owner retry with acknowledgement, after the quarantine has elapsed. |
| `preparing` | `ready` | Create confirmed, or an owned emoji with the content-derived name adopted. |
| `preparing` | `uncertain` | Ambiguous outcome, or a claim older than 60 s found by reconcile. |
| `preparing` | `failed` | Definite failure of this request, and the claim did not start in `uncertain`. |
| `ready` | `missing` | `GET /applications/{id}/emojis/{emoji_id}` returned 404 with JSON code 10014. |
| `ready` | `uncertain` | The bound emoji ID is unusable (see "Malformed rows"). |
| `uncertain` | `ready` | Read-only reconcile finds exactly one emoji that the bot owns with the content-derived name. |

### Uncertain-origin protection

Every claim remembers where it started (`claim_origin`: new, failed, missing or
uncertain). A claim that started in `uncertain` can only return to `uncertain` or
become `ready`. These outcomes no longer turn it into `failed`:

* a definite rejection (HTTP 400, 401, 403, 404 or 405) of the recovery POST,
* a full application emoji list (2000),
* a cancellation or error before dispatch,
* HTTP 429, 5xx, a timeout, a connection reset or a malformed 2xx body.

A rejection of the second POST proves nothing about the first one. The first one
may have succeeded. The quarantine restarts (15 minutes, `QUARANTINE_SECONDS`) and
the next recovery needs acknowledgement again.

Claims that started in a fresh row, `failed` or `missing` keep the old behaviour: a
definite rejection or a full list settles them as `failed`.

### How the transport classifies a mutating request

The transport never follows redirects (`allow_redirects=False`) and never replays a
request. For POST, PATCH and DELETE:

| Response | Result |
|---|---|
| 2xx with the expected body | Success. |
| 400, 401, 403, 404, 405 | Definite rejection. Nothing changed. |
| Anything else: 1xx, 3xx, 408, 409, 413, 422, 429, 5xx, unknown codes | Ambiguous. For a create this means `uncertain`. |
| Timeout, connection failure, 2xx with a body that does not match | Ambiguous. |

A GET that fails is not ambiguous. The caller retries it later.

### What `missing` means

`missing` means one thing: Discord's Get Application Emoji endpoint answered 404 with
JSON error code 10014 (Unknown emoji) for that exact, validated application and emoji
ID. Discord lists 10014 in its JSON error code table. Its Emoji Resource page does
not describe the 404 body of this endpoint. If the real body ever differs, the code
reports "unknown" and the asset stays as it is. That direction is safe.

An emoji missing from `list_emojis()` is not enough. The code asks the exact endpoint.

| Answer for a bound ID | Result |
|---|---|
| 200 with the same ID | Present. The usual ownership and name checks run. |
| 404 with code 10014 | Absent. `ready` becomes `missing`. |
| 403, 5xx, timeout, connection failure, other 404, mismatching body | Unknown. Nothing changes. No create. |
| 429 | Rate limited. Nothing changes. The retry is scheduled. |

For `failed` and `missing` assets that still carry an emoji ID, the same question
runs before any new POST. Present means the bound emoji conflicts and needs owner
recovery (or, for `missing`, the emoji is re-bound when it is owned, not animated and
carries the content-derived name). Unknown blocks the create and reports
`emoji_state_unknown`.

For an ambiguous create with no stored ID, the code has only the application emoji
list and the deterministic name. It adopts one emoji that the bot owns and whose name
is derived from the content hash. A name match does not prove the image bytes match.
Discord exposes no image hash. Without a match the asset stays `uncertain` and only an
acknowledged owner retry can create again. A second list after the quarantine is more
evidence, not proof.

### Rate limits (HTTP 429)

* The delay is the larger of the `Retry-After` header and the JSON `retry_after`
  field. If Discord sends neither, the code uses 60 s (`DEFAULT_RATE_LIMIT_SECONDS`).
  It never waits longer than 24 hours on one answer.
* The worker pauses every Discord request in its process until that time has passed.
  The pause lives in memory, so another process learns the limit from its own 429.
* A rate-limited read before dispatch releases the claim, moves the queue row to
  `retry` no earlier than the delay, and gives back the attempt that the claim counted.
  The row does not move towards the park limit.
* A rate-limited create stays ambiguous. The asset becomes `uncertain` (or stays
  `uncertain`). The queue row waits for the longer of the delay and the 900 s recheck
  window, and the attempt is given back.
* The explicit prepare action stops sending after the first 429 in a batch.
* The Dashboard answers with HTTP 429, a JSON body with `retry_after`, and a
  `Retry-After` header rounded up to whole seconds.
* The log records the method, path, delay, scope, global flag and whether Discord
  supplied the delay.

The code does not assume route buckets for emoji endpoints. Discord states that these
routes use their own limits and may return 429.

### Dispatch and cancellation boundary

1. **Claim.** `_insert_asset_or_claim` or `_claim_existing` writes `preparing` and a new
   `attempt_id` in one `BEGIN IMMEDIATE` transaction, as a compare-and-swap.
2. **Release before dispatch.** `_PreDispatch` wraps every step between the claim and
   the POST. Any exception in that window, including `CancelledError` and
   `StalePreparation`, releases the claim with `_release_unposted_claim`. A claim from
   `uncertain` returns to `uncertain`. Other claims return to `failed`.
3. **`dispatch_started`.** `_create_claimed` sets this in-memory flag immediately
   before `create_emoji()`, with no `await` in between. It is not stored in the
   database.
4. **No release after dispatch.** Once the flag is set, nothing releases the claim. A
   cancelled or timed-out POST proves nothing. The row stays `preparing` and reconcile
   turns it into `uncertain` after 60 s (`PREPARING_STALE_SECONDS`).
5. **Release once.** A `released` flag stops nested guards from releasing twice.
6. **Cleanup.** `_finish_cleanup` runs the release to the end even when further
   cancellations arrive. It remembers the cancellation and raises it after the cleanup
   finishes. The cleanup is one indexed UPDATE under the SQLite busy timeout.
7. **Cleanup failure.** `_release_unposted_claim` logs a failed release and does not
   raise. The row then stays `preparing` and ages into `uncertain`. That is stricter
   than needed and safe.

### Retry budget and quarantine

* `MAX_AUTO_ATTEMPTS` is 4. Each claim counts one attempt. A queue row that fails with
  a business error waits 900 s times its attempt count (`RETRY_BACKOFF_SECONDS`) and
  parks after the fourth attempt.
* These outcomes do not spend the budget: an outage at pass level, a 429, an
  inconclusive Get Application Emoji answer, and `uncertain` assets (the worker never
  re-sends them).
* The quarantine lasts 900 s. It starts when a row becomes `uncertain` and restarts
  when a recovery attempt returns to `uncertain`.
* The guild limit is 100 assets and counts `ready`, `preparing` and `uncertain`. A
  released claim returns to `failed` and stops counting at once. Discord's limit of
  2000 application emojis is checked against a fresh list before each POST.

### Malformed rows

"Prepared" means status `ready` and an emoji ID that matches `^[1-9][0-9]{0,19}$`
(ASCII digits only). The check never raises. The report, Backfill, the worker, the
rename scan, the Dashboard publisher and the Inventory and Rank attachment use the
same rule. SQL uses an equivalent `GLOB` expression.

A `ready` row with an unusable ID is not reused and not marked done. The first touch
moves it to `uncertain` with `last_error` starting `invalid_ready_asset:`, keeps the
damaged ID as evidence, and starts the quarantine. The worker then defers the queue
row. The code never creates a replacement from a malformed row, because the earlier
create may have worked and only the stored ID may be damaged.

Recovery uses the existing path. Read-only reconcile adopts one emoji that the bot
owns whose name is derived from the content hash. That evidence shows the emoji came
from this pipeline for this content. It does not prove the image bytes match. If no
emoji matches, only an acknowledged owner retry after the quarantine can create again.

`GET /api/shop-publisher/emojis/report` lists these rows in `invalid_ready_assets`
for the guild. An attachment batch skips a malformed row and keeps the other icons.

### Crash and recovery limits

* A crash after dispatch leaves `preparing`. It becomes `uncertain` after 60 s. If the
  emoji was created, reconcile adopts it. If it was not, the owner needs the
  acknowledged retry.
* A crash during a release leaves `preparing`, with the same result.
* The in-process lock covers one process only. The bot and the Dashboard are separate
  processes. Between them only the database serialises work: the generation fence on
  queue rows and the compare-and-swap on assets. The lock does not stop two processes
  from listing at the same time. The compare-and-swap still lets only one claim win.
* The code cannot prove that an emoji's image matches a content hash.
* The code cannot prove that a lost response means "not created".
* Discord does not document list consistency for application emojis. The code does not
  rely on it, and it does not rely on it being strong either.

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

## Round-4 fixes: the concurrency fences, and a reproducible JS suite

Round 3 promised "no obsolete image can be uploaded". That was **not yet true at the
actual POST**, and this round closes it. Four distinct fences exist and they are *not*
interchangeable — conflating them was the root of the round-3 overclaim.

| # | Fence | Scope | Where |
|---|---|---|---|
| a | **Queue-row fence** | generation compare-and-swap on `shop_emoji_queue` | `Q.mark_done`, `Q.record_attempt`, `Q.mark_failed` (`WHERE generation=?`) |
| b | **Source / listing fence** | re-reads `shop_items` and the queue generation before work | `W._source_still_current`, `SE._fence_for_url` |
| c | **Creation-boundary fence** | revalidation after the final list/reconcile, immediately before `create_emoji()` | `SE._create_claimed` |
| d | **Requeue fence** | re-reads listing + generation *inside* the requeue write | `Q.requeue_if_current` |

### R4-1. The fence was before the window, not at the boundary (critical)

`_prepare_one()` checked staleness after the download and then called
`_create_claimed()`, which **awaits** `list_emojis()` and `_reconcile_asset()` before
`create_emoji()`. A product changing during that list call could still POST the old
image. `validate_source` is now threaded through `_create_claimed()` and
`_remap_changed_source()`, and re-run after every suspendable await.

**Documented serialization boundary** (exactly what is protected):

```
[final list_emojis + _reconcile_asset awaits]  ->  validate_source()  ->  create_emoji() POST
```

Nothing is awaited between the validator returning `True` and the POST, so that interval
is the protected one. What this does **not** claim: SQLite's single-writer rule does not
synchronise a *future* Dashboard write that lands after the POST was dispatched — a
create that is already in flight cannot be recalled. That is why the late-completion path
(`duplicate_detected`, `late_completion`, `create_uncertain`) is retained and never emits
a second POST or a delete.

### R4-2. The explicit owner path had no fence at all

`prepare_product_emojis()` derived its URL list once and passed **no** validator, so every
`validate_source` guard inside `_prepare_one` was inert for the Dashboard's Prepare
button — only the worker was fenced. It now builds `SE._fence_for_url(db, guild_id, url,
product_ids)`, which re-reads the listings that produced that URL and requires at least
one of them to still point at it ("at least one" because several products may share one
icon URL, and the batch prepares each URL once).

### R4-3. Stale requeue overwrote newer intent (critical)

`_process_row()` called `Q.enqueue_product(..., current_url, reason="icon_url_changed")`
unconditionally from a snapshot read several awaits earlier, so worker A's stale URL
could overwrite a newer queued URL. `Q.requeue_if_current()` now re-reads `shop_items`
and the queue generation inside the write transaction and refuses the write unless both
still match the snapshot it started from; `_process_row` reports `requeued_new_url` or
`stale_requeue` accordingly.

### R4-4. `/emojis/retry` reset states it did not own

`reactivate()` now requires `state='parked'`. A `queued` row is a deliberate no-op (no
new generation, so a worker mid-flight is not invalidated) and a `retry` row keeps its
`attempts`, `last_error` and `next_attempt_at` backoff. There is no silent force-retry.
All five cases are exercised **through the real API route**, not by calling
`reactivate()` directly.

### R4-5. A test that did not test its name

`test_deleted_listing_does_not_destroy_a_newer_generation` never deleted anything. It now
genuinely `DELETE`s the `shop_items` row, asserts the row is gone **and** that the
generation-CAS keeps the newer queue row, plus a stale-generation variant.

### R4-6. Reproducible JS tests (`npm ci && npm test`)

`scripts/test_shop_designer_ui.js` required a globally or `/tmp`-installed `jsdom`
(optionally via `JSDOM_PATH`) — so a clean clone could not run it. `jsdom` is now a
pinned devDependency (`31.0.0`) with a committed `package-lock.json`, `node_modules/` is
gitignored, and the harness resolves only the declared dependency. A missing `jsdom` is a
hard failure (**exit 2**) with instructions, never a silent skip.

Verified from a clean copy (tracked files only, no `node_modules`, no `.venv`, no env
vars): `npm ci` → 28 packages, then `npm test` → **27 harnesses passed, exit 0**. At
`b048dec` the same clean copy cannot even `npm ci` (no lockfile, exit 1).

### R4-7. Own regression found and fixed

Round 2/3 had the Shop Designer panel auto-fetch `/emojis/report` on mount. That broke
**five pre-existing harnesses** (`test_shop_designer_actions.js` and friends use a
read-route allowlist for page load) and made every page open cost an unnecessary Discord-
backed read. The mount-time fetch is removed; the panel loads progress only when the owner
asks. The UI harness now asserts the absence explicitly *and* that the Refresh control
still loads the report.

## Test evidence (commands, counts, real exit codes)

Each command was run from the repository root. Exit codes are the **test process's own**
(no `tail`), read directly with `$?` immediately after the command.

| Command | Result | Exit |
|---|---|---|
| `PYTHONPATH=. python scripts/test_shop_emoji_automation.py` | **Ran 165 — OK** | **0** |
| `PYTHONPATH=. python scripts/test_shop_emoji_migration.py` | Ran 6 — OK | 0 |
| `node scripts/test_shop_designer_ui.js` | **30 interaction checks, 0 failures** | **0** |
| `npm test` (27 harnesses) | **27 passed** | **0** |
| `python -m pytest scripts/test_shop_emoji.py -q` | **96 passed** | 0 |
| `python -m pytest scripts/test_shop_icon_resolution.py -q` | 13 passed | 0 |
| `python -m pytest scripts/test_inventory_activity.py -q` | 16 passed | 0 |
| `python -m pytest scripts/test_inventory_entry_safety.py -q` | 9 passed | 0 |
| `python -m pytest scripts/test_inventory_panel.py -q` | 30 passed | 0 |
| `python -m pytest scripts/test_inventory_six_tabs.py -q` | 9 passed | 0 |
| `python -m pytest scripts/test_shop_emoji_api.py -q` | 8 passed, 5 subtests | 0 |
| `python -m pytest scripts/test_shop_emoji_tls.py -q` | 1 passed | 0 |
| `python -m pytest scripts/test_shop_options.py -q` | 5 passed | 0 |
| `python -m pytest scripts/test_shop_product_management.py -q` | 50 passed, 8 subtests | 0 |
| `python -m pytest scripts/test_shop_publisher_api.py -q` | 19 passed, 15 subtests | 0 |
| `python -m pytest scripts/test_shop_categories_api.py -q` | 16 passed | 0 |
| `python -m pytest scripts/test_shop_designs_api.py -q` | 22 passed, 8 subtests | 0 |
| `python -m pytest scripts/test_shop_json_body_api.py -q` | 1 passed | 0 |
| `python -m pytest scripts/test_shop_publications.py -q` | 23 passed | 0 |
| `python -m pytest scripts/test_shop_publication_reconcile.py -q` | 30 passed | 0 |
| `python -m pytest scripts/test_shop_select_dispatch.py -q` | 9 passed, 12 subtests | 0 |
| `python -m pytest scripts/test_dashboard_redesign.py -q` | 3 passed | 0 |
| `python -m pytest test_db_migration_concurrency.py -q` | 9 passed, 2 subtests | 0 |
| `python scripts/test_shop_publisher.py` | 115/115 passed | 0 |
| `python scripts/test_dashboard_page_scripts.py` | 148 passed (**partial**: see below) | 0 |

`test_dashboard_page_scripts.py` prints `SKIPPED: acorn is not installed` at `e0ca738`
too, so it is **pre-existing and unchanged by this PR**; its two acorn-backed checks do
not run. With acorn present those checks pass (177 passed, 0 failed) — verified once in a
throwaway copy, not added as a dependency here to avoid touching the dashboard's runtime
surface.

### Round-5 test evidence (commands, real exit codes)

Run from the repository root on 2026-11-11 at `bb15cd8` + the round-5 commits,
Python 3.11.2, SQLite 3.40.1. Exit codes are the test process's own.

| Command | Result | Exit |
|---|---|---|
| `PYTHONPATH=. python -m pytest scripts/test_shop_emoji.py -q` | **209 passed** (was 202) | 0 |
| `PYTHONPATH=. python scripts/test_shop_emoji_automation.py` | **Ran 173 — OK** | 0 |
| `PYTHONPATH=. python scripts/test_api_error_contract.py` | **Ran 9 — OK** (new) | 0 |
| `PYTHONPATH=. python scripts/test_shop_emoji_multiprocess.py` | **Ran 7 — OK** (new) | 0 |
| `PYTHONPATH=. python scripts/test_shop_emoji_migration.py` | Ran 6 — OK | 0 |
| `PYTHONPATH=. python scripts/test_shop_emoji_api.py` | Ran 8 — OK | 0 |
| `PYTHONPATH=. python scripts/test_shop_json_body_api.py` | Ran 1 — OK | 0 |
| `PYTHONPATH=. python scripts/test_shop_publisher_api.py` | Ran 19 — OK | 0 |
| `PYTHONPATH=. python scripts/test_shop_publication_reconcile.py` | Ran 30 — OK | 0 |
| `PYTHONPATH=. python scripts/test_shop_categories_api.py` | Ran 16 — OK | 0 |
| `PYTHONPATH=. python scripts/test_shop_designs_api.py` | Ran 22 — OK | 0 |
| `PYTHONPATH=. python -m pytest scripts/test_shop_publications.py -q` | 23 passed | 0 |
| `PYTHONPATH=. python -m pytest test_db_migration_concurrency.py -q` | 9 passed, 2 subtests | 0 |
| `PYTHONPATH=. python scripts/test_shop_publisher.py` | 115/115 passed | 0 |
| `PYTHONPATH=. python -m pytest scripts/test_shop_options.py -q` | 5 passed | 0 |
| `PYTHONPATH=. python -m pytest scripts/test_shop_product_management.py -q` | 50 passed, 8 subtests | 0 |
| `PYTHONPATH=. python -m pytest scripts/test_shop_icon_resolution.py -q` | 13 passed | 0 |

Not run in this round: the jsdom/`npm test` harnesses (no frontend file was
touched by round 5; they were green in round 4), and anything that would touch
live Discord or Railway.

**Removal proofs** (the fix is reverted, the suite is re-run, the fix is
restored — the commands are in the commit messages):

| Fix reverted | Observed failure |
|---|---|
| application-wide binding check | `test_duplicate_delete_refuses_an_id_bound_to_another_guilds_asset`: `DID NOT RAISE ShopEmojiError` — the DELETE proceeded |
| binding protection for non-ready rows | `test_duplicate_delete_refuses_an_id_the_asset_row_still_records` fails |
| Get-based presence confirmation | the two presence tests report `product_emoji_missing` instead of `product_emoji_presence_unknown` / success |
| `/api` JSON error contract | 5 failures + 1 error in `scripts/test_api_error_contract.py` |

### Round-4 test → fix map (each was RED before its fix, GREEN after)

| Test | Failed before with | Fix it proves |
|---|---|---|
| `PostBoundaryFenceTests.test_url_changed_during_the_final_list_never_posts_the_old_image` | `the POST boundary must revalidate: URL A must not be created` | R4-1 (worker create) |
| `PostBoundaryFenceTests.test_cleared_image_during_the_final_list_never_posts` | create happened after the icon was cleared | R4-1 |
| `PostBoundaryFenceTests.test_the_validator_is_invoked_immediately_before_create_emoji` | `events=['list','list','create']` — no validation after the final list | R4-1 ordering, behaviourally |
| `test_owner_prepare_never_uploads_an_image_the_listing_stopped_using` | `the obsolete image was uploaded after the listing moved away from it` | R4-2 |
| `test_uncertain_recovery_post_is_fenced_at_the_boundary` | `the recovery POST ran for an image the listing had already replaced` | R4-1 (recovery path) |
| `test_changed_source_remap_post_is_fenced_at_the_boundary` | `the remap uploaded the replacement image after the listing reverted` | R4-1 (remap path) |
| `StaleRequeueTests.test_requeue_from_a_stale_snapshot_does_not_overwrite_a_newer_url` | `4 != 3` (generation bumped by a stale worker) | R4-3 |
| `StaleRequeueTests.test_requeue_is_refused_when_the_listing_moved_on_again` | write accepted from a dead snapshot | R4-3, source half |
| `StaleRequeueTests.test_the_current_url_is_still_prepared_after_a_stale_requeue` | — | R4-3 converges: current URL prepared, superseded URL never uploaded, exactly 1 create |
| `RetryStateContractTests` (5) | queued and retry rows were reactivated | R4-4, via the real `/emojis/retry` route |
| `StaleQueueDeletionTests` (5) | never deleted anything | R4-5 |

The pytest files need `OWNER_ID` (they exit during import without it):
`OWNER_ID=999999999999 DISCORD_CLIENT_ID=123456789012345678 DATABASE_PATH=/tmp/x.db PYTHONPATH=. …`

**109 → 104 automation tests: none removed.** The 104 are the previous 76 plus 28 new;
test counts are not the claim — every test above asserts on row state, attempt counts,
`creates`/`renames`/`deletes`/`patch_requests`/`patched_emoji_ids` counters, `images.fetches`
and pre/post database rows.

### Automation suite composition (165)

`BulkBackfillTests` 5 · `DuplicateContentTests` 4 · `GenerationFencingTests` 4 ·
`BackfillScopeTests` 6 · `SourceFailureTests` 4 · `DiscordFailureTests` 5 ·
`ProductionUncertainCreateTests` 2 · `GuildAuthorizationTests` 4 · `QuotaTests` 4 ·
`DiscordTransportTests` 4 · `SharedHashScaleTests` 1 · **`BackfillReactivationTests` 3** ·
**`BackfillIdempotenceTests` 4** · **`ReadyDefinitionTests` 6** ·
**`CanonicalNameLookupTests` 4** · **`RenameCursorScopeTests` 2** ·
**`DashboardBulkActionTests` 5** · **`DashboardControlPresenceTests` 4** ·
`FutureChangeTests` 5 · `NamingTests` 7 · `SharedIdentityTests` 4 · `NameCollisionTests` 4 ·
`LegacyRenameTests` 4 · `ConcurrentWorkerTests` 2 · `ReadOnlyConsumerTests` 7

Round 4 adds **13**: `PostBoundaryFenceTests` 3, `StaleRequeueTests` 4,
`RetryStateContractTests` 5, and one extra case in `StaleQueueDeletionTests` (5, from 4).
`FenceCoverageTests` keeps its 5 structural checks, but they are no longer the *only*
evidence for the recovery and remap paths — the two new pytest boundary tests prove
those behaviourally instead.

Rounds 5–7 add **28**: `StaleWriteInterleavingTests` 10 (requeue, Backfill, owner-retry and
two out-of-order Dashboard saves, each swept across every statement boundary and drained to
convergence, plus three "consumed queue row is never resurrected" cases), `PostBoundaryReleaseTests` 11 (the real POST boundary, release/quota/alias/shared
asset behaviour) and `PreDispatchCancellationTests` 7 (cancellation or an exception at every
pre-dispatch await, cancellation after dispatch, which must never release the claim, and a
second cancellation during claim cleanup, which must not abandon it).

(bold = added in round 2)

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
| `utils/shop_emoji_queue.py` | `state`/`generation` columns, `shop_icon_backfill` authorisation, same-URL no-op, **`requeue_if_current()` (listing + generation re-read inside the write)**, **`reactivate()` restricted to `state='parked'`** with `expect_generation`/`expect_source_url`, dict return `{queued, reactivated}`, `usable_emoji_id_for()` + `classify_catalog()` (the single readiness predicate), **generation-fenced `clear_product()`**, `stats()` separating deferred/retrying/uncertain, CAS writers, race-safe migration |
| `utils/shop_emoji.py` | 8-char suffix, readable naming, `canonical_product_label`, `product_names_for_asset`/`_canonical_or_raw` (canonicalised name lookup), `emoji_name_conflict`, `_add_column` race-safe migration, **`validate_source` threaded through `_create_claimed`/`_remap_changed_source` and re-run at the ACTUAL POST boundary after the final list/reconcile**, **`_fence_for_url` so the explicit owner path is fenced too** |
| `utils/shop_emoji_worker.py` | `_process_row` fencing, **`_source_still_current()` fence injected into `_prepare_one`**, **conditional requeue via `requeue_if_current()` instead of an unconditional enqueue**, **fenced `clear_product`/`mark_done` in every deletion branch**, guild-scoped rename cursor, `_labels_for` canonicalisation, rename results carry identity, `rename_all`/`rename_pass` |
| `cogs/shop_emoji_worker.py` | NEW — worker + authorised-guild discovery loops |
| `dashboard/api/shop_emoji.py` | backfill (authorise + bulk queue), read-only queue/retry/names/report; report rebuilt on `classify_catalog()` (`invalid_source_products`, `fully_prepared`); `/retry` reactivation fenced; no DDL on GET |
| `dashboard/api/economy_shop.py` | auto-queue hooks on create / `icon_url` change |
| `dashboard/templates/manage/shopdesigner.html` | NEW owner-only “Prepare all Shop product icons” panel |
| `dashboard/static/js/shop-designer.js` | backfill handler, progress renderer (invalid URLs, deferred vs uncertain), parked-retry switch, success message preserved across refresh, control re-enabled after failure |
| `main.py` | load the new cog |
| `test_shop_publisher_api.py` | stale catalog key set now includes `app_emoji` |
| `scripts/test_shop_emoji_automation.py` | **165 tests** (`PostBoundaryFenceTests`, `StaleRequeueTests`, `RetryStateContractTests`, rewritten `StaleQueueDeletionTests`) |
| `scripts/test_shop_designer_ui.js` | NEW — real jsdom interaction test: renders the template, mounts the real module, clicks the real button, asserts the outgoing request, and asserts that mounting fetches nothing (**30 checks**) |
| `scripts/test_shop_emoji_migration.py` | NEW — pre-feature schema, idempotent/repeated init, race-safe concurrent init, previous-revision queue upgrade |
| `tools/icon_diagnostic.py`, `INVESTIGATION_PRODUCT_ICONS.md` | read-only diagnostic used for the investigation |
| `scripts/test_shop_emoji.py` | 3 new boundary regressions (owner prepare, uncertain recovery, changed-source remap) — 96 tests |
| `package.json`, `package-lock.json`, `.gitignore` | pinned `jsdom` devDependency + committed lockfile so `npm ci && npm test` works from a clean clone; `node_modules/` ignored |

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
6. **The UI interaction test needs jsdom**, now a pinned devDependency (`31.0.0`) with a
   committed `package-lock.json`, so `npm ci && npm test` works from a clean clone with no
   `/tmp` install and no environment variable. A missing `jsdom` exits **2** with
   instructions rather than passing vacuously. No browser binary is available in this
   environment, so this is a real DOM + real event dispatch in jsdom, **not** a
   headless-browser render — and the Dashboard "control present" checks remain static
   source assertions, which is a weaker claim than the behaviour tests and is labelled as
   such.
7. **`stats()["deferred"]` keeps its old meaning** (every row in a future backoff) for
   backwards compatibility, but every UI label and the report now separate
   `retrying_backoff` from `uncertain`. Anything else reading `deferred` should be
   reviewed with the same distinction in mind.
