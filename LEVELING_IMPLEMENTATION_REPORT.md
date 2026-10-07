# Leveling / Dashboard implementation report

> Historical implementation report. Its earlier cooldown, spam-detector, and runtime design descriptions predate the current Leveling architecture. For current defaults, one-time migration, penalty budget, test coverage, and verification boundaries, use `XP_PACING_SIMULATION.md` and the active production code/tests.

Scope: the slices commissioned from `LEVELING_DASHBOARD_INVESTIGATION.md`
(unchanged). Nothing is committed — the working tree holds all changes.
Follow-up decisions were taken on 2026-10-06 and are applied (see §5).
**Level reward roles** are an opt-in progression controlled by `remove_old_reward_role` — see §7
(placed after §4 so the item numbering of the original plan is preserved).
The second follow-up slice (Dashboard script lifecycle audit, anti-spam penalty semantics,
leave/rejoin determinism, missing-role reconciliation, blacklist boundary) is in §8; its decisions
D1 = (B) and D2 = (B) are implemented there, D3 is deferred, and the full evidence trail is in
`LEVELING_FOLLOWUP_AUDIT.md`.

---

## 1. Files changed

### Bot side
| file | change |
|---|---|
| `cogs/leveling.py` | `/level` **Claim All button state** (item 1) + **Stats view** rework (item 8); `_claim_age` / `_claim_reward_text` / **`claim_result_footer`** helpers; **`/resetleaderboard` no longer arms the scheduled auto-reset** (UPDATE-only reset-config write); the hard-coded cooldown fallback is now 20; **anti-spam penalty is a real deduction** (D1 = B) via `apply_spam_penalty()` + the approved anti-spam **embed reply** to the offending message, once per spam window (`SPAM_WARNING_TEXT`, `_spam_warning_due`, `_warn_spam`) |
| `utils/xp_calculator.py` | `LEVELING_CONFIG_DEFAULTS` (the single definition of "unconfigured"), `get_leveling_config()` returns **stored row ⊕ defaults** (S3), default cooldown **20 s** (pacing); **`apply_spam_penalty()`** — floor at zero + level recomputed in one transaction (D1 = B) |
| `utils/level_claims.py` | `list_claims()` also returns `created_at`, `fulfilled_at`, `source` — read-only, for the Stats history. **Level reward roles can be a progression** (§7): `superseded_role_ids()` + `enforce_role_progression()` are gated by `remove_old_reward_role`. **Membership reconciliation** (D2 = B): the shared `reconcile_role_progression()` reads fulfilled claims only; the join listener restores all accumulated roles when OFF, while the full Claim All pass uses the highest fulfilled set when ON; no new engine or ledger writes |
| `database.py` | `leveling_config.xp_cooldown_seconds` column default 30 → 20 (schema default only; no migration, no rewrite of stored rows) |

### Dashboard API
| file | change |
|---|---|
| `dashboard/api/leveling.py` | **S2**: whole-number validation with structured JSON errors, non-fatal post-commit audit, real persistence failures still surface. **S3**: `GET /leveling/config` returns the same effective config the runtime uses; `spam_window_seconds` added to the write path; default cooldown 20. `remove_old_reward_role` is a **live setting again**: validated 0/1, written by the upsert, and returned by the GET (§7) |

### Templates (24 of the 29 changed files)
| file | change |
|---|---|
| `dashboard/templates/systems/leveling.html` | S1 wrapper + publishes; new **Spam window (seconds)** field and its load/save wiring (S3); prestige-multiplier labels no longer call `currencyNameFor()` before `dashboard.js` loads; **`remove_old_reward_role` checkbox + hint** ("Remove old reward role when new one is given"), loaded and saved like every other setting (§7) |
| `dashboard/templates/systems/minigames.html`, `.../minigame_builder.html` | S1 wrapper + publishes; `REWARD_TYPE_LABELS` built lazily so `currencyLabelText()` is not called at page-eval time |
| 21 other templates (`systems/*`, `manage/*`, `config/*`, `general/*`, `server_select.html`) | S1 wrapper + publishes only (no logic change) |
| `dashboard/templates/base.html`, `dashboard/static/js/dashboard.js` | **untouched** |

### Tests and verification coverage
| file | covers |
|---|---|
| `scripts/test_leveling_reset_config.py` | `/resetleaderboard` does not arm auto-reset for an unconfigured guild; effective-config precedence; cooldown default 10; approved spam defaults (window 20, threshold 10); `remove_old_reward_role` defaults OFF, validates 0/1, and stored ON is read by runtime |
| `scripts/test_dashboard_page_scripts.py` | S1 + item 2: for **all 36 dashboard routes**, the page script is emitted **once**, sits inside `#content-area`, parses under `node --check`, is re-evaluation safe (no top-level `const`/`let`, checked with acorn), and every function the markup calls from an inline handler still resolves by bare name (top-level declaration, `window`/`globalThis`/`__neroGlobal` publish, or the shared `dashboard.js`); the nested `{% block scripts %}` is gone |
| `scripts/test_level_reward_role_progression.py` | The §7 toggle in both modes: exclusive-ON first reward/replacement/multi-level skip/unrelated roles preserved/shared role id/failed add+failure retries, exclusive-OFF accumulation, and both switch directions (OFF→ON supersedes on the next delivery; ON→OFF keeps the older role) with the claim ledger unchanged throughout |
| `scripts/simulate_negative_xp.py` | Item 3 audit: the six XP write paths and their floors, the shipped write (consistent) beside the legacy stale-level write, the rejected no-floor variant (negative progress, debt, rank), and the re-cross/duplicate-reward proof against the real claim ledger; refuses to run if the production shapes it models drift |
| `scripts/test_spam_penalty.py` | Item 3 / D1 = B: real deduction, floor at zero, consistency at every boundary, demotion creates no entitlement and revokes no fulfilled claim, re-earning pays once, the approved anti-spam **embed reply** (byte-compared text, reply-not-channel-send, footer carries the XP actually deducted, one reply per window while every spamming message is still penalised, a failed reply never rolls the penalty back), penalty 0 / detection off unchanged |
| `scripts/test_xp_safety_audit.py` | Final completeness audit: the `levels`-writer inventory (new write paths fail until classified), per-writer clamps and level recomputes, the spam gate's position and liveness, the penalty's claim-free body, claim idempotency, the D2 reconciler's boundaries, no sweep / no background reconciliation, unrelated-role preservation, the dashboard harness's anti-degradation guards, and the untouched systems + frozen settings |
| `scripts/test_rejoin_reconciliation.py` | Items 4–5 / D2 = B: the real join callback restores every fulfilled role with replacement OFF and only the highest fulfilled set with replacement ON; API-saved toggle, idempotency, partial same-Level failure/retry, unrelated roles, and no XP/economy/claim/reward/ledger mutation |
| `scripts/test_leveling_reward_e2e.py` | Production Dashboard API create + supported DELETE/POST replacement of role/currency/product/boost definitions → SQLite/API/UI readback → `give_reward()`/`record_crossing()` → `claim_available()` delivery; independent same-Level claims, duplicate protection, and partial higher-role retry |

New docs: `XP_PACING_SIMULATION.md` (pacing simulation), `LEVELING_FOLLOWUP_AUDIT.md` (items 3–5 audit + resolved decisions).
Diff size: **29 files, 1160 insertions, 142 deletions**, plus the new (untracked) documents and the five test/simulation scripts; the 24 template files contribute the S1 wrapper/publish blocks, of which the only deleted lines are the 24 `{% block scripts %}` + 24 `{% endblock %}` pairs plus the 14 lines of the three documented label/toggle edits.

---

## 2. Exact behaviour fixed

**S1 — duplicated page-script emission / dead nav (24 templates).**
Before: each page declared `{% block scripts %}` *inside* `{% block content %}`, so `base.html` emitted the child's inline page script **twice**. The second evaluation of any page with a top-level `const`/`let` threw `SyntaxError: Identifier 'X' has already been declared` and that copy died; on an htmx navigation the re-inserted script died the same way, so the page's init never re-ran (`/leveling` nav = **0** API calls; `/minigames` nav = 0 and its load-time init was dead too).
After: the script is emitted **once** (block tags removed), still inside `#content-area`, evaluated inside one IIFE with `var __neroGlobal = (typeof window !== 'undefined') ? window : globalThis;` and one `__neroGlobal.<fn> = <fn>;` per top-level declaration — so re-evaluation on every nav is legal and the page API (and every inline `onclick=`/`onchange=` handler) resolves exactly as before. `/leveling` now performs its 9 config-panel calls on the first load and on **each** navigation, with no duplicates.

**S2 — Leveling save contract (`POST /api/leveling/config`).**
Before: `int(data.get(...))` on 14 fields. A blank input (`""`, which is what a cleared number box sends), `"30.5"` or a negative value raised `ValueError` → Flask **HTML 500** → `ajaxSave`'s `res.json()` threw → the page showed *Connection error* and nothing was written. A post-commit `log_action()` failure also returned 500 although the config **had** been saved. An unparseable body silently saved all defaults.
After: validation runs before any write, with the form's own wording — `"XP per word is required"`, `"Cooldown (seconds) must be a whole number"`, `"Max XP per message must be between 0 and 100000"`, `"Request body is not valid JSON"` — returned as `400 {"success": false, "error": …}`; bounds mirror the form's `min`/`max` with headroom; a DB failure returns `500 {"success": false, "error": "Could not save leveling config: …"}` and is logged (never masked); an audit-row failure is logged and the save still reports success. Absent keys keep their defaults as before.

**S3 — effective config display.**
Before: `GET /leveling/config` returned the raw row, or `{}` for a guild with no row → a fresh guild's page showed everything OFF/blank while the bot was running the full defaults (enabled, 1/5/50 XP, 30 s cooldown, voice 3/min, spam 3/10). A legacy row's NULL column read the same as "off".
After: one shared helper (`utils.xp_calculator.get_leveling_config`) returns defaults ⊕ row for both the runtime **and** the API, so the page shows what the bot enforces. `spam_window_seconds` — which *was* live (`cogs/leveling.py` reads it for the anti-spam window) but had no UI — is now editable (validated 1–3600 s, default 10) and included in the INSERT/UPDATE. Dead settings were not added to the surface; the dead one that was there is gone.

**Item 1 — `/level` Claim All.**
Before: primary + always enabled. After: **green (`ButtonStyle.success`) + enabled** iff at least one claim is `pending` or a retryable `failed`; **gray (`secondary`) + disabled** otherwise (fulfilled-only, or a claim inside its 60 s lease). The state is derived from `list_claims()` on card creation, after `claim_all`, and on every Level/Stats refresh. Presentation only — no engine, schema or entitlement change.

**Item 8 — `/level` Stats.**
Before: Level / Total XP / Progress / Ready to claim / Fulfilled — i.e. the Level page's numbers again. After, from the existing `level_reward_claims` rows only: **Claimed** (`1 of 3 crossed rewards` + latest with age), **Waiting** (pending and retryable-failure counts + oldest with age), **Last failure** (the frozen reward + `last_error`, only when a failure exists), **Highest unclaimed** (reward + status). No second tracking system, no schema change; `/rank` untouched.

**Decided follow-ups applied this pass**
* **Pacing (cooldown only):** `xp_cooldown_seconds` default **30 → 20 s** in every live default site (runtime fallback, effective-config dict, dashboard validator, DB column default). `xp_per_word`, the clamps, `spam_threshold`/`spam_xp_penalty`, the level curve and everything else are untouched. Impact: the cooldown ceiling rises 720 → 1,080 XP-messages per 6 h day; L100-in-300-days moves from unreachable (best case 417 days) to reachable at ~289 days **only** by a member saturating the cooldown (1 message every 20 s for the whole 6 h, every day, ≥12-word messages). Anyone under 1 message/20 s sees **no change at all**.
* **Historical interim note, superseded by §7:** an earlier pass treated `remove_old_reward_role` as dead and removed its form/API wiring. That was later reversed; the setting is live again, persisted through the Dashboard and used by the runtime as documented in §7. `levelup_embed_data` remains untouched (no UI, no reader).
* **`/resetleaderboard` (isolated fix):** the reset-config write is now `UPDATE … WHERE guild_id = ?` instead of an upsert, so a manual reset can no longer create/enable a scheduled auto-reset for a guild that never configured one. Guilds with an existing config keep their `enabled`/period and their auto-reset behaviour; the reset itself (archive → zero XP/level) is unchanged.
* **S4 (voice “require another participant”): deferred** by decision. Voice XP, the `≥2 real members` engine gate, Missions, MVP and `activity_stats` are all untouched; the toggle and the participant redesign stay on the follow-up list.

**Two same-class bugs found and fixed while verifying** (both pre-existing, independent of S1):
* `/minigames` + `/minigames/builder`: `currencyLabelText()` (defined in `dashboard.js`, loaded *after* the page script) was called while building `REWARD_TYPE_LABELS` at page-eval time → `ReferenceError` that aborted the rest of the script, so `mgState`, `switchTab('config')` and the init fetches never ran. The map is now built on first use; the pinned `currencyLabelText('coins'/'diamonds')` text is unchanged.
* `/leveling`: the prestige-multiplier card called `currencyNameFor()` at load and threw, leaving it stuck on “Loading…”. It now reads the same `window.__CURRENCY__` until `dashboard.js` is present.

---

## 3. Tests / harness results (all re-run after the final edits)

| check | result |
|---|---|
| `npm test` (13 JS harnesses, incl. the 275-check currency-icon suite) | **13/13 passed** |
| **new** `scripts/test_spam_penalty.py` (D1 = B) | **41 passed, 0 failed**; mutation-tested — dropping the level recompute fails 3 checks, dropping the floor fails 4, sending the warning to the channel instead of replying fails 6, showing the configured instead of the real amount fails 1, warning when nothing was deducted fails 1, ignoring the rate-limit fails 1 |
| `scripts/test_xp_safety_audit.py` (latest source audit) | **111 passed, 0 failed**; D2 checks cover persisted settings, OFF/ON restoration, no ledger mutation, and no sweep/background reconciliation |
| `scripts/test_rejoin_reconciliation.py` (current D2 path) | **18 passed, 0 failed**; real join callback, OFF/ON API persistence, missing/deleted role retry, partial same-Level retry, idempotence, and no durable XP/economy/claim mutation |
| `test_slice1_leveling_gate.py` … `test_slice4_boost_claims.py`, `test_vi_lifecycle.py`, `test_phase1_runtime.py`, `test_phase2_backend.py` | **all pass** (`test_rank_integration.py` needs `uharfbuzz`/`freetype-py`; passes once installed) |
| **new** `scripts/test_dashboard_page_scripts.py` | **177 checks, 0 failed** across **all 36 routes**; mutation-tested (blanking one `__neroGlobal` publish line leaves 17 markup handlers unresolved, so the harness fails) |
| **new** `scripts/test_leveling_reset_config.py` | **17 passed, 0 failed** (no auto-reset arming; configured guild unchanged; cooldown default 20; stored row wins; toggle defaults OFF, validates 0/1, stored ON is read) |
| `scripts/test_level_reward_role_progression.py` | **46 passed, 0 failed** — both toggle modes, both switch directions, independent same-Level roles, partial add retry, and deferred replacement through real `record_crossing` → `claim_available` |
| S1 A/B lifecycle sweep (24 routes × load/nav × before/after) | 11 routes flagged, all `[nav re-init 0 calls]` (DOM-only init, no API call — verified by script-execution instrumentation); **zero** critical errors; `/leveling` 9/9 load and 9/9 nav; `/minigames`, `/minigames/builder` init restored (0 → 3 calls); `/trade`, `/mvp`, `/tickets`, `/config/general`, `/embed-builder`, `/server-select` show one set of calls instead of two |
| S1 same-realm re-evaluation probe | all 24 routes: second evaluation legal, every markup handler resolves |
| S1 containment audit | 24/24 templates: every pre-existing non-blank page-script line byte-identical; only the block tags (+ the documented label/toggle edits) removed |
| S2 probe (real Flask client + scratch DB) | **17/17** — valid form payload saves (incl. the new window field); blank / `30.5` / `-1` → field-named JSON 400 with nothing written; non-JSON body → JSON error; audit failure → 200 + value persisted; DB failure → JSON 500 and the old value kept; partial payload keeps defaults (cooldown 20); GET round-trips |
| S3 end-to-end (real Flask server + jsdom + real `dashboard.js`) | **18/18** — fresh guild shows ON / 1 / 5 / 50 / **20** / voice 3 / spam 3·10·10; dead toggle absent from the form **and** from the API payload; API and runtime agree; **Save with no changes → success, row written, GET unchanged**; editing only the window preserves every other value; no page errors; prestige card renders |
| `/level` Stats probe | **14/14** — no Level/Total XP/Progress fields; counts, ages, failure reason, highest unclaimed; nothing exceeds Discord's 1024-char field limit |
| Claim-All state probe | 8/8 cases (none → gray+disabled; pending/failed → green+enabled; fulfilled-only and in-lease processing → disabled) |

---

## 4. XP pacing simulation (item 5)

Full matrix in **`XP_PACING_SIMULATION.md`** (live gate order: spam → cooldown → clamp; real curve
`floor(100·L^1.5)`; L100 = 4,050,079 XP = 13,500 XP/day; jittered arrivals, 6 h/day, 300-day target).

| scenario (voice on, 10 words) | XP/day | L50 | L75 | L100 |
|---|---:|---:|---:|---:|
| casual 200 msg/day, cd 20 s (default) | 2,752 | 264 d | 720 d | 1,472 d |
| heavy 800 msg/day, cd 20 s | 5,144 | 141 d | 386 d | 788 d |
| rapid 1,440 msg/day, cd 20 s | 5,000 | 145 d | 397 d | 811 d |
| rapid 1,440 msg/day, 12 words, cd 20 s | 6,208 | 117 d | 320 d | 653 d |
| 3-msg bursts every 45 s, cd 20 s | 1,080 (chat cancelled by spam penalties) | 672 d | 1,835 d | >10 y |

* The **cooldown ceiling** is the first hard wall; after the change it is 1,080 XP-messages per 6 h day (was 720).
* L100 in 300 days still needs 13,500 XP/day ⇒ at the new ceiling, ≥12-word messages at one message every 20 s for the whole 6 h, every day (≈289 days). Realistic volumes (200–800 messages/day) are unaffected by the cooldown change and pace at multi-year.
* **Open side finding (declined for now):** the anti-spam defaults make an ordinary 3-message burst (3 s apart) net **zero** XP at ≤10 words, because the 10 XP penalty cancels a 10 XP message. Candidate pair (`spam_threshold` 5, `spam_xp_penalty` 3) remains available as an independent follow-up.

---

## 7. Level reward roles: accumulate by default, progression when `remove_old_reward_role` is ON

**Audit first — the delivery path.** A guild's `leveling_rewards` rows are a progression
(Level 5 → Role 5, Level 10 → Role 10, …). The reward path is:

`Dashboard API` → persisted `leveling_rewards` / config → production `give_reward()` crossing →
`record_crossing()` pending entitlements → member runs `/level` → **Claim All** →
`claim_available()` (reserve under `BEGIN IMMEDIATE` → per-claim fulfilment → `deliver_role()` →
`_mark(fulfilled)`).

`leveling_rewards` is written by the Dashboard API and re-read by `_definitions()` at crossing time.
`deliver_role()` is used by the existing claim delivery loops (permanent and temporary roles) and by
`reconcile_role_progression()` for membership-only restoration. Separately, the production
`on_member_join` listener calls that reconciler for already-fulfilled roles. Delivery remains
centralized in the claim engine; reconciliation never creates or pays an entitlement.

**The setting, and why it is a real toggle.** `remove_old_reward_role` is a live product setting
again (a guild config, not dead code):

| Where | What it does now |
|---|---|
| DB | `leveling_config.remove_old_reward_role INTEGER DEFAULT 0` — column kept, **OFF by default** |
| Dashboard → Leveling | checkbox **"Remove old reward role when new one is given"** + hint in the announcement card; loaded with the rest of the config and saved by the normal Save |
| Config API | validated as 0/1, written by the upsert, returned by `GET /leveling/config` — runtime and Dashboard agree (S3 requirement) |
| Runtime | `claim_available()` reads it once when it processes role claims; OFF never removes roles during delivery. The join reconciler also reads the persisted setting: OFF restores every fulfilled role claim, while ON restores the highest fulfilled set and enforces replacement after successful delivery |

**Behaviour.** Two helpers in `utils/level_claims.py`:

* `superseded_role_ids(db, guild_id, user_id, level, role_id)` — reads the **claim ledger**, not
  live config: the role ids of *fulfilled* `role` claims at levels below the one being delivered,
  minus any role that the highest fulfilled level also grants. An empty list means "nothing to
  remove".
* `enforce_role_progression(member, guild_id, user_id, level, role_id)` — removes those roles from
  the member (skipping roles they no longer hold or that were deleted from the guild), returning
  what it actually removed.

The existing `claim_available()` role loop marks a failed Discord add retryable. Replacement is a
separate post-delivery reconciliation: a lower-role removal failure does not reopen the already
fulfilled higher-role claim, and the next full Claim All pass can retry that membership reconciliation.
The delivery-time removal is gated by `exclusive_roles`; with the setting OFF, roles are never removed.

**Both modes, tested** (`scripts/test_level_reward_role_progression.py`, 46 checks):

| mode | behaviour |
|---|---|
| OFF (default) | reaching Level 10 adds the Level 10 role and **keeps** Level 5; unrelated roles untouched; both claims fulfilled; repeated passes are no-ops |
| OFF → ON | flipping the setting touches no ledger row and creates no entitlement; the **next** delivery supersedes the accumulated lower roles |
| ON | first reward delivered; higher-level delivery removes the superseded lower roles; a role shared with the highest fulfilled level is kept; failures keep the claim retryable |
| ON → OFF | a new role is added and older roles are **not** removed; claims stay fulfilled and unduplicated |

**Deliberate boundaries**

* Only `track='role'` claims are considered — currency, Shop, boost and temp-role claims keep their
  exact semantics (the Shop temp-role loop is untouched).
* Roles with no fulfilled role claim are never removed, so manually granted roles, reaction roles,
  booster roles and other systems' roles survive.
* A role id configured at two levels is never removed (it is in the "keep" set of the highest
  fulfilled level).
* The rule is evaluated from the ledger. A member who has lost a fulfilled role is reconciled on
  their next join (all fulfilled roles when OFF; the highest fulfilled set when ON), or during the
  qualifying full Claim All pass when ON. There is deliberately no guild-wide/background sweep.
* No schema change; the setting is exposed in the Dashboard/API and read by the runtime; `levelup_embed_data`
  is untouched.

**Tests — `scripts/test_level_reward_role_progression.py`, 46 checks, 0 failed** (uses the real
`record_crossing` → `claim_available` path with a fake Discord member; rejoin coverage is in
`scripts/test_rejoin_reconciliation.py`):

| # | case | asserted |
|---|---|---|
| 1 | first Level reward role | role added, **nothing** removed, unrelated role survives, claim fulfilled |
| 2 | replacing an older role | L10 added, L5 removed, unrelated role survives, both claims fulfilled exactly once |
| 3 | skipping multiple levels (L10 → L15 + L20 at once) | each skipped level keeps one fulfilled row; only **L20**'s role is held; L15's is gone |
| 4 | unrelated roles | manual/other-system roles and the current progression role untouched; a no-op claim pass removes nothing |
| 4b | a role id shared by two levels | never removed |
| 5 | failed **removal** | higher-role claim remains fulfilled; removal error is reported separately, old role remains, and a later reconciliation retries without replaying the claim |
| 5b | failed **add** | role claim stays retryable; nothing is removed while the add fails, and retry delivers it before superseding lower roles |
| 5c | partial same-Level **add** | successful and failed role claims stay independent; lower role remains until every role at the highest Level is present, then retry completes replacement |

**Regression risk of this slice**

| risk | mitigation |
|---|---|
| a role that is not a Level reward gets removed | removal set is derived from fulfilled `role` claims only; `member.roles` is consulted before removing, and members without a role list remove nothing |
| a member ends up with no role | the new role is added *before* any removal, inside the existing retryable-claim path |
| repeated runs re-remove or re-add | removals skip roles not held; `_mark(fulfilled)` is idempotent; a further claim pass reports `owned=0` |
| removing a lower role can fail (Discord) | reported as a reconciliation failure without reopening the fulfilled higher claim; join or a later full Claim All pass retries membership reconciliation |
| interference with other systems | only `track='role'` claims are read; Shop/Prestige/Missions/MVP/reaction roles/boosts untouched |
| existing tests | full Python suite + `npm test` re-run: slice 2 (role ownership/retry), slice 3 (Shop claims), slice 4 (boost claims), vi-lifecycle, phase2-backend, the S1 and reset/config harnesses all still pass. `test_slice1_leveling_gate.py` and `test_phase1_runtime.py` fail **identically on a pristine `HEAD` checkout** in this sandbox (missing `DISCORD_TOKEN`), i.e. unrelated to this change |

---

## 5. Decisions taken / remaining

**Historical interim decisions (2026-10-06; the toggle note was later superseded by §7):** S4 deferred; an earlier pacing pass described a 20 s cooldown; `remove_old_reward_role` was temporarily treated as dead and removed from the Dashboard before being restored as a live setting; `/resetleaderboard` remains UPDATE-only with a regression test.

**Still open**
1. **XP pacing target** — with cooldown 20 s the ~10-month L100 is reachable only at a saturating schedule. If a normal member should reach it, the level curve (or a new passive/voice source) has to change; not attempted.
2. **Anti-spam burst-zeroing** (above) — own decision, independent of pacing.
3. **`levelup_embed_data`** — no UI, no reader; left untouched deliberately.
4. **Dead claim-path code** — `check_and_award_level_rewards` still exists with no live caller since claim-based delivery; retire later or leave as documentation. (The `remove_old_reward_role` column is **not** dead any more — see §7.)
5. Small observation: nothing enforces `xp_min_per_message ≤ xp_max_per_message`; the clamp makes a min>max pair behave as "max wins".

---

## 6. Regression risk

| change | risk | mitigation / evidence |
|---|---|---|
| S1 IIFE + publishes (24 templates) | a future top-level function used from markup must be in the publish list | **now enforced** by `scripts/test_dashboard_page_scripts.py` (all 36 routes: single emission, containment, syntax, no top-level `const`/`let`, handler resolution) — 177 checks, mutation-tested; the run prints an explicit SKIPPED notice if node/acorn is missing, so a degraded run cannot pass as a full one |
| `get_leveling_config` returning defaults ⊕ row | code that relied on a NULL column being `None` now sees a number | it is the S3 fix; `levelup_channel_id`/`levelup_message` keep `None`; covered by the S2/S3 probes and the reset/config test |
| Cooldown default 30 → 20 | faster pacing for members who send more than 1 message per 20 s | no formula change; the anti-spam gate still runs first and the ceiling is still capped at 1,080 grants/day; stored guild rows keep their own value untouched |
| `remove_old_reward_role` is live again | ON replaces superseded Level roles after a successful higher-role delivery or qualifying reconciliation | default is OFF; persisted setting is read by the runtime; both modes and switch directions are covered by 46 progression checks and 18 join/reconciliation checks; only fulfilled `role` claims are considered for removal |
| Dashboard page-script lifecycle (item 2) | a page made inert by a future edit | enforced by the 177-check harness over all 36 routes, which asserts every inline markup handler still resolves and rejects a new top-level `const`/`let` |
| Anti-spam penalty now deducts XP (D1 = B) | a member can be demoted by spamming, and their rank/level changes without an XP grant | the deduction is floored at zero (no debt), the level is recomputed in the same transaction, and no entitlement/role/claim is touched — `scripts/test_spam_penalty.py` (31 checks, mutation-tested) proves each of those, including that re-earning cannot pay twice |
| Role membership reconciliation on rejoin / Claim All (D2 = B) | a fulfilled role removed by Discord membership loss is restored from persisted claims | join restores all fulfilled roles when OFF; Claim All / join restore the highest fulfilled set when ON; reconciliation never changes XP/Level or claims and has no sweep/background loop — `scripts/test_rejoin_reconciliation.py` |
| UPDATE-only reset-config write | none for configured guilds; unconfigured guilds now never receive an auto-reset config | regression test asserts both directions, plus that the reset itself (archive + zero) is unchanged |
| `list_claims` extra keys | additive only | slice-2 suite green; no engine code reads them |
| Stricter config validation | an external client sending `"30.5"` is rejected instead of half-saved | absent keys still default; bounds keep headroom over the form's `min`/`max` |
| Stats rework | layout change only; field length capped | 14/14 probe incl. the 1024-char limit |
| Untouched by design | XP formula except the cooldown default, level curve, per-message clamps, claim/entitlement engine, schema (no migration), `/rank`, Shop/Prestige, `base.html`, `dashboard.js`, `activity_engine` | — |

*Observation, not a defect:* four `GET /api/guild/roles` calls are issued on Leveling load because `nero-select.js` populates its options cache asynchronously — a one-request stampede per page load, not duplicate init, and pre-existing.

---

## 8. Second follow-up slice — Dashboard scripts, anti-spam audit, leave/rejoin, blacklist

Full audit for items 3–5, with the simulation output and the exact decisions needed:
**`LEVELING_FOLLOWUP_AUDIT.md`**.

| item | outcome | status |
|---|---|---|
| 1. `remove_old_reward_role` restored as a real toggle | see §7 — DB column kept, checkbox + hint in the Leveling Dashboard, API validated/written/returned; OFF = accumulate, ON = exclusive progression; 46/46 progression checks plus 18/18 join/reconciliation checks | **implemented** |
| 2. Dashboard script duplication — fix the **whole** Dashboard | audited every nav route: nested `{% block scripts %}` exists only in `base.html` (already fixed), and the four routes outside the S1 wrapper (`/backups`, `/tag-missions`, `/tag-partners`) declare **no** top-level `const`/`let`, so they were already re-evaluation safe; `/moderation` is a genuine IIFE inside the script, not an unwrapped leak. **No template change was needed and none was made.** The permanent harness was extended from the 24 rollout routes to **all 36** and now asserts the real invariant (single emission, containment in `#content-area`, syntax, no top-level `const`/`let`, and every inline markup handler still resolving) — 177 checks, mutation-tested | **complete, verified** |
| 3. Anti-spam penalty semantics (**D1 = B, implemented**) | `apply_spam_penalty()` (`utils/xp_calculator.py`): a spam message **deducts** XP from the member, floored at zero — never negative, never a debt — and `level` is recomputed from the resulting XP in the same transaction, so `levels.xp` and `levels.level` cannot disagree. A penalty may therefore demote a member (intended); that is not a crossing, so no entitlement is created, no fulfilled claim is revoked and no role is removed, and re-earning the level cannot pay twice. The old stale-level defect and its silent next-message demotion are gone. The approved anti-spam defaults (`spam_threshold=10`, `spam_window_seconds=20`, `spam_xp_penalty_divisor=1000`) are **unchanged** — only penalty semantics changed. The member gets one reply per spam window (not per message): an embed whose description is the approved text — `> مع كل احتراماتي لا تسبام <:brick:1556981905218478162>` / `> -# من لفلك نيهاهاها   XP تم خصم (≖⩊≖)` — with the XP actually deducted in the embed footer; it is a **reply to the offending message**, and a failure to send it can never roll the penalty back. | **implemented, 41/41** |
| 4. Leave/rejoin + missing-role reconciliation (**D2 = B, implemented**) | The member-join listener calls `reconcile_role_progression()` as a membership-only action; it never changes XP/Level, pays a claim, or writes the ledger. With `remove_old_reward_role` OFF, all fulfilled role claims are restored because roles accumulate; with it ON, only the highest fulfilled set is restored and lower fulfilled roles are removed after the set is present. Partial same-Level failures block replacement; unrelated roles are untouched. No sweep, timer, background task, or second reward engine. | **verified by `scripts/test_rejoin_reconciliation.py`** |
| 5. Blacklist boundary | The member blacklist system is **under construction and is not `leveling_blacklist_roles`** (that stays a role-based XP opt-out: message XP via `get_xp_multiplier`, voice XP via `is_role_blacklisted`). **Nothing was implemented** for it: no table, no command, no automatic restore/unrestore. The future invariant — *a blacklisted member must not have automatic reconciliation restore roles the blacklist intentionally removed* — is documented in the code at the two chokepoints (`deliver_role()` calls and the reconcile call in `claim_available`) and in the audit's §5. | **deferred by design** |

**Decisions — resolved on 2026-10-06 (second pass):**

* **D1 = (B) — implemented.** Penalty deducts real XP, floored at zero (no negative XP, no debt),
  with `level` recomputed in the same transaction. A penalty may legitimately demote the member;
  that creates no crossing, no entitlement, no claim change and no role change.
* **D2 = (B) — implemented.** `reconcile_role_progression()` is shared by the join callback and the
  existing Claim All path. OFF restores every fulfilled role claim because roles accumulate; ON
  restores the highest fulfilled set and removes lower roles only after that set is present.
* **D3 — deferred by design.** The member blacklist is still under construction; nothing was wired,
  and `leveling_blacklist_roles` was explicitly *not* treated as the member blacklist. The future
  invariant is recorded in the code and in the audit.
* **No retroactive guild-wide sweep** — reconciliation is per-member on join or on the qualifying
  full Claim All pass; no background task or guild-wide member pass was added.
* **D3 / member blacklist — still OPEN, by instruction.** No member blacklist exists in this repo
  (`leveling_blacklist_roles` is a role-based XP opt-out, and `utils/command_gating.py`'s role/
  channel blacklists govern command use). Nothing was implemented for it. The five bypass risks and
  the two chokepoints where it must be enforced are recorded in `LEVELING_FOLLOWUP_AUDIT.md` §5.5,
  including the invariant that a blacklisted member must never have a Level role restored by the
  automatic reconciliation.

**Not changed by this pass:** the `remove_old_reward_role` schema, Dashboard field/API validation, or
claim identity/status/idempotency semantics. D2 membership reconciliation follows the persisted
setting as described in §7; the four unwrapped templates remain unchanged (§8 item 2).

**Latest targeted verification for this branch:** `scripts/test_leveling_reward_e2e.py` 11/11;
`test_rejoin_reconciliation.py` 18/18; `test_level_reward_role_progression.py` 46/46;
`test_xp_safety_audit.py` 111/111; `test_leveling_reset_config.py` 17/17;
`test_leveling_config_migration.py` all 8 checks; slices 1–4, runtime, and all five XP caller
integration checks passed. The previously passing anti-spam penalty and dashboard/JS harnesses were
not changed by this E2E work. The earlier 39/42 progression result was a stale `.pyc` false alarm
from mutation testing; current source passes all 46 progression checks. Nothing committed or pushed.
